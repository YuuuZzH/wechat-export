"""
聊天记录导出 — 按联系人/群聊多选、时间、关键词过滤。
支持 TXT / 对话式 HTML / Excel（按时间平铺）/ CSV。
"""
import csv
import os
import re
import sqlite3
import zipfile
from datetime import datetime

from engine.constants import TZ, MSG_TYPES_CN
from engine.services.message.decode import decompress_content
from engine.services.message import _build_sender_map
from engine.services.sender_model import (ShardSenderModel, load_name2id)

# 导出会话格式（chatlab 走独立模块）
CHAT_FORMATS = ('txt', 'html', 'xlsx', 'csv', 'md', 'markdown', 'json', 'pgsql', 'sql')
# 别名 → 规范名
_FORMAT_ALIASES = {
    'markdown': 'md',
    'sql': 'pgsql',
}


def _normalize_fmt(fmt):
    fmt = (fmt or 'txt').strip().lower()
    fmt = _FORMAT_ALIASES.get(fmt, fmt)
    if fmt not in ('txt', 'html', 'xlsx', 'csv', 'md', 'json', 'pgsql'):
        return 'txt'
    return fmt


def _fmt_ext(fmt):
    return {'pgsql': 'sql', 'md': 'md'}.get(fmt, fmt)


def _write_by_fmt(fmt, out_path, messages, meta):
    if fmt == 'html':
        _write_html_conversation(out_path, messages, meta)
    elif fmt == 'xlsx':
        _write_xlsx(out_path, messages, meta)
    elif fmt == 'csv':
        _write_csv(out_path, messages, meta)
    elif fmt == 'md':
        _write_markdown(out_path, messages, meta)
    elif fmt == 'json':
        _write_json(out_path, messages, meta)
    elif fmt == 'pgsql':
        _write_pgsql(out_path, messages, meta)
    else:
        _write_txt(out_path, messages, meta)


def _extract_own_wxid(tables: list) -> str:
    """Derive own wxid from db_path directory structure."""
    for t in tables:
        db_path = os.path.abspath(t["db_path"])
        parts = db_path.replace("\\", "/").split("/")
        for i, part in enumerate(parts):
            if part == "db_storage" and i > 0:
                return parts[i - 1]
            if part == "message" and i >= 1 and parts[i - 1].startswith("wxid_"):
                return parts[i - 1]
        for part in parts:
            if part.startswith("wxid_") and len(part) > 6:
                return part
    try:
        import json
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(tables[0]["db_path"]))),
            "..", ".wechat_exp_config.json")
        for _ in range(3):
            config_path = os.path.normpath(config_path)
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    if cfg.get("last_backup_wxid"):
                        return cfg["last_backup_wxid"]
            config_path = os.path.join(
                os.path.dirname(config_path), "..", ".wechat_exp_config.json")
    except Exception:
        pass
    return ""


def _resolve_sender_any(uid, contact_db_path):
    """Resolve a username/wxid to its display name from contact.db."""
    if not uid or not contact_db_path or not os.path.isfile(contact_db_path):
        return uid
    try:
        conn = sqlite3.connect(f'file:{contact_db_path}?mode=ro', uri=True)
        row = conn.execute(
            "SELECT remark, nick_name, alias, username FROM contact WHERE username=?",
            (uid,)
        ).fetchone()
        if not row:
            m = re.match(r'^(.+?)_\d{2,}[a-z0-9]*$', uid)
            base = m.group(1) if m else uid
            pat = base.replace("_", chr(92) + "_") + chr(92) + "_%"
            row = conn.execute(
                "SELECT remark, nick_name, alias, username FROM contact "
                "WHERE username LIKE ? ESCAPE '\\' LIMIT 1",
                (pat,)
            ).fetchone()
        conn.close()
        if row:
            remark, nick, alias, username = [(v or '').strip() for v in row]
            uname = (username or '').strip()
            for cand in (remark, nick, alias):
                if cand and cand != uid and cand != uname:
                    return cand
        return uid
    except (sqlite3.Error, OSError):
        return uid


def _parse_keywords(keyword):
    """解析关键词：空格=AND，`|` 或 `OR`=OR。返回 (mode, terms)。"""
    if not keyword:
        return 'and', []
    raw = str(keyword).strip()
    if not raw:
        return 'and', []
    if '|' in raw or re.search(r'\s+OR\s+', raw, re.I):
        parts = re.split(r'\s*\|\s*|\s+OR\s+', raw, flags=re.I)
        terms = [p.strip() for p in parts if p.strip()]
        return 'or', terms
    terms = [t for t in raw.split() if t]
    return 'and', terms


def _match_keywords(text, mode, terms):
    if not terms:
        return True
    hay = text or ''
    if mode == 'or':
        return any(t in hay for t in terms)
    return all(t in hay for t in terms)


def _find_contact_db(tables):
    contact_db_path = None
    for t in tables:
        d = os.path.dirname(t["db_path"])
        parent = os.path.dirname(d)
        candidate = os.path.join(parent, "contact", "contact.db")
        if os.path.exists(candidate):
            return candidate
    for t in tables:
        db_path = t["db_path"]
        parts = db_path.replace("\\", "/").split("/")
        for i in range(len(parts)):
            cand = "/".join(parts[:i] + ["contact", "contact.db"])
            if os.path.exists(cand):
                return cand
    return contact_db_path


def _load_sender_map(contact_db_path):
    sender_map = {}
    if not contact_db_path:
        return sender_map
    try:
        conn = sqlite3.connect(contact_db_path)
        for r in conn.execute(
            "SELECT id, remark, nick_name, alias, username FROM contact"
        ):
            wxid = (r[4] or '').strip()
            if not wxid:
                continue
            remark_v = (r[1] or '').strip()
            nick_v = (r[2] or '').strip()
            alias_v = (r[3] or '').strip()
            contact_display = remark_v if (remark_v and remark_v != wxid) else (
                nick_v if (nick_v and nick_v != wxid) else (
                    alias_v if (alias_v and alias_v != wxid) else wxid
                )
            )
            if contact_display:
                sender_map[wxid] = contact_display
            if remark_v and remark_v != wxid:
                m = re.match(r'^(.+?)_\d{2,}[a-z0-9]*$', wxid)
                if m:
                    base = m.group(1)
                    if base not in sender_map:
                        sender_map[base] = contact_display
        conn.close()
    except (sqlite3.Error, OSError):
        pass
    return sender_map


def _normalize_tables(chat_info):
    """确保 tables 条目含 table_name（兼容 fast_chat_list 旧结构）。"""
    import hashlib
    uname = chat_info.get("username") or ""
    tables = chat_info.get("tables") or []
    if not tables and uname:
        return tables
    default_tname = ("Msg_%s" % hashlib.md5(uname.encode()).hexdigest()) if uname else None
    out = []
    for t in tables:
        if not isinstance(t, dict):
            continue
        entry = dict(t)
        if not entry.get("table_name") and default_tname:
            entry["table_name"] = default_tname
        if entry.get("db_path") and entry.get("table_name"):
            out.append(entry)
    return out


def collect_messages(chat_info, start_ts=None, end_ts=None, keyword=None,
                     include_asr=False, asr_lookup=None, display_name=None):
    """收集单个会话的消息，返回标准化消息列表（已按时间排序）。"""
    if include_asr and asr_lookup is None:
        asr_lookup = make_asr_lookup(_chat_id_of(chat_info))

    uname = chat_info["username"]
    display = display_name or chat_info.get("display_name") or uname
    tables = _normalize_tables(chat_info)
    if not tables:
        return []
    kw_mode, kw_terms = _parse_keywords(keyword)

    all_rows = []
    for t in tables:
        conn = None
        try:
            conn = sqlite3.connect(t["db_path"])
            where = []
            params = []
            if start_ts:
                where.append("create_time >= ?")
                params.append(start_ts)
            if end_ts:
                where.append("create_time <= ?")
                params.append(end_ts)
            # 关键词在解码后再过滤（压缩/二进制内容 SQL LIKE 不可靠）
            query = f"""
                SELECT local_id, local_type, real_sender_id, create_time,
                       status, message_content, origin_source
                FROM [{t['table_name']}]
                WHERE create_time > 1000000000
            """
            if where:
                query += " AND " + " AND ".join(where)
            query += " ORDER BY create_time ASC"
            for r in conn.execute(query, params):
                all_rows.append((t["db_path"], r))
        except (sqlite3.Error, OSError):
            pass
        finally:
            if conn:
                conn.close()

    if not all_rows:
        return []

    all_rows.sort(key=lambda r: r[1][3] or 0)

    contact_db_path = _find_contact_db(tables)
    sender_map = _load_sender_map(contact_db_path)

    own_wxid = _extract_own_wxid(tables)
    db_sender_maps = {}
    db_name2id = {}
    db_models = {}
    for t in tables:
        try:
            conn = sqlite3.connect(t["db_path"])
            db_sender_maps[t["db_path"]] = _build_sender_map(
                conn, t["table_name"], own_wxid=own_wxid, chat_id=uname)
            db_name2id[t["db_path"]] = load_name2id(conn)
            conn.close()
        except (sqlite3.Error, OSError):
            db_sender_maps[t["db_path"]] = {}
            db_name2id[t["db_path"]] = {}
        _n2i = db_name2id.get(t["db_path"]) or {}
        db_models[t["db_path"]] = ShardSenderModel(_n2i, uname, own_wxid)

    is_group = uname.endswith("@chatroom")
    messages = []

    for db_path, row in all_rows:
        local_id, ltype_raw, sender_id, create_time, status, content, origin = row
        base_type = ltype_raw & 0xFFFF if isinstance(ltype_raw, int) else ltype_raw

        if isinstance(content, bytes):
            content = decompress_content(content)
        if isinstance(content, bytes):
            try:
                content = content.decode('utf-8', errors='replace')
            except Exception:
                content = ""

        _model = db_models.get(db_path)
        if _model is not None:
            _side, _src, _member = _model.classify(
                sender_id, origin, content, local_type=base_type)
        else:
            _side, _member = ("me" if origin == 1 else "unknown"), None

        if _side == "me":
            sender = "我"
        elif _side == "system":
            sender = "系统消息"
        elif _side == "unknown":
            sender = "归属未定"
        else:
            _who = _member or ("" if is_group else uname)
            if _who:
                sender = sender_map.get(_who, _who)
                if sender == _who:
                    sender = _resolve_sender_any(_who, contact_db_path) or _who
            else:
                sender = f"ID:{sender_id}" if sender_id else ""

        voice_text = None
        if include_asr and asr_lookup and base_type == 34:
            try:
                voice_text = asr_lookup(create_time, local_id)
            except Exception:
                voice_text = None
        text = _format_content(content, base_type, is_group, voice_text=voice_text)

        # Keyword filter matches message body only (not sender display name).
        # Otherwise nicknames like "洲收号" falsely match keyword "号".
        if not _match_keywords(text, kw_mode, kw_terms):
            continue

        dt = datetime.fromtimestamp(create_time, tz=TZ)
        messages.append({
            "create_time": create_time,
            "timestamp": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "date": dt.strftime("%Y-%m-%d"),
            "sender": sender,
            "is_me": _side == "me",
            "is_system": _side == "system" or base_type in (10000, 10002),
            "type": base_type,
            "type_name": MSG_TYPES_CN.get(base_type, f"类型{base_type}"),
            "text": text,
            "chat_name": display,
            "chat_id": uname,
            "is_group": is_group,
            "local_id": local_id,
        })

    return messages


def export_chat(chat_info, out_dir, start_ts=None, end_ts=None, keyword=None,
                print_fn=None, progress_fn=None, fmt='txt', display_name=None,
                include_asr=False, asr_lookup=None):
    """导出单个聊天的消息记录。

    Returns: (msg_count, file_path)
    """
    if print_fn is None:
        print_fn = print

    fmt = _normalize_fmt(fmt)

    display = display_name or chat_info.get("display_name") or chat_info["username"]
    messages = collect_messages(
        chat_info, start_ts=start_ts, end_ts=end_ts, keyword=keyword,
        include_asr=include_asr, asr_lookup=asr_lookup, display_name=display)

    if not messages:
        return 0, None

    meta = {
        "display": display,
        "uname": chat_info["username"],
        "start_ts": start_ts,
        "end_ts": end_ts,
        "keyword": keyword or "",
        "exported_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
    }

    safe_name = re.sub(r'[<>:"/\\|?*]', '_', display)[:40] or 'chat'
    os.makedirs(out_dir, exist_ok=True)
    ext = _fmt_ext(fmt)
    out_path = os.path.join(out_dir, f"{safe_name}.{ext}")
    _write_by_fmt(fmt, out_path, messages, meta)

    return len(messages), out_path


def export_chats(chat_infos, out_dir, start_ts=None, end_ts=None, keyword=None,
                 print_fn=None, progress_fn=None, fmt='txt', include_asr=False,
                 merge=True):
    """导出多个会话。

    - xlsx/csv：默认合并为一个按时间平铺的文件（含「会话」列）
    - txt/html：默认每个会话一个文件，多个时打成 zip；merge=True 时合并为单文件

    Returns: (total_count, file_path, details)
      details = [(display_name, count, path_or_None), ...]
    """
    if print_fn is None:
        print_fn = print
    if progress_fn is None:
        progress_fn = lambda pct, msg: None

    fmt = _normalize_fmt(fmt)

    os.makedirs(out_dir, exist_ok=True)
    all_msgs = []
    details = []
    total = len(chat_infos) or 1

    for i, chat in enumerate(chat_infos):
        display = chat.get("display_name") or chat["username"]
        progress_fn(int((i + 1) / total * 90), f"导出: {display}")
        msgs = collect_messages(
            chat, start_ts=start_ts, end_ts=end_ts, keyword=keyword,
            include_asr=include_asr, display_name=display)
        details.append((display, len(msgs), None))
        if msgs:
            all_msgs.extend(msgs)
            print_fn(f"  {display}: {len(msgs)} 条消息")

    if not all_msgs:
        return 0, None, details

    all_msgs.sort(key=lambda m: (m["create_time"] or 0, m.get("local_id") or 0))

    stamp = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
    kw_tag = ""
    if keyword:
        safe_kw = re.sub(r'[<>:"/\\|?*\s]+', '_', str(keyword))[:20]
        kw_tag = f"_kw_{safe_kw}"

    meta = {
        "display": "多会话合并" if len(chat_infos) > 1 else (
            chat_infos[0].get("display_name") or chat_infos[0]["username"]),
        "uname": ",".join(c["username"] for c in chat_infos[:20]),
        "start_ts": start_ts,
        "end_ts": end_ts,
        "keyword": keyword or "",
        "exported_at": datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "chat_count": len(chat_infos),
    }

    # 表格/结构化：默认按时间平铺合并；txt/html/md 可按 merge 合并
    merge_fmts = ('xlsx', 'csv', 'json', 'pgsql')
    text_fmts = ('txt', 'html', 'md')
    if fmt in merge_fmts or (merge and len(chat_infos) >= 1 and fmt in text_fmts):
        ext = _fmt_ext(fmt)
        name = f"messages_{stamp}{kw_tag}.{ext}"
        out_path = os.path.join(out_dir, name)
        _write_by_fmt(fmt, out_path, all_msgs, meta)
        progress_fn(100, "完成")
        return len(all_msgs), out_path, details

    # 多文件 + zip
    file_paths = []
    new_details = []
    for chat in chat_infos:
        display = chat.get("display_name") or chat["username"]
        count, path = export_chat(
            chat, out_dir, start_ts=start_ts, end_ts=end_ts, keyword=keyword,
            print_fn=print_fn, fmt=fmt, display_name=display,
            include_asr=include_asr)
        new_details.append((display, count, path))
        if path:
            file_paths.append(path)

    if len(file_paths) == 1:
        progress_fn(100, "完成")
        return new_details[0][1], file_paths[0], new_details

    zip_name = f"messages_{stamp}{kw_tag}.zip"
    zip_path = os.path.join(out_dir, zip_name)
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for p in file_paths:
            zf.write(p, arcname=os.path.basename(p))
    progress_fn(100, "完成")
    return sum(d[1] for d in new_details), zip_path, new_details


def export_all_contacts(decrypted_dir, out_dir, start_ts=None, end_ts=None,
                        keyword=None, name_filter=None, print_fn=None,
                        progress_fn=None, fmt='txt', include_asr=False):
    """导出所有匹配联系人的聊天记录。"""
    from chat_list import scan_chats

    if print_fn is None:
        print_fn = print
    if progress_fn is None:
        progress_fn = lambda pct, msg: None

    chats, _, _ = scan_chats(decrypted_dir)
    selected = []
    for c in chats:
        if name_filter:
            kw = name_filter.lower()
            if kw not in c["display_name"].lower() and kw not in c["username"].lower():
                continue
        selected.append(c)

    # 表格/结构化合并；txt/html/md 按会话分文件（多文件时打 zip）
    fmt = _normalize_fmt(fmt)
    merge = fmt in ('xlsx', 'csv', 'json', 'pgsql')
    _total, path, details = export_chats(
        selected, out_dir, start_ts=start_ts, end_ts=end_ts, keyword=keyword,
        print_fn=print_fn, progress_fn=progress_fn, fmt=fmt,
        include_asr=include_asr, merge=merge)
    if path:
        print_fn(f"输出文件: {os.path.basename(path)}")
    return details


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def _meta_lines(meta, msg_count):
    lines = [
        f"联系人: {meta.get('display', '')}",
        f"微信号: {meta.get('uname', '')}",
        f"导出时间: {meta.get('exported_at', '')}",
    ]
    if meta.get("chat_count") and meta["chat_count"] > 1:
        lines.append(f"会话数: {meta['chat_count']}")
    if meta.get("start_ts"):
        lines.append(
            f"起始: {datetime.fromtimestamp(meta['start_ts'], tz=TZ).strftime('%Y-%m-%d %H:%M:%S')}")
    if meta.get("end_ts"):
        lines.append(
            f"结束: {datetime.fromtimestamp(meta['end_ts'], tz=TZ).strftime('%Y-%m-%d %H:%M:%S')}")
    if meta.get("keyword"):
        lines.append(f"关键词: {meta['keyword']}")
    lines.append(f"消息数: {msg_count}")
    lines.append("=" * 70)
    lines.append("")
    return lines


def _write_txt(out_path, messages, meta):
    lines = _meta_lines(meta, len(messages))
    multi = (meta.get("chat_count") or 0) > 1
    for m in messages:
        type_tag = ""
        if m["type"] != 1:
            type_tag = f"[{m['type_name']}] "
        prefix = f"[{m['chat_name']}] " if multi else ""
        lines.append(f"[{m['timestamp']}] {prefix}{type_tag}{m['sender']}: {m['text']}")
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


def _write_markdown(out_path, messages, meta):
    """Markdown 对话导出（便于博客/仓库阅读）。"""
    lines = [
        f"# {meta.get('display') or '聊天记录'}",
        "",
        f"- 会话标识: `{meta.get('uname') or ''}`",
        f"- 导出时间: {meta.get('exported_at') or ''}",
        f"- 消息数: {len(messages)}",
    ]
    if meta.get("keyword"):
        lines.append(f"- 关键词: {meta['keyword']}")
    lines.extend(["", "---", ""])
    multi = (meta.get("chat_count") or 0) > 1
    prev_date = None
    for m in messages:
        day = (m.get("date") or (m.get("timestamp") or "")[:10]) or ""
        if day and day != prev_date:
            lines.append(f"## {day}")
            lines.append("")
            prev_date = day
        type_tag = ""
        if m.get("type") != 1:
            type_tag = f"**[{m.get('type_name') or ''}]** "
        chat_prefix = f"[{m.get('chat_name')}] " if multi else ""
        text = (m.get("text") or "").replace("\r\n", "\n").replace("\r", "\n")
        # 缩进多行正文，避免打断列表
        if "\n" in text:
            body = "\n".join(("  " + ln) if ln else "" for ln in text.split("\n"))
            lines.append(f"- **{chat_prefix}{m.get('sender') or ''}** · `{m.get('timestamp') or ''}`  ")
            lines.append(f"  {type_tag}")
            lines.append(body)
        else:
            lines.append(
                f"- **{chat_prefix}{m.get('sender') or ''}** · `{m.get('timestamp') or ''}`: "
                f"{type_tag}{text}"
            )
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


def _write_json(out_path, messages, meta):
    """纯 JSON 消息数组（非 ChatLab 专属格式）。"""
    import json
    payload = {
        "meta": {
            "display": meta.get("display"),
            "username": meta.get("uname"),
            "chatCount": meta.get("chat_count") or 1,
            "exportedAt": meta.get("exported_at"),
            "keyword": meta.get("keyword") or "",
            "messageCount": len(messages),
        },
        "messages": [
            {
                "createTime": m.get("create_time"),
                "timestamp": m.get("timestamp"),
                "date": m.get("date"),
                "sender": m.get("sender"),
                "isMe": bool(m.get("is_me")),
                "isSystem": bool(m.get("is_system")),
                "type": m.get("type"),
                "typeName": m.get("type_name"),
                "text": m.get("text"),
                "chatName": m.get("chat_name"),
                "chatId": m.get("chat_id"),
                "isGroup": bool(m.get("is_group")),
                "localId": m.get("local_id"),
            }
            for m in messages
        ],
    }
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _sql_escape(s):
    if s is None:
        return 'NULL'
    return "'" + str(s).replace("'", "''") + "'"


def _write_pgsql(out_path, messages, meta):
    """PostgreSQL 兼容的 DDL + INSERT（可用 psql 导入）。"""
    lines = [
        "-- WeChat Export → PostgreSQL",
        f"-- display: {meta.get('display') or ''}",
        f"-- exported_at: {meta.get('exported_at') or ''}",
        f"-- message_count: {len(messages)}",
        "",
        "CREATE TABLE IF NOT EXISTS wechat_messages (",
        "  id BIGSERIAL PRIMARY KEY,",
        "  create_time BIGINT,",
        "  timestamp TEXT,",
        "  chat_id TEXT,",
        "  chat_name TEXT,",
        "  sender TEXT,",
        "  is_me BOOLEAN,",
        "  is_system BOOLEAN,",
        "  msg_type INTEGER,",
        "  type_name TEXT,",
        "  body TEXT,",
        "  is_group BOOLEAN,",
        "  local_id BIGINT",
        ");",
        "",
        "BEGIN;",
    ]
    for m in messages:
        lines.append(
            "INSERT INTO wechat_messages "
            "(create_time, timestamp, chat_id, chat_name, sender, is_me, is_system, "
            "msg_type, type_name, body, is_group, local_id) VALUES ("
            f"{int(m.get('create_time') or 0)}, "
            f"{_sql_escape(m.get('timestamp'))}, "
            f"{_sql_escape(m.get('chat_id'))}, "
            f"{_sql_escape(m.get('chat_name'))}, "
            f"{_sql_escape(m.get('sender'))}, "
            f"{'TRUE' if m.get('is_me') else 'FALSE'}, "
            f"{'TRUE' if m.get('is_system') else 'FALSE'}, "
            f"{int(m.get('type') or 0)}, "
            f"{_sql_escape(m.get('type_name'))}, "
            f"{_sql_escape(m.get('text'))}, "
            f"{'TRUE' if m.get('is_group') else 'FALSE'}, "
            f"{int(m.get('local_id') or 0)}"
            ");"
        )
    lines.extend(["COMMIT;", ""])
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))


def _write_csv(out_path, messages, meta):
    with open(out_path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(["时间", "会话", "会话ID", "发送者", "类型", "内容"])
        for m in messages:
            w.writerow([
                m["timestamp"], m["chat_name"], m["chat_id"],
                m["sender"], m["type_name"], m["text"],
            ])


def _write_xlsx(out_path, messages, meta):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError as e:
        raise RuntimeError('导出 xlsx 需要 openpyxl：pip install openpyxl') from e

    # 清理 openpyxl 非法控制字符
    ctrl_re = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')

    def clean(v):
        if v is None:
            return ''
        return ctrl_re.sub('', str(v))

    wb = Workbook()
    ws = wb.active
    ws.title = '聊天记录'
    headers = ["时间", "会话", "发送者", "类型", "内容", "会话ID"]
    ws.append(headers)

    header_fill = PatternFill('solid', fgColor='1F4E79')
    header_font = Font(bold=True, color='FFFFFF')
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center', vertical='center')
    ws.freeze_panes = 'A2'

    me_fill = PatternFill('solid', fgColor='FFE8D6')
    sys_fill = PatternFill('solid', fgColor='F5F5F5')

    for m in messages:
        row = [
            clean(m["timestamp"]),
            clean(m["chat_name"]),
            clean(m["sender"]),
            clean(m["type_name"]),
            clean(m["text"]),
            clean(m["chat_id"]),
        ]
        ws.append(row)
        r = ws.max_row
        if m.get("is_me"):
            for c in range(1, 7):
                ws.cell(r, c).fill = me_fill
        elif m.get("is_system"):
            for c in range(1, 7):
                ws.cell(r, c).fill = sys_fill

    # 元信息 sheet
    info = wb.create_sheet('导出信息', 0)
    for label, val in [
        ("联系人/会话", meta.get("display", "")),
        ("会话标识", meta.get("uname", "")),
        ("会话数", meta.get("chat_count") or 1),
        ("导出时间", meta.get("exported_at", "")),
        ("关键词", meta.get("keyword") or "（无）"),
        ("消息数", len(messages)),
    ]:
        info.append([label, val])

    widths = [20, 22, 14, 10, 60, 28]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    info.column_dimensions['A'].width = 14
    info.column_dimensions['B'].width = 50

    wb.save(out_path)


def _write_html_conversation(out_path, messages, meta):
    """对话气泡样式 HTML，自带页内搜索高亮（离线可用）。"""
    multi = (meta.get("chat_count") or 0) > 1 or len({m["chat_id"] for m in messages}) > 1
    keyword = meta.get("keyword") or ""

    bubbles = []
    last_date = None
    for m in messages:
        if m["date"] != last_date:
            last_date = m["date"]
            bubbles.append(
                f'<div class="day-sep"><span>{_escape_html(last_date)}</span></div>')

        if m.get("is_system"):
            bubbles.append(
                f'<div class="sys">{_escape_html(m["text"])}</div>')
            continue

        side = "me" if m.get("is_me") else "other"
        chat_badge = ""
        if multi:
            chat_badge = (
                f'<div class="chat-badge">{_escape_html(m["chat_name"])}</div>')
        type_badge = ""
        if m["type"] != 1:
            type_badge = f'<span class="type-tag">{_escape_html(m["type_name"])}</span> '
        bubbles.append(f'''
<div class="row {side}" data-text="{_escape_html(m['sender'] + ' ' + m['text'])}">
  {chat_badge}
  <div class="meta"><span class="sender">{_escape_html(m["sender"])}</span>
    <span class="time">{_escape_html(m["timestamp"])}</span></div>
  <div class="bubble">{type_badge}{_escape_html(m["text"])}</div>
</div>''')

    kw_attr = _escape_html(keyword)
    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>聊天记录 - {_escape_html(meta.get("display", ""))}</title>
<style>
  :root {{
    --bg: #ededed; --panel: #f7f7f7; --me: #95ec69; --other: #fff;
    --text: #111; --muted: #888; --accent: #07c160;
  }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    background: var(--bg); color: var(--text);
    font-family: "Microsoft YaHei","PingFang SC",sans-serif;
    min-height: 100vh;
  }}
  .top {{
    position: sticky; top: 0; z-index: 10;
    background: #2e2e2e; color: #fff; padding: 12px 16px;
    display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
    box-shadow: 0 1px 4px rgba(0,0,0,.2);
  }}
  .top h1 {{ font-size: 16px; font-weight: 600; margin-right: auto; }}
  .top .meta {{ font-size: 12px; color: #bbb; width: 100%; }}
  .search-box {{
    display: flex; gap: 8px; align-items: center; flex: 1; min-width: 220px;
    max-width: 420px;
  }}
  .search-box input {{
    flex: 1; border: none; border-radius: 16px; padding: 8px 14px;
    font-size: 13px; outline: none;
  }}
  .search-box button {{
    background: var(--accent); color: #fff; border: none; border-radius: 16px;
    padding: 8px 14px; cursor: pointer; font-size: 13px;
  }}
  .hit-count {{ font-size: 12px; color: #bbb; white-space: nowrap; }}
  .thread {{
    max-width: 820px; margin: 0 auto; padding: 16px 12px 40px;
  }}
  .day-sep {{
    text-align: center; margin: 18px 0 10px; color: var(--muted); font-size: 12px;
  }}
  .day-sep span {{
    background: rgba(0,0,0,.08); padding: 3px 10px; border-radius: 10px;
  }}
  .sys {{
    text-align: center; color: var(--muted); font-size: 12px;
    margin: 10px 0; padding: 0 24px;
  }}
  .row {{
    display: flex; flex-direction: column; margin: 10px 0;
    max-width: 78%;
  }}
  .row.me {{ margin-left: auto; align-items: flex-end; }}
  .row.other {{ margin-right: auto; align-items: flex-start; }}
  .chat-badge {{
    font-size: 11px; color: #07c160; margin-bottom: 2px;
  }}
  .meta {{
    font-size: 11px; color: var(--muted); margin-bottom: 3px;
    display: flex; gap: 8px;
  }}
  .row.me .meta {{ flex-direction: row-reverse; }}
  .bubble {{
    padding: 10px 12px; border-radius: 8px; font-size: 14px;
    line-height: 1.55; white-space: pre-wrap; word-break: break-word;
    box-shadow: 0 1px 1px rgba(0,0,0,.06);
  }}
  .row.me .bubble {{ background: var(--me); border-top-right-radius: 2px; }}
  .row.other .bubble {{ background: var(--other); border-top-left-radius: 2px; }}
  .type-tag {{
    display: inline-block; font-size: 11px; color: #666;
    background: rgba(0,0,0,.06); border-radius: 4px; padding: 1px 5px;
    margin-right: 4px;
  }}
  mark {{
    background: #ffe58f; color: inherit; padding: 0 1px; border-radius: 2px;
  }}
  .row.dim {{ opacity: .28; }}
  .empty {{ text-align: center; color: var(--muted); padding: 48px 0; }}
</style>
</head>
<body>
<div class="top">
  <h1>{_escape_html(meta.get("display", "聊天记录"))}</h1>
  <div class="search-box">
    <input id="q" type="search" placeholder="页内搜索关键词…" value="{kw_attr}">
    <button type="button" id="btn-q">查找</button>
    <span class="hit-count" id="hits"></span>
  </div>
  <div class="meta">
    消息 {_escape_html(str(len(messages)))} 条
    · 导出 {_escape_html(meta.get("exported_at", ""))}
    {" · 关键词: " + _escape_html(keyword) if keyword else ""}
  </div>
</div>
<div class="thread" id="thread">
{''.join(bubbles) if bubbles else '<div class="empty">没有匹配的消息</div>'}
</div>
<script>
(function() {{
  var input = document.getElementById('q');
  var hitsEl = document.getElementById('hits');
  var rows = Array.prototype.slice.call(document.querySelectorAll('.row'));

  function clearMarks(el) {{
    el.querySelectorAll('mark').forEach(function(m) {{
      var t = document.createTextNode(m.textContent);
      m.parentNode.replaceChild(t, m);
    }});
    el.normalize();
  }}

  function highlight(el, term) {{
    if (!term) return 0;
    var walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null);
    var nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    var count = 0;
    nodes.forEach(function(node) {{
      var text = node.nodeValue;
      var idx = text.toLowerCase().indexOf(term.toLowerCase());
      if (idx < 0) return;
      var frag = document.createDocumentFragment();
      var pos = 0;
      while (idx >= 0) {{
        if (idx > pos) frag.appendChild(document.createTextNode(text.slice(pos, idx)));
        var mark = document.createElement('mark');
        mark.textContent = text.slice(idx, idx + term.length);
        frag.appendChild(mark);
        count++;
        pos = idx + term.length;
        idx = text.toLowerCase().indexOf(term.toLowerCase(), pos);
      }}
      if (pos < text.length) frag.appendChild(document.createTextNode(text.slice(pos)));
      node.parentNode.replaceChild(frag, node);
    }});
    return count;
  }}

  function run() {{
    var term = (input.value || '').trim();
    var total = 0;
    rows.forEach(function(row) {{
      clearMarks(row);
      var blob = row.getAttribute('data-text') || '';
      var ok = !term || blob.toLowerCase().indexOf(term.toLowerCase()) >= 0;
      row.classList.toggle('dim', !ok);
      if (ok && term) {{
        var bubble = row.querySelector('.bubble');
        if (bubble) total += highlight(bubble, term);
      }}
    }});
    hitsEl.textContent = term ? ('命中 ' + total + ' 处') : '';
  }}

  document.getElementById('btn-q').addEventListener('click', run);
  input.addEventListener('keydown', function(e) {{
    if (e.key === 'Enter') run();
  }});
  if (input.value) run();
}})();
</script>
</body>
</html>'''
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)


def _escape_html(text):
    if text is None:
        return ''
    return (str(text)
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;'))


def _format_content(content, base_type, is_group, voice_text=None):
    """格式化消息内容为可读文本。"""
    if content is None:
        return ""

    if is_group and isinstance(content, str) and ":\n" in content[:100]:
        parts = content.split(":\n", 1)
        if len(parts) == 2:
            content = parts[1]

    if base_type == 1:
        return content
    elif base_type == 3:
        return "[图片]"
    elif base_type == 6:
        return "[文件]"
    elif base_type == 34:
        spoken = (voice_text or '').strip()
        return "[语音] %s" % spoken if spoken else "[语音]"
    elif base_type == 42:
        return "[名片]"
    elif base_type == 43:
        return "[视频]"
    elif base_type == 47:
        return "[表情]"
    elif base_type == 48:
        return "[位置]"
    elif base_type == 49:
        _APP_TYPE_LABELS = {
            '16': '音乐', '33': '小程序', '36': '视频号',
            '2001': '红包', '2000': '转账',
        }
        if isinstance(content, str) and '<appmsg' in content:
            try:
                import xml.etree.ElementTree as ET
                root = ET.fromstring(content)
                title = root.findtext('.//title')
                app_type = root.findtext('.//type') or ''
                label = _APP_TYPE_LABELS.get(app_type, '链接')
                if title:
                    return f"[{label}] {title}"
                if app_type in _APP_TYPE_LABELS:
                    return f"[{_APP_TYPE_LABELS[app_type]}]"
            except ET.ParseError:
                pass
        return "[链接/应用]"
    elif base_type == 50:
        return "[网络电话]"
    elif base_type in (10000, 10002):
        if isinstance(content, str) and '<sysmsg' in content:
            try:
                import xml.etree.ElementTree as ET
                root = ET.fromstring(content)
                inner = root.findtext('.//content')
                if inner:
                    return f"[系统] {inner.strip()}"
            except Exception:
                pass
        return f"[系统消息] {content}" if content else "[系统消息]"
    return str(content) if content else ""


def make_asr_lookup(chat_id, store=None):
    """返回 (create_time, local_id) -> 语音转写文字 的查询函数。"""
    def _lookup(create_time, local_id):
        try:
            key = '%d:%d' % (int(create_time), int(local_id))
            from engine.services import asr_store as _s
            rows = _s.lookup_for_chat(chat_id, [(create_time, local_id)], store=store)
        except Exception:
            return None
        row = rows.get(key)
        return (row or {}).get('text') or None
    return _lookup


def _chat_id_of(chat_info):
    if isinstance(chat_info, dict):
        for k in ('username', 'chat_id', 'user_name', 'id', 'name'):
            v = chat_info.get(k)
            if v:
                return str(v)
        return ''
    for k in ('username', 'chat_id', 'user_name', 'id', 'name'):
        v = getattr(chat_info, k, None)
        if v:
            return str(v)
    return ''
