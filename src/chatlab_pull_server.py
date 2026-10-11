# -*- coding: utf-8 -*-
"""ChatLab Pull 远程数据源协议服务。

实现协议 v1（https://github.com/ChatLab/ChatLab/blob/main/docs/cn/standard/chatlab-pull.md）：
  GET /                    服务信息（便于用户确认在线）
  GET /health              健康检查
  GET /sessions            发现：对话列表（支持 keyword / limit / cursor 分页）
  GET /sessions/<id>/messages
                          拉取：format=chatlab&since=<ts>&limit=<n>
                          全量 since=0/缺省；增量 since>0
                          分页续拉通过 sync.hasMore + sync.nextSince
  GET /push/messages       SSE 实时通知（可选，仅唤醒拉取，不传数据）

认证：可选 Bearer Token（--token），SSE 亦支持 ?access_token=
"""
import json
import os
import threading
import time

DEFAULT_PAGE = 2000          # /sessions/<id>/messages 单批默认上限
SSE_HEARTBEAT = 15           # SSE 心跳间隔（秒）
SSE_POLL = 3                 # SSE 检测新数据的间隔（秒）


def _json_response(payload, status=200):
    from flask import Response
    body = json.dumps(payload, ensure_ascii=False)
    return Response(body, status=status, mimetype="application/json; charset=utf-8")


def _unauthorized():
    return _json_response({"error": "unauthorized",
                           "message": "invalid or missing bearer token"}, 401)


def fast_chat_list(decrypted_dir, own_wxid=None, with_counts=False):
    """轻量会话列表扫描：仅读各分片 Name2Id + contact.db 名称表。

    相比 chat_list.scan_chats()（会遍历全部 Msg_ 表做 COUNT(*) 并构建群成员表，
    在数十万条消息时需数分钟），本函数可在数秒内返回会话清单，
    适合 Pull 服务的 /sessions 发现端点。

    Args:
        with_counts: True 时额外统计每会话消息数（较慢，默认 False）
    Returns: [{username, display_name, msg_count, is_group, tables}]
    """
    import sqlite3
    import chatlab_export as ce

    names = ce._load_contact_names(decrypted_dir)
    msg_dir = os.path.join(decrypted_dir, "message")
    if not os.path.isdir(msg_dir):
        msg_dir = decrypted_dir

    dbs = []
    for f in sorted(os.listdir(msg_dir)):
        if f.startswith("message_") and f.endswith(".db"):
            dbs.append(os.path.join(msg_dir, f))

    import hashlib

    sessions = {}
    for db_idx, db_path in enumerate(dbs):
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
            try:
                rows = conn.execute("SELECT user_name FROM Name2Id").fetchall()
            except sqlite3.Error:
                rows = []
            # Prefetch Msg_ table names in this shard (avoid listing users with no table)
            try:
                msg_tables = {
                    r[0] for r in conn.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' AND name LIKE 'Msg_%'"
                    )
                }
            except sqlite3.Error:
                msg_tables = set()
            for (u,) in rows:
                u = (u or "").strip()
                if not u:
                    continue
                h = hashlib.md5(u.encode()).hexdigest()
                tname = "Msg_%s" % h
                if tname not in msg_tables:
                    continue
                s = sessions.setdefault(u, {"username": u, "tables": [], "msg_count": 0})
                s["tables"].append({
                    "db_idx": db_idx,
                    "db_path": db_path,
                    "table_name": tname,
                })
                if with_counts:
                    try:
                        cnt = conn.execute(
                            "SELECT COUNT(*) FROM [%s]" % tname).fetchone()[0]
                        s["msg_count"] += int(cnt or 0)
                    except sqlite3.Error:
                        pass
            conn.close()
        except sqlite3.Error:
            continue

    out = []
    for u, s in sessions.items():
        is_group = u.endswith("@chatroom")
        display = names.get(u) or u
        out.append({"username": u,
                    "display_name": display,
                    "is_group": is_group,
                    "msg_count": s["msg_count"],
                    "tables": s["tables"]})
    out.sort(key=lambda c: (-(c.get("msg_count") or 0), c["display_name"]))
    return out


def create_pull_app(decrypted_dir, own_wxid=None, token=None, print_fn=None,
                    verbose=False):
    """构造 Pull 协议 Flask 应用。

    Args:
        decrypted_dir: 解密后的数据目录（backup 输出目录）
        own_wxid: 本人 wxid（用于把"我"映射为 platformId）
        token: 可选 Bearer Token；为 None 时不校验
        print_fn: 日志函数
    Returns: (app, helper)
    """
    from flask import Flask, request
    import chatlab_export as ce
    from chat_list import scan_chats

    if print_fn is None:
        print_fn = print

    app = Flask(__name__)
    app.json.ensure_ascii = False
    cache = {"chats": None, "at": 0, "building": False}
    CACHE_TTL = 300
    lock = threading.Lock()

    def _chats():
        """会话列表（轻量扫描 + 内存缓存）。"""
        now = time.time()
        cached = cache.get("chats")
        if cached is not None and now - cache["at"] <= CACHE_TTL:
            return cached
        with lock:
            cached = cache.get("chats")
            if cached is not None and time.time() - cache["at"] <= CACHE_TTL:
                return cached
            cache["building"] = True
            try:
                chats = fast_chat_list(decrypted_dir, own_wxid=own_wxid)
            finally:
                cache["building"] = False
            cache["chats"] = chats
            cache["at"] = time.time()
            return chats

    def _auth_ok():
        if not token:
            return True
        if request.args.get("access_token") == token:
            return True
        hdr = request.headers.get("Authorization", "")
        if hdr.startswith("Bearer "):
            return hdr[7:].strip() == token
        return False

    def _cors(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        return resp

    @app.after_request
    def _after(resp):
        # 记录每个请求：Web 界面日志可据此排查 ChatLab 连接问题
        try:
            if request.method != "OPTIONS":
                print_fn("  %s %s -> %d" % (request.method, request.path, resp.status_code))
        except Exception:
            pass
        return _cors(resp)

    @app.errorhandler(404)
    def _not_found(_e):
        return _json_response({
            "error": "not_found",
            "message": "未知路径: %s" % request.path,
            "endpoints": ["/sessions", "/sessions/<id>/messages",
                          "/push/messages", "/health"],
            "note": "上述端点同时支持根路径与 /api/v1 前缀"
                    "（ChatLab 客户端会请求 /api/v1/...，请确认使用的是本程序）",
        }, 404)

    @app.route("/", methods=["GET", "OPTIONS"])
    def _root():
        return _json_response({
            "name": "WeChat Export Pull Data Source",
            "protocol": "chatlab-pull/v1",
            "platform": ce.CHATLAB_PLATFORM,
            "format": "chatlab/" + ce.CHATLAB_VERSION,
            "endpoints": ["/sessions", "/sessions/<id>/messages", "/push/messages"],
            "authRequired": bool(token),
        })

    @app.route("/health", methods=["GET", "OPTIONS"])
    def _health():
        try:
            n = len(_chats())
        except Exception as e:
            return _json_response({"status": "error", "message": str(e)}, 500)
        return _json_response({"status": "ok", "sessions": n,
                               "dataDir": os.path.basename(os.path.abspath(decrypted_dir))})

    @app.route("/sessions", methods=["GET", "OPTIONS"])
    def _sessions():
        if request.method == "OPTIONS":
            return _json_response({})
        if not _auth_ok():
            return _unauthorized()
        keyword = (request.args.get("keyword") or "").strip().lower()
        # counts=1 时补统计消息数（较慢，默认不统计）
        try:
            limit = int(request.args.get("limit") or 0)
        except ValueError:
            limit = 0
        cursor = request.args.get("cursor") or ""

        items = []
        for c in _chats():
            name = c.get("display_name") or c.get("username") or ""
            sid = c.get("username") or ""
            if keyword and keyword not in name.lower() and keyword not in sid.lower():
                continue
            items.append({
                "id": sid,
                "name": name,
                "platform": ce.CHATLAB_PLATFORM,
                "type": "group" if sid.endswith("@chatroom") else "private",
                # 轻量扫描不统计消息数（可选字段）；为 0 时置 null，避免误导
                "messageCount": (int(c.get("msg_count")) if c.get("msg_count") else None),
                "memberCount": None,
                "lastMessageAt": None,
            })
        # 稳定排序：消息数降序 + id 升序
        items.sort(key=lambda x: (-(x["messageCount"] or 0), x["id"]))
        total = len(items)

        page = None
        if limit and limit > 0 and total > limit:
            start = 0
            if cursor:
                try:
                    start = int(cursor.split(":")[0])
                except ValueError:
                    start = 0
            window = items[start:start + limit]
            has_more = (start + limit) < total
            page = {"hasMore": has_more,
                    "nextCursor": "%d:%s" % (start + limit, window[-1]["id"]) if has_more and window else None}
            items = window

        payload = {"sessions": items}
        if page is not None:
            payload["page"] = page
        if verbose:
            print_fn("  /sessions -> %d 个%s" % (len(items), " (keyword=%s)" % keyword if keyword else ""))
        return _json_response(payload)

    @app.route("/sessions/<path:session_id>/messages", methods=["GET", "OPTIONS"])
    def _messages(session_id):
        if request.method == "OPTIONS":
            return _json_response({})
        if not _auth_ok():
            return _unauthorized()

        fmt = (request.args.get("format") or "chatlab").lower()
        if fmt != "chatlab":
            return _json_response({"error": "unsupported_format",
                                   "message": "only format=chatlab is supported"}, 400)
        try:
            since = int(request.args.get("since") or 0)
        except ValueError:
            since = 0
        try:
            limit = int(request.args.get("limit") or 0)
        except ValueError:
            limit = 0
        if limit <= 0:
            limit = DEFAULT_PAGE

        chat = None
        for c in _chats():
            if c.get("username") == session_id:
                chat = c
                break
        if chat is None:
            return _json_response({"error": "session_not_found",
                                   "message": "unknown session id: " + session_id}, 404)

        try:
            payload = build_session_payload(decrypted_dir, chat, own_wxid=own_wxid,
                                            since=since, limit=limit)
        except Exception as e:
            return _json_response({"error": "build_failed", "message": str(e)}, 500)

        if verbose:
            print_fn("  /sessions/%s/messages since=%d limit=%d -> %d 条 (hasMore=%s)"
                     % (session_id, since, limit, len(payload.get("messages", [])),
                        payload.get("sync", {}).get("hasMore")))
        return _json_response(payload)

    @app.route("/push/messages", methods=["GET"])
    def _sse():
        if not _auth_ok():
            return _unauthorized()
        from flask import Response

        def _stream():
            last_seen = None
            last_hb = time.time()
            while True:
                try:
                    probe = _latest_marks()
                except Exception:
                    probe = None
                if probe and last_seen is not None and probe != last_seen:
                    changed = [k for k in probe if last_seen.get(k) != probe[k]]
                    for sid in changed:
                        ev = {"eventId": "evt-%s-%d" % (abs(hash(sid)) % 100000, int(time.time())),
                              "sessionId": sid,
                              "timestamp": int(probe[sid])}
                        yield "event: message.new\ndata: %s\n\n" % json.dumps(ev, ensure_ascii=False)
                if probe:
                    last_seen = probe
                if time.time() - last_hb >= SSE_HEARTBEAT:
                    last_hb = time.time()
                    yield ": keepalive\n\n"
                time.sleep(SSE_POLL)

        return Response(_stream(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache",
                                 "X-Accel-Buffering": "no"})

    def _latest_marks():
        """各会话最新消息时间（用于 SSE 变化检测），基于 message DB 文件 mtime。"""
        marks = {}
        msg_dir = os.path.join(decrypted_dir, "message")
        if not os.path.isdir(msg_dir):
            return marks
        for root, _dirs, files in os.walk(msg_dir):
            for f in files:
                if not f.endswith(".db"):
                    continue
                try:
                    marks[f] = os.path.getmtime(os.path.join(root, f))
                except OSError:
                    pass
        per_session = {}
        for c in _chats():
            sid = c.get("username") or ""
            best = 0
            for t in (c.get("tables") or []):
                base = os.path.basename(t.get("db_path") or "")
                best = max(best, marks.get(base, 0))
            if sid:
                per_session[sid] = best
        return per_session

    # ChatLab 客户端会把用户输入的地址规范化：不以 /api/v1 结尾时自动补上
    # （ChatLab/ChatLab 的 normalizeBaseUrl）。因此同一套端点必须在
    # 根路径与 /api/v1 前缀下都可用，否则 ChatLab 会报 HTTP 404。
    for _rule, _view, _methods in (
            ("/sessions", _sessions, ["GET", "OPTIONS"]),
            ("/sessions/<path:session_id>/messages", _messages, ["GET", "OPTIONS"]),
            ("/push/messages", _sse, ["GET"]),
            ("/health", _health, ["GET", "OPTIONS"]),
            ("/", _root, ["GET", "OPTIONS"]),
    ):
        _v1_rule = "/api/v1" + _rule if _rule != "/" else "/api/v1"
        app.add_url_rule(_v1_rule, "v1" + _view.__name__, _view, methods=_methods)

    helper = {"chats": _chats, "marks": _latest_marks, "cache": cache}
    return app, helper


def build_session_payload(decrypted_dir, chat, own_wxid=None, since=0, limit=2000):
    """构造 Pull 协议的会话响应（ChatLab 格式 JSON + sync 元信息）。

    全量（since<=0）：携带 chatlab + meta + members + messages；
    增量（since>0）：仅携带 messages（协议要求 meta/members 仅在变更时携带）。
    """
    import chatlab_export as ce

    chat_id = chat.get("username") or ""
    is_group = chat_id.endswith("@chatroom")
    names = ce._load_contact_names(decrypted_dir)
    members = ce.collect_members(decrypted_dir, chat, own_wxid=own_wxid)

    messages = []
    truncated = False
    last_ts = since or 0
    for row, sender_map, shard, _conn in ce._iter_ordered_rows(
            decrypted_dir, chat_id, since_ts=(since if since > 0 else None),
            since_id=None):
        ts = int(row[3] or 0)
        if since > 0 and ts < since:
            continue
        try:
            rec = ce._light_record(row, shard, sender_map, chat_id, own_wxid,
                                   names, is_group)
        except Exception:
            continue
        messages.append(rec)
        last_ts = max(last_ts, ts)
        if len(messages) >= limit:
            truncated = True
            break

    payload = {}
    if since <= 0:
        header = ce._header_block(chat)
        payload["chatlab"] = header["chatlab"]
        payload["meta"] = header["meta"]
        payload["members"] = members
    payload["messages"] = messages
    payload["sync"] = {"hasMore": bool(truncated), "nextSince": int(last_ts)}
    return payload


def _data_summary(decrypted_dir):
    """统计可用于展示的数据概览。"""
    msg_dir = os.path.join(decrypted_dir, "message")
    total = 0
    if os.path.isdir(msg_dir):
        for f in os.listdir(msg_dir):
            if f.endswith(".db"):
                try:
                    total += os.path.getsize(os.path.join(msg_dir, f))
                except OSError:
                    pass
    return total


def run_pull_server(decrypted_dir, own_wxid=None, host="127.0.0.1", port=8765,
                    token=None, print_fn=None):
    """启动 Pull 协议服务（阻塞）。"""
    if print_fn is None:
        print_fn = print
    if not os.path.isdir(decrypted_dir):
        raise FileNotFoundError("解密目录不存在: " + str(decrypted_dir))

    app, helper = create_pull_app(decrypted_dir, own_wxid=own_wxid, token=token,
                                  print_fn=print_fn, verbose=True)

    print_fn("ChatLab Pull 数据源已启动")
    print_fn("  数据目录: " + str(decrypted_dir))
    print_fn("  监听地址: http://%s:%d" % (host, port))
    print_fn("  认证: " + ("Bearer Token 已启用" if token else "无（本地使用）"))
    print_fn("")
    print_fn("在 ChatLab 中添加远程数据源，地址填: http://%s:%d" % (host, port))
    if token:
        print_fn("  Token: " + token)
    print_fn("按 Ctrl+C 停止")

    # 后台预热会话列表（scan_chats 需扫描全部分片，较慢）；
    # 服务先监听，避免 ChatLab 首次请求等待过久。
    def _prewarm():
        try:
            t0 = time.time()
            n = len(helper["chats"]())
            print_fn("  [就绪] 已扫描 %d 个会话（耗时 %.1fs）" % (n, time.time() - t0))
        except Exception as e:
            print_fn("  [警告] 扫描会话失败: %s" % e)
    threading.Thread(target=_prewarm, daemon=True).start()

    app.run(host=host, port=port, threaded=True, debug=False, use_reloader=False)

