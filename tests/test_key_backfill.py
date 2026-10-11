# -*- coding: utf-8 -*-
"""issue #21 回归：**按证明回填 salt** —— 老配置只有 rel 表时，应自动补齐 salt 表。

这样：① 历史用户无需任何操作就能获得"按 salt 区分"的能力；
     ② 已经发生过覆盖的账号，只要在能取到密钥的场合（登录该账号时）跑一次，就会把
        「哪个 salt 属于哪个密钥」这一事实固化下来，之后再切账号也不会丢。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backup import decryptor  # noqa: E402
from engine import config_file  # noqa: E402

PAGE_SZ = decryptor.PAGE_SZ
SALT_A_BYTES = bytes(range(16))
SALT_B_BYTES = bytes(range(16, 32))
SALT_A, SALT_B = SALT_A_BYTES.hex(), SALT_B_BYTES.hex()
KEY_A, KEY_B, KEY_WRONG = b'A' * 32, b'B' * 32, b'W' * 32
REL = os.path.join('message', 'message_0.db')
REL_B = os.path.join('message', 'media_0.db')


def _page(salt_bytes):
    return salt_bytes + b'\x00' * (PAGE_SZ - 16)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """两个"账号"目录各含一个同名库（salt/密钥都不同）+ 假 HMAC。"""
    cfg = tmp_path / 'cfg.json'
    monkeypatch.setattr(config_file, '_config_path', lambda: str(cfg))

    acct_a = tmp_path / 'acct_A' / 'db_storage'
    acct_b = tmp_path / 'acct_B' / 'db_storage'
    pages = {}
    for root, salt, keys in ((acct_a, SALT_A_BYTES, {REL: KEY_A}),
                             (acct_b, SALT_B_BYTES, {KEY_A: KEY_A})):
        d = root / 'message'
        d.mkdir(parents=True)
        p = d / 'message_0.db'
        p.write_bytes(_page(salt) + b'x' * 100)
        pages[os.path.normcase(str(p))] = _page(salt)

    def fake_read_page1(path):
        page = pages.get(os.path.normcase(str(path)))
        if page is None:
            raise OSError(path)
        return page

    def fake_verify(key, page1):
        if len(page1) < 16 or not isinstance(key, (bytes, bytearray)):
            return False
        return {SALT_A: KEY_A, SALT_B: KEY_B}.get(bytes(page1[:16]).hex()) == bytes(key)

    monkeypatch.setattr(decryptor, '_read_page1', fake_read_page1)
    monkeypatch.setattr(decryptor, '_verify_enc_key', fake_verify)
    return {'cfg': cfg, 'acct_a': str(acct_a), 'acct_b': str(acct_b),
            'db_a': str(acct_a / 'message' / 'message_0.db'),
            'db_b': str(acct_b / 'message' / 'message_0.db')}


def test_backfill_writes_salt_for_verified_key(env, tmp_path):
    """老配置只有 rel 键：回填后必须出现对应的 salt 条目。"""
    config_file.set_db_keys({REL: KEY_A.hex()}, db_dir=env['acct_a'])
    added = decryptor.backfill_salt_keys(env['acct_a'])
    assert added == 1
    assert config_file.get_db_keys_by_salt() == {SALT_A: KEY_A.hex()}


def test_backfill_is_idempotent(env):
    config_file.set_db_keys({REL: KEY_A.hex()}, db_dir=env['acct_a'])
    assert decryptor.backfill_salt_keys(env['acct_a']) == 1
    assert decryptor.backfill_salt_keys(env['acct_a']) == 0
    assert config_file.get_db_keys_by_salt() == {SALT_A: KEY_A.hex()}


def test_backfill_skips_unverifiable_keys(env):
    """密钥对不上某个库时不能瞎写（宁可少写，不可写错）。"""
    config_file.set_db_keys({REL: KEY_WRONG.hex()}, db_dir=env['acct_a'])
    assert decryptor.backfill_salt_keys(env['acct_a']) == 0
    assert config_file.get_db_keys_by_salt() == {}


def test_backfill_keeps_both_accounts_after_second_run(env):
    """关键场景：两个账号先后各自回填 ⇒ 两个 salt 的密钥都在。"""
    config_file.set_db_keys({REL: KEY_A.hex()}, db_dir=env['acct_a'])
    decryptor.backfill_salt_keys(env['acct_a'])
    config_file.set_db_keys({REL: KEY_B.hex()}, db_dir=env['acct_b'])
    decryptor.backfill_salt_keys(env['acct_b'])
    assert config_file.get_db_keys_by_salt() == {SALT_A: KEY_A.hex(), SALT_B: KEY_B.hex()}


def test_decrypt_for_backup_backfills_salt_keys(env, monkeypatch, tmp_path):
    """备份流程本身要顺手回填（用户无需额外操作）。"""
    from engine import decrypt as engine_decrypt

    monkeypatch.setattr(engine_decrypt, 'decrypt_database',
                        lambda src, dst, key, **kw: True)
    config_file.set_db_keys({REL: KEY_A.hex()}, db_dir=env['acct_a'])
    out = tmp_path / 'out'
    ok, skipped = decryptor.decrypt_for_backup(env['acct_a'], str(out),
                                              config_file.get_db_keys())
    assert skipped == []
    assert config_file.get_db_keys_by_salt() == {SALT_A: KEY_A.hex()}


def test_decrypt_for_backup_uses_salt_keys_when_rel_key_is_other_account(env, monkeypatch, tmp_path):
    """账号 B 的库：rel 表里是账号 A 的错密钥，但 salt 表里有 B 的正确密钥 ⇒ 必须解开。"""
    from engine import decrypt as engine_decrypt

    used = []
    monkeypatch.setattr(engine_decrypt, 'decrypt_database',
                        lambda src, dst, key, **kw: used.append(key) or True)
    config_file.set_db_keys({REL: KEY_A.hex()}, db_dir=env['acct_a'])
    config_file.set_db_keys({REL: KEY_A.hex()}, db_dir=env['acct_b'],
                            salt_keys={SALT_B: KEY_B.hex()})
    ok, skipped = decryptor.decrypt_for_backup(env['acct_b'], str(tmp_path / 'out2'),
                                              config_file.get_db_keys())
    assert skipped == [], '有 salt 密钥时不该再跳过'
    assert used and used[0] == KEY_B
