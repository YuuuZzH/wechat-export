# -*- coding: utf-8 -*-
"""issue #21 回归：密钥必须能**按 salt** 存储，避免多账号同名库互相覆盖。

背景（真机复现）：
  `.wechat_exp_config.json` 的 `db_keys` 以**相对路径**为键（`message\\message_0.db`），
  而不同微信账号的同名库相对路径完全相同、salt 不同 ⇒ 后备份的账号会顶掉前一个账号的密钥。
  本机双账号实测：账号 1 有 21/22 个库可解，账号 2 只有 1/18（17 个库的密钥已丢失）。

salt 是每个加密库文件的唯一标识（page1 前 16 字节，32 位 hex），提取链路内部本来就以
salt 为键（`key_map: {salt_hex: key}`），只有"写配置"这一步把它拍平丢了。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import config_file  # noqa: E402

REL = os.path.join('message', 'message_0.db')
SALT_A = 'a1' * 16
SALT_B = 'b2' * 16
KEY_A = '1a' * 32
KEY_B = '2b' * 32
DIR_A = r'D:\acct_A\db_storage'
DIR_B = r'D:\acct_B\db_storage'


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    path = tmp_path / 'config.json'
    monkeypatch.setattr(config_file, '_config_path', lambda: str(path))
    return path


def _raw(path):
    with open(str(path), encoding='utf-8') as f:
        return json.load(f)


def test_two_accounts_same_rel_path_keep_both_keys(cfg):
    """两个账号的同名库：salt 不同 ⇒ 两个密钥都必须留着。"""
    config_file.set_db_keys({REL: KEY_A}, db_dir=DIR_A, salt_keys={SALT_A: KEY_A})
    config_file.set_db_keys({REL: KEY_B}, db_dir=DIR_B, salt_keys={SALT_B: KEY_B})

    assert config_file.get_db_keys_by_salt() == {SALT_A: KEY_A, SALT_B: KEY_B}
    # 老字段保留（兼容旧读取方），语义仍是"该相对路径最后一次写入的密钥"
    assert config_file.get_db_keys()[REL] == KEY_B


def test_salt_keys_are_additive_and_idempotent(cfg):
    """重复写入同一 salt 不产生重复项，且不丢其它 salt。"""
    config_file.set_db_keys({REL: KEY_A}, salt_keys={SALT_A: KEY_A})
    config_file.set_db_keys({REL: KEY_B}, salt_keys={SALT_B: KEY_B})
    config_file.set_db_keys({REL: KEY_B}, salt_keys={SALT_B: KEY_B})
    assert config_file.get_db_keys_by_salt() == {SALT_A: KEY_A, SALT_B: KEY_B}


def test_db_dirs_accumulate_across_accounts(cfg):
    """_db_dir 保留"最近一次"，但新增 _db_dirs 记录所有见过的账号目录。"""
    config_file.set_db_keys({REL: KEY_A}, db_dir=DIR_A, salt_keys={SALT_A: KEY_A})
    config_file.set_db_keys({REL: KEY_B}, db_dir=DIR_B, salt_keys={SALT_B: KEY_B})
    raw = _raw(cfg)
    assert raw['_db_dir'] == DIR_B
    assert raw['_db_dirs'] == [DIR_A, DIR_B]


def test_legacy_config_without_salt_field_is_supported(cfg):
    """老配置没有 db_keys_by_salt：读取返回空 dict，不报错。"""
    with open(str(cfg), 'w', encoding='utf-8') as f:
        json.dump({'db_keys': {REL: KEY_A}, '_db_dir': DIR_A}, f)
    assert config_file.get_db_keys_by_salt() == {}
    assert config_file.get_db_keys() == {REL: KEY_A}


def test_set_db_keys_without_salt_keys_still_works(cfg):
    """老调用方式（不传 salt_keys）必须保持可用。"""
    config_file.set_db_keys({REL: KEY_A}, db_dir=DIR_A)
    assert config_file.get_db_keys() == {REL: KEY_A}
    assert config_file.get_db_keys_by_salt() == {}


def test_salt_key_entries_survive_other_rel_key_writes(cfg):
    """只写 rel 键（不带 salt）也不能把已有 salt 键抹掉。"""
    config_file.set_db_keys({REL: KEY_A}, salt_keys={SALT_A: KEY_A})
    config_file.set_db_keys({'contact\\contact.db': 'c3' * 32})
    assert config_file.get_db_keys_by_salt() == {SALT_A: KEY_A}
