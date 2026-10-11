# -*- coding: utf-8 -*-
"""顶层编排测试：端到端跑一遍（合成数据），覆盖布局、清单、降级与报告。"""
import csv
import json
import os
import sqlite3
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.services.voice_export import export_voices  # noqa: E402

CHAT = 'wxid_demo_1a2b'
GRANDPA = 'wxid_grandpa_3c4d'
ME = 'wxid_me12cd34ef56_9f2c'


def _mk(tmp_path, voice_data_for=(1, 2)):
    import hashlib
    dec = tmp_path / 'decrypted'
    (dec / 'message').mkdir(parents=True)
    tbl = 'Msg_' + hashlib.md5(CHAT.encode()).hexdigest()
    msg = sqlite3.connect(str(dec / 'message' / 'message_0.db'))
    msg.execute('CREATE TABLE Name2Id (rowid INTEGER PRIMARY KEY, user_name TEXT)')
    msg.execute('INSERT INTO Name2Id VALUES (1, ?)', (CHAT,))
    msg.execute('INSERT INTO Name2Id VALUES (2, ?)', (ME,))
    msg.execute('INSERT INTO Name2Id VALUES (3, ?)', (GRANDPA,))
    msg.execute('CREATE TABLE [%s] (local_id INTEGER, local_type INTEGER, create_time INTEGER,'
                ' real_sender_id INTEGER, origin_source INTEGER, message_content BLOB)' % tbl)
    msg.execute('INSERT INTO [%s] VALUES (1, 34, 1614933012, 3, 0, NULL)' % tbl)
    msg.execute('INSERT INTO [%s] VALUES (2, 34, 1614933100, 2, 1, NULL)' % tbl)
    msg.commit()
    msg.close()
    med = sqlite3.connect(str(dec / 'message' / 'media_0.db'))
    med.execute('CREATE TABLE Name2Id (rowid INTEGER PRIMARY KEY, user_name TEXT)')
    med.execute('INSERT INTO Name2Id VALUES (1, ?)', (CHAT,))
    med.execute('CREATE TABLE VoiceInfo (chat_name_id INTEGER, create_time INTEGER,'
                ' local_id INTEGER, voice_data BLOB)')
    for lid, ts in ((1, 1614933012), (2, 1614933100)):
        if lid in voice_data_for:
            med.execute('INSERT INTO VoiceInfo VALUES (1, ?, ?, ?)',
                        (ts, lid, b'\x02#!SILK_V3' + b'x' * 40))
    med.commit()
    med.close()
    return str(dec)


@pytest.fixture(autouse=True)
def _fake_pcm(monkeypatch):
    """1 秒 24k 假 PCM（不依赖真实 SILK）。"""
    from engine.services.voice_export import collect, layout
    monkeypatch.setattr(collect, 'silk_to_pcm', lambda p: b'\x01\x00' * 24000)
    monkeypatch.setattr(layout, '_silk_to_pcm', lambda p: b'\x01\x00' * 24000)


def test_export_voices_end_to_end(tmp_path):
    dec = _mk(tmp_path)
    rep = export_voices(dec, str(tmp_path / 'export'), chats=[CHAT], own_wxid=ME,
                        name_lookup=lambda w: '爷爷' if w == GRANDPA else ('我' if w == ME else w),
                        fmt='wav', layouts=('html-folder', 'files', 'merged'))
    out = rep['out_dir']
    assert rep['count'] == 2 and rep['missing'] == 0
    for name in ('index.html', 'manifest.csv', 'manifest.json', 'README.txt'):
        assert os.path.isfile(os.path.join(out, name)), name
    assert rep['zip'] and zipfile.is_zipfile(rep['zip'])
    meta = json.load(open(os.path.join(out, 'manifest.json'), encoding='utf-8'))
    assert meta['count'] == 2
    assert {r['sender_name'] for r in meta['items']} == {'爷爷', '我'}
    assert all(float(r['offset_end']) > 0 for r in meta['items'])
    assert all(r['merged_file'].startswith('merged/') for r in meta['items'])
    page = open(os.path.join(out, 'index.html'), encoding='utf-8').read()
    assert '爷爷' in page and 'https://' not in page
    assert rep['capabilities']['wav'] is True


def test_export_voices_reports_missing(tmp_path):
    dec = _mk(tmp_path, voice_data_for=(1,))
    rep = export_voices(dec, str(tmp_path / 'e2'), chats=[CHAT], own_wxid=ME, fmt='wav')
    assert rep['count'] == 1 and rep['missing'] == 1
    missing_csv = os.path.join(rep['out_dir'], 'missing.csv')
    assert os.path.isfile(missing_csv)
    rows = list(csv.DictReader(open(missing_csv, encoding='utf-8-sig')))
    assert len(rows) == 1 and rows[0]['reason']


def test_export_voices_rejects_m4a_without_ffmpeg(tmp_path, monkeypatch):
    from engine.services.voice_export import audio
    monkeypatch.setattr(audio, '_find_ffmpeg', lambda: None)
    dec = _mk(tmp_path)
    with pytest.raises(RuntimeError):
        export_voices(dec, str(tmp_path / 'e3'), chats=[CHAT], fmt='m4a')


def test_export_voices_downgrades_mp3_without_lameenc(tmp_path, monkeypatch, capsys):
    from engine.services.voice_export import audio
    monkeypatch.setattr(audio, '_import_lameenc', lambda: None)
    dec = _mk(tmp_path)
    rep = export_voices(dec, str(tmp_path / 'e4'), chats=[CHAT], fmt='mp3',
                        layouts=('files',))
    files = [r['file'] for r in _manifest_items(rep)]
    assert files and all(f.endswith('.wav') for f in files)


def _manifest_items(rep):
    path = os.path.join(rep['out_dir'], 'manifest.json')
    return json.load(open(path, encoding='utf-8'))['items']


def test_export_voices_inline_html_and_progress(tmp_path):
    dec = _mk(tmp_path)
    seen = []
    rep = export_voices(dec, str(tmp_path / 'e5'), chats=[CHAT], fmt='wav',
                        layouts=('html-inline',), zip_output=False,
                        progress_fn=lambda stage, msg: seen.append(stage))
    assert os.path.isfile(os.path.join(rep['out_dir'], 'all-in-one.html'))
    assert rep['zip'] == ''
    assert seen and 'finish' in seen


def test_export_voices_cancel_still_writes_manifest(tmp_path):
    dec = _mk(tmp_path)
    rep = export_voices(dec, str(tmp_path / 'e6'), chats=[CHAT], fmt='wav',
                        layouts=('files',), cancel=lambda: True)
    assert os.path.isfile(os.path.join(rep['out_dir'], 'manifest.csv'))


def test_export_voices_never_uses_done_as_progress_stage(tmp_path):
    """回归：`sse.py` 把 stage='done' 当作**完成事件**（不带 result）。

    真机浏览器验收踩过：pipeline 用 `_progress('done', ...)` 推最后一行进度 ⇒ 前端
    收到一个没有 count/zip_url 的 done，提前渲染"共导出 0 条"并失去下载链接。
    进度行的 stage 必须避开 'done'（这里用 'finish'）。
    """
    stages = []
    dec = _mk(tmp_path)
    export_voices(dec, str(tmp_path / 'e7'), chats=[CHAT], fmt='wav',
                  layouts=('files',), zip_output=False,
                  progress_fn=lambda stage, message: stages.append(stage))
    assert 'done' not in stages, '进度回调不得使用 done 这个 stage'
    assert 'finish' in stages
