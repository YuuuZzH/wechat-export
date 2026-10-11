# -*- coding: utf-8 -*-
"""Encrypted-source fingerprints for incremental backup.

If an encrypted WeChat DB's (size, mtime, wal_size) matches the previous
backup's recorded fingerprint and the previous decrypted file still exists,
we hardlink/copy that decrypted file instead of re-decrypting.
"""
from __future__ import annotations

import json
import os
import shutil
from typing import Optional

FINGERPRINT_REL = os.path.join('data', 'source_fingerprint.json')


def fingerprint_path(backup_dir: str) -> str:
    return os.path.join(backup_dir, FINGERPRINT_REL)


def source_sig(enc_path: str) -> dict:
    """Signature of an encrypted DB (+ optional -wal size)."""
    st = os.stat(enc_path)
    mtime_ns = getattr(st, 'st_mtime_ns', int(st.st_mtime * 1_000_000_000))
    wal = enc_path + '-wal'
    wal_size = 0
    if os.path.isfile(wal):
        try:
            wal_size = os.path.getsize(wal)
        except OSError:
            wal_size = 0
    return {
        'size': int(st.st_size),
        'mtime_ns': int(mtime_ns),
        'wal_size': int(wal_size),
    }


def load_fingerprint(backup_dir: Optional[str]) -> dict:
    if not backup_dir:
        return {}
    path = fingerprint_path(backup_dir)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        files = data.get('files') if isinstance(data, dict) else None
        return files if isinstance(files, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def save_fingerprint(backup_dir: str, files: dict) -> None:
    path = fingerprint_path(backup_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        'version': 1,
        'files': files,
    }
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def rel_key(db_storage_path: str, enc_path: str) -> str:
    """Relative key under db_storage, e.g. message/message_0.db."""
    rel = os.path.relpath(enc_path, db_storage_path)
    return rel.replace('\\', '/')


def try_reuse_decrypted(
    enc_path: str,
    dst_path: str,
    link_dest: Optional[str],
    prev_fp: dict,
    db_storage_path: str,
) -> bool:
    """Reuse decrypted DB from previous backup when encrypted source unchanged.

    Returns True if dst_path is ready (reused); False if caller must decrypt.
    """
    if not link_dest or not prev_fp:
        return False
    key = rel_key(db_storage_path, enc_path)
    prev = prev_fp.get(key)
    if not isinstance(prev, dict):
        return False
    try:
        cur = source_sig(enc_path)
    except OSError:
        return False
    if (cur.get('size') != prev.get('size')
            or cur.get('mtime_ns') != prev.get('mtime_ns')
            or cur.get('wal_size') != prev.get('wal_size')):
        return False

    prev_db = os.path.join(link_dest, *key.split('/'))
    link_abs = os.path.abspath(link_dest)
    out_abs = os.path.abspath(os.path.join(dst_path, os.pardir, os.pardir))
    # contact/hardlink live one level deeper than message/*.db — normalize to backup root
    # dst: <out>/message/x.db → parents[2]=out; <out>/contact/contact.db → parents[2]=out
    # already correct via join(dst, .., ..)

    # In-place / same-path: decrypted file already present
    try:
        if os.path.isfile(dst_path) and os.path.getsize(dst_path) > 0:
            if os.path.normcase(link_abs) == os.path.normcase(out_abs):
                return True
            if os.path.isfile(prev_db) and os.path.samefile(dst_path, prev_db):
                return True
    except OSError:
        pass

    if not os.path.isfile(prev_db) or os.path.getsize(prev_db) <= 0:
        return False

    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    if os.path.exists(dst_path):
        try:
            if os.path.samefile(prev_db, dst_path):
                return True
        except OSError:
            pass
        try:
            os.remove(dst_path)
        except OSError:
            return False
    try:
        os.link(prev_db, dst_path)
    except OSError:
        try:
            shutil.copy2(prev_db, dst_path)
        except OSError:
            return False
    return os.path.isfile(dst_path) and os.path.getsize(dst_path) > 0


def resolve_link_dest(
    output_dir: str,
    incremental: bool,
    link_dest: Optional[str] = None,
    wxid: Optional[str] = None,
) -> Optional[str]:
    """Pick previous backup root for incremental reuse."""
    if link_dest and os.path.isdir(os.path.join(link_dest, 'message')):
        return os.path.abspath(link_dest)
    if not incremental:
        return None

    from engine.config_file import get_backup_data_dir, get_backup_wxid, get_latest_backup_dir

    out_abs = os.path.abspath(output_dir)
    prev = get_backup_data_dir()
    if prev and os.path.isdir(os.path.join(prev, 'message')):
        prev_wx = get_backup_wxid()
        if not (wxid and prev_wx and prev_wx != wxid):
            return os.path.abspath(prev)

    parent = os.path.dirname(out_abs)
    latest = get_latest_backup_dir(parent)
    if latest:
        return os.path.abspath(latest)
    return None
