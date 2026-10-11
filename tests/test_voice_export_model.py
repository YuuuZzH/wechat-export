# -*- coding: utf-8 -*-
"""语音导出：数据模型与命名规则测试（合成数据）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.services.voice_export.model import (VoiceItem, safe_filename,
                                                voice_basename, pick_sample_rate)  # noqa: E402


def test_safe_filename_strips_illegal_chars_and_trailing_dots():
    assert safe_filename('a/b\\c:d*e?f"g<h>i|j') == 'a_b_c_d_e_f_g_h_i_j'
    assert safe_filename('  名字...  ') == '名字'
    assert safe_filename('') == 'unknown'
    assert safe_filename('   ') == 'unknown'
    assert len(safe_filename('x' * 200)) == 80


def test_safe_filename_keeps_cjk_and_emoji():
    assert safe_filename('爷爷') == '爷爷'
    assert safe_filename('奶奶🎵') == '奶奶🎵'


def test_voice_basename_is_name_plus_local_datetime():
    got = voice_basename('爷爷', 1614933012)      # 2021-03-05 12:30:12 +08:00
    assert got.startswith('爷爷_20210305-')
    assert len(got) == len('爷爷_') + 15


def test_voice_basename_falls_back_for_empty_name():
    assert voice_basename('', 1614933012).startswith('unknown_')


def test_pick_sample_rate_uses_xml_length_as_tiebreaker():
    # 1 秒 PCM：24k -> 48000B，16k -> 32000B
    assert pick_sample_rate(48000, 1000) == 24000
    assert pick_sample_rate(32000, 1000) == 16000
    assert pick_sample_rate(48000, 0) == 24000      # XML 缺失 -> 默认 24k
    assert pick_sample_rate(0, 1000) == 24000


def test_voice_item_defaults_are_safe():
    it = VoiceItem(chat_id='c', chat_name='会话', local_id=1, create_time=1614933012)
    assert it.sender_name == '' and it.silk_path == '' and it.error == ''
    assert it.duration_s == 0.0 and it.offset_start == 0.0 and it.offset_end == 0.0
    assert it.sample_rate == 24000 and it.transcript == ''


def test_voice_item_datetime_text_is_local():
    # 1614933012 = 2021-03-05T08:30:12Z；engine.constants.TZ 为 +08:00 → 16:30:12
    it = VoiceItem(chat_id='c', chat_name='会话', local_id=1, create_time=1614933012)
    assert it.datetime_text == '2021-03-05 16:30:12'
    # 与 voice_basename 用的是同一套本地时间
    assert voice_basename('我', 1614933012).endswith('20210305-163012')
