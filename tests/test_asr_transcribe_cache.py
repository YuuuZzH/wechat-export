# -*- coding: utf-8 -*-
"""issue #24：转写结果要复用（第二次打开秒回），且支持强制重识别。

策略：在 `media.transcribe_voice_cached()` 里做"先查库 → 未命中才识别 → 成功入库"。
测试用临时目录放假的 silk/wav 文件，并 monkeypatch 掉真正的识别与存储位置。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.services import asr_store, media  # noqa: E402


@pytest.fixture()
def voice_dir(tmp_path, monkeypatch):
    """造一个假的语音（silk + 同名 wav），并把存储指向临时库。"""
    d = tmp_path / 'decrypted' / 'media' / 'voice'
    d.mkdir(parents=True)
    silk = d / 'voice_demo.silk'
    silk.write_bytes(b'SILK-DEMO-BYTES')
    (d / 'voice_demo.wav').write_bytes(b'RIFF-DEMO')          # 已存在 ⇒ 跳过解码
    monkeypatch.setattr(asr_store, 'store_path', lambda: str(tmp_path / 'asr.db'))
    monkeypatch.setattr(media, '_transcribe_wav', lambda wav: '第一次识别出的文字')
    return str(tmp_path / 'decrypted')


def test_second_call_hits_cache(voice_dir, monkeypatch):
    calls = {'n': 0}

    def fake(wav):
        calls['n'] += 1
        return '识别文字-%d' % calls['n']

    monkeypatch.setattr(media, '_transcribe_wav', fake)
    r1 = media.transcribe_voice_cached(voice_dir, 'voice_demo.silk')
    r2 = media.transcribe_voice_cached(voice_dir, 'voice_demo.silk')
    assert r1['text'] == '识别文字-1' and r1['cached'] is False
    assert r2['text'] == '识别文字-1' and r2['cached'] is True
    assert calls['n'] == 1, '第二次不该再跑识别'


def test_refresh_forces_retranscribe_and_overwrites(voice_dir, monkeypatch):
    calls = {'n': 0}

    def fake(wav):
        calls['n'] += 1
        return '识别文字-%d' % calls['n']

    monkeypatch.setattr(media, '_transcribe_wav', fake)
    media.transcribe_voice_cached(voice_dir, 'voice_demo.silk')
    r2 = media.transcribe_voice_cached(voice_dir, 'voice_demo.silk', refresh=True)
    assert r2['text'] == '识别文字-2' and r2['cached'] is False
    assert calls['n'] == 2
    # 覆盖后，第三次取到的应是新文字
    assert media.transcribe_voice_cached(voice_dir, 'voice_demo.silk')['text'] == '识别文字-2'


def test_empty_result_is_not_cached(voice_dir, monkeypatch):
    calls = {'n': 0}

    def fake(wav):
        calls['n'] += 1
        return ''

    monkeypatch.setattr(media, '_transcribe_wav', fake)
    media.transcribe_voice_cached(voice_dir, 'voice_demo.silk')
    media.transcribe_voice_cached(voice_dir, 'voice_demo.silk')
    assert calls['n'] == 2, '空结果不入库 ⇒ 下次仍要重试'


def test_hash_is_content_based(voice_dir, monkeypatch):
    """同一份内容、不同文件名 ⇒ 仍能命中（备份换目录也能复用）。"""
    monkeypatch.setattr(media, '_transcribe_wav', lambda wav: '同一段语音')
    media.transcribe_voice_cached(voice_dir, 'voice_demo.silk')
    d = os.path.join(voice_dir, 'media', 'voice')
    with open(os.path.join(d, 'renamed.silk'), 'wb') as f:
        f.write(b'SILK-DEMO-BYTES')
    with open(os.path.join(d, 'renamed.wav'), 'wb') as f:
        f.write(b'RIFF-DEMO')
    r = media.transcribe_voice_cached(voice_dir, 'renamed.silk')
    assert r['cached'] is True and r['text'] == '同一段语音'


def test_transcribe_voice_wrapper_still_returns_text(voice_dir, monkeypatch):
    """老的调用方式（返回字符串）必须保持兼容。"""
    monkeypatch.setattr(media, '_transcribe_wav', lambda wav: '兼容文字')
    assert media.transcribe_voice(voice_dir, 'voice_demo.silk') == '兼容文字'
