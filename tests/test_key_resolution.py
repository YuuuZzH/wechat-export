# -*- coding: utf-8 -*-
"""issue #21 回归：密钥解析必须**先按 salt 精确匹配**，且 basename 命中也要校验。

原逻辑（`decryptor._resolve_key`）：basename 命中就**直接返回、不做任何校验**，
只有 basename 没命中时才走 HMAC 全量试探。多账号场景下 basename 命中的是**另一个账号**的
同名库密钥 ⇒ 解密必然失败，而本可救回来的 HMAC 回退根本不会执行。

这里用可预测的假 HMAC 校验（不涉及真实密钥/真实数据）测"顺序与回退"这一逻辑本身。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backup import decryptor  # noqa: E402

SALT_A_BYTES = bytes(range(16))          # 000102…0f
SALT_B_BYTES = bytes(range(16, 32))      # 101112…1f
SALT_A = SALT_A_BYTES.hex()
SALT_B = SALT_B_BYTES.hex()
KEY_A = b'A' * 32
KEY_B = b'B' * 32
KEY_WRONG = b'W' * 32

PAGE_SZ = decryptor.PAGE_SZ
PAGE1_A = SALT_A_BYTES + b'\x00' * (PAGE_SZ - 16)
PAGE1_B = SALT_B_BYTES + b'\x00' * (PAGE_SZ - 16)

REL = os.path.join('message', 'message_0.db')
DB_A = r'D:\acct_A\db_storage\message\message_0.db'
DB_B = r'D:\acct_B\db_storage\message\message_0.db'

PAGES = {os.path.normcase(DB_A): PAGE1_A, os.path.normcase(DB_B): PAGE1_B}
# 假 HMAC：密钥必须与 page1 的 salt 登记一致才算通过
KEYS_BY_SALT = {SALT_A: KEY_A, SALT_B: KEY_B}


@pytest.fixture()
def fake_crypto(monkeypatch):
    calls = {'verify': 0}

    def fake_read_page1(path):
        page = PAGES.get(os.path.normcase(path))
        if page is None:
            raise OSError(path)
        return page

    def fake_verify(key, page1):
        calls['verify'] += 1
        if len(page1) < 16 or not isinstance(key, (bytes, bytearray)):
            return False
        return KEYS_BY_SALT.get(bytes(page1[:16]).hex()) == bytes(key)

    monkeypatch.setattr(decryptor, '_read_page1', fake_read_page1)
    monkeypatch.setattr(decryptor, '_verify_enc_key', fake_verify)
    return calls


def test_resolver_prefers_salt_match_over_basename(fake_crypto):
    """同名 basename 的密钥是"另一个账号"的：必须按 salt 选对。"""
    rel_keys = {REL: KEY_WRONG.hex()}
    salt_keys = {SALT_A: KEY_A.hex(), SALT_B: KEY_B.hex()}
    assert decryptor._resolve_key(rel_keys, DB_A, salt_keys) == KEY_A
    assert decryptor._resolve_key(rel_keys, DB_B, salt_keys) == KEY_B


def test_resolver_rejects_unverified_basename_key(fake_crypto):
    """basename 命中但校验不过时，必须继续尝试（不能直接返回错密钥）。"""
    rel_keys = {REL: KEY_WRONG.hex()}
    assert decryptor._resolve_key(rel_keys, DB_A, None) is None


def test_resolver_falls_back_to_hmac_when_basename_key_is_wrong(fake_crypto):
    """错密钥占了 basename，真密钥在别的路径下 ⇒ 仍要通过 HMAC 回退找回来。"""
    rel_keys = {REL: KEY_WRONG.hex(), os.path.join('contact', 'contact.db'): KEY_A.hex()}
    assert decryptor._resolve_key(rel_keys, DB_A, None) == KEY_A


def test_resolver_hmac_fallback_searches_salt_keys_too(fake_crypto):
    """真密钥只存在于 salt 表中（rel 表里没有）时也能找回。"""
    rel_keys = {REL: KEY_WRONG.hex()}
    salt_keys = {SALT_A: KEY_A.hex()}
    # salt 表已能直接命中，这里把 salt 条目挪到一个不匹配的 salt 上，强制走 HMAC 回退
    salt_keys_other = {'ff' * 16: KEY_A.hex()}
    assert decryptor._resolve_key(rel_keys, DB_A, salt_keys) == KEY_A
    assert decryptor._resolve_key(rel_keys, DB_A, salt_keys_other) == KEY_A


def test_resolver_returns_none_when_nothing_matches(fake_crypto):
    """一个都对不上时返回 None（调用方据此报告"缺少密钥"，而不是拿错密钥硬解）。"""
    assert decryptor._resolve_key({REL: KEY_WRONG.hex()}, DB_B, {}) is None


def test_resolver_keeps_fast_path_for_verified_basename(fake_crypto):
    """basename 命中且校验通过时仍走快路径（不要退化成全量扫描）。"""
    rel_keys = {REL: KEY_A.hex()}
    assert decryptor._resolve_key(rel_keys, DB_A, None) == KEY_A
    assert fake_crypto['verify'] <= 2, '快路径不应触发全量试探'


def test_resolve_key_for_loads_keys_from_config(monkeypatch, fake_crypto):
    """对外入口要同时读 rel 表与 salt 表。"""
    from engine import config_file

    monkeypatch.setattr(config_file, 'get_db_keys',
                        lambda: {REL: KEY_WRONG.hex()})
    monkeypatch.setattr(config_file, 'get_db_keys_by_salt',
                        lambda: {SALT_B: KEY_B.hex()})
    assert decryptor.resolve_key_for(DB_B) == KEY_B
