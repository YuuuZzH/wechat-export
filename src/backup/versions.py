# -*- coding: utf-8 -*-
"""Backup version registry: snapshots, diff, activate (rollback).

Each successful backup directory may contain:
  data/version_meta.json   — identity + stats
  data/chats_snapshot.json — lightweight chat list for diffs

Working set is whichever path ``set_backup_data_dir`` points to; "rollback"
just re-points config (+ live Flask DECRYPTED_DIR when provided).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime
from typing import Any, Optional

META_REL = os.path.join('data', 'version_meta.json')
SNAPSHOT_REL = os.path.join('data', 'chats_snapshot.json')


def version_stamp(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).strftime('%Y-%m-%d_%H%M%S')


def default_version_output_dir(base_backup_root: str, now: Optional[datetime] = None) -> str:
    """``<root>/YYYY-MM-DD_HHMMSS`` — one folder per run (no same-day overwrite)."""
    return os.path.join(base_backup_root, version_stamp(now))


def _atomic_write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_json(path: str) -> Optional[dict]:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def is_backup_dir(path: str) -> bool:
    return bool(path) and os.path.isdir(os.path.join(path, 'message'))


def snapshot_chats(backup_dir: str) -> list[dict[str, Any]]:
    """Read chat rows from data/chats.db (preferred) for a lightweight snapshot."""
    chats_db = os.path.join(backup_dir, 'data', 'chats.db')
    if not os.path.isfile(chats_db):
        return []
    out: list[dict[str, Any]] = []
    try:
        conn = sqlite3.connect(chats_db)
        cols = {r[1] for r in conn.execute('PRAGMA table_info(chats)').fetchall()}
        if 'chat_id' not in cols:
            conn.close()
            return []
        select = 'chat_id, display_name, message_count, last_msg_time, is_group'
        # tolerate older schemas missing columns
        want = [c.strip() for c in select.split(',')]
        use = [c for c in want if c in cols]
        rows = conn.execute(
            'SELECT ' + ', '.join(use) + ' FROM chats'
        ).fetchall()
        conn.close()
        for row in rows:
            d = dict(zip(use, row))
            cid = (d.get('chat_id') or '').strip()
            if not cid:
                continue
            out.append({
                'chat_id': cid,
                'display_name': d.get('display_name') or cid,
                'message_count': int(d.get('message_count') or 0),
                'last_msg_time': d.get('last_msg_time'),
                'is_group': bool(d.get('is_group') or cid.endswith('@chatroom')),
            })
    except sqlite3.Error:
        return []
    out.sort(key=lambda c: (-(c.get('message_count') or 0), c.get('display_name') or ''))
    return out


def load_chats_snapshot(backup_dir: str) -> list[dict[str, Any]]:
    data = _read_json(os.path.join(backup_dir, SNAPSHOT_REL))
    if data and isinstance(data.get('chats'), list):
        return [c for c in data['chats'] if isinstance(c, dict) and c.get('chat_id')]
    # Lazy build from chats.db for older backups
    return snapshot_chats(backup_dir)


def load_version_meta(backup_dir: str) -> Optional[dict[str, Any]]:
    meta = _read_json(os.path.join(backup_dir, META_REL))
    if meta:
        meta.setdefault('path', os.path.abspath(backup_dir))
        return meta
    if not is_backup_dir(backup_dir):
        return None
    # Synthesize meta for pre-versioning backups
    chats = load_chats_snapshot(backup_dir)
    try:
        mtime = int(os.path.getmtime(backup_dir))
    except OSError:
        mtime = 0
    return {
        'version': 1,
        'id': os.path.basename(os.path.abspath(backup_dir)),
        'created_at': mtime,
        'path': os.path.abspath(backup_dir),
        'wxid': '',
        'chat_count': len(chats),
        'message_total': sum(int(c.get('message_count') or 0) for c in chats),
        'legacy': True,
    }


def register_version(
    backup_dir: str,
    *,
    wxid: str = '',
    stats: Optional[dict] = None,
    parent_path: Optional[str] = None,
) -> dict[str, Any]:
    """Write version_meta + chats_snapshot after a successful backup."""
    backup_dir = os.path.abspath(backup_dir)
    chats = snapshot_chats(backup_dir)
    _atomic_write_json(os.path.join(backup_dir, SNAPSHOT_REL), {
        'version': 1,
        'generated_at': int(time.time()),
        'chats': chats,
    })
    parent_id = None
    if parent_path and is_backup_dir(parent_path):
        parent_meta = load_version_meta(parent_path)
        if parent_meta:
            parent_id = parent_meta.get('id')
    meta = {
        'version': 1,
        'id': os.path.basename(backup_dir),
        'created_at': int(time.time()),
        'path': backup_dir,
        'wxid': wxid or '',
        'parent_id': parent_id,
        'parent_path': os.path.abspath(parent_path) if parent_path else None,
        'stats': stats or {},
        'chat_count': len(chats),
        'message_total': sum(int(c.get('message_count') or 0) for c in chats),
        'legacy': False,
    }
    _atomic_write_json(os.path.join(backup_dir, META_REL), meta)
    return meta


def discover_version_roots(hint_dir: Optional[str] = None) -> list[str]:
    """Find directories that contain backup versions (parents of backup roots)."""
    roots: list[str] = []
    seen = set()

    def _add(p: str):
        p = os.path.abspath(p)
        if p and p not in seen and os.path.isdir(p):
            seen.add(p)
            roots.append(p)

    if hint_dir:
        hint = os.path.abspath(hint_dir)
        if is_backup_dir(hint):
            _add(os.path.dirname(hint))
        else:
            _add(hint)

    try:
        from engine.config_file import get_backup_data_dir
        cur = get_backup_data_dir()
        if cur and is_backup_dir(cur):
            _add(os.path.dirname(os.path.abspath(cur)))
    except Exception:
        pass

    # Project / exe default backup folder
    try:
        import sys
        if getattr(sys, 'frozen', False):
            _add(os.path.join(os.path.dirname(sys.executable), 'backup'))
        else:
            here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            _add(os.path.join(here, 'backup'))
            _add(os.path.join(os.path.dirname(here), 'backup'))
    except Exception:
        pass
    return roots


def list_versions(
    base_dir: Optional[str] = None,
    *,
    wxid: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List backup versions under one or more parent directories."""
    parents = [base_dir] if base_dir else discover_version_roots()
    items: list[dict[str, Any]] = []
    seen_paths = set()
    for parent in parents:
        if not parent or not os.path.isdir(parent):
            continue
        # parent itself may be a backup (flat layout)
        candidates = []
        if is_backup_dir(parent):
            candidates.append(parent)
        try:
            for name in os.listdir(parent):
                full = os.path.join(parent, name)
                if is_backup_dir(full):
                    candidates.append(full)
        except OSError:
            continue
        for full in candidates:
            ab = os.path.abspath(full)
            if ab in seen_paths:
                continue
            seen_paths.add(ab)
            meta = load_version_meta(ab)
            if not meta:
                continue
            if wxid and meta.get('wxid') and meta.get('wxid') != wxid:
                continue
            items.append(meta)

    items.sort(key=lambda m: int(m.get('created_at') or 0), reverse=True)
    return items[: max(1, min(200, int(limit or 50)))]


def diff_versions(old_dir: str, new_dir: str) -> dict[str, Any]:
    """Compare two backups: added/removed chats and message-count deltas."""
    old_dir = os.path.abspath(old_dir)
    new_dir = os.path.abspath(new_dir)
    if not is_backup_dir(old_dir):
        return {'ok': False, 'error': f'旧版本不是有效备份: {old_dir}'}
    if not is_backup_dir(new_dir):
        return {'ok': False, 'error': f'新版本不是有效备份: {new_dir}'}

    old_chats = {c['chat_id']: c for c in load_chats_snapshot(old_dir)}
    new_chats = {c['chat_id']: c for c in load_chats_snapshot(new_dir)}

    added_ids = sorted(set(new_chats) - set(old_chats))
    removed_ids = sorted(set(old_chats) - set(new_chats))
    common = set(old_chats) & set(new_chats)

    grown, shrunk = [], []
    msg_delta = 0
    for cid in common:
        o = int(old_chats[cid].get('message_count') or 0)
        n = int(new_chats[cid].get('message_count') or 0)
        d = n - o
        msg_delta += d
        entry = {
            'chat_id': cid,
            'display_name': new_chats[cid].get('display_name') or cid,
            'is_group': bool(new_chats[cid].get('is_group')),
            'old_count': o,
            'new_count': n,
            'delta': d,
        }
        if d > 0:
            grown.append(entry)
        elif d < 0:
            shrunk.append(entry)
    grown.sort(key=lambda x: -x['delta'])
    shrunk.sort(key=lambda x: x['delta'])

    for cid in added_ids:
        msg_delta += int(new_chats[cid].get('message_count') or 0)
    for cid in removed_ids:
        msg_delta -= int(old_chats[cid].get('message_count') or 0)

    def _brief(cid, src):
        c = src[cid]
        return {
            'chat_id': cid,
            'display_name': c.get('display_name') or cid,
            'message_count': int(c.get('message_count') or 0),
            'is_group': bool(c.get('is_group')),
            'last_msg_time': c.get('last_msg_time'),
        }

    old_meta = load_version_meta(old_dir) or {}
    new_meta = load_version_meta(new_dir) or {}
    return {
        'ok': True,
        'old': {'id': old_meta.get('id'), 'path': old_dir, 'created_at': old_meta.get('created_at')},
        'new': {'id': new_meta.get('id'), 'path': new_dir, 'created_at': new_meta.get('created_at')},
        'summary': {
            'added_chats': len(added_ids),
            'removed_chats': len(removed_ids),
            'grown_chats': len(grown),
            'shrunk_chats': len(shrunk),
            'message_delta': msg_delta,
            'old_chat_count': len(old_chats),
            'new_chat_count': len(new_chats),
        },
        'added': [_brief(i, new_chats) for i in added_ids],
        'removed': [_brief(i, old_chats) for i in removed_ids],
        'grown': grown[:50],
        'shrunk': shrunk[:50],
    }


def activate_version(backup_dir: str, *, wxid: str = '') -> dict[str, Any]:
    """Point app config at ``backup_dir`` (rollback / switch working set)."""
    backup_dir = os.path.abspath(backup_dir)
    if not is_backup_dir(backup_dir):
        return {'ok': False, 'error': f'无效备份目录: {backup_dir}'}
    meta = load_version_meta(backup_dir) or {}
    use_wxid = wxid or meta.get('wxid') or ''
    from engine.config_file import set_backup_data_dir
    set_backup_data_dir(backup_dir, wxid=use_wxid or None)
    return {
        'ok': True,
        'path': backup_dir,
        'id': meta.get('id') or os.path.basename(backup_dir),
        'wxid': use_wxid,
        'meta': meta,
    }


def find_previous_version(backup_dir: str) -> Optional[str]:
    """Locate the chronologically previous sibling backup (for auto-diff)."""
    backup_dir = os.path.abspath(backup_dir)
    parent = os.path.dirname(backup_dir)
    versions = list_versions(parent, limit=100)
    paths = [os.path.abspath(v['path']) for v in versions if v.get('path')]
    try:
        idx = paths.index(backup_dir)
    except ValueError:
        return None
    if idx + 1 < len(paths):
        return paths[idx + 1]
    return None
