# -*- coding: utf-8 -*-
"""合并计划与 PCM 拼接/重采样测试（合成数据）。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.services.voice_export.model import VoiceItem, plan_merge  # noqa: E402
from engine.services.voice_export import audio  # noqa: E402


def _item(name, ts, dur_s, rate=24000, chat='张三'):
    it = VoiceItem(chat_id='c1', chat_name=chat, local_id=ts % 1000, create_time=ts,
                   sender_name=name, sample_rate=rate)
    it.duration_s = dur_s
    return it


def test_plan_merge_single_orders_by_time_and_sets_offsets_with_gap():
    items = [_item('爷爷', 300, 2.0), _item('爷爷', 100, 1.0), _item('我', 200, 3.0)]
    groups = plan_merge(items, merge_by='person', split='single', gap_s=1.0)
    g = [x for x in groups if x.key == '爷爷'][0]
    assert [i.create_time for i in g.items] == [100, 300]
    assert g.items[0].offset_start == 0.0 and g.items[0].offset_end == pytest.approx(1.0)
    # 第二段：前面 1.0s 语音 + 1.0s 静音 ⇒ 从 2.0s 开始
    assert g.items[1].offset_start == pytest.approx(2.0)
    assert g.items[1].offset_end == pytest.approx(4.0)
    assert g.file_stem.startswith('爷爷_全部语音')
    assert g.items[1].out_name.startswith('爷爷_')


def test_plan_merge_split_per_month_creates_one_group_per_month():
    items = [_item('爷爷', 1614933012, 1.0), _item('爷爷', 1617600000, 1.0)]   # 3 月 / 4 月
    groups = plan_merge(items, merge_by='person', split='per-month')
    assert len(groups) == 2
    assert all(len(g.items) == 1 for g in groups)
    assert sorted(g.file_stem for g in groups) == ['爷爷_202103', '爷爷_202104']


def test_plan_merge_split_per_day_and_per_n():
    items = [_item('爷爷', 1614933012, 1.0), _item('爷爷', 1614933013, 1.0),
             _item('爷爷', 1617600000, 1.0)]
    per_day = plan_merge(items, merge_by='person', split='per-day')
    assert len(per_day) == 2
    per_n = plan_merge(items, merge_by='person', split='per-n', per_n=2)
    assert len(per_n) == 2 and per_n[0].file_stem.endswith('第1批')


def test_plan_merge_by_chat_groups_by_conversation():
    items = [_item('爷爷', 1, 1.0, chat='家族群'), _item('奶奶', 2, 1.0, chat='家族群'),
             _item('爷爷', 3, 1.0, chat='私聊')]
    groups = plan_merge(items, merge_by='chat')
    assert sorted(g.key for g in groups) == ['家族群', '私聊']
    fam = [g for g in groups if g.key == '家族群'][0]
    assert len(fam.items) == 2 and fam.items[0].sender_name == '爷爷'


def test_plan_merge_none_returns_one_group_per_item():
    items = [_item('爷爷', 1, 1.0), _item('爷爷', 2, 1.0)]
    groups = plan_merge(items, merge_by='none')
    assert len(groups) == 2 and all(g.items for g in groups)


def test_plan_merge_skips_items_without_duration_or_with_error():
    items = [_item('爷爷', 1, 1.0), _item('爷爷', 2, 0.0)]
    items[1].error = 'SILK 解码失败'
    groups = plan_merge(items, merge_by='person')
    assert len(groups) == 1 and len(groups[0].items) == 1


def test_plan_merge_empty_input_returns_no_groups():
    assert plan_merge([], merge_by='person') == []


def test_concat_pcm_inserts_silence_and_resamples_to_target():
    a = b'\x01\x00' * 24000   # 1.0s @24k
    b = b'\x02\x00' * 16000   # 1.0s @16k
    out = audio.concat_pcm([(a, 24000), (b, 16000)], gap_s=0.5, dst_rate=24000)
    # 1.0 + 0.5(静音) + 1.0 = 2.5 秒 @24k
    assert len(out) == int(2.5 * 24000) * 2
    silence = out[int(1.0 * 24000) * 2:int(1.5 * 24000) * 2]
    assert set(silence) == {0}
    # 第一段保持原样
    assert out[:len(a)] == a


def test_concat_pcm_single_segment_needs_no_silence():
    a = b'\x01\x00' * 2400
    assert audio.concat_pcm([(a, 24000)], gap_s=1.0, dst_rate=24000) == a


def test_resample_pcm_keeps_duration():
    pcm = b'\x00\x01' * 16000    # 1.0s @16k
    got = audio.resample_pcm(pcm, 16000, 24000)
    assert audio.pcm_duration_s(got, 24000) == pytest.approx(1.0, abs=0.01)
    assert audio.resample_pcm(pcm, 16000, 16000) == pcm      # 同率直通


def test_resample_pcm_handles_odd_length_and_empty():
    assert audio.resample_pcm(b'', 16000, 24000) == b''
    assert len(audio.resample_pcm(b'\x01\x02\x03', 16000, 24000)) % 2 == 0


def test_silence_pcm_length():
    assert len(audio.silence_pcm(1.0, 24000)) == 48000
    assert set(audio.silence_pcm(0.5, 16000)) == {0}
