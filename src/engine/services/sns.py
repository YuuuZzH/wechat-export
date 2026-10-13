"""Offline Moments (朋友圈) reader — decrypt sns.db → parse Timeline XML.

Supports WeChat 4.x table names:
- SnsTimeLine(tid, user_name, content)
- FeedsV20 / similar Feeds* tables (discovered dynamically)

Read-only; no delete intercept / write-back.
"""
from __future__ import annotations

import os
import re
import sqlite3
import xml.etree.ElementTree as ET
from typing import Any, Optional

from engine.parsers.types import _detect_live_photo


def find_sns_db(decrypted_dir: str) -> Optional[str]:
    """Locate decrypted sns.db under a backup / decrypted account tree."""
    if not decrypted_dir:
        return None
    candidates = [
        os.path.join(decrypted_dir, 'sns', 'sns.db'),
        os.path.join(decrypted_dir, 'sns.db'),
    ]
    # Account subdir layout: decrypted/<wxid>/sns/sns.db
    if os.path.isdir(decrypted_dir):
        try:
            for name in os.listdir(decrypted_dir):
                p = os.path.join(decrypted_dir, name, 'sns', 'sns.db')
                candidates.append(p)
        except OSError:
            pass
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None


def _table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def _pick_timeline_table(conn: sqlite3.Connection) -> Optional[str]:
    names = _table_names(conn)
    for preferred in ('SnsTimeLine', 'SnsTimeline', 'sns_timeline'):
        if preferred in names:
            return preferred
    for n in names:
        if n.lower().startswith('feeds') and 'fts' not in n.lower():
            return n
    return None


def _text_of(node, path: str) -> str:
    if node is None:
        return ''
    child = node.find(path)
    if child is None:
        return ''
    return (child.text or '').strip()


def _safe_int(v) -> Optional[int]:
    try:
        if v is None or v == '':
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def parse_timeline_xml(xml_text: str) -> dict[str, Any]:
    """Parse a Moments TimelineObject XML blob into a structured post."""
    if not xml_text:
        return {
            'id': '', 'username': '', 'nickname': '', 'create_time': None,
            'content': '', 'content_style': '', 'media': [], 'links': [],
            'is_live_photo': False,
        }
    cleaned = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', xml_text)
    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError:
        # Last resort: wrap fragments
        try:
            root = ET.fromstring('<root>' + cleaned + '</root>')
        except ET.ParseError as exc:
            return {
                'id': '', 'username': '', 'nickname': '', 'create_time': None,
                'content': f'[无法解析 XML: {exc}]', 'content_style': '',
                'media': [], 'links': [], 'is_live_photo': False,
            }

    timeline = root.find('TimelineObject') if root.tag != 'TimelineObject' else root
    if timeline is None:
        timeline = root.find('.//TimelineObject') or root

    create_time = _safe_int(_text_of(timeline, 'createTime'))
    content = _text_of(timeline, 'contentDesc')
    post_id = _text_of(timeline, 'id')
    username = _text_of(timeline, 'username')
    nickname = _text_of(timeline, 'nickname')
    style = (
        _text_of(timeline, 'ContentObject/contentStyle')
        or _text_of(timeline, 'ContentObject/contentSubStyle')
    )

    enc_key = None
    enc = timeline.find('.//enc')
    if enc is not None and enc.get('key'):
        enc_key = enc.get('key')
    if not enc_key:
        m = re.search(r'<enc\s+key=["\'](\d+)["\']', cleaned, re.I)
        if m:
            enc_key = m.group(1)

    media: list[dict[str, Any]] = []
    any_live = False
    for item in timeline.findall('.//media'):
        url = _text_of(item, 'url') or (item.get('url') or '')
        thumb = _text_of(item, 'thumb') or (item.get('thumb') or '')
        token = _text_of(item, 'token') or (item.get('token') or '')
        key = _text_of(item, 'key') or (item.get('key') or '')
        mtype = _text_of(item, 'type') or (item.get('type') or '')
        live = _detect_live_photo(item, ET.tostring(item, encoding='unicode'))
        live_url = live.get('live_video_url')
        live_token = None
        live_node = item.find('livePhoto') or item.find('livephoto')
        if live_node is not None:
            live_token = _text_of(live_node, 'token') or live_node.get('token')
            if not live_url:
                live_url = _text_of(live_node, 'url') or live_node.get('url')
        if live.get('is_live_photo'):
            any_live = True
        video_key = live.get('live_enc_key') or enc_key or live.get('live_video_aeskey') or key
        media.append({
            'type': mtype,
            'url': url,
            'thumb': thumb,
            'token': token,
            'key': key,
            'is_live_photo': bool(live.get('is_live_photo')),
            'live_video_url': live_url,
            'live_video_token': live_token,
            'live_video_key': video_key,
            'enc_key': enc_key,
        })

    links = []
    for path in ('ContentObject/contentUrl', './/url'):
        for node in timeline.findall(path):
            t = (node.text or '').strip()
            if t and t.startswith('http'):
                links.append(t)

    return {
        'id': post_id,
        'username': username,
        'nickname': nickname,
        'create_time': create_time,
        'content': content,
        'content_style': style,
        'media': media,
        'links': sorted(set(links)),
        'is_live_photo': any_live,
        'enc_key': enc_key,
        'raw_xml': xml_text if len(xml_text) < 200000 else xml_text[:200000],
    }


def _row_content(cols: list[str], row: tuple) -> tuple[Any, str, str]:
    d = dict(zip(cols, row))
    tid = d.get('tid') or d.get('id') or d.get('feed_id') or d.get('feedId')
    user = d.get('user_name') or d.get('username') or d.get('userName') or ''
    content = d.get('content') or d.get('Content') or d.get('xml') or d.get('buffer') or ''
    if isinstance(content, bytes):
        # May be zstd — try plain utf-8 first
        if content[:4] == b'\x28\xb5\x2f\xfd':
            try:
                from engine.services.message.decode import get_zstd_decompressor
                content = get_zstd_decompressor().decompress(
                    content, max_output_size=20 * 1024 * 1024
                ).decode('utf-8', errors='replace')
            except Exception:
                content = content.decode('utf-8', errors='replace')
        else:
            content = content.decode('utf-8', errors='replace')
    return tid, str(user or ''), str(content or '')


def list_posts(
    decrypted_dir: str,
    *,
    username: str = '',
    keyword: str = '',
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """List Moments posts from decrypted sns.db."""
    sns_db = find_sns_db(decrypted_dir)
    if not sns_db:
        return {
            'ok': False,
            'error': '未找到解密后的 sns.db。请重新「一键备份」（会尝试解密朋友圈库），并确保密钥提取时微信已打开过朋友圈。',
            'posts': [],
            'total': 0,
            'sns_db': None,
        }

    try:
        conn = sqlite3.connect(sns_db)
        table = _pick_timeline_table(conn)
        if not table:
            names = _table_names(conn)
            conn.close()
            return {
                'ok': False,
                'error': 'sns.db 中未识别到朋友圈时间线表（期望 SnsTimeLine / Feeds*）',
                'tables': names,
                'posts': [],
                'total': 0,
                'sns_db': sns_db,
            }
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
        col_str = ', '.join(f'"{c}"' for c in cols)
        rows = conn.execute(f'SELECT {col_str} FROM "{table}"').fetchall()
        conn.close()
    except sqlite3.Error as e:
        return {
            'ok': False,
            'error': f'读取 sns.db 失败: {e}',
            'posts': [],
            'total': 0,
            'sns_db': sns_db,
        }

    uname_q = (username or '').strip().lower()
    kw = (keyword or '').strip().lower()
    posts: list[dict[str, Any]] = []
    for row in rows:
        tid, user, content = _row_content(cols, row)
        if not content:
            continue
        if uname_q and uname_q not in user.lower():
            continue
        try:
            post = parse_timeline_xml(content)
        except Exception as exc:
            post = {
                'id': str(tid), 'username': user, 'nickname': '',
                'create_time': None, 'content': f'[解析失败: {exc}]',
                'content_style': '', 'media': [], 'links': [],
                'is_live_photo': False,
            }
        post['tid'] = tid
        post['db_user_name'] = user
        if not post.get('username'):
            post['username'] = user
        ct = post.get('create_time')
        if start_ts and (ct is None or ct < start_ts):
            continue
        if end_ts and (ct is None or ct > end_ts):
            continue
        if kw:
            blob = (post.get('content') or '') + ' ' + ' '.join(post.get('links') or [])
            if kw not in blob.lower() and kw not in user.lower():
                continue
        # Drop huge raw xml from list payload
        post.pop('raw_xml', None)
        posts.append(post)

    posts.sort(key=lambda p: p.get('create_time') or 0, reverse=True)
    total = len(posts)
    limit = max(1, min(200, int(limit or 50)))
    offset = max(0, int(offset or 0))
    page = posts[offset:offset + limit]

    # Attach display names from contact.db when possible
    try:
        from engine.services.address_book import get_all_contacts
        cmap = {c['wxid']: c.get('display_name') or c['wxid'] for c in get_all_contacts(decrypted_dir)}
        for p in page:
            u = p.get('db_user_name') or p.get('username') or ''
            p['display_name'] = cmap.get(u) or p.get('nickname') or u
    except Exception:
        for p in page:
            p['display_name'] = p.get('nickname') or p.get('db_user_name') or p.get('username') or ''

    return {
        'ok': True,
        'sns_db': sns_db,
        'table': table,
        'posts': page,
        'total': total,
        'limit': limit,
        'offset': offset,
    }


def export_posts_json(decrypted_dir: str, out_path: str, **kwargs) -> dict[str, Any]:
    """Export Moments posts to a JSON file (no media download)."""
    import json
    kwargs.setdefault('limit', 5000)
    kwargs.setdefault('offset', 0)
    result = list_posts(decrypted_dir, **kwargs)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or '.', exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return {'path': out_path, 'count': result.get('total') or 0, 'ok': result.get('ok')}


def export_posts_html(decrypted_dir: str, out_path: str, **kwargs) -> dict[str, Any]:
    """Export a simple offline HTML timeline (CDN urls kept as links)."""
    kwargs.setdefault('limit', 500)
    kwargs.setdefault('offset', 0)
    result = list_posts(decrypted_dir, **kwargs)
    posts = result.get('posts') or []
    lines = [
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">',
        '<title>朋友圈导出 — WeChat Export</title>',
        '<style>body{font-family:sans-serif;max-width:720px;margin:24px auto;padding:0 16px;background:#f6f7f9;color:#222}',
        '.post{background:#fff;border:1px solid #ddd;border-radius:8px;padding:14px 16px;margin-bottom:14px}',
        '.meta{color:#888;font-size:12px;margin-bottom:8px}',
        '.content{white-space:pre-wrap;line-height:1.55}',
        '.media{color:#06c;font-size:12px;margin-top:8px}',
        '.live{display:inline-block;background:#e04570;color:#fff;font-size:11px;padding:1px 6px;margin-left:6px}',
        '</style></head><body>',
        f'<h1>朋友圈导出</h1><p>共 {result.get("total") or 0} 条（本页 {len(posts)}）</p>',
    ]
    from datetime import datetime
    for p in posts:
        ts = p.get('create_time')
        when = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M') if ts else '未知时间'
        live = ' <span class="live">实况</span>' if p.get('is_live_photo') else ''
        name = _html_esc(p.get('display_name') or p.get('username') or '')
        body = _html_esc(p.get('content') or '[无文字]')
        media_bits = []
        for i, m in enumerate(p.get('media') or [], 1):
            u = m.get('url') or ''
            tag = '实况' if m.get('is_live_photo') else '媒体'
            media_bits.append(f'<div>{tag}{i}: <a href="{_html_esc(u)}" target="_blank" rel="noopener">{_html_esc(u[:80])}</a></div>')
        lines.append(
            f'<article class="post"><div class="meta">{when} · {name}{live}</div>'
            f'<div class="content">{body}</div>'
            f'<div class="media">{"".join(media_bits)}</div></article>'
        )
    lines.append('</body></html>')
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or '.', exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    return {'path': out_path, 'count': len(posts), 'ok': result.get('ok')}


def _html_esc(s: str) -> str:
    return (s or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
