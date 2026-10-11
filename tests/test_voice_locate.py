# -*- coding: utf-8 -*-
"""issue #24 第③步前置：语音文件定位要可被外部复用（回传本地识别结果时算内容哈希）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.services import media  # noqa: E402


def test_locate_finds_silk_in_media_voice(tmp_path):
    d = tmp_path / 'decrypted' / 'media' / 'voice'
    d.mkdir(parents=True)
    (d / 'demo.silk').write_bytes(b'SILK')
    got = media.locate_voice_file(str(tmp_path / 'decrypted'), 'demo.silk')
    assert got == str(d / 'demo.silk')


def test_locate_finds_silk_in_sibling_voice_dir(tmp_path):
    d = tmp_path / 'voice'
    d.mkdir(parents=True)
    (d / 'demo2.silk').write_bytes(b'SILK')
    got = media.locate_voice_file(str(tmp_path / 'decrypted'), 'demo2.silk')
    assert got == str(d / 'demo2.silk')


def test_locate_returns_none_when_missing(tmp_path):
    assert media.locate_voice_file(str(tmp_path), 'nope.silk') is None
