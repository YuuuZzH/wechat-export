"""手动输入数据库密钥。

使用场景：用户手上已经有密钥（别的工具提取过、旧备份、或从别处获得），
不需要再跑自动提取流程，直接把密钥粘进来，让「解密 / 备份 / 导出 / 查看器」
等功能直接可用。

支持多个数据库、多个密钥。每行一条，# 开头为注释。**自动识别格式**，支持：

    <64位hex>                        # 通用密钥：自动匹配到用了它的数据库
    <96位hex>                        # 微信 x'<64位key><32位salt>' 形式，按 salt 精确匹配
    message_0.db = <64位hex>         # 指定数据库（文件名或相对路径均可）
    message/message_0.db: <64位hex>  # 相对路径，/ 与 \\ 都行
    <32位salt> = <64位hex>           # 指定 salt

    # 直接整段粘贴本项目密钥扫描的日志输出（推荐）：
    [Cipher-FOUND] message/message_0.db salt=<32位salt> -> <64位密钥>

    日志的头尾行（形如 `[Cipher] ...`）会被识别为噪声并忽略，不计为错误。
    粘贴**打码/脱敏**过的日志（含 ``*``、``…``、``...``）会被明确识别并提示，
    因为 HMAC 校验需要完整密钥，打码内容无法使用。

匹配是否成功由 SQLCipher 4 的 page1 HMAC 实测校验，不会把错密钥写进配置。
"""
import os
import re
from datetime import datetime

from engine.config_file import get_db_keys, set_db_keys

HEX64_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{64}(?![0-9a-fA-F])")
HEX96_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{96}(?![0-9a-fA-F])")
HEX32_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")
GROUPED_HEX_RE = re.compile(r"(?:[0-9a-fA-F]{2,8}[\s\-]+){3,}[0-9a-fA-F]{2,8}")

# 本项目密钥扫描（config_cipher_extract）的日志行：
#   [Cipher-FOUND] message\message_0.db salt=<32位hex> -> <64位hex>
_CIPHER_FOUND_RE = re.compile(
    r"^\s*\[Cipher-FOUND\]\s+(?P<rel>\S+)\s+salt=(?P<salt>\S+)\s*->\s*(?P<key>\S+)\s*$")

# 判定「噪声日志行」用：带 [标签] 前缀、且整行不含足够长的 hex 串
_BRACKET_LOG_RE = re.compile(r"^\s*\[[^\]]{1,40}\]")
_HEXLIKE_RE = re.compile(r"[0-9a-fA-F]{16,}")

# 打码/脱敏标记：``*``、Unicode 省略号、ASCII 三点（项目 mask_key() 用后者）
_MASK_MARKERS = ("*", "\u2026", "...")
_UNMASKED_HINT = "用 export -m keys 或密钥扫描命令重新导出，其 stdout 是完整密钥"


def _looks_masked(token):
    """该字段是否被打码/脱敏（含 * / … / ...）。"""
    return bool(token) and any(m in token for m in _MASK_MARKERS)


def _is_full_hex(token, n):
    return bool(re.fullmatch(r"[0-9a-fA-F]{%d}" % n, token or ""))


# 输入解码的候选编码：Windows 控制台重定向出来的日志通常是 GBK
_INPUT_ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "cp936")


def decode_text_bytes(raw):
    """把上传/读到的字节解码成文本。

    先按 UTF-8（含 BOM）严格解码，失败再按 GBK —— Windows 控制台
    `> log.txt` 出来的日志是 GBK，直接当 UTF-8 读会抛 UnicodeDecodeError。
    全部失败时用 UTF-8 errors=replace 兜底，永不抛异常。
    """
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw
    for enc in _INPUT_ENCODINGS:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def read_text_file(path):
    """读取密钥文本文件（自动处理 UTF-8 / GBK / BOM）。失败返回空串。"""
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "rb") as f:
            return decode_text_bytes(f.read())
    except OSError:
        return ""


def mask_key(key_hex):
    """密钥打码显示，避免界面上/日志里出现完整密钥。"""
    if not key_hex or len(key_hex) < 12:
        return "***"
    return key_hex[:6] + "..." + key_hex[-4:]


def _strip_noise(text):
    t = text.strip()
    for ch in [chr(39), chr(34), "\u2018", "\u2019", "\u201c", "\u201d"]:
        t = t.replace(ch, "")
    t = re.sub(r"\bx'", "", t)
    t = re.sub(r"\b0x", "", t, flags=re.IGNORECASE)
    return t.strip()


def parse_entries(text):
    """解析用户粘贴的密钥文本（自动识别格式）。

    Returns: [{"raw", "key", "salt_hint", "db_hint", "error",
               "kind", "format", "note", "masked_fields"}]

    kind ∈ 'key' | 'masked' | 'noise' | 'error'；行号与输入行**一一对应**
    （噪声行也会占位，便于界面按行定位）。
    """
    entries = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue

        # ---- 1) 本项目密钥扫描日志（信息最全，优先识别）----
        mf = _CIPHER_FOUND_RE.match(line)
        if mf:
            rel = mf.group("rel")
            salt_tok = mf.group("salt")
            key_tok = mf.group("key")
            masked_fields = [name for name, tok in (("salt", salt_tok), ("key", key_tok))
                             if _looks_masked(tok)]
            if masked_fields:
                entries.append({
                    "raw": raw_line.strip(), "key": None, "salt_hint": None,
                    "db_hint": None, "kind": "masked", "format": "config_cipher_log",
                    "masked_fields": masked_fields, "error": None,
                    "note": ("检测到密钥被打码/脱敏（%s），无法用于校验："
                             "HMAC 校验需要完整密钥。请粘贴**未打码**的完整原始输出——%s"
                             % ("、".join(masked_fields), _UNMASKED_HINT)),
                })
                continue
            if not (_is_full_hex(salt_tok, 32) and _is_full_hex(key_tok, 64)):
                entries.append({
                    "raw": raw_line.strip(), "key": None, "salt_hint": None,
                    "db_hint": rel, "kind": "error", "format": "config_cipher_log",
                    "masked_fields": [], "note": "",
                    "error": "salt 应为 32 位 hex、密钥应为 64 位 hex",
                })
                continue
            entries.append({
                "raw": raw_line.strip(), "key": key_tok.lower(),
                "salt_hint": salt_tok.lower(), "db_hint": rel,
                "kind": "key", "format": "config_cipher_log",
                "masked_fields": [], "note": "", "error": None,
            })
            continue

        # ---- 2) 通用格式 ----
        cleaned = _strip_noise(line)
        key_hex = None
        salt_hint = None
        hint = cleaned

        m96 = HEX96_RE.search(cleaned)
        m64 = HEX64_RE.search(cleaned)
        grouped = None
        if not m96 and not m64:
            gm = GROUPED_HEX_RE.search(cleaned)
            if gm:
                joined = re.sub(r"[\s\-]+", "", gm.group(0))
                if len(joined) >= 64 and all(c in "0123456789abcdefABCDEF" for c in joined):
                    grouped = (gm.group(0), joined)

        if m96:
            key_hex = m96.group(0)[:64]
            salt_hint = m96.group(0)[64:].lower()
            hint = cleaned.replace(m96.group(0), " ")
        elif m64:
            key_hex = m64.group(0)
            hint = cleaned.replace(m64.group(0), " ")
        elif grouped:
            token, joined = grouped
            # 形如 x'<key><salt>'：优先当作 key+salt（96 位），否则当作纯 key
            key_hex = joined[:64]
            if len(joined) >= 96:
                salt_hint = joined[64:96].lower()
            hint = cleaned.replace(token, " ")

        db_hint = None
        if hint:
            h = hint.strip().strip("=:;,|\t ").strip()
            h = h.strip("=:;,|\t ").strip()
            if h:
                if not salt_hint:
                    m32 = HEX32_RE.fullmatch(h)
                    if m32:
                        salt_hint = h.lower()
                        h = ""
                if h:
                    db_hint = h

        entry = {"raw": raw_line.strip(), "key": key_hex, "salt_hint": salt_hint,
                 "db_hint": db_hint, "error": None, "kind": "key",
                 "format": "generic", "note": "", "masked_fields": []}
        if key_hex is None:
            # 日志噪声行（带 [标签] 前缀且不含 hex 串）不算错误；
            # 其余无密钥行仍是错误，保持既有行为。
            if _BRACKET_LOG_RE.match(line) and not _HEXLIKE_RE.search(line):
                entry["kind"] = "noise"
                entry["format"] = None
                entry["note"] = "日志行，已忽略"
            else:
                entry["kind"] = "error"
                entry["format"] = None
                entry["error"] = "未识别到 64 位十六进制密钥"
        elif len(key_hex) != 64:
            entry["kind"] = "error"
            entry["error"] = "密钥长度不是 64 位十六进制"
        elif db_hint and not db_hint.lower().endswith(".db") and "." not in db_hint:
            # 提示串既不像数据库名也不是 salt：忽略它，按通用密钥处理
            entry["db_hint"] = None
        entries.append(entry)
    return entries


def summarize(entries):
    """按识别结果汇总，供界面/CLI 展示「识别到什么、为什么用不了」。"""
    out = {"total": len(entries), "cipher_log": 0, "generic": 0,
           "noise": 0, "masked": 0, "malformed": 0, "errors": 0}
    for e in entries:
        kind = e.get("kind")
        if kind == "key":
            if e.get("format") == "config_cipher_log":
                out["cipher_log"] += 1
            else:
                out["generic"] += 1
        elif kind == "noise":
            out["noise"] += 1
        elif kind == "masked":
            out["masked"] += 1
        elif e.get("error"):
            out["errors"] += 1
            if kind == "error" and e.get("format") == "config_cipher_log":
                out["malformed"] += 1
    return out


def _load_db_keys():
    try:
        keys = get_db_keys() or {}
    except Exception:
        return {}
    out = {}
    for k, v in keys.items():
        if isinstance(v, str) and len(v) == 64:
            out[str(k)] = v
    return out


def scan_databases(db_dir, with_pages=True):
    """列出 db_dir 下所有数据库及其密钥状态。

    Returns: [{"rel", "rel_norm", "name", "path", "size_mb", "salt", "page1",
               "key", "verified"}]
    """
    from engine.services.wechat_key_extract import collect_db_files, verify_enc_key

    if not db_dir or not os.path.isdir(db_dir):
        return []
    db_files, _salt_to_dbs = collect_db_files(db_dir)
    configured = _load_db_keys()
    norm_configured = {}
    for k, v in configured.items():
        norm_configured[k.replace("\\", "/").lower()] = v
        norm_configured[os.path.basename(k).lower()] = v

    out = []
    for rel, path, size, salt_hex, page1 in db_files:
        rel_norm = rel.replace("\\", "/")
        key = (configured.get(rel) or configured.get(rel_norm)
               or norm_configured.get(rel_norm.lower())
               or norm_configured.get(os.path.basename(rel).lower()))
        # 少数数据库本身就是明文 SQLite（未加密），不需要密钥
        plain = page1[:16] == b"SQLite format 3" + bytes(1)
        verified = plain
        if key and not plain:
            try:
                verified = bool(verify_enc_key(bytes.fromhex(key), page1))
            except ValueError:
                verified = False
        out.append({
            "rel": rel,
            "rel_norm": rel_norm,
            "name": os.path.basename(rel),
            "path": path,
            "size_mb": round(size / 1024 / 1024, 1),
            "salt": salt_hex,
            "page1": page1 if with_pages else None,
            "key": key or "",
            "key_masked": mask_key(key) if key else "",
            "verified": verified,
            "plain": plain,
        })
    out.sort(key=lambda d: d["rel_norm"].lower())
    return out


def status(db_dir):
    """密钥覆盖情况汇总（供界面展示）。"""
    dbs = scan_databases(db_dir, with_pages=False)
    total = len(dbs)
    plain = sum(1 for d in dbs if d.get("plain"))
    verified = sum(1 for d in dbs if d["verified"] and not d.get("plain"))
    wrong = sum(1 for d in dbs if d["key"] and not d["verified"] and not d.get("plain"))
    missing = total - verified - wrong - plain
    return {
        "dbDir": db_dir or "",
        "total": total,
        "verified": verified,
        "missing": missing,
        "invalid": wrong,
        "plain": plain,
        "databases": [{"rel": d["rel"], "name": d["name"], "sizeMb": d["size_mb"],
                       "salt": d["salt"][:16], "hasKey": bool(d["key"]),
                       "verified": d["verified"], "keyMasked": d["key_masked"],
                       "plain": bool(d.get("plain"))}
                      for d in dbs],
    }

def _resolve_rel(dbs, hint):
    """把用户写的数据库提示（文件名或相对路径）解析成真实相对路径。"""
    if not hint:
        return None
    h = hint.replace("\\", "/").strip().strip("./").lower()
    for d in dbs:
        if d["rel_norm"].lower() == h:
            return d["rel"]
    base = os.path.basename(h)
    for d in dbs:
        if d["name"].lower() == base:
            return d["rel"]
    return None


def match_entries(db_dir, entries):
    """用 HMAC 实测把用户输入的密钥与数据库配对（不写配置）。"""
    from engine.services.wechat_key_extract import verify_enc_key

    dbs = scan_databases(db_dir, with_pages=True)
    by_norm = {d["rel_norm"].lower(): d for d in dbs}
    by_base = {}
    by_salt = {}
    for d in dbs:
        by_base.setdefault(d["name"].lower(), []).append(d)
        by_salt.setdefault(d["salt"].lower(), []).append(d)

    results = []
    for lineno, e in enumerate(entries, 1):
        # 不回传原始输入（可能整行就是密钥），只给行号供界面定位
        item = {"line": lineno,
                "keyMasked": mask_key(e["key"]) if e["key"] else "",
                "dbHint": e["db_hint"] or "",
                "saltHint": (e["salt_hint"] or "")[:16],
                "format": e.get("format") or "",
                "kind": e.get("kind") or "",
                "saltMismatch": False,
                "status": "invalid", "message": "", "matched": []}

        # 噪声日志行：不是错误，直接说明已忽略
        if e.get("kind") == "noise":
            item["status"] = "noise"
            item["message"] = e.get("note") or "已忽略（日志行）"
            results.append(item)
            continue
        # 打码/脱敏：明确告知原因与下一步，而不是含糊的「未识别到密钥」
        if e.get("kind") == "masked":
            item["status"] = "masked"
            item["message"] = e.get("note") or "密钥被打码，无法校验"
            results.append(item)
            continue

        if e["error"] or not e["key"]:
            item["message"] = e["error"] or "缺少密钥"
            results.append(item)
            continue
        try:
            key_bytes = bytes.fromhex(e["key"])
        except ValueError:
            item["message"] = "密钥不是合法的十六进制字符串"
            results.append(item)
            continue

        salt_mismatch = False
        if e["db_hint"]:
            rel = _resolve_rel(dbs, e["db_hint"])
            if not rel:
                item["status"] = "db_not_found"
                item["message"] = "没找到数据库: " + e["db_hint"]
                results.append(item)
                continue
            candidates = [d for d in dbs if d["rel"] == rel]
            # 日志给了盐却与本地库不符 → 很可能来自另一台机器/旧版本
            if e["salt_hint"] and candidates and all(
                    d["salt"].lower() != e["salt_hint"].lower() for d in candidates):
                salt_mismatch = True
        elif e["salt_hint"]:
            candidates = by_salt.get(e["salt_hint"].lower()) or []
            if not candidates:
                item["status"] = "db_not_found"
                item["message"] = "没有 salt 以 " + e["salt_hint"][:16] + " 开头的数据库"
                results.append(item)
                continue
        else:
            candidates = dbs

        matched = []
        for d in candidates:
            if not d["page1"]:
                continue
            try:
                ok = verify_enc_key(key_bytes, d["page1"])
            except Exception:
                ok = False
            if ok:
                matched.append({"rel": d["rel"], "name": d["name"],
                                "salt": d["salt"][:16], "salt_full": d["salt"],
                                "sizeMb": d["size_mb"]})
        if matched:
            item["status"] = "matched"
            item["matched"] = matched
            item["message"] = "HMAC 校验通过，匹配 %d 个数据库" % len(matched)
            if salt_mismatch:
                item["saltMismatch"] = True
                item["message"] += "；注意该行的 salt 与本地库不符（疑似旧日志）"
        else:
            item["status"] = "no_match"
            if salt_mismatch:
                item["saltMismatch"] = True
                local = candidates[0]["salt"][:16] if candidates else ""
                item["message"] = (
                    "HMAC 校验未通过，且该行 salt 与本地库不符"
                    "（日志 salt=%s…，本地 salt=%s…）——"
                    "很可能这份日志来自另一台机器/旧版本微信；"
                    "若本机登录过**多个微信账号**，也可能是同名库被另一个账号的密钥覆盖（本版已按 salt 区分）"
                    % (e["salt_hint"][:16], local))
            else:
                scope = "所选数据库" if (e["db_hint"] or e["salt_hint"]) else "本机任何数据库"
                item["message"] = ("HMAC 校验未通过：该密钥与" + scope + "不匹配。"
                                   "若本机登录过**多个微信账号**，常见原因是同名库密钥被"
                                   "另一个账号覆盖——重新登录该账号并提取一次密钥即可恢复")
        results.append(item)
    return results


def apply_entries(db_dir, entries, force=False):
    """匹配 + 保存密钥。

    force=True 时，明确指定了数据库但校验未通过的密钥也会被保存（用户自担）。
    Returns: {"results", "saved", "savedDbs", "status"}
    """
    results = match_entries(db_dir, entries)
    dbs = scan_databases(db_dir, with_pages=False)
    pairs = {}
    salt_pairs = {}
    for e, r in zip(entries, results):
        if r["status"] == "matched":
            for m in r["matched"]:
                pairs[m["rel"]] = e["key"]
                # 同时按 salt 保存完整标识：多账号同名库不会互相覆盖（issue #21）
                if m.get("salt_full"):
                    salt_pairs[m["salt_full"]] = e["key"]
        elif force and e["db_hint"] and r["status"] in ("no_match",):
            # 数据库文件不在本机时也允许按用户写的名字强制保存
            rel = _resolve_rel(dbs, e["db_hint"]) or e["db_hint"].replace("/", "\\")
            if rel:
                pairs[rel] = e["key"]
                for d in dbs:
                    if d.get("rel") == rel and d.get("salt"):
                        salt_pairs[d["salt"]] = e["key"]
                r["status"] = "forced"
                r["message"] = "未通过校验，已按指定数据库强制保存：" + rel
    saved = 0
    if pairs or salt_pairs:
        set_db_keys(pairs, db_dir=db_dir, salt_keys=salt_pairs)
        saved = len(pairs)
    return {"results": results, "saved": saved,
            "savedDbs": sorted(pairs.keys()), "status": status(db_dir)}


def remove_key(db_dir, rel):
    """删除某个数据库已保存的密钥。"""
    from engine.config_file import remove_db_keys
    n = remove_db_keys([rel])
    return {"removed": n, "status": status(db_dir)}


def export_key_list(db_dir, entries, out_path):
    """把 (数据库路径, 密钥) 对应关系导出成**可回读**的清单文件。

    格式为 `<相对路径> = <64位hex>` —— 正是 parse_entries 支持的写法，
    因此导出文件**可以直接粘回**「手动输入密钥」页面 / import-keys 使用。

    未通过本机校验的条目也会导出（用户可能要在另一台机器上使用），
    但会在其上一行加注释标注；打码/噪声/无路径的条目不会导出。
    """
    results = match_entries(db_dir, entries) if db_dir else []
    status_by_line = {r["line"]: r for r in results}

    pairs = []
    unverified = 0
    for i, e in enumerate(entries, 1):
        key = e.get("key")
        rel = e.get("db_hint")
        if not key or not rel:
            continue
        r = status_by_line.get(i) or {}
        ok = r.get("status") == "matched"
        if not ok:
            unverified += 1
        pairs.append((rel.replace("/", "\\"), key, ok))

    lines = [
        "# WeChat Export 密钥清单（**含完整密钥**，请勿外传、勿提交到版本库）",
        "# 生成时间: %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "# 共 %d 条；其中未通过本机校验 %d 条（可能是另一台机器/旧版本的库）"
        % (len(pairs), unverified),
        "# 格式 `<相对路径> = <64位hex>`，可直接粘回「手动输入密钥」页面导入",
        "",
    ]
    for rel, key, ok in pairs:
        if not ok:
            lines.append("# ↓ 未通过本机校验，导入时会再次校验")
        lines.append("%s = %s" % (rel, key))

    out_dir = os.path.dirname(os.path.abspath(out_path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return {"path": out_path, "count": len(pairs), "unverified": unverified}


def format_results(results):
    """把匹配结果格式化成终端文本。"""
    lines = []
    for i, r in enumerate(results, 1):
        if r["status"] == "matched":
            names = ", ".join(m["rel"] for m in r["matched"])
            lines.append("  [%d] %s  ->  %s" % (i, r["keyMasked"], names))
        elif r["status"] == "noise":
            lines.append("  [%d] （忽略日志行）" % i)
        else:
            lines.append("  [%d] %s  ->  %s (%s)" % (i, r["keyMasked"],
                                                     r["status"], r["message"]))
    return "\n".join(lines)
