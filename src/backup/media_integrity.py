# -*- coding: utf-8 -*-
"""Media integrity: missing image/video/voice inventory + remigrate holes.

Scans decrypted backup message DBs for media references, checks whether
files exist under ``backup/media/{images,videos,files,voice}/``, and can
re-run targeted migration (hardlink/copy from live WeChat or prior backup)
plus voice extraction from ``media_*.db`` VoiceInfo.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from typing import Any, Callable, Optional

from .migrator import (
    _TYPE_CATEGORY,
    _date_str_to_ts,
    _extract_paths_from_xml,
    _find_media_source,
    migrate_media,
)

try:
    import zstandard as zstd
    _ZSTD_DCTX = zstd.ZstdDecompressor()
except ImportError:
    zstd = None
    _ZSTD_DCTX = None

# User-facing type groups
TYPE_LABELS = {
    3: '图片',
    43: '视频',
    34: '语音',
    6: '文件',
    47: '表情',
    49: '附件',
}

# Default scan types for UI: image / video / voice (+ files optional)
DEFAULT_TYPES = (3, 43, 34)


def _load_display_names(output_dir: str) -> dict[str, str]:
    chats_db = os.path.join(output_dir, 'data', 'chats.db')
    out: dict[str, str] = {}
    if not os.path.isfile(chats_db):
        return out
    try:
        conn = sqlite3.connect(chats_db)
        rows = conn.execute(
            'SELECT chat_id, display_name FROM chats'
        ).fetchall()
        conn.close()
        for cid, name in rows:
            if cid:
                out[str(cid)] = name or str(cid)
    except sqlite3.Error:
        pass
    return out


def _index_backup_media(output_dir: str) -> dict[str, set[str]]:
    """Map category → lowercased basenames present under media/."""
    index: dict[str, set[str]] = {
        'images': set(), 'videos': set(), 'files': set(), 'voice': set(),
    }
    root = os.path.join(output_dir, 'media')
    for cat in index:
        d = os.path.join(root, cat)
        if not os.path.isdir(d):
            continue
        try:
            for name in os.listdir(d):
                if os.path.isfile(os.path.join(d, name)):
                    index[cat].add(name.lower())
        except OSError:
            pass
    return index


def _file_present_in_index(index: dict[str, set[str]], media_type: int, md5: str,
                           basename: Optional[str] = None) -> bool:
    cat = _TYPE_CATEGORY.get(media_type, 'files')
    names = index.get(cat) or set()
    if basename and basename.lower() in names:
        return True
    if not md5 or len(md5) < 8:
        return False
    md5_l = md5.lower()
    for n in names:
        if n.startswith(md5_l):
            return True
    return False


def _voice_silk_present(index: dict[str, set[str]], create_time: int, local_id: int) -> bool:
    names = index.get('voice') or set()
    key = f'{create_time}_{local_id}.silk'.lower()
    if key in names:
        return True
    # Also accept any silk mentioning the pair
    prefix = f'{create_time}_{local_id}'.lower()
    return any(n.startswith(prefix) for n in names)


def _source_in_prior_backup(link_dest: Optional[str], media_type: int, md5: str) -> bool:
    if not link_dest or not md5:
        return False
    cat = _TYPE_CATEGORY.get(media_type, 'files')
    d = os.path.join(link_dest, 'media', cat)
    if not os.path.isdir(d):
        return False
    md5_l = md5.lower()
    try:
        for name in os.listdir(d):
            if name.lower().startswith(md5_l) and os.path.isfile(os.path.join(d, name)):
                return True
    except OSError:
        pass
    return False


def _iter_file_media_refs(
    db_path: str,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    types: Optional[set[int]] = None,
) -> list[dict[str, Any]]:
    """Like migrator._iter_media_refs but keeps local_id / create_time / chat hint."""
    types = types or {3, 6, 43, 47, 49}
    where_parts = ['create_time > 1000000000']
    where_params: list = []
    if start_ts is not None:
        where_parts.append('create_time >= ?')
        where_params.append(start_ts)
    if end_ts is not None:
        where_parts.append('create_time <= ?')
        where_params.append(end_ts)
    where_clause = 'WHERE ' + ' AND '.join(where_parts)

    out: list[dict[str, Any]] = []
    try:
        conn = sqlite3.connect(db_path)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
        ).fetchall()
        for (tname,) in tables:
            chat_hash = tname[4:] if tname.startswith('Msg_') else ''
            chat_id = None
            try:
                from backup.indexer import _resolve_chat_id
                chat_id = _resolve_chat_id(conn, chat_hash)
            except Exception:
                chat_id = None
            rows = conn.execute(
                f'SELECT local_id, local_type, create_time, message_content '
                f'FROM [{tname}] {where_clause}',
                where_params,
            ).fetchall()
            for local_id, local_type, create_time, content in rows:
                actual = local_type & 0xFFFF
                if actual not in types or actual == 34:
                    continue
                if not (isinstance(content, bytes) and len(content) > 4):
                    continue
                if _ZSTD_DCTX is None:
                    continue
                try:
                    raw = _ZSTD_DCTX.decompress(content)
                    lt = raw.find(b'<')
                    if lt > 0:
                        raw = raw[lt:]
                    xml_str = raw.decode('utf-8', errors='replace')
                except (ValueError, OSError):
                    continue
                refs: list = []
                _extract_paths_from_xml(xml_str, refs, actual)
                for ref in refs:
                    out.append({
                        'md5': ref.get('md5') or '',
                        'media_type': actual,
                        'local_id': local_id,
                        'create_time': create_time,
                        'chat_id': chat_id or (f'unknown_{chat_hash[:8]}' if chat_hash else ''),
                        'kind': 'file',
                    })
        conn.close()
    except sqlite3.Error:
        pass
    return out


def _iter_voice_refs(
    db_path: str,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Enumerate type-34 voice messages (no md5 required)."""
    where_parts = ['create_time > 1000000000']
    where_params: list = []
    if start_ts is not None:
        where_parts.append('create_time >= ?')
        where_params.append(start_ts)
    if end_ts is not None:
        where_parts.append('create_time <= ?')
        where_params.append(end_ts)
    where_clause = 'WHERE ' + ' AND '.join(where_parts)

    out: list[dict[str, Any]] = []
    try:
        conn = sqlite3.connect(db_path)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
        ).fetchall()
        for (tname,) in tables:
            chat_hash = tname[4:] if tname.startswith('Msg_') else ''
            chat_id = None
            try:
                from backup.indexer import _resolve_chat_id
                chat_id = _resolve_chat_id(conn, chat_hash)
            except Exception:
                chat_id = None
            rows = conn.execute(
                f'SELECT local_id, local_type, create_time FROM [{tname}] {where_clause}',
                where_params,
            ).fetchall()
            for local_id, local_type, create_time in rows:
                if (local_type & 0xFFFF) != 34:
                    continue
                out.append({
                    'md5': '',
                    'media_type': 34,
                    'local_id': local_id,
                    'create_time': create_time,
                    'chat_id': chat_id or (f'unknown_{chat_hash[:8]}' if chat_hash else ''),
                    'kind': 'voice',
                })
        conn.close()
    except sqlite3.Error:
        pass
    return out


def _voice_in_db(output_dir: str, create_time: int, local_id: int) -> bool:
    """True if VoiceInfo still has the blob (recoverable without live WeChat)."""
    msg_dir = os.path.join(output_dir, 'message')
    if not os.path.isdir(msg_dir):
        return False
    try:
        for fname in os.listdir(msg_dir):
            if not (fname.startswith('media_') and fname.endswith('.db')):
                continue
            path = os.path.join(msg_dir, fname)
            try:
                conn = sqlite3.connect(path)
                # VoiceInfo may or may not exist
                tables = {
                    r[0] for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if 'VoiceInfo' not in tables:
                    conn.close()
                    continue
                row = conn.execute(
                    'SELECT 1 FROM VoiceInfo WHERE create_time=? AND local_id=? LIMIT 1',
                    (create_time, local_id),
                ).fetchone()
                conn.close()
                if row:
                    return True
            except sqlite3.Error:
                continue
    except OSError:
        pass
    return False


def scan_integrity(
    output_dir: str,
    db_dir: Optional[str] = None,
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    types: Optional[list[int]] = None,
    link_dest: Optional[str] = None,
    limit_items: int = 500,
) -> dict[str, Any]:
    """Scan backup for missing media; classify recoverable vs gone.

    Returns summary + sample ``missing`` items for UI.
    """
    output_dir = os.path.abspath(output_dir)
    type_set = set(types or DEFAULT_TYPES)
    start_ts = _date_str_to_ts(start_date) if start_date else None
    end_ts = _date_str_to_ts(end_date, end_of_day=True) if end_date else None

    msg_dir = os.path.join(output_dir, 'message')
    if not os.path.isdir(msg_dir):
        return {
            'ok': False,
            'error': f'无效备份目录（缺少 message/）: {output_dir}',
            'summary': {},
            'missing': [],
        }

    if link_dest is None:
        try:
            from backup.fingerprint import resolve_link_dest
            link_dest = resolve_link_dest(output_dir, incremental=True)
        except Exception:
            link_dest = None
        # Don't use current output_dir as its own link_dest
        if link_dest and os.path.abspath(link_dest) == output_dir:
            # Prefer sibling previous version
            try:
                from backup.versions import find_previous_version
                link_dest = find_previous_version(output_dir)
            except Exception:
                link_dest = None

    index = _index_backup_media(output_dir)
    names = _load_display_names(output_dir)

    file_types = type_set - {34}
    voice_on = 34 in type_set

    raw_file: list[dict] = []
    raw_voice: list[dict] = []
    for fname in os.listdir(msg_dir):
        if not fname.endswith('.db') or fname.startswith('media_'):
            continue
        path = os.path.join(msg_dir, fname)
        if file_types:
            raw_file.extend(_iter_file_media_refs(path, start_ts, end_ts, file_types))
        if voice_on:
            raw_voice.extend(_iter_voice_refs(path, start_ts, end_ts))

    # Deduplicate file refs by md5 (keep first occurrence for listing)
    seen_md5: set[str] = set()
    file_refs: list[dict] = []
    for ref in raw_file:
        md5 = (ref.get('md5') or '').lower()
        if not md5 or md5 in seen_md5:
            continue
        seen_md5.add(md5)
        file_refs.append(ref)

    # Deduplicate voice by (create_time, local_id)
    seen_voice: set[tuple] = set()
    voice_refs: list[dict] = []
    for ref in raw_voice:
        key = (ref.get('create_time'), ref.get('local_id'))
        if key in seen_voice:
            continue
        seen_voice.add(key)
        voice_refs.append(ref)

    by_type = {t: {'total': 0, 'present': 0, 'missing': 0, 'recoverable': 0, 'gone': 0}
               for t in sorted(type_set)}
    missing_items: list[dict] = []
    present = 0
    missing = 0
    recoverable = 0
    gone = 0

    for ref in file_refs:
        mtype = int(ref.get('media_type') or 0)
        md5 = ref.get('md5') or ''
        bucket = by_type.setdefault(
            mtype, {'total': 0, 'present': 0, 'missing': 0, 'recoverable': 0, 'gone': 0})
        bucket['total'] += 1

        basename = None
        src = None
        if db_dir:
            src = _find_media_source(db_dir, ref, decrypted_dir=output_dir)
            if src:
                basename = os.path.basename(src)

        ok = _file_present_in_index(index, mtype, md5, basename)
        if ok:
            bucket['present'] += 1
            present += 1
            continue

        can = bool(src) or _source_in_prior_backup(link_dest, mtype, md5)

        bucket['missing'] += 1
        missing += 1
        if can:
            bucket['recoverable'] += 1
            recoverable += 1
            status = 'recoverable'
        else:
            bucket['gone'] += 1
            gone += 1
            status = 'gone'

        if len(missing_items) < max(1, limit_items):
            cid = ref.get('chat_id') or ''
            missing_items.append({
                'kind': 'file',
                'media_type': mtype,
                'type_label': TYPE_LABELS.get(mtype, str(mtype)),
                'md5': md5,
                'chat_id': cid,
                'display_name': names.get(cid, cid),
                'local_id': ref.get('local_id'),
                'create_time': ref.get('create_time'),
                'status': status,
                'recoverable': can,
            })

    for ref in voice_refs:
        bucket = by_type.setdefault(
            34, {'total': 0, 'present': 0, 'missing': 0, 'recoverable': 0, 'gone': 0})
        bucket['total'] += 1
        ct = int(ref.get('create_time') or 0)
        lid = int(ref.get('local_id') or 0)
        ok = _voice_silk_present(index, ct, lid)
        if ok:
            bucket['present'] += 1
            present += 1
            continue
        can = _voice_in_db(output_dir, ct, lid)
        bucket['missing'] += 1
        missing += 1
        if can:
            bucket['recoverable'] += 1
            recoverable += 1
            status = 'recoverable'
        else:
            bucket['gone'] += 1
            gone += 1
            status = 'gone'
        if len(missing_items) < max(1, limit_items):
            cid = ref.get('chat_id') or ''
            missing_items.append({
                'kind': 'voice',
                'media_type': 34,
                'type_label': '语音',
                'md5': '',
                'chat_id': cid,
                'display_name': names.get(cid, cid),
                'local_id': lid,
                'create_time': ct,
                'status': status,
                'recoverable': can,
            })

    total = present + missing
    return {
        'ok': True,
        'output_dir': output_dir,
        'db_dir': db_dir or None,
        'link_dest': link_dest,
        'summary': {
            'total': total,
            'present': present,
            'missing': missing,
            'recoverable': recoverable,
            'gone': gone,
            'by_type': by_type,
        },
        'missing': missing_items,
        'missing_truncated': len(missing_items) < missing,
        'scanned_at': int(datetime.now().timestamp()),
    }


def remigrate_missing(
    output_dir: str,
    db_dir: Optional[str] = None,
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    types: Optional[list[int]] = None,
    link_dest: Optional[str] = None,
    on_progress: Optional[Callable[[str, str, float], None]] = None,
) -> dict[str, Any]:
    """Fill missing media holes: migrate file md5s + extract voice silks."""
    if on_progress is None:
        on_progress = lambda stage, detail, p: None

    scan = scan_integrity(
        output_dir, db_dir,
        start_date=start_date, end_date=end_date,
        types=types, link_dest=link_dest,
        limit_items=100000,
    )
    if not scan.get('ok'):
        return {'ok': False, 'error': scan.get('error'), 'scan': scan}

    if link_dest is None:
        link_dest = scan.get('link_dest')

    missing = scan.get('missing') or []
    file_md5s = {
        (m.get('md5') or '').lower()
        for m in missing
        if m.get('kind') == 'file' and m.get('recoverable') and m.get('md5')
    }
    voice_items = [
        m for m in missing
        if m.get('kind') == 'voice' and m.get('recoverable')
    ]

    result: dict[str, Any] = {
        'ok': True,
        'file_targets': len(file_md5s),
        'voice_targets': len(voice_items),
        'migrate': None,
        'voice': {'extracted': 0, 'failed': 0},
        'scan_before': scan.get('summary'),
    }

    if file_md5s:
        if not db_dir and not link_dest:
            result['migrate'] = {
                'skipped_reason': '无微信数据目录且无上一版备份可复用',
                'total': 0, 'hardlinked': 0, 'copied': 0, 'skipped': 0,
                'link_reused': 0, 'errors': [],
            }
        else:
            on_progress('remigrate', f'补迁文件媒体 {len(file_md5s)} 个…', 0.1)
            # Build refs list from missing items
            refs = [
                {'md5': m['md5'], 'media_type': m['media_type']}
                for m in missing
                if m.get('kind') == 'file' and m.get('recoverable') and m.get('md5')
            ]
            use_db = db_dir or ''
            stats = migrate_media(
                use_db, output_dir,
                start_date=start_date, end_date=end_date,
                link_dest=link_dest,
                md5_allowlist=file_md5s,
                refs=refs,
                on_progress=lambda name, n, t: on_progress(
                    'remigrate', f'{name} ({n}/{t})', 0.1 + 0.6 * (n / max(t, 1))),
            )
            result['migrate'] = stats

    if voice_items:
        on_progress('voice', f'提取语音 {len(voice_items)} 条…', 0.75)
        from engine.services.media import _extract_voice_from_db
        extracted = 0
        failed = 0
        total_v = len(voice_items)
        for i, item in enumerate(voice_items):
            ct = int(item.get('create_time') or 0)
            lid = int(item.get('local_id') or 0)
            path = _extract_voice_from_db(
                output_dir, ct, lid, db_dir=db_dir,
                chat=item.get('chat_id') or None,
            )
            if path and os.path.isfile(path):
                extracted += 1
            else:
                failed += 1
            if i % 20 == 0 or i + 1 == total_v:
                on_progress(
                    'voice',
                    f'语音 {i + 1}/{total_v}',
                    0.75 + 0.2 * ((i + 1) / max(total_v, 1)),
                )
        result['voice'] = {'extracted': extracted, 'failed': failed}

    on_progress('done', '补迁完成', 1.0)
    # Quick recount of remaining missing (cheap re-scan summary only)
    after = scan_integrity(
        output_dir, db_dir,
        start_date=start_date, end_date=end_date,
        types=types, link_dest=link_dest,
        limit_items=50,
    )
    result['scan_after'] = after.get('summary')
    result['remaining_missing'] = (after.get('summary') or {}).get('missing', 0)
    return result
