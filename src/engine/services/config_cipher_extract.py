# -*- coding: utf-8 -*-
"""WeChat 4.1.10+ DB key extraction via read-only Config.Cipher scan.

Background
----------
WeChat 4.1+ no longer keeps the raw DB encryption key (x'<64hex>' strings) in
process memory. Instead the WCDB runtime keeps a `com.Tencent.WCDB.Config.Cipher`
string object whose config blob is XOR-obfuscated with a FIXED 32-byte mask and
contains the `x'<64hex key><32hex salt>'` literal for every database.

The mask and object layout are version-stable constants (verified against
Weixin 4.1.12.55, 2026-08). Because the scan only needs PROCESS_VM_READ |
PROCESS_QUERY_INFORMATION, it works WITHOUT administrator rights and WITHOUT
restarting WeChat - a big usability win over the hook strategies.

Layout (per process):
  string "com.Tencent.WCDB.Config.Cipher" -> find its std::string node
    node+0x10 = data ptr, node+0x18 = length            (validated)
    node+0x28 = config_ptr
    obj = read(config_ptr+0x88, 0x28) ; data_ptr=obj+0x8, data_len=obj+0x10
    blob = read(data_ptr, data_len)   (0 < len <= 1024)
  blob ^ XOR_MASK (repeating) -> decode -> find x'<64..192 hex>' literals
  key = first 64 hex chars, optional embedded salt = next 32 hex chars
  verify via HMAC-SHA512 (verify_enc_key); save matches.

Reference: github.com/TANGandXue/wcdb-key-tool (wcdb_key_tool_windows.py).
"""
import ctypes
import ctypes.wintypes as wt
import hashlib
import hmac as hmac_mod
import os
import re
import struct
import time

PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16
HMAC_SZ = 64
RESERVE_SZ = 80
IV_SZ = 16

# ---- Config.Cipher constants (version-stable) ----
CONFIG_CIPHER_NAME = b'com.Tencent.WCDB.Config.Cipher'
CONFIG_XOR_MASK = bytes.fromhex(
    "d2c7442458020000004889442450488b"
    "450048844c2448488944254048584c24"
)
CONFIG_BLOB_MAX = 1024
CONFIG_LITERAL_RE = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")
MAX_USER_ADDRESS = 0x0000_8000_0000_0000

_KNOWN_EXE_NAMES = {'weixin.exe', 'wechat.exe'}

kernel32 = ctypes.windll.kernel32
MEM_COMMIT = 0x1000
READABLE = {0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80}


class MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_uint64), ("AllocationBase", ctypes.c_uint64),
        ("AllocationProtect", wt.DWORD), ("_pad1", wt.DWORD),
        ("RegionSize", ctypes.c_uint64), ("State", wt.DWORD),
        ("Protect", wt.DWORD), ("Type", wt.DWORD), ("_pad2", wt.DWORD),
    ]


# ---------------------------------------------------------------------------
#  HMAC verification (SQLCipher 4, same as key_scan / wechat_key_extract)
# ---------------------------------------------------------------------------
def verify_enc_key(enc_key, db_page1):
    salt = db_page1[:SALT_SZ]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)
    hmac_data = db_page1[SALT_SZ: PAGE_SZ - RESERVE_SZ + IV_SZ]
    stored_hmac = db_page1[PAGE_SZ - HMAC_SZ: PAGE_SZ]
    hm = hmac_mod.new(mac_key, hmac_data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hm.digest() == stored_hmac


# ---------------------------------------------------------------------------
#  Process / memory helpers (pure ctypes, no psutil dependency)
# ---------------------------------------------------------------------------
def find_wechat_pids():
    """Return [(rss_bytes, pid), ...] sorted by memory desc (pure ctypes)."""
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
            ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
            ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", wt.LONG),
            ("dwFlags", wt.DWORD), ("szExeFile", ctypes.c_char * 260),
        ]

    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return []
    pids = []
    pe = PROCESSENTRY32()
    pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
    psapi = ctypes.windll.psapi
    if kernel32.Process32First(snapshot, ctypes.byref(pe)):
        while True:
            exe = pe.szExeFile.decode('utf-8', errors='replace').lower()
            if exe in _KNOWN_EXE_NAMES:
                pid = pe.th32ProcessID
                h = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ,
                                         False, pid)
                mem = 0
                if h:
                    try:
                        pmc = PROCESS_MEMORY_COUNTERS()
                        pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
                        if psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
                            mem = pmc.WorkingSetSize
                    finally:
                        kernel32.CloseHandle(h)
                pids.append((mem, pid))
            if not kernel32.Process32Next(snapshot, ctypes.byref(pe)):
                break
    kernel32.CloseHandle(snapshot)
    pids.sort(key=lambda x: x[0], reverse=True)
    return pids


def read_mem(h, addr, sz):
    buf = ctypes.create_string_buffer(sz)
    n = ctypes.c_size_t(0)
    if kernel32.ReadProcessMemory(h, ctypes.c_uint64(addr), buf, sz, ctypes.byref(n)):
        return buf.raw[:n.value]
    return None


def enum_regions(h):
    regs = []
    addr = 0
    mbi = MBI()
    while addr < 0x7FFFFFFFFFFF:
        if kernel32.VirtualQueryEx(h, ctypes.c_uint64(addr), ctypes.byref(mbi),
                                   ctypes.sizeof(mbi)) == 0:
            break
        if (mbi.State == MEM_COMMIT and mbi.Protect in READABLE
                and 0 < mbi.RegionSize < 500 * 1024 * 1024):
            regs.append((mbi.BaseAddress, mbi.RegionSize))
        nxt = mbi.BaseAddress + mbi.RegionSize
        if nxt <= addr:
            break
        addr = nxt
    return regs


def _u64_from(data, offset):
    if offset < 0 or offset + 8 > len(data):
        return 0
    return struct.unpack_from("<Q", data, offset)[0]


def _probable_32_byte_key(data):
    return (len(data) == KEY_SZ and len(set(data)) >= 15
            and data not in {b"\x00" * KEY_SZ, b"\xff" * KEY_SZ})


def _xor_repeat(data, mask):
    return bytes(v ^ mask[i % len(mask)] for i, v in enumerate(data))


def _iter_chunks(regions, read_region, chunk_size=2 * 1024 * 1024, overlap=0):
    for base, size in regions:
        offset = 0
        tail = b""
        tail_base = base
        while offset < size:
            cur = min(chunk_size, size - offset)
            chunk = read_region(base + offset, cur) or b""
            data_base = tail_base if tail else base + offset
            data = tail + chunk
            if data:
                yield data_base, data
                if overlap:
                    tail = data[-overlap:]
                    tail_base = data_base + max(0, len(data) - len(tail))
                else:
                    tail = b""
                    tail_base = base + offset + cur
            else:
                tail = b""
                tail_base = base + offset + cur
            offset += cur


# ---------------------------------------------------------------------------
#  Candidate extraction from a decoded Config.Cipher blob
# ---------------------------------------------------------------------------
def _blob_key_candidates(blob):
    """XOR-decode the blob and yield (key_hex, embedded_salt_or_None)."""
    if not blob or len(blob) > CONFIG_BLOB_MAX:
        return
    decoded = _xor_repeat(blob, CONFIG_XOR_MASK)
    seen = set()
    for m in CONFIG_LITERAL_RE.finditer(decoded):
        run = m.group(1).decode("ascii").lower()
        starts = [0]
        if len(run) > 96:
            starts.extend(range(0, len(run) - 63, 32))
            starts.append(len(run) - 64)
        for start in dict.fromkeys(starts):
            if start < 0 or start + 64 > len(run):
                continue
            key_hex = run[start:start + 64]
            try:
                key = bytes.fromhex(key_hex)
            except ValueError:
                continue
            if not _probable_32_byte_key(key):
                continue
            embedded = run[start + 64:start + 96] if start + 96 <= len(run) else None
            item = (key_hex, embedded)
            if item not in seen:
                seen.add(item)
                yield item


# ---------------------------------------------------------------------------
#  Per-process scan
# ---------------------------------------------------------------------------
def _detect_db_dirs():
    """本机所有微信 ``db_storage`` 候选目录。

    单独的薄封装只是为了**可被测试注入**（测试里绝不允许去探测本机真实微信目录）。
    """
    try:
        from engine.utils import find_all_wechat_data_dirs
        return [d.get('db_path') for d in find_all_wechat_data_dirs() if d.get('db_path')]
    except Exception:
        return []


def find_db_dir_matching_salts(candidate_salts, exclude=None, db_dirs=None):
    """找出"这批候选密钥的 salt 与哪个 ``db_storage`` 最匹配"（issue #15 的根因修法）。

    背景：微信 4.x 每个库有自己的密钥，内存里 Config.Cipher 里那批密钥属于
    **当前正在运行的那个账号**。如果被扫描的目录是**另一个账号**（多账号机器上很常见，
    界面里那个"上次用过的目录"就是），就会出现"取到了一堆候选、但一个都验不过"。

    与"微信不支持/版本变了"的区别是**可判定的**：候选密钥**自带 salt**，
    拿它们去和每个候选目录的 salt 表求交即可 —— 相交不为空说明"内存里这批密钥属于这个目录"。

    Returns:
        ``(db_dir, hits)``；没有任何目录有交集时返回 ``(None, 0)``。**从不抛异常。**
    """
    cand = set(s.lower() for s in (candidate_salts or set()) if s)
    if not cand:
        return None, 0
    if db_dirs is None:
        db_dirs = _detect_db_dirs()
    ex = os.path.normcase(os.path.normpath(exclude)) if exclude else None
    best_dir, best_hits = None, 0
    for d in db_dirs or []:
        if not d:
            continue
        if ex and os.path.normcase(os.path.normpath(d)) == ex:
            continue
        try:
            from engine.services.wechat_key_extract import collect_db_files
            _files, salt_to_dbs = collect_db_files(d)
        except Exception:
            continue
        hits = len(cand & set(s.lower() for s in salt_to_dbs))
        if hits > best_hits:
            best_dir, best_hits = d, hits
    return best_dir, best_hits


def _explain_failure(stats_list, salt_to_dbs, cand_salts, print_fn, *, opening_advice=True):
    """失败时给出**原因 + 可操作建议**（issue #15 用户明确要求的那一条）。

    以前这里只留一句"未能从任何微信进程中提取到密钥"，用户无从下手。
    现在把每进程的原始计数与**分档结论**都打出来，并给出下一步。
    """
    print_fn("[Cipher] 未能提取到任何密钥。本次扫描的诊断信息：")
    for i, st in enumerate(stats_list, 1):
        if st.get('opened'):
            print_fn("  进程#%d: 可读内存区域 %d | Config.Cipher 字样 %d 处 | 节点 %d | "
                     "候选密钥 %d | 验证通过 %d"
                     % (i, st.get('regions', 0), st.get('needles', 0), st.get('nodes', 0),
                        st.get('candidates', 0), st.get('verified', 0)))
        else:
            print_fn("  进程#%d: **打不开**（OpenProcess 失败，GetLastError=%d）"
                     " | 其余统计不可用"
                     % (i, st.get('open_error', 0)))

    n_pids = len(stats_list)
    opened = [st for st in stats_list if st.get('opened')]
    needles = sum(st.get('needles', 0) for st in stats_list)
    nodes = sum(st.get('nodes', 0) for st in stats_list)
    cands = sum(st.get('candidates', 0) for st in stats_list)
    known_salts = set(s.lower() for s in (salt_to_dbs or {}))
    cand_set = set(s.lower() for s in (cand_salts or set()) if s)

    print_fn("[Cipher] 分档结论：")
    if n_pids and not opened:
        errs = sorted(set(st.get('open_error', 0) for st in stats_list))
        print_fn("  * 一个微信进程都读不了（GetLastError=%s）。"
                 "ERROR_ACCESS_DENIED(5) 最常见的原因是**权限不足**。"
                 % (','.join(str(e) for e in errs)))
        if opening_advice:
            print_fn("    建议：**以管理员身份重新运行本程序**后重试；"
                     "同时确认杀软没有拦截对微信进程的读取。")
    elif needles == 0:
        print_fn("  * 在所有进程的内存里**都没有找到 Config.Cipher 字样** ⇒ "
                 "多半不是微信主进程，或该微信版本的对象结构已不同。")
        print_fn("    建议：确认微信**已登录并保持运行**；以管理员身份重试；"
                 "仍失败请改用「Hook」策略或参考 README 的手动输入密钥。")
    elif nodes == 0:
        print_fn("  * 找到了 Config.Cipher 字样（%d 处），但**没有任何节点结构匹配上** ⇒ "
                 "通常是微信版本变化导致内存布局不同。" % needles)
        print_fn("    建议：核对微信版本（本项目在 4.1.12.55 上验证）；"
                 "把本文诊断信息反馈给维护者 yuuu。")
    elif cands == 0:
        print_fn("  * 找到了节点（%d 个），但**没能从配置块里取出任何候选密钥** ⇒ "
                 "配置块的读取或解码有问题。" % nodes)
        print_fn("    建议：以管理员身份重试；仍失败请把本文诊断信息反馈给维护者 yuuu。")
    elif cand_set and known_salts and not (cand_set & known_salts):
        print_fn("  * 取到了 %d 个候选密钥，但它们**自带的 salt 与被扫描的 %d 个数据库"
                 "一个都不匹配**。" % (cands, len(known_salts)))
        print_fn("    ⇒ 最可能的原因是：**你扫描的 db_storage 与当前登录的微信账号不是同一个**"
                 "（多账号机器上很容易发生——界面里那个目录可能是上次用过的另一个账号）。")
        print_fn("    建议：在页面上确认「微信数据目录」选的是**微信正在使用的那个**"
                 "（可用「🔍 深度搜索 / 检测到的数据目录」里的推荐项，标有 ⭐ 的就是正在使用的），"
                 "或直接在微信里「设置 → 文件管理 → 打开文件夹」核对后重试。")
    else:
        print_fn("  * 取到了 %d 个候选密钥，与所扫目录的 salt 有交集，但**全部未通过校验** ⇒ "
                 "这些库可能已被换过密钥，或首页数据异常。" % cands)
        print_fn("    建议：用「Hook」策略重试，或在「手动输入密钥」页粘贴密钥后导入。")


def scan_pid_for_config_cipher(pid, db_files, salt_to_dbs, key_map, print_fn,
                               remaining_salts):
    """Read-only Config.Cipher scan of one WeChat PID.

    Returns dict of stats; mutates key_map / remaining_salts in place.
    """
    stats = {"needles": 0, "nodes": 0, "candidates": 0, "verified": 0,
             # issue #15：诊断字段 —— "打不开进程"与"打开了但没找到"必须能区分开
             "opened": False, "open_error": 0, "regions": 0,
             # 候选密钥**自带的 salt**：用来判定"内存里这批密钥属于哪个账号目录"
             "cand_salts": set()}
    h = kernel32.OpenProcess(0x0010 | 0x0400, False, pid)  # VM_READ|QUERY
    if not h:
        stats["open_error"] = int(kernel32.GetLastError() or 0)
        return stats
    stats["opened"] = True
    try:
        regions = enum_regions(h)
        stats["regions"] = len(regions) if regions else 0
        if not regions:
            return stats

        # 1) locate the literal string occurrences
        needle_addrs = set()
        for base, data in _iter_chunks(regions,
                                       lambda a, s: read_mem(h, a, s),
                                       overlap=len(CONFIG_CIPHER_NAME) - 1):
            pos = data.find(CONFIG_CIPHER_NAME)
            while pos >= 0:
                needle_addrs.add(base + pos)
                pos = data.find(CONFIG_CIPHER_NAME, pos + 1)
        stats["needles"] = len(needle_addrs)
        if not needle_addrs:
            return stats

        pair_patterns = [
            struct.pack("<Q", addr) + struct.pack("<Q", len(CONFIG_CIPHER_NAME))
            for addr in needle_addrs
        ]
        seen_cands = set()

        for base, data in _iter_chunks(regions, lambda a, s: read_mem(h, a, s),
                                       overlap=0x80):
            if not remaining_salts:
                break
            for pat in pair_patterns:
                pos = data.find(pat)
                while pos >= 0:
                    qaddr = base + pos
                    node_base = qaddr - 0x10
                    node = read_mem(h, node_base, 0x50)
                    if node and len(node) >= 0x40:
                        if (_u64_from(node, 0x10) in needle_addrs
                                and _u64_from(node, 0x18) == len(CONFIG_CIPHER_NAME)):
                            config_ptr = _u64_from(node, 0x28)
                            if 0x10000 <= config_ptr < MAX_USER_ADDRESS:
                                stats["nodes"] += 1
                                obj = read_mem(h, config_ptr + 0x88, 0x28)
                                if obj and len(obj) >= 0x18:
                                    data_ptr = _u64_from(obj, 0x8)
                                    data_len = _u64_from(obj, 0x10)
                                    if (0 < data_len <= CONFIG_BLOB_MAX
                                            and 0x10000 <= data_ptr < MAX_USER_ADDRESS):
                                        blob = read_mem(h, data_ptr, int(data_len))
                                        if blob and len(blob) == data_len:
                                            for key_hex, emb_salt in _blob_key_candidates(blob):
                                                cand = (key_hex, emb_salt)
                                                if cand in seen_cands:
                                                    continue
                                                seen_cands.add(cand)
                                                stats["candidates"] += 1
                                                if emb_salt:
                                                    stats["cand_salts"].add(emb_salt)
                                                try:
                                                    key = bytes.fromhex(key_hex)
                                                except ValueError:
                                                    continue
                                                if emb_salt and emb_salt in remaining_salts:
                                                    target_salts = [emb_salt]
                                                else:
                                                    target_salts = list(remaining_salts)
                                                for salt_hex in target_salts:
                                                    if salt_hex not in remaining_salts:
                                                        continue
                                                    for rel, _p, _sz, s, page1 in db_files:
                                                        if s == salt_hex and verify_enc_key(key, page1):
                                                            key_map[salt_hex] = key_hex
                                                            remaining_salts.discard(salt_hex)
                                                            stats["verified"] += 1
                                                            print_fn(
                                                                f"  [Cipher-FOUND] {rel} "
                                                                f"salt={salt_hex} -> {key_hex}")
                                                            break
                                                    if salt_hex not in remaining_salts:
                                                        break
                    pos = data.find(pat, pos + 1)
    finally:
        kernel32.CloseHandle(h)
    return stats


# ---------------------------------------------------------------------------
#  Top-level entry (strategy-compatible with key_scan / wechat_key_extract)
# ---------------------------------------------------------------------------
def extract_keys_via_config_cipher(db_dir, db_files, salt_to_dbs, key_map,
                                   print_fn=None, progress_fn=None, *, resolution=None):
    """Extract DB keys with the read-only Config.Cipher scan.

    No admin rights, no WeChat restart, no hooking. Works on WeChat 4.1.10+
    (verified 4.1.12.55). Mutates key_map in place; returns keys found.

    Args:
        db_dir: path to WeChat db_storage (used only for messages)
        db_files: list from collect_db_files()
        salt_to_dbs: {salt_hex: [rel,...]}
        key_map: dict being filled {salt_hex: key_hex}
        print_fn / progress_fn: optional callbacks
        resolution: 可选出参。若本次**自动换过目标目录**（issue #15：内存里的密钥属于
            另一个账号），这里会被填上 ``{'db_dir','db_files','salt_to_dbs','retargeted','reason'}``
            —— 调用方必须据此**改用新的目标**去保存结果，否则密钥会存到错的账号名下。
            失败时也会写入 ``reason``（``no_pids`` / ``no_keys`` / ``account_mismatch``）。

    Returns:
        int — 本次验证通过的密钥数（语义与旧版一致；新增的 ``resolution`` 是**可选**的）。
    """
    if print_fn is None:
        print_fn = print
    if progress_fn is None:
        progress_fn = lambda pct, msg: None

    remaining = set(salt_to_dbs) - set(key_map)
    if not remaining:
        return 0

    pids = find_wechat_pids()
    if not pids:
        print_fn("[Cipher] 未检测到微信进程。请先启动微信并登录，然后重试。")
        print_fn("[Cipher] 启动微信后无需任何额外操作——本扫描为只读，不注入、不重启。")
        if resolution is not None:
            resolution['reason'] = 'no_pids'
        return 0

    progress_fn(0, "Config.Cipher 只读扫描：检测到微信进程，开始定位密钥对象...")
    print_fn(f"[Cipher] 检测到微信进程: {[p for _, p in pids]}")
    print_fn("[Cipher] 只读扫描 WCDB Config.Cipher 对象 (无需管理员权限) ...")

    t0 = time.time()
    stats_list, total_found, cand_salts = _scan_pids_for_keys(
        pids, db_files, salt_to_dbs, key_map, print_fn, progress_fn, remaining)

    # --- issue #15 的根因修法：候选取到了却一个都没验过 ⇒ 先怀疑"扫错账号目录" ---
    # 内存里那批密钥属于**当前正在运行的账号**；候选**自带 salt**，所以"属于哪个目录"是**可判定**的。
    if not total_found and cand_salts:
        new_dir, hits = find_db_dir_matching_salts(cand_salts, exclude=db_dir)
        if new_dir:
            print_fn(f"[Cipher] 注意：本次取到的 {len(cand_salts)} 个候选密钥的 salt "
                     f"与被扫描的目录**完全不匹配**，但与另一个微信数据目录匹配 {hits} 个。")
            print_fn(f"[Cipher] 内存里的密钥属于**当前正在运行的那个账号** ⇒ "
                     f"自动改用该目录重扫一次：{new_dir}")
            try:
                from engine.services.wechat_key_extract import collect_db_files
                new_files, new_salts = collect_db_files(new_dir)
            except Exception as e:
                print_fn(f"[Cipher] 读取该目录失败({type(e).__name__})，继续用原目录的结论。")
                new_files = None
            if new_files:
                remaining2 = set(new_salts) - set(key_map)
                if remaining2:
                    stats2, found2, _c2 = _scan_pids_for_keys(
                        pids, new_files, new_salts, key_map, print_fn, progress_fn, remaining2)
                    stats_list += stats2
                    total_found += found2
                if total_found and resolution is not None:
                    resolution.update({'db_dir': new_dir, 'db_files': new_files,
                                       'salt_to_dbs': new_salts, 'retargeted': True,
                                       'reason': 'account_mismatch'})
                elif not total_found:
                    print_fn("[Cipher] 换到该目录后仍然没能验证通过。")

    if not total_found:
        _explain_failure(stats_list, salt_to_dbs, cand_salts, print_fn)

    print_fn(f"[Cipher] 扫描完成: {time.time() - t0:.1f}s, "
             f"共验证 {total_found} 个密钥")
    if resolution is not None and not total_found and 'reason' not in resolution:
        resolution['reason'] = 'no_keys'
    return total_found


def _scan_pids_for_keys(pids, db_files, salt_to_dbs, key_map, print_fn, progress_fn,
                        remaining):
    """逐 PID 扫描；返回 ``(stats_list, total_found, cand_salts)``。"""
    stats_list = []
    total_found = 0
    cand_salts = set()
    for mem, pid in pids:
        if not remaining:
            break
        stats = scan_pid_for_config_cipher(pid, db_files, salt_to_dbs, key_map,
                                           print_fn, remaining)
        stats_list.append(stats)
        cand_salts |= set(stats.get('cand_salts') or ())
        progress_fn(min(90, 20 + total_found * 5),
                    f"进程 PID={pid}: 已验证 {stats['verified']} 个密钥"
                    f"（候选 {stats['candidates']} 个）")
        if stats["verified"]:
            total_found += stats["verified"]
            print_fn(f"[Cipher] PID={pid}: {stats['verified']} 个密钥验证通过 "
                     f"(节点 {stats['nodes']}, 候选 {stats['candidates']})")
    return stats_list, total_found, cand_salts

