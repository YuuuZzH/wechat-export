"""Decrypt WeChat databases for backup, using engine.decrypt."""
import hashlib
import hmac as hmac_mod
import os
import json
import struct
from typing import Callable

# SQLCipher 4 constants (must match engine/constants.py)
PAGE_SZ = 4096
KEY_SZ = 32
SALT_SZ = 16


def load_keys(key_file: str = None) -> dict:
    """Load decryption keys from config or a legacy JSON file.

    When key_file is None, reads from .wechat_exp_config.json via get_db_keys().
    Otherwise loads from the given file (legacy all_keys.json format).

    Returns dict mapping db_path -> key (hex string).
    Handles both formats: plain hex strings and {"enc_key": "..."} dicts.
    """
    if key_file is None:
        from engine.config_file import get_db_keys
        return get_db_keys()

    if not os.path.exists(key_file):
        return {}
    try:
        with open(key_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
    result = {}
    for k, v in data.items():
        if k.startswith('_'):
            continue
        if isinstance(v, dict):
            hex_key = v.get('enc_key', '')
            if hex_key and len(hex_key) == 64:
                result[k] = hex_key
        elif isinstance(v, str) and len(v) == 64:
            result[k] = v
    return result


def _verify_enc_key(enc_key: bytes, db_page1: bytes) -> bool:
    """Verify an encryption key against page 1 HMAC (SQLCipher 4).

    Args:
        enc_key: raw 32-byte encryption key
        db_page1: first 4096 bytes of the encrypted database

    Returns True if the key's HMAC matches the stored HMAC in page 1.
    """
    salt = db_page1[:SALT_SZ]
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=KEY_SZ)
    hmac_data = db_page1[SALT_SZ: PAGE_SZ - 80 + 16]
    stored_hmac = db_page1[PAGE_SZ - 64: PAGE_SZ]
    hm = hmac_mod.new(mac_key, hmac_data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hm.digest() == stored_hmac


def _read_page1(src_path: str) -> bytes:
    """Read the first page of an encrypted database file."""
    with open(src_path, 'rb') as f:
        return f.read(PAGE_SZ)


def _find_key_for_basename(keys: dict, basename: str) -> bytes:
    """Find a decryption key matching a given db basename."""
    for kpath, kval in keys.items():
        if os.path.basename(kpath) == basename:
            return bytes.fromhex(kval)
    return None


def _find_key_by_hmac(keys: dict, src_path: str, salt_keys: dict = None,
                      page1: bytes = None) -> bytes:
    """Find the correct key for a DB by trying all known keys against page 1 HMAC.

    Reads page 1 of the source DB and verifies each known key's HMAC.
    Returns the first matching key, or None if no key matches.
    """
    if page1 is None:
        try:
            page1 = _read_page1(src_path)
        except OSError:
            return None

    if len(page1) < PAGE_SZ:
        return None

    # Deduplicate unique key hex values to avoid redundant HMAC verifications.
    # salt 表里的密钥也要一起试（多账号时正确密钥可能只在 salt 表里）。
    candidates = list(keys.items()) + list((salt_keys or {}).items())
    unique_hex = list(dict.fromkeys(kval for kpath, kval in candidates
                                    if len(str(kval)) == 64))
    for key_hex in unique_hex:
        try:
            key_bytes = bytes.fromhex(str(key_hex))
        except ValueError:
            continue
        if _verify_enc_key(key_bytes, page1):
            return key_bytes
    return None


def _salt_of(page1: bytes) -> str:
    """page1 的前 16 字节即该加密库的 salt（32 位 hex），文件级唯一标识。"""
    return page1[:SALT_SZ].hex() if len(page1) >= SALT_SZ else ''


def _find_key_by_salt(salt_keys: dict, salt_hex: str, page1: bytes) -> bytes:
    """按 salt 精确匹配并**实测校验**（同 salt 必为同一文件，校验只是兜底）。"""
    if not salt_hex or not salt_keys:
        return None
    key_hex = salt_keys.get(salt_hex)
    if not key_hex or len(str(key_hex)) != 64:
        return None
    try:
        key = bytes.fromhex(str(key_hex))
    except ValueError:
        return None
    return key if _verify_enc_key(key, page1) else None


def _find_key_by_basename_verified(keys: dict, basename: str, page1: bytes) -> bytes:
    """按 basename 命中后**仍要校验**：多账号同名库场景下 basename 会命中别的账号的密钥。"""
    for kpath, kval in keys.items():
        if os.path.basename(kpath) != basename:
            continue
        if len(str(kval)) != 64:
            continue
        try:
            key = bytes.fromhex(str(kval))
        except ValueError:
            continue
        if _verify_enc_key(key, page1):
            return key
    return None


def _resolve_key(keys: dict, src_path: str, salt_keys: dict = None) -> bytes:
    """Resolve the correct encryption key for a database file.

    Priority:
      1. **salt 精确匹配**（page1 前 16 字节）—— 多账号同名库互不干扰
      2. basename 匹配，但必须通过 HMAC 校验
      3. HMAC 全量试探（rel 表 + salt 表）
    """
    try:
        page1 = _read_page1(src_path)
    except OSError:
        page1 = b''

    if len(page1) >= PAGE_SZ:
        key = _find_key_by_salt(salt_keys or {}, _salt_of(page1), page1)
        if key is not None:
            return key
        key = _find_key_by_basename_verified(keys, os.path.basename(src_path), page1)
        if key is not None:
            return key
        return _find_key_by_hmac(keys, src_path, salt_keys, page1=page1)

    # 读不到 page1（文件太短/IO 失败）：退回老的 basename 直取，避免比修复前更差
    key = _find_key_for_basename(keys, os.path.basename(src_path))
    if key is not None:
        return key
    return _find_key_by_hmac(keys, src_path, salt_keys)


def resolve_key_for(src_path: str, keys: dict = None, salt_keys: dict = None) -> bytes:
    """对外入口：不传密钥时自动从配置里取（rel 表 + salt 表），再解析单个库的密钥。"""
    if keys is None or salt_keys is None:
        from engine.config_file import get_db_keys, get_db_keys_by_salt
        if keys is None:
            keys = get_db_keys()
        if salt_keys is None:
            salt_keys = get_db_keys_by_salt()
    return _resolve_key(keys, src_path, salt_keys)


def _decrypt_one(src: str, dst: str, key: bytes, on_progress, label: str,
                 progress_start: float, progress_end: float) -> bool:
    """Decrypt a single database file. Returns True on success."""
    from engine.decrypt import decrypt_database

    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        success = decrypt_database(src, dst, key,
                                   print_fn=lambda m: on_progress(f"{label}: {m}", progress_start) if on_progress else None)
        return bool(success)
    except Exception:
        return False


def _iter_encrypted_dbs(db_storage_path: str):
    """遍历 db_storage 下所有加密库（跳过 -wal/-shm）。"""
    for root, _dirs, files in os.walk(db_storage_path):
        for f in sorted(files):
            if f.endswith('.db') and not f.endswith('-wal') and not f.endswith('-shm'):
                yield os.path.join(root, f)


def backfill_salt_keys(db_storage_path: str, keys: dict = None, salt_keys: dict = None,
                       on_progress: Callable[[str], None] = None) -> int:
    """把"已验证的密钥 ↔ salt"这一事实固化进配置（**只增不改**、幂等）。

    老配置只有 rel 表，本函数借一次"能取到密钥"的机会把 salt 表补齐；读不到 page1 或
    校验不过的库一律跳过（宁可少写，不可写错）。返回新增条数。
    """
    from engine.config_file import get_db_keys, get_db_keys_by_salt, set_db_keys

    if keys is None:
        keys = get_db_keys()
    if salt_keys is None:
        salt_keys = get_db_keys_by_salt()

    known = {str(k): str(v) for k, v in (salt_keys or {}).items()}
    added = {}
    for path in _iter_encrypted_dbs(db_storage_path):
        try:
            page1 = _read_page1(path)
        except OSError:
            continue
        if len(page1) < PAGE_SZ:
            continue
        salt = _salt_of(page1)
        if not salt or known.get(salt):
            continue
        key = _resolve_key(keys, path, salt_keys)
        if key is None or not _verify_enc_key(key, page1):
            continue
        added[salt] = key.hex()
        known[salt] = key.hex()
        if on_progress:
            on_progress('已记录密钥身份: salt=%s… (%s)' % (salt[:8], os.path.basename(path)))
    if added:
        set_db_keys({}, db_dir=db_storage_path, salt_keys=added)
    return len(added)


def decrypt_for_backup(
    db_storage_path: str,
    output_dir: str,
    keys: dict,
    on_progress: Callable[[str, float], None] = None,
    salt_keys: dict = None,
    link_dest: str = None,
) -> list:
    """Decrypt WeChat databases from db_storage to output_dir.

    Writes decrypted DBs into the same subdirectory structure the chat viewer
    expects: message/*.db, contact/contact.db, hardlink/hardlink.db.

    When ``link_dest`` points at a previous backup with matching source
    fingerprints, unchanged DBs are hardlinked/copied instead of re-decrypted.

    Args:
        db_storage_path: WeChat db_storage directory
        output_dir: Backup output root
        keys: {db_path_relative: key_hex} mapping
        on_progress: callback(current_db, progress_0_to_1)
        link_dest: previous backup root for incremental reuse

    Returns:
        (decrypted_paths, skipped_missing_key, stats)
        stats: {reused, decrypted, fingerprint}
    """
    from engine.decrypt import decrypt_database
    from .fingerprint import (
        load_fingerprint, rel_key, save_fingerprint, source_sig, try_reuse_decrypted,
    )

    # salt 表：多账号同名库靠它区分（老配置里可能为空，下面会顺手回填）
    if salt_keys is None:
        try:
            from engine.config_file import get_db_keys_by_salt
            salt_keys = get_db_keys_by_salt()
        except Exception:
            salt_keys = {}

    prev_fp = load_fingerprint(link_dest) if link_dest else {}
    new_fp = {}
    reused = 0
    freshly = 0

    msg_src = os.path.join(db_storage_path, 'message')
    msg_out = os.path.join(output_dir, 'message')
    os.makedirs(msg_out, exist_ok=True)

    results = []
    skipped_missing_key = []

    def _handle_one(src_path: str, dst_path: str, label: str, progress: float) -> bool:
        nonlocal reused, freshly
        try:
            new_fp[rel_key(db_storage_path, src_path)] = source_sig(src_path)
        except OSError:
            pass
        if try_reuse_decrypted(src_path, dst_path, link_dest, prev_fp, db_storage_path):
            results.append(dst_path)
            reused += 1
            if on_progress:
                on_progress(f"复用未变更: {label}", progress)
            return True
        key = _resolve_key(keys, src_path, salt_keys)
        if key is None:
            return False
        try:
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            ok = decrypt_database(
                src_path, dst_path, key,
                print_fn=(lambda m, lb=label: on_progress(f"{lb}: {m}", progress)
                          if on_progress else None),
            )
        except Exception:
            ok = False
        if ok:
            results.append(dst_path)
            freshly += 1
            if on_progress:
                on_progress(f"已解密: {label}", progress)
            return True
        if on_progress:
            on_progress(f"跳过 {label} (解密失败/HMAC不匹配)", progress)
        return False

    # --- Message & Media databases ---
    if os.path.isdir(msg_src):
        msg_files = sorted(
            [f for f in os.listdir(msg_src) if f.startswith('message_') and f.endswith('.db')]
        )
        media_files = sorted(
            [f for f in os.listdir(msg_src) if f.startswith('media_') and f.endswith('.db')]
        )
        db_files = msg_files + media_files
        # +3: contact + hardlink + sns（朋友圈，可选）
        total = len(db_files) + 3
        for i, fname in enumerate(db_files):
            src_path = os.path.join(msg_src, fname)
            dst_path = os.path.join(msg_out, fname)
            prog = (i + 1) / total
            if try_reuse_decrypted(src_path, dst_path, link_dest, prev_fp, db_storage_path):
                try:
                    new_fp[rel_key(db_storage_path, src_path)] = source_sig(src_path)
                except OSError:
                    pass
                results.append(dst_path)
                reused += 1
                if on_progress:
                    on_progress(f"复用未变更: {fname}", prog)
                continue
            key = _resolve_key(keys, src_path, salt_keys)
            if key is None:
                try:
                    p1 = _read_page1(src_path)
                    salt_hex = p1[:SALT_SZ].hex() if len(p1) >= SALT_SZ else '?'
                except OSError:
                    salt_hex = '?'
                unique_count = len(set(v for v in keys.values() if len(v) == 64))
                skipped_missing_key.append(fname)
                if on_progress:
                    on_progress(
                        f"跳过 {fname} (salt={salt_hex}, 已尝试 {unique_count} 个唯一密钥均不匹配)",
                        prog)
                continue

            def _page_progress(cur_pct, detail, fn=fname, idx=i, tot=total):
                if on_progress:
                    on_progress(f"解密 {fn} ({detail} 页)", (idx + cur_pct / 100.0) / tot)

            try:
                new_fp[rel_key(db_storage_path, src_path)] = source_sig(src_path)
            except OSError:
                pass
            try:
                success = decrypt_database(
                    src_path, dst_path, key,
                    print_fn=lambda m, fn=fname: on_progress(f"{fn}: {m}", (i + 0.1) / total) if on_progress else None,
                    progress_fn=_page_progress)
            except Exception:
                success = False

            if not success:
                if on_progress:
                    on_progress(f"跳过 {fname} (解密失败/HMAC不匹配)", prog)
                continue

            results.append(dst_path)
            freshly += 1
            if on_progress:
                on_progress(f"已解密: {fname}", prog)
    else:
        total = 3  # contact + hardlink + sns

    # --- Contact database ---
    contact_src = os.path.join(db_storage_path, 'contact', 'contact.db')
    contact_dst = os.path.join(output_dir, 'contact', 'contact.db')
    if os.path.isfile(contact_src):
        if not _handle_one(contact_src, contact_dst, 'contact.db', 0.94):
            if on_progress and _resolve_key(keys, contact_src, salt_keys) is None:
                on_progress('跳过 contact.db (缺密钥)', 0.94)

    # --- Hardlink database ---
    hardlink_src = os.path.join(db_storage_path, 'hardlink', 'hardlink.db')
    hardlink_dst = os.path.join(output_dir, 'hardlink', 'hardlink.db')
    if os.path.isfile(hardlink_src):
        if not _handle_one(hardlink_src, hardlink_dst, 'hardlink.db', 0.97):
            if on_progress and _resolve_key(keys, hardlink_src, salt_keys) is None:
                on_progress('跳过 hardlink.db (缺密钥)', 0.97)

    # --- SNS / Moments database (optional; key may be missing on older harvests) ---
    sns_src = os.path.join(db_storage_path, 'sns', 'sns.db')
    sns_dst = os.path.join(output_dir, 'sns', 'sns.db')
    if os.path.isfile(sns_src):
        if not _handle_one(sns_src, sns_dst, 'sns.db', 0.99):
            if on_progress and _resolve_key(keys, sns_src, salt_keys) is None:
                on_progress('跳过 sns.db (缺密钥 — 可打开微信朋友圈后重新提取密钥)', 0.99)

    # Persist fingerprints for next incremental run
    try:
        if new_fp:
            save_fingerprint(output_dir, new_fp)
    except Exception:
        pass

    # 自愈：把这次"已验证的密钥 ↔ salt"固化下来，下次（哪怕换账号）也能靠 salt 认出来
    try:
        added = backfill_salt_keys(db_storage_path, keys, salt_keys,
                                   on_progress=(lambda m: on_progress(m, 0.995))
                                   if on_progress else None)
        if added and on_progress:
            on_progress(f"已记录 {added} 个数据库的密钥身份（按 salt，多账号不会互相覆盖）", 0.997)
    except Exception:
        pass

    stats = {'reused': reused, 'decrypted': freshly, 'fingerprint': len(new_fp)}
    if on_progress:
        skipped = total - len(results)
        msg = f"完成: 新解密 {freshly}，复用 {reused}"
        if skipped > 0:
            msg += f"，跳过 {skipped} (密钥缺失/HMAC不匹配)"
        if skipped_missing_key:
            msg += f"\n缺少密钥的数据库: {', '.join(skipped_missing_key)}"
            msg += "\n请确保微信正在运行，然后重新执行密钥提取"
        on_progress(msg, 1.0)
    return results, skipped_missing_key, stats
