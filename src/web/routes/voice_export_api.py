# -*- coding: utf-8 -*-
"""语音导出 HTTP 接口。

* ``GET  /api/export/voice/status``        能力探测（wav / mp3 / m4a / ffmpeg）
* ``GET  /api/export/voice/senders``       某会话的发送者列表（含条数，来自统计口径）
* ``POST /api/export/voice``               SSE 导出（阶段进度 + 完成事件带 zip 下载地址）
* ``GET  /api/export/voice/download/<zip>`` 下载打包好的导出目录

与其它导出路由一致：后台线程跑，SSE 推进度；产物落在 ``<程序目录>/export/voice/``。
"""

import os
import sys
import threading

from flask import Blueprint, current_app, jsonify, request, send_from_directory

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from web.sse import create_sse_progress  # noqa: E402

voice_export_bp = Blueprint('voice_export_api', __name__, url_prefix='/api/export/voice')

if getattr(sys, 'frozen', False):
    _DATA_ROOT = os.path.dirname(sys.executable)
else:
    _DATA_ROOT = os.path.normpath(os.path.join(_BASE, '..'))

_OUT_ROOT = os.path.join(_DATA_ROOT, 'export', 'voice')

_LAYOUTS = ('html-folder', 'html-inline', 'files', 'merged')

#: 同一时刻只允许一个语音导出。并发导出会同时扫库/解压/编码：
#: 既浪费 IO，也踩到了共享 zstd 上下文的线程安全问题（真机"连点两次"曾把进程打崩）。
_EXPORT_BUSY = threading.Lock()


def _decrypted_dir():
    return current_app.config.get('DECRYPTED_DIR', '')


@voice_export_bp.route('/status')
def voice_status():
    """界面据此决定 M4A 是否可选、是否提示安装 ffmpeg。"""
    from engine.services.voice_export import capabilities
    caps = capabilities()
    caps['ffmpeg_name'] = os.path.basename(caps.get('ffmpeg') or '')
    return jsonify(caps)


@voice_export_bp.route('/senders')
def voice_senders():
    """某会话里的发送者（键 = 筛选用的 sender 值，名字已解析）。"""
    chat = (request.args.get('chat') or '').strip()
    decrypted = _decrypted_dir()
    own_wxid = current_app.config.get('WXID') or ''
    if not chat or not decrypted:
        return jsonify({'senders': []})
    try:
        from engine.services.message import get_chat_stats
        stats = get_chat_stats(decrypted, chat, wxid=own_wxid)
        dist = stats.get('sender_distribution') or {}
        senders = [{'key': str(key), 'name': (value or {}).get('name') or str(key),
                    'count': int((value or {}).get('count') or 0)}
                   for key, value in dist.items()]
        senders.sort(key=lambda s: -s['count'])
        return jsonify({'senders': senders})
    except Exception as exc:                     # 统计失败不该把页面打挂
        return jsonify({'senders': [], 'error': str(exc)})


def _do_export(opts, push, cancel):
    """跑一次导出（独立函数，便于测试 monkeypatch）。"""
    from engine.services.voice_export import export_voices
    report = export_voices(
        opts['decrypted_dir'], _OUT_ROOT,
        chats=opts.get('chats'), senders=opts.get('senders'),
        start_ts=opts.get('start_ts'), end_ts=opts.get('end_ts'),
        include_other_chats=opts.get('include_other_chats', False),
        own_wxid=opts.get('own_wxid', ''), name_lookup=opts.get('name_lookup'),
        fmt=opts.get('fmt') or 'mp3', layouts=tuple(opts.get('layouts') or _LAYOUTS),
        merge_by=opts.get('merge_by') or 'person', split=opts.get('split') or 'single',
        gap_s=float(opts.get('gap_s') or 1.0), keep_silk=bool(opts.get('keep_silk')),
        workers=int(opts.get('workers') or 4),
        mp3_quality=int(opts.get('mp3_quality') or 7),
        include_asr=bool(opts.get('include_asr')),
        zip_output=bool(opts.get('zip_output', True)),
        progress_fn=lambda stage, message: push(stage, message, 0.5),
        cancel=lambda: bool(cancel.get('stop')))
    return report


@voice_export_bp.route('', methods=['POST'], strict_slashes=False)
def voice_export():
    data = request.get_json(silent=True) or {}
    chat_text = (data.get('chat') or '').strip()
    senders = [str(s) for s in (data.get('senders') or []) if str(s).strip()]
    if not chat_text and not senders:
        return jsonify({'error': 'bad_request', 'message': '至少要选择会话或发送者'}), 400
    decrypted = _decrypted_dir()
    if not decrypted or not os.path.isdir(decrypted):
        return jsonify({'error': 'no_data',
                        'message': '还没有解密/备份数据，请先执行「一键备份」'}), 400

    push, generate = create_sse_progress()
    own_wxid = current_app.config.get('WXID') or ''

    # 先做参数/会话解析（这两步可能提前返回，不能占着并发锁）
    chat_id = ''
    if chat_text:
        from engine.services.voice_export.collect import resolve_chat_target
        resolved, matches = resolve_chat_target(decrypted, chat_text, own_wxid)
        if matches:
            push.select([{'username': m['username'], 'display_name': m['display_name'],
                          'msg_count': m.get('msg_count', 0)} for m in matches])
            return generate()
        if not resolved:
            return jsonify({'error': 'chat_not_found',
                            'message': '未找到会话：%s' % chat_text}), 404
        chat_id = resolved

    # 会话可以填 wxid **或显示名**（页面/搜索索引里常见的是显示名）——必须先解析成真实
    # chat_id 再进采集层：消息表名是 md5(wxid)，拿显示名比对会一条都收不到。
    if not _EXPORT_BUSY.acquire(blocking=False):
        return jsonify({'error': 'busy',
                        'message': '已有一个语音导出正在进行中，请等它完成（或点「停止」取消）后再试。'
                        }), 409

    cancel = {'stop': False}
    opts = {
        'decrypted_dir': decrypted,
        'chats': [chat_id] if chat_id else None,
        'senders': senders or None,
        'start_ts': data.get('start_ts'),
        'end_ts': data.get('end_ts'),
        'include_other_chats': bool(data.get('include_other_chats')),
        'own_wxid': own_wxid,
        'name_lookup': current_app.config.get('NAME_LOOKUP'),
        'fmt': data.get('format') or 'mp3',
        'layouts': data.get('layouts'),
        'merge_by': data.get('merge_by'),
        'split': data.get('split'),
        'gap_s': data.get('gap_s'),
        'keep_silk': data.get('keep_silk'),
        'workers': data.get('workers'),
        'mp3_quality': data.get('mp3_quality'),
        'include_asr': data.get('include_asr'),
        'zip_output': data.get('zip_output', True),
    }

    def _worker():
        try:
            report = _do_export(opts, push, cancel)
            zip_name = os.path.basename(report.get('zip') or '')
            push.done({
                'count': report.get('count', 0),
                'missing': report.get('missing', 0),
                'duration_total_s': round(report.get('duration_total_s', 0.0), 1),
                'dir': os.path.basename(report.get('out_dir', '')),
                'filename': zip_name,
                'zip_url': ('/api/export/voice/download/' + zip_name) if zip_name else '',
                'merged': report.get('merged', []),
                'errors': report.get('errors', []),
            })
        except Exception as exc:
            push.error(str(exc))
        finally:
            _EXPORT_BUSY.release()

    threading.Thread(target=_worker, daemon=True).start()
    return generate()


@voice_export_bp.route('/download/<path:name>')
def voice_download(name):
    """下载导出目录的 zip（只允许根目录下的文件，防目录穿越）。"""
    safe = os.path.basename(name)
    return send_from_directory(os.path.normpath(_OUT_ROOT), safe, as_attachment=True)
