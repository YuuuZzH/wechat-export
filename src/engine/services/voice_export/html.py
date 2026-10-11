# -*- coding: utf-8 -*-
"""生成可**脱离本程序单独打开**的 HTML（零外部依赖：不引用 CDN、不发网络请求）。

两种形态：

* :func:`write_html_folder` —— 文件夹式：``index.html`` + 相对路径引用 ``audio/``；
* :func:`write_html_inline` —— 单文件内嵌：音频 base64 内嵌，一个文件即可发给家人。

页面按人分组，每条显示时间/时长/来源会话/文字稿，可播放、可下载；
若生成了合并文件，会在"合并音频"区列出，并给每条一个 ``#t=偏移`` 的定位链接。
"""

import base64
import html as _h
import os

CSS = """body{font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;background:#111418;
color:#e6edf3;margin:0;padding:24px}h1{font-size:20px;margin:0 0 4px}
h2{font-size:16px;margin:28px 0 8px;border-bottom:1px solid #30363d;padding-bottom:6px}
.row{display:flex;gap:10px;align-items:center;padding:6px 0;border-bottom:1px solid #21262d;
flex-wrap:wrap}.t{color:#8b949e;font-size:12px;min-width:150px}.d{color:#8b949e;font-size:12px;
min-width:48px}.c{color:#8b949e;font-size:12px}.tx{color:#7ee787;font-size:12px}
audio{height:32px}#q{margin:12px 0;padding:6px 10px;width:280px;background:#0d1117;color:#e6edf3;
border:1px solid #30363d;border-radius:6px}a{color:#58a6ff;font-size:12px;text-decoration:none}
p.hint{color:#8b949e;font-size:12px}"""

JS = """function flt(v){v=(v||'').toLowerCase();document.querySelectorAll('.row').forEach(
function(r){r.style.display=(r.dataset.s||'').toLowerCase().indexOf(v)>=0?'':'none';});}"""


def _row(item, src, merged_file='', src_is_merged=False):
    """一条语音的 HTML。``src`` 可为相对路径或 data URI。"""
    jump = ''
    if merged_file and item.offset_start and not src_is_merged:
        jump = ('<a href="%s#t=%.1f" title="在合并文件中定位">定位</a>'
                % (_h.escape(merged_file), item.offset_start))
    transcript = ('<span class="tx">%s</span>' % _h.escape(item.transcript)) if item.transcript else ''
    data = ' '.join([item.datetime_text, item.chat_name, item.transcript])
    audio = ('<audio controls preload="none" src="%s"></audio>' % _h.escape(src)) if src else \
        '<span class="d">（音频缺失）</span>'
    download = ('<a href="%s" download>下载</a>' % _h.escape(src)) if src else ''
    return ('<div class="row" data-s="%s"><span class="t">%s</span><span class="d">%.1fs</span>'
            '%s<span class="c">%s</span>%s%s%s</div>'
            % (_h.escape(data), _h.escape(item.datetime_text), item.duration_s or 0.0,
               audio, _h.escape(item.chat_name), download, jump, transcript))


def build_html(items, title, src_of, merged=None, merged_of=None):
    """``src_of(item) -> str`` 给出音频地址；``merged_of(item) -> str`` 给出该条所在的合并文件。"""
    merged_of = merged_of or (lambda item: '')
    groups = {}
    for item in items:
        groups.setdefault(item.sender_name or '未知', []).append(item)
    total_s = sum(item.duration_s or 0.0 for item in items)
    parts = ['<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             '<title>%s</title><style>%s</style></head><body>' % (_h.escape(title), CSS),
             '<h1>%s</h1>' % _h.escape(title),
             '<p class="hint">共 %d 条 · 总时长 %.1f 分钟 · 本页面完全离线，双击即可打开</p>'
             % (len(items), total_s / 60.0),
             '<input id="q" placeholder="过滤：时间 / 会话 / 文字稿关键字" oninput="flt(this.value)">']
    if merged:
        parts.append('<h2>合并音频</h2>')
        for entry in merged:
            group = entry.get('group')
            label = _h.escape(getattr(group, 'file_stem', '') or entry.get('file', ''))
            parts.append('<div class="row"><span class="t">%s</span><span class="d">%.1fs</span>'
                         '<audio controls preload="none" src="%s"></audio>'
                         '<a href="%s" download>下载</a></div>'
                         % (label, entry.get('duration_s', 0.0), _h.escape(entry['file']),
                            _h.escape(entry['file'])))
    for name in sorted(groups):
        group_items = sorted(groups[name], key=lambda i: i.create_time)
        span = sum(i.duration_s or 0.0 for i in group_items)
        parts.append('<h2>%s（%d 条 · %.1f 分钟）</h2>'
                     % (_h.escape(name), len(group_items), span / 60.0))
        for item in group_items:
            merged_file = merged_of(item)
            individual = src_of(item)
            if individual:
                parts.append(_row(item, individual, merged_file=merged_file))
            elif merged_file:
                # 只导出了合并文件：直接指向合并文件里的**时间点**（浏览器支持 #t= 片段）
                parts.append(_row(item, '%s#t=%.1f' % (merged_file, item.offset_start),
                                  merged_file=merged_file, src_is_merged=True))
            else:
                parts.append(_row(item, ''))
    parts.append('<script>%s</script></body></html>' % JS)
    return ''.join(parts)


def write_html_folder(items, out_dir, title, layouts=None, merged=None):
    """文件夹式：``index.html`` + 相对路径音频。"""
    file_of = {}
    for lay in (layouts or []):
        item = lay.get('item')
        if item is not None:
            file_of[id(item)] = lay.get('file', '')
    merged_of_map = {}
    for entry in (merged or []):
        for item in entry.get('items', []):
            merged_of_map[id(item)] = entry.get('file', '')
    path = os.path.join(out_dir, 'index.html')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(build_html(items, title, lambda it: file_of.get(id(it), ''),
                           merged=merged, merged_of=lambda it: merged_of_map.get(id(it), '')))
    return path


def write_html_inline(items, out_path, title, encoder=None, max_mb=200.0,
                      audio_files=None, pcm_cache=None):
    """单文件内嵌：音频 base64 内嵌（体积超限时抛 ValueError，提示改用文件夹式）。

    ``audio_files``：``{id(item): 已写出的音频文件绝对路径}``。给了就直接复用这些文件
    （**不再解码/编码一遍** —— 真机测量：不复用时默认布局每条语音会被多解码一次）。
    """
    estimate_mb = sum((i.duration_s or 0.0) for i in items) * 0.008 * 1.34
    if estimate_mb > max_mb:
        raise ValueError('预计内嵌体积约 %.0f MB，超过上限 %.0f MB；请改用文件夹式 HTML'
                         % (estimate_mb, max_mb))
    if encoder is None:
        encoder = _make_data_uri_encoder(audio_files or {}, pcm_cache)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(build_html(items, title, encoder))
    return out_path


_MIME_BY_EXT = {'.mp3': 'audio/mpeg', '.m4a': 'audio/mp4', '.aac': 'audio/aac',
                '.wav': 'audio/wav', '.ogg': 'audio/ogg'}


def _make_data_uri_encoder(audio_files, pcm_cache=None):
    """优先复用已写出的音频文件，其次用 PCM 缓存（只解码一次），最后才重新解码。"""
    def encode(item):
        path = audio_files.get(id(item))
        if path and os.path.isfile(path):
            ext = os.path.splitext(path)[1].lower()
            mime = _MIME_BY_EXT.get(ext, 'audio/mpeg')
            try:
                with open(path, 'rb') as f:
                    data = f.read()
                return 'data:%s;base64,%s' % (mime, base64.b64encode(data).decode('ascii'))
            except OSError:
                return ''
        return _default_inline_encoder(item, pcm_cache)
    return encode


def _default_inline_encoder(item, pcm_cache=None):
    """内嵌编码器：PCM（缓存优先）→ MP3 → data URI（失败返回空串，页面显示"音频缺失"）。"""
    from . import audio
    from .layout import _pcm_of
    pcm = _pcm_of(item, pcm_cache)
    if not pcm:
        return ''
    tmp = (item.silk_path or 'inline') + '.inline.mp3'
    got = audio.encode_mp3(pcm, tmp, item.sample_rate)
    if not got:
        return ''
    try:
        with open(got, 'rb') as f:
            data = f.read()
        return 'data:audio/mpeg;base64,' + base64.b64encode(data).decode('ascii')
    finally:
        try:
            os.remove(got)
        except OSError:
            pass
