# -*- coding: utf-8 -*-
"""语音采集测试：按人归属、日期/发送者过滤、缺失清单（合成分片，不碰真实数据）。"""
import hashlib
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.services.voice_export.collect import collect_voice_items  # noqa: E402

CHAT = 'wxid_demo_1a2b'
GRANDPA = 'wxid_grandpa_3c4d'
ME = 'wxid_me12cd34ef56_9f2c'


def _mk_shard(tmp_path, *, with_voice_rows=(1, 2, 3), voice_data_for=(1,)):
    """最小可用解密目录：message/message_0.db（Name2Id + Msg_ 表）+ media/media_0.db（VoiceInfo）。"""
    dec = tmp_path / 'decrypted'
    (dec / 'message').mkdir(parents=True)
    (dec / 'media').mkdir(parents=True)
    tbl = 'Msg_' + hashlib.md5(CHAT.encode()).hexdigest()
    msg = sqlite3.connect(str(dec / 'message' / 'message_0.db'))
    msg.execute('CREATE TABLE Name2Id (rowid INTEGER PRIMARY KEY, user_name TEXT)')
    msg.execute('INSERT INTO Name2Id VALUES (1, ?)', (CHAT,))
    msg.execute('INSERT INTO Name2Id VALUES (2, ?)', (ME,))
    msg.execute('INSERT INTO Name2Id VALUES (3, ?)', (GRANDPA,))
    msg.execute('CREATE TABLE [%s] (local_id INTEGER, local_type INTEGER, create_time INTEGER,'
                ' real_sender_id INTEGER, origin_source INTEGER, message_content BLOB)' % tbl)
    rows = {1: (34, 1614933012, 3, 0),      # 爷爷
            2: (34, 1614933100, 2, 1),      # 我
            3: (34, 1614933200, 1, 0),      # 对方
            4: (1, 1614933300, 3, 0)}       # 文本（不应被采集）
    for lid in with_voice_rows:
        lt, ts, rsid, org = rows[lid]
        msg.execute('INSERT INTO [%s] VALUES (?, ?, ?, ?, ?, NULL)' % tbl, (lid, lt, ts, rsid, org))
    msg.execute('INSERT INTO [%s] VALUES (4, 1, 1614933300, 3, 0, ?)' % tbl, (b'hi',))
    msg.commit()
    msg.close()
    med = sqlite3.connect(str(dec / 'media' / 'media_0.db'))
    med.execute('CREATE TABLE Name2Id (rowid INTEGER PRIMARY KEY, user_name TEXT)')
    med.execute('INSERT INTO Name2Id VALUES (1, ?)', (CHAT,))
    med.execute('CREATE TABLE VoiceInfo (chat_name_id INTEGER, create_time INTEGER,'
                ' local_id INTEGER, voice_data BLOB)')
    for lid in voice_data_for:
        lt, ts, rsid, org = rows[lid]
        med.execute('INSERT INTO VoiceInfo VALUES (1, ?, ?, ?)',
                    (ts, lid, b'\x02#!SILK_V3' + b'x' * 40))
    med.commit()
    med.close()
    return str(dec), tbl


def _lookup(w):
    return {GRANDPA: '爷爷'}.get(w, w)


def test_collect_groups_by_sender_without_extracting(tmp_path):
    dec, tbl = _mk_shard(tmp_path)
    items, missing = collect_voice_items(
        dec, chats=[CHAT], own_wxid=ME, name_lookup=_lookup, extract=False)
    by_name = {}
    for it in items:
        by_name.setdefault(it.sender_name, []).append(it)
    assert sorted(by_name) == sorted(['爷爷', '我', CHAT])
    assert len(by_name['爷爷']) == 1 and by_name['爷爷'][0].create_time == 1614933012
    assert by_name['我'][0].sender_id == ME
    assert missing == []                       # 未抽取时不做存在性判定
    assert all(it.chat_id == CHAT for it in items)
    assert all(it.silk_path == '' for it in items)


def test_collect_marks_missing_when_extraction_fails(tmp_path, monkeypatch):
    from engine.services.voice_export import collect
    dec, tbl = _mk_shard(tmp_path)
    monkeypatch.setattr(collect, '_extract_voice_from_db',
                        lambda d, ct, lid, **kw: ('/tmp/%d.silk' % lid) if lid == 1 else None)
    monkeypatch.setattr(collect, 'silk_to_pcm', lambda p: b'\x01\x00' * 24000)   # 1.0s @24k
    items, missing = collect_voice_items(
        dec, chats=[CHAT], own_wxid=ME, name_lookup=_lookup, extract=True)
    assert len(items) == 1 and items[0].sender_name == '爷爷'
    assert items[0].duration_s == pytest.approx(1.0) and items[0].sample_rate == 24000
    assert len(missing) == 2
    assert all(m['reason'] for m in missing)


def test_collect_respects_sender_filter_and_date_range(tmp_path):
    dec, tbl = _mk_shard(tmp_path)
    items, _m = collect_voice_items(dec, chats=[CHAT], senders=['爷爷'], own_wxid=ME,
                                    name_lookup=_lookup, extract=False)
    assert len(items) == 1 and items[0].sender_name == '爷爷'
    items2, _m2 = collect_voice_items(dec, chats=[CHAT], start_ts=1614933150,
                                      own_wxid=ME, name_lookup=_lookup, extract=False)
    assert [it.create_time for it in items2] == [1614933200]
    items3, _m3 = collect_voice_items(dec, chats=[CHAT], end_ts=1614933101,
                                      own_wxid=ME, name_lookup=_lookup, extract=False)
    assert [it.create_time for it in items3] == [1614933012, 1614933100]


def test_collect_skips_other_chats_unless_requested(tmp_path):
    dec, tbl = _mk_shard(tmp_path)
    items, _m = collect_voice_items(dec, chats=['wxid_someone_else_9z8y'],
                                    own_wxid=ME, name_lookup=_lookup, extract=False)
    assert items == []
    items2, _m2 = collect_voice_items(dec, chats=[], own_wxid=ME, name_lookup=_lookup,
                                      extract=False)
    assert len(items2) == 3                    # chats 为空 = 跨会话全扫


def test_collect_reads_xml_voicelength_as_duration_fallback(tmp_path):
    """content 里带 voicelength 时（extract=False）用作时长兜底。"""
    import zstandard
    dec, tbl = _mk_shard(tmp_path)
    xml = b'<msg><voicemsg voicelength="8340" /></msg>'
    mc = zstandard.ZstdCompressor().compress(xml)
    conn = sqlite3.connect(str(os.path.join(dec, 'message', 'message_0.db')))
    conn.execute('UPDATE [%s] SET message_content=? WHERE local_id=1' % tbl, (mc,))
    conn.commit()
    conn.close()
    items, _m = collect_voice_items(dec, chats=[CHAT], own_wxid=ME, name_lookup=_lookup,
                                    extract=False)
    grandpa = [it for it in items if it.sender_name == '爷爷'][0]
    assert grandpa.duration_ms == 8340
    assert grandpa.duration_s == pytest.approx(8.34)


class TestResolveChatTarget:
    """会话名（显示名）→ 真实 wxid。真机浏览器验收踩过：不解析就一条都收不到。"""

    CHATS = [
        {'username': 'wxid_demo_1a2b', 'display_name': '张三', 'msg_count': 5},
        {'username': 'wxid_other_5e6f', 'display_name': '张三（同事）', 'msg_count': 2},
        {'username': 'wxid_group_9z8y', 'display_name': '项目群', 'msg_count': 9},
    ]

    def _patch(self, monkeypatch):
        from engine.services.voice_export import collect as collect_mod
        monkeypatch.setattr(collect_mod, '_load_chat_list', lambda d, own='': self.CHATS)

    def test_exact_username_and_display_name(self, monkeypatch):
        from engine.services.voice_export.collect import resolve_chat_target
        self._patch(monkeypatch)
        assert resolve_chat_target('d', 'wxid_demo_1a2b') == ('wxid_demo_1a2b', [])
        assert resolve_chat_target('d', '项目群') == ('wxid_group_9z8y', [])

    def test_unique_fuzzy_match_resolves(self, monkeypatch):
        from engine.services.voice_export.collect import resolve_chat_target
        self._patch(monkeypatch)
        assert resolve_chat_target('d', '项目') == ('wxid_group_9z8y', [])

    def test_ambiguous_returns_candidates_without_guessing(self, monkeypatch):
        from engine.services.voice_export.collect import resolve_chat_target
        self._patch(monkeypatch)
        # '张' 同时命中「张三」和「张三（同事）」⇒ 不猜，交给用户选
        chat_id, matches = resolve_chat_target('d', '张')
        assert chat_id is None and len(matches) == 2
        assert {m['username'] for m in matches} == {'wxid_demo_1a2b', 'wxid_other_5e6f'}

    def test_exact_display_name_wins_over_similar_names(self, monkeypatch):
        from engine.services.voice_export.collect import resolve_chat_target
        self._patch(monkeypatch)
        # 精确命中显示名时直接用它（'张三（同事）' 是另一个名字，不算歧义）
        assert resolve_chat_target('d', '张三') == ('wxid_demo_1a2b', [])

    def test_unknown_passes_through_unchanged(self, monkeypatch):
        from engine.services.voice_export.collect import resolve_chat_target
        self._patch(monkeypatch)
        assert resolve_chat_target('d', '查无此人') == ('查无此人', [])

    def test_empty_text_is_noop(self, monkeypatch):
        from engine.services.voice_export.collect import resolve_chat_target
        self._patch(monkeypatch)
        assert resolve_chat_target('d', '  ') == (None, [])
