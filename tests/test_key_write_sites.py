# -*- coding: utf-8 -*-
"""issue #21 回归：四个"写配置"入口都必须**同时**写 salt 表。

提取链路内部本来就是 `{salt_hex: key_hex}`，只有落盘这一步被拍平成 rel 表 —— 于是多账号
同名库互相覆盖。这里逐个入口钉住"必须带 salt 落盘"。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import config_file  # noqa: E402

SALT_A = 'a1' * 16
KEY_A = '1a' * 32
REL = os.path.join('message', 'message_0.db')
DB_DIR = r'D:\acct_A\db_storage'
PAGE1 = b'\x00' * 4096


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    monkeypatch.setattr(config_file, '_config_path', lambda: str(path))
    return path


def test_key_scan_save_results_writes_salt(cfg, tmp_path):
    """提取密钥（CLI `keys`）落盘时要带 salt。"""
    import key_scan

    db_files = [(REL, str(tmp_path / 'message_0.db'), 1.0, SALT_A, PAGE1)]
    salt_to_dbs = {SALT_A: [REL]}
    key_scan.save_results(db_files, salt_to_dbs, {SALT_A: KEY_A}, DB_DIR,
                          None, lambda *a, **k: None)
    assert config_file.get_db_keys_by_salt() == {SALT_A: KEY_A}
    assert config_file.get_db_keys()[REL] == KEY_A


def test_wechat_key_extract_save_writes_salt(cfg, tmp_path):
    """Web/服务层的密钥提取落盘同样要带 salt。"""
    from engine.services import wechat_key_extract as wke

    db_files = [(REL, str(tmp_path / 'message_0.db'), 1.0, SALT_A, PAGE1)]
    wke.save_key_results(db_files, {SALT_A: [REL]}, {SALT_A: KEY_A}, DB_DIR,
                         lambda *a, **k: None)
    assert config_file.get_db_keys_by_salt() == {SALT_A: KEY_A}


def test_persist_extracted_keys_writes_both_maps(cfg):
    """"key_map 就是 salt→key"的入口（Web 备份的第 4 阶段走这里）。"""
    saved = config_file.persist_extracted_keys({SALT_A: KEY_A}, {SALT_A: [REL]}, DB_DIR)
    assert saved == 1
    assert config_file.get_db_keys_by_salt() == {SALT_A: KEY_A}
    assert config_file.get_db_keys()[REL] == KEY_A


def test_manual_keys_apply_entries_writes_full_salt(cfg, tmp_path, monkeypatch):
    """手动输入密钥页保存时也要带 **完整** salt（页面上显示的 16 位只是截断）。"""
    from engine import manual_keys

    full_salt = SALT_A
    monkeypatch.setattr(manual_keys, 'match_entries', lambda db_dir, entries: [
        {'status': 'matched', 'message': '', 'matched': [
            {'rel': REL, 'name': 'message_0.db', 'salt': full_salt[:16],
             'salt_full': full_salt, 'sizeMb': 1.0}]}])
    monkeypatch.setattr(manual_keys, 'scan_databases', lambda db_dir, with_pages=False: [])
    monkeypatch.setattr(manual_keys, 'status', lambda db_dir: {'ok': 0})
    res = manual_keys.apply_entries(DB_DIR, [{'key': KEY_A}])
    assert res['saved'] == 1
    assert config_file.get_db_keys_by_salt() == {SALT_A: KEY_A}


def test_manual_keys_match_entries_exposes_full_salt(tmp_path, monkeypatch):
    """match_entries 的 matched 条目必须带**完整** salt（页面展示用的 16 位是截断值）。

    落盘要靠完整 salt 才能精确识别文件，否则又退回"按路径撞运气"。
    """
    from engine import manual_keys
    from engine.services import wechat_key_extract as wke

    d = tmp_path / 'message'
    d.mkdir()
    (d / 'message_0.db').write_bytes(b'\x11' * 16 + b'\x00' * (4096 - 16))
    monkeypatch.setattr(wke, 'verify_enc_key', lambda key, page1: True)

    res = manual_keys.match_entries(str(tmp_path),
                                   [{'key': KEY_A, 'error': '', 'db_hint': '', 'salt_hint': ''}])
    matched = res[0]['matched']
    assert matched, '应匹配到 1 个库'
    assert len(matched[0]['salt_full']) == 32, 'salt_full 必须是 32 位 hex'
    assert matched[0]['salt_full'].startswith(matched[0]['salt'])
