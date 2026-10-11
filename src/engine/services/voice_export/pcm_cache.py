# -*- coding: utf-8 -*-
"""语音 PCM 缓存：让同一条语音在一次导出里**只解码一次**。

真机测量（73 条语音、默认四种布局）：SILK 解码 **294 次**、总耗时 34.4s；
而每条语音其实只需要解码一次 —— 逐条写出、合并、内嵌 HTML 各自又解码了一遍。
这里把解码结果落到临时文件，后续阶段直接读回。

设计取舍：

* 落**磁盘**而不是内存：5,000 条语音的 PCM 有 GB 级，放内存会顶不住；
* 有**预算上限**（默认 512MB）：超预算就不再缓存，调用方回退为按需解码 —— 只慢不错；
* 线程安全：解码阶段是多线程的（`workers`），`put/get` 都加锁。
"""

import os
import shutil
import tempfile
import threading

DEFAULT_BUDGET_BYTES = 1024 * 1024 * 1024


class PcmCache:
    """把 ``<create_time>_<local_id>`` 对应的 PCM 缓存在临时目录里。"""

    def __init__(self, root=None, budget_bytes=DEFAULT_BUDGET_BYTES):
        self._own_root = root is None
        self._root = root or tempfile.mkdtemp(prefix='wechat_exp_voice_pcm_')
        try:
            os.makedirs(self._root, exist_ok=True)
        except OSError:
            pass
        self._budget = int(budget_bytes or 0)
        self._used = 0
        self._lock = threading.Lock()
        self._closed = False

    # -- 基本信息 ---------------------------------------------------------
    @property
    def root(self):
        return self._root

    @property
    def used_bytes(self):
        return self._used

    @property
    def enabled(self):
        return self._budget > 0

    def key_for(self, create_time, local_id):
        return '%s_%s.pcm' % (int(create_time), int(local_id))

    # -- 读写 -------------------------------------------------------------
    def put(self, key, pcm):
        """缓存一段 PCM；超预算/已关闭/写失败时返回 None（调用方回退到按需解码）。"""
        if not pcm or not self.enabled or self._closed:
            return None
        size = len(pcm)
        with self._lock:
            if self._used + size > self._budget:
                return None
            path = os.path.join(self._root, key)
            self._used += size
        try:
            with open(path, 'wb') as f:
                f.write(pcm)
        except OSError:
            with self._lock:
                self._used -= size
            return None
        return path

    def get(self, path):
        """读回缓存的 PCM；文件不存在/读失败返回 None。"""
        if not path:
            return None
        try:
            with open(path, 'rb') as f:
                return f.read() or None
        except OSError:
            return None

    def close(self):
        """删除自己创建的临时目录（外部传入的 root 不动）。"""
        self._closed = True
        if self._own_root and self._root and os.path.isdir(self._root):
            shutil.rmtree(self._root, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
