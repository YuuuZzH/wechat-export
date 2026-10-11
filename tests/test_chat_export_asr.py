# -*- coding: utf-8 -*-
"""issue #24 第④步（格式层）：导出聊天记录时可选带上语音转文字。

关键约束：**默认不改变导出结果**（不勾选时与旧版逐字节一致），
只有显式传入语音文字时才把 `[语音]` 变成 `[语音] 文字…`。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chat_export import _format_content  # noqa: E402


def test_voice_without_asr_is_unchanged():
    """不传语音文字时，输出必须与旧版完全一致（向后兼容）。"""
    assert _format_content('', 34, False) == '[语音]'
    assert _format_content(None, 34, False) == ''
    assert _format_content('', 1, False) == ''


def test_voice_with_asr_appends_text():
    assert _format_content('', 34, False, voice_text='今天天气不错') == '[语音] 今天天气不错'


def test_voice_with_blank_asr_stays_plain():
    """空/空白文字视为"没有转写"，不能产出 `[语音] ` 这种带尾空格的怪格式。"""
    assert _format_content('', 34, False, voice_text='') == '[语音]'
    assert _format_content('', 34, False, voice_text='   \n') == '[语音]'


def test_other_types_ignore_voice_text():
    """语音文字只影响语音消息，别把其它类型也改了。"""
    assert _format_content('你好', 1, False, voice_text='不应出现') == '你好'
    assert _format_content('', 3, False, voice_text='不应出现') == '[图片]'
    assert _format_content('', 43, False, voice_text='不应出现') == '[视频]'
