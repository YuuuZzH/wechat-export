"""Cleanup service: analyze space usage, preview deletions, execute cleanup.

Three-phase design:
  1. analyze  — read-only scan of chats.db + hardlink.db + disk sampling
  2. preview  — count matching messages/files, no writes
  3. execute  — os.unlink media files only (DB records untouched)

Media-only deletion: removes image/video/file/voice files from WeChat storage
directories. Chat history records in the database are preserved — this avoids
the pysqlcipher3 dependency for writing to encrypted message_N.db files.

Space analysis uses DB-based estimation with disk sampling correction.
DB records may reference files that no longer exist on disk (moved, cleaned,
or deleted outside the tool). We sample ~300 random files to measure the
actual survival rate, then scale all estimates by that factor.
"""
import hashlib
import os
import random
import re
import sqlite3
import time
import uuid

from engine.services.name_resolver import _find_contact_db, pick_display_name, _load_chatroom_names, chatroom_fallback_name
from engine.services.address_book import _find_chats_db

# In-memory confirm token store: token -> {data, expiry}
_confirm_tokens = {}
_TOKEN_TTL = 60  # seconds


def _generate_confirm_token(data: dict) -> str:
    """Generate a one-time confirmation token valid for 60 seconds."""
    now = time.time()
    expired = [t for t, v in _confirm_tokens.items() if v['expiry'] < now]
    for t in expired:
        del _confirm_tokens[t]
    token = uuid.uuid4().hex
    _confirm_tokens[token] = {'data': data, 'expiry': now + _TOKEN_TTL}
    return token


def _verify_confirm_token(token: str) -> dict:
    """Verify and consume a confirmation token. Returns stored data or None."""
    now = time.time()
    entry = _confirm_tokens.pop(token, None)
    if not entry or entry['expiry'] < now:
        return None
    return entry['data']


def _get_avg_media_sizes(decrypted_dir: str) -> dict:
    """Return {media_type: avg_bytes} from hardlink.db for estimation."""
    avg_sizes = {}
    hardlink_db = os.path.join(decrypted_dir, 'hardlink', 'hardlink.db')
    if not os.path.isfile(hardlink_db):
        hardlink_db = os.path.join(decrypted_dir, 'HardLink', 'hardlink.db')
    if not os.path.isfile(hardlink_db):
        # Fallback: typical WeChat media sizes
        return {3: 200000, 43: 5000000, 6: 2000000, 34: 50000}
    type_map = {'image_hardlink_info_v4': 3, 'video_hardlink_info_v4': 43,
                'file_hardlink_info_v4': 6}
    try:
        conn = sqlite3.connect(hardlink_db)
        for table, media_type in type_map.items():
            try:
                row = conn.execute(
                    f"SELECT AVG(file_size) FROM [{table}]"
                ).fetchone()
                if row and row[0]:
                    avg_sizes[media_type] = int(row[0])
            except sqlite3.Error:
                pass
        conn.close()
    except sqlite3.Error:
        pass
    # Fill in missing types with fallback values
    for mt, fallback in ((3, 200000), (43, 5000000), (6, 2000000), (34, 50000)):
        if mt not in avg_sizes:
            avg_sizes[mt] = fallback
    return avg_sizes


def analyze_chats(decrypted_dir: str, db_dir: str) -> list:
    """Analyze space usage per chat with disk-verified file accounting.

    Phase 1: Fast counts from chats.db (chat metadata + per-type media counts).
    Phase 2: One-pass MD5 extraction from all message_N.db shards, mapping
             Msg tables back to known chat_ids via MD5 hash matching.
    Phase 3: Batch-resolve all MD5s through hardlink.db, verify disk existence.
    Phase 4: Sum actual bytes from files that exist on disk.

    Returns {chats: [...], sampling: {}, notes: 'disk-verified'}
    """
    chats_db = _find_chats_db(decrypted_dir)
    if not chats_db:
        return {'chats': [], 'sampling': {}, 'notes': 'disk-verified'}

    # -- Load display names from contact.db --
    display_names = {}
    contact_db = _find_contact_db(decrypted_dir)
    if contact_db and os.path.isfile(contact_db):
        try:
            conn = sqlite3.connect(contact_db)
            for r in conn.execute(
                "SELECT username, remark, nick_name, alias FROM contact"
            ):
                uname, remark, nick, alias = r
                uname = (uname or '').strip()
                if uname:
                    display_names[uname] = pick_display_name(
                        uname, remark, nick, alias, uname
                    )
            conn.close()
        except sqlite3.Error:
            pass

        # Enrich with chat_room names for groups with empty contact fields
        chatroom_names = _load_chatroom_names(decrypted_dir)
        for chatroom_id, name in chatroom_names.items():
            current = display_names.get(chatroom_id, '')
            if not current or current == chatroom_id:
                display_names[chatroom_id] = name

    # -- Phase 1: Per-chat counts and metadata from chats.db --
    per_chat_counts = {}  # chat_id -> {media_type: count}
    chats_info = {}       # chat_id -> {display, msg_count, is_group, src_db,
                          #             first_t, last_t}
    try:
        conn = sqlite3.connect(chats_db)
        has_messages = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
        ).fetchone()
        if has_messages:
            for r in conn.execute(
                "SELECT chat_id, (local_type & 0xFFFF), COUNT(*) "
                "FROM messages "
                "WHERE (local_type & 0xFFFF) IN (3, 6, 34, 43) "
                "GROUP BY chat_id, (local_type & 0xFFFF)"
            ):
                chat_id, raw_type, count = r
                if chat_id not in per_chat_counts:
                    per_chat_counts[chat_id] = {}
                per_chat_counts[chat_id][raw_type] = count

        for r in conn.execute(
            "SELECT chat_id, display_name, message_count, first_msg_time, "
            "last_msg_time, is_group, source_db FROM chats"
        ):
            chat_id, disp, msg_count, first_t, last_t, is_group, src_db = r
            display = display_names.get(chat_id) or disp or chat_id
            if display == chat_id and chat_id.endswith('@chatroom'):
                display = chatroom_fallback_name(chat_id)
            chats_info[chat_id] = {
                'display': display,
                'msg_count': msg_count or 0,
                'is_group': bool(is_group),
                'src_db': src_db or '',
                'first_t': first_t,
                'last_t': last_t,
            }
        conn.close()
    except sqlite3.Error:
        return {'chats': [], 'sampling': {}, 'notes': 'disk-verified'}

    # -- Phase 2: One-pass MD5 extraction from message DB shards --
    # Single connection per shard; iterate matching Msg tables inline.
    msg_dir = os.path.join(decrypted_dir, 'message')
    all_chat_ids = set(per_chat_counts.keys())
    chat_md5s = {cid: [] for cid in all_chat_ids}
    global_md5_types = {}

    # Pre-compute hash→chat_id map once
    id_to_hash = {}
    for chat_id in all_chat_ids:
        id_to_hash[hashlib.md5(chat_id.encode()).hexdigest()] = chat_id

    all_shard_paths = set()
    for search_dir in (msg_dir, os.path.dirname(msg_dir)):
        if not os.path.isdir(search_dir):
            continue
        for fname in os.listdir(search_dir):
            if fname.startswith('message_') and fname.endswith('.db') \
               and 'fts' not in fname and 'resource' not in fname:
                all_shard_paths.add(os.path.join(search_dir, fname))

    for db_path in sorted(all_shard_paths):
        try:
            shard_conn = sqlite3.connect(db_path)
            shard_tables = [
                r[0] for r in shard_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                ).fetchall()
            ]
            for table_name in shard_tables:
                hash_suffix = table_name[4:]  # strip 'Msg_' prefix
                chat_id = id_to_hash.get(hash_suffix)
                if not chat_id:
                    continue
                try:
                    rows = shard_conn.execute(
                        f"""SELECT message_content, (local_type & 0xFFFF)
                            FROM [{table_name}]
                            WHERE (local_type & 0xFFFF) IN (3, 6, 34, 43)
                              AND message_content IS NOT NULL
                            LIMIT 500"""
                    ).fetchall()
                except sqlite3.Error:
                    continue
                seen_per_chat = set()
                for content, raw_type in rows:
                    md5_val = _extract_md5_from_content(content)
                    if md5_val and md5_val not in seen_per_chat:
                        seen_per_chat.add(md5_val)
                        chat_md5s[chat_id].append((md5_val, raw_type))
                        if md5_val not in global_md5_types:
                            global_md5_types[md5_val] = raw_type
            shard_conn.close()
        except sqlite3.Error:
            continue

    # -- Phase 3: Batch disk resolution --
    all_md5_list = list(global_md5_types.keys())
    md5_disk_info = _batch_resolve_md5_paths(
        decrypted_dir, all_md5_list, global_md5_types, db_dir
    )

    # -- Phase 4: Per-chat byte computation from disk-existing files --
    results = []
    for chat_id, info in chats_info.items():
        counts = per_chat_counts.get(chat_id, {})
        img_count = counts.get(3, 0)
        vid_count = counts.get(43, 0)
        file_count = counts.get(6, 0)
        voice_count = counts.get(34, 0)
        media_count = img_count + vid_count + file_count + voice_count

        image_bytes = 0
        video_bytes = 0
        file_bytes = 0
        voice_bytes = 0
        for md5_val, media_type in chat_md5s.get(chat_id, []):
            dinfo = md5_disk_info.get(md5_val, {})
            if dinfo.get('exists'):
                size = dinfo.get('size', 0)
                if media_type == 3:
                    image_bytes += size
                elif media_type == 43:
                    video_bytes += size
                elif media_type == 6:
                    file_bytes += size
                elif media_type == 34:
                    voice_bytes += size

        results.append({
            'chat_id': chat_id,
            'display_name': info['display'],
            'is_group': info['is_group'],
            'source_db': info['src_db'],
            'total_bytes': int(image_bytes + video_bytes + file_bytes + voice_bytes),
            'image_bytes': int(image_bytes),
            'video_bytes': int(video_bytes),
            'file_bytes': int(file_bytes),
            'voice_bytes': int(voice_bytes),
            'message_count': info['msg_count'],
            'media_count': media_count,
            'first_msg_time': info['first_t'],
            'last_msg_time': info['last_t'],
        })

    results.sort(key=lambda c: c['total_bytes'], reverse=True)
    return {
        'chats': results,
        'sampling': {},
        'notes': 'disk-verified',
    }


def preview_deletion(decrypted_dir: str, db_dir: str, chat_ids: list,
                     start_date: str, end_date: str,
                     msg_types: list) -> dict:
    """Preview what would be deleted. Read-only, no writes.

    Returns: {total_messages, total_files, total_bytes, by_chat, files_preview, confirm_token}
    """
    where_parts = []
    params = []
    if start_date:
        where_parts.append("create_time >= ?")
        params.append(_date_to_ts(start_date))
    if end_date:
        where_parts.append("create_time <= ?")
        params.append(_date_to_ts(end_date, end_of_day=True))
    if msg_types:
        placeholders = ','.join(['?'] * len(msg_types))
        where_parts.append(f"(local_type & 0xFFFF) IN ({placeholders})")
        params.extend(msg_types)
    where_clause = "WHERE " + " AND ".join(where_parts) if where_parts else ""

    file_sizes = _load_file_sizes(decrypted_dir)
    msg_dir = os.path.join(decrypted_dir, 'message')
    parent = os.path.dirname(db_dir) if db_dir else ''

    # Phase 1: collect all (md5, media_type) pairs from message DBs
    by_chat = []
    total_messages = 0
    all_md5s = {}  # md5 -> media_type (first seen wins)
    chat_md5s = {}  # chat_id -> set of md5s
    chat_msg_counts = {}  # chat_id -> msg count

    for chat_id in chat_ids:
        all_tables = _find_msg_tables(msg_dir, chat_id)
        if not all_tables:
            continue
        seen = set()
        c_msgs = 0
        for db_path, table_name in all_tables:
            try:
                conn = sqlite3.connect(db_path)
                count = conn.execute(
                    f"SELECT COUNT(*) FROM [{table_name}] {where_clause}", params
                ).fetchone()[0]
                if count > 0:
                    rows = conn.execute(
                        f"SELECT message_content, local_type FROM [{table_name}] "
                        f"{where_clause}", params
                    ).fetchall()
                    for content, local_type in rows:
                        c_msgs += 1
                        md5_val = _extract_md5_from_content(content)
                        if md5_val and md5_val not in seen:
                            seen.add(md5_val)
                            media_type = local_type & 0xFFFF if isinstance(local_type, (int, float)) else 6
                            if md5_val not in all_md5s:
                                all_md5s[md5_val] = media_type
                conn.close()
            except sqlite3.Error:
                continue
        if seen:
            chat_md5s[chat_id] = seen
            chat_msg_counts[chat_id] = c_msgs
            total_messages += c_msgs

    # Phase 2: batch-resolve MD5s → disk paths via hardlink.db (single connection)
    md5_path_map = _batch_resolve_md5_paths(decrypted_dir, list(all_md5s.keys()),
                                             all_md5s, db_dir)

    # Phase 3: check disk existence and compute accurate sizes
    files_preview = []
    total_files = 0
    total_bytes = 0
    for chat_id in chat_ids:
        seen = chat_md5s.get(chat_id, set())
        if not seen:
            continue
        chat_files = 0
        chat_bytes = 0
        for md5_val in seen:
            chat_files += 1
            disk_info = md5_path_map.get(md5_val)
            if disk_info and disk_info['exists']:
                chat_bytes += disk_info['size']
                total_bytes += disk_info['size']
            if len(files_preview) < 200:
                files_preview.append({
                    'md5': md5_val,
                    'size': disk_info['size'] if disk_info else (file_sizes.get(md5_val, 0)),
                    'disk_exists': disk_info['exists'] if disk_info else False,
                    'chat_id': chat_id,
                })
        by_chat.append({
            'chat_id': chat_id,
            'msg_count': chat_msg_counts.get(chat_id, 0),
            'file_count': chat_files,
            'bytes': chat_bytes,
        })
        total_files += chat_files

    token = _generate_confirm_token({
        'chat_ids': chat_ids,
        'start_date': start_date,
        'end_date': end_date,
        'msg_types': msg_types,
    })

    return {
        'total_messages': total_messages,
        'total_files': total_files,
        'total_bytes': total_bytes,
        'by_chat': by_chat,
        'files_preview': files_preview,
        'confirm_token': token,
    }


def execute_deletion(decrypted_dir: str, db_dir: str, confirm_token: str,
                     chat_ids: list, start_date: str, end_date: str,
                     msg_types: list, on_progress=None) -> dict:
    """Execute deletion: os.unlink media files only (DB records untouched).

    Reads message_N.db (decrypted) to identify media MD5s, then resolves
    file paths via hardlink.db and deletes the source files from disk.
    Chat history records in the database are preserved.

    Returns: {deleted_files, freed_bytes, errors}
    """
    if not db_dir:
        return {'error': '未配置源数据库路径 (db_dir)', 'deleted_files': 0,
                'freed_bytes': 0, 'errors': ['db_dir not set']}

    # Verify token
    token_data = _verify_confirm_token(confirm_token)
    if not token_data:
        return {'error': '确认已过期或无效，请重新预览', 'deleted_files': 0,
                'freed_bytes': 0, 'errors': ['invalid token']}

    # Build WHERE clause
    where_parts = []
    params = []
    if start_date:
        where_parts.append("create_time >= ?")
        params.append(_date_to_ts(start_date))
    if end_date:
        where_parts.append("create_time <= ?")
        params.append(_date_to_ts(end_date, end_of_day=True))
    if msg_types:
        placeholders = ','.join(['?'] * len(msg_types))
        where_parts.append(f"(local_type & 0xFFFF) IN ({placeholders})")
        params.extend(msg_types)
    where_clause = " AND ".join(where_parts)

    decrypted_msg_dir = os.path.join(decrypted_dir, 'message')

    file_sizes = _load_file_sizes(decrypted_dir)
    deleted_files = [0]
    freed_bytes = [0]
    errors = []

    for i, chat_id in enumerate(chat_ids):
        if on_progress:
            on_progress(f"处理 {chat_id}...", i / len(chat_ids))

        all_tables = _find_msg_tables(decrypted_msg_dir, chat_id)
        if not all_tables:
            continue

        # Collect media (md5, local_type) pairs from ALL matching DBs
        media_refs = set()
        for db_path, table_name in all_tables:
            try:
                conn = sqlite3.connect(db_path)
                for (content, local_type) in conn.execute(
                    f"SELECT message_content, local_type FROM [{table_name}] "
                    f"WHERE local_type & 0xFFFF IN (3, 6, 43) AND {where_clause}",
                    params
                ):
                    md5_val = _extract_md5_from_content(content)
                    if md5_val:
                        raw_type = local_type & 0xFFFF
                        media_refs.add((md5_val, raw_type))
                conn.close()
            except sqlite3.Error:
                continue

        # Delete media files from source
        if media_refs:
            _delete_media_files_by_md5(media_refs, decrypted_dir, db_dir,
                                       file_sizes, deleted_files,
                                       freed_bytes, errors)

    if on_progress:
        on_progress("完成", 1.0)

    return {
        'deleted_files': deleted_files[0],
        'freed_bytes': freed_bytes[0],
        'errors': errors,
    }


def _delete_media_files_by_md5(media_refs: set, decrypted_dir: str, db_dir: str,
                                file_sizes: dict, deleted_files_list: list,
                                freed_bytes_list: list, errors: list) -> None:
    """Delete media files from source WeChat directories using hardlink.db paths.

    Uses _resolve_from_hardlink_db to find exact file paths from dir1/dir2
    references, then resolves the absolute source path via db_dir.
    """
    from engine.services.media import _resolve_from_hardlink_db

    parent = os.path.dirname(db_dir) if db_dir else ''

    for md5_val, media_type in media_refs:
        rel_path = _resolve_from_hardlink_db(decrypted_dir, md5_val, media_type)
        if not rel_path:
            continue
        full_path = os.path.join(parent, rel_path.replace('/', os.sep))
        if not os.path.isfile(full_path):
            continue
        try:
            size = os.path.getsize(full_path)
            os.unlink(full_path)
            deleted_files_list[0] += 1
            freed_bytes_list[0] += size
        except OSError as e:
            errors.append(f"无法删除 {full_path}: {str(e)}")


def _batch_resolve_md5_paths(decrypted_dir: str, md5s: list, md5_types: dict,
                              db_dir: str = '') -> dict:
    """Batch-resolve MD5s to disk paths using a single hardlink.db connection.

    Returns {md5: {'exists': bool, 'size': int, 'path': str|None}}
    """
    result = {m: {'exists': False, 'size': 0, 'path': None} for m in md5s}
    if not md5s:
        return result

    hardlink_db = os.path.join(decrypted_dir, 'hardlink', 'hardlink.db')
    if not os.path.isfile(hardlink_db):
        hardlink_db = os.path.join(decrypted_dir, 'HardLink', 'hardlink.db')
    if not os.path.isfile(hardlink_db):
        return result

    table_map = {3: 'image', 43: 'video', 6: 'file', 34: 'voice'}

    try:
        conn = sqlite3.connect(hardlink_db)
        # Load dir2id mappings once
        dir_map = {}
        try:
            for r in conn.execute("SELECT rowid, * FROM dir2id"):
                dir_map[r[0]] = r[1]
        except sqlite3.Error:
            pass

        for media_type, suffix in table_map.items():
            table_name = f'{suffix}_hardlink_info_v4'
            # Get MD5s of this type
            type_md5s = [m for m in md5s if md5_types.get(m) == media_type]
            if not type_md5s:
                continue
            try:
                # Batch query in chunks of 500
                for i in range(0, len(type_md5s), 500):
                    chunk = type_md5s[i:i + 500]
                    placeholders = ','.join(['?'] * len(chunk))
                    rows = conn.execute(
                        f"SELECT md5, file_name, dir1, dir2 FROM [{table_name}] "
                        f"WHERE md5 IN ({placeholders})"
                        f"ORDER BY CASE WHEN substr(file_name, -6)='_h.dat' THEN 2 "
                        f"WHEN substr(file_name, -6)='_t.dat' THEN 3 ELSE 1 END",
                        chunk
                    ).fetchall()
                    seen_md5 = set()
                    for r in rows:
                        md5_val, file_name, dir1, dir2 = r
                        if md5_val in seen_md5:
                            continue
                        seen_md5.add(md5_val)
                        dir1_name = dir_map.get(dir1)
                        dir2_name = dir_map.get(dir2)
                        rel_path = None
                        if media_type == 3 and dir1_name and dir2_name:
                            rel_path = f'msg/attach/{dir1_name}/{dir2_name}/Img/{file_name}'
                        elif media_type == 43 and dir1_name and dir2_name:
                            rel_path = f'msg/attach/{dir1_name}/{dir2_name}/Video/{file_name}'
                        elif media_type == 6 and dir1_name:
                            rel_path = f'msg/attach/{dir1_name}/{file_name}'
                        elif media_type == 34 and dir1_name and dir2_name:
                            rel_path = f'msg/attach/{dir1_name}/{dir2_name}/Voice/{file_name}'
                        if rel_path:
                            parent = os.path.dirname(db_dir) if db_dir else ''
                            full_path = os.path.join(parent, rel_path.replace('/', os.sep))
                            if os.path.isfile(full_path):
                                try:
                                    result[md5_val] = {
                                        'exists': True,
                                        'size': os.path.getsize(full_path),
                                        'path': full_path,
                                    }
                                except OSError:
                                    pass
            except sqlite3.Error:
                continue
        conn.close()
    except sqlite3.Error:
        pass
    return result


def _load_file_sizes(decrypted_dir: str) -> dict:
    """Load md5->file_size from hardlink.db."""
    sizes = {}
    hardlink_db = os.path.join(decrypted_dir, 'hardlink', 'hardlink.db')
    if not os.path.isfile(hardlink_db):
        return sizes
    try:
        conn = sqlite3.connect(hardlink_db)
        for table in ('image_hardlink_info_v4', 'video_hardlink_info_v4',
                      'file_hardlink_info_v4'):
            try:
                for r in conn.execute(
                    f"SELECT md5, file_size FROM [{table}]"
                ):
                    md5, size = r
                    if md5 and size:
                        sizes[md5] = max(sizes.get(md5, 0), int(size))
            except sqlite3.Error:
                pass
        conn.close()
    except sqlite3.Error:
        pass
    return sizes


def _find_msg_tables(msg_dir: str, chat_id: str) -> list:
    """Find ALL message_N.db files containing the Msg_<hash> table for chat_id.

    Searches both the msg_dir and its parent for message_*.db files, merging
    results from both locations. WeChat 4.x shards a chat's messages across
    multiple DB files — all must be searched for accurate media counts.
    Returns list of (db_path, table_name) tuples.
    """
    hash_val = hashlib.md5(chat_id.encode()).hexdigest()
    table_name = f"Msg_{hash_val}"
    result = []

    # Collect db files from both msg_dir and parent
    all_db_paths = {}
    for search_dir in (msg_dir, os.path.dirname(msg_dir)):
        if not os.path.isdir(search_dir):
            continue
        for fname in os.listdir(search_dir):
            if fname.endswith('.db'):
                full = os.path.join(search_dir, fname)
                if full not in all_db_paths:
                    all_db_paths[full] = fname

    for db_path in sorted(all_db_paths.keys()):
        try:
            conn = sqlite3.connect(db_path)
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table_name,)
            ).fetchone()
            conn.close()
            if exists:
                result.append((db_path, table_name))
        except sqlite3.Error:
            continue
    return result


def _extract_md5_from_content(content) -> str:
    """Extract MD5 hash from message content (zstd XML or plain text)."""
    if not content:
        return None
    xml_str = None
    if isinstance(content, bytes):
        if len(content) >= 4 and content[:4] == b'\x28\xb5\x2f\xfd':
            try:
                import zstandard as zstd
                dctx = zstd.ZstdDecompressor()
                xml_str = dctx.decompress(content, max_output_size=65536).decode('utf-8', errors='replace')
            except Exception:
                pass
        if xml_str is None:
            try:
                xml_str = content.decode('utf-8', errors='replace')
            except Exception:
                pass
    elif isinstance(content, str):
        xml_str = content
    if not xml_str:
        return None
    for attr in ('md5', 'cdnattachmd5', 'newmd5', 'rawmd5'):
        m = re.search(rf'{attr}="([a-f0-9]{{32}})"', xml_str)
        if m:
            return m.group(1)
    return None

def _date_to_ts(date_str: str, end_of_day: bool = False) -> int:
    """Convert YYYY-MM-DD to Unix timestamp."""
    from datetime import datetime
    fmt = '%Y-%m-%d %H:%M:%S' if end_of_day else '%Y-%m-%d'
    if end_of_day:
        date_str = f'{date_str} 23:59:59'
    return int(datetime.strptime(date_str, fmt).timestamp())
