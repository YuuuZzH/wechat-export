# -*- coding: utf-8 -*-
"""issue #21 回归：其余三个消费端也必须用 **salt 感知**的解析。

消费端共五处，除了 backup 的 `decryptor`（已覆盖）还有：
  * `engine.decrypt.run_decrypt`（解密页 / `decrypt` 命令）
  * `services/media._decrypt_media_db_on_the_fly`（语音/图片库按需解密）
  * `wechat_key_extract.load_from_config`（提取前先看配置里有没有现成密钥）
这三处原本都是「basename 命中就用」甚至「随便取一个键」，多账号下必然拿错密钥。
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


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """账号 A 的库（salt/密钥 A）；配置的 rel 表里放**账号 B/错的**密钥，salt 表里放 A 的。"""
    cfg = tmp_path / 'cfg.json'
    monkeypatch.setattr(config_file, '_config_path', lambda: str(cfg))

    db_dir = tmp_path / 'acct_A' / 'db_storage'
    msg = db_dir / 'message'
    msg.mkdir(parents=True)
    page1 = SALT_A_BYTES + b'\x00' * (PAGE_SZ - 16)
    (msg / 'message_0.db').write_bytes(page1 + b'x' * 64)

    def fake_read_page1(path):
        if os.path.normcase(str(path)) == os.path.normcase(str(msg / 'message_0.db')):
            return page1
        raise OSError(str(path))

    def fake_verify(key, page):
        if len(page) < 16 or not isinstance(key, (bytes, bytearray)):
            return False
        return {SALT_A: KEY_A}.get(bytes(page[:16]).hex()) == bytes(key)

    monkeypatch.setattr(decryptor, '_read_page1', fake_read_page1)
    monkeypatch.setattr(decryptor, '_verify_enc_key', fake_verify)
    return {'db_dir': str(db_dir), 'db': str(msg / 'message_0.db')}


def test_run_decrypt_prefers_salt_key(env, tmp_path, monkeypatch):
    """解密页/CLI：rel 表是错密钥、salt 表正确 ⇒ 必须用 salt 表那把解开。"""
    import sqlite3

    from engine import decrypt as engine_decrypt

    used = []

    def fake_decrypt(src, dst, key, **kw):
        used.append(key)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        conn = sqlite3.connect(dst)          # run_decrypt 会用 sqlite 打开产物校验
        conn.execute('CREATE TABLE t (x)')
        conn.commit()
        conn.close()
        return True

    monkeypatch.setattr(engine_decrypt, 'decrypt_database', fake_decrypt)
    config_file.set_db_keys({REL: KEY_WRONG.hex()}, db_dir=env['db_dir'],
                            salt_keys={SALT_A: KEY_A.hex()})
    out = tmp_path / 'out'
    ok, failed, skipped = engine_decrypt.run_decrypt(db_dir=env['db_dir'], out_dir=str(out),
                                                    print_fn=lambda *a, **k: None,
                                                    progress_fn=lambda *a, **k: None)
    assert used and used[0] == KEY_A, '应按 salt 命中账号 A 的正确密钥'
    assert (ok, failed, skipped) == (1, 0, 0)


def test_run_decrypt_reports_skip_instead_of_using_wrong_key(env, tmp_path, monkeypatch):
    """只有错密钥时必须报"跳过"，不能拿错密钥硬解。"""
    from engine import decrypt as engine_decrypt

    used = []
    monkeypatch.setattr(engine_decrypt, 'decrypt_database',
                        lambda src, dst, key, **kw: used.append(key) or False)
    config_file.set_db_keys({REL: KEY_WRONG.hex()}, db_dir=env['db_dir'])
    ok, failed, skipped = engine_decrypt.run_decrypt(db_dir=env['db_dir'],
                                                    out_dir=str(tmp_path / 'o2'),
                                                    print_fn=lambda *a, **k: None,
                                                    progress_fn=lambda *a, **k: None)
    assert not used, '不该用错密钥去解密'
    assert ok == 0 and skipped >= 1


def test_media_decrypt_uses_salt_key(env, tmp_path, monkeypatch):
    """媒体库按需解密：同样要按 salt 选密钥。"""
    from engine.services import media

    used = []
    monkeypatch.setattr(media, 'decrypt_database',
                        lambda src, dst, key, **kw: used.append(key) or True,
                        raising=False)
    import engine.decrypt as engine_decrypt
    monkeypatch.setattr(engine_decrypt, 'decrypt_database',
                        lambda src, dst, key, **kw: used.append(key) or True)
    config_file.set_db_keys({REL: KEY_WRONG.hex()}, db_dir=env['db_dir'],
                            salt_keys={SALT_A: KEY_A.hex()})
    got = media._decrypt_media_db_on_the_fly(env['db'], str(tmp_path / 'dec'))
    assert got, '应成功解密（返回目标路径）'
    assert used and used[0] == KEY_A
    assert b'' not in used


def test_media_decrypt_returns_none_without_key(env, tmp_path):
    """没有可用密钥时返回 None，不要瞎试。"""
    from engine.services import media

    config_file.set_db_keys({REL: KEY_WRONG.hex()}, db_dir=env['db_dir'])
    assert media._decrypt_media_db_on_the_fly(env['db'], str(tmp_path / 'dec2')) is None


def test_key_extract_load_from_config_prefers_salt(env, monkeypatch):
    """提取前的"先看配置"也要认 salt 表（老配置只有 rel 表时才退回路径匹配）。"""
    from engine.services import wechat_key_extract as wke

    monkeypatch.setattr(config_file, 'get_db_keys_by_salt', lambda: {SALT_A: KEY_A.hex()})
    monkeypatch.setattr(wke, 'verify_enc_key', lambda key, page1: True)
    page1 = SALT_A_BYTES + b'\x00' * (PAGE_SZ - 16)
    db_files = [(REL, env['db'], 1.0, SALT_A, page1)]
    key_map = {}
    wke.load_from_config(env['db_dir'], db_files, {SALT_A: [REL]}, key_map,
                         lambda *a, **k: None)
    assert key_map == {SALT_A: KEY_A.hex()}


def test_manual_keys_message_hints_multi_account_overwrite(tmp_path, monkeypatch):
    """手动密钥页说"不匹配"时，必须提醒**多账号互相覆盖**这一最常见原因。

    原提示只说"很可能来自另一台机器或旧版本微信"，会把多账号用户带偏（issue #21 第 3/4 点）。
    """
    from engine import manual_keys
    from engine.services import wechat_key_extract as wke

    d = tmp_path / 'message'
    d.mkdir()
    (d / 'message_0.db').write_bytes(b'\x11' * 16 + b'\x00' * (4096 - 16))
    monkeypatch.setattr(wke, 'verify_enc_key', lambda key, page1: False)

    res = manual_keys.match_entries(str(tmp_path), [
        {'key': KEY_WRONG.hex(), 'error': '', 'db_hint': '', 'salt_hint': ''}])
    msg = res[0]['message']
    assert '另一个账号' in msg or '多账号' in msg, msg
