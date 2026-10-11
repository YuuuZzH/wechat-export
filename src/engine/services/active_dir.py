"""按「微信进程实际在用哪个 db_storage」给检测到的目录排序并标注。

用户需求：目录选择对话框要「结合分析当前微信进程在使用哪个 db，给出推荐目录优先的
排序，并标注」。

三级信号（从强到弱，任一级失败都会降级到下一级）：

  T0  句柄归属：``NtQuerySystemInformation(SystemExtendedHandleInformation)`` 拿到全系统
      句柄快照 → 过滤微信 PID 的 File 句柄 → ``DuplicateHandle`` 到自己进程 →
      ``NtQueryObject(ObjectNameInformation)`` 拿设备路径 → ``QueryDosDevice`` 映射盘符。
      **精确命中**「微信正在用哪一个 db_storage」。
  T1  独占打开探测：``CreateFileW(path, GENERIC_READ, share=0)``，失败且
      ``GetLastError() == ERROR_SHARING_VIOLATION`` ⇒ 有进程正持有该 db。只看能否打开，
      打开成功立刻 ``CloseHandle``。
  T2  最近写入时间：``message/*.db-shm|*.db-wal`` 的最新 mtime 距今多少分钟。

红线（本项目 SDD 硬约束）：
  * **只用标准库 ctypes**，**本模块**不引入 psutil：自己的依赖面保持最小，且**不假设 psutil 可用**
    （psutil 自 Task 24 起虽已被打包链包含，但本模块不为它改变行为或打包参数）。
  * **不写任何文件**，不对微信的 db 做任何写操作。
  * **绝不抛异常**：任何一步失败都降级，并在 ``Probe.errors`` 里留下原因（不静默）。
  * 端点整体 2 秒内返回：T0 有总时间预算，超时就降级并记原因。

踩坑记录（已修，勿回退）：
  1. ``NtQuerySystemInformation`` 返回头是**两个 ULONG_PTR**（NumberOfHandles + Reserved）
     ⇒ 数组起始偏移 = ``2 * sizeof(size_t)``（见 ``_HEADER_BYTES``）。
  2. 缓冲区要留余量（``ret + 1MB``），否则句柄数在两次调用之间变动会一直
     ``STATUS_INFO_LENGTH_MISMATCH``。
  3. ctypes 默认 ``restype=c_int`` 会把 ``GetCurrentProcess()`` 的伪句柄（-1）截断，
     ``DuplicateHandle`` 于是**静默失败** ⇒ 所有返回句柄的 Win32 调用都显式声明
     ``restype = ctypes.c_void_p`` + ``argtypes``（见 ``_HANDLE_FUNCS`` 与自检测试）。
  4. ``NtQueryObject`` 对某些句柄类型会**挂住** ⇒ 每个句柄查询放在线程池里做，并给整体
     超时；超时放弃未完成的部分、保留已查到的结果（daemon 线程不阻止进程退出）。
"""
from __future__ import annotations

import ctypes
import os
import struct
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field

_IS_WINDOWS = os.name == 'nt'

# --- 等级 / 文案（前后端必须**逐字一致**）---
REASON_IN_USE = "⭐ 微信进程正在使用（PID {pids}）"
REASON_LOCKED = "⭐ 数据库正被占用（微信正在运行）"
REASON_RECENT = "最近活跃（{n} 分钟前有写入）"
REASON_CONFIG = "微信配置指向此目录"
REASON_IDLE = "未发现活动迹象"

PID_SEP = ','                          # 「PID x,y」用半角逗号分隔
RECENT_MINUTES = 15.0                  # T2 ≤ 15 分钟算「最近活跃」
TIER_ORDER = {'in_use': 0, 'locked': 1, 'recent': 2, 'config': 3, 'idle': 4}
WECHAT_EXE_NAMES = ('weixin.exe', 'wechat.exe')

# --- Win32 常量 ---
TH32CS_SNAPPROCESS = 0x00000002
PROCESS_DUP_HANDLE = 0x0040
DUPLICATE_SAME_ACCESS = 0x0002
GENERIC_READ = 0x80000000
FILE_SHARE_NONE = 0x00000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
ERROR_SHARING_VIOLATION = 32
ERROR_LOCK_VIOLATION = 33
ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_ACCESS_DENIED = 5
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
SYSTEM_EXTENDED_HANDLE_INFORMATION = 64
OBJECT_NAME_INFORMATION = 1

# --- NtQuerySystemInformation 返回布局（x64）---
# 头部：两个 ULONG_PTR（NumberOfHandles + Reserved）。数组元素字段偏移：
#   Object(0) UniqueProcessId(8) HandleValue(16) ObjectTypeIndex(24) GrantedAccess(32)
# 条目**步长**本机实测为 40 字节（不是常见文档里的 56）：
#   需要的缓冲区大小 = 16 + 40 * 句柄数（本机 153536 句柄 ⇒ 6141456 字节，精确吻合）。
# 步长搞错的后果**不是崩溃而是静默采样**：步长 56 时只有 1/5 的条目落在真实边界上，
# 于是 T0 只看到 20% 的句柄、File 类型号也查不到（本机实测踩到过）。
# 因此这里只把 40 当默认值，真正用的是 ``_pick_entry_size`` 的自校准结果。
_ENTRY_FMT = '<QQQQI'          # Object, UniqueProcessId, HandleValue, ObjectTypeIndex, GrantedAccess
_ENTRY_SIZE = 40               # 默认值（本机实测步长）
_ENTRY_SIZE_CANDIDATES = (40, 56, 48, 32)
_MAX_PLAUSIBLE_PID = 1 << 24   # Windows PID 远小于此；步长错时读到的是 8 字节随机量
_HEADER_BYTES = 2 * struct.calcsize('P')   # 坑 1：两个 ULONG_PTR
_PVOID_MAX = ctypes.c_void_p(-1).value


# ---------------------------------------------------------------------------
#  Win32 绑定：所有返回句柄的调用都显式声明 restype/argtypes（坑 3）
# ---------------------------------------------------------------------------
def _load_lib(name):
    if not _IS_WINDOWS:
        return None
    try:
        return ctypes.WinDLL(name, use_last_error=True)
    except OSError:
        return None


_kernel32 = _load_lib('kernel32')
_ntdll = _load_lib('ntdll')


def _bind(lib, name, restype, argtypes):
    if lib is None:
        return None
    fn = getattr(lib, name, None)
    if fn is None:
        return None
    fn.restype = restype
    fn.argtypes = argtypes
    return fn


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ('dwSize', wintypes.DWORD),
        ('cntUsage', wintypes.DWORD),
        ('th32ProcessID', wintypes.DWORD),
        ('th32DefaultHeapID', ctypes.c_void_p),
        ('th32ModuleID', wintypes.DWORD),
        ('cntThreads', wintypes.DWORD),
        ('th32ParentProcessID', wintypes.DWORD),
        ('pcPriClassBase', ctypes.c_long),
        ('dwFlags', wintypes.DWORD),
        ('szExeFile', ctypes.c_wchar * 260),
    ]


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [('Length', ctypes.c_ushort),
                ('MaximumLength', ctypes.c_ushort),
                ('Buffer', ctypes.c_void_p)]


_H = ctypes.c_void_p
_PE32W = ctypes.POINTER(_PROCESSENTRY32W)

_HANDLE_FUNCS = {
    # 名字 -> 绑定的函数（None 表示该平台/库没有）
    'CreateToolhelp32Snapshot': _bind(_kernel32, 'CreateToolhelp32Snapshot',
                                      _H, [wintypes.DWORD, wintypes.DWORD]),
    'OpenProcess': _bind(_kernel32, 'OpenProcess',
                         _H, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]),
    'CreateFileW': _bind(_kernel32, 'CreateFileW',
                         _H, [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, _H]),
    'GetCurrentProcess': _bind(_kernel32, 'GetCurrentProcess', _H, []),
}
_Process32FirstW = _bind(_kernel32, 'Process32FirstW', wintypes.BOOL, [_H, _PE32W])
_Process32NextW = _bind(_kernel32, 'Process32NextW', wintypes.BOOL, [_H, _PE32W])
_CloseHandle = _bind(_kernel32, 'CloseHandle', wintypes.BOOL, [_H])
_DuplicateHandle = _bind(_kernel32, 'DuplicateHandle', wintypes.BOOL,
                         [_H, _H, _H, ctypes.POINTER(_H), wintypes.DWORD,
                          wintypes.BOOL, wintypes.DWORD])
_QueryDosDeviceW = _bind(_kernel32, 'QueryDosDeviceW', wintypes.DWORD,
                         [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_wchar),
                          wintypes.DWORD])
_NtQuerySystemInformation = _bind(_ntdll, 'NtQuerySystemInformation', ctypes.c_long,
                                  [ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                                   ctypes.POINTER(ctypes.c_ulong)])
_NtQueryObject = _bind(_ntdll, 'NtQueryObject', ctypes.c_long,
                       [_H, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                        ctypes.POINTER(ctypes.c_ulong)])

_CreateToolhelp32Snapshot = _HANDLE_FUNCS['CreateToolhelp32Snapshot']
_OpenProcess = _HANDLE_FUNCS['OpenProcess']
_CreateFileW = _HANDLE_FUNCS['CreateFileW']
_GetCurrentProcess = _HANDLE_FUNCS['GetCurrentProcess']


def _win32_prototypes():
    """返回 [(名字, 函数对象), ...]：所有**返回句柄**的 Win32 调用。

    自检用（也是测试断言点）：这些函数的 ``restype`` 必须是 ``c_void_p``，
    否则伪句柄/无效句柄 -1 会被截断成 c_int（坑 3）。
    """
    return [(name, fn) for name, fn in _HANDLE_FUNCS.items() if fn is not None]


def _is_invalid_handle(h):
    if h is None:
        return True
    try:
        v = int(h)
    except (TypeError, ValueError):
        return True
    return v == 0 or v == _PVOID_MAX or (v & 0xFFFFFFFF) == 0xFFFFFFFF


def _note(errors, message):
    """记录原因（绝不静默）；errors 为 None 时忽略。"""
    if errors is None:
        return
    try:
        errors.append(str(message))
    except Exception:
        pass


def _close_handle(h):
    if h is None or _CloseHandle is None:
        return
    try:
        _CloseHandle(_H(int(h)))
    except (OSError, TypeError, ValueError, ctypes.ArgumentError):
        pass


def normkey(path):
    """路径归一化键（大小写/斜杠不敏感），用于把信号对到目录上。"""
    if path is None:
        return ''
    try:
        return os.path.normcase(os.path.normpath(str(path)))
    except (TypeError, ValueError):
        return str(path).lower()


# ---------------------------------------------------------------------------
#  T0：进程枚举 + 句柄归属
# ---------------------------------------------------------------------------
def wechat_pids(errors=None):
    """返回 ``Weixin.exe`` / ``WeChat.exe`` 的 PID 列表（升序去重）。绝不抛。"""
    if _CreateToolhelp32Snapshot is None or _Process32FirstW is None:
        _note(errors, 't0: 进程枚举不可用（非 Windows 或 kernel32 缺失）')
        return []
    pids = set()
    snap = None
    try:
        snap = _CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if _is_invalid_handle(snap):
            _note(errors, 't0: 进程枚举 CreateToolhelp32Snapshot 失败（错误码 %s）'
                  % ctypes.get_last_error())
            return []
        pe = _PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = _Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            try:
                if (pe.szExeFile or '').lower() in WECHAT_EXE_NAMES:
                    pid = int(pe.th32ProcessID)
                    if pid > 0:
                        pids.add(pid)
            except (TypeError, ValueError):
                pass
            ok = _Process32NextW(snap, ctypes.byref(pe))
    except (OSError, ctypes.ArgumentError) as e:
        _note(errors, 't0: 进程枚举异常: %s' % e)
    finally:
        _close_handle(snap)
    return sorted(pids)


def _snapshot_handles(max_tries=6, slack=1 << 20):
    """取全系统句柄快照 buffer（含头部），失败抛 ``OSError``（调用方负责降级）。"""
    if _NtQuerySystemInformation is None:
        raise OSError('ntdll.NtQuerySystemInformation 不可用')
    size = 1 << 20
    last = 0
    for _ in range(max_tries):
        buf = ctypes.create_string_buffer(size)
        ret = ctypes.c_ulong(0)
        status = _NtQuerySystemInformation(SYSTEM_EXTENDED_HANDLE_INFORMATION,
                                          buf, size, ctypes.byref(ret))
        code = int(status) & 0xFFFFFFFF
        if code == 0:
            return buf
        last = code
        if code == STATUS_INFO_LENGTH_MISMATCH:
            # 坑 2：ret 是"需要多大"，留 1MB 余量避免句柄数变动导致反复重试
            size = max(int(ret.value) + slack, size * 2)
            continue
        raise OSError('NtQuerySystemInformation 失败: 0x%08X' % code)
    raise OSError('NtQuerySystemInformation 反复返回 STATUS_INFO_LENGTH_MISMATCH: 0x%08X'
                  % last)


def _pick_entry_size(buf, sample=2000):
    """自校准条目步长（坑 5）：用「PID/句柄值是否合理」给候选步长打分。

    步长正确时几乎 100% 的条目都合理；步长错误时读到的是随机 8 字节量，
    只有恰好落在真实边界的少数条目才合理（本机实测 56 vs 40 约为 20% vs 100%）。
    Windows 的 PID 与句柄值都是 4 的倍数，据此排除「小整数恰好像 PID」的假阳性。
    """
    if buf is None or len(buf) <= _HEADER_BYTES:
        return _ENTRY_SIZE
    declared = int(struct.unpack_from('<Q', buf, 0)[0])
    best, best_score = None, -1.0
    for size in _ENTRY_SIZE_CANDIDATES:
        capacity = (len(buf) - _HEADER_BYTES) // size
        n = min(declared, capacity, int(sample))
        if n <= 0:
            continue
        ok = 0
        for i in range(n):
            base = _HEADER_BYTES + i * size
            pid = struct.unpack_from('<Q', buf, base + 8)[0]
            handle = struct.unpack_from('<Q', buf, base + 16)[0]
            if (0 <= pid < _MAX_PLAUSIBLE_PID and pid % 4 == 0
                    and 0 < handle < (1 << 32) and handle % 4 == 0):
                ok += 1
        score = float(ok) / float(n)
        if score > best_score:
            best, best_score = size, score
    return best or _ENTRY_SIZE


def _iter_handle_entries(buf, entry_size=None):
    """产出 ``(pid, handle_value, object_type_index)``。

    坑 1：数组从 2 个 ULONG_PTR 之后开始；同时按 buffer 容量夹紧（防谎报句柄数）。
    坑 5：条目步长用 ``_pick_entry_size`` 自校准（默认 40）。
    """
    if buf is None:
        return
    total = len(buf)
    if total <= _HEADER_BYTES:
        return
    size = int(entry_size) if entry_size else _pick_entry_size(buf)
    if size <= ctypes.sizeof(ctypes.c_void_p) * 2:
        return
    declared = struct.unpack_from('<Q', buf, 0)[0]
    capacity = (total - _HEADER_BYTES) // size
    for i in range(min(int(declared), capacity)):
        off = _HEADER_BYTES + i * size
        _obj, pid, handle, type_index, _access = struct.unpack_from(_ENTRY_FMT, buf, off)
        yield int(pid), int(handle), int(type_index)


def _open_read_only_raw(path, share=FILE_SHARE_NONE):
    """只读打开，返回 ``(句柄值|None, 错误码)``。**绝不写**。"""
    if not path or _CreateFileW is None:
        return None, 0
    try:
        h = _CreateFileW(str(path), GENERIC_READ, share, None, OPEN_EXISTING,
                         FILE_ATTRIBUTE_NORMAL, None)
    except (OSError, TypeError, ValueError, ctypes.ArgumentError):
        return None, 0
    if _is_invalid_handle(h):
        return None, int(ctypes.get_last_error())
    return h, 0


def _open_read_only(path, share=FILE_SHARE_NONE):
    h, _err = _open_read_only_raw(path, share)
    return h


def is_locked(path):
    """T1：``True`` = 正被某进程持有；``False`` = 能独占打开（没被占用）；``None`` = 无法判定。

    ``CreateFileW(GENERIC_READ, share=0)`` 只读打开，成功立即 ``CloseHandle``。
    """
    h, err = _open_read_only_raw(path, FILE_SHARE_NONE)
    if h is not None:
        _close_handle(h)
        return False
    if err in (ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION):
        return True
    # 不存在 / 权限不足 / 其余错误码 ⇒ 无法判定（降级，不要瞎猜）
    return None


def _open_process(pid, access=PROCESS_DUP_HANDLE):
    if _OpenProcess is None or not pid:
        return None
    try:
        h = _OpenProcess(access, False, int(pid))
    except (OSError, TypeError, ValueError, ctypes.ArgumentError):
        return None
    return None if _is_invalid_handle(h) else h


def _current_process_handle():
    """``GetCurrentProcess()`` 的伪句柄 —— 必须 restype=c_void_p，否则被截断（坑 3）。"""
    if _GetCurrentProcess is not None:
        h = _GetCurrentProcess()
        if not _is_invalid_handle(h):
            return h
    return _PVOID_MAX


def _duplicate_handle(proc_handle, handle):
    """把目标进程的句柄复制到本进程；成功返回本进程里的句柄值，失败 None。"""
    if _DuplicateHandle is None or not proc_handle:
        return None
    dup = _H()
    try:
        ok = _DuplicateHandle(_H(int(proc_handle)), _H(int(handle)),
                              _H(int(_current_process_handle())),
                              ctypes.byref(dup), 0, False, DUPLICATE_SAME_ACCESS)
    except (OSError, TypeError, ValueError, ctypes.ArgumentError):
        return None
    if not ok or _is_invalid_handle(dup.value):
        return None
    return int(dup.value)


def _query_object_name(handle, buf_size=4096):
    """``NtQueryObject(ObjectNameInformation)`` → 对象名（文件是 ``\\Device\\...`` 设备路径）。"""
    if _NtQueryObject is None or not handle:
        return None
    buf = ctypes.create_string_buffer(buf_size)
    ret = ctypes.c_ulong(0)
    try:
        status = _NtQueryObject(_H(int(handle)), OBJECT_NAME_INFORMATION, buf,
                                buf_size, ctypes.byref(ret))
    except (OSError, TypeError, ValueError, ctypes.ArgumentError):
        return None
    if int(status) & 0xFFFFFFFF:
        return None
    try:
        us = ctypes.cast(buf, ctypes.POINTER(_UNICODE_STRING)).contents
        if not us.Buffer or not us.Length:
            return None
        return ctypes.wstring_at(us.Buffer, int(us.Length) // 2)
    except (TypeError, ValueError, OSError):
        return None


def _open_probe_handles():
    """打开 1~2 个**自己的**只读文件句柄，用来反查 File 的对象类型值（读完即关）。"""
    out = []
    candidates = [__file__,
                  os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               'utils.py')]
    for path in candidates:
        h = _open_read_only(path, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE)
        if h is not None:
            out.append(int(h))
        if len(out) >= 2:
            break
    return out


def _file_type_key(snapshot, own_handles, entry_size=None):
    """返回 ``(掩码, 目标值)``：与「自己打开的文件句柄」同一对象类型的类型值。

    本机实测：File 对象的该字段是全类型常量（0x002A000000120089）。开两个不同文件
    可以检验哪些 bit 不稳定（不同 Windows 版本可能附带额外标志位），不稳定的 bit
    从掩码里去掉。取不到时返回 ``(None, None)`` ⇒ 调用方不过滤类型（降级，不是失败）。
    """
    if not own_handles:
        return None, None
    me = os.getpid()
    try:
        mine = {}
        for pid, handle, type_index in _iter_handle_entries(snapshot, entry_size):
            if pid == me:
                mine[handle] = type_index
        values = [mine[int(h)] for h in own_handles if int(h) in mine]
    except Exception:
        return None, None
    if not values:
        return None, None
    target = int(values[0])
    diff = 0
    for v in values[1:]:
        diff |= (int(v) ^ target)
    mask = (~diff) & 0xFFFFFFFFFFFFFFFF
    return mask, (target & mask)


def _resolve_handle_names(pairs, timeout_s=0.5, workers=4):
    """线程池解析句柄名，返回 ``({(pid, handle): 设备路径}, 是否超时)``。

    坑 4：``NtQueryObject`` 对某些句柄类型会挂住 ⇒ 每个查询独立线程 + 整体超时；
    超时后放弃未完成的（daemon 线程），**已查到的结果保留**。
    """
    results = {}
    jobs = list(pairs or ())
    if not jobs:
        return results, False
    lock = threading.Lock()
    state = {'next': 0}

    def worker():
        while True:
            with lock:
                i = state['next']
                if i >= len(jobs):
                    return
                state['next'] = i + 1
            pid, handle, proc_handle = jobs[i]
            name = None
            try:
                dup = _duplicate_handle(proc_handle, handle)
                if dup is not None:
                    try:
                        name = _query_object_name(dup)
                    finally:
                        _close_handle(dup)
            except Exception:
                name = None
            with lock:
                results[(int(pid), int(handle))] = name

    threads = [threading.Thread(target=worker, daemon=True)
               for _ in range(max(1, min(int(workers), len(jobs))))]
    for t in threads:
        t.start()
    deadline = time.time() + max(0.0, float(timeout_s))
    for t in threads:
        t.join(max(0.0, deadline - time.time()))
    return results, any(t.is_alive() for t in threads)


_DOS_MAP_CACHE = {'at': 0.0, 'map': {}}


def _dos_drive_map(ttl_s=30.0):
    """``\\Device\\HarddiskVolumeN`` → 盘符（缓存 30s）。"""
    now = time.time()
    cached = _DOS_MAP_CACHE['map']
    if cached and now - _DOS_MAP_CACHE['at'] < ttl_s:
        return cached
    out = {}
    if _IS_WINDOWS and _QueryDosDeviceW is not None:
        buf = ctypes.create_unicode_buffer(1024)
        for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
            try:
                n = _QueryDosDeviceW(letter + ':', buf, 1024)
            except (OSError, TypeError, ValueError, ctypes.ArgumentError):
                continue
            if n:
                dev = buf.value.rstrip('\\').lower()
                if dev.startswith('\\device\\'):
                    out.setdefault(dev, letter + ':')
    _DOS_MAP_CACHE['map'] = out
    _DOS_MAP_CACHE['at'] = now
    return out


def _device_to_dos_path(device_path):
    """``\\Device\\HarddiskVolume9\\x\\y.db`` → ``Z:\\x\\y.db``；映射不了返回 None。"""
    if not device_path or not isinstance(device_path, str):
        return None
    if not device_path.lower().startswith('\\device\\'):
        return None
    parts = device_path.split('\\')
    if len(parts) < 4 or not parts[2]:
        return None
    drive = _dos_drive_map().get('\\'.join(parts[:3]).lower())
    if not drive:
        return None
    rest = '\\'.join(parts[3:])
    return drive + '\\' + rest if rest else drive + '\\'


def _db_storage_root(path):
    """从任意路径找出所属的 ``db_storage`` 根目录（不区分大小写）；找不到返回 None。"""
    if not path or not isinstance(path, str):
        return None
    try:
        cur = os.path.normpath(path)
    except (TypeError, ValueError):
        return None
    while True:
        base = os.path.basename(cur)
        if base.lower() == 'db_storage':
            return cur
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            return None
        cur = parent


def _map_handles_to_roots(candidates, workers, timeout_s, errors):
    """把 ``[(pid, handle)]`` 解析成 ``({db_storage 根: [pid...]}, 是否超时)``。绝不抛。

    只做"打开进程 → DuplicateHandle → NtQueryObject → 设备路径 → db_storage 根"，
    不拍新快照（所以 T0 全程**只有一次** ``NtQuerySystemInformation`` 快照）。
    """
    out = {}
    proc_handles = {}
    pairs = []
    for pid, handle in candidates:
        if pid not in proc_handles:
            proc_handles[pid] = _open_process(pid)
            if proc_handles[pid] is None:
                _note(errors, 't0: OpenProcess(PID=%d) 失败（错误码 %s）'
                      % (pid, ctypes.get_last_error()))
        if proc_handles[pid] is None:
            continue
        pairs.append((pid, handle, proc_handles[pid]))

    try:
        if not pairs:
            return {}, False
        names, timed_out = _resolve_handle_names(pairs, timeout_s=timeout_s,
                                                workers=workers)
        if timed_out:
            _note(errors, 't0: 句柄名查询超时（%.0f ms 预算用尽），已保留查到的部分结果'
                  % (float(timeout_s) * 1000.0))
        for pid, handle, _ph in pairs:
            name = names.get((int(pid), int(handle)))
            if not name:
                continue
            dos_path = _device_to_dos_path(name)
            if not dos_path:
                continue
            root = _db_storage_root(dos_path)
            if not root:
                continue
            out.setdefault(root, set()).add(int(pid))
        return {k: sorted(v) for k, v in out.items()}, timed_out
    finally:
        for ph in proc_handles.values():
            _close_handle(ph)


def _scan_in_use(pids, errors=None, timeout_s=0.5, workers=4):
    """T0 主体，返回 ``({db_storage 根: [pid...]}, 是否超时)``。绝不抛。

    优化（控制方提示）：``learn_file_type_index()`` 与 ``snapshot()`` 只拍**一次**快照 ——
    先开自己的试探句柄 → 一次 ``NtQuerySystemInformation`` → 同一个 buffer 里既反查
    「File 类型值」，又筛出微信 PID 的候选句柄。实测本机 T0 ≈ 0.3~0.45 s（两次快照的写法约 1.6 s）。

    降级（控制方提示 2）：File 类型值一律**探测**得到（不写死；本机是 0x002A000000120089，
    控制方参考实现里的「42」是它的高字节），拿不到就不过滤类型；即使过滤由于类型值**过紧**
    而漏掉了真正的 db 句柄，也会在**剩余预算内**用全句柄再试一次 —— 宁可多查几次也不静默漏报。
    """
    if not pids:
        return {}, False
    try:
        timeout_s = max(0.0, float(timeout_s))
    except (TypeError, ValueError):
        timeout_s = 0.5
    deadline = time.monotonic() + timeout_s

    own_handles = []
    try:
        # 先开自己的试探句柄，再做快照（否则句柄不在快照里，拿不到 File 类型值）
        own_handles = _open_probe_handles()
        snapshot = _snapshot_handles()
        entry_size = _pick_entry_size(snapshot)
        entries = list(_iter_handle_entries(snapshot, entry_size))
    except Exception as e:
        for h in own_handles:
            _close_handle(h)
        _note(errors, 't0: 句柄快照失败: %s' % e)
        return {}, False

    try:
        type_mask, type_target = _file_type_key(snapshot, own_handles, entry_size)
    except Exception as e:
        type_mask, type_target = None, None
        _note(errors, 't0: File 对象类型值探测异常: %s' % e)
    finally:
        for h in own_handles:
            _close_handle(h)

    wanted = set(int(p) for p in pids)
    all_entries = [(pid, handle) for pid, handle, _t in entries if pid in wanted]
    if type_mask is None:
        filtered = all_entries
        _note(errors, 't0(降级): 未取到 File 对象类型值，退化为扫描该进程全部句柄'
              if all_entries else 't0(降级): 未取到 File 对象类型值')
    else:
        filtered = [(pid, handle) for pid, handle, t in entries
                    if pid in wanted and (t & type_mask) == type_target]

    out, timed_out = _map_handles_to_roots(filtered, workers, timeout_s, errors)

    # 结果驱动的兜底重试：只有"过滤命中 0 个 db_storage"且预算还够时才做
    if (not out and type_mask is not None and all_entries
            and len(all_entries) != len(filtered)):
        remaining = deadline - time.monotonic()
        if remaining > 0.15:
            _note(errors, 't0(降级): 按 File 类型值过滤未命中 db_storage'
                          '（候选 %d 个/全部 %d 个），改用全句柄扫描重试'
                  % (len(filtered), len(all_entries)))
            retry, retry_timed_out = _map_handles_to_roots(all_entries, workers,
                                                           remaining, errors)
            out = retry or out
            timed_out = timed_out or retry_timed_out
        else:
            _note(errors, 't0(降级): 类型过滤未命中，但时间预算不足，未做全句柄重试')
    return out, timed_out


def dirs_in_use_by_wechat(errors=None, timeout_s=0.5, workers=4):
    """T0 对外入口：``{db_storage 根路径: sorted(PIDs)}``；失败返回 ``{}`` 并记原因。"""
    try:
        pids = wechat_pids(errors=errors)
        if not pids:
            return {}
        out, _timed_out = _scan_in_use(pids, errors=errors, timeout_s=timeout_s,
                                       workers=workers)
        return out
    except Exception as e:      # 兜底：绝不抛
        _note(errors, 't0: 句柄归属探测异常: %s' % e)
        return {}


# ---------------------------------------------------------------------------
#  T2：最近写入时间
# ---------------------------------------------------------------------------
def _shm_wal_files(dir_path):
    msg_dir = os.path.join(str(dir_path), 'message')
    try:
        names = os.listdir(msg_dir)
    except (OSError, TypeError, ValueError):
        return []
    out = []
    for name in names:
        low = name.lower()
        if low.endswith('.db-shm') or low.endswith('.db-wal'):
            out.append(os.path.join(msg_dir, name))
    return out


def last_write_minutes(dir_path):
    """``message/*.db-shm|*.db-wal`` 的最新 mtime 距今多少分钟；没有则 ``None``。绝不抛。"""
    if not dir_path:
        return None
    newest = None
    for f in _shm_wal_files(dir_path):
        try:
            m = os.path.getmtime(f)
        except OSError:
            continue
        if newest is None or m > newest:
            newest = m
    if newest is None:
        return None
    return max(0.0, (time.time() - newest) / 60.0)


# ---------------------------------------------------------------------------
#  T1 候选文件
# ---------------------------------------------------------------------------
def _t1_candidate_files(db_path, limit=2):
    """T1 只探测少量最可能被持有的文件：``message/message_*.db`` + ``contact/contact.db``。"""
    out = []
    msg_dir = os.path.join(str(db_path), 'message')
    try:
        names = sorted(n for n in os.listdir(msg_dir)
                       if n.lower().startswith('message_') and n.lower().endswith('.db'))
    except (OSError, TypeError, ValueError):
        names = []
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = 0
    for name in names[:limit]:
        out.append(os.path.join(msg_dir, name))
    contact = os.path.join(str(db_path), 'contact', 'contact.db')
    try:
        if os.path.isfile(contact):
            out.append(contact)
    except (OSError, TypeError, ValueError):
        pass
    return out


def _probe_locked(dirs, probe_files_per_dir, deadline, errors):
    """逐个目录做 T1，返回 ``({normkey: True|False|None}, 探测文件数)``。"""
    locked = {}
    probed = 0
    out_of_budget = False
    for entry in dirs:
        db_path = entry.get('db_path') if isinstance(entry, dict) else None
        if not db_path:
            continue
        verdict = None
        for f in _t1_candidate_files(db_path, probe_files_per_dir):
            if time.perf_counter() > deadline:
                out_of_budget = True
                break
            try:
                verdict_file = is_locked(f)
            except Exception:
                verdict_file = None
            probed += 1
            if verdict_file is True:
                verdict = True
                break
            if verdict_file is False and verdict is None:
                verdict = False          # 打开成功 ⇒ 该文件没被占用
        locked[normkey(db_path)] = verdict
        if out_of_budget:
            break
    if out_of_budget:
        _note(errors, 't1: 时间预算用尽，剩余目录未做占用探测')
    return locked, probed


# ---------------------------------------------------------------------------
#  信号汇总
# ---------------------------------------------------------------------------
@dataclass
class Probe:
    """T0/T1/T2 的原始信号（可注入：`rank_dirs(..., signals=Probe(...))`）。"""
    pids: list = field(default_factory=list)
    wechat_running: bool = False
    in_use: dict = field(default_factory=dict)          # {db_storage 根: [pid...]}
    locked: dict = field(default_factory=dict)          # {normkey(db_path): True|False|None}
    last_write_min: dict = field(default_factory=dict)  # {normkey(db_path): float|None}
    t0_ok: bool = True
    t0_elapsed_ms: float = 0.0
    t0_hits: int = 0
    t1_probed: int = 0
    t2_probed: int = 0
    errors: list = field(default_factory=list)

    def as_dict(self):
        return {
            't0_ok': bool(self.t0_ok),
            't0_elapsed_ms': float(self.t0_elapsed_ms),
            't0_hits': int(self.t0_hits),
            't1_probed': int(self.t1_probed),
            't2_probed': int(self.t2_probed),
            'wechat_running': bool(self.wechat_running),
            'wechat_pids': [int(p) for p in self.pids],
            'errors': [str(e) for e in self.errors],
        }


def _wechat_running_fallback(errors=None):
    """进程枚举失败时才用的兜底（tasklist）；正常路径不用，避免额外子进程开销。"""
    try:
        from engine.utils import is_wechat_running
        return bool(is_wechat_running())
    except Exception as e:
        _note(errors, 't0(降级): tasklist 兜底判定微信进程失败: %s' % e)
        return False


def collect_signals(dirs, config_dir=None, budget_s=1.0, workers=4,
                    probe_files_per_dir=2):
    """采集 T0/T1/T2 信号。**绝不抛**；失败原因全部落在 ``Probe.errors``。"""
    t_start = time.perf_counter()
    errors = []
    dirs = [d for d in (dirs or []) if isinstance(d, dict)]

    pids = wechat_pids(errors=errors)
    enum_failed = any(e.startswith('t0: 进程枚举') for e in errors)
    wechat_running = bool(pids)
    if not wechat_running and enum_failed:
        wechat_running = _wechat_running_fallback(errors=errors)
        if wechat_running:
            _note(errors, 't0(降级): 进程枚举失败，已改用 tasklist 判定微信在运行')

    # ---- T0 ----
    t0_start = time.perf_counter()
    in_use, timed_out = {}, False
    if pids:
        t0_timeout = max(0.2, float(budget_s) * 0.6)
        try:
            in_use, timed_out = _scan_in_use(pids, errors=errors,
                                             timeout_s=t0_timeout, workers=workers)
        except Exception as e:      # _scan_in_use 自己不抛，这里是二次兜底
            in_use, timed_out = {}, False
            _note(errors, 't0: 句柄归属探测异常: %s' % e)
    probe = Probe(pids=list(pids), wechat_running=bool(wechat_running),
                  in_use=dict(in_use),
                  t0_elapsed_ms=round((time.perf_counter() - t0_start) * 1000.0, 1),
                  t0_hits=len(in_use))
    probe.t0_ok = (not timed_out) and not any(e.startswith('t0: ') for e in errors)

    # ---- T1（只在微信在运行时做；tier=locked 的前提就是"微信在运行"）----
    if probe.wechat_running:
        budget_left = float(budget_s) - (time.perf_counter() - t_start)
        if budget_left <= 0.05:
            _note(errors, 't1: 跳过占用探测（%.0f ms 时间预算已用尽）'
                  % (float(budget_s) * 1000.0))
        else:
            deadline = t_start + float(budget_s)
            locked, probed = _probe_locked(dirs, probe_files_per_dir, deadline, errors)
            probe.locked = locked
            probe.t1_probed = probed

    # ---- T2（很便宜：每个目录几次 stat）----
    last = {}
    for entry in dirs:
        db_path = entry.get('db_path')
        if not db_path:
            continue
        try:
            last[normkey(db_path)] = last_write_minutes(db_path)
        except Exception as e:
            last[normkey(db_path)] = None
            _note(errors, 't2: 最近写入时间读取失败: %s' % e)
    probe.last_write_min = last
    probe.t2_probed = len(last)
    probe.errors = errors
    return probe


# ---------------------------------------------------------------------------
#  排序 + 标注
# ---------------------------------------------------------------------------
def _index_by_normkey(mapping):
    out = {}
    for k, v in (mapping or {}).items():
        out[normkey(k)] = v
    return out


def _sort_key(d):
    lwm = d.get('last_write_min')
    try:
        size = float(d.get('size_mb') or 0.0)
    except (TypeError, ValueError):
        size = 0.0
    return (TIER_ORDER.get(d.get('tier'), 99),
            0 if lwm is not None else 1,
            float(lwm) if lwm is not None else 0.0,
            -size,
            str(d.get('db_path') or ''))


def apply_idle_defaults(dirs):
    """降级用：给每个目录补上 rank 字段（统一 idle），保证前端字段永远齐全。"""
    out = []
    for d in (dirs or []):
        if not isinstance(d, dict):
            continue
        d['tier'] = 'idle'
        d['reason'] = REASON_IDLE
        d['recommended'] = False
        d['pids'] = []
        d['active'] = False
        d['last_write_min'] = None
        out.append(d)
    return out


def degraded_probe(message):
    """降级用的 probe 字典（保留原因，不静默）。"""
    return Probe(t0_ok=False, errors=[str(message)]).as_dict()


def rank_and_probe(dirs, config_dir=None, signals=None, budget_s=1.0, workers=4,
                   probe_files_per_dir=2):
    """加字段 + 排序，返回 ``(dirs, recommended_path, probe)``。绝不抛。"""
    items = [d for d in (dirs or []) if isinstance(d, dict)]
    if signals is None:
        signals = collect_signals(items, config_dir=config_dir, budget_s=budget_s,
                                  workers=workers,
                                  probe_files_per_dir=probe_files_per_dir)
    in_use = _index_by_normkey(getattr(signals, 'in_use', None))
    locked = _index_by_normkey(getattr(signals, 'locked', None))
    last = _index_by_normkey(getattr(signals, 'last_write_min', None))
    running = bool(getattr(signals, 'wechat_running', False))
    cfg_key = normkey(config_dir) if config_dir else ''

    for d in items:
        key = normkey(d.get('db_path'))
        pids = []
        for p in (in_use.get(key) or []):
            try:
                pids.append(int(p))
            except (TypeError, ValueError):
                continue
        pids = sorted(set(pids))
        lwm = last.get(key)
        try:
            lwm = None if lwm is None else float(lwm)
        except (TypeError, ValueError):
            lwm = None

        if pids:
            tier, reason = 'in_use', REASON_IN_USE.format(
                pids=PID_SEP.join(str(p) for p in pids))
        elif locked.get(key) is True and running:
            tier, reason = 'locked', REASON_LOCKED
        elif lwm is not None and lwm <= RECENT_MINUTES:
            tier, reason = 'recent', REASON_RECENT.format(n='%.1f' % lwm)
        elif cfg_key and key == cfg_key:
            tier, reason = 'config', REASON_CONFIG
        else:
            tier, reason = 'idle', REASON_IDLE

        d['tier'] = tier
        d['reason'] = reason
        d['pids'] = pids
        d['active'] = tier in ('in_use', 'locked')
        d['recommended'] = False
        d['last_write_min'] = lwm

    items.sort(key=_sort_key)
    recommended = ''
    if items:
        items[0]['recommended'] = True
        recommended = items[0].get('db_path') or ''
    return items, recommended, signals


def rank_dirs(dirs, config_dir=None, signals=None, budget_s=1.0, workers=4,
              probe_files_per_dir=2):
    """对外主入口：返回 ``(排序后的 dirs, recommended_path)``。

    ``signals`` 可注入（测试/复用已采集的信号）；不传则内部 ``collect_signals``。
    """
    ranked, recommended, _probe = rank_and_probe(
        dirs, config_dir=config_dir, signals=signals, budget_s=budget_s,
        workers=workers, probe_files_per_dir=probe_files_per_dir)
    return ranked, recommended
