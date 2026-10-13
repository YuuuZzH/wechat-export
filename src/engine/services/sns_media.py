"""Resolve Moments media: prefer local cache under xwechat_files, else CDN + ISAAC.

Local paths often look like:
  <account>/cache/**/Sns/Img/**
  <account>/business/sns/**

CDN decrypt uses sns_isaac.xor_decrypt (images full-file; videos first 128KB).
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import urllib.error
import urllib.request
from typing import Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from engine.services.sns_isaac import looks_like_image, looks_like_video, xor_decrypt

_UA = 'MicroMessenger Client'
_CACHE_DIR_NAME = 'sns_media_cache'


def _account_roots(db_dir: Optional[str], decrypted_dir: Optional[str]) -> list[str]:
    roots = []
    for base in (db_dir, decrypted_dir):
        if not base:
            continue
        # db_storage → parent account folder
        p = os.path.abspath(base)
        roots.append(p)
        parent = os.path.dirname(p)
        if os.path.basename(p).lower() == 'db_storage':
            roots.append(parent)
        # decrypted/<wxid>
        if os.path.isdir(p):
            try:
                for name in os.listdir(p):
                    roots.append(os.path.join(p, name))
            except OSError:
                pass
    # dedupe
    out, seen = [], set()
    for r in roots:
        r = os.path.abspath(r)
        if r not in seen and os.path.isdir(r):
            seen.add(r)
            out.append(r)
    return out


def find_local_sns_file(url: str, db_dir: str = None, decrypted_dir: str = None) -> Optional[str]:
    """Best-effort find a local cached SNS file matching the CDN URL token/md5."""
    if not url:
        return None
    # Extract last path segment / md5-like token
    path = urlparse(url).path
    segs = [s for s in path.split('/') if s]
    needles = []
    for s in segs[-3:]:
        needles.append(s)
        m = re.search(r'([a-fA-F0-9]{16,64})', s)
        if m:
            needles.append(m.group(1))
    needles = [n for n in needles if len(n) >= 8]
    if not needles:
        return None

    search_roots = []
    for root in _account_roots(db_dir, decrypted_dir):
        for sub in ('cache', 'business', 'Sns', 'sns'):
            p = os.path.join(root, sub)
            if os.path.isdir(p):
                search_roots.append(p)
        # also search root itself shallowly
        search_roots.append(root)

    hits = []
    for root in search_roots:
        try:
            for dirpath, _dirnames, filenames in os.walk(root):
                # Cap walk depth / breadth for responsiveness
                rel = os.path.relpath(dirpath, root)
                if rel.count(os.sep) > 6:
                    continue
                lower_dir = dirpath.lower()
                if 'sns' not in lower_dir and 'cache' not in lower_dir and 'business' not in lower_dir:
                    # still allow if we're already under a sns-named root
                    if 'sns' not in root.lower() and 'cache' not in root.lower():
                        continue
                for fn in filenames:
                    fl = fn.lower()
                    for n in needles:
                        if n.lower() in fl:
                            full = os.path.join(dirpath, fn)
                            hits.append(full)
                            break
                if len(hits) >= 5:
                    break
        except OSError:
            continue
        if hits:
            break
    return hits[0] if hits else None


def build_cdn_url(url: str, token: str = '', *, is_video: bool = False) -> str:
    if not url:
        return ''
    fixed = url.replace('http://', 'https://')
    if not is_video:
        fixed = re.sub(r'/150($|\?)', r'/0\1', fixed)
    if not token:
        return fixed
    parsed = urlparse(fixed)
    q = parse_qs(parsed.query, keep_blank_values=True)
    # flatten
    flat = {k: (v[0] if isinstance(v, list) else v) for k, v in q.items()}
    if is_video:
        # token must be first
        items = [('token', token), ('idx', flat.get('idx') or '1')]
        for k, v in flat.items():
            if k in ('token', 'idx'):
                continue
            items.append((k, v))
        query = urlencode(items)
    else:
        flat['token'] = token
        flat.setdefault('idx', '1')
        query = urlencode(flat)
    return urlunparse(parsed._replace(query=query))


def is_video_url(url: str) -> bool:
    u = (url or '').lower()
    if 'vweixinthumb' in u:
        return False
    return ('snsvideodownload' in u) or ('/video' in u) or u.endswith('.mp4')


def download_cdn(url: str, token: str = '', *, is_video: bool = False, timeout: int = 30):
    full = build_cdn_url(url, token, is_video=is_video)
    headers = {
        'User-Agent': _UA,
        'Accept': '*/*',
        'Connection': 'keep-alive',
    }
    if not is_video:
        headers['Accept-Encoding'] = 'gzip, deflate'
    req = urllib.request.Request(full, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        # urllib may auto-decompress gzip; fine
        enc = (resp.headers.get('x-Enc') or resp.headers.get('X-Enc') or '').strip()
        return data, enc == '1'


def fetch_and_decrypt(
    url: str,
    *,
    token: str = '',
    key: str = '',
    is_video: bool = None,
    db_dir: str = None,
    decrypted_dir: str = None,
    cache_dir: str = None,
) -> dict:
    """Return {ok, path|bytes, content_type, source, error}."""
    if is_video is None:
        is_video = is_video_url(url)

    local = find_local_sns_file(url, db_dir=db_dir, decrypted_dir=decrypted_dir)
    if local and os.path.isfile(local):
        return {
            'ok': True,
            'path': local,
            'source': 'local_cache',
            'is_video': is_video,
            'content_type': 'video/mp4' if is_video else 'image/jpeg',
        }

    if not url:
        return {'ok': False, 'error': '缺少媒体 URL'}

    cache_root = cache_dir or os.path.join(decrypted_dir or tempfile.gettempdir(), _CACHE_DIR_NAME)
    os.makedirs(cache_root, exist_ok=True)
    digest = hashlib.sha1(f'{url}|{token}|{key}|{int(bool(is_video))}'.encode('utf-8')).hexdigest()
    ext = 'mp4' if is_video else 'jpg'
    cache_path = os.path.join(cache_root, f'{digest}.{ext}')
    if os.path.isfile(cache_path) and os.path.getsize(cache_path) > 0:
        return {
            'ok': True,
            'path': cache_path,
            'source': 'cdn_cache',
            'is_video': is_video,
            'content_type': 'video/mp4' if is_video else 'image/jpeg',
        }

    try:
        raw, needs_enc = download_cdn(url, token, is_video=is_video)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {'ok': False, 'error': f'CDN 下载失败: {e}'}

    data = raw
    if needs_enc or key:
        data = xor_decrypt(raw, key, video=bool(is_video))

    if is_video and not looks_like_video(data) and key:
        # try again forcing decrypt even if header missing
        data = xor_decrypt(raw, key, video=True)
    if (not is_video) and not looks_like_image(data) and key:
        data = xor_decrypt(raw, key, video=False)

    try:
        with open(cache_path, 'wb') as f:
            f.write(data)
    except OSError as e:
        return {'ok': False, 'error': f'写入缓存失败: {e}'}

    return {
        'ok': True,
        'path': cache_path,
        'source': 'cdn',
        'is_video': is_video,
        'content_type': 'video/mp4' if is_video else 'image/jpeg',
        'decrypted': bool(key),
    }
