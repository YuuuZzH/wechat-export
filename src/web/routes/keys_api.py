"""手动输入密钥 API — 让用户自己粘贴密钥，写进统一配置后供其它功能使用。

  GET  /api/keys/status   查询密钥覆盖情况（按数据库逐条校验 HMAC）
  GET  /api/keys/dirs     列出检测到的微信 db_storage 目录
  POST /api/keys/verify   解析并实测匹配（不保存）
  POST /api/keys/save     匹配并保存（可选强制保存未通过校验的）
  POST /api/keys/remove   删除某个数据库已保存的密钥
  POST /api/keys/export   把解析出的 (数据库, 密钥) 对应关系导出为可回读的清单文件

verify / save / export 三者都接受两种输入：
  · JSON  body 的 `text` 字段（粘贴）
  · multipart 上传的 `file` 字段（日志文件，utf-8 失败时按 gbk 再试）
两种输入都走同一套自动格式识别（见 engine/manual_keys.parse_entries）。
"""
import os
import sys
from datetime import datetime

from flask import Blueprint, current_app, jsonify, make_response, request

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

keys_bp = Blueprint("keys_api", __name__, url_prefix="/api/keys")


def _decode_upload(raw):
    """按候选编码解码上传内容（复用 engine 层实现，避免编码知识两处维护）。"""
    from engine.manual_keys import decode_text_bytes
    return decode_text_bytes(raw)


def _read_pasted_text():
    """从请求中取出用户粘贴/上传的密钥文本。文件优先于 text 字段。"""
    uploaded = request.files.get("file")
    if uploaded is not None and uploaded.filename is not None:
        raw = uploaded.read()
        if raw:
            return _decode_upload(raw), os.path.basename(uploaded.filename)
    data = request.get_json(silent=True) or {}
    return (data.get("text") or ""), ""


def _input_db_dir(data=None):
    if data is None:
        data = request.get_json(silent=True) or {}
    return _resolve_db_dir(data.get("db_dir") or request.form.get("db_dir"))


def _dir_hint():
    try:
        from engine.utils import data_dir_hint
        return data_dir_hint(short=True)
    except Exception:
        return "未找到微信数据目录。请在微信「设置 → 文件管理」查看数据目录，或手动填写。"


def _export_dir():
    """密钥清单的落盘目录：非冻结 = <仓库根>/output，冻结 = <exe 目录>/output。

    单独抽成模块级函数，是为了给「导出目录怎么算」留一条**生产路径本身就在用**的缝：
    测试 monkeypatch 它即可把落盘重定向到临时目录，从而不再污染仓库真实 output/
    （known-issues #41：合成导出与真实导出同名同格式同目录，会让 output/ 无法审计）。

    默认行为与原实现逐字一致，禁止改动这两条分支的语义。
    """
    if getattr(sys, "frozen", False):
        data_root = os.path.dirname(sys.executable)
    else:
        data_root = os.path.normpath(os.path.join(_BASE, ".."))
    return os.path.join(data_root, "output")


def _detect_dirs(mode="auto"):
    """列出微信数据目录。

    mode: fast = 只做快速探测；auto = 找不到时自动做有限深度搜索（15s）；
          deep = 用户主动点「深度搜索」，给更长时间与层数（45s / 7 层）
    """
    try:
        from engine.utils import find_all_wechat_data_dirs
    except Exception:
        return []
    try:
        if mode == "fast":
            return find_all_wechat_data_dirs(deep=False)
        if mode == "deep":
            return find_all_wechat_data_dirs(deep=True, budget_s=45.0, max_depth=7)
        return find_all_wechat_data_dirs(deep=True)
    except Exception:
        return []


def _resolve_db_dir(param=None):
    """解析要操作的 db_storage 目录：入参 → 配置 → 应用配置 → 自动检测。"""
    if param and os.path.isdir(param):
        return param
    try:
        from engine.config_file import get_db_dir
        d = get_db_dir()
        if d and os.path.isdir(d):
            return d
    except Exception:
        pass
    d = current_app.config.get("DB_DIR") or ""
    if d and os.path.isdir(d):
        return d
    dirs = _detect_dirs()
    if dirs:
        return dirs[0]["db_path"]
    return param or ""


def _config_db_dir():
    """配置里记住的 db_storage（用户上次选过的目录）——`tier=config` 的判据。"""
    try:
        from engine.config_file import get_db_dir
        return get_db_dir() or None
    except Exception:
        return None


def _degraded_rank(dirs, message):
    """排序/探针失败时的降级：字段照给（全 idle）+ 原因照记，绝不 500、绝不空列表。"""
    try:
        from engine.services.active_dir import apply_idle_defaults, degraded_probe
        return apply_idle_defaults(dirs), '', degraded_probe(message)
    except Exception:
        return dirs, '', {"t0_ok": False, "t0_elapsed_ms": 0.0, "t0_hits": 0,
                          "t1_probed": 0, "t2_probed": 0, "wechat_running": False,
                          "wechat_pids": [], "errors": [str(message)]}


def _rank_dirs_safe(dirs):
    """给目录加 tier/reason/pids/active/last_write_min 并排序。

    Returns: (ranked_dirs, recommended_path, probe_dict)
    任何失败都降级（原因写进 probe.errors）。
    """
    try:
        from engine.services.active_dir import rank_and_probe
    except Exception as e:
        return _degraded_rank(dirs, "rank: 排序模块不可用: %s" % e)
    try:
        ranked, recommended, probe = rank_and_probe(dirs,
                                                    config_dir=_config_db_dir())
        return ranked, recommended, probe.as_dict()
    except Exception as e:
        return _degraded_rank(dirs, "rank: 排序失败: %s" % e)


@keys_bp.route("/dirs", methods=["GET"])
def keys_dirs():
    mode = (request.args.get("mode") or "auto").lower()
    if mode not in ("fast", "auto", "deep"):
        mode = "auto"
    dirs = _detect_dirs(mode)
    ranked, recommended, probe = _rank_dirs_safe(dirs)
    return jsonify({"dirs": ranked, "current": _resolve_db_dir(), "mode": mode,
                    "recommended_path": recommended,
                    "wechat_running": bool(probe.get("wechat_running", False)),
                    "probe": probe})


def _resolve_db_storage(raw):
    """把用户填的目录尽量解析成真正的 db_storage（兼容 4 种填法）。"""
    import os as _os
    if not raw:
        return None
    p = _os.path.normpath(raw.strip().strip(chr(34)))

    def _first_account(xwf):
        try:
            cands = []
            for name in _os.listdir(xwf):
                full = _os.path.join(xwf, name, "db_storage")
                if _os.path.isdir(full):
                    try:
                        cands.append((_os.path.getmtime(full), full))
                    except OSError:
                        cands.append((0.0, full))
            cands.sort(reverse=True)
            return cands[0][1] if cands else None
        except OSError:
            return None

    base = _os.path.basename(p).lower()
    if base == "db_storage":
        return p if _os.path.isdir(p) else None
    direct = _os.path.join(p, "db_storage")
    if _os.path.isdir(direct):
        return direct
    if base == "xwechat_files":
        return _first_account(p)
    inner = _os.path.join(p, "xwechat_files")
    if _os.path.isdir(inner):
        return _first_account(inner)
    return None

@keys_bp.route("/dbdir", methods=["POST"])
def keys_set_dbdir():
    """保存用户手动指定的 db_storage 目录，供备份/解密/密钥页复用。"""
    import os as _os
    data = request.get_json(silent=True) or {}
    raw = (data.get("path") or "").strip().strip(chr(34))
    if not raw:
        return jsonify({"error": "empty_path", "message": "请填写微信数据目录"}), 400
    path = _resolve_db_storage(raw)
    if path is None:
        exists = _os.path.isdir(_os.path.normpath(raw.strip().strip(chr(34))))
        if not exists:
            return jsonify({"error": "not_found",
                            "message": "目录不存在: " + raw}), 400
        return jsonify({"error": "not_db_storage",
                        "message": "该目录下找不到 db_storage（或 db_storage 下没有 message 子目录）: "
                                   + raw}), 400
    if not _os.path.isdir(_os.path.join(path, "message")):
        return jsonify({"error": "not_db_storage",
                        "message": "该目录下没有 message 子目录，看起来不是微信的 db_storage: " + path}), 400
    try:
        from engine.config_file import set_db_dir
        set_db_dir(path)
    except Exception as e:
        return jsonify({"error": "save_failed", "message": str(e)}), 500
    return jsonify({"ok": True, "dbDir": path, "status": _status_safe(path)})


def _status_safe(db_dir):
    try:
        from engine.manual_keys import status
        return status(db_dir)
    except Exception:
        return None


@keys_bp.route("/status", methods=["GET"])
def keys_status():
    from engine.manual_keys import status

    db_dir = _resolve_db_dir(request.args.get("db_dir"))
    if not db_dir or not os.path.isdir(db_dir):
        return jsonify({"error": "db_dir_missing",
                        "message": _dir_hint(),
                        "dbDir": db_dir or "", "total": 0, "verified": 0,
                        "missing": 0, "invalid": 0, "databases": []}), 200
    try:
        out = status(db_dir)
    except Exception as e:
        return jsonify({"error": "status_failed", "message": str(e)}), 500
    return jsonify(out)


@keys_bp.route("/verify", methods=["POST"])
def keys_verify():
    from engine.manual_keys import match_entries, parse_entries, status, summarize

    text, _fname = _read_pasted_text()
    db_dir = _input_db_dir()
    if not db_dir or not os.path.isdir(db_dir):
        return jsonify({"error": "db_dir_missing",
                        "message": _dir_hint()}), 400
    entries = parse_entries(text)
    if not entries:
        return jsonify({"error": "empty_input", "message": "没有解析到任何密钥行"}), 400
    results = match_entries(db_dir, entries)
    return jsonify({"results": results, "status": status(db_dir), "saved": 0,
                    "summary": summarize(entries)})


@keys_bp.route("/save", methods=["POST"])
def keys_save():
    from engine.manual_keys import apply_entries, parse_entries, summarize

    text, _fname = _read_pasted_text()
    data = request.get_json(silent=True) or {}
    db_dir = _input_db_dir(data)
    if not db_dir or not os.path.isdir(db_dir):
        return jsonify({"error": "db_dir_missing",
                        "message": _dir_hint()}), 400
    entries = parse_entries(text)
    if not entries:
        return jsonify({"error": "empty_input", "message": "没有解析到任何密钥行"}), 400
    try:
        out = apply_entries(db_dir, entries, force=bool(data.get("force")
                                                        or request.form.get("force")))
    except Exception as e:
        return jsonify({"error": "save_failed", "message": str(e)}), 500
    out["dbDir"] = db_dir
    out["summary"] = summarize(entries)
    return jsonify(out)


@keys_bp.route("/export", methods=["POST"])
def keys_export():
    """把解析出的 (数据库, 密钥) 对应关系导出为可直接回读的清单文件。"""
    from engine.manual_keys import export_key_list, parse_entries, summarize

    text, _fname = _read_pasted_text()
    db_dir = _input_db_dir()
    if not db_dir or not os.path.isdir(db_dir):
        return jsonify({"error": "db_dir_missing",
                        "message": _dir_hint()}), 400
    entries = parse_entries(text)
    if not entries:
        return jsonify({"error": "empty_input", "message": "没有解析到任何密钥行"}), 400

    # 默认写到 output/（已被 .gitignore 忽略），避免密钥文件进版本库
    fname = "keys_export_%s.txt" % datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(_export_dir(), fname)
    try:
        res = export_key_list(db_dir, entries, out_path)
    except Exception as e:
        return jsonify({"error": "export_failed", "message": str(e)}), 500

    if not res["count"]:
        s = summarize(entries)
        hint = []
        if s.get("masked"):
            hint.append("有 %d 行是打码/脱敏的（HMAC 校验需要完整密钥）" % s["masked"])
        if not hint:
            hint.append("没有解析到「数据库 + 密钥」成对的条目")
        return jsonify({"error": "nothing_to_export",
                        "message": "没有可导出的条目：" + "；".join(hint),
                        "summary": s}), 400

    with open(out_path, encoding="utf-8") as f:
        content = f.read()
    resp = make_response(content)
    resp.headers["Content-Type"] = "text/plain; charset=utf-8"
    resp.headers["Content-Disposition"] = "attachment; filename=%s" % fname
    return resp


@keys_bp.route("/remove", methods=["POST"])
def keys_remove():
    from engine.manual_keys import remove_key

    data = request.get_json(silent=True) or {}
    rel = data.get("rel") or ""
    if not rel:
        return jsonify({"error": "missing_rel", "message": "缺少数据库路径"}), 400
    db_dir = _resolve_db_dir(data.get("db_dir"))
    try:
        out = remove_key(db_dir, rel)
    except Exception as e:
        return jsonify({"error": "remove_failed", "message": str(e)}), 500
    return jsonify(out)
