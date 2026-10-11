"""Hardlink-based media migration for WeChat 4.x backup."""
import os
import re
import sqlite3
import shutil
from datetime import datetime
from typing import Callable, Optional

try:
    import zstandard as zstd
    _ZSTD_DCTX = zstd.ZstdDecompressor()
except ImportError:
    zstd = None
    _ZSTD_DCTX = None


def _date_str_to_ts(date_str: str, end_of_day: bool = False) -> int:
    """Convert YYYY-MM-DD to Unix timestamp."""
    fmt = '%Y-%m-%d %H:%M:%S' if end_of_day else '%Y-%m-%d'
    if end_of_day:
        date_str = f'{date_str} 23:59:59'
    return int(datetime.strptime(date_str, fmt).timestamp())


def _iter_media_refs(db_path: str, start_ts: int = None,
                     end_ts: int = None) -> list:
    """Extract all media references from a decrypted message DB.

    Scans message_content for media md5 references in WeChat 4.x XML format.
    Optionally filters by create_time range.

    Returns list of {md5, media_type}.
    """
    where_parts = ["create_time > 1000000000"]
    where_params = []
    if start_ts is not None:
        where_parts.append("create_time >= ?")
        where_params.append(start_ts)
    if end_ts is not None:
        where_parts.append("create_time <= ?")
        where_params.append(end_ts)
    where_clause = "WHERE " + " AND ".join(where_parts)

    refs = []
    try:
        conn = sqlite3.connect(db_path)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
        ).fetchall()
        for (tname,) in tables:
            rows = conn.execute(
                f"SELECT local_id, local_type, message_content FROM [{tname}] {where_clause}",
                where_params
            ).fetchall()
            for local_id, local_type, content in rows:
                actual_type = local_type & 0xFFFF
                if actual_type not in (3, 6, 34, 43, 47, 49):
                    continue
                if isinstance(content, bytes) and len(content) > 4:
                    try:
                        if _ZSTD_DCTX is None:
                            continue
                        raw = _ZSTD_DCTX.decompress(content)
                        lt = raw.find(b'<')
                        if lt > 0:
                            raw = raw[lt:]
                        xml_str = raw.decode('utf-8', errors='replace')
                        _extract_paths_from_xml(xml_str, refs, actual_type)
                    except (ValueError, OSError):
                        continue
        conn.close()
    except sqlite3.Error:
        pass
    return refs


def _extract_paths_from_xml(xml_str: str, refs: list, media_type: int):
    """Extract file md5s from WeChat 4.x message XML.

    Handles three XML patterns:
      1. md5="..." attribute on <img />, <videomsg />, <emoji /> tags
      2. <md5>...</md5> element inside <appattach> (file attachments)
      3. <cdnthumbmd5>...</cdnthumbmd5> element (thumbnails)
    """

    # Pattern 1: md5 attribute — <img md5="abc123..." />, <videomsg md5="..." />, <emoji md5="..." />
    for m in re.finditer(r'''md5\s*=\s*["']([a-fA-F0-9]{32})["']''', xml_str):
        refs.append({'md5': m.group(1), 'media_type': media_type})

    # Pattern 2: <md5> element — <appattach><md5>abc123...</md5></appattach>
    for m in re.finditer(r'<md5>([a-fA-F0-9]{32})</md5>', xml_str):
        refs.append({'md5': m.group(1), 'media_type': media_type})

    # Pattern 3: <cdnthumbmd5> element — thumbnails in appmsg
    for m in re.finditer(r'<cdnthumbmd5>([a-fA-F0-9]{32})</cdnthumbmd5>', xml_str):
        refs.append({'md5': m.group(1), 'media_type': media_type})


# Mapping from WeChat message local_type to (msg_subdir, hardlink_table)
_TYPE_MAP = {
    3:  ('msg/attach', 'image_hardlink_info_v4'),   # image
    43: ('msg/video',  'video_hardlink_info_v4'),   # video
    6:  ('msg/file',   'file_hardlink_info_v4'),    # file
    47: ('msg/attach', 'image_hardlink_info_v4'),   # emoticon → stored as image
    34: ('msg/attach', 'image_hardlink_info_v4'),   # voice
    49: ('msg/file',   'file_hardlink_info_v4'),    # appmsg → may contain file
}

# Derived output category per type — keep in sync with _TYPE_MAP
_TYPE_CATEGORY = {
    3: 'images', 43: 'videos', 6: 'files', 34: 'voice',
    47: 'images', 49: 'files',
}


def _find_media_source(db_dir: str, media_ref: dict,
                       decrypted_dir: str = None) -> str:
    """Find the original media file in WeChat storage.

    Uses the hardlink database to resolve MD5 → file path within the
    wxid directory (parent of db_storage).

    WeChat 4.x directory layout:
        xwechat_files/<wxid>/
          db_storage/hardlink/hardlink.db    ← hardlink index (encrypted)
          msg/attach/<chat_hash>/<YYYY-MM>/<md5>.dat   ← images
          msg/video/<YYYY-MM>/<md5>.mp4                ← videos
          msg/file/<YYYY-MM>/<original_name>           ← files

    Args:
        db_dir: WeChat db_storage directory
        media_ref: {md5, media_type} dict
        decrypted_dir: Path to decrypted data dir (for readable hardlink DB)

    Returns:
        Absolute path to source file, or None if not found.
    """
    wxid_dir = os.path.dirname(db_dir)
    md5 = media_ref.get('md5', '')
    media_type = media_ref.get('media_type', 0)

    if not md5:
        return None

    subdir, table = _TYPE_MAP.get(media_type, (None, None))
    if not subdir:
        return None

    # Try decrypted hardlink DB first, then source (encrypted, will fail gracefully)
    hardlink_candidates = []
    if decrypted_dir:
        hardlink_candidates.append(os.path.join(decrypted_dir, 'hardlink', 'hardlink.db'))
    hardlink_candidates.append(os.path.join(db_dir, 'hardlink', 'hardlink.db'))

    hardlink_db = None
    for candidate in hardlink_candidates:
        if os.path.isfile(candidate):
            hardlink_db = candidate
            break

    if not hardlink_db:
        return None

    try:
        conn = sqlite3.connect(hardlink_db)

        # Check if the expected table exists
        table_check = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,)
        ).fetchone()
        if not table_check:
            conn.close()
            return None

        # Fetch ALL rows for this md5, preferring original (.dat) over
        # thumbnails (_h.dat, _t.dat). The same CDN md5 can have multiple
        # rows in the HardLink DB — one for the original and one for each
        # thumbnail variant.
        rows = conn.execute(
            f"SELECT file_name, dir1, dir2 FROM [{table}] WHERE md5=? "
            f"ORDER BY CASE WHEN substr(file_name, -6)='_h.dat' THEN 2 "
            f"WHEN substr(file_name, -6)='_t.dat' THEN 3 ELSE 1 END",
            (md5,)
        ).fetchall()

        if not rows:
            conn.close()
            return None

        # Pre-resolve dir1/dir2 IDs once (same for all rows)
        dir_ids = set()
        for _, d1, d2 in rows:
            if d1:
                dir_ids.add(d1)
            if d2:
                dir_ids.add(d2)
        dir_names = {}
        for did in dir_ids:
            drow = conn.execute(
                "SELECT username FROM dir2id WHERE rowid=?", (did,)
            ).fetchone()
            dir_names[did] = drow[0] if drow else ''
        conn.close()

        # Try each row in priority order until one resolves to an existing file
        for fname, d1, d2 in rows:
            dir1_name = dir_names.get(d1, '') if d1 else ''
            dir2_name = dir_names.get(d2, '') if d2 else ''

            if media_type in (3, 47) and dir2_name:
                base = os.path.join(wxid_dir, subdir, dir1_name)
                full = os.path.join(base, dir2_name, 'Img', fname)
                if os.path.isfile(full):
                    return full
                full = os.path.join(base, dir2_name, fname)
                if os.path.isfile(full):
                    return full

            base = os.path.join(wxid_dir, subdir, dir1_name)
            if dir2_name:
                full = os.path.join(base, dir2_name, fname)
                if os.path.isfile(full):
                    return full
            full = os.path.join(base, fname)
            if os.path.isfile(full):
                return full

    except sqlite3.Error:
        pass

    return None


def migrate_media(
    db_dir: str,
    output_dir: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    link_dest: Optional[str] = None,
    on_progress: Callable[[str, int, int], None] = None,
) -> dict:
    """Migrate all media files using hardlinks, falling back to copy.

    Args:
        db_dir: WeChat db_storage directory (for finding source files)
        output_dir: Backup output root (media goes to output_dir/media/)
        start_date: YYYY-MM-DD start of message date range (None = unbounded)
        end_date: YYYY-MM-DD end of message date range (None = unbounded)
        link_dest: Previous backup root for inter-backup hardlinks.
            When set, each media file checks link_dest/media/<subdir>/<name>
            first. If found, os.link() reuses the old copy instead of
            accessing the WeChat source — saving both time and disk space.
    Returns:
        {total: N, hardlinked: N, copied: N, skipped: N, link_reused: N, errors: [str]}
    """
    start_ts = _date_str_to_ts(start_date) if start_date else None
    end_ts = _date_str_to_ts(end_date, end_of_day=True) if end_date else None

    msg_dir = os.path.join(output_dir, 'message')
    media_out = os.path.join(output_dir, 'media')
    os.makedirs(os.path.join(media_out, 'images'), exist_ok=True)
    os.makedirs(os.path.join(media_out, 'videos'), exist_ok=True)
    os.makedirs(os.path.join(media_out, 'files'), exist_ok=True)
    os.makedirs(os.path.join(media_out, 'voice'), exist_ok=True)

    stats = {'total': 0, 'hardlinked': 0, 'copied': 0, 'skipped': 0,
             'link_reused': 0, 'errors': []}

    if not os.path.isdir(msg_dir):
        return stats

    all_refs = []
    for fname in os.listdir(msg_dir):
        if fname.endswith('.db'):
            all_refs.extend(_iter_media_refs(os.path.join(msg_dir, fname),
                                              start_ts=start_ts, end_ts=end_ts))

    # Deduplicate by md5
    seen = set()
    unique_refs = []
    for ref in all_refs:
        md5 = ref.get('md5', '')
        if md5 and md5 not in seen:
            seen.add(md5)
            unique_refs.append(ref)
    all_refs = unique_refs

    stats['total'] = len(all_refs)

    decrypted_data_dir = output_dir
    for i, ref in enumerate(all_refs):
        src = _find_media_source(db_dir, ref, decrypted_dir=decrypted_data_dir)
        if not src:
            stats['skipped'] += 1
            continue

        mtype = ref.get('media_type', 0)
        subdir = _TYPE_CATEGORY.get(mtype, 'files')
        dst = os.path.join(media_out, subdir, os.path.basename(src))

        if os.path.exists(dst):
            stats['skipped'] += 1
            continue

        linked = False
        if link_dest:
            link_src = os.path.join(link_dest, 'media', subdir,
                                    os.path.basename(src))
            if os.path.isfile(link_src):
                try:
                    os.link(link_src, dst)
                    stats['link_reused'] += 1
                    linked = True
                except OSError:
                    pass

        if not linked:
            try:
                os.link(src, dst)
                stats['hardlinked'] += 1
            except OSError:
                try:
                    shutil.copy2(src, dst)
                    stats['copied'] += 1
                except OSError as e:
                    stats['errors'].append(f"{src}: {e}")

        if on_progress:
            safe_name = os.path.basename(src).encode('ascii', errors='replace').decode('ascii')
            on_progress(safe_name, i + 1, len(all_refs))

    return stats
