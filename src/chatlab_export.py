# -*- coding: utf-8 -*-
"""ChatLab 标准格式导出（JSON / JSONL）— 支持断点续传。

将解密后的微信聊天记录转换为 ChatLab 数据交换格式（v0.0.2）。

断点续传设计（大数据量 / 中途中断 / 系统重启场景）：
* 按会话分文件：每个会话一个 .jsonl，已写完的会话文件始终可独立导入 ChatLab。
* 行级 flush：每 FLUSH_EVERY 行落盘，崩溃最多丢最后几行（而非整个文件）。
* 坏尾截断：续传时扫描文件尾部，丢弃最后一行不完整的 JSON，保证文件始终可解析。
* 进度清单：输出目录下 _chatlab_export_state.json 记录每个会话的游标
  （最后一条消息的 timestamp + platformMessageId），原子写（tmp + os.replace）。
* 复合游标续传：以 (create_time, local_id) 为游标继续，同秒多条消息也不会漏。
* JSON 格式：单文件结构无法续传，改为写 .partial 完成后原子改名，
  避免中断留下无法解析的半截文件；需要续传请使用 JSONL。
"""
import json
import os
import re
import sqlite3
import time

CHATLAB_VERSION = "0.0.2"
CHATLAB_PLATFORM = "wechat"
GENERATOR = "WeChat Export"
STATE_FILENAME = "_chatlab_export_state.json"
FLUSH_EVERY = 50
STATE_EVERY = 500
TAIL_SCAN_BYTES = 256 * 1024

_WECHAT_TO_CHATLAB = {
    1: 0, 3: 1, 34: 2, 43: 3, 6: 4, 47: 5, 48: 8, 42: 27, 50: 23,
    10000: 80, 10002: 80,
}

_APPMSG_TO_CHATLAB = {
    "6": 4, "5": 7, "57": 25, "2000": 21, "2001": 20, "33": 24,
    "36": 24, "3": 24, "17": 3, "19": 26, "51": 99, "87": 99, "115": 99,
}

_TYPE_PLACEHOLDER = {
    1: "[图片]", 2: "[语音]", 3: "[视频]", 4: "[文件]", 5: "[表情]",
    7: "[链接]", 8: "[位置]", 20: "[红包]", 21: "[转账]", 22: "[拍一拍]",
    23: "[通话]", 24: "[分享]", 25: "[引用回复]", 26: "[转发]", 27: "[名片]",
    80: "[系统消息]", 81: "[撤回消息]", 99: "[其他消息]",
}

_RECALL_MARKERS = ("revokemsg", "revoke", "撤回")


def map_message_type(ltype, xml_parsed=None):
    """微信 local_type 映射为 ChatLab 消息类型编号。"""
    try:
        lt = int(ltype) & 0xFFFFFFFF
    except (TypeError, ValueError):
        return 99
    if lt == 49:
        app_type = str((xml_parsed or {}).get("type") or "").strip()
        return _APPMSG_TO_CHATLAB.get(app_type, 7)
    if lt in (10000, 10002):
        blob = json.dumps(xml_parsed or {}, ensure_ascii=False).lower()
        if any(m in blob for m in _RECALL_MARKERS):
            return 81
        return 80
    return _WECHAT_TO_CHATLAB.get(lt, 99)


def message_content(msg, chatlab_type):
    """提取 ChatLab content 字段（文本取原文，富媒体取可读标题或占位符）。"""
    xml_parsed = msg.get("xml_parsed") or {}
    if chatlab_type == 0:
        text = msg.get("content")
        return text if isinstance(text, str) else ("" if text is None else str(text))
    for key in ("title", "text", "label", "des", "poiname", "filename", "name"):
        val = xml_parsed.get(key)
        if isinstance(val, str) and val.strip():
            head = _TYPE_PLACEHOLDER.get(chatlab_type, "[消息]")
            if chatlab_type == 25:
                return val.strip()
            return head + " " + val.strip()
    placeholder = _TYPE_PLACEHOLDER.get(chatlab_type)
    if placeholder:
        return placeholder
    text = msg.get("content")
    if isinstance(text, str) and text.strip() and not text.lstrip().startswith("<"):
        return text.strip()
    return None


def collect_members(decrypted_dir, chat, own_wxid=None):
    """构造 ChatLab members 数组（群聊取真实成员，私聊取双方）。"""
    chat_id = chat.get("username") or ""
    display = chat.get("display_name") or chat_id
    members = []
    seen = set()

    def _push(pid, name, roles=None):
        pid = (pid or "").strip()
        if not pid or pid in seen:
            return
        seen.add(pid)
        item = {"platformId": pid, "accountName": (name or pid)}
        if roles:
            item["roles"] = roles
        members.append(item)

    if own_wxid:
        _push(own_wxid, "我")
    if (chat_id or "").endswith("@chatroom"):
        try:
            from engine.services.chat import get_group_members
            for m in (get_group_members(decrypted_dir, chat_id) or []):
                role = [{"id": "owner"}] if m.get("is_owner") else None
                _push(m.get("wxid"), m.get("display_name"), roles=role)
        except Exception:
            pass
    else:
        _push(chat_id, display)
    return members


def load_state(out_dir):
    """读取导出进度清单（不存在时返回空结构）。"""
    path = os.path.join(out_dir, STATE_FILENAME)
    try:
        with open(path, "r", encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict) and isinstance(st.get("sessions"), dict):
            return st
    except (OSError, ValueError):
        pass
    return {"version": 1, "generator": GENERATOR, "sessions": {}}


def save_state(out_dir, state):
    """原子写进度清单（tmp + os.replace），中断不会留下损坏清单。"""
    path = os.path.join(out_dir, STATE_FILENAME)
    state["updatedAt"] = int(time.time())
    tmp = path + ".tmp"
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def scan_jsonl_tail(path, truncate_bad_tail=True):
    """扫描已有 JSONL，返回续传游标并截断尾部不完整的行。

    Returns: dict(count, lastTimestamp, lastMessageId, bytes, truncated) 或 None
    """
    if not os.path.isfile(path):
        return None
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size == 0:
        return None

    read_size = min(size, TAIL_SCAN_BYTES)
    try:
        with open(path, "rb") as f:
            f.seek(size - read_size)
            blob = f.read(read_size)
    except OSError:
        return None

    parts = blob.split(b"\n")
    base = size - read_size
    if base > 0:
        parts = parts[1:]
        base = base + len(blob.split(b"\n")[0]) + 1

    good_upto = base
    last = None
    offset = base
    for raw in parts:
        line_len = len(raw) + 1
        if raw.strip():
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                break
            if obj.get("_type") == "message":
                try:
                    last = (int(obj.get("timestamp") or 0),
                            str(obj.get("platformMessageId") or ""))
                except (TypeError, ValueError):
                    pass
        offset += line_len
        good_upto = offset

    truncated = False
    if truncate_bad_tail and good_upto < size:
        try:
            with open(path, "r+b") as f:
                f.truncate(good_upto)
            truncated = True
        except OSError:
            pass

    count = 0
    try:
        with open(path, "rb") as f:
            for raw in f:
                if b'"_type": "message"' in raw:
                    count += 1
    except OSError:
        pass

    return {"count": count,
            "lastTimestamp": last[0] if last else 0,
            "lastMessageId": last[1] if last else "",
            "bytes": good_upto,
            "truncated": truncated}

def _iter_ordered_rows(decrypted_dir, chat_id, since_ts=None, since_id=None):
    """跨分片按 create_time 升序产出 (row, sender_map, table_name)（k-way merge）。

    since_ts / since_id: 续传游标，仅产出 (create_time, local_id) 严格大于游标的行。
    """
    from engine.services.message import _find_all_chat_dbs, _build_sender_map

    all_dbs = _find_all_chat_dbs(decrypted_dir, chat_id)
    cols = ("local_id, local_type, origin_source, create_time, status, "
            "message_content, real_sender_id, packed_info_data")
    cursors = []
    for db_path, table_name in all_dbs:
        try:
            conn = sqlite3.connect(db_path)
            sender_map = _build_sender_map(conn, table_name, own_wxid=None,
                                           chat_id=chat_id)
            cur = conn.cursor()
            base_sql = ("SELECT " + cols + " FROM [%s] WHERE create_time > 1000000000 "
                        % table_name)
            if since_ts:
                cur.execute(base_sql + "AND (create_time > ? OR "
                            "(create_time = ? AND local_id > ?)) "
                            "ORDER BY create_time ASC",
                            (since_ts, since_ts, since_id or 0))
            else:
                cur.execute(base_sql + "ORDER BY create_time ASC")
            row = cur.fetchone()
            if row is not None:
                # shard 用分片文件名（如 message_1.db）：微信各分片的 local_id
                # 都从 1 开始，仅靠表名无法区分分片，会导致 platformMessageId 重复。
                shard = os.path.basename(db_path)
                cursors.append([row[3] or 0, row[0] or 0, conn, cur, sender_map,
                                row, shard])
            else:
                conn.close()
        except sqlite3.Error:
            continue

    try:
        while cursors:
            cursors.sort(key=lambda c: (c[0], c[1]))
            head = cursors[0]
            yield head[5], head[4], head[6], head[2]
            nxt = head[3].fetchone()
            if nxt is None:
                head[2].close()
                cursors.pop(0)
            else:
                head[5] = nxt
                head[0] = nxt[3] or 0
                head[1] = nxt[0] or 0
    finally:
        for c in cursors:
            try:
                c[2].close()
            except Exception:
                pass


def _header_block(chat):
    chat_id = chat.get("username") or ""
    is_group = chat_id.endswith("@chatroom")
    meta = {"name": chat.get("display_name") or chat_id,
            "platform": CHATLAB_PLATFORM,
            "type": "group" if is_group else "private"}
    if is_group:
        meta["groupId"] = chat_id
    return {"chatlab": {"version": CHATLAB_VERSION,
                       "exportedAt": int(time.time()),
                       "generator": GENERATOR},
            "meta": meta}


def _safe_filename(name):
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", str(name)).strip()
    return (cleaned or "chat")[:60]


def _build_record(msg, chat_id, own_wxid, members_by_id, shard=None):
    """把项目内部 message dict 转成 ChatLab 消息对象。

    platformMessageId 需在会话内唯一：微信 local_id 仅在单个分片内唯一，
    因此拼接分片标识（Msg 表名）避免跨分片重复。
    """
    ctype = map_message_type(msg.get("msg_type"), msg.get("xml_parsed"))
    if msg.get("is_sender"):
        sender_id = own_wxid or "__self__"
        sender_name = "我"
    else:
        sender_id = (msg.get("sender_wxid") or msg.get("sender_name")
                     or chat_id or "unknown")
        sender_name = msg.get("sender_name") or sender_id
    mid = str(msg.get("id"))
    if shard:
        mid = "%s:%s" % (shard, mid)
    record = {"platformMessageId": mid,
              "sender": str(sender_id),
              "accountName": str(sender_name),
              "timestamp": int(msg.get("create_time") or 0),
              "type": ctype,
              "content": message_content(msg, ctype)}
    member = members_by_id.get(str(sender_id))
    if member and member.get("groupNickname"):
        record["groupNickname"] = member["groupNickname"]
    return record


def _load_contact_names(decrypted_dir):
    """一次性加载 contact.db 的 wxid → 显示名映射（导出场景比逐条查询快得多）。"""
    names = {}
    for rel in ("contact/contact.db", "contact.db"):
        path = os.path.join(decrypted_dir, *rel.split("/"))
        if not os.path.isfile(path):
            continue
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            for r in conn.execute(
                    "SELECT username, remark, nick_name, alias FROM contact"):
                wxid = (r[0] or "").strip()
                if not wxid:
                    continue
                for cand in ((r[1] or "").strip(), (r[2] or "").strip(),
                             (r[3] or "").strip()):
                    if cand and cand != wxid:
                        names[wxid] = cand
                        break
            conn.close()
        except sqlite3.Error:
            pass
        break
    return names


_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_RE_APPMSG_TYPE = None


def _decode_raw(raw):
    """解码 message_content：zstd 压缩的返回解压后文本，其余按 UTF-8 解码。"""
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, (bytes, bytearray)):
        return str(raw)
    data = bytes(raw)
    if len(data) >= 4 and data[:4] == _ZSTD_MAGIC:
        try:
            import zstandard
            dctx = zstandard.ZstdDecompressor()
            out = dctx.decompress(data, max_output_size=32 * 1024 * 1024)
            return out.decode("utf-8", errors="replace")
        except Exception:
            return ""
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return ""
    if text and text[:100].count("\ufffd") > len(text[:100]) * 0.3:
        return ""
    return text


def _extract_appmsg(text):
    """从 appmsg XML 中轻量提取 (appmsg_type, title)（正则，不做完整 XML 解析）。"""
    if not isinstance(text, str) or "<appmsg" not in text:
        return None, None
    m = re.search(r"<type>\s*(\d+)\s*</type>", text)
    app_type = m.group(1) if m else None
    t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", text, re.S)
    title = t.group(1).strip() if t else None
    return app_type, title


def _light_record(row, shard, sender_map, chat_id, own_wxid, names, is_group):
    """把 Msg_ 行直接转换为 ChatLab 消息（不解析媒体、不做完整 XML 解析）。"""
    local_id, ltype_raw, origin, create_time, _status, content, rsid = (
        row[0], row[1], row[2], row[3], row[4], row[5], row[6])
    lt = (ltype_raw & 0xFFFFFFFF) if isinstance(ltype_raw, int) else (ltype_raw or 0)

    text = _decode_raw(content)
    app_type = title = None
    if lt == 49:  # 链接/文件/引用等需要 title 分类
        app_type, title = _extract_appmsg(text)
    xml_parsed = {"type": app_type} if app_type else None
    if title:
        xml_parsed = dict(xml_parsed or {})
        xml_parsed["title"] = title

    ctype = map_message_type(lt, xml_parsed)

    # 发送者判定：origin=1 为自己；群聊文本带 "wxid:\n" 前缀
    is_self = (origin == 1)
    sender_wxid = None
    body = text or ""
    if is_group and isinstance(body, str) and ":\n" in body[:100]:
        head = body.split(":\n", 1)[0]
        cand = _clean_prefix(head)
        body = body.split(":\n", 1)[1] if ":\n" in body else body
        if cand:
            sender_wxid = cand
            if own_wxid and cand == own_wxid:
                is_self = True
    if not is_self and sender_wxid is None and rsid and sender_map:
        sender_wxid = sender_map.get(int(rsid))
        if sender_wxid in ("__self__",) or (own_wxid and sender_wxid == own_wxid):
            is_self = True
    if not is_group and not is_self and not sender_wxid:
        sender_wxid = chat_id

    if is_self:
        sid = own_wxid or "__self__"
        sname = "我"
    else:
        sid = sender_wxid or (names.get(chat_id) if not is_group else None) or chat_id
        sname = names.get(sid) or sid
    if is_group and lt in (10000, 10002):
        sname = "系统消息"

    if ctype == 0:
        out_content = body if isinstance(body, str) else ""
    else:
        out_content = message_content({"content": body, "xml_parsed": xml_parsed}, ctype)

    return {"platformMessageId": "%s:%s" % (shard, local_id),
            "sender": str(sid),
            "accountName": str(sname),
            "timestamp": int(create_time or 0),
            "type": ctype,
            "content": out_content}


def _clean_prefix(head):
    """从群聊消息前缀中提取 wxid/username（容忍二进制垃圾前缀）。"""
    if not head:
        return None
    m = re.findall(r"(?:wxid_[A-Za-z0-9]{10,20}|[A-Za-z][A-Za-z0-9_]{3,30}"
                   r"|[0-9]{5,20}@openim|[0-9]{5,20})", head)
    return m[-1] if m else None

def export_chatlab(decrypted_dir, chat, out_path, fmt="jsonl", own_wxid=None,
                   resume=True, start_ts=None, end_ts=None,
                   keyword=None, print_fn=None, progress_fn=None):
    """导出单个会话为 ChatLab 格式（JSONL 支持断点续传）。

    Args:
        decrypted_dir: 解密数据目录
        chat: scan_chats() 返回的会话 dict
        out_path: 输出文件路径
        fmt: "jsonl"（可续传，推荐）或 "json"（写 .partial 完成后原子改名）
        own_wxid: 本人 wxid
        resume: True 时读取已有文件进度并续传（仅 JSONL）
        keyword: 可选，正文关键词过滤（空格=AND，| =OR）；有关键词时禁用续传
    Returns: dict(count_new, count_total, path, resumed, status)
    """
    if print_fn is None:
        print_fn = print
    if progress_fn is None:
        progress_fn = lambda pct, msg: None

    from engine.services.message import _row_to_message
    from chat_export import _parse_keywords, _match_keywords

    chat_id = chat.get("username") or ""
    is_group = chat_id.endswith("@chatroom")
    is_jsonl = (fmt or "jsonl").lower() == "jsonl"
    header = _header_block(chat)
    members = collect_members(decrypted_dir, chat, own_wxid=own_wxid)
    members_by_id = {m["platformId"]: m for m in members}
    # 轻量路径：一次性加载 wxid → 显示名，避免逐条查库（导出性能关键）
    names = _load_contact_names(decrypted_dir)
    kw_mode, kw_terms = _parse_keywords(keyword)
    # 关键词过滤会改变结果集，续传游标不可靠 → 强制重导
    if kw_terms:
        resume = False

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)

    # --- 续传：探测已有进度 ---
    resumed = False
    since_ts = since_id = None
    count_before = 0
    write_path = out_path
    if is_jsonl and resume:
        prog = scan_jsonl_tail(out_path, truncate_bad_tail=True)
        if prog:
            since_ts = prog["lastTimestamp"] or None
            since_id = prog["lastMessageId"] or None
            count_before = prog["count"]
            resumed = True
    elif not is_jsonl and resume and os.path.exists(out_path):
        # JSON 已完成则跳过；存在 .partial 说明上次中断，重新导出
        try:
            with open(out_path, "r", encoding="utf-8") as f:
                json.load(f)
            return {"count_new": 0, "count_total": 0, "path": out_path,
                    "resumed": True, "status": "done"}
        except (OSError, ValueError):
            pass
    if not is_jsonl:
        write_path = out_path + ".partial"

    if start_ts and (not since_ts or start_ts > since_ts):
        since_ts = start_ts
        since_id = None

    count_new = 0
    wxid_name_cache = {}
    mode = "a" if (is_jsonl and resumed) else "w"

    with open(write_path, mode, encoding="utf-8") as f:
        if not (is_jsonl and resumed):
            if is_jsonl:
                f.write(json.dumps({"_type": "header", **header},
                                   ensure_ascii=False) + "\n")
                for m in members:
                    f.write(json.dumps({"_type": "member", **m},
                                       ensure_ascii=False) + "\n")
            else:
                f.write("{" + json.dumps("chatlab") + ": "
                        + json.dumps(header["chatlab"], ensure_ascii=False) + ", "
                        + json.dumps("meta") + ": "
                        + json.dumps(header["meta"], ensure_ascii=False) + ", "
                        + json.dumps("members") + ": "
                        + json.dumps(members, ensure_ascii=False) + ", "
                        + json.dumps("messages") + ": [")
                f.flush()
        first = not (is_jsonl) and (count_before == 0)

        for row, sender_map, shard, _conn in _iter_ordered_rows(
                decrypted_dir, chat_id, since_ts=since_ts, since_id=since_id):
            ts = row[3] or 0
            if end_ts and ts > end_ts:
                break
            if start_ts and ts < start_ts:
                continue
            try:
                record = _light_record(row, shard, sender_map, chat_id, own_wxid,
                                       names, is_group)
            except Exception:
                continue
            if kw_terms and not _match_keywords(
                    record.get('content') or '', kw_mode, kw_terms):
                continue
            if is_jsonl:
                f.write(json.dumps({"_type": "message", **record},
                                   ensure_ascii=False) + "\n")
            else:
                if not first:
                    f.write(", ")
                f.write(json.dumps(record, ensure_ascii=False))
                first = False
            count_new += 1
            if count_new % FLUSH_EVERY == 0:
                f.flush()
                progress_fn(0, "已导出 %d 条" % (count_before + count_new))
        if not is_jsonl:
            f.write("]}")
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass

    if not is_jsonl:
        os.replace(write_path, out_path)

    return {"count_new": count_new,
            "count_total": count_before + count_new,
            "path": out_path,
            "resumed": resumed,
            "status": "done"}


def export_all_chatlab(decrypted_dir, out_dir, fmt="jsonl", own_wxid=None,
                      resume=True, name_filter=None, start_ts=None, end_ts=None,
                      print_fn=None, progress_fn=None):
    """批量导出所有会话为 ChatLab 格式（JSONL 支持中断后续传）。

    Returns: dict(exported, skipped, failed, results)
    """
    from chat_list import scan_chats

    if print_fn is None:
        print_fn = print
    if progress_fn is None:
        progress_fn = lambda pct, msg: None

    os.makedirs(out_dir, exist_ok=True)
    state = load_state(out_dir)
    state["format"] = "jsonl" if (fmt or "jsonl").lower() == "jsonl" else "json"
    sessions = state.setdefault("sessions", {})
    state.setdefault("startedAt", int(time.time()))

    chats, _, _ = scan_chats(decrypted_dir)
    ext = "jsonl" if (fmt or "jsonl").lower() == "jsonl" else "json"
    total = len(chats) or 1
    exported = skipped = failed = 0
    results = []

    for i, c in enumerate(chats):
        chat_id = c.get("username") or ""
        name = c.get("display_name") or chat_id
        if name_filter:
            kw = name_filter.lower()
            if kw not in name.lower() and kw not in chat_id.lower():
                continue
        safe = _safe_filename(name)
        path = os.path.join(out_dir, "%s.%s" % (safe, ext))
        prev = sessions.get(chat_id) or {}
        progress_fn(int((i + 1) / total * 100), "导出 %s" % safe)

        # 已完成的会话在续传模式下直接跳过
        if resume and prev.get("status") == "done" and os.path.isfile(path):
            skipped += 1
            results.append((name, prev.get("exported", 0), path, "skipped"))
            print_fn("  [跳过] %s（已完成 %s 条）" % (safe, prev.get("exported", 0)))
            continue

        try:
            r = export_chatlab(decrypted_dir, c, path, fmt=fmt, own_wxid=own_wxid,
                               resume=resume, start_ts=start_ts, end_ts=end_ts,
                               print_fn=print_fn, progress_fn=progress_fn)
        except Exception as e:
            failed += 1
            sessions[chat_id] = {"file": os.path.basename(path), "status": "error",
                                 "error": str(e)[:200],
                                 "updatedAt": int(time.time())}
            save_state(out_dir, state)
            print_fn("  [失败] %s: %s" % (safe, e))
            continue

        # 记录进度（原子写，崩溃后可据此跳过或续传）
        prog = scan_jsonl_tail(path, truncate_bad_tail=False) or {}
        sessions[chat_id] = {
            "file": os.path.basename(path),
            "status": "done",
            "exported": r["count_total"],
            "lastTimestamp": prog.get("lastTimestamp", 0),
            "lastMessageId": prog.get("lastMessageId", ""),
            "bytes": prog.get("bytes", 0),
            "resumed": r["resumed"],
            "updatedAt": int(time.time()),
        }
        save_state(out_dir, state)
        exported += 1
        results.append((name, r["count_total"], path,
                        "resumed" if r["resumed"] else "new"))
        tag = "续传完成" if r["resumed"] else "完成"
        print_fn("  %s: 本次 +%d，累计 %d 条 [%s]"
                 % (safe, r["count_new"], r["count_total"], tag))

    save_state(out_dir, state)
    return {"exported": exported, "skipped": skipped, "failed": failed,
            "results": results, "state_file": os.path.join(out_dir, STATE_FILENAME)}
