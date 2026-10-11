# -*- coding: utf-8 -*-
"""显示名解析回归：导出的目录名/清单/HTML 里必须是**人能读的名字**，不是 wxid。

真机现象（用户报告）：输入「孙艳」能导出，但到处显示 `wxid_nio4lmb7i0yh11`。
根因：核心层把 `chat_name` 直接设成 chat_id，且调用方没传 name_lookup 时发送者也用 wxid。
修法：`collect` 内部自建名字映射（contact.db + 会话显示名），Web/CLI 不用各自传。
"""
import hashlib
import os
import sqlite3
import sys

import pytest
import zstandard

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.services.voice_export.collect import (build_chat_names,  # noqa: E402
                                                  build_name_lookup,
                                                  collect_voice_items)

CHAT = 'wxid_demo_1a2b'
GRANDPA = 'wxid_grandpa_3c4d'
ME = 'wxid_me12cd34ef56_9f2c'


def _mk(tmp_path, with_contact=True):
    dec = tmp_path / 'decrypted'
    (dec / 'message').mkdir(parents=True)
    table = 'Msg_' + hashlib.md5(CHAT.encode()).hexdigest()
    xml = zstandard.ZstdCompressor().compress(b'<msg><voicemsg voicelength="1000" /></msg>')
    msg = sqlite3.connect(str(dec / 'message' / 'message_0.db'))
    msg.execute('CREATE TABLE Name2Id (rowid INTEGER PRIMARY KEY, user_name TEXT)')
    msg.execute('INSERT INTO Name2Id VALUES (1, ?)', (CHAT,))
    msg.execute('INSERT INTO Name2Id VALUES (2, ?)', (ME,))
    msg.execute('INSERT INTO Name2Id VALUES (3, ?)', (GRANDPA,))
    msg.execute('CREATE TABLE [%s] (local_id INTEGER, local_type INTEGER, create_time INTEGER,'
                ' real_sender_id INTEGER, origin_source INTEGER, message_content BLOB)' % table)
    msg.execute('INSERT INTO [%s] VALUES (1, 34, 1614933012, 3, 0, ?)' % table, (xml,))
    msg.execute('INSERT INTO [%s] VALUES (2, 34, 1614933100, 2, 1, ?)' % table, (xml,))
    msg.commit()
    msg.close()
    # 语音数据放在 <decrypted>/message/media_*.db 的 VoiceInfo 里（真实布局如此）
    med = sqlite3.connect(str(dec / 'message' / 'media_0.db'))
    med.execute('CREATE TABLE Name2Id (rowid INTEGER PRIMARY KEY, user_name TEXT)')
    med.execute('INSERT INTO Name2Id VALUES (1, ?)', (CHAT,))
    med.execute('CREATE TABLE VoiceInfo (chat_name_id INTEGER, create_time INTEGER,'
                ' local_id INTEGER, voice_data BLOB)')
    blob = b'\x02#!SILK_V3' + b'a' * 40
    med.execute('INSERT INTO VoiceInfo VALUES (1, 1614933012, 1, ?)', (blob,))
    med.execute('INSERT INTO VoiceInfo VALUES (1, 1614933100, 2, ?)', (blob,))
    med.commit()
    med.close()
    if with_contact:
        (dec / 'contact').mkdir()
        c = sqlite3.connect(str(dec / 'contact' / 'contact.db'))
        c.execute('CREATE TABLE contact (username TEXT, remark TEXT, nick_name TEXT, alias TEXT)')
        c.execute('INSERT INTO contact VALUES (?, ?, ?, ?)', (GRANDPA, '爷爷', '老王', ''))
        c.execute('INSERT INTO contact VALUES (?, ?, ?, ?)', (ME, '', '我本人', ''))
        c.commit()
        c.close()
    return str(dec)


def test_build_name_lookup_uses_contact_remark(tmp_path):
    dec = _mk(tmp_path)
    lookup = build_name_lookup(dec)
    assert lookup(GRANDPA) == '爷爷'
    assert lookup('wxid_unknown_9z8y') == 'wxid_unknown_9z8y'   # 表里没有就原样返回


def test_collect_resolves_sender_names_without_caller_supplying_lookup(tmp_path):
    """核心层必须自己解析名字（Web/CLI 都不再需要各自传 name_lookup）。"""
    dec = _mk(tmp_path)
    items, _m = collect_voice_items(dec, chats=[CHAT], own_wxid=ME, extract=False)
    names = sorted(it.sender_name for it in items)
    assert names == ['我', '爷爷'], names
    assert all('wxid_' not in it.sender_name for it in items)


def test_explicit_name_lookup_still_wins(tmp_path):
    dec = _mk(tmp_path)
    items, _m = collect_voice_items(dec, chats=[CHAT], own_wxid=ME,
                                    name_lookup=lambda w: '覆盖' if w == GRANDPA else w,
                                    extract=False)
    assert '覆盖' in [it.sender_name for it in items]


def test_collect_uses_chat_display_name(tmp_path, monkeypatch):
    """会话名也用显示名（HTML/清单里的「来源会话」列）。"""
    import engine.services.voice_export.collect as collect_mod
    dec = _mk(tmp_path)
    monkeypatch.setattr(collect_mod, '_load_chat_list', lambda d, own='': [
        {'username': CHAT, 'display_name': '孙艳', 'msg_count': 2}])
    assert build_chat_names(dec) == {CHAT: '孙艳'}
    items, _m = collect_voice_items(dec, chats=[CHAT], own_wxid=ME, extract=False)
    assert {it.chat_name for it in items} == {'孙艳'}


def test_missing_contact_db_falls_back_to_wxid(tmp_path):
    """没有 contact.db 时不能崩，退回 wxid（而不是报错）。"""
    dec = _mk(tmp_path, with_contact=False)
    items, _m = collect_voice_items(dec, chats=[CHAT], own_wxid=ME, extract=False)
    assert len(items) == 2
    assert GRANDPA in [it.sender_id for it in items]


def test_pipeline_output_dir_uses_display_name(tmp_path, monkeypatch):
    """导出目录名也要是人名（真机上目录名曾是 wxid_xxx）。"""
    from engine.services.voice_export import export_voices, collect as collect_mod, layout

    dec = _mk(tmp_path)
    monkeypatch.setattr(collect_mod, 'silk_to_pcm', lambda p: b'\x01\x00' * 24000)
    monkeypatch.setattr(layout, '_silk_to_pcm', lambda p: b'\x01\x00' * 24000)
    report = export_voices(dec, str(tmp_path / 'out'), chats=[CHAT], own_wxid=ME,
                           senders=[GRANDPA], fmt='wav', layouts=('files',),
                           zip_output=False)
    label = os.path.basename(report['out_dir'])
    assert label.startswith('爷爷_'), label
    assert 'wxid_' not in label
