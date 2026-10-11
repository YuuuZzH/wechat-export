# -*- coding: utf-8 -*-
"""端到端集成测试：跨会话多人、采样率判定、缺失语音、m4a 缺 ffmpeg。

合成数据，不读真实库。覆盖设计文档 §1 的三种交付形态 + 两个补充形态。
"""
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import zipfile

import pytest
import zstandard

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.services.voice_export import export_voices  # noqa: E402

CHAT_A = 'wxid_demo_1a2b'
CHAT_B = 'wxid_other_5e6f'
GRANDPA = 'wxid_grandpa_3c4d'
ME = 'wxid_me12cd34ef56_9f2c'


def _mk(tmp_path):
    """两个会话、三个人、4 条语音（其中 1 条在 VoiceInfo 里没有数据）。"""
    dec = tmp_path / 'decrypted'
    (dec / 'message').mkdir(parents=True)
    msg = sqlite3.connect(str(dec / 'message' / 'message_0.db'))
    msg.execute('CREATE TABLE Name2Id (rowid INTEGER PRIMARY KEY, user_name TEXT)')
    msg.execute('INSERT INTO Name2Id VALUES (1, ?)', (CHAT_A,))
    msg.execute('INSERT INTO Name2Id VALUES (2, ?)', (ME,))
    msg.execute('INSERT INTO Name2Id VALUES (3, ?)', (GRANDPA,))
    msg.execute('INSERT INTO Name2Id VALUES (4, ?)', (CHAT_B,))
    tables = {}
    for chat in (CHAT_A, CHAT_B):
        table = 'Msg_' + hashlib.md5(chat.encode()).hexdigest()
        tables[chat] = table
        msg.execute('CREATE TABLE [%s] (local_id INTEGER, local_type INTEGER,'
                    ' create_time INTEGER, real_sender_id INTEGER, origin_source INTEGER,'
                    ' message_content BLOB)' % table)
    # A: 爷爷 3 月 + 我 4 月；B: 爷爷 4 月（跨会话聚合用）；第 4 条故意没有 VoiceInfo
    xml = zstandard.ZstdCompressor().compress(b'<msg><voicemsg voicelength="1000" /></msg>')
    msg.execute('INSERT INTO [%s] VALUES (1, 34, 1614933012, 3, 0, ?)' % tables[CHAT_A], (xml,))
    msg.execute('INSERT INTO [%s] VALUES (2, 34, 1617600000, 2, 1, ?)' % tables[CHAT_A], (xml,))
    msg.execute('INSERT INTO [%s] VALUES (3, 34, 1617600100, 3, 0, ?)' % tables[CHAT_B], (xml,))
    msg.execute('INSERT INTO [%s] VALUES (4, 34, 1617600200, 3, 0, ?)' % tables[CHAT_B], (xml,))
    msg.commit()
    msg.close()

    med = sqlite3.connect(str(dec / 'message' / 'media_0.db'))
    med.execute('CREATE TABLE Name2Id (rowid INTEGER PRIMARY KEY, user_name TEXT)')
    med.execute('INSERT INTO Name2Id VALUES (1, ?)', (CHAT_A,))
    med.execute('INSERT INTO Name2Id VALUES (2, ?)', (CHAT_B,))
    med.execute('CREATE TABLE VoiceInfo (chat_name_id INTEGER, create_time INTEGER,'
                ' local_id INTEGER, voice_data BLOB)')
    blob = b'\x02#!SILK_V3' + b'a' * 40
    med.execute('INSERT INTO VoiceInfo VALUES (1, 1614933012, 1, ?)', (blob,))
    med.execute('INSERT INTO VoiceInfo VALUES (1, 1617600000, 2, ?)', (blob,))
    med.execute('INSERT INTO VoiceInfo VALUES (2, 1617600100, 3, ?)', (blob,))
    # local_id=4 故意不写 => missing
    med.commit()
    med.close()
    return str(dec)


@pytest.fixture(autouse=True)
def _fake_pcm(monkeypatch):
    """第 1 条给 1 秒 @24k，第 2、3 条给 1 秒 @16k（验证采样率判定 + 合并重采样）。"""
    from engine.services import media
    from engine.services.voice_export import collect, layout

    def fake(path):
        name = os.path.basename(str(path))
        if name.startswith('161760'):
            return b'\x02\x00' * 16000      # 1.0 秒 @16k
        return b'\x01\x00' * 24000          # 1.0 秒 @24k

    monkeypatch.setattr(collect, 'silk_to_pcm', fake)
    monkeypatch.setattr(layout, '_silk_to_pcm', fake)
    # 单文件内嵌 HTML 的默认编码器是函数内 import，所以要打 media 上的那份
    monkeypatch.setattr(media, 'silk_to_pcm', fake)


def _lookup(wxid):
    if wxid == GRANDPA:
        return '爷爷'
    if wxid == ME:
        return '我'
    return wxid


def test_e2e_cross_chat_multi_person_all_layouts(tmp_path):
    dec = _mk(tmp_path)
    report = export_voices(dec, str(tmp_path / 'out'), chats=[CHAT_A, CHAT_B],
                           include_other_chats=True, own_wxid=ME, name_lookup=_lookup,
                           fmt='wav', layouts=('html-folder', 'files', 'merged'),
                           merge_by='person', gap_s=1.0, workers=1)
    out = report['out_dir']
    assert report['count'] == 3 and report['missing'] == 1      # 第 4 条没有语音数据
    assert report['duration_total_s'] == pytest.approx(3.0)     # 3 条各 1 秒（16k 也已正确判定）

    for name in ('index.html', 'manifest.csv', 'manifest.json', 'missing.csv', 'README.txt'):
        assert os.path.isfile(os.path.join(out, name)), name
    assert report['zip'] and zipfile.is_zipfile(report['zip'])

    # 逐条文件：姓名+时间戳命名，且按人分目录
    audio_files = []
    for root, _dirs, names in os.walk(os.path.join(out, 'audio')):
        audio_files.extend(os.path.join(root, n) for n in names)
    assert len(audio_files) == 3
    assert any(os.path.basename(f).startswith('爷爷_2021') for f in audio_files)
    assert all(re.match(r'.+_\d{8}-\d{6}\.wav$', os.path.basename(f)) for f in audio_files)

    # 合并：爷爷 2 条（跨会话）合成一个文件；我 1 条
    merged = report['merged']
    stems = sorted(m['stem'] for m in merged)
    assert any(s.startswith('爷爷_全部语音') for s in stems)
    assert any(s.startswith('我_') for s in stems)
    grandpa = [m for m in merged if m['stem'].startswith('爷爷')][0]
    assert grandpa['duration_s'] == pytest.approx(3.0)          # 1 + 1(静音) + 1

    # 清单：字段齐全、偏移递增、合并文件映射正确
    rows = list(csv.DictReader(open(os.path.join(out, 'manifest.csv'), encoding='utf-8-sig')))
    assert len(rows) == 3
    for row in rows:
        assert row['merged_file'].startswith('merged/')
        assert float(row['offset_end']) > float(row['offset_start'])
    grandpa_rows = sorted([r for r in rows if r['sender_name'] == '爷爷'],
                          key=lambda r: float(r['offset_start']))
    assert [float(r['offset_start']) for r in grandpa_rows] == pytest.approx([0.0, 2.0])
    assert {r['sample_rate'] for r in rows} == {'24000', '16000'}

    # HTML：零外部依赖、含每个人、含音频标签
    page = open(os.path.join(out, 'index.html'), encoding='utf-8').read()
    assert '爷爷' in page and '我' in page
    assert page.count('<audio') >= 3
    assert not re.search(r'https?://', page) and 'src="//' not in page

    # 缺失清单：写明原因
    missing = list(csv.DictReader(open(os.path.join(out, 'missing.csv'), encoding='utf-8-sig')))
    assert len(missing) == 1 and '备份中找不到' in missing[0]['reason']

    meta = json.load(open(os.path.join(out, 'manifest.json'), encoding='utf-8'))
    assert meta['count'] == 3


def test_e2e_m4a_without_ffmpeg_stops_before_writing(tmp_path, monkeypatch):
    from engine.services.voice_export import audio
    monkeypatch.setattr(audio, '_find_ffmpeg', lambda: None)
    dec = _mk(tmp_path)
    with pytest.raises(RuntimeError):
        export_voices(dec, str(tmp_path / 'out2'), chats=[CHAT_A], fmt='m4a')


def test_e2e_inline_html_is_self_contained(tmp_path):
    dec = _mk(tmp_path)
    report = export_voices(dec, str(tmp_path / 'out3'), chats=[CHAT_A], own_wxid=ME,
                           name_lookup=_lookup, fmt='wav', layouts=('html-inline',),
                           zip_output=False)
    page = open(os.path.join(report['out_dir'], 'all-in-one.html'), encoding='utf-8').read()
    assert not re.search(r'https?://', page)
