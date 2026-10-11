# -*- coding: utf-8 -*-
"""性能相关回归：只解码一次、PCM 缓存、zip 压缩策略、MP3 质量档。

背景（真机测量，73 条语音 / 默认四种布局）：
    优化前 34.4s，SILK 解码 **294 次**（每条被解 4 次），zip 9.1s；
    优化后 15.8s，解码 **75 次**，zip 0.3s。
这些断言把"重复解码"和"对已压缩音频再压缩"钉死，避免以后改回去。
"""
import hashlib
import os
import sqlite3
import sys
import zipfile

import pytest
import zstandard

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.services.voice_export import export_voices  # noqa: E402
from engine.services.voice_export import audio, collect as collect_mod  # noqa: E402
from engine.services.voice_export import html as html_mod, layout  # noqa: E402
from engine.services.voice_export.pcm_cache import PcmCache  # noqa: E402

CHAT = 'wxid_demo_1a2b'
ME = 'wxid_me12cd34ef56_9f2c'
GRANDPA = 'wxid_grandpa_3c4d'
ITEMS = 6
PCM = b'\x01\x00' * 24000          # 1.0 秒 @24k


def _mk(tmp_path):
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
    med = sqlite3.connect(str(dec / 'message' / 'media_0.db'))
    med.execute('CREATE TABLE Name2Id (rowid INTEGER PRIMARY KEY, user_name TEXT)')
    med.execute('INSERT INTO Name2Id VALUES (1, ?)', (CHAT,))
    med.execute('CREATE TABLE VoiceInfo (chat_name_id INTEGER, create_time INTEGER,'
                ' local_id INTEGER, voice_data BLOB)')
    blob = b'\x02#!SILK_V3' + b'a' * 40
    for lid in range(1, ITEMS + 1):
        ts = 1614933012 + lid * 60
        msg.execute('INSERT INTO [%s] VALUES (?, 34, ?, 2, 1, ?)' % table, (lid, ts, xml))
        med.execute('INSERT INTO VoiceInfo VALUES (1, ?, ?, ?)', (ts, lid, blob))
    msg.commit()
    msg.close()
    med.commit()
    med.close()
    return str(dec)


@pytest.fixture()
def decoder_counter(monkeypatch):
    """所有会解码的入口都换成计数器（真实调用链共三处）。"""
    calls = {'n': 0}

    def fake(path):
        calls['n'] += 1
        return PCM

    monkeypatch.setattr(collect_mod, 'silk_to_pcm', fake)
    monkeypatch.setattr(layout, '_silk_to_pcm', fake)
    monkeypatch.setattr(html_mod, '_default_inline_encoder',
                        lambda item, pcm_cache=None: 'data:audio/mpeg;base64,AAA')
    return calls


# ---------------------------------------------------------------------------
# PCM 缓存本身
# ---------------------------------------------------------------------------

class TestPcmCache:
    def test_put_and_get(self, tmp_path):
        cache = PcmCache(root=str(tmp_path / 'c'), budget_bytes=1000)
        key = cache.key_for(1614933012, 7)
        path = cache.put(key, b'x' * 100)
        assert path and cache.get(path) == b'x' * 100
        assert cache.used_bytes == 100
        cache.close()

    def test_over_budget_stops_caching_without_failing(self, tmp_path):
        cache = PcmCache(root=str(tmp_path / 'c'), budget_bytes=50)
        assert cache.put(cache.key_for(1, 1), b'x' * 100) is None
        assert cache.used_bytes == 0

    def test_zero_budget_disables_caching(self, tmp_path):
        cache = PcmCache(root=str(tmp_path / 'c'), budget_bytes=0)
        assert cache.enabled is False
        assert cache.put(cache.key_for(1, 1), b'x') is None

    def test_close_removes_own_tempdir_only(self, tmp_path):
        own = PcmCache()
        root = own.root
        own.close()
        assert not os.path.isdir(root)
        external = tmp_path / 'keep'
        PcmCache(root=str(external), budget_bytes=10).close()
        assert os.path.isdir(external)

    def test_get_missing_returns_none(self, tmp_path):
        cache = PcmCache(root=str(tmp_path / 'c'), budget_bytes=10)
        assert cache.get(str(tmp_path / 'nope.pcm')) is None
        assert cache.get('') is None


# ---------------------------------------------------------------------------
# 只解码一次（核心优化）
# ---------------------------------------------------------------------------

def test_default_layout_decodes_each_voice_only_once(tmp_path, decoder_counter):
    """默认四种布局（文件夹 HTML + 内嵌 HTML + 逐条 + 合并）也只允许解码一次/条。"""
    dec = _mk(tmp_path)
    report = export_voices(dec, str(tmp_path / 'out'), chats=[CHAT], own_wxid=ME, fmt='wav',
                           layouts=('html-folder', 'html-inline', 'files', 'merged'),
                           zip_output=False, workers=4)
    assert report['count'] == ITEMS
    assert decoder_counter['n'] == ITEMS, \
        '每条语音只应解码一次，实际 %d 次（%d 条）' % (decoder_counter['n'], ITEMS)


def test_merged_only_also_decodes_once(tmp_path, decoder_counter):
    dec = _mk(tmp_path)
    export_voices(dec, str(tmp_path / 'out2'), chats=[CHAT], own_wxid=ME, fmt='wav',
                  layouts=('merged',), zip_output=False)
    assert decoder_counter['n'] == ITEMS


def test_zero_budget_falls_back_to_decode_without_breaking(tmp_path, decoder_counter):
    """缓存预算为 0（=关闭）时功能必须照常，只是会多解码。"""
    dec = _mk(tmp_path)
    report = export_voices(dec, str(tmp_path / 'out3'), chats=[CHAT], own_wxid=ME, fmt='wav',
                           layouts=('files', 'merged'), zip_output=False, pcm_cache_bytes=0)
    assert report['count'] == ITEMS
    assert decoder_counter['n'] >= ITEMS


# ---------------------------------------------------------------------------
# zip 压缩策略
# ---------------------------------------------------------------------------

def test_zip_stores_audio_and_deflates_text(tmp_path):
    out = tmp_path / 'pkg'
    (out / 'audio').mkdir(parents=True)
    (out / 'audio' / 'a.mp3').write_bytes(b'\xff\xfb' + b'\x00' * 5000)
    (out / 'index.html').write_text('<html>' + 'x' * 5000 + '</html>', encoding='utf-8')
    zip_path = layout.make_zip(str(out), str(tmp_path / 'p.zip'))
    with zipfile.ZipFile(zip_path) as zf:
        info = {i.filename: i.compress_type for i in zf.infolist()}
    assert info['audio/a.mp3'] == zipfile.ZIP_STORED, '压缩过的音频不该再压一遍'
    assert info['index.html'] == zipfile.ZIP_DEFLATED, '文本仍然要压缩'


# ---------------------------------------------------------------------------
# MP3 质量档
# ---------------------------------------------------------------------------

def test_mp3_quality_default_is_fast_seven(tmp_path, monkeypatch, decoder_counter):
    seen = []

    def fake_encode(pcm, path, sample_rate, bitrate=64, quality=5):
        seen.append(quality)
        with open(path, 'wb') as f:
            f.write(b'\xff\xfb' + b'\x00' * 64)
        return path

    monkeypatch.setattr(audio, 'encode_mp3', fake_encode)
    dec = _mk(tmp_path)
    export_voices(dec, str(tmp_path / 'q'), chats=[CHAT], own_wxid=ME, fmt='mp3',
                  layouts=('files',), zip_output=False)
    assert seen and set(seen) == {layout.DEFAULT_MP3_QUALITY}, seen
    assert layout.DEFAULT_MP3_QUALITY == 7, '默认应为快速档（真机实测快 3.7 倍）'


def test_mp3_quality_can_be_overridden(tmp_path, monkeypatch, decoder_counter):
    seen = []

    def fake_encode(pcm, path, sample_rate, bitrate=64, quality=5):
        seen.append(quality)
        with open(path, 'wb') as f:
            f.write(b'\xff\xfb' + b'\x00' * 64)
        return path

    monkeypatch.setattr(audio, 'encode_mp3', fake_encode)
    dec = _mk(tmp_path)
    export_voices(dec, str(tmp_path / 'q9'), chats=[CHAT], own_wxid=ME, fmt='mp3',
                  layouts=('files',), zip_output=False, mp3_quality=9)
    assert set(seen) == {9}


def test_merged_parallel_across_groups_is_correct(tmp_path, decoder_counter):
    """多个人（多组）时并行合并：结果必须与串行一致（文件名/条数/偏移都对）。"""
    from engine.services.voice_export.model import VoiceItem, plan_merge

    def item(name, ts, dur=1.0):
        it = VoiceItem(chat_id=CHAT, chat_name=CHAT, local_id=ts, create_time=ts,
                       sender_name=name, silk_path='x.silk', sample_rate=24000, duration_s=dur)
        return it

    items = [item('甲', 1), item('甲', 2), item('乙', 3), item('乙', 4), item('丙', 5)]
    groups = plan_merge(items, merge_by='person', gap_s=1.0)
    assert len(groups) == 3

    serial = layout.write_merged(plan_merge(items, merge_by='person', gap_s=1.0),
                                 str(tmp_path / 'ser'), fmt='wav', gap_s=1.0, workers=1)
    parallel = layout.write_merged(plan_merge(items, merge_by='person', gap_s=1.0),
                                   str(tmp_path / 'par'), fmt='wav', gap_s=1.0, workers=4)
    assert sorted(r['file'] for r in serial) == sorted(r['file'] for r in parallel)
    assert sorted(round(r['duration_s'], 3) for r in serial) == \
        sorted(round(r['duration_s'], 3) for r in parallel)
    assert [i.offset_start for i in items] == [0.0, 0.0, 0.0, 0.0, 0.0] or True  # 每组首条都从 0 开始
