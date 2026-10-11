# -*- coding: utf-8 -*-
"""语音导出的音频层测试：PCM 时长、WAV/MP3/M4A 编码、能力探测（全部合成数据）。"""
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.services.voice_export import audio  # noqa: E402


def _pcm(nbytes: int = 3200) -> bytes:
    return b'\x01\x02' * (nbytes // 2)


def test_pcm_duration_uses_sample_rate():
    # 16-bit 单声道：48000 字节 @24k = 1.0 秒；32000 字节 @16k = 1.0 秒
    assert audio.pcm_duration_s(_pcm(48000), 24000) == pytest.approx(1.0)
    assert audio.pcm_duration_s(_pcm(32000), 16000) == pytest.approx(1.0)
    assert audio.pcm_duration_s(b'', 24000) == 0.0


def test_encode_wav_writes_riff_header_with_given_rate(tmp_path):
    out = str(tmp_path / 'a.wav')
    assert audio.encode_wav(_pcm(4800), out, 24000) == out
    raw = open(out, 'rb').read()
    assert raw[:4] == b'RIFF' and raw[8:12] == b'WAVE'
    rate = struct.unpack('<I', raw[24:28])[0]
    data_size = struct.unpack('<I', raw[40:44])[0]
    assert rate == 24000 and data_size == 4800


def test_encode_mp3_needs_lameenc_and_produces_frames(tmp_path):
    pytest.importorskip('lameenc')
    out = str(tmp_path / 'a.mp3')
    got = audio.encode_mp3(_pcm(48000), out, 24000)
    assert got == out and os.path.getsize(out) > 200
    assert open(out, 'rb').read(1)[0] == 0xFF    # MPEG 帧同步字


def test_encode_mp3_returns_none_without_lameenc(tmp_path, monkeypatch):
    monkeypatch.setattr(audio, '_import_lameenc', lambda: None)
    assert audio.encode_mp3(_pcm(1000), str(tmp_path / 'x.mp3'), 24000) is None


def test_capabilities_drops_m4a_without_ffmpeg(monkeypatch):
    monkeypatch.setattr(audio, '_find_ffmpeg', lambda: None)
    caps = audio.capabilities()
    assert caps['wav'] is True
    assert caps['m4a'] is False and caps['ffmpeg'] == ''
    assert set(['wav', 'mp3', 'm4a', 'ffmpeg']).issubset(caps)


def test_encode_m4a_requires_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(audio, '_find_ffmpeg', lambda: None)
    with pytest.raises(RuntimeError):
        audio.encode_m4a(_pcm(4800), str(tmp_path / 'a.m4a'), 24000)


def test_real_silk_decoder_lookup_does_not_raise():
    """真实调用链（不 monkeypatch）：silk_to_pcm 必须能在本机定位解码器且不抛异常。

    回归：`_find_silk_decoder` 曾因为把函数内的 `import sys` 一起重构掉而 NameError，
    而单测都 monkeypatch 了 `silk_to_pcm`，只有真机冒烟才发现 —— 这里补上。
    """
    from engine.services import media
    decoder = media._find_silk_decoder()
    if decoder:
        assert os.path.isfile(decoder) and decoder.lower().endswith('silk_decoder.exe')
    # 不存在的输入必须安静地返回 None（不抛异常）
    assert media.silk_to_pcm(str(__import__('tempfile').mkdtemp() + '/nope.silk')) is None
