# -*- coding: utf-8 -*-
"""写盘层测试：逐条命名、合并（含静音与偏移）、体积保护、清单、缺失清单、zip。"""
import csv
import json
import os
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.services.voice_export import layout  # noqa: E402
from engine.services.voice_export.model import VoiceItem, plan_merge  # noqa: E402


def _item(tmp_path, name='爷爷', ts=1614933012, dur=1.0, rate=24000, lid=1):
    silk = tmp_path / ('%s_%s.silk' % (name, ts))
    silk.write_bytes(b'\x02#!SILK_V3' + b'x' * 40)
    it = VoiceItem(chat_id='wxid_demo_1a2b', chat_name='张三', local_id=lid, create_time=ts,
                   sender_name=name, silk_path=str(silk), sample_rate=rate, duration_s=dur)
    return it


@pytest.fixture(autouse=True)
def _fake_pcm(monkeypatch):
    """1 秒 24k 的假 PCM：不再依赖真实 SILK 解码。"""
    monkeypatch.setattr(layout, '_silk_to_pcm', lambda p: b'\x01\x00' * 24000)


def test_write_individual_uses_name_and_timestamp(tmp_path):
    it = _item(tmp_path)
    out = tmp_path / 'out'
    res = layout.write_individual([it], str(out), fmt='wav', sample_rate='auto')
    assert len(res) == 1
    rel = res[0]['file']
    assert rel.startswith('audio/爷爷/爷爷_20210305-') and rel.endswith('.wav')
    assert (out / rel).exists()
    assert it.out_name.startswith('爷爷_')


def test_write_individual_dedupes_same_second_names(tmp_path):
    a, b = _item(tmp_path, ts=1614933012, lid=1), _item(tmp_path, ts=1614933012, lid=2)
    res = layout.write_individual([a, b], str(tmp_path / 'o'), fmt='wav', sample_rate='auto')
    names = [os.path.basename(r['file']) for r in res]
    assert names[0] != names[1] and names[1].endswith('_2.wav')


def test_write_individual_keeps_original_silk(tmp_path):
    it = _item(tmp_path)
    res = layout.write_individual([it], str(tmp_path / 'o'), fmt='wav', sample_rate='auto',
                                 keep_silk=True)
    assert os.path.isfile(os.path.join(str(tmp_path / 'o'), 'original-silk', '爷爷',
                                       os.path.basename(res[0]['file'])[:-4] + '.silk'))


def test_write_individual_falls_back_to_wav_when_mp3_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(layout.audio, 'encode_mp3', lambda *a, **k: None)
    res = layout.write_individual([_item(tmp_path)], str(tmp_path / 'o'), fmt='mp3',
                                  sample_rate='auto')
    assert res[0]['file'].endswith('.wav')


def test_write_individual_surfaces_decode_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(layout, '_silk_to_pcm', lambda p: None)
    it = _item(tmp_path)
    res = layout.write_individual([it], str(tmp_path / 'o'), fmt='wav', sample_rate='auto')
    assert res == [] and it.error == 'SILK 解码失败'


def test_write_merged_concatenates_with_gap_and_updates_offsets(tmp_path):
    items = [_item(tmp_path, ts=100, dur=1.0, lid=1), _item(tmp_path, ts=200, dur=1.0, lid=2)]
    groups = plan_merge(items, merge_by='person', gap_s=1.0)
    res = layout.write_merged(groups, str(tmp_path / 'o'), fmt='wav', gap_s=1.0)
    assert len(res) == 1 and res[0]['file'].startswith('merged/爷爷_')
    assert res[0]['duration_s'] == pytest.approx(3.0)      # 1 + 1(静音) + 1
    assert items[1].offset_start == pytest.approx(2.0)     # 写盘后按真实时长回填
    assert items[1].offset_end == pytest.approx(3.0)


def test_write_merged_splits_when_group_exceeds_max_bytes(tmp_path):
    items = [_item(tmp_path, ts=i, dur=2.0, lid=i) for i in (1, 2, 3)]
    groups = plan_merge(items, merge_by='person', gap_s=0.0)
    res = layout.write_merged(groups, str(tmp_path / 'o'), fmt='wav', gap_s=0.0,
                             max_bytes=200000)          # 2 秒 @24k ≈ 96000B ⇒ 每批 2 条
    files = sorted(r['file'] for r in res)
    assert len(res) == 2 and all('第' in f and f.endswith('.wav') for f in files)
    assert sum(len(r['items']) for r in res) == 3


def test_write_manifest_includes_fields_and_merged_mapping(tmp_path):
    it = _item(tmp_path)
    out = tmp_path / 'o'
    layouts = layout.write_individual([it], str(out), fmt='wav', sample_rate='auto')
    groups = plan_merge([it], merge_by='person')
    merged = layout.write_merged(groups, str(out), fmt='wav', gap_s=1.0)
    csv_path, json_path = layout.write_manifest([it], str(out), layouts=layouts, merged=merged)
    rows = list(csv.DictReader(open(csv_path, encoding='utf-8-sig')))
    assert rows[0]['sender_name'] == '爷爷' and rows[0]['datetime'].startswith('2021-03-05')
    assert rows[0]['merged_file'].startswith('merged/')
    for field in ('sender_name', 'sender_id', 'chat_name', 'datetime', 'duration_s',
                  'sample_rate', 'file', 'merged_file', 'offset_start', 'offset_end',
                  'transcript', 'status'):
        assert field in rows[0]
    meta = json.load(open(json_path, encoding='utf-8'))
    assert meta['count'] == 1 and float(meta['items'][0]['offset_end']) >= 1.0


def test_write_missing_and_readme(tmp_path):
    out = tmp_path / 'o'
    os.makedirs(out, exist_ok=True)
    path = layout.write_missing([{'sender_name': '爷爷', 'chat_name': '张三',
                                  'datetime': '2021-03-05 16:30:12', 'reason': '缺失'}], str(out))
    assert '缺失' in open(path, encoding='utf-8-sig').read()
    readme = layout.write_readme(str(out), '语音导出 20260924')
    text = open(readme, encoding='utf-8').read()
    assert 'index.html' in text and 'manifest.csv' in text


def test_make_zip_contains_written_files(tmp_path):
    out = tmp_path / 'o'
    layout.write_individual([_item(tmp_path)], str(out), fmt='wav', sample_rate='auto')
    z = layout.make_zip(str(out), str(tmp_path / 'pack.zip'))
    with zipfile.ZipFile(z) as zf:
        names = zf.namelist()
    assert any(n.endswith('.wav') for n in names)
