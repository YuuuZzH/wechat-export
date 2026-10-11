"""Tests for voice lookup across media_*.db shards (issue: 语音播放 404).

全部使用临时目录与合成数据，不涉及真实微信数据。
"""
import os
import sqlite3
import time

import pytest

from engine.services import media

SILK = b"\x02#!SILK_V3" + b"\x00" * 40
SILK2 = b"\x02#!SILK_V3" + b"\x11" * 40


def _make_media_db(path, rows, names=("chat_a", "chat_b")):
    """构造一个含 Name2Id + VoiceInfo 的最小 media_*.db。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE Name2Id (user_name TEXT)")
    con.execute("CREATE TABLE VoiceInfo (chat_name_id INTEGER, create_time INTEGER,"
                " local_id INTEGER, svr_id INTEGER, voice_data BLOB, data_index INTEGER)")
    for name in names:
        con.execute("INSERT INTO Name2Id (user_name) VALUES (?)", (name,))
    for row in rows:
        con.execute("INSERT INTO VoiceInfo (chat_name_id, create_time, local_id,"
                    " svr_id, voice_data, data_index) VALUES (?,?,?,?,?,?)", row)
    con.commit()
    con.close()


@pytest.fixture
def decrypted_dir(tmp_path):
    d = tmp_path / "decrypted"
    (d / "message").mkdir(parents=True)
    return str(d)


class TestCacheFilename:
    def test_long_hex_blob_is_hashed(self):
        name = media._voice_cache_filename("7f" * 120, 1789531971, 1585)
        assert name.endswith(".silk")
        assert len(name) == 37, name

    def test_non_blob_path_falls_back_to_ct_lid(self):
        # 真实文件名（如 media/voice/abc.silk）由调用方直接按 basename 查找，
        # 这里返回的是"从数据库提取后写缓存"用的名字
        assert media._voice_cache_filename("abc.silk", 1, 2) == "1_2.silk"

    def test_non_blob_without_ids_is_empty(self):
        assert media._voice_cache_filename("abc.silk") == ""

    def test_ct_lid_fallback(self):
        assert media._voice_cache_filename("", 1789531971, 1585) == "1789531971_1585.silk"

    def test_no_path_no_ids(self):
        assert media._voice_cache_filename("", None, None) == ""


class TestExtractAcrossShards:
    def test_finds_row_in_second_media_shard(self, decrypted_dir):
        # media_0.db 没有该条语音，media_1.db 才有（正是线上 404 的场景）
        _make_media_db(os.path.join(decrypted_dir, "message", "media_0.db"), [])
        _make_media_db(os.path.join(decrypted_dir, "message", "media_1.db"),
                       [(1, 1789531971, 1585, 0, SILK, 0)])
        path = media._extract_voice_from_db(decrypted_dir, 1789531971, 1585)
        assert path and path.endswith("1789531971_1585.silk")
        assert open(path, "rb").read() == SILK

    def test_returns_none_when_missing(self, decrypted_dir):
        _make_media_db(os.path.join(decrypted_dir, "message", "media_0.db"), [])
        assert media._extract_voice_from_db(decrypted_dir, 1, 2) is None

    def test_chat_argument_disambiguates(self, decrypted_dir):
        # 同 (create_time, local_id) 在两个会话下各有一条，给 chat 时取对应那条
        _make_media_db(os.path.join(decrypted_dir, "message", "media_0.db"),
                       [(1, 1789531971, 1585, 0, SILK, 0),
                        (2, 1789531971, 1585, 0, SILK2, 0)])
        p1 = media._extract_voice_from_db(decrypted_dir, 1789531971, 1585, chat="chat_a")
        assert open(p1, "rb").read() == SILK
        # 换缓存键再取第二个会话，确认能取到另一条
        p2 = media._extract_voice_from_db(decrypted_dir, 1789531971, 1585, chat="chat_b",
                                          cache_key="b.silk")
        assert open(p2, "rb").read() == SILK2

    def test_cache_reused_on_second_call(self, decrypted_dir):
        _make_media_db(os.path.join(decrypted_dir, "message", "media_1.db"),
                       [(1, 111, 22, 0, SILK, 0)])
        p1 = media._extract_voice_from_db(decrypted_dir, 111, 22)
        first_mtime = os.path.getmtime(p1)
        time.sleep(0.05)
        p2 = media._extract_voice_from_db(decrypted_dir, 111, 22)
        assert p1 == p2
        assert os.path.getmtime(p2) == first_mtime  # 命中缓存，未重写


class TestStaleShardFallback:
    def test_newer_source_shard_is_decrypted_on_demand(self, decrypted_dir, tmp_path, monkeypatch):
        src_root = tmp_path / "src"
        src_msg = src_root / "message"
        src_msg.mkdir(parents=True)
        # 解密副本里没有这条语音
        _make_media_db(os.path.join(decrypted_dir, "message", "media_1.db"), [])
        # 源库分片更新且含该语音
        src_db = str(src_msg / "media_1.db")
        _make_media_db(src_db, [(1, 777, 88, 0, SILK, 0)])
        future = time.time() + 60
        os.utime(src_db, (future, future))

        calls = []

        def fake_decrypt(src, dec_dir):
            calls.append(src)
            dst = os.path.join(dec_dir, "message", os.path.basename(src))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            import shutil
            shutil.copy2(src, dst)
            return dst

        monkeypatch.setattr(media, "_decrypt_media_db_on_the_fly", fake_decrypt)
        path = media._extract_voice_from_db(decrypted_dir, 777, 88, db_dir=str(src_root))
        assert calls, "应该对较新的源库分片做按需解密"
        assert path and open(path, "rb").read() == SILK

    def test_up_to_date_copy_is_not_decrypted_again(self, decrypted_dir, tmp_path, monkeypatch):
        src_root = tmp_path / "src"
        src_msg = src_root / "message"
        src_msg.mkdir(parents=True)
        _make_media_db(str(src_msg / "media_1.db"), [(1, 999, 55, 0, SILK, 0)])
        # 解密副本更新（mtime 更晚）→ 不应再触发按需解密
        _make_media_db(os.path.join(decrypted_dir, "message", "media_1.db"),
                       [(1, 999, 55, 0, SILK, 0)])
        now = time.time()
        os.utime(str(src_msg / "media_1.db"), (now - 600, now - 600))

        calls = []
        monkeypatch.setattr(media, "_decrypt_media_db_on_the_fly",
                            lambda src, d: calls.append(src) or None)
        path = media._extract_voice_from_db(decrypted_dir, 999, 55, db_dir=str(src_root))
        assert path and not calls
