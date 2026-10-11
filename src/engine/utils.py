"""
公共工具: 微信路径自动检测、联系人加载、群聊名称解析、消息DB迭代。
"""
import glob as _glob
import os
import re
import shutil
import sqlite3
import hashlib
from collections import defaultdict


# --- 目录名黑名单：深度搜索时跳过，避免浪费时间/越界 ---
_JUNK_DIRS = {
    "windows", "$recycle.bin", "system volume information", "recovery",
    "programdata", "appdata", "application data", "node_modules", ".git",
    "temp", "tmp", "$windows.~bt", "$windows.~ws", "perflogs", "msocache",
    "intel", "amd", "nvidia", "drivers", "python27", "python3", "anaconda3",
    "__pycache__", ".cache", ".vscode", ".gradle", ".nuget", ".m2",
}


def _decode_text(raw):
    """尽力解码配置文件内容（UTF-16 仅在有 BOM 时尝试，避免把 GBK 误判成 UTF-16）。"""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except (UnicodeDecodeError, UnicodeError):
            pass
    for enc in ("utf-8-sig", "gbk"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return ""


def _iter_path_like_values(text):
    """从配置文本里提取所有"看起来像路径"的值（支持纯路径与 key=value）。"""
    for line in (text or "").splitlines():
        line = line.strip().strip("\ufeff")
        if not line or line.startswith(("#", ";", "[")):
            continue
        value = line.split("=", 1)[1] if "=" in line else line
        value = value.strip().strip(chr(34)).strip(chr(39)).strip()
        if not value:
            continue
        yield value


def _normalize_save_path(value):
    """把微信的特殊保存位置记号转成真实路径。"""
    v = (value or "").strip().strip(chr(34))
    low = v.lower().rstrip(":")
    home = os.environ.get("USERPROFILE", "")
    if low in ("mydocument", "mydocuments", "documents", "document"):
        return os.path.join(home, "Documents")
    if low in ("mydesktop", "desktop"):
        return os.path.join(home, "Desktop")
    return v


def _wechat_config_paths():
    """读取微信自身配置（%APPDATA%\\Tencent\\xwechat\\config\\*.ini 等）。

    不同版本写法不一：可能是纯路径、key=value、带引号、MyDocument: 记号，
    也可能直接指向 xwechat_files 或 db_storage。这里全部兜住。
    """
    appdata = os.environ.get("APPDATA", "")
    local = os.environ.get("LOCALAPPDATA", "")
    search_dirs = [
        os.path.join(appdata, "Tencent", "xwechat", "config"),
        os.path.join(appdata, "Tencent", "xwechat", "All Users", "config"),
        os.path.join(appdata, "Tencent", "WeChat", "config"),
        os.path.join(local, "Tencent", "xwechat", "config"),
    ]
    found = []
    for cfg_dir in search_dirs:
        if not os.path.isdir(cfg_dir):
            continue
        try:
            names = os.listdir(cfg_dir)
        except OSError:
            continue
        for name in names:
            if not name.lower().endswith((".ini", ".txt")):
                continue
            fpath = os.path.join(cfg_dir, name)
            try:
                with open(fpath, "rb") as f:
                    raw = f.read(4096)
            except OSError:
                continue
            if b"\x00" in raw[:4]:
                pass  # UTF-16 也允许，交给 _decode_text
            text = _decode_text(raw)
            for value in _iter_path_like_values(text):
                path = _normalize_save_path(value)
                if len(path) < 3 or len(path) > 260:
                    continue
                # 只要形如盘符/UNC 的路径就收下（哪怕暂时不存在）
                if re.match(r"^[A-Za-z]:[\\/]", path) or path.startswith("\\\\"):
                    found.append(os.path.normpath(path))
    return found


def _registry_paths():
    """从注册表读取微信保存位置（Tencent\\Weixin / xwechat）。"""
    try:
        import winreg
    except ImportError:
        return []
    keys = [
        (winreg.HKEY_CURRENT_USER, r"Software\Tencent\Weixin"),
        (winreg.HKEY_CURRENT_USER, r"Software\Tencent\xwechat"),
        (winreg.HKEY_CURRENT_USER, r"Software\Tencent\WeChat"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Tencent\Weixin"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Tencent\Weixin"),
    ]
    out = []
    for root, sub in keys:
        try:
            key = winreg.OpenKey(root, sub)
        except OSError:
            continue
        try:
            idx = 0
            while True:
                try:
                    _name, value, _type = winreg.EnumValue(key, idx)
                except OSError:
                    break
                idx += 1
                if not isinstance(value, str):
                    continue
                path = _normalize_save_path(value)
                if re.match(r"^[A-Za-z]:[\\/]", path) or path.startswith("\\\\"):
                    out.append(os.path.normpath(path))
        finally:
            key.Close()
    return out


def _logical_drives():
    """固定盘 + 可移动盘（跳过网络盘/光驱）。"""
    drives = []
    try:
        import ctypes
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        for i in range(26):
            if not (bitmask & (1 << i)):
                continue
            root = chr(ord("A") + i) + ":\\"
            try:
                dtype = ctypes.windll.kernel32.GetDriveTypeW(root)
            except Exception:
                dtype = 1
            if dtype in (2, 3) and os.path.isdir(root):
                drives.append(root)
    except Exception:
        for root in ("C:\\", "D:\\", "E:\\", "F:\\"):
            if root not in drives and os.path.isdir(root):
                drives.append(root)
    return drives


def _glob_db_storage(xwechat_dir):
    """在 xwechat_files 目录下找出所有 */db_storage。"""
    if not os.path.isdir(xwechat_dir):
        return []
    out = []
    try:
        pattern = os.path.join(xwechat_dir, "*", "db_storage")
        for match in _glob.glob(pattern):
            if os.path.isdir(match):
                out.append(match)
    except OSError:
        pass
    return out


def _candidates_under(root):
    r"""给一个候选根目录，返回其中可能的 db_storage（兼容多种层级）。

    支持：盘符/自定义目录（<root>\xwechat_files\<账号>\db_storage）、
    xwechat_files 目录本身、账号目录（<root>\db_storage）、以及 db_storage 本身。
    """
    base = os.path.basename(os.path.normpath(root)).lower()
    if base == "db_storage":
        return [root] if os.path.isdir(root) else []
    out = []
    direct = os.path.join(root, "db_storage")
    if os.path.isdir(direct):
        out.append(direct)
    out.extend(_glob_db_storage(root if base == "xwechat_files"
                                else os.path.join(root, "xwechat_files")))
    return out


def _deep_find_db_storage(budget_s=15.0, max_depth=5):
    """有限深度 BFS 查找 xwechat_files/*/db_storage（自动兜底）。

    仅在快速探测一无所获时调用：优先钻取名字像微信的目录，
    跳过系统/缓存目录，并有时间与访问量上限，避免卡住。
    """
    import time
    started = time.time()
    found = []
    seen = set()
    visited = 0

    queue = [(d, 0) for d in _logical_drives()]
    home = os.environ.get("USERPROFILE", "")
    if home and os.path.isdir(home):
        queue.append((home, 0))

    interesting = ("wx", "wechat", "weixin", "tencent", "chat",
                   "\u5fae\u4fe1", "\u817e\u8baf", "files", "data")

    while queue:
        if time.time() - started > budget_s or visited > 30000:
            break
        path, depth = queue.pop(0)
        norm = os.path.normcase(os.path.normpath(path))
        if norm in seen:
            continue
        seen.add(norm)
        visited += 1
        try:
            entries = list(os.scandir(path))
        except OSError:
            continue

        pending = []
        for entry in entries:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            name = entry.name
            low = name.lower()
            if low == "xwechat_files":
                found.extend(_glob_db_storage(entry.path))
                continue
            if low == "db_storage":
                found.append(entry.path)
                continue
            if low in _JUNK_DIRS or low.startswith("$"):
                continue
            if depth + 1 <= max_depth:
                pending.append((entry.path, depth + 1, low))

        # 名字像微信的目录"深度优先"先挖（Tencent\\WeChat\\微信 这类很常见），
        # 其余目录保持广度优先，保证浅层的 xwechat_files 也能尽快被发现
        priority = [item for item in pending if any(k in item[2] for k in interesting)]
        rest = [item for item in pending if not any(k in item[2] for k in interesting)]
        queue = ([(p, d) for p, d, _low in priority]
                 + queue
                 + [(p, d) for p, d, _low in rest])

    return found

def _get_fast_data_roots():
    """Collect candidate data-root directories, skipping slow/network drives.

    Only scans DRIVE_FIXED and DRIVE_REMOVABLE drives; skips DRIVE_REMOTE (network),
    DRIVE_CDROM, DRIVE_RAMDISK, and DRIVE_NO_ROOT_DIR.
    """
    data_roots = []

    def _add(p):
        if not p:
            return
        try:
            norm = os.path.normpath(p)
        except (TypeError, ValueError):
            return
        if os.path.isdir(norm) and norm not in data_roots:
            data_roots.append(norm)

    # Strategy 1: 微信自身配置（%APPDATA%\Tencent\xwechat\config\*.ini）
    for p in _wechat_config_paths():
        _add(p)
        # 配置里的目录可能已被移动，父目录仍值得一试
        _add(os.path.dirname(os.path.normpath(p.rstrip("\\/"))))

    # Strategy 2: 注册表记录的保存位置（Tencent\Weixin / xwechat）
    for p in _registry_paths():
        _add(p)
        _add(os.path.dirname(os.path.normpath(p.rstrip("\\/"))))

    # Strategy 3: 常见位置
    userprofile = os.environ.get("USERPROFILE", "")
    homedrive = os.environ.get("HOMEDRIVE", "C:")
    localappdata = os.environ.get("LOCALAPPDATA", "")
    _add(os.path.join(userprofile, "Documents"))
    _add(os.path.join(userprofile, "Documents", "xwechat_files"))
    _add(userprofile)
    _add(os.path.join(localappdata, "Tencent"))
    _add(homedrive + os.sep)

    # Strategy 4: 所有固定盘 / 可移动盘
    for root in _logical_drives():
        _add(root)

    return data_roots


def _scan_roots_for_wechat(data_roots):
    """Scan data_roots for xwechat_files/*/db_storage.

    Checks if xwechat_files exists before globbing, and returns
    deduplicated list of db_storage paths.
    """
    candidates = []
    seen = set()

    for root in data_roots:
        for match in _candidates_under(root):
            try:
                norm = os.path.normcase(os.path.normpath(match))
            except (TypeError, ValueError):
                continue
            if norm in seen or not os.path.isdir(match):
                continue
            seen.add(norm)
            candidates.append(match)

    return candidates


_DETECT_CACHE = {"key": None, "value": None, "at": 0.0}


def data_dir_hint(short=False):
    r"""找不到微信数据目录时给用户的排查步骤（CLI 与 Web 共用）。"""
    if short:
        return ("未找到微信数据目录。请在微信「设置 → 文件管理 → 打开文件夹」确认 "
                "xwechat_files\\<账号>\\db_storage 存在；然后在页面点「深度搜索」，"
                "或把该目录填进输入框后点「使用该目录」。")
    return (
        "未找到微信数据目录（db_storage）。请按顺序排查：\n"
        "  1) 微信 → 设置 → 文件管理 → 打开文件夹，确认里面有 xwechat_files\\<账号>\\db_storage\n"
        "  2) 重新运行本程序（会自动做一次深度目录搜索，约十几秒）\n"
        "  3) 仍然找不到时手动指定：--db-dir \"X:\\...\\db_storage\""
    )

def _detect_db_storage(deep=True, use_cache=True, budget_s=15.0, max_depth=5):
    """返回所有可用的 db_storage 目录（先快后深，带缓存）。

    fast 阶段只做几十次 isdir 判断；只有一无所获时才进入有限深度 BFS，
    因此对正常用户几乎没有额外开销，却能救回"数据放在自定义深层目录"的情况。
    """
    import time
    key = (tuple(_get_fast_data_roots()), bool(deep), float(budget_s), int(max_depth))
    now = time.time()
    cache = _DETECT_CACHE
    if (use_cache and cache["value"] is not None and cache["key"] == key
            and now - cache["at"] < 30):
        return cache["value"]

    candidates = _scan_roots_for_wechat(_get_fast_data_roots())
    if not candidates and deep:
        candidates = _deep_find_db_storage(budget_s=budget_s, max_depth=max_depth)

    # 去重 + 过滤掉没有 message/contact 子目录的伪目录
    out = []
    seen = set()
    for path in candidates:
        norm = os.path.normcase(os.path.normpath(path))
        if norm in seen:
            continue
        seen.add(norm)
        out.append(path)

    if use_cache:
        cache["key"] = key
        cache["value"] = out
        cache["at"] = now
    return out

def find_wechat_data_dir():
    r"""自动检测微信 db_storage 数据目录。

    检测策略 (按优先级):
      1. 解析 %APPDATA%\Tencent\xwechat\config\*.ini (微信自写配置，最可靠)
      2. 常见数据根目录 + xwechat_files\*\db_storage 定深 glob
      3. 遍历固定/可移动驱动器根目录查找 xwechat_files

    多个账号时优先选 message 目录最近修改过的 (当前活跃账号)。
    Returns: db_storage 目录路径, 或 None
    """
    candidates = _detect_db_storage(deep=True)

    if not candidates:
        return None

    # ---- 多个账号时选最近活跃的 (message 目录 mtime 最大) ----
    def _activity_score(db_storage_path):
        msg_dir = os.path.join(db_storage_path, "message")
        try:
            return os.path.getmtime(msg_dir) if os.path.isdir(msg_dir) else 0
        except OSError:
            return 0

    candidates.sort(key=_activity_score, reverse=True)
    return candidates[0]


def _get_dir_size_mb(dir_path):
    """Quickly estimate directory size in MB using scandir (breadth-first, one level).
    For db_storage we only need a rough estimate; walking one level is fast and enough.
    """
    try:
        total = 0
        for entry in os.scandir(dir_path):
            try:
                if entry.is_file(follow_symlinks=False):
                    total += entry.stat().st_size
                elif entry.is_dir(follow_symlinks=False):
                    for sub in os.scandir(entry.path):
                        try:
                            if sub.is_file(follow_symlinks=False):
                                total += sub.stat().st_size
                        except OSError:
                            pass
            except OSError:
                pass
        return round(total / (1024 * 1024), 1)
    except OSError:
        return 0


def find_all_wechat_data_dirs(deep=True, budget_s=15.0, max_depth=5):
    """检测所有微信 db_storage 目录，返回列表供用户选择。

    Args:
        deep: 快速探测一无所获时，是否再做有限深度的目录搜索兜底
        budget_s / max_depth: 深度搜索的时间与层数上限
            （界面上的「深度搜索」按钮会用 45s / 7 层）
    Returns: [{'db_path': str, 'wxid': str, 'mtime': float,
               'db_count': int, 'size_mb': float}, ...] 按活跃度降序
    """
    candidates = _detect_db_storage(deep=deep, budget_s=budget_s,
                                    max_depth=max_depth)

    if not candidates:
        return []

    result = []
    for db_path in candidates:
        parent = os.path.dirname(db_path)
        wxid = os.path.basename(parent)
        msg_dir = os.path.join(db_path, "message")
        try:
            mtime = os.path.getmtime(msg_dir) if os.path.isdir(msg_dir) else 0
        except OSError:
            mtime = 0
        # Count message_N.db files
        try:
            db_count = 0
            if os.path.isdir(msg_dir):
                db_count = sum(1 for f in os.listdir(msg_dir)
                              if f.startswith('message_') and f.endswith('.db'))
        except OSError:
            db_count = 0
        size_mb = _get_dir_size_mb(db_path)
        result.append({
            'db_path': db_path,
            'wxid': wxid,
            'mtime': mtime,
            'db_count': db_count,
            'size_mb': size_mb,
        })

    result.sort(key=lambda x: x['mtime'], reverse=True)
    return result


def is_wechat_running():
    """检查 Weixin.exe 是否在运行。"""
    import subprocess
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10
        )
        return "Weixin.exe" in r.stdout
    except (subprocess.SubprocessError, OSError):
        return False


def load_contacts(decrypted_dir):
    """加载联系人映射。
    Returns:
        id_to_name: {contact_id: display_name}
        name_to_id: {username: contact_id}
        usernames: set of all usernames
    """
    id_to_name = {}
    name_to_id = {}
    usernames = set()

    db_path = os.path.join(decrypted_dir, "contact", "contact.db")
    if not os.path.exists(db_path):
        return id_to_name, name_to_id, usernames

    conn = sqlite3.connect(db_path)
    try:
        for r in conn.execute(
            "SELECT id, username, remark, nick_name, alias FROM contact"
        ):
            cid, uname, remark, nick, alias = r
            uname = (uname or "").strip()
            remark_v = (remark or '').strip()
            nick_v = (nick or '').strip()
            alias_v = (alias or '').strip()
            display = remark_v if (remark_v and remark_v != uname) else (nick_v if (nick_v and nick_v != uname) else (alias_v if (alias_v and alias_v != uname) else uname))
            if cid and display:
                id_to_name[cid] = display
            if uname:
                name_to_id[uname] = cid
                usernames.add(uname)
    finally:
        conn.close()
    return id_to_name, name_to_id, usernames


def iter_message_dbs(decrypted_dir):
    """迭代解密后的 message_N.db 文件。"""
    msg_dir = os.path.join(decrypted_dir, "message")
    dbs = []
    if not os.path.isdir(msg_dir):
        return dbs
    for f in os.listdir(msg_dir):
        m = re.match(r'message_(\d+)\.db', f, re.IGNORECASE)
        if m:
            dbs.append((int(m.group(1)), os.path.join(msg_dir, f)))
    return sorted(dbs, key=lambda x: x[0])


def get_msg_table(conn, username):
    """获取指定 username 对应的 Msg_ 表是否存在于此 DB 中。"""
    h = hashlib.md5(username.encode()).hexdigest()
    tname = f"Msg_{h}"
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (tname,)
    ).fetchone()
    return tname if row else None


# ---------------------------------------------------------------------------
# wxid 值形式：账号**目录名** ↔ **裸 wxid**（Critical #17 / known-issues #20）
# ---------------------------------------------------------------------------

_WXID_DIR_SUFFIX_LEN = 4
_HEX_DIGITS = "0123456789abcdefABCDEF"


def bare_wxid(value):
    """账号**目录名** → 消息侧使用的**裸 wxid**（Critical #17 / known-issues #20）。

    同一个账号在不同的地方存成两种形式：

    * **账号目录名**（`config_file.get_backup_wxid()` / `app.config['WXID']` /
      `_detect_wxid()`）形如 `<裸 wxid>_<4 位十六进制>`，真实值 24 字符；
    * **裸 wxid**（消息侧：分片 `Name2Id.user_name`、`chats.db.chats.chat_id`、
      `msg_meta.sender_username`）19 字符。

    于是任何"配置里的这个值 == 消息侧的那个值"的比较**永远不成立** —— 搜索侧
    （`发送者:我` 恒返回 0，Critical #17）与会话列表侧（本人会话泄漏进列表，
    known-issues #20）踩的是同一个坑。修法只有一条：比较前把两种形式都接受。

    判据（严格，**宁可不动也不乱截**）：按 `_` 切分后**≥3 段**、且**最后一段长度为 4
    且全为十六进制字符**（大小写均可）→ 去掉最后一段；其余情况（**已经是裸 wxid**、
    空值、`None`、尾段长度不对或不是十六进制）**原样返回**（`None` → `''`）。
    **幂等**：`bare_wxid(bare_wxid(x)) == bare_wxid(x)`。

    ⚠️ 调用方**不要只用返回值**：配置里到底存哪种形式无法保证，多带一个不存在的值
    在集合/`IN` 比较里是无害的，所以调用方通常**同时接受**原值与返回值
    （见 `search._resolve_scope` 与 `chat._own_wxid_forms`）。

    ⚠️ 命名是**自证式**的：它只回答"裸 wxid 长什么样"，不做任何猜测性规范化 ——
    不要改名成 `normalize*`（那会暗示它可以顺手 strip/小写/补前缀）。
    语义与 `engine.services.search._bare_wxid()` 必须逐字一致，由
    `tests/test_bare_wxid.py::test_utils_bare_wxid_matches_search_bare_wxid` 锁住。
    """
    text = (value or '').strip()
    parts = text.split('_')
    if len(parts) < 3:
        return text
    tail = parts[-1]
    if len(tail) == _WXID_DIR_SUFFIX_LEN and all(c in _HEX_DIGITS for c in tail):
        return '_'.join(parts[:-1])
    return text


# ---------------------------------------------------------------------------
# 账号 **id** 候选项：为"密钥派生"准备多个形态，由**真数据验证**取胜
# ---------------------------------------------------------------------------
# 背景（GitHub issue #16 新评论 + known-issues #43/#45/#46）
# ----------------------------------------------------------
# 微信 4.x 的账号**目录名**有三种形态（用户实测）：
#   * `<裸 id>_<4 位十六进制>`（本机就是这种，如 24 字符）
#   * 就是 `<裸 id>` 本身（没有随机后缀）
#   * `<自定义微信号>_<4 位十六进制>` —— **自定义微信号不带 `wxid_` 前缀**
# 而密钥派生（`MD5(str(code) + id)`）要的是**裸 id**，不是目录名。
# 于是历史上到处写 `if wxid.startswith('wxid_'): ... else: 跳过` —— 对第三形态
# **整条密钥链路直接失效**（issue #16 报告者：`[mmkv] Cannot determine wxid`）。
#
# 为什么不直接把 `bare_wxid()` 放宽？两条理由：
#   1. `bare_wxid()` 的"≥3 段"判据是被
#      `tests/test_bare_wxid.py` 与 `search._bare_wxid()` 逐字锁住的**比较用**语义，
#      它的调用方（搜索 `发送者:我`、会话归属）拿它去**做相等比较**，
#      放宽会让"本来就是裸 id"的值被削掉尾巴（如 `wxid_1234` → `wxid`）；
#   2. 两段式 `<id>_<4hex>` 与"裸 id 里恰好带一个下划线"**在结构上不可区分**。
# ⇒ 正确做法不是"猜一个更聪明的规则"，而是**给出多个候选项**，由调用方拿
# **真实的加密文件**去验证（`v2_key_extract._try_key` / 数据库首页 HMAC 校验），
# 验证通过的那个才算数。**这一步是安全的前提**：没有验证就不要用这些候选值。
#
# ⚠️ 这些候选项**只许用于"值比较 / 密钥派生入参"**，绝不许拿去拼**文件路径** ——
# 路径必须用**真实存在的目录名**（`media._account_dirs_under()` / `resolve_account_dir()`）。


def strip_account_dir_suffix(value) -> str:
    """去掉账号名尾部的 `_<4 位十六进制>` —— **不看前缀，两段式也认**。

    与 :func:`bare_wxid` 的区别只有一条：`bare_wxid` 要求 `_` 分段 ≥3（保守，
    宁可不动也不乱截），本函数**也处理两段式**（`myalias_68f8` → `myalias`）。
    单独一个函数是因为它**更激进**，只适合用在"后面还有真数据验证"的地方。

    幂等：`strip_account_dir_suffix(strip_account_dir_suffix(x)) == strip_account_dir_suffix(x)`
    （去掉之后尾部不再是 4 位十六进制时自然停下；`wxid_1234` 这种会继续被削，
    所以**只许在验证场景里用**）。
    """
    text = (value or '').strip()
    parts = text.split('_')
    if len(parts) < 2:
        return text
    tail = parts[-1]
    if len(tail) == _WXID_DIR_SUFFIX_LEN and all(c in _HEX_DIGITS for c in tail):
        return '_'.join(parts[:-1])
    return text


def account_id_candidates(value) -> list:
    """把"一个可能是账号目录名 / 裸 id 的值"展开成**候选账号 id**（按可信度排序）。

    返回去重后的列表（可能为空列表 = 没拿到任何可用输入）。顺序即"猜"的先后：
    原值 → `bare_wxid()`（严格规则）→ 去 `_<4hex>` 后缀（宽松规则）。

    ⚠️ **调用方必须用真数据验证**（见本节的模块级说明）：这些是**候选**，不是结论。
    ⚠️ **不许**用返回值拼路径。
    ⚠️ 返回 `[]`（拿不到任何输入）与"候选都验证失败"是**两件不同的事**，
    日志里必须分开说 —— 这正是 issue #16 里 `Cannot determine wxid` 那句话
    既没说清"是没候选还是候选不匹配"、又把整条链路静默掐断的地方。
    """
    raw = (value or '').strip()
    out = []
    for cand in (raw, bare_wxid(raw), strip_account_dir_suffix(raw),
                 strip_account_dir_suffix(bare_wxid(raw)),
                 bare_wxid(strip_account_dir_suffix(raw))):
        if cand and cand not in out:
            out.append(cand)
    return out
