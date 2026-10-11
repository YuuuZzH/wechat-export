# -*- coding: utf-8 -*-
"""HTML 生成测试：文件夹式 / 单文件内嵌 / 零外部依赖 / 合并定位。"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.services.voice_export import html as H  # noqa: E402
from engine.services.voice_export.model import VoiceItem, plan_merge  # noqa: E402


def _items():
    a = VoiceItem(chat_id='c', chat_name='张三', local_id=1, create_time=1614933012,
                  sender_name='爷爷', duration_s=8.4, out_name='爷爷_20210305-163012')
    b = VoiceItem(chat_id='c', chat_name='张三', local_id=2, create_time=1614933100,
                  sender_name='我', duration_s=3.0, out_name='我_20210305-163140',
                  transcript='你好')
    return [a, b]


def test_folder_html_lists_people_audio_and_offsets(tmp_path):
    items = _items()
    groups = plan_merge(items, merge_by='person')
    merged = [{'group': groups[0], 'file': 'merged/爷爷_全部语音.mp3', 'duration_s': 9.4,
               'items': [items[0]]}]
    path = H.write_html_folder(items, str(tmp_path), '爷爷的语音',
                               layouts=[{'item': items[0], 'file': 'audio/爷爷/x.mp3'}],
                               merged=merged)
    text = open(path, encoding='utf-8').read()
    assert '<meta charset="utf-8">' in text
    assert '爷爷' in text and '我' in text and '张三' in text
    assert text.count('<audio') >= 2
    assert 'audio/爷爷/x.mp3' in text
    assert '你好' in text                                   # 文字稿
    assert 'merged/爷爷_全部语音.mp3' in text                # 合并文件区
    assert not re.search(r'https?://', text)                 # 零外部依赖
    assert 'src="//' not in text
    assert 'oninput' in text                                 # 本地过滤框


def test_folder_html_escapes_user_text(tmp_path):
    it = VoiceItem(chat_id='c', chat_name='<b>群</b>', local_id=1, create_time=1614933012,
                   sender_name='<script>x</script>', duration_s=1.0,
                   transcript='<img src=x onerror=1>')
    path = H.write_html_folder([it], str(tmp_path), 'T', layouts=[{'item': it, 'file': 'a.mp3'}])
    text = open(path, encoding='utf-8').read()
    assert '<script>x</script>' not in text
    assert '&lt;script&gt;' in text
    assert 'onerror=1' not in text.replace('&lt;img src=x onerror=1&gt;', '')


def test_inline_html_embeds_data_uri(tmp_path):
    out = str(tmp_path / 'all.html')
    H.write_html_inline(_items(), out, '标题',
                        encoder=lambda it: 'data:audio/mpeg;base64,AAA')
    text = open(out, encoding='utf-8').read()
    assert 'data:audio/mpeg;base64,AAA' in text
    assert not re.search(r'https?://', text)


def test_inline_html_refuses_when_estimated_too_large(tmp_path):
    big = _items()
    for it in big:
        it.duration_s = 600.0            # 2 × 600s × 8KB/s ≈ 9.6MB
    with pytest.raises(ValueError):
        H.write_html_inline(big, str(tmp_path / 'x.html'), 't', max_mb=0.001,
                            encoder=lambda it: 'data:audio/mpeg;base64,AAA')


def test_html_falls_back_to_merged_file_timepoint(tmp_path):
    """只导出合并文件时：每条直接指向合并文件的时间点，而不是显示"音频缺失"。"""
    items = _items()
    groups = plan_merge(items, merge_by='person')
    merged = [{'group': groups[0], 'file': 'merged/爷爷_全部语音.mp3', 'duration_s': 5.4,
               'items': [items[0]]},
              {'group': groups[0], 'file': 'merged/我_全部语音.mp3', 'duration_s': 3.0,
               'items': [items[1]]}]
    path = H.write_html_folder(items, str(tmp_path), 'T', merged=merged)
    text = open(path, encoding='utf-8').read()
    # 2 个"合并音频"播放器 + 2 个逐条播放器（都指向合并文件的时间点）
    assert text.count('<audio') == 4
    assert 'merged/爷爷_全部语音.mp3#t=' in text
    assert '（音频缺失）' not in text


def test_html_marks_missing_when_nothing_to_play(tmp_path):
    path = H.write_html_folder(_items(), str(tmp_path), 'T')
    text = open(path, encoding='utf-8').read()
    assert '（音频缺失）' in text and '<audio' not in text
