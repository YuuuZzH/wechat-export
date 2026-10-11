"""Tests for engine/manual_keys.py — 手动输入密钥的解析、匹配与保存。

所有测试都用**合成**的 SQLCipher 页面与随机密钥，不涉及任何真实数据。
"""
import hashlib
import hmac as hmac_mod
import json
import os
import struct

import pytest

from engine import config_file
from engine import manual_keys as mk

PAGE_SZ = 4096
SALT_SZ = 16
KEY_SZ = 32

KEY_A = "11" * 32
KEY_B = "22" * 32
KEY_C = "ab" * 32


def make_page1(key_hex, salt=b"0123456789abcdef"):
    """构造一个 HMAC 合法（但内容随机）的 SQLCipher page1。"""
    body = os.urandom(PAGE_SZ - SALT_SZ - 64)
    mac_key = hashlib.pbkdf2_hmac("sha512", bytes.fromhex(key_hex),
                                  bytes(b ^ 0x3A for b in salt), 2, dklen=KEY_SZ)
    # HMAC 覆盖 page1[16 : 4032]（salt 之后、IV 之前），不含 salt
    hm = hmac_mod.new(mac_key, body, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return salt + body + hm.digest()


@pytest.fixture
def db_dir(tmp_path):
    """两个数据库：message/message_0.db（KEY_A）与 contact/contact.db（KEY_B）。"""
    root = tmp_path / "db_storage"
    (root / "message").mkdir(parents=True)
    (root / "contact").mkdir(parents=True)
    (root / "message" / "message_0.db").write_bytes(make_page1(KEY_A, b"a" * 16))
    (root / "message" / "message_1.db").write_bytes(make_page1(KEY_C, b"c" * 16))
    (root / "contact" / "contact.db").write_bytes(make_page1(KEY_B, b"b" * 16))
    return str(root)


@pytest.fixture
def clean_config(tmp_path, monkeypatch):
    """把配置文件重定向到临时目录，绝不碰用户真实配置。"""
    cfg_path = str(tmp_path / "cfg" / ".wechat_exp_config.json")
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    monkeypatch.setattr(config_file, "_config_path", lambda: cfg_path)
    return cfg_path


class TestParse:
    def test_plain_hex(self):
        e = mk.parse_entries(KEY_A)[0]
        assert e["key"] == KEY_A
        assert e["db_hint"] is None and e["salt_hint"] is None and e["error"] is None

    def test_uppercase_is_kept_as_hex(self):
        e = mk.parse_entries(KEY_A.upper())[0]
        assert e["key"].lower() == KEY_A

    def test_db_hint_equals(self):
        e = mk.parse_entries("message_0.db = " + KEY_A)[0]
        assert e["key"] == KEY_A
        assert e["db_hint"] == "message_0.db"

    def test_db_hint_path_and_colon(self):
        e = mk.parse_entries("message/message_1.db: " + KEY_C)[0]
        assert e["db_hint"] == "message/message_1.db"

    def test_wechat_blob_key_plus_salt(self):
        salt = "de" * 16
        e = mk.parse_entries("x'" + KEY_A + salt + "'")[0]
        assert e["key"] == KEY_A
        assert e["salt_hint"] == salt

    def test_grouped_hex_with_spaces(self):
        spaced = " ".join(KEY_B[i:i + 8] for i in range(0, 64, 8))
        e = mk.parse_entries(spaced)[0]
        assert e["key"].lower() == KEY_B

    def test_salt_hint_only(self):
        salt = "7f" * 16
        e = mk.parse_entries(salt + " " + KEY_A)[0]
        assert e["key"] == KEY_A
        assert e["salt_hint"] == salt

    def test_comments_and_blank_lines_are_skipped(self):
        text = "# 注释\n\n   \n// 另一种注释\n" + KEY_A
        entries = mk.parse_entries(text)
        assert len(entries) == 1

    def test_invalid_line_reports_error(self):
        e = mk.parse_entries("这不是密钥")[0]
        assert e["key"] is None and e["error"]

    def test_multiple_lines(self):
        text = KEY_A + "\n" + KEY_B + "\n" + "message_0.db = " + KEY_C
        entries = mk.parse_entries(text)
        assert [x["key"] for x in entries] == [KEY_A, KEY_B, KEY_C]


class TestMask:
    def test_mask_hides_middle(self):
        assert mk.mask_key(KEY_A) == "111111...1111"
        assert KEY_A not in mk.mask_key(KEY_A)

    def test_mask_short(self):
        assert mk.mask_key("") == "***"


class TestScanAndStatus:
    def test_scan_lists_all_databases(self, db_dir, clean_config):
        dbs = mk.scan_databases(db_dir)
        names = sorted(d["rel_norm"] for d in dbs)
        assert names == ["contact/contact.db", "message/message_0.db", "message/message_1.db"]

    def test_status_all_missing(self, db_dir, clean_config):
        st = mk.status(db_dir)
        assert st["total"] == 3 and st["verified"] == 0 and st["missing"] == 3
        assert st["plain"] == 0

    def test_empty_dir(self, tmp_path, clean_config):
        st = mk.status(str(tmp_path / "nope"))
        assert st["total"] == 0

    def test_plain_sqlite_needs_no_key(self, tmp_path, clean_config):
        root = tmp_path / "plain"
        root.mkdir()
        (root / "plain.db").write_bytes(b"SQLite format 3" + bytes(1) + bytes(PAGE_SZ - 16))
        st = mk.status(str(root))
        assert st["total"] == 1
        assert st["plain"] == 1
        assert st["missing"] == 0
        assert st["databases"][0]["plain"] is True


class TestMatch:
    def test_generic_key_matches_right_db(self, db_dir, clean_config):
        res = mk.match_entries(db_dir, mk.parse_entries(KEY_A))
        assert res[0]["status"] == "matched"
        assert [m["name"] for m in res[0]["matched"]] == ["message_0.db"]

    def test_wrong_key_does_not_match(self, db_dir, clean_config):
        res = mk.match_entries(db_dir, mk.parse_entries("33" * 32))
        assert res[0]["status"] == "no_match"
        assert res[0]["matched"] == []

    def test_db_hint_scopes_match(self, db_dir, clean_config):
        res = mk.match_entries(db_dir, mk.parse_entries("contact.db = " + KEY_B))
        assert res[0]["status"] == "matched"
        assert res[0]["matched"][0]["rel"].replace("\\", "/") == "contact/contact.db"

    def test_db_hint_with_wrong_key(self, db_dir, clean_config):
        res = mk.match_entries(db_dir, mk.parse_entries("contact.db = " + KEY_A))
        assert res[0]["status"] == "no_match"

    def test_unknown_db_hint(self, db_dir, clean_config):
        res = mk.match_entries(db_dir, mk.parse_entries("nope.db = " + KEY_A))
        assert res[0]["status"] == "db_not_found"

    def test_salt_hint_scopes_match(self, db_dir, clean_config):
        dbs = mk.scan_databases(db_dir)
        salt = [d for d in dbs if d["name"] == "message_1.db"][0]["salt"]
        res = mk.match_entries(db_dir, mk.parse_entries(salt + " " + KEY_C))
        assert res[0]["status"] == "matched"
        assert res[0]["matched"][0]["name"] == "message_1.db"

    def test_results_never_leak_full_key(self, db_dir, clean_config):
        res = mk.match_entries(db_dir, mk.parse_entries(KEY_A))
        assert KEY_A not in json.dumps(res)
        assert res[0]["line"] == 1
        assert res[0]["keyMasked"].endswith("1111")


class TestApplyAndRemove:
    def test_apply_saves_verified_keys(self, db_dir, clean_config):
        out = mk.apply_entries(db_dir, mk.parse_entries(KEY_A + "\n" + KEY_B))
        assert out["saved"] == 2
        assert out["status"]["verified"] == 2
        saved = json.load(open(clean_config, encoding="utf-8"))["db_keys"]
        assert set(saved.values()) == {KEY_A, KEY_B}

    def test_unverified_is_not_saved(self, db_dir, clean_config):
        out = mk.apply_entries(db_dir, mk.parse_entries("44" * 32))
        assert out["saved"] == 0
        assert out["status"]["verified"] == 0

    def test_force_saves_unverified_when_db_given(self, db_dir, clean_config):
        out = mk.apply_entries(db_dir, mk.parse_entries("message_0.db = " + "44" * 32), force=True)
        assert out["saved"] == 1
        assert out["status"]["invalid"] == 1

    def test_apply_merges_with_existing(self, db_dir, clean_config):
        mk.apply_entries(db_dir, mk.parse_entries(KEY_A))
        out = mk.apply_entries(db_dir, mk.parse_entries(KEY_B))
        assert out["status"]["verified"] == 2
        saved = json.load(open(clean_config, encoding="utf-8"))["db_keys"]
        assert len(saved) == 2

    def test_remove_key(self, db_dir, clean_config):
        mk.apply_entries(db_dir, mk.parse_entries(KEY_A))
        out = mk.remove_key(db_dir, "message\\message_0.db")
        assert out["removed"] == 1
        assert out["status"]["verified"] == 0

    def test_format_results_is_readable(self, db_dir, clean_config):
        res = mk.match_entries(db_dir, mk.parse_entries(KEY_A))
        text = mk.format_results(res)
        assert "matched" in text or "message_0.db" in text
        assert KEY_A not in text
