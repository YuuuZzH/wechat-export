# -*- coding: utf-8 -*-
"""issue #23 回归：模型下载的 UA / 多源回退 / 断点续传 / 已就绪文件跳过。

真机复现（修复前）：
    同一 URL、同一网络
      urllib 默认 UA（Python-urllib/3.x）→ HTTP 403 Forbidden（连测 2 次）
      浏览器 UA                          → 200（连测 4 次）
    浏览器 UA + Range 取 22MB 权重前 2KB  → 206 Partial Content
⇒ 根因是下载请求没带浏览器 UA，被 hf-mirror.com 的反爬层拒绝；"换源"给的 HF 官方在国内直连超时。

测试用假的 HTTP 层（monkeypatch `http_download.open_url`），不碰真实网络。
"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.services import http_download as hd  # noqa: E402


class FakeResponse:
    def __init__(self, body=b'', status=200, headers=None):
        self._body = body
        self._pos = 0
        self.status = status
        self.headers = headers or {}

    def read(self, n=-1):
        if n is None or n < 0:
            chunk = self._body[self._pos:]
        else:
            chunk = self._body[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_browser_headers_carry_a_browser_ua():
    """下载请求必须带浏览器 UA（裸 urllib 的 Python-urllib/3.x 会被 403）。"""
    h = hd.browser_headers()
    ua = h.get('User-Agent', '')
    assert 'Mozilla' in ua, ua
    assert 'Python-urllib' not in ua
    assert h.get('Accept')


def test_download_file_uses_browser_headers(monkeypatch, tmp_path):
    """download_file 走 open_url，且请求头带浏览器 UA。"""
    seen = {}

    def fake_open(url, timeout=60, headers=None, use_proxy=True):
        seen['url'] = url
        seen['headers'] = dict(headers or {})
        return FakeResponse(b'hello', 200, {'Content-Length': '5'})

    monkeypatch.setattr(hd, 'open_url', fake_open)
    dest = str(tmp_path / 'f.bin')
    n = hd.download_file('https://example.com/f.bin', dest)
    assert n == 5
    assert io.open(dest, 'rb').read() == b'hello'
    assert 'Mozilla' in seen['headers'].get('User-Agent', '')


def test_download_file_resumes_from_part_file(monkeypatch, tmp_path):
    """已存在 .part 时发 Range 续传（206），并在完成后改名为正式文件。"""
    seen = {}

    def fake_open(url, timeout=60, headers=None, use_proxy=True):
        seen['headers'] = dict(headers or {})
        # 服务器支持 Range：返回剩余部分
        return FakeResponse(b'world', 206, {'Content-Range': 'bytes 5-9/10'})

    monkeypatch.setattr(hd, 'open_url', fake_open)
    dest = str(tmp_path / 'f.bin')
    io.open(dest + '.part', 'wb').write(b'hello')
    n = hd.download_file('https://example.com/f.bin', dest)
    assert seen['headers'].get('Range') == 'bytes=5-'
    assert io.open(dest, 'rb').read() == b'helloworld'
    assert n == 10
    assert not os.path.exists(dest + '.part')


def test_download_file_restarts_when_server_ignores_range(monkeypatch, tmp_path):
    """服务器不支持 Range（返回 200 全量）时必须从头写，不能拼接出损坏文件。"""
    def fake_open(url, timeout=60, headers=None, use_proxy=True):
        return FakeResponse(b'fresh', 200, {'Content-Length': '5'})

    monkeypatch.setattr(hd, 'open_url', fake_open)
    dest = str(tmp_path / 'f.bin')
    io.open(dest + '.part', 'wb').write(b'stale')
    hd.download_file('https://example.com/f.bin', dest)
    assert io.open(dest, 'rb').read() == b'fresh'


def test_download_file_reports_http_status_on_error(monkeypatch, tmp_path):
    """403 之类的 HTTP 错误要带上状态码（便于界面提示与排查）。"""
    import urllib.error

    def fake_open(url, timeout=60, headers=None, use_proxy=True):
        raise urllib.error.HTTPError(url, 403, 'Forbidden', {}, None)

    monkeypatch.setattr(hd, 'open_url', fake_open)
    with pytest.raises(hd.DownloadError) as ei:
        hd.download_file('https://example.com/f.bin', str(tmp_path / 'x.bin'))
    assert ei.value.status == 403
    assert '403' in str(ei.value)


def test_download_file_detects_truncated_body(monkeypatch, tmp_path):
    """下载长度与服务器声明不符时必须报错，不能留下残缺文件。"""
    def fake_open(url, timeout=60, headers=None, use_proxy=True):
        return FakeResponse(b'abc', 200, {'Content-Length': '100'})

    monkeypatch.setattr(hd, 'open_url', fake_open)
    dest = str(tmp_path / 'f.bin')
    with pytest.raises(hd.DownloadError):
        hd.download_file('https://example.com/f.bin', dest)
    assert not os.path.exists(dest), '残缺文件不应留下'
