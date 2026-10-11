"""Adapter layer — ported chat_helpers functions for wrapped cards.

Bridges the gap between the tempWeChatDataAnalysis chat_helpers.py
and our engine/ services layer. Functions are ported verbatim where
engine/ has no equivalent; adapted where engine/ provides partial coverage.
"""
import base64
import html
import os
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

try:
    import zstandard as zstd
except Exception:
    zstd = None

_MD5_HEX_RE = re.compile(rb"(?i)[0-9a-f]{32}")
_DAT_MD5_RE = re.compile(rb"(?i)([0-9a-f]{32})(?:[._][thbc])?\.dat")
_PACKED_INFO_HEX_RE = re.compile(r"(?i)^[0-9a-f]+$")


# ---- Category A: Trivial helpers (copied verbatim) ----

def _quote_ident(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _build_avatar_url(account_dir_name: str, username: str) -> str:
    return f"/api/avatar/{quote(username)}"


def _decode_sqlite_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", errors="ignore")
        except Exception:
            return ""
    if isinstance(value, memoryview):
        try:
            return bytes(value).decode("utf-8", errors="ignore")
        except Exception:
            return ""
    return str(value)


def _is_mostly_printable_text(s: str) -> bool:
    if not s:
        return False
    sample = s[:600]
    if not sample:
        return False
    printable = sum(1 for ch in sample if ch.isprintable() or ch in {"\n", "\r", "\t"})
    return (printable / len(sample)) >= 0.85


def _looks_like_xml(s: str) -> bool:
    if not s:
        return False
    t = s.lstrip()
    if t.startswith('"') and t.endswith('"'):
        t = t.strip('"').lstrip()
    return t.startswith("<")


def _strip_cdata(s: str) -> str:
    if not s:
        return ""
    out = s.replace("<![CDATA[", "").replace("]]>", "")
    return out.strip()


def _extract_xml_tag_text(xml_text: str, tag: str) -> str:
    if not xml_text or not tag:
        return ""
    m = re.search(
        rf"<{re.escape(tag)}>(.*?)</{re.escape(tag)}>",
        xml_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not m:
        return ""
    return _strip_cdata(m.group(1) or "")


def _extract_xml_attr(xml_text: str, attr: str) -> str:
    if not xml_text or not attr:
        return ""
    m = re.search(rf"{re.escape(attr)}\s*=\s*['\"]([^'\"]+)['\"]", xml_text, flags=re.IGNORECASE)
    return (m.group(1) or "").strip() if m else ""


def _extract_md5_from_blob(blob: Any) -> str:
    if blob is None:
        return ""
    if isinstance(blob, memoryview):
        data = bytes(blob)
    elif isinstance(blob, (bytes, bytearray)):
        data = bytes(blob)
    else:
        try:
            data = bytes(blob)
        except Exception:
            return ""

    if not data:
        return ""

    try:
        m2 = _DAT_MD5_RE.findall(data)
    except Exception:
        m2 = []
    if m2:
        best2 = Counter([x.lower() for x in m2]).most_common(1)[0][0]
        try:
            return best2.decode("ascii", errors="ignore")
        except Exception:
            return ""

    m = _MD5_HEX_RE.findall(data)
    if not m:
        return ""
    best = Counter([x.lower() for x in m]).most_common(1)[0][0]
    try:
        return best.decode("ascii", errors="ignore")
    except Exception:
        return ""


def _resource_lookup_chat_id(resource_conn: sqlite3.Connection, username: str) -> Optional[int]:
    if not username:
        return None
    try:
        row = resource_conn.execute(
            "SELECT rowid FROM ChatName2Id WHERE user_name = ? LIMIT 1",
            (username,),
        ).fetchone()
        if row and row[0] is not None:
            return int(row[0])
    except Exception:
        return None
    return None


def _lookup_resource_md5(
    resource_conn: sqlite3.Connection,
    chat_id: Optional[int],
    message_local_type: int,
    server_id: int,
    local_id: int,
    create_time: int,
) -> str:
    if server_id <= 0 and local_id <= 0:
        return ""

    where_chat = ""
    params_prefix: list = []
    if chat_id is not None and int(chat_id) > 0:
        where_chat = " AND chat_id = ?"
        params_prefix.append(int(chat_id))

    where_type = ""
    if int(message_local_type) > 0:
        where_type = " AND message_local_type = ?"
        params_prefix.append(int(message_local_type))

    try:
        if server_id > 0:
            row = resource_conn.execute(
                "SELECT packed_info FROM MessageResourceInfo WHERE message_svr_id = ?"
                + where_chat
                + where_type
                + " ORDER BY message_id DESC LIMIT 1",
                [int(server_id)] + params_prefix,
            ).fetchone()
            if row and row[0] is not None:
                md5 = _extract_md5_from_blob(row[0])
                if md5:
                    return md5
    except Exception:
        pass

    try:
        if local_id > 0 and create_time > 0:
            row = resource_conn.execute(
                "SELECT packed_info FROM MessageResourceInfo WHERE message_local_id = ? AND message_create_time = ?"
                + where_chat
                + where_type
                + " ORDER BY message_id DESC LIMIT 1",
                [int(local_id), int(create_time)] + params_prefix,
            ).fetchone()
            if row and row[0] is not None:
                return _extract_md5_from_blob(row[0])
    except Exception:
        pass

    return ""


def _to_char_token_text(s: str) -> str:
    t = str(s or "").strip()
    if not t:
        return ""
    chars = [ch for ch in t.lower() if not ch.isspace()]
    return " ".join(chars)


def _pick_display_name(contact_row, fallback_username: str) -> str:
    if contact_row is None:
        return fallback_username

    uname = (fallback_username or '').strip()
    for key in ("remark", "nick_name", "alias"):
        try:
            v = contact_row[key]
        except Exception:
            v = None
        if isinstance(v, str) and v.strip():
            val = v.strip()
            if val != uname:
                return val

    return fallback_username


def _should_keep_session(username: str, include_official: bool) -> bool:
    if not username:
        return False

    if not include_official and username.startswith("gh_"):
        return False

    if username.startswith(("weixin", "qqmail", "fmessage", "medianote", "floatbottle", "newsapp")):
        return False

    if "@kefu.openim" in username:
        return False
    if "@openim" in username:
        return False
    if "service_" in username:
        return False

    if username in {
        "brandsessionholder",
        "brandservicesessionholder",
        "notifymessage",
        "opencustomerservicemsg",
        "notification_messages",
        "userexperience_alarm",
    }:
        return False

    return username.endswith("@chatroom") or username.startswith("wxid_") or ("@" not in username)


# ---- Category B: Significant helpers (ported verbatim) ----

def _decode_message_content(compress_value: Any, message_value: Any) -> str:
    def try_decode_text_blob(text: str) -> Optional[str]:
        t = (text or "").strip()
        if not t:
            return None

        zstd_magic = b"\x28\xb5\x2f\xfd"

        if len(t) >= 16 and len(t) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", t):
            try:
                raw = bytes.fromhex(t)
                if zstd is not None and raw.startswith(zstd_magic):
                    try:
                        out = zstd.decompress(raw)
                        s2 = out.decode("utf-8", errors="ignore")
                        s2 = html.unescape(s2.strip())
                        if _looks_like_xml(s2) or _is_mostly_printable_text(s2):
                            return s2
                    except Exception:
                        pass
                s2 = raw.decode("utf-8", errors="ignore")
                s2 = html.unescape(s2.strip())
                s2_lower = s2.lower()
                if (
                    _looks_like_xml(s2)
                    or ("<msg" in s2_lower and "</msg>" in s2_lower)
                    or "<appmsg" in s2_lower
                ):
                    return s2
            except Exception:
                return None

        if len(t) >= 24 and len(t) % 4 == 0 and re.fullmatch(r"[A-Za-z0-9+/=]+", t):
            try:
                raw = base64.b64decode(t)
                if zstd is not None and raw.startswith(zstd_magic):
                    try:
                        out = zstd.decompress(raw)
                        s2 = out.decode("utf-8", errors="ignore")
                        s2 = html.unescape(s2.strip())
                        if _looks_like_xml(s2) or _is_mostly_printable_text(s2):
                            return s2
                    except Exception:
                        pass
                s2 = raw.decode("utf-8", errors="ignore")
                s2 = html.unescape(s2.strip())
                s2_lower = s2.lower()
                if (
                    _looks_like_xml(s2)
                    or ("<msg" in s2_lower and "</msg>" in s2_lower)
                    or "<appmsg" in s2_lower
                ):
                    return s2
            except Exception:
                return None

        return None

    msg_text = _decode_sqlite_text(message_value)

    s = html.unescape(msg_text.strip())
    s2 = try_decode_text_blob(s)
    if s2:
        msg_text = s2

    if isinstance(message_value, (bytes, bytearray, memoryview)):
        raw = bytes(message_value) if isinstance(message_value, memoryview) else message_value
        if raw.startswith(b"\x28\xb5\x2f\xfd") and zstd is not None:
            try:
                out = zstd.decompress(raw)
                s = out.decode("utf-8", errors="ignore")
                s = html.unescape(s.strip())
                if _looks_like_xml(s) or _is_mostly_printable_text(s):
                    msg_text = s
            except Exception:
                pass

    if compress_value is None:
        return msg_text

    if isinstance(compress_value, str):
        s = html.unescape(compress_value.strip())
        s2 = try_decode_text_blob(s)
        if s2:
            return s2
        if _looks_like_xml(s) or _is_mostly_printable_text(s):
            return s
        return msg_text

    data: Optional[bytes] = None
    if isinstance(compress_value, memoryview):
        data = bytes(compress_value)
    elif isinstance(compress_value, (bytes, bytearray)):
        data = bytes(compress_value)

    if not data:
        return msg_text

    if zstd is not None:
        try:
            out = zstd.decompress(data)
            s = out.decode("utf-8", errors="ignore")
            s = html.unescape(s.strip())
            if _looks_like_xml(s) or _is_mostly_printable_text(s):
                return s
        except Exception:
            pass

    try:
        s = data.decode("utf-8", errors="ignore")
        s = html.unescape(s.strip())
        s2 = try_decode_text_blob(s)
        if s2:
            return s2
        if _looks_like_xml(s) or _is_mostly_printable_text(s):
            return s
    except Exception:
        pass

    return msg_text


def _load_contact_rows(contact_db_path: Path, usernames: list) -> dict:
    uniq = list(dict.fromkeys([u for u in usernames if u]))
    if not uniq:
        return {}

    result: dict = {}

    conn = sqlite3.connect(str(contact_db_path))
    conn.row_factory = sqlite3.Row
    try:
        def query_table(table: str, targets: list) -> None:
            if not targets:
                return
            placeholders = ",".join(["?"] * len(targets))
            sql = f"""
                SELECT username, remark, nick_name, alias, big_head_url, small_head_url
                FROM {table}
                WHERE username IN ({placeholders})
            """
            rows = conn.execute(sql, targets).fetchall()
            for r in rows:
                result[r["username"]] = r

        query_table("contact", uniq)
        missing = [u for u in uniq if u not in result]
        query_table("stranger", missing)
        return result
    finally:
        conn.close()


# ---- Category D: Adapter wrappers ----

def _iter_message_db_paths(account_dir: Path) -> list:
    """Scan account_dir for message*.db and biz_message*.db files.

    Searches both the account_dir root and its message/ subdirectory to
    handle both old (flat) and new (subdirectory) decrypted layouts.
    """
    if not account_dir.exists():
        return []

    def _match(name: str) -> bool:
        ln = name.lower()
        if ln in {"session.db", "contact.db", "head_image.db"}:
            return False
        if ln in {"message_resource.db", "message_fts.db"}:
            return False
        if re.match(r"^message(_\d+)?\.db$", ln):
            return True
        if re.match(r"^biz_message(_\d+)?\.db$", ln):
            return True
        return False

    candidates: list = []
    # Root directory
    for p in account_dir.glob("*.db"):
        if _match(p.name):
            candidates.append(p)
    # message/ subdirectory (current project layout)
    msg_subdir = account_dir / "message"
    if msg_subdir.is_dir():
        for p in msg_subdir.glob("*.db"):
            if _match(p.name):
                candidates.append(p)
    candidates.sort(key=lambda x: x.name)
    return candidates


def _detect_account_wxid(base_dir: Path) -> str | None:
    """Detect account owner wxid from message DB Name2Id tables.

    Samples Msg tables in the first available message DB, mapping real_sender_id
    values back to usernames. The wxid that sends messages in the most conversations
    is the account owner.
    """
    if not base_dir.is_dir():
        return None
    db_paths = _iter_message_db_paths(base_dir)
    if not db_paths:
        return None

    from collections import Counter

    for db_path in db_paths:
        if not db_path.exists():
            continue
        try:
            conn = sqlite3.connect(str(db_path))
            conn.text_factory = bytes
            try:
                tables = [
                    t[0] for t in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
                    ).fetchall()
                ]
                tables = [
                    (t.decode("utf-8", errors="ignore") if isinstance(t, bytes) else t)
                    for t in tables
                ]
                if not tables:
                    continue

                sender_counts: Counter[str] = Counter()
                sample = tables[:30]  # Sample first 30 tables for speed
                for t in sample:
                    try:
                        ids = conn.execute(
                            f'SELECT DISTINCT real_sender_id FROM "{t}"'
                        ).fetchall()
                    except Exception:
                        continue
                    for r in ids:
                        try:
                            uname = conn.execute(
                                "SELECT user_name FROM Name2Id WHERE rowid = ?",
                                (r[0],),
                            ).fetchone()
                        except Exception:
                            continue
                        if uname:
                            val = (
                                uname[0].decode("utf-8", errors="ignore")
                                if isinstance(uname[0], bytes)
                                else str(uname[0])
                            )
                            if val.startswith("wxid_"):
                                sender_counts[val] += 1

                if sender_counts:
                    best = sender_counts.most_common(1)[0][0]
                    return best
            finally:
                conn.close()
        except Exception:
            continue
    return None


def resolve_account_dir(account: Optional[str] = None) -> Path:
    """Resolve account directory from Flask app config.

    Unlike the FastAPI version which scans a global output dir, this wraps
    current_app.config to find the decrypted data.
    """
    from flask import current_app, abort

    decrypted_dir = current_app.config.get('DECRYPTED_DIR', '')
    default_wxid = current_app.config.get('WXID')

    if account:
        candidate = Path(decrypted_dir) / account
        if candidate.is_dir() and (candidate / "session.db").exists():
            return candidate
        candidate = Path(account)
        if candidate.is_dir() and (candidate / "session.db").exists():
            return candidate

    if default_wxid:
        candidate = Path(decrypted_dir) / default_wxid
        if candidate.is_dir() and (candidate / "session.db").exists():
            return candidate

    # Fallback: scan for accounts
    base = Path(decrypted_dir)
    if base.exists():
        for p in base.iterdir():
            if p.is_dir() and (p / "session.db").exists() and (p / "contact.db").exists():
                return p

    # Fallback: flat backup layout (no account subdirs). Detect wxid from Name2Id
    # and persist it so cards can find the account owner even in flat layouts.
    if base.is_dir():
        wxid = current_app.config.get('WXID') or _detect_account_wxid(base)
        if wxid:
            current_app.config['WXID'] = wxid
            # Write a fast cache file so future lookups are instant
            try:
                (base / '.wxid').write_text(wxid, encoding='ascii')
            except Exception:
                pass
            # Also persist to config file for background threads
            try:
                from engine.config_file import set_backup_data_dir
                set_backup_data_dir(str(base), wxid=wxid)
            except Exception:
                pass
        return base

    try:
        abort(404, description="No decrypted databases found.")
    except Exception:
        pass
    return Path(decrypted_dir)


def get_account_wxid(account_dir: Path) -> str:
    """Return the account owner wxid for the given data directory.

    Tries (in order): .wxid cache file, Flask app config, persisted config file,
    Name2Id detection, then falls back to account_dir.name.
    """
    # 1) Fast cache file in the data directory
    wxid_file = account_dir / '.wxid'
    if wxid_file.exists():
        try:
            wxid = wxid_file.read_text(encoding='ascii').strip()
            if wxid:
                return wxid
        except Exception:
            pass

    # 2) Flask app config (only available during request handling)
    try:
        from flask import current_app
        wxid = current_app.config.get('WXID', '')
        if wxid:
            return str(wxid)
    except Exception:
        pass

    # 3) Persisted config file (works from any thread)
    try:
        from engine.config_file import get_backup_wxid
        wxid = get_backup_wxid()
        if wxid:
            return wxid
    except Exception:
        pass

    # 4) Detect from Name2Id in message DBs (slow, last resort)
    wxid = _detect_account_wxid(account_dir)
    if wxid:
        # Cache for next time
        try:
            wxid_file.write_text(wxid, encoding='ascii')
        except Exception:
            pass
        return wxid

    # 5) Fallback
    return str(account_dir.name or "").strip()


def _year_range_epoch_seconds(year: int) -> tuple:
    """Return (start_epoch, end_epoch) for a given year in local time."""
    import time
    from datetime import datetime
    start = int(datetime(year, 1, 1).timestamp())
    end = int(datetime(year + 1, 1, 1).timestamp())
    return start, end


# ---- FTS5 stubs (return empty/absent so cards fall back to raw DB scans) ----


def get_chat_search_index_db_path(account_dir: Path) -> Path:
    """Stub: FTS5 search index is not available. Always returns a non-existent path."""
    return account_dir / "_chat_search_index_fts5_unavailable.db"


def _row_to_search_hit(
    row,
    *,
    db_path: Path,
    table_name: str,
    username: str,
    account_dir: Path,
    is_group: bool,
    my_rowid: int | None,
) -> dict:
    """Stub: minimal search-hit dict for moment payload rendering (no FTS5)."""
    content = ""
    render_type = "other"
    try:
        if hasattr(row, "keys"):
            content = _decode_message_content(
                row["compress_content"] if "compress_content" in row.keys() else None,
                row["message_content"] if "message_content" in row.keys() else None,
            )
            lt = int(row["local_type"] or 0) if "local_type" in row.keys() else 0
        else:
            lt = 0
    except Exception:
        lt = 0

    if lt == 1:
        render_type = "text"
    elif lt == 3:
        render_type = "image"
    elif lt == 34:
        render_type = "voice"
    elif lt == 43:
        render_type = "video"
    elif lt == 47:
        render_type = "emoji"
    elif lt in (49, 17179869233, 21474836529, 154618822705, 12884901937, 270582939697):
        render_type = "link"
    elif lt == 25769803825:
        render_type = "file"
    elif lt == 10000:
        render_type = "system"

    return {"content": content, "renderType": render_type}
