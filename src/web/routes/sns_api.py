"""Moments (朋友圈) HTTP API — read-only browse + export."""
from __future__ import annotations

import os
import threading
from datetime import datetime

from flask import Blueprint, current_app, jsonify, request, send_file

from web.sse import create_sse_progress, sse_response

sns_bp = Blueprint('sns_api', __name__, url_prefix='/api/sns')


def _decrypted_dir() -> str:
    return current_app.config.get('DECRYPTED_DIR') or ''


def _db_dir() -> str:
    return current_app.config.get('DB_DIR') or ''


def _out_dir() -> str:
    base = _decrypted_dir() or os.getcwd()
    out = os.path.join(base, 'exports', 'sns')
    os.makedirs(out, exist_ok=True)
    return out


@sns_bp.route('/status')
def sns_status():
    from engine.services.sns import find_sns_db
    path = find_sns_db(_decrypted_dir())
    return jsonify({
        'ok': True,
        'available': bool(path),
        'sns_db': path,
        'hint': None if path else '备份中未包含 sns.db，请重新备份并确保密钥覆盖朋友圈库',
    })


@sns_bp.route('/posts')
def sns_posts():
    from engine.services.sns import list_posts

    def _ts(name):
        raw = (request.args.get(name) or '').strip()
        if not raw:
            return None
        if raw.isdigit():
            return int(raw)
        for fmt in ('%Y-%m-%d', '%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S'):
            try:
                return int(datetime.strptime(raw, fmt).timestamp())
            except ValueError:
                continue
        return None

    try:
        limit = int(request.args.get('limit') or 50)
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = int(request.args.get('offset') or 0)
    except (TypeError, ValueError):
        offset = 0

    result = list_posts(
        _decrypted_dir(),
        username=request.args.get('username') or '',
        keyword=request.args.get('q') or request.args.get('keyword') or '',
        start_ts=_ts('start'),
        end_ts=_ts('end'),
        limit=limit,
        offset=offset,
    )
    status = 200 if result.get('ok') else 404
    return jsonify(result), status


@sns_bp.route('/media')
def sns_media():
    """Fetch one Moments media item (local cache or CDN+ISAAC)."""
    from engine.services.sns_media import fetch_and_decrypt

    url = request.args.get('url') or ''
    token = request.args.get('token') or ''
    key = request.args.get('key') or ''
    is_video = (request.args.get('video') or '').lower() in ('1', 'true', 'yes')
    if not url:
        return jsonify({'ok': False, 'error': '缺少 url'}), 400

    result = fetch_and_decrypt(
        url,
        token=token,
        key=key,
        is_video=is_video if request.args.get('video') is not None else None,
        db_dir=_db_dir(),
        decrypted_dir=_decrypted_dir(),
        cache_dir=os.path.join(_out_dir(), 'media_cache'),
    )
    if not result.get('ok'):
        return jsonify(result), 404
    path = result.get('path')
    if not path or not os.path.isfile(path):
        return jsonify({'ok': False, 'error': '文件不存在'}), 404
    return send_file(
        path,
        mimetype=result.get('content_type') or 'application/octet-stream',
        conditional=True,
        max_age=3600,
    )


@sns_bp.route('/export', methods=['POST'])
def sns_export():
    """POST JSON: {format: json|html, username?, q?, start?, end?, limit?} — SSE."""
    data = request.get_json(silent=True) or {}
    fmt = (data.get('format') or 'html').strip().lower()
    if fmt not in ('json', 'html'):
        return jsonify({'ok': False, 'error': 'format 仅支持 json / html'}), 400

    decrypted = _decrypted_dir()
    out_dir = _out_dir()
    push, gen = create_sse_progress()

    def _run():
        try:
            from engine.services.sns import export_posts_html, export_posts_json
            push('export', '正在导出朋友圈…', 0.2)
            stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            kwargs = {
                'username': data.get('username') or '',
                'keyword': data.get('q') or data.get('keyword') or '',
                'limit': int(data.get('limit') or 500),
                'offset': 0,
            }
            if fmt == 'json':
                path = os.path.join(out_dir, f'sns_{stamp}.json')
                res = export_posts_json(decrypted, path, **kwargs)
            else:
                path = os.path.join(out_dir, f'sns_{stamp}.html')
                res = export_posts_html(decrypted, path, **kwargs)
            if not res.get('ok') and res.get('count', 0) == 0:
                # still allow empty export file if list failed
                push.error(res.get('error') or '导出失败（可能缺少 sns.db）')
                return
            fname = os.path.basename(path)
            push.done({
                'filename': fname,
                'download_url': '/api/sns/download/' + fname,
                'count': res.get('count') or 0,
            })
        except Exception as e:
            push.error(str(e))

    threading.Thread(target=_run, daemon=True).start()
    return sse_response(gen)


@sns_bp.route('/download/<path:filename>')
def sns_download(filename):
    if '..' in filename or filename.startswith('/') or '\\' in filename:
        return jsonify({'error': 'bad filename'}), 400
    return send_file(os.path.join(_out_dir(), filename), as_attachment=True)
