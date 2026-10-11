# -*- coding: utf-8 -*-
"""对外 HTTP 下载助手：浏览器 UA、断点续传、明确的错误信息。

背景（issue #23）：模型下载原来用裸 `urllib.request.urlopen`，UA 是 `Python-urllib/3.x`，
而默认源 `hf-mirror.com` 前面有反爬层会**按客户端指纹拒绝**。真机实测（同一 URL、同一网络）：

    Python 默认 UA → HTTP 403 Forbidden（连测 2 次）
    浏览器 UA      → 200 OK（连测 4 次）
    浏览器 UA + Range 取大文件前 2KB → 206 Partial Content

所以项目里所有对外下载统一走这里：默认带浏览器 UA、支持断点续传、失败时带状态码。
"""
import os
import urllib.error
import urllib.request

BROWSER_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')

DEFAULT_TIMEOUT = 60
CHUNK = 1 << 20
_OPENERS = {}


class DownloadError(IOError):
    """下载失败：带 URL、HTTP 状态码与原因，便于界面提示与排查。"""

    def __init__(self, url, status=None, reason='', detail=''):
        self.url = url
        self.status = status
        self.reason = reason
        self.detail = detail
        parts = []
        if status:
            parts.append('HTTP %s %s' % (status, reason or ''))
        if detail:
            parts.append(detail)
        super().__init__('%s（%s）' % ('; '.join(p for p in parts if p.strip()) or '下载失败', url))


def browser_headers(extra=None):
    """浏览器风格的请求头（反爬层会看 UA；Accept 一并带上更稳）。"""
    headers = {
        'User-Agent': BROWSER_UA,
        'Accept': '*/*',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Connection': 'keep-alive',
    }
    if extra:
        headers.update(extra)
    return headers


def _opener(use_proxy=True):
    key = bool(use_proxy)
    if key not in _OPENERS:
        if use_proxy:
            _OPENERS[key] = urllib.request.build_opener()
        else:
            _OPENERS[key] = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return _OPENERS[key]


def open_url(url, timeout=DEFAULT_TIMEOUT, headers=None, use_proxy=True):
    """带浏览器 UA 打开 URL（返回可当上下文管理器用的响应对象）。"""
    req = urllib.request.Request(url, headers=browser_headers(headers))
    try:
        return _opener(use_proxy).open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise DownloadError(url, e.code, e.reason or '') from None
    except urllib.error.URLError as e:
        raise DownloadError(url, None, '', '连接失败: %s' % (getattr(e, 'reason', e),)) from None


def _total_from_headers(headers, start):
    """从响应头推断文件总长（206 用 Content-Range，200 用 Content-Length）。"""
    cr = headers.get('Content-Range') or ''
    if '/' in cr:
        try:
            return int(cr.rsplit('/', 1)[1])
        except ValueError:
            pass
    cl = headers.get('Content-Length')
    if cl and str(cl).isdigit():
        return int(cl) + (start or 0)
    return None


def download_file(url, dest, timeout=180, progress_fn=None, resume=True, use_proxy=True):
    """下载到 ``dest``（先写 ``dest.part``，完成后改名），支持断点续传。

    Returns: 最终文件字节数。失败抛 :class:`DownloadError`（不会留下残缺的正式文件）。
    """
    part = dest + '.part'
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    start = os.path.getsize(part) if (resume and os.path.isfile(part)) else 0
    extra = {'Range': 'bytes=%d-' % start} if start else None
    # 显式带上浏览器 UA（即使调用方换了 opener，UA 也随请求走）
    headers = browser_headers(extra)
    try:
        resp = open_url(url, timeout=timeout, headers=headers, use_proxy=use_proxy)
    except DownloadError:
        raise
    except urllib.error.HTTPError as e:            # 兼容直接抛 HTTPError 的调用路径
        raise DownloadError(url, e.code, e.reason or '') from None
    except urllib.error.URLError as e:
        raise DownloadError(url, None, '',
                            '连接失败: %s' % (getattr(e, 'reason', e),)) from None
    with resp:
        status = getattr(resp, 'status', 200)
        append = bool(start) and status == 206
        if start and not append:
            start = 0                     # 服务器不支持续传：从头写，避免拼接出损坏文件
        total = _total_from_headers(resp.headers, start if append else 0)
        mode = 'ab' if append else 'wb'
        written = start if append else 0
        try:
            with open(part, mode) as out:
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    written += len(chunk)
                    if progress_fn:
                        progress_fn(written, total)
        except OSError as e:
            raise DownloadError(url, None, '', '写入失败: %s' % e) from None
    if total and written != total:
        try:
            os.remove(part)
        except OSError:
            pass
        raise DownloadError(url, None, '', '下载不完整: 期望 %d 字节，实际 %d' % (total, written))
    os.replace(part, dest)
    return written
