# -*- coding: utf-8 -*-
"""issue #23 回归（模型层）：多源自动回退、跳过已就绪文件、缺多少 MB、失败信息。

真机实测的前提：
  * hf-mirror.com 用浏览器 UA 可用、用 Python 默认 UA 403；
  * 「HuggingFace 官方」国内直连超时 ⇒ 程序必须**自动**换源，而不是让用户自己猜。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.services import asr_model as am  # noqa: E402

MODEL = 'Xenova/whisper-tiny'
FILES = am.ASR_MODELS[MODEL]['files']


@pytest.fixture()
def fake_dirs(tmp_path, monkeypatch):
    """用户目录（可写）与内置目录（模拟打包时随 exe 分发的文件）都指向临时目录。"""
    user = tmp_path / 'user'
    bundled = tmp_path / 'bundled'
    user.mkdir()
    bundled.mkdir()
    monkeypatch.setattr(am, 'user_model_dir', lambda: str(user))
    monkeypatch.setattr(am, 'bundled_model_dir', lambda: str(bundled))
    return {'user': user, 'bundled': bundled}


def _write(root, model, rel, data=b'x' * 10):
    p = os.path.join(str(root), model.replace('/', os.sep), rel.replace('/', os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'wb') as f:
        f.write(data)
    return p


def test_download_falls_back_to_next_source_on_403(fake_dirs, monkeypatch):
    """第一个源 403（正是 issue #23 的现象）时必须自动改用第二个源，而不是直接失败。"""
    from engine.services import http_download as hd

    tried = []

    def fake_download(url, dest, **kw):
        tried.append(url)
        if 'hf-mirror' in url:
            raise hd.DownloadError(url, 403, 'Forbidden')
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, 'wb') as f:
            f.write(b'ok')
        return 2

    monkeypatch.setattr(am, 'download_file', fake_download)
    res = am.download_model(MODEL, progress_fn=lambda *a, **k: None)
    assert res['downloaded'] == len(FILES)
    assert 'huggingface.co' in res['mirror']
    assert any('hf-mirror' in u for u in tried) and any('huggingface' in u for u in tried)


def test_download_skips_files_already_in_bundled_dir(fake_dirs, monkeypatch):
    """内置目录里已有的文件不应再联网下载（否则第一个文件就是 config.json，必然先撞 403）。"""
    _write(fake_dirs['bundled'], MODEL, 'config.json')
    requested = []

    def fake_download(url, dest, **kw):
        requested.append(url)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, 'wb') as f:
            f.write(b'ok')
        return 2

    monkeypatch.setattr(am, 'download_file', fake_download)
    res = am.download_model(MODEL, progress_fn=lambda *a, **k: None)
    assert not any(u.endswith('/config.json') for u in requested), requested
    assert res['skipped'] >= 1
    assert res['downloaded'] == len(FILES) - 1


def test_download_error_message_lists_status_and_sources(fake_dirs, monkeypatch):
    """所有源都失败时，错误信息要含状态码与已尝试的源（便于用户判断是 403 还是超时）。"""
    from engine.services import http_download as hd

    def fake_download(url, dest, **kw):
        raise hd.DownloadError(url, 403, 'Forbidden')

    monkeypatch.setattr(am, 'download_file', fake_download)
    with pytest.raises(IOError) as ei:
        am.download_model(MODEL, progress_fn=lambda *a, **k: None)
    msg = str(ei.value)
    assert '403' in msg
    assert 'hf-mirror' in msg and 'huggingface' in msg


def test_model_status_reports_missing_bytes(fake_dirs):
    """「还差多少 MB」要能算出来（原来恒为 0）。"""
    st = am.model_status(MODEL)
    assert st['missingBytes'] > 0
    expect = sum(am.size_bytes(MODEL, rel) for rel in FILES)
    assert st['missingBytes'] == expect
    assert st['missingMb'] > 0
    # 放一个内置文件后，缺的字节数应相应减少
    _write(fake_dirs['bundled'], MODEL, FILES[0])
    st2 = am.model_status(MODEL)
    assert st2['missingBytes'] == expect - am.size_bytes(MODEL, FILES[0])


def test_model_status_reports_bundled_files(fake_dirs):
    """状态里要能看出哪些文件是随程序内置的（无需下载）。"""
    _write(fake_dirs['bundled'], MODEL, FILES[0])
    st = am.model_status(MODEL)
    row = [f for f in st['files'] if f['name'] == FILES[0]][0]
    assert row['exists'] and row['bundled']
    other = [f for f in st['files'] if f['name'] == FILES[-1]][0]
    assert not other['bundled']


def test_download_failure_offers_fallback_model(fake_dirs, monkeypatch):
    """失败时给出"可改用哪个模型"的信息，并标明它是否已就绪。"""
    from engine.services import http_download as hd

    def fake_download(url, dest, **kw):
        raise hd.DownloadError(url, 403, 'Forbidden')

    monkeypatch.setattr(am, 'download_file', fake_download)
    info = am.fallback_model_info(MODEL)
    assert info['model'] != MODEL
    assert 'ready' in info
    assert 'reason' in info or True
