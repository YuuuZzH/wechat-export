# -*- coding: utf-8 -*-
"""Incremental backup: fingerprint skip + link_dest resolution."""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(ROOT, 'src'))

from backup import fingerprint as fp  # noqa: E402


def _touch(path: str, data: bytes = b'hello'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)


def test_source_sig_includes_wal(tmp_path):
    enc = tmp_path / 'message_0.db'
    enc.write_bytes(b'x' * 100)
    assert fp.source_sig(str(enc))['wal_size'] == 0
    (tmp_path / 'message_0.db-wal').write_bytes(b'wal')
    assert fp.source_sig(str(enc))['wal_size'] == 3


def test_try_reuse_when_fingerprint_matches(tmp_path):
    storage = tmp_path / 'db_storage'
    prev = tmp_path / 'backup_old'
    out = tmp_path / 'backup_new'
    enc = storage / 'message' / 'message_0.db'
    prev_db = prev / 'message' / 'message_0.db'
    dst = out / 'message' / 'message_0.db'
    _touch(str(enc), b'enc-data')
    _touch(str(prev_db), b'decrypted-payload')

    sig = fp.source_sig(str(enc))
    fp.save_fingerprint(str(prev), {'message/message_0.db': sig})
    prev_fp = fp.load_fingerprint(str(prev))

    assert fp.try_reuse_decrypted(
        str(enc), str(dst), str(prev), prev_fp, str(storage))
    assert dst.is_file()
    assert dst.read_bytes() == b'decrypted-payload'


def test_try_reuse_skips_when_source_changed(tmp_path):
    storage = tmp_path / 'db_storage'
    prev = tmp_path / 'backup_old'
    out = tmp_path / 'backup_new'
    enc = storage / 'message' / 'message_0.db'
    prev_db = prev / 'message' / 'message_0.db'
    dst = out / 'message' / 'message_0.db'
    _touch(str(enc), b'enc-data')
    _touch(str(prev_db), b'decrypted-payload')

    # fingerprint from older size
    fp.save_fingerprint(str(prev), {
        'message/message_0.db': {'size': 1, 'mtime_ns': 1, 'wal_size': 0},
    })
    prev_fp = fp.load_fingerprint(str(prev))
    assert not fp.try_reuse_decrypted(
        str(enc), str(dst), str(prev), prev_fp, str(storage))
    assert not dst.exists()


def test_resolve_link_dest_uses_latest_sibling(tmp_path, monkeypatch):
    root = tmp_path / 'backup'
    old = root / '2026-01-01'
    new = root / '2026-01-02'
    _touch(str(old / 'message' / 'message_0.db'), b'a')
    os.makedirs(new, exist_ok=True)

    monkeypatch.setattr('engine.config_file.get_backup_data_dir', lambda: None)
    monkeypatch.setattr('engine.config_file.get_backup_wxid', lambda: None)

    got = fp.resolve_link_dest(str(new), incremental=True)
    assert got and os.path.samefile(got, old)


def test_resolve_link_dest_disabled_without_explicit(tmp_path):
    root = tmp_path / 'backup'
    old = root / '2026-01-01'
    new = root / '2026-01-02'
    _touch(str(old / 'message' / 'message_0.db'), b'a')
    os.makedirs(new, exist_ok=True)
    assert fp.resolve_link_dest(str(new), incremental=False) is None
