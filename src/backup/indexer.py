"""Build unified chats.db index from decrypted WeChat databases."""
import hashlib
import os
import re
import sqlite3
from datetime import datetime


def _date_str_to_ts(date_str: str, end_of_day: bool = False) -> int:
    """Convert YYYY-MM-DD to Unix timestamp."""
    fmt = '%Y-%m-%d %H:%M:%S' if end_of_day else '%Y-%m-%d'
    if end_of_day:
        date_str = f'{date_str} 23:59:59'
    return int(datetime.strptime(date_str, fmt).timestamp())


def _build_time_where(start_date: str, end_date: str) -> tuple:
    """Build a WHERE clause string and params list for create_time filtering."""
    clauses = ["create_time > 1000000000"]
    params = []
    if start_date:
        clauses.append("create_time >= ?")
        params.append(_date_str_to_ts(start_date))
    if end_date:
        clauses.append("create_time <= ?")
        params.append(_date_str_to_ts(end_date, end_of_day=True))
    return "WHERE " + " AND ".join(clauses), params


def build_index(decrypted_dir: str, output_db: str, on_progress=None,
                start_date: str = None, end_date: str = None) -> str:
    """Build a unified chats.db from all message_N.db files.

    Schema:
        chats: chat_id, display_name, message_count, first_msg_time, last_msg_time, is_group
        messages: chat_id, local_id, local_type, create_time (lean metadata)

    Args:
        decrypted_dir: Path to output dir containing message/ subdirectory with message_N.db files
        output_db: Path to write chats.db (e.g., output_dir/data/chats.db)
        on_progress: Optional callback(detail, progress_0_to_1)
        start_date: YYYY-MM-DD start of message date range (None = unbounded)
        end_date: YYYY-MM-DD end of message date range (None = unbounded)

    Returns:
        Path to the created database.
    """
    where_clause, where_params = _build_time_where(start_date, end_date)

    os.makedirs(os.path.dirname(output_db), exist_ok=True)
    conn = sqlite3.connect(output_db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS chats (
            chat_id TEXT PRIMARY KEY,
            display_name TEXT,
            message_count INTEGER DEFAULT 0,
            first_msg_time INTEGER,
            last_msg_time INTEGER,
            is_group INTEGER DEFAULT 0,
            source_db TEXT
        );
        CREATE TABLE IF NOT EXISTS messages (
            chat_id TEXT,
            local_id INTEGER,
            local_type INTEGER,
            create_time INTEGER,
            PRIMARY KEY (chat_id, local_id)
        );
        CREATE TABLE IF NOT EXISTS contacts (
            wxid TEXT PRIMARY KEY,
            display_name TEXT,
            remark TEXT,
            nick_name TEXT,
            alias TEXT,
            is_group INTEGER DEFAULT 0
        );
    """)
    # Migration: add source_db column to existing chats table
    try:
        conn.execute("ALTER TABLE chats ADD COLUMN source_db TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.execute("BEGIN")

    msg_dir = os.path.join(decrypted_dir, 'message')
    if not os.path.isdir(msg_dir):
        msg_dir = decrypted_dir  # fallback: raw dir itself

    db_files = sorted(
        [f for f in os.listdir(msg_dir) if f.endswith('.db')]
    ) if os.path.isdir(msg_dir) else []

    # Resolve display names: load contact.db + session.db once
    display_names = _load_display_names(decrypted_dir)

    def _report(detail, progress):
        if on_progress:
            on_progress(detail, progress)

    _report("创建索引表...", 0.05)

    skipped = []
    for fi, fname in enumerate(db_files):
        db_path = os.path.join(msg_dir, fname)
        base_progress = 0.05 + (fi / len(db_files)) * 0.9
        _report(f"扫描 {fname}...", base_progress)
        try:
            src = sqlite3.connect(db_path)
            tables = src.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
            ).fetchall()
            for ti, (tname,) in enumerate(tables):
                h = tname[4:]  # strip "Msg_" prefix
                chat_id = _resolve_chat_id(src, h) or f"unknown_{h[:8]}"
                display = display_names.get(chat_id, chat_id)
                is_group = 1 if chat_id.endswith('@chatroom') else 0
                count, first, last = src.execute(
                    f"SELECT COUNT(*), MIN(create_time), MAX(create_time) FROM [{tname}] {where_clause}",
                    where_params
                ).fetchone()

                # Aggregate across shards: merge with existing entry if present
                prev = conn.execute(
                    "SELECT message_count, first_msg_time, last_msg_time, source_db FROM chats WHERE chat_id=?",
                    (chat_id,)
                ).fetchone()
                if prev:
                    count = (prev[0] or 0) + (count or 0)
                    if first is None or (prev[1] and prev[1] < first):
                        first = prev[1]
                    if last is None or (prev[2] and prev[2] > last):
                        last = prev[2]
                    source_dbs = f"{prev[3]}, {fname}" if prev[3] else fname
                else:
                    source_dbs = fname

                conn.execute(
                    """INSERT OR REPLACE INTO chats(chat_id, display_name,
                       message_count, first_msg_time, last_msg_time, is_group, source_db)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (chat_id, display, count or 0, first, last, is_group, source_dbs)
                )

                # Index message metadata (batch insert)
                rows = src.execute(
                    f"SELECT local_id, local_type, create_time FROM [{tname}] {where_clause}",
                    where_params
                ).fetchall()
                conn.executemany(
                    "INSERT OR IGNORE INTO messages(chat_id, local_id, local_type, create_time) VALUES (?, ?, ?, ?)",
                    [(chat_id, r[0], r[1], r[2]) for r in rows]
                )
                if tables:
                    table_progress = base_progress + (ti / len(tables)) * (0.9 / len(db_files))
                    _report(f"索引 {fname} ({ti+1}/{len(tables)})", table_progress)
            src.close()
        except sqlite3.Error:
            skipped.append(fname)
            _report(f"跳过损坏: {fname}", base_progress + 0.05)
            continue

    _report("索引通讯录...", 0.93)
    _index_all_contacts(decrypted_dir, conn)

    _report("提交索引...", 0.97)
    conn.commit()
    conn.close()
    if skipped:
        _report(f"索引完成 (跳过: {', '.join(skipped)})", 1.0)
    else:
        _report("索引完成", 1.0)
    return output_db


def _resolve_chat_id(conn, hash_val: str) -> str:
    """Resolve Msg_<hash> back to a username via the Name2Id table.

    Returns username on match, or None.
    """
    try:
        for (uname,) in conn.execute("SELECT user_name FROM Name2Id"):
            if uname and hashlib.md5(uname.encode()).hexdigest() == hash_val:
                return uname
    except sqlite3.Error:
        pass
    return None


def _load_display_names(decrypted_dir: str) -> dict:
    """Pre-load all wxid → display_name mappings from contact.db and session.db.

    Mirrors the resolution logic from chat._resolve_display() but runs once
    during indexing so the web viewer can read pre-resolved names directly.
    """
    names = {}

    # 1. contact.db: extract display names from contact table
    contact_db = _find_file(decrypted_dir, 'contact/contact.db', 'contact.db')
    if os.path.isfile(contact_db):
        try:
            conn = sqlite3.connect(contact_db)
            for r in conn.execute(
                "SELECT username, remark, nick_name, alias FROM contact"
            ):
                uname, remark, nick, alias = r
                uname = (uname or '').strip()
                if not uname:
                    continue
                remark_v = (remark or '').strip()
                nick_v = (nick or '').strip()
                alias_v = (alias or '').strip()
                # Best name: remark > nick > alias > username
                # Skip fields containing replacement chars (garbled encoding).
                best = uname
                if remark_v and '�' not in remark_v and remark_v != uname:
                    best = remark_v
                elif nick_v and '�' not in nick_v and nick_v != uname:
                    best = nick_v
                elif alias_v and '�' not in alias_v and alias_v != uname:
                    best = alias_v
                names[uname] = best

                # Also index by alias for reverse lookup (wxid stored as alias)
                if alias_v and alias_v != uname and alias_v not in names:
                    names[alias_v] = best
            conn.close()
        except sqlite3.Error:
            pass

        # 2. chat_room owners: "xxx的群聊" fallback for groups
        try:
            conn = sqlite3.connect(contact_db)
            for r in conn.execute("SELECT username, owner FROM chat_room"):
                uname, owner = r
                uname = (uname or '').strip()
                owner = (owner or '').strip()
                if uname and owner and uname not in names:
                    owner_display = names.get(owner, owner)
                    names[uname] = f'{owner_display}的群聊'
            conn.close()
        except sqlite3.Error:
            pass

    # 3. session.db: session summaries for group names ("GroupName:preview")
    session_db = _find_file(decrypted_dir, 'session/session.db', 'session.db')
    if os.path.isfile(session_db):
        try:
            conn = sqlite3.connect(session_db)
            for r in conn.execute("SELECT username, summary FROM SessionTable"):
                uname, summary = r
                uname = (uname or '').strip()
                if not uname or uname in names:
                    continue
                summary = (summary or '').strip()
                if not summary:
                    continue
                for sep in (':', '：'):
                    if sep in summary:
                        name_part = summary.split(sep, 1)[0].strip()
                        if name_part and len(name_part) < 60:
                            names[uname] = name_part
                            break
                else:
                    if len(summary) < 40 and not any(c in summary for c in '\n\r'):
                        names[uname] = summary.strip()
            conn.close()
        except sqlite3.Error:
            pass

    return names


def _find_file(decrypted_dir: str, *rel_paths: str) -> str:
    """Find a file by trying multiple relative paths, with a shallow walk fallback."""
    for rel in rel_paths:
        path = os.path.join(decrypted_dir, rel.replace('/', os.sep))
        if os.path.isfile(path):
            return path
    target_name = os.path.basename(rel_paths[0])
    try:
        for entry in os.scandir(decrypted_dir):
            if entry.is_dir():
                candidate = os.path.join(entry.path, target_name)
                if os.path.isfile(candidate):
                    return candidate
            elif entry.is_file() and entry.name == target_name:
                return entry.path
    except OSError:
        pass
    return os.path.join(decrypted_dir, rel_paths[0].replace('/', os.sep))


def _index_all_contacts(decrypted_dir: str, conn: sqlite3.Connection) -> None:
    """Write ALL contacts from contact.db into the contacts table in chats.db.

    Pre-computes display names and dynamically discovers all available contact
    attributes (description, sex, region, signature, etc.) during indexing.
    """
    contact_db = _find_file(decrypted_dir, 'contact/contact.db', 'contact.db')
    if not os.path.isfile(contact_db):
        return

    # Reuse display name resolution (same as _load_display_names above)
    display_names = _load_display_names(decrypted_dir)

    # Discover available extra columns
    _KNOWN_EXTRA = ['description', 'sex', 'country', 'province', 'city',
                    'signature', 'small_head_url', 'big_head_url', 'contactType']
    try:
        src = sqlite3.connect(contact_db)
        available_cols = set(
            r[1] for r in src.execute("PRAGMA table_info(contact)").fetchall()
        )
        extra_cols = [c for c in _KNOWN_EXTRA if c in available_cols]

        # Migrate: add missing columns to chats.db contacts table
        existing_cols = set(
            r[1] for r in conn.execute("PRAGMA table_info(contacts)").fetchall()
        )
        for col in extra_cols:
            if col not in existing_cols:
                try:
                    conn.execute(f"ALTER TABLE contacts ADD COLUMN {col} TEXT")
                except sqlite3.OperationalError:
                    pass

        base_cols = ['wxid', 'display_name', 'remark', 'nick_name', 'alias', 'is_group']
        all_cols = base_cols + extra_cols
        placeholders = ', '.join(['?' for _ in all_cols])

        def _insert_contact(uname, display, remark, nick, alias, is_group, src_row):
            vals = [uname, display, remark, nick, alias, is_group]
            for c in extra_cols:
                val = src_row.get(c)
                vals.append(str(val).strip() if val is not None and str(val).strip() else '')
            conn.execute(
                f"INSERT OR REPLACE INTO contacts({', '.join(all_cols)}) VALUES ({placeholders})",
                vals
            )

        # Build column index map for the SELECT
        select_cols = ['username', 'remark', 'nick_name', 'alias'] + extra_cols
        select_str = ', '.join(select_cols)

        # Index all contacts (users + groups from contact table)
        for row in src.execute(f"SELECT {select_str} FROM contact"):
            d = dict(zip(select_cols, row))
            uname = (d.get('username') or '').strip()
            if not uname:
                continue
            is_group = 1 if uname.endswith('@chatroom') else 0
            display = display_names.get(uname, uname)
            remark = (d.get('remark') or '').strip()
            nick = (d.get('nick_name') or '').strip()
            alias = (d.get('alias') or '').strip()
            _insert_contact(uname, display, remark, nick, alias, is_group, d)

        # Index groups from chat_room table that may not be in contact table
        for r in src.execute("SELECT username, owner FROM chat_room"):
            uname, owner = r
            uname = (uname or '').strip()
            if not uname:
                continue
            display = display_names.get(uname, uname)
            _insert_contact(uname, display, '', '', '', 1, {})

        src.close()
    except sqlite3.Error:
        pass
