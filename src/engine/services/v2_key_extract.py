"""Extract V2 image AES keys from WeChat (Weixin.exe) process memory.

WeChat 4.x V2 format uses per-image AES-128-ECB keys that are ONLY stored
in process memory while images are being viewed. They are never persisted to disk.

WeChat stores these keys as 32-character hex strings in memory (bounded by
non-alphanumeric characters). This module uses regex scanning to find them —
the same approach used by ZedeX/weixin-decrypte-script.

Key verification: a correct key decrypts the first AES block into a valid
image header (JPEG: FF D8 FF, PNG: 89 50 4E 47, GIF: 47 49 46 38, etc.).

Usage:
    from engine.services.v2_key_extract import find_keys_for_files, is_wechat_running
    found = find_keys_for_files(decrypted_dir, wxid, [md5_val])
"""

import ctypes
import ctypes.wintypes as wt
import os
import re
import sqlite3
import struct
import sys

kernel32 = ctypes.windll.kernel32

# ---------------------------------------------------------------------------
# Win32 helpers
# ---------------------------------------------------------------------------

MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100
PAGE_READWRITE = 0x04
PAGE_WRITECOPY = 0x08
PAGE_EXECUTE_READWRITE = 0x40
PAGE_EXECUTE_WRITECOPY = 0x80

_RW_FLAGS = (PAGE_READWRITE | PAGE_WRITECOPY |
             PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY)

# Regex: 32 alphanumeric chars bounded by non-alphanumeric chars
# (same as ZedeX/weixin-decrypte-script RE_KEY32)
_RE_KEY32 = re.compile(rb'(?<![a-zA-Z0-9])[a-zA-Z0-9]{32}(?![a-zA-Z0-9])')
_RE_KEY16 = re.compile(rb'(?<![a-zA-Z0-9])[a-zA-Z0-9]{16}(?![a-zA-Z0-9])')


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


def _get_wechat_pids():
    """Return list of Weixin.exe PIDs."""
    import subprocess
    try:
        result = subprocess.run(
            ['tasklist.exe', '/FI', 'IMAGENAME eq Weixin.exe', '/FO', 'CSV', '/NH'],
            capture_output=True, text=True, timeout=10
        )
        pids = []
        for line in result.stdout.strip().split('\n'):
            if 'Weixin.exe' in line:
                parts = line.strip('"').split('","')
                if len(parts) >= 2:
                    try:
                        pids.append(int(parts[1]))
                    except ValueError:
                        pass
        return pids
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Image verification (same as ZedeX try_key)
# ---------------------------------------------------------------------------

def _is_valid_image_header(first_bytes):
    """Check if decrypted data starts with a known image header (strong check)."""
    if len(first_bytes) < 8:
        return False

    # JPEG: FF D8 FF
    if first_bytes[:3] == b'\xff\xd8\xff':
        return True

    # PNG: 89 50 4E 47 0D 0A 1A 0A
    if first_bytes[:8] == b'\x89PNG\r\n\x1a\n':
        return True

    # GIF: 47 49 46 38 (37 61 | 39 61)
    if first_bytes[:6] in (b'GIF87a', b'GIF89a'):
        return True

    # WebP: 52 49 46 46 ... 57 45 42 50
    if first_bytes[:4] == b'RIFF' and len(first_bytes) >= 12 and first_bytes[8:12] == b'WEBP':
        return True

    # WeChat WxGF: 77 78 67 66
    if first_bytes[:4] == b'wxgf':
        return True

    return False


def _try_key(key_bytes, ciphertext):
    """Test a key candidate against ciphertext (same algorithm as ZedeX).

    Args:
        key_bytes: raw bytes from memory (ASCII string)
        ciphertext: 32 bytes of AES ciphertext from file

    Returns:
        Image format string ('JPEG', 'PNG', etc.) or None.
    """
    try:
        from Crypto.Cipher import AES
    except ImportError:
        return None

    try:
        # ZedeX uses the full key_bytes directly (32 bytes → AES-256)
        cipher = AES.new(key_bytes, AES.MODE_ECB)
        decrypted = cipher.decrypt(ciphertext[:16])
    except Exception:
        return None

    if _is_valid_image_header(decrypted):
        if decrypted[:3] == b'\xff\xd8\xff':
            return 'JPEG'
        if decrypted[:4] == b'\x89PNG':
            return 'PNG'
        if decrypted[:4] == b'RIFF':
            return 'WEBP'
        if decrypted[:4] == b'wxgf':
            return 'WXGF'
        if decrypted[:3] == b'GIF':
            return 'GIF'
    return None


# ---------------------------------------------------------------------------
# HardLink path resolution (inline, same as media.py)
# ---------------------------------------------------------------------------

def _get_base_storage(decrypted_dir):
    """Get WeChat file storage root from hardlink.db db_info."""
    hardlink_db = os.path.join(decrypted_dir, "hardlink", "hardlink.db")
    if not os.path.isfile(hardlink_db):
        return None
    try:
        conn = sqlite3.connect(hardlink_db)
        row = conn.execute("SELECT ValueStdStr FROM db_info WHERE Key='uuid'").fetchone()
        conn.close()
        if row and row[0]:
            parts = str(row[0]).split('_', 2)
            if len(parts) >= 3:
                storage_path = parts[-1]
                if os.path.isdir(storage_path):
                    return storage_path
    except sqlite3.Error:
        pass
    return None


def _resolve_hardlink_path(decrypted_dir, media_info, wxid):
    """Resolve a media md5 to an absolute file path.

    Tries both the md5 column (CDN md5) and file_name LIKE match (file md5)
    since the hardlink DB's md5 column stores CDN md5, not the local file's md5.
    """
    if not media_info:
        return None
    md5 = media_info.get('md5', '')
    if not md5 or len(md5) != 32:
        return None

    hardlink_db = os.path.join(decrypted_dir, "hardlink", "hardlink.db")
    if not os.path.isfile(hardlink_db):
        return None

    try:
        conn = sqlite3.connect(hardlink_db)
        # First try direct md5 match (works when md5 is the CDN md5 from XML)
        row = conn.execute(
            "SELECT file_name, dir1, dir2 FROM image_hardlink_info_v4 WHERE md5=?",
            (md5,)
        ).fetchone()
        # If direct match fails, try file_name LIKE (file md5 is the .dat file name prefix)
        if not row:
            row = conn.execute(
                "SELECT file_name, dir1, dir2 FROM image_hardlink_info_v4 "
                "WHERE file_name LIKE ? LIMIT 1",
                (md5 + '%',)
            ).fetchone()
        if not row:
            conn.close()
            return None

        file_name, dir1, dir2 = row
        d2 = conn.execute("SELECT username FROM dir2id WHERE rowid=?", (dir2,)).fetchone()
        dir2_name = d2[0] if d2 else None
        d1 = conn.execute("SELECT username FROM dir2id WHERE rowid=?", (dir1,)).fetchone()
        dir1_name = d1[0] if d1 else None
        conn.close()

        if dir1_name and dir2_name:
            rel_path = f'msg/attach/{dir1_name}/{dir2_name}/Img/{file_name}'
            rel_path = rel_path.replace('/', os.sep)
            base = _get_base_storage(decrypted_dir) or 'D:\\xwechat_files'
            for sr in [base, 'D:\\xwechat_files', 'C:\\xwechat_files']:
                if os.path.isdir(sr) and wxid:
                    candidate = os.path.join(sr, wxid, rel_path)
                    if os.path.isfile(candidate):
                        return candidate
    except sqlite3.Error:
        pass
    return None


# ---------------------------------------------------------------------------
# Memory scanning
# ---------------------------------------------------------------------------

def _scan_memory_for_aes_keys(h_process, ciphertext, print_fn=None):
    """Scan process memory for AES keys using regex (ZedeX algorithm).

    Searches for 32-char and 16-char alphanumeric strings in readable
    committed memory regions, testing each as an AES key.

    Args:
        h_process: OpenProcess handle
        ciphertext: 16 bytes of AES ciphertext from the file
        print_fn: optional logging function

    Returns:
        First 16 chars of the found key string, or None
    """
    import time
    if print_fn is None:
        print_fn = lambda *args, **kwargs: None

    # Enumerate regions
    mbi = MEMORY_BASIC_INFORMATION()
    all_regions = []
    rw_regions = []

    address = 0
    while address < 0x7FFFFFFFFFFF:
        result = kernel32.VirtualQueryEx(
            h_process, ctypes.c_void_p(address),
            ctypes.byref(mbi), ctypes.sizeof(mbi)
        )
        if result == 0:
            break
        if (mbi.State == MEM_COMMIT and
            mbi.Protect != PAGE_NOACCESS and
            (mbi.Protect & PAGE_GUARD) == 0 and
            mbi.RegionSize <= 50 * 1024 * 1024):
            region = (mbi.BaseAddress, mbi.RegionSize, mbi.Protect)
            all_regions.append(region)
            if (mbi.Protect & _RW_FLAGS) != 0:
                rw_regions.append(region)
        next_addr = address + mbi.RegionSize
        if next_addr <= address:
            break
        address = next_addr

    rw_mb = sum(r[1] for r in rw_regions) / 1024 / 1024
    all_mb = sum(r[1] for r in all_regions) / 1024 / 1024
    print_fn(f"[v2_key] RW: {len(rw_regions)} regions ({rw_mb:.0f}MB), "
             f"Total: {len(all_regions)} ({all_mb:.0f}MB)")

    # Phase 1: scan RW regions first (more likely to contain keys)
    for phase_name, regions in [("Phase 1 (RW)", rw_regions),
                                 ("Phase 2 (all)", all_regions)]:
        if phase_name == "Phase 2 (all)":
            rw_set = set((r[0], r[1]) for r in rw_regions)
            regions = [r for r in all_regions if (r[0], r[1]) not in rw_set]

        candidates_32 = 0
        candidates_16 = 0
        t0 = time.time()

        for idx, (base_addr, region_size, _protect) in enumerate(regions):
            if idx % 200 == 0:
                elapsed = time.time() - t0
                print_fn(f"  [{phase_name}] {idx}/{len(regions)} ({elapsed:.1f}s, "
                         f"32c:{candidates_32} 16c:{candidates_16})",
                         end='', flush=True)

            buf = ctypes.create_string_buffer(region_size)
            bytes_read = ctypes.c_size_t(0)
            ok = kernel32.ReadProcessMemory(
                h_process, ctypes.c_void_p(base_addr),
                buf, region_size, ctypes.byref(bytes_read)
            )
            if not ok or bytes_read.value < 32:
                continue
            data = buf.raw[:bytes_read.value]

            # Search for 32-char hex strings
            for m in _RE_KEY32.finditer(data):
                key_bytes = m.group()
                candidates_32 += 1

                # Try multiple key formats (WeChat may store keys differently):
                # 1. Raw ASCII (first 16 bytes) -> AES-128
                fmt = _try_key(key_bytes[:16], ciphertext)
                if fmt:
                    key_str = key_bytes.decode('ascii')
                    print_fn(f"\n[v2_key] Found AES key (32-char ASCII-AES128)! -> {fmt}")
                    print_fn(f"  Full: {key_str}")
                    return key_str

                # 2. Raw ASCII (full 32 bytes) -> AES-256 (ZedeX approach)
                fmt = _try_key(key_bytes, ciphertext)
                if fmt:
                    key_str = key_bytes.decode('ascii')
                    print_fn(f"\n[v2_key] Found AES key (32-char ASCII-AES256)! -> {fmt}")
                    print_fn(f"  Full: {key_str}")
                    return key_str

                # 3. Hex-decoded -> AES-128 (most likely correct format)
                try:
                    decoded = bytes.fromhex(key_bytes.decode('ascii'))
                    fmt = _try_key(decoded, ciphertext)
                    if fmt:
                        key_str = key_bytes.decode('ascii')
                        print_fn(f"\n[v2_key] Found AES key (32-char hex-decoded)! -> {fmt}")
                        print_fn(f"  Full: {key_str}")
                        return key_str
                except ValueError:
                    pass

            # Search for 16-char strings
            for m in _RE_KEY16.finditer(data):
                key_bytes = m.group()
                candidates_16 += 1

                # Try raw ASCII -> AES-128
                fmt = _try_key(key_bytes, ciphertext)
                if fmt:
                    key_str = key_bytes.decode('ascii')
                    print_fn(f"\n[v2_key] Found AES key (16-char ASCII)! -> {fmt}")
                    print_fn(f"  Key: {key_str}")
                    return key_str

                # Try hex-decoded (e.g., "a1b2c3d4e5f6a7b8" -> 8 bytes, too short for AES
                # but the original 16 ASCII chars could be half a 32-char key)
                try:
                    key_str = key_bytes.decode('ascii')
                    if all(c in '0123456789abcdefABCDEF' for c in key_str):
                        decoded = bytes.fromhex(key_str)
                        if len(decoded) == 16:  # 32 hex chars -> 16 bytes
                            fmt = _try_key(decoded, ciphertext)
                            if fmt:
                                print_fn(f"\n[v2_key] Found AES key (hex-decoded from 32-char)! -> {fmt}")
                                return key_str
                except ValueError:
                    pass

        elapsed = time.time() - t0
        total = candidates_32 + candidates_16
        print_fn(f"\n  [{phase_name}] Done: {total} candidates ({elapsed:.1f}s)")

    return None


def _scan_near_v2_headers(h_process, ciphertext, print_fn=None):
    """Scan memory near V2 header patterns for AES keys (raw binary or ASCII hex).

    wx_key's analysis reveals that WeChat stores the V2 AES key as raw bytes in
    memory near the V2 header. The previous printable-ASCII-only filter was
    discarding genuine raw-binary keys. This version uses a sliding 16-byte
    window over a 512-byte buffer around each V2 header and tests every
    candidate — no character-set filtering.

    Also tests hex-decoded interpretation: if the 16-byte window looks like
    printable hex, we hex-decode to 16 raw bytes (32 hex → 16 bytes) and test
    that too, covering the case where WeChat stores hex-encoded keys in memory.

    Strategy (ordered by likelihood, from wx_key analysis):
      1. Raw 16-byte windows near V2 headers (step=4 for speed)
      2. Hex-decoded windows (32-char hex → 16 raw bytes)
      3. Full 32-byte windows near V2 headers (step=8)

    Returns:
        Key bytes (16 raw bytes), or None
    """
    import time
    if print_fn is None:
        print_fn = lambda *args, **kwargs: None

    V2_MAGIC = b'\x07\x08\x56\x32\x08\x07'
    WINDOW_HALF = 256
    t0 = time.time()

    # Find all V2 magic occurrences in committed readable memory
    mbi = MEMORY_BASIC_INFORMATION()
    v2_addrs = []
    address = 0
    while address < 0x7FFFFFFFFFFF:
        result = kernel32.VirtualQueryEx(
            h_process, ctypes.c_void_p(address),
            ctypes.byref(mbi), ctypes.sizeof(mbi)
        )
        if result == 0:
            break
        if (mbi.State == MEM_COMMIT and
            mbi.Protect != PAGE_NOACCESS and
            (mbi.Protect & PAGE_GUARD) == 0 and
            0 < mbi.RegionSize <= 50 * 1024 * 1024):
            region_base = ctypes.cast(mbi.BaseAddress, ctypes.c_void_p).value
            if region_base is None:
                next_addr = address + mbi.RegionSize
                address = next_addr
                continue
            region_size = mbi.RegionSize
            try:
                buf = ctypes.create_string_buffer(region_size)
            except (OverflowError, MemoryError):
                next_addr = address + mbi.RegionSize
                address = next_addr
                continue
            bytes_read = ctypes.c_size_t(0)
            ok = kernel32.ReadProcessMemory(
                h_process, ctypes.c_void_p(region_base),
                buf, region_size, ctypes.byref(bytes_read)
            )
            if ok and bytes_read.value > 0:
                data = buf.raw[:bytes_read.value]
                idx = data.find(V2_MAGIC)
                while idx != -1:
                    v2_addrs.append(region_base + idx)
                    idx = data.find(V2_MAGIC, idx + 1)
        next_addr = address + mbi.RegionSize
        if next_addr <= address:
            break
        address = next_addr

    elapsed = time.time() - t0
    print_fn(f"[v2_key:headers] Found {len(v2_addrs)} V2 header matches in {elapsed:.1f}s")

    if not v2_addrs:
        return None

    # For each V2 header, read the full ±256 byte buffer once,
    # then slide a 16-byte window across it testing every candidate.
    tested_16 = 0
    tested_32 = 0
    tested_hex = 0

    for addr in v2_addrs:
        buf_start = addr - WINDOW_HALF
        buf_size = WINDOW_HALF * 2
        try:
            buf = ctypes.create_string_buffer(buf_size)
        except (OverflowError, MemoryError):
            continue
        bytes_read = ctypes.c_size_t(0)
        ok = kernel32.ReadProcessMemory(
            h_process, ctypes.c_void_p(buf_start),
            buf, buf_size, ctypes.byref(bytes_read)
        )
        if not ok or bytes_read.value < 16:
            continue
        data = buf.raw[:bytes_read.value]

        # Pass 1: raw 16-byte sliding window, step=4
        for i in range(0, len(data) - 16, 4):
            candidate = data[i:i + 16]
            tested_16 += 1
            fmt = _try_key(candidate, ciphertext)
            if fmt:
                print_fn(f"\n[v2_key:headers] Found raw AES key! -> {fmt}")
                print_fn(f"  Key (hex): {candidate.hex()}")
                return candidate

        # Pass 2: hex-decoded interpretation
        # If a 32-byte window looks like ASCII hex, decode to 16 raw bytes
        for i in range(0, len(data) - 32, 16):
            chunk = data[i:i + 32]
            try:
                hex_str = chunk.decode('ascii')
            except UnicodeDecodeError:
                continue
            if not all(c in '0123456789abcdefABCDEF' for c in hex_str):
                continue
            try:
                decoded = bytes.fromhex(hex_str)
            except ValueError:
                continue
            tested_hex += 1
            fmt = _try_key(decoded, ciphertext)
            if fmt:
                print_fn(f"\n[v2_key:headers] Found hex-encoded AES key! -> {fmt}")
                print_fn(f"  Key (hex): {decoded.hex()}")
                return decoded

        # Pass 3: raw 32-byte sliding window, step=8
        # (wx_key captures 32 raw bytes — the first 16 may be the AES key)
        for i in range(0, len(data) - 32, 8):
            candidate32 = data[i:i + 32]
            tested_32 += 1
            fmt = _try_key(candidate32[:16], ciphertext)
            if fmt:
                key16 = candidate32[:16]
                print_fn(f"\n[v2_key:headers] Found AES key in 32-byte block! -> {fmt}")
                print_fn(f"  Key (hex): {key16.hex()}")
                return key16

    print_fn(f"[v2_key:headers] Tested {tested_16} raw-16 + {tested_hex} hex-decode"
             f" + {tested_32} raw-32 candidates — no key found")
    return None


# ---------------------------------------------------------------------------
# wx_key pattern-based function location
# ---------------------------------------------------------------------------

# Version-specific byte patterns from wx_key/remote_scanner.cpp
# These locate the image decryption function in Weixin.dll.
# offset: applied to the matched pattern address to get the hook/key location.
_WX_KEY_PATTERNS = [
    # >4.1.6.14
    {
        'min_ver': (4, 1, 6, 15),
        'max_ver': (99, 0, 0, 0),
        'pattern': bytes([
            0x24, 0x50, 0x48, 0xC7, 0x45, 0x00, 0xFE, 0xFF,
            0xFF, 0xFF, 0x44, 0x89, 0xCF, 0x44, 0x89, 0xC3,
            0x49, 0x89, 0xD6, 0x48, 0x89, 0xCE, 0x48, 0x89,
        ]),
        'mask': 'xxxxxxxxxxxxxxxxxxxxxxxx',  # all exact
        'offset': -3,
    },
    # >=4.1.4 && <=4.1.6.14
    {
        'min_ver': (4, 1, 4, 0),
        'max_ver': (4, 1, 6, 14),
        'pattern': bytes([
            0x24, 0x08, 0x48, 0x89, 0x6C, 0x24, 0x10, 0x48,
            0x89, 0x74, 0x00, 0x18, 0x48, 0x89, 0x7C, 0x00,
            0x20, 0x41, 0x56, 0x48, 0x83, 0xEC, 0x50, 0x41,
        ]),
        'mask': 'xxxxxxxxxx?xxxx?xxxxxxxx',  # wildcards at pos 10, 15
        'offset': -3,
    },
    # <4.1.4 (4.0.x, 4.1.0–4.1.3)
    {
        'min_ver': (4, 0, 0, 0),
        'max_ver': (4, 1, 3, 9999),
        'pattern': bytes([
            0x24, 0x50, 0x48, 0xC7, 0x45, 0x00, 0xFE, 0xFF,
            0xFF, 0xFF, 0x44, 0x89, 0xCF, 0x44, 0x89, 0xC3,
            0x49, 0x89, 0xD6, 0x48, 0x89, 0xCE, 0x48, 0x89,
        ]),
        'mask': 'xxxxxxxxxxxxxxxxxxxxxxxx',  # all exact, same as >4.1.6.14
        'offset': -0xF,  # different offset for older versions
    },
]


def _get_wechat_version(h_process):
    """Extract WeChat (Weixin.dll) version from the target process.

    Returns a (major, minor, build, revision) tuple, or None.
    """
    import time

    # Find Weixin.dll base address
    try:
        psapi = ctypes.windll.psapi
    except Exception:
        psapi = ctypes.windll.kernel32

    hModules = (ctypes.c_ulonglong * 1024)()
    cbNeeded = wt.DWORD(0)
    # Use K32EnumProcessModulesEx to get all modules
    try:
        k32 = ctypes.windll.kernel32
        ok = k32.K32EnumProcessModulesEx(h_process, ctypes.byref(hModules),
                                          ctypes.sizeof(hModules),
                                          ctypes.byref(cbNeeded), 0x03)
    except Exception:
        return None
    if not ok:
        return None

    n_modules = cbNeeded.value // ctypes.sizeof(ctypes.c_ulonglong)
    for i in range(min(n_modules, 1024)):
        mod_handle = ctypes.c_void_p(hModules[i])
        name_buf = ctypes.create_unicode_buffer(260)
        k32.K32GetModuleBaseNameW(h_process, mod_handle, name_buf, 260)
        mod_name = name_buf.value.lower()
        if mod_name == 'weixin.dll':
            # Get full path for version info
            path_buf = ctypes.create_unicode_buffer(260)
            k32.K32GetModuleFileNameExW(h_process, mod_handle, path_buf, 260)
            dll_path = path_buf.value
            if not dll_path:
                continue

            # Get version info size
            import ctypes.wintypes as _wt
            dummy = wt.DWORD(0)
            size = ctypes.windll.version.GetFileVersionInfoSizeW(dll_path, ctypes.byref(dummy))
            if size == 0:
                continue

            ver_buf = ctypes.create_string_buffer(size)
            ok = ctypes.windll.version.GetFileVersionInfoW(dll_path, 0, size, ver_buf)
            if not ok:
                continue

            fixed_info = ctypes.c_void_p()
            fixed_len = wt.UINT(0)
            ok = ctypes.windll.version.VerQueryValueW(
                ver_buf, '\\', ctypes.byref(fixed_info), ctypes.byref(fixed_len))
            if not ok:
                continue

            # VS_FIXEDFILEINFO: dwProductVersionMS (major.minor), dwProductVersionLS (build.revision)
            class VS_FIXEDFILEINFO(ctypes.Structure):
                _fields_ = [
                    ("dwSignature", wt.DWORD),
                    ("dwStrucVersion", wt.DWORD),
                    ("dwFileVersionMS", wt.DWORD),
                    ("dwFileVersionLS", wt.DWORD),
                    ("dwProductVersionMS", wt.DWORD),
                    ("dwProductVersionLS", wt.DWORD),
                ]
            info = ctypes.cast(fixed_info, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
            ms_val = info.dwProductVersionMS
            ls_val = info.dwProductVersionLS
            major = ms_val >> 16
            minor = ms_val & 0xFFFF
            build = ls_val >> 16
            revision = ls_val & 0xFFFF
            return (major, minor, build, revision)

    return None


def _match_pattern(data, pattern_bytes, mask_str, start=0):
    """Find pattern in data using mask. Returns offset or -1."""
    plen = len(pattern_bytes)
    if len(data) - start < plen:
        return -1
    for i in range(start, len(data) - plen + 1):
        match = True
        for j in range(plen):
            if mask_str[j] == '?':
                continue
            if data[i + j] != pattern_bytes[j]:
                match = False
                break
        if match:
            return i
    return -1


def _scan_wx_key_pattern(h_process, ciphertext, print_fn=None):
    """Use wx_key's version-specific patterns to locate the image decryption
    function in Weixin.dll and extract keys from nearby memory.

    wx_key hooks a function in Weixin.dll that receives/handles the 32-byte
    image key during decryption. We can't hook from Python, but we can:
    1. Locate the function using the same byte patterns
    2. Read memory around the function during active image viewing
    3. Test 16-byte/32-byte windows near the function as key candidates

    Returns:
        Key bytes (16 raw bytes), or None
    """
    import time
    if print_fn is None:
        print_fn = lambda *args, **kwargs: None

    t0 = time.time()

    # Get WeChat version
    ver = _get_wechat_version(h_process)
    if ver is None:
        print_fn("[v2_key:wx_pattern] Could not determine WeChat version")
        return None
    ver_str = '.'.join(str(v) for v in ver)
    print_fn(f"[v2_key:wx_pattern] WeChat version: {ver_str}")

    # Select matching pattern
    selected = None
    for cfg in _WX_KEY_PATTERNS:
        if (ver >= cfg['min_ver'] and ver <= cfg['max_ver']):
            selected = cfg
            break

    if selected is None:
        print_fn(f"[v2_key:wx_pattern] No pattern for version {ver_str}")
        return None

    print_fn(f"[v2_key:wx_pattern] Using pattern (offset={selected['offset']})")

    # Find Weixin.dll module
    try:
        k32 = ctypes.windll.kernel32
    except Exception:
        return None
    hModules = (ctypes.c_ulonglong * 1024)()
    cbNeeded = wt.DWORD(0)
    ok = k32.K32EnumProcessModulesEx(h_process, ctypes.byref(hModules),
                                      ctypes.sizeof(hModules),
                                      ctypes.byref(cbNeeded), 0x03)
    if not ok:
        return None

    n_modules = cbNeeded.value // ctypes.sizeof(ctypes.c_ulonglong)
    weixin_base = None
    weixin_size = 0
    for i in range(min(n_modules, 1024)):
        mod_handle = ctypes.c_void_p(hModules[i])
        name_buf = ctypes.create_unicode_buffer(260)
        k32.K32GetModuleBaseNameW(h_process, mod_handle, name_buf, 260)
        mod_name = name_buf.value.lower()
        if mod_name == 'weixin.dll':
            class MODULEINFO(ctypes.Structure):
                _fields_ = [
                    ("lpBaseOfDll", ctypes.c_void_p),
                    ("SizeOfImage", wt.DWORD),
                    ("EntryPoint", ctypes.c_void_p),
                ]
            mod_info = MODULEINFO()
            ok2 = k32.K32GetModuleInformation(h_process, mod_handle,
                                               ctypes.byref(mod_info),
                                               ctypes.sizeof(mod_info))
            if ok2:
                weixin_base = mod_info.lpBaseOfDll or None
                weixin_size = mod_info.SizeOfImage
            break

    if weixin_base is None or weixin_size == 0:
        print_fn("[v2_key:wx_pattern] Weixin.dll not found")
        return None

    print_fn(f"[v2_key:wx_pattern] Weixin.dll at 0x{weixin_base:X}, size={weixin_size / 1024 / 1024:.1f}MB")

    # Read Weixin.dll memory in chunks and search for pattern
    CHUNK = 1024 * 1024
    plen = len(selected['pattern'])
    pattern_found_at = None
    for chunk_start in range(0, weixin_size, CHUNK):
        chunk_size = min(CHUNK + plen, weixin_size - chunk_start)
        buf = ctypes.create_string_buffer(chunk_size)
        bytes_read = ctypes.c_size_t(0)
        ok = kernel32.ReadProcessMemory(
            h_process, ctypes.c_void_p(weixin_base + chunk_start),
            buf, chunk_size, ctypes.byref(bytes_read))
        if not ok or bytes_read.value < plen:
            continue
        data = buf.raw[:bytes_read.value]
        offset = _match_pattern(data, selected['pattern'], selected['mask'])
        if offset >= 0:
            pattern_found_at = weixin_base + chunk_start + offset
            break

    if pattern_found_at is None:
        print_fn("[v2_key:wx_pattern] Pattern not found in Weixin.dll")
        return None

    target_addr = pattern_found_at + selected['offset']
    print_fn(f"[v2_key:wx_pattern] Pattern at 0x{pattern_found_at:X}, target=0x{target_addr:X}")

    # Read memory around the target address (±1024 bytes) to find key candidates
    SCAN_RANGE = 1024
    buf_start = target_addr - SCAN_RANGE
    buf_size = SCAN_RANGE * 2
    try:
        buf = ctypes.create_string_buffer(buf_size)
    except (OverflowError, MemoryError):
        return None
    bytes_read = ctypes.c_size_t(0)
    ok = kernel32.ReadProcessMemory(
        h_process, ctypes.c_void_p(buf_start),
        buf, buf_size, ctypes.byref(bytes_read))
    if not ok or bytes_read.value < 16:
        return None

    data = buf.raw[:bytes_read.value]

    # Test 16-byte sliding window, step=4
    tested = 0
    for i in range(0, len(data) - 16, 4):
        candidate = data[i:i + 16]
        tested += 1
        fmt = _try_key(candidate, ciphertext)
        if fmt:
            print_fn(f"\n[v2_key:wx_pattern] Found key at target+{i - SCAN_RANGE}! -> {fmt}")
            print_fn(f"  Key (hex): {candidate.hex()}")
            return candidate

    # Also test hex-decoded 32-byte windows
    for i in range(0, len(data) - 32, 16):
        chunk = data[i:i + 32]
        try:
            hex_str = chunk.decode('ascii')
        except UnicodeDecodeError:
            continue
        if not all(c in '0123456789abcdefABCDEF' for c in hex_str):
            continue
        try:
            decoded = bytes.fromhex(hex_str)
        except ValueError:
            continue
        fmt = _try_key(decoded, ciphertext)
        if fmt:
            print_fn(f"\n[v2_key:wx_pattern] Found hex-encoded key! -> {fmt}")
            return decoded

    elapsed = time.time() - t0
    print_fn(f"[v2_key:wx_pattern] Tested {tested} candidates in {elapsed:.1f}s — no key found")
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_keys_for_files(decrypted_dir, wxid, md5_list, print_fn=None, account_xor=None):
    """Extract V2 AES keys for specific image md5s from running WeChat process.

    Searches WeChat process memory for 32-char hex strings that decrypt
    the requested files' AES blocks. This requires the images to have been
    viewed in WeChat (which loads their keys into memory).

    Args:
        decrypted_dir: path to the decrypted backup directory
        wxid: WeChat user ID (e.g., 'wxid_example12345_10e8')
        md5_list: list of XML md5 hex strings
        print_fn: optional logging function
        account_xor: 已知的账号级 XOR（``code & 0xFF``）。不传时自动采用本进程内
            已用真文件验证过的那一个（``get_derived_xor``）。

    Returns:
        FoundV2Keys: ``{md5: aes_key_ascii (16 bytes)}``，并附带 ``derived_xor``
        （账号级 XOR 真值；拿不到时为 ``None``）。它继承 ``dict``，所以
        ``found[md5]`` / ``in`` / ``len`` / ``dict(found)`` 语义与历史版本一致。
    """
    if print_fn is None:
        print_fn = lambda *args, **kwargs: None

    if not md5_list:
        return FoundV2Keys()

    # Build tasks: resolve each md5 to a file and get ciphertext
    tasks = []
    for md5_val in md5_list:
        if len(md5_val) != 32:
            continue
        media_info = {'md5': md5_val, 'media_type': 3}
        file_path = _resolve_hardlink_path(decrypted_dir, media_info, wxid)
        if not file_path or not os.path.isfile(file_path):
            continue

        try:
            with open(file_path, 'rb') as f:
                data = f.read(128)
        except OSError:
            continue

        if len(data) < 31 or data[:6] != b'\x07\x08V2\x08\x07':
            continue

        # First 16 bytes of AES section (for verification)
        aes_block = data[15:31]
        tasks.append((md5_val, file_path, aes_block))

    if not tasks:
        print_fn("[v2_key] No valid V2 files found to find keys for")
        return FoundV2Keys()

    print_fn(f"[v2_key] Searching memory for {len(tasks)} file(s)...")

    pids = _get_wechat_pids()
    if not pids:
        print_fn("[v2_key] Weixin.exe is not running")
        return FoundV2Keys()

    # 账号级 XOR 真值：调用方给的优先，其次本进程内已验证过的那一个。
    # 拿不到时保持 None —— 由调用方回退 `media._DAT_V2_DEFAULT_XOR`，绝不在这里猜。
    derived_xor = account_xor
    if derived_xor is None:
        derived_xor = get_derived_xor(decrypted_dir)
    if derived_xor is not None:
        derived_xor = int(derived_xor) & 0xFF

    found = {}
    for md5_val, file_path, aes_block in tasks:
        for pid in pids:
            print_fn(f"[v2_key] Scanning PID {pid} for md5={md5_val[:16]}...")
            access = 0x0010 | 0x0400  # PROCESS_VM_READ | PROCESS_QUERY_INFORMATION
            h_process = kernel32.OpenProcess(access, False, pid)
            if not h_process:
                continue
            try:
                # Strategy 1: Scan near V2 headers (raw binary + hex-decode windows)
                # This is the most reliable — keys are near where they're used.
                key_bytes = _scan_near_v2_headers(h_process, aes_block, print_fn)
                if key_bytes:
                    found[md5_val] = key_bytes
                    break

                # Strategy 2: wx_key pattern-based function location
                # Locate the image decryption function and read nearby memory.
                key_bytes = _scan_wx_key_pattern(h_process, aes_block, print_fn)
                if key_bytes:
                    found[md5_val] = key_bytes
                    break

                # Strategy 3: Regex hex-string scan (ZedeX approach)
                # Fallback — finds 32-char/16-char hex strings in all RW memory.
                key_str = _scan_memory_for_aes_keys(h_process, aes_block, print_fn)
                if key_str:
                    found[md5_val] = key_str[:16].encode('ascii')
                    break
            finally:
                kernel32.CloseHandle(h_process)

        if md5_val in found:
            break  # Move to next file (but we only process first one)

    print_fn(f"[v2_key] Found {len(found)}/{len(md5_list)} keys")
    if found:
        _merge_into_cache(decrypted_dir, found, xor_key=derived_xor)
    return FoundV2Keys(found, derived_xor=derived_xor)


# ---------------------------------------------------------------------------
# 账号级派生 XOR
# ---------------------------------------------------------------------------

# XOR 是**按账号派生**出来的真值（``code & 0xFF``），不是可以写死的常量。
# 这里记录每个备份目录上"已经用真文件的 AES 段验证通过"的那个值，供同一进程内
# 后续的内存扫描 / 收割路径复用。
# issue #16 症状 2：把 XOR 写死成 0xC9 时，凡是 ``code & 0xFF != 0xC9`` 的账号，
# 解出来的图只有上面一小部分能显示、其余是纯色/垃圾 —— 因为 XOR 只作用文件尾部。
_DERIVED_XOR_BY_DIR = {}


def _dir_key(decrypted_dir) -> str:
    """把备份目录规范化成注册表键（大小写/相对路径不同的写法要指向同一条）。"""
    try:
        return os.path.normcase(os.path.abspath(str(decrypted_dir)))
    except (OSError, ValueError, TypeError):
        return str(decrypted_dir)


def _record_derived_xor(decrypted_dir, xor_key) -> None:
    """记住某个备份目录上**已验证**的账号级 XOR。``None`` 表示"还不知道"，不记。"""
    if xor_key is None:
        return
    _DERIVED_XOR_BY_DIR[_dir_key(decrypted_dir)] = int(xor_key) & 0xFF


def get_derived_xor(decrypted_dir):
    """取该备份目录上已验证的账号级 XOR（``code & 0xFF``）。

    返回 ``None`` = "从没成功派生过"，调用方**必须**回退
    ``media._DAT_V2_DEFAULT_XOR`` —— 不许把 None 当 0 用，也不许自己猜一个值。
    """
    return _DERIVED_XOR_BY_DIR.get(_dir_key(decrypted_dir))


class FoundV2Keys(dict):
    """``{md5: aes_key}`` 再加上派生出来的账号级 XOR 真值。

    继承 ``dict`` 是刻意的**向后兼容**选择：既有调用方的 ``found[md5]`` / ``in`` /
    ``len`` / ``dict(found)`` 语义逐字不变；只有需要尾部 XOR 的调用方才读
    ``derived_xor``（拿不到时为 ``None``，由调用方回退默认值）。
    ``cache_repaired`` 是**额外的可观测面**，不参与"发现了哪些密钥"的语义：
    它记录这次调用顺手纠正了几条**已经缓存**的坏条目（``0`` = 一条都没动）。
    MMKV 路径在"所有 V2 文件都已缓存"的稳态下（= issue #46 的现场）返回值仍是**空集合**，
    调用方只能靠这个字段知道"缓存被修过了、内存里那份 key_map 得重读"。
    """

    def __init__(self, *args, derived_xor=None, cache_repaired=0, **kwargs):
        super().__init__(*args, **kwargs)
        self.derived_xor = derived_xor
        self.cache_repaired = cache_repaired


def _same_xor(raw, xor_key: int) -> bool:
    """缓存里的 ``xor_key`` 可能是 ``'0xc9'`` / ``'0xC9'`` / 整数 —— 一律按**数值**比。"""
    try:
        if isinstance(raw, str):
            return int(raw, 16) == (int(xor_key) & 0xFF)
        return int(raw) == (int(xor_key) & 0xFF)
    except (TypeError, ValueError):
        return False


def _merge_into_cache(decrypted_dir, new_keys, xor_key=None, xor_src=None):
    """Merge newly found keys into _media_keys.json cache.

    xor_key: 账号级派生 XOR（``code & 0xFF``）。**给出真值时写这个值**，并且会把**这批
        md5 上**旧版本写死成 0xC9 的既有条目一并改回来（命中缓存的 md5 会在
        ``_load_or_build_image_key_map`` 里短路掉后续所有推导，必须就地纠正）。
        注意：这里**只**修 ``new_keys`` 里出现过的 md5，不会去重写整个缓存 ——
        "整库回溯修复"不在这里做（见报告里的残留限制）。
        不给出（内存扫描路径手上确实没有派生值）时保持历史行为：写 ``0xc9``。
    xor_src: 这个 ``xor_key`` **是怎么来的**（可观测性，issue #16 新评论要求）。
        取值：``'derived'``（内存/离线派生并用真文件验证过，缺省且在 xor_key 非空时用）、
        ``'repaired'``（离线修复通道就地纠正写入）、``'default'``（拿不到派生值 ⇒ 0xC9）。
        ⚠️ 为什么要存进文件：**老版本写下的 ``0xc9`` 与"派生真值恰好是 0xC9"在缓存里
        长得一模一样**，读侧日志只能说 "xor from cache"，报告者那种"缓存里的 0xc9 到底是
        真值还是老版本写死的"就无法区分。存了来源，读侧才能如实分类。
        老缓存没有这个字段 ⇒ 读侧记为 ``legacy``（**不假设**它是哪一种），并打印出来。
    """
    import json
    keys_file = os.path.join(decrypted_dir, '_media_keys.json')

    existing = {}
    try:
        if os.path.isfile(keys_file):
            with open(keys_file, 'r', encoding='utf-8') as f:
                existing = json.load(f)
    except Exception:
        pass

    xor_str = '0x%02x' % (int(xor_key) & 0xFF) if xor_key is not None else '0xc9'
    if xor_src is None:
        xor_src = 'derived' if xor_key is not None else 'default'

    md5_keys = existing.get('md5_keys', {})
    added = 0
    repaired = 0
    for md5_val, aes_key in new_keys.items():
        entry = md5_keys.get(md5_val)
        if entry is None:
            md5_keys[md5_val] = {
                'aes_key': aes_key.hex(),
                'xor_key': xor_str,
                'xor_src': xor_src,
            }
            added += 1
        elif not isinstance(entry, dict):
            continue
        else:
            # 来源也要写：老条目可能是"来源不明"，纠正之后必须变成"已知来源"。
            if entry.get('xor_src') != xor_src:
                entry['xor_src'] = xor_src
            if xor_key is not None and not _same_xor(entry.get('xor_key'), xor_key):
                entry['xor_key'] = xor_str
                repaired += 1

    if added or repaired:
        existing['md5_keys'] = md5_keys
        try:
            with open(keys_file, 'w', encoding='utf-8') as f:
                json.dump(existing, f, indent=2)
        except Exception:
            pass
        # 写入侧同样要能看出"这次用的是派生值还是默认值" —— 两者从结果上看不出区别，
        # 正是本缺陷长期潜伏的原因之一。
        _src = (f'derived {xor_str}' if xor_key is not None
                else 'default 0xc9 (no derived value available)')
        print(f"[v2_key] _media_keys.json: +{added} new, ~{repaired} repaired "
              f"— xor source = {_src} (xor_src={xor_src})", flush=True)
    return added


def _repair_cached_xor(decrypted_dir, xor_key, aes_key=None) -> int:
    """修复通道：把**已经缓存**条目里与"已验证真值"不等的 ``xor_key`` **就地纠正**。

    为什么需要它（known-issues #46 的残留）
    --------------------------------------
    ``_merge_into_cache`` 的纠正分支**只覆盖 ``new_keys`` 里出现过的 md5**
    （实现就是 ``for md5_val, aes_key in new_keys.items()``）。而 MMKV 路径的
    ``pending`` 明确排除**已经缓存**的 md5 ⇒ 只靠那一次 merge，**永远**纠正不到
    "已经写坏、又已经缓存"的那条条目 —— 而那正是 issue #46 现场（老版本写死的 0xC9
    已经落盘）；缓存命中还会短路掉后续所有推导 ⇒ 错值永久固化。
    本函数补的就是这个差集，且**完全独立于发现语义**：不改 ``pending``、不改
    ``found_all``、不改返回值里"发现了哪些 md5"。

    ⚠️ 与内存路径的分工：
      * **内存路径**（``find_keys_for_files``）对"被请求的 md5"生效、**需要微信在运行**；
      * **本通道**只用本机 MMKV 里派生并用**真文件**验证过的账号级值，
        **不需要微信在运行** —— 这正是"微信没在跑也要能自愈"的唯一抓手。

    安全边界（三条，缺一不可）
    --------------------------
    1. 只在 ``xor_key`` 是**已验证真值**时被调用（调用方保证）；这里不猜任何值，
       也**绝不**写 ``media._DAT_V2_DEFAULT_XOR``；
    2. 只改 ``xor_key`` 字段 —— ``aes_key`` 是另一个维度的数据，本通道没有重新"发现"密钥；
    3. 只动 ``aes_key`` 与本次用真文件验证过的账号密钥**一致**的条目：``xor_key`` 是
       **账号级**属性，只有条目确实属于本账号时"改成真值"才是确定的。归属不明的条目
       （``aes_key`` 不同，可能是别的账号/别的实验留下的）**不动**并在日志里报出来 ——
       改错会把本来能显示的图改坏。

    幂等：值已经相等的条目直接跳过；一条都不需要改时**不碰文件**（不重写，mtime 不变）。

    Returns: 实际被纠正的条目数（``0`` = 一条都没动）。
    """
    import json
    keys_file = os.path.join(decrypted_dir, '_media_keys.json')
    truth = int(xor_key) & 0xFF

    try:
        if not (os.path.isfile(keys_file) and os.path.getsize(keys_file) > 0):
            print(f"[mmkv] 缓存修复通道: 没有缓存文件（{os.path.basename(keys_file)}）"
                  f"⇒ 不动", flush=True)
            return 0
        with open(keys_file, 'r', encoding='utf-8') as f:
            existing = json.load(f)
    except Exception as e:
        print(f"[mmkv] 缓存修复通道: 缓存读不出来（{e}）⇒ 不动", flush=True)
        return 0

    md5_keys = existing.get('md5_keys') or {}
    if not isinstance(md5_keys, dict):
        print("[mmkv] 缓存修复通道: md5_keys 结构异常 ⇒ 不动", flush=True)
        return 0

    wanted_aes = aes_key.hex().lower() if aes_key else None
    payload = {}
    unowned = 0
    for md5_val, entry in md5_keys.items():
        if not isinstance(entry, dict):
            continue
        if _same_xor(entry.get('xor_key'), truth):
            continue                      # 幂等：值已经是对的 ⇒ 跳过（不重写）
        if wanted_aes is not None and str(entry.get('aes_key') or '').lower() != wanted_aes:
            unowned += 1                  # 归属不明 ⇒ 保守不动
            continue
        try:
            payload[md5_val] = bytes.fromhex(entry['aes_key'])
        except (KeyError, ValueError, TypeError):
            unowned += 1
            continue

    tail = (f"；另有 {unowned} 条归属不明（aes_key 与本次用真文件验证过的账号密钥不同）"
            f"**不动**") if unowned else ""

    if not payload:
        print(f"[mmkv] 缓存修复通道: 无需修复 —— {len(md5_keys)} 条已缓存条目的 xor_key "
              f"都已是 0x{truth:02x}（幂等，未写盘）{tail}", flush=True)
        return 0

    # 复用 `_merge_into_cache(..., xor_key=真值)` 的既有纠正能力：这些 md5 **已经在缓存里**，
    # 它对已存在的条目**只改 `xor_key`、不动 `aes_key`**（就是它的既有纠正分支）。
    # `xor_src='repaired'`：让读侧能看出"这个值是被离线修复通道就地纠正过的"。
    _merge_into_cache(decrypted_dir, payload, xor_key=truth, xor_src='repaired')

    fixed = len(payload)
    try:
        with open(keys_file, 'r', encoding='utf-8') as f:
            after = json.load(f).get('md5_keys') or {}
        fixed = sum(1 for m in payload if _same_xor((after.get(m) or {}).get('xor_key'), truth))
    except Exception:
        pass

    print(f"[mmkv] 缓存修复通道: 已就地纠正 {fixed} 条已缓存条目的 xor_key → 0x{truth:02x}"
          f"（**不需要微信在运行**）{tail}", flush=True)
    return fixed


def is_wechat_running():
    """Check if Weixin.exe is currently running."""
    return len(_get_wechat_pids()) > 0


# ---------------------------------------------------------------------------
# MMKV-based local key extraction (py_wx_key algorithm)
# ---------------------------------------------------------------------------

def _scan_mmkv_kvcomm_dirs():
    """Find all kvcomm directories under xwechat paths.

    Returns list of absolute directory paths.
    """
    import glob as _glob
    dirs = []
    appdata = os.environ.get('APPDATA', '')
    if not appdata:
        return dirs

    xwechat_root = os.path.join(appdata, 'Tencent', 'xwechat')
    if not os.path.isdir(xwechat_root):
        return dirs

    # Scan all subdirectories for kvcomm folders
    for root, subdirs, _files in os.walk(xwechat_root):
        # Limit depth to avoid scanning too deep
        depth = root[len(xwechat_root):].count(os.sep)
        if depth > 3:
            subdirs.clear()
            continue
        if os.path.basename(root) == 'kvcomm':
            dirs.append(root)

    return dirs


def _parse_mmkv_codes(kvcomm_dirs: list) -> list:
    """Extract numeric codes from key_*_.statistic files in kvcomm directories.

    Returns list of unique integer codes found.
    """
    import re as _re
    _KEY_FILE_RE = _re.compile(r'^key_(\d+)_.+\.statistic$')

    codes = set()
    for d in kvcomm_dirs:
        if not os.path.isdir(d):
            continue
        try:
            for fname in os.listdir(d):
                m = _KEY_FILE_RE.match(fname)
                if m:
                    code = int(m.group(1))
                    # py_wx_key filters: code > 0 && code <= 4294967295
                    if code > 0 and code <= 4294967295:
                        codes.add(code)
        except OSError:
            continue

    return sorted(codes)


def _derive_key_from_mmkv(code: int, wxid: str) -> tuple:
    """Derive V2 AES key and XOR key from an MMKV statistic code + wxid.

    Algorithm from py_wx_key (H3CoF6):
      xorKey = code & 0xFF
      aesKey = MD5(str(code) + wxid).hex()[:16]  (first 16 hex chars)

    The aesKey is used as 16 ASCII bytes (NOT hex-decoded to 8 raw bytes).
    pycryptodome's AES.new() accepts these 16 ASCII chars directly as a
    128-bit key.

    Returns (xor_key: int, aes_key: bytes) — aes_key is 16 ASCII bytes.
    """
    import hashlib
    code_str = str(code)
    hash_input = (code_str + wxid).encode('utf-8')
    md5_hex = hashlib.md5(hash_input).hexdigest()
    # First 16 hex chars used as ASCII bytes (16 bytes = AES-128 key)
    aes_key = md5_hex[:16].encode('ascii')
    xor_key = code & 0xFF
    return xor_key, aes_key


def _clean_wxid(wxid: str) -> str:
    """Strip the suffix after the second underscore (py_wx_key CleanWxid).

    'wxid_example12345_10e8' -> 'wxid_example12345'

    ⚠️ **不要再用它当"这个值能不能用来派生密钥"的守门人**（issue #16 新评论）：
    它对**不带 `wxid_` 前缀**的账号目录名原样返回（`myalias_68f8` → `myalias_68f8`），
    而历史上调用方紧接着写 `if not wxid.startswith('wxid_')` ⇒ 自定义微信号的账号
    **整条密钥链路被静默掐断**（报告者的日志：`[mmkv] Cannot determine wxid`）。
    现在改用 :func:`_account_id_candidates` 给出**多个候选**，由真文件验证取胜。
    保留本函数只是为了与 py_wx_key 的原始语义保持可比。
    """
    if not wxid or not wxid.startswith('wxid_'):
        return wxid
    parts = wxid.split('_')
    if len(parts) >= 3:
        return '_'.join(parts[:2])
    return wxid


# 账号目录下的 `.wxid`（备份/年报流程会写）：只是**众多来源之一**，同样要过验证。
_WXID_SIDE_FILE = '.wxid'


def _account_dir_name_from_hardlink_db(decrypted_dir: str) -> list:
    """从 `hardlink.db` 的 `db_info.uuid` 推出存储根，返回根下**所有真实子目录名**。

    issue #16：以前这里只认 `wxid_` 前缀 ⇒ 目录名是 `<自定义微信号>_68f8` 的机器
    一个候选都拿不到。现在**不按名字筛**，由调用方用真文件验证决定。
    """
    names = []
    hardlink_db = os.path.join(decrypted_dir, "hardlink", "hardlink.db")
    if not os.path.isfile(hardlink_db):
        hardlink_db = os.path.join(decrypted_dir, "HardLink", "hardlink.db")
    if not os.path.isfile(hardlink_db):
        return names
    try:
        conn = sqlite3.connect(hardlink_db)
        row = conn.execute(
            "SELECT ValueStdStr FROM db_info WHERE Key='uuid'"
        ).fetchone()
        conn.close()
    except sqlite3.Error:
        return names
    if not row or not row[0]:
        return names
    parts = str(row[0]).split('_', 2)
    if len(parts) < 3:
        return names
    storage_root = parts[-1]
    if not os.path.isdir(storage_root):
        return names
    try:
        for d in sorted(os.listdir(storage_root)):
            if d.startswith('.') or not os.path.isdir(os.path.join(storage_root, d)):
                continue
            names.append(d)
    except OSError:
        return names
    return names


def _account_id_candidates(decrypted_dir: str, wxid: str = None) -> tuple:
    """收集"账号 id 的候选形态" —— 返回 ``(candidates, raw_sources)``。

    ``raw_sources`` 是**没展开成形态**的原始名字（目录名一类），供"按目录去 glob
    `.dat`"这类**要访问文件**的调用使用；``candidates`` 才是给密钥派生用的。

    来源（按可信度排序）：
      1. 调用方给的值（可能是账号目录名、也可能是裸 id）；
      2. ``<decrypted_dir>/.wxid``（备份流程留下的账号名，存在才用）；
      3. ``basename(dirname(decrypted_dir))``（历史行为）；
      4. `hardlink.db` → 存储根 → 根下所有真实子目录名。

    ⚠️ 全部只是**候选**。调用方必须拿真文件（V2 `.dat` 的 AES 段 / 数据库首页 HMAC）
    验证；验证用 :func:`_try_key`。
    """
    from engine.utils import account_id_candidates as _expand  # 避免模块级循环依赖

    raw = []
    if wxid:
        raw.append(str(wxid).strip())
    try:
        side = os.path.join(str(decrypted_dir), _WXID_SIDE_FILE)
        if os.path.isfile(side):
            with open(side, 'r', encoding='utf-8', errors='replace') as f:
                side_val = (f.read() or '').strip()
            if side_val:
                raw.append(side_val)
    except OSError:
        pass
    raw.append(os.path.basename(os.path.dirname(str(decrypted_dir))))
    raw.extend(_account_dir_name_from_hardlink_db(str(decrypted_dir)))

    raw_sources = []
    for r in raw:
        r = (r or '').strip()
        if r and r not in raw_sources:
            raw_sources.append(r)

    candidates = []
    for r in raw_sources:
        for c in _expand(r):
            if c and c not in candidates:
                candidates.append(c)
    return candidates, raw_sources


def _load_v2_ciphertexts_for_any_name(decrypted_dir, raw_sources) -> dict:
    """按多个**目录名候选**去收集 V2 密文样本（`media/images` 那条与名字无关）。

    `_load_v2_ciphertexts()` 用名字去 glob 实机存储目录；名字不对时只有
    `<备份>/media/images` 那一路有效。这里依次试每个名字，拿到非空即止 ——
    验证需要的是**样本**，不是"名字对不对"。
    """
    for name in list(raw_sources) + [None]:
        tasks = _load_v2_ciphertexts(decrypted_dir, name)
        if tasks:
            return tasks
    return {}


def extract_keys_from_mmkv(decrypted_dir: str, wxid: str = None) -> dict:
    """Extract V2 AES keys from local MMKV statistic files (**no WeChat process needed**).

    Scans %APPDATA%\\Tencent\\xwechat\\**\\kvcomm\\ for key_*_.statistic files,
    derives per-account AES/XOR keys, and tests them against V2 ciphertexts in
    the backup. Working keys are cached to _media_keys.json.

    This is the py_wx_key approach — purely offline, local file-based key
    derivation. No WeChat process or memory scanning required.

    ⚠️ 与内存路径的分工（issue #46 的残留修复点）
    --------------------------------------------
    * **本函数（离线）**：只要本机 `%APPDATA%` 里还有 `kvcomm\\key_*_.statistic`，
      并且备份里还有 **V2 密文**可用于验证，就能独立得出"账号级 XOR 真值"。
      **不需要微信在运行**，因此它是"微信没在跑时也要能自愈"的唯一抓手。
    * **内存路径**（``find_keys_for_files`` → ``_merge_into_cache(..., xor_key=…)``）：
      需要 `Weixin.exe` 在运行，对**被请求的 md5** 生效（与缓存里有没有它无关）。

    本函数除了"发现新密钥"，还会在拿到**已验证真值**时顺手把**已经缓存**的坏条目纠正
    （见 :func:`_repair_cached_xor`）—— 这正是此前缺的那一块：``pending`` 排除了已缓存的
    md5 ⇒ 老版本写死 0xC9 落盘后**永远**不会被改回来。
    **发现语义与产出集合逐字不变**：``pending`` 的算法不变，返回的仍然只是
    "本次**新发现**的 md5 集合"（修复通道不往返回值里塞任何 md5）。

    Args:
        decrypted_dir: path to decrypted backup directory
        wxid: WeChat user ID (auto-detected if None)

    Returns:
        FoundV2Keys: ``{md5: key_bytes}``（= 本次**新发现**的密钥；稳态下为空 dict），
        并附带 ``derived_xor``（本次用真文件验证过的账号级 XOR 真值；拿不到时为 ``None``）
        与 ``cache_repaired``（本次顺手纠正了几条已缓存条目；``0`` = 一条都没动）。
        它继承 ``dict`` ⇒ 既有 ``found[md5]`` / ``in`` / ``len`` / ``dict(found)`` 语义不变。
    """
    if wxid is None:
        wxid = os.path.basename(os.path.dirname(decrypted_dir))

    # ---- 账号 id 候选（issue #16 新评论：**不再按 `wxid_` 前缀猜名字**）----------
    # 旧行为：名字不以 `wxid_` 开头 ⇒ 直接 return {}（日志 `Cannot determine wxid`）。
    # 对"自定义微信号 + `<微信号>_68f8` 目录名"的机器，这等于把**唯一**能离线自愈
    # 的通道整段掐死（报告者现场）。现在给出一组候选，后面的真文件验证自然淘汰错的。
    id_candidates, raw_sources = _account_id_candidates(decrypted_dir, wxid)
    if not id_candidates:
        print("[mmkv] 没有任何账号 id 候选（给的值/目录名/hardlink.db 都拿不到）"
              " — skipping MMKV extraction", flush=True)
        return {}
    print(f"[mmkv] 账号 id 候选 {len(id_candidates)} 个（源 {len(raw_sources)} 个）；"
          f"由真文件验证决定用哪个", flush=True)

    # 1. Find kvcomm directories and parse codes
    kvcomm_dirs = _scan_mmkv_kvcomm_dirs()
    if not kvcomm_dirs:
        print("[mmkv] No kvcomm directories found", flush=True)
        return {}

    codes = _parse_mmkv_codes(kvcomm_dirs)
    if not codes:
        print("[mmkv] No key statistic files found in kvcomm dirs", flush=True)
        return {}

    print(f"[mmkv] Found {len(codes)} MMKV code(s) in {len(kvcomm_dirs)} kvcomm dirs: {codes}",
          flush=True)

    # 2. 派生候选：(code × 账号 id 形态) —— 每个组合都用**真文件**验证
    candidates = []
    for code in codes:
        for cand in id_candidates:
            xor_key, aes_key = _derive_key_from_mmkv(code, cand)
            candidates.append((xor_key, aes_key, code, cand))

    if not candidates:
        return {}

    # 3. Load V2 ciphertexts for verification（多个目录名候选都试，拿到样本即止）
    tasks = _load_v2_ciphertexts_for_any_name(decrypted_dir, raw_sources)
    if not tasks:
        print("[mmkv] No V2 .dat files found for verification", flush=True)
        return {}

    print(f"[mmkv] Testing {len(candidates)} candidate(s) "
          f"({len(codes)} code × {len(id_candidates)} id 形态) against {len(tasks)} V2 file(s)...",
          flush=True)

    # 4. Verify each candidate against a small sample, then cache globally.
    #    The key is per-account (not per-image), so we only need to verify
    #    against a few files to confirm it works.
    import json as _json
    found_all = {}
    keys_file = os.path.join(decrypted_dir, '_media_keys.json')

    # Load existing keys to avoid re-work
    existing_md5s = set()
    try:
        if os.path.isfile(keys_file) and os.path.getsize(keys_file) > 0:
            with open(keys_file, 'r', encoding='utf-8') as f:
                cached = _json.load(f)
            existing_md5s = set(cached.get('md5_keys', {}).keys())
    except Exception:
        pass

    pending = {md5: v for md5, v in tasks.items() if md5 not in existing_md5s}

    # ⚠️ 这里**不再**在 `pending` 为空时提前 return（known-issues #46 的残留）。
    # 已缓存的 md5 不进 pending ⇒ 一旦缓存里的 `xor_key` 被写坏（老版本写死的 0xC9），
    # 提前 return 会让它**永远**不被纠正 —— 而这条路径是**离线**的（不需要微信在运行），
    # 正是"微信没在跑也想自愈"唯一的抓手。
    # 改动后的语义：**发现**仍然只做 pending 那一套（产出集合不变），
    # 但顺手把**已缓存**的条目按"已验证真值"就地纠正（见 `_repair_cached_xor`）。
    cache_repair_only = not pending
    if cache_repair_only:
        print("[mmkv] All V2 keys already cached — 改走**缓存修复通道**"
              "（本路径不需要微信在运行）", flush=True)

    # 验证样本池：
    #   * 有未缓存文件 ⇒ 用 `pending` 的前 5 个（**逐字保持**历史行为）；
    #   * 全是已缓存的稳态 ⇒ 退回到 `tasks` 里的**已缓存**文件当样本。它们同样是本备份
    #     目录里的真 V2 密文，"这把 AES 能不能解开本账号的图"的验证能力完全等价。
    # ⚠️ 绝不能让样本池为空：`match_count == len(sample_md5s) == 0` 会让**每一个**候选都
    #     "验证通过" ⇒ 写出一个从没被验证过的 XOR（那正是本任务明令禁止的"猜"）。
    sample_pool = tasks if cache_repair_only else pending
    sample_md5s = list(sample_pool.keys())[:5]
    if not sample_md5s:
        print("[mmkv] No V2 file available for verification", flush=True)
        return {}

    # Sample up to 5 files for verification; if the key works on these,
    # it works for ALL files (per-account key, not per-image).
    verified_codes = set()
    verified_xor = None
    verified_code = None
    verified_aes = None
    verified_id = None

    for xor_key, aes_key, code, id_form in candidates:
        match_count = 0
        for md5 in sample_md5s:
            fmt = _try_key(aes_key, sample_pool[md5][1])
            if fmt:
                match_count += 1
        if match_count == len(sample_md5s):
            print(f"[mmkv] Code {code} verified ({match_count}/{len(sample_md5s)} sample files)"
                  f" — 账号 id 形态={id_form!r}（真文件验证通过，这一形态就是本账号的）",
                  flush=True)
            verified_codes.add(code)
            verified_xor = xor_key
            verified_code = code
            verified_aes = aes_key
            verified_id = id_form
            # Cache for ALL pending files (not just the sample)
            for md5 in pending:
                found_all[md5] = aes_key
            break  # One working code is enough
        elif match_count > 0:
            print(f"[mmkv] Code {code} partial match ({match_count}/{len(sample_md5s)})"
                  f" — 账号 id 形态={id_form!r}",
                  flush=True)
            verified_codes.add(code)
            verified_xor = xor_key
            verified_code = code
            verified_aes = aes_key
            verified_id = id_form
            for md5 in pending:
                found_all[md5] = aes_key
            break
        else:
            print(f"[mmkv] Code {code} no match on sample files"
                  f"（id 形态={id_form!r}）", flush=True)

    # XOR 与 AES 是**同一个 code** 派生的（`code & 0xFF`）：AES 在**真文件**上验证通过
    # ⇒ 这个 code 就是本账号的 ⇒ 派生出的 XOR 是**真值**，可以登记（供后续内存扫描 /
    # 缓存修复复用），也可以用来纠正缓存。拿不到真值时**绝不**登记、更不写默认值。
    if verified_xor is not None:
        _record_derived_xor(decrypted_dir, verified_xor)
        print(f"[mmkv] 已验证的派生 XOR = 0x{verified_xor & 0xFF:02x}"
              f"（code={verified_code}，账号 id 形态={verified_id!r}）"
              f"—— 已登记，供后续内存扫描 / 缓存修复复用",
              flush=True)

    if found_all:
        print(f"[mmkv] Success! Account key derived locally — cached for {len(found_all)} files",
              flush=True)
        _merge_into_cache(decrypted_dir, found_all, xor_key=verified_xor)
    elif verified_xor is None:
        print(f"[mmkv] No keys matched — 试过 {len(id_candidates)} 个账号 id 形态 × "
              f"{len(codes)} 个 code，都不匹配：账号可能用别的 id / 别的 code，"
              f"或这些 .dat 不属于本账号", flush=True)
    else:
        # 验上了，但没有任何未缓存的文件 ⇒ 这次不是"发现"，只是"修复"（别再喊"没匹配上"）。
        print(f"[mmkv] 密钥已用真文件验证通过（code={verified_code}），"
              f"本次没有未缓存的文件需要发现", flush=True)

    # --- 修复通道（与"发现语义"分离的独立开关）---
    repaired = 0
    if verified_xor is None:
        print("[mmkv] 未取得**已验证**的派生 XOR ⇒ 本次**不动**缓存"
              "（绝不猜、绝不写默认值）", flush=True)
    else:
        repaired = _repair_cached_xor(decrypted_dir, verified_xor, aes_key=verified_aes)

    return FoundV2Keys(found_all, derived_xor=verified_xor, cache_repaired=repaired)


# ---------------------------------------------------------------------------
# Continuous key harvester
# ---------------------------------------------------------------------------

def _load_v2_ciphertexts(decrypted_dir, wxid):
    """Pre-load ciphertexts from all V2 .dat files in the backup.

    Returns dict: {md5: (file_path, aes_block_16b)} for all V2 files.
    """
    import glob as _glob

    tasks = {}
    # Search for .dat files in media/images (backup) and original storage
    search_dirs = [
        os.path.join(decrypted_dir, 'media', 'images'),
    ]
    # Also search original WeChat storage
    base = _get_base_storage(decrypted_dir)
    if base and wxid:
        for pattern in ['msg/attach/*/*/Img/*.dat', 'msg/image/*/*.dat']:
            search_dirs.append(os.path.join(base, wxid, pattern))

    for sdir in search_dirs:
        if '*' in sdir:
            for fpath in _glob.glob(sdir):
                md5 = os.path.splitext(os.path.basename(fpath))[0]
                # Strip _t, _h suffixes
                for sfx in ('_t', '_h'):
                    if md5.endswith(sfx):
                        md5 = md5[:-2]
                if md5 in tasks or len(md5) < 16:
                    continue
                try:
                    with open(fpath, 'rb') as f:
                        data = f.read(128)
                except OSError:
                    continue
                if len(data) >= 31 and data[:6] == b'\x07\x08V2\x08\x07':
                    tasks[md5] = (fpath, data[15:31])
        elif os.path.isdir(sdir):
            for fname in os.listdir(sdir):
                if not fname.lower().endswith('.dat'):
                    continue
                fpath = os.path.join(sdir, fname)
                md5 = os.path.splitext(fname)[0]
                for sfx in ('_t', '_h'):
                    if md5.endswith(sfx):
                        md5 = md5[:-2]
                if md5 in tasks or len(md5) < 16:
                    continue
                try:
                    with open(fpath, 'rb') as f:
                        data = f.read(128)
                except OSError:
                    continue
                if len(data) >= 31 and data[:6] == b'\x07\x08V2\x08\x07':
                    tasks[md5] = (fpath, data[15:31])

    return tasks


def _test_key_against_all(key_bytes, tasks):
    """Test a key candidate against all known ciphertexts.

    Returns (md5, format) for the first match, or (None, None).
    """
    for md5, (_, ciphertext) in tasks.items():
        fmt = _try_key(key_bytes, ciphertext)
        if fmt:
            return md5, fmt
    return None, None


def harvest_v2_keys(decrypted_dir, wxid=None, interval=2.0,
                    max_rounds=None, print_fn=None, stop_event=None):
    """Continuously scan WeChat memory for V2 AES keys and cache them.

    Pre-loads all V2 file ciphertexts from the backup, then polls WeChat
    process memory at the given interval. Each round tries all 3 strategies
    against ALL known ciphertexts — much more efficient than one-at-a-time.

    Keys are immediately cached to _media_keys.json so subsequent offline
    viewing works without WeChat running.

    Args:
        decrypted_dir: path to decrypted backup directory
        wxid: WeChat user ID (auto-detected if None)
        interval: seconds between scan rounds (default 2.0)
        max_rounds: maximum scan rounds (None = run until interrupted)
        print_fn: optional logging function
        stop_event: optional threading.Event — when set, harvester exits cleanly

    Returns:
        dict: {md5: key_bytes} for all found keys
    """
    import time

    if print_fn is None:
        print_fn = lambda *a, **kw: None

    # 账号名/账号 id 候选：harvest 只用它去**找 `.dat` 样本**（要访问文件 ⇒ 用目录名），
    # 以及登记派生 XOR 时给 `_record_derived_xor` 用（那一步按 decrypted_dir 归一化）。
    if wxid is None:
        wxid = os.path.basename(os.path.dirname(decrypted_dir))
    id_candidates, raw_sources = _account_id_candidates(decrypted_dir, wxid)

    if not raw_sources and not id_candidates:
        print_fn("[v2_harvest] ERROR: 没有任何账号名候选（给的值 / 目录名 / hardlink.db 都拿不到）")
        return {}

    # Load V2 ciphertexts（多个目录名候选依次试）
    tasks = _load_v2_ciphertexts_for_any_name(decrypted_dir, raw_sources)
    if not tasks:
        print_fn("[v2_harvest] No V2 .dat files found in backup")
        return {}

    print_fn(f"[v2_harvest] Loaded {len(tasks)} V2 ciphertexts for verification"
             f"（账号名候选 {len(raw_sources)} 个 / id 形态 {len(id_candidates)} 个）")

    # Load existing cache to avoid re-scanning
    found_all = {}
    import json as _json
    keys_file = os.path.join(decrypted_dir, '_media_keys.json')
    existing_md5s = set()
    try:
        if os.path.isfile(keys_file) and os.path.getsize(keys_file) > 0:
            with open(keys_file, 'r', encoding='utf-8') as f:
                cached = _json.load(f)
            md5_keys = cached.get('md5_keys', {})
            existing_md5s = set(md5_keys.keys())
            print_fn(f"[v2_harvest] {len(existing_md5s)} keys already cached")
    except Exception:
        pass

    pending = {md5: v for md5, v in tasks.items() if md5 not in existing_md5s}
    if not pending:
        print_fn("[v2_harvest] All V2 keys already cached!")
        return {}

    print_fn(f"[v2_harvest] {len(pending)} files still need keys")

    # --- Baseline: try MMKV-based local key derivation (py_wx_key approach) ---
    # This can resolve keys OFFLINE without WeChat running at all.
    try:
        mmkv_found = extract_keys_from_mmkv(decrypted_dir, wxid)
        if mmkv_found:
            for md5, key_bytes in mmkv_found.items():
                found_all[md5] = key_bytes
                if md5 in pending:
                    del pending[md5]
            print_fn(f"[v2_harvest] MMKV baseline resolved {len(mmkv_found)} keys, "
                     f"{len(pending)} remaining")
        if not pending:
            print_fn("[v2_harvest] All keys resolved via MMKV local derivation!")
            return found_all
    except Exception as e:
        print_fn(f"[v2_harvest] MMKV extraction failed: {e}")

    # 账号级 XOR 真值（MMKV 基线验证过时才有）：下面所有新找到的密钥都要带上它。
    # 写死 0xC9 会让 `code & 0xFF != 0xC9` 的账号只解出图片上半截（issue #16 症状 2）。
    derived_xor = get_derived_xor(decrypted_dir)

    if not is_wechat_running():
        print_fn("[v2_harvest] WeChat is not running. Start WeChat and scroll through "
                 "chats with images to expose keys in memory, then run this command.")
        return {}

    round_num = 0
    last_new = 0
    t_start = time.time()

    try:
        while max_rounds is None or round_num < max_rounds:
            if stop_event and stop_event.is_set():
                print_fn("[v2_harvest] Stop event received — exiting")
                break
            round_num += 1
            t0 = time.time()

            pids = _get_wechat_pids()
            if not pids:
                print_fn(f"[v2_harvest:r{round_num}] WeChat exited — stopping")
                break

            round_found = 0
            for pid in pids:
                access = 0x0010 | 0x0400
                h_process = kernel32.OpenProcess(access, False, pid)
                if not h_process:
                    continue
                try:
                    # Strategy 1: V2 header proximity scan
                    # Find all V2 headers, read ±256 bytes around each,
                    # test 16-byte sliding windows against ALL ciphertexts
                    V2_MAGIC = b'\x07\x08\x56\x32\x08\x07'
                    WINDOW_HALF = 256
                    mbi = MEMORY_BASIC_INFORMATION()
                    v2_addrs = []
                    address = 0
                    while address < 0x7FFFFFFFFFFF:
                        result = kernel32.VirtualQueryEx(
                            h_process, ctypes.c_void_p(address),
                            ctypes.byref(mbi), ctypes.sizeof(mbi)
                        )
                        if result == 0:
                            break
                        if (mbi.State == MEM_COMMIT and
                            mbi.Protect != PAGE_NOACCESS and
                            (mbi.Protect & PAGE_GUARD) == 0 and
                            0 < mbi.RegionSize <= 50 * 1024 * 1024):
                            region_base = ctypes.cast(mbi.BaseAddress, ctypes.c_void_p).value
                            if region_base is None:
                                next_addr = address + mbi.RegionSize
                                address = next_addr
                                continue
                            try:
                                buf = ctypes.create_string_buffer(mbi.RegionSize)
                            except (OverflowError, MemoryError):
                                next_addr = address + mbi.RegionSize
                                address = next_addr
                                continue
                            br = ctypes.c_size_t(0)
                            ok = kernel32.ReadProcessMemory(
                                h_process, ctypes.c_void_p(region_base),
                                buf, mbi.RegionSize, ctypes.byref(br)
                            )
                            if ok and br.value > 0:
                                data = buf.raw[:br.value]
                                idx = data.find(V2_MAGIC)
                                while idx != -1:
                                    v2_addrs.append(region_base + idx)
                                    idx = data.find(V2_MAGIC, idx + 1)
                        next_addr = address + mbi.RegionSize
                        if next_addr <= address:
                            break
                        address = next_addr

                    if v2_addrs:
                        tested = 0
                        for addr in v2_addrs:
                            buf_start = addr - WINDOW_HALF
                            buf_size = WINDOW_HALF * 2
                            try:
                                buf = ctypes.create_string_buffer(buf_size)
                            except (OverflowError, MemoryError):
                                continue
                            br = ctypes.c_size_t(0)
                            ok = kernel32.ReadProcessMemory(
                                h_process, ctypes.c_void_p(buf_start),
                                buf, buf_size, ctypes.byref(br)
                            )
                            if not ok or br.value < 16:
                                continue
                            data = buf.raw[:br.value]

                            # 16-byte windows, step=4
                            for i in range(0, len(data) - 16, 4):
                                candidate = data[i:i + 16]
                                tested += 1
                                md5_match, fmt = _test_key_against_all(candidate, pending)
                                if md5_match:
                                    key_hex = candidate.hex()
                                    print_fn(f"  [v2_harvest:r{round_num}] "
                                             f"Found key for {md5_match[:16]}... -> {fmt}")
                                    found_all[md5_match] = candidate
                                    _merge_into_cache(decrypted_dir,
                                                      {md5_match: candidate},
                                                      xor_key=derived_xor)
                                    del pending[md5_match]
                                    round_found += 1
                                    if not pending:
                                        break
                            if not pending:
                                break

                            # 32-char hex decode windows
                            for i in range(0, len(data) - 32, 16):
                                chunk = data[i:i + 32]
                                try:
                                    hex_str = chunk.decode('ascii')
                                except UnicodeDecodeError:
                                    continue
                                if not all(c in '0123456789abcdefABCDEF'
                                          for c in hex_str):
                                    continue
                                try:
                                    decoded = bytes.fromhex(hex_str)
                                except ValueError:
                                    continue
                                md5_match, fmt = _test_key_against_all(decoded, pending)
                                if md5_match:
                                    print_fn(f"  [v2_harvest:r{round_num}] "
                                             f"Found key for {md5_match[:16]}... -> {fmt} (hex)")
                                    found_all[md5_match] = decoded
                                    _merge_into_cache(decrypted_dir,
                                                      {md5_match: decoded},
                                                      xor_key=derived_xor)
                                    del pending[md5_match]
                                    round_found += 1
                                    if not pending:
                                        break
                            if not pending:
                                break

                    # Strategy 2: Scan RW memory for 32-char hex strings
                    if pending:
                        rw_regions = []
                        address = 0
                        while address < 0x7FFFFFFFFFFF:
                            result = kernel32.VirtualQueryEx(
                                h_process, ctypes.c_void_p(address),
                                ctypes.byref(mbi), ctypes.sizeof(mbi)
                            )
                            if result == 0:
                                break
                            if (mbi.State == MEM_COMMIT and
                                (mbi.Protect & _RW_FLAGS) != 0 and
                                (mbi.Protect & PAGE_GUARD) == 0 and
                                0 < mbi.RegionSize <= 50 * 1024 * 1024):
                                rw_regions.append(
                                    (ctypes.cast(mbi.BaseAddress, ctypes.c_void_p).value,
                                     mbi.RegionSize)
                                )
                            next_addr = address + mbi.RegionSize
                            if next_addr <= address:
                                break
                            address = next_addr

                        for base_addr, region_size in rw_regions:
                            if not pending:
                                break
                            try:
                                buf = ctypes.create_string_buffer(region_size)
                            except (OverflowError, MemoryError):
                                continue
                            br = ctypes.c_size_t(0)
                            ok = kernel32.ReadProcessMemory(
                                h_process, ctypes.c_void_p(base_addr),
                                buf, region_size, ctypes.byref(br)
                            )
                            if not ok or br.value < 32:
                                continue
                            data = buf.raw[:br.value]

                            for m in _RE_KEY32.finditer(data):
                                key_bytes = m.group()
                                # hex-decoded
                                try:
                                    decoded = bytes.fromhex(
                                        key_bytes.decode('ascii'))
                                    md5_match, fmt = _test_key_against_all(
                                        decoded, pending)
                                    if md5_match:
                                        print_fn(f"  [v2_harvest:r{round_num}] "
                                                 f"Found key for {md5_match[:16]}..."
                                                 f" -> {fmt} (regex)")
                                        found_all[md5_match] = decoded
                                        _merge_into_cache(
                                            decrypted_dir,
                                            {md5_match: decoded},
                                            xor_key=derived_xor)
                                        del pending[md5_match]
                                        round_found += 1
                                        if not pending:
                                            break
                                except ValueError:
                                    pass

                                if not pending:
                                    break

                finally:
                    kernel32.CloseHandle(h_process)

                if not pending:
                    break

            elapsed = time.time() - t0
            total_found = len(found_all)
            if round_found > 0:
                last_new = round_num
                print_fn(f"[v2_harvest:r{round_num}] +{round_found} keys "
                         f"({elapsed:.1f}s) — {total_found} total, "
                         f"{len(pending)} remaining")
            elif round_num % 5 == 0:
                runtime = time.time() - t_start
                print_fn(f"[v2_harvest:r{round_num}] no new keys ({elapsed:.1f}s) "
                         f"— {total_found} total, {len(pending)} remaining "
                         f"(running {runtime:.0f}s)")

            if not pending:
                print_fn(f"[v2_harvest] ALL {total_found} keys found in "
                         f"{round_num} rounds!")
                break

            # Auto-stop: if no new keys for 20 rounds (40s at 2s interval),
            # reduce frequency to every 5s
            if round_num - last_new > 20:
                if interval < 5.0:
                    print_fn("[v2_harvest] No new keys for 20 rounds — "
                             "slowing to 5s interval. Keep scrolling in WeChat.")
                    interval = 5.0
            if round_num - last_new > 60:
                print_fn("[v2_harvest] No new keys for 60 rounds — giving up. "
                         "Open more images in WeChat and re-run.")
                break

            # Sleep in sub-second increments to allow responsive stop
            if stop_event:
                for _ in range(int(interval * 10)):
                    if stop_event.is_set():
                        break
                    time.sleep(0.1)
            else:
                time.sleep(interval)

    except KeyboardInterrupt:
        print_fn(f"\n[v2_harvest] Interrupted. Saved {len(found_all)} keys.")

    return found_all
