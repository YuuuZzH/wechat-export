"""Pull API — 管理 ChatLab Pull 数据源服务（启动 / 停止 / 状态）。

ChatLab 的「远程数据源」协议（chatlab-pull/v1）允许 ChatLab 直接拉取本程序的
聊天记录，无需先导出文件。本模块把该服务集成进 Web 界面：
  GET  /api/pull/status  — 查询运行状态
  POST /api/pull/start   — 启动服务（可自动避让占用端口）
  POST /api/pull/stop    — 停止服务
"""
import os
import socket
import sys
import threading
import time
from collections import deque

from flask import Blueprint, current_app, jsonify, request

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

pull_bp = Blueprint("pull_api", __name__, url_prefix="/api/pull")

_LOCK = threading.Lock()
_STATE = {
    "running": False,
    "host": "127.0.0.1",
    "port": 8765,
    "token": "",
    "started_at": None,
    "sessions": None,
    "server": None,
    "thread": None,
    "log": deque(maxlen=200),
}


def _log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    with _LOCK:
        _STATE["log"].append(line)
    print("[pull] " + str(msg))


def _lan_ips():
    """本机局域网 IPv4（供手机版 ChatLab 连接）。"""
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips


def _port_free(host, port):
    """端口是否可用。

    Windows 下 SO_REUSEADDR 允许绑定"已被占用"的端口（与 Linux 语义不同），
    会导致误判 → 两个服务抢同一端口。因此先探测是否已有人在监听，再用
    SO_EXCLUSIVEADDRUSE 做独占绑定测试。
    """
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        if s.connect_ex((probe_host, port)) == 0:
            return False
    except OSError:
        pass
    finally:
        s.close()

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _payload():
    with _LOCK:
        host = _STATE["host"]
        port = _STATE["port"]
        running = _STATE["running"]
        token = _STATE["token"]
        sessions = _STATE["sessions"]
        started = _STATE["started_at"]
        logs = list(_STATE["log"])[-60:]
    local = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    primary = "http://%s:%d" % (local, port)
    urls = [primary]
    if host in ("0.0.0.0", "::"):
        for ip in _lan_ips():
            urls.append("http://%s:%d" % (ip, port))
    return {
        "running": running,
        "host": host,
        "port": port,
        "token": token,
        "url": primary,
        "urls": urls,
        "sessions": sessions,
        "startedAt": started,
        "startedAtText": (time.strftime("%H:%M:%S", time.localtime(started)) if started else ""),
        "endpoints": [
            {"method": "GET", "path": "/sessions", "desc": "会话列表（keyword / limit / cursor）"},
            {"method": "GET", "path": "/sessions/<id>/messages", "desc": "消息（format=chatlab, since, limit）"},
            {"method": "GET", "path": "/push/messages", "desc": "SSE 增量推送（可选）"},
            {"method": "GET", "path": "/health", "desc": "健康检查"},
        ],
        "log": logs,
    }


@pull_bp.route("/status", methods=["GET"])
def pull_status():
    return jsonify(_payload())


@pull_bp.route("/start", methods=["POST"])
def pull_start():
    data = request.get_json(silent=True) or {}
    decrypted_dir = current_app.config.get("DECRYPTED_DIR", "")
    if not decrypted_dir or not os.path.isdir(decrypted_dir):
        return jsonify({"error": "decrypted_dir_missing",
                        "message": "数据目录不存在，请先完成一键备份"}), 400

    with _LOCK:
        if _STATE["running"]:
            out = _payload()
            out["alreadyRunning"] = True
            return jsonify(out)

    host = (data.get("host") or "127.0.0.1").strip() or "127.0.0.1"
    token = (data.get("token") or "").strip()
    auto = bool(data.get("auto_port", True))
    try:
        base_port = int(data.get("port") or 8765)
    except (TypeError, ValueError):
        base_port = 8765
    if not (1 <= base_port <= 65535):
        return jsonify({"error": "bad_port", "message": "端口需在 1-65535 之间"}), 400

    chosen = None
    for i in range(0, 10 if auto else 1):
        candidate = base_port + i
        if candidate > 65535:
            break
        if _port_free(host, candidate):
            chosen = candidate
            break
    if chosen is None:
        return jsonify({"error": "port_in_use",
                        "message": "端口 %d 已被占用，请换一个端口" % base_port}), 409

    try:
        from chatlab_pull_server import create_pull_app
        from werkzeug.serving import make_server

        app, helper = create_pull_app(
            decrypted_dir,
            own_wxid=current_app.config.get("WXID"),
            token=token or None,
            print_fn=_log, verbose=False)
        server = make_server(host, chosen, app, threaded=True)
    except Exception as e:
        _log("启动失败: %s" % e)
        return jsonify({"error": "start_failed", "message": str(e)}), 500

    thread = threading.Thread(target=server.serve_forever,
                              name="chatlab-pull-serve", daemon=True)
    thread.start()

    with _LOCK:
        _STATE.update({
            "running": True,
            "host": host,
            "port": chosen,
            "token": token,
            "sessions": None,
            "started_at": time.time(),
            "server": server,
            "thread": thread,
        })
    _log("服务已启动: http://%s:%d（%s）" % (host, chosen,
                                              "已启用 Token" if token else "未启用 Token"))

    def _prewarm():
        try:
            t0 = time.time()
            n = len(helper["chats"]())
            with _LOCK:
                _STATE["sessions"] = n
            _log("已扫描 %d 个会话（%.1fs），可以连接了" % (n, time.time() - t0))
        except Exception as e:
            _log("扫描会话失败: %s" % e)
    threading.Thread(target=_prewarm, daemon=True).start()

    return jsonify(_payload())


@pull_bp.route("/stop", methods=["POST"])
def pull_stop():
    with _LOCK:
        server = _STATE["server"]
        thread = _STATE["thread"]
        running = _STATE["running"]
    if not running or server is None:
        out = _payload()
        out["alreadyStopped"] = True
        return jsonify(out)
    try:
        server.shutdown()
    except Exception as e:
        _log("停止时出错: %s" % e)
    try:
        server.server_close()
    except Exception:
        pass
    if thread is not None:
        thread.join(timeout=5)
    with _LOCK:
        _STATE.update({"running": False, "server": None, "thread": None,
                       "sessions": None, "started_at": None})
    _log("服务已停止")
    return jsonify(_payload())
