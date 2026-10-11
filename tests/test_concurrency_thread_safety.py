# -*- coding: utf-8 -*-
"""并发/线程安全回归：共享 zstd 上下文曾被多线程同时使用导致进程崩溃。

真机现象（用户报告）：在 Web 里点第二次「语音导出」时程序直接退出。
根因：`message/decode.py` 与 `message/__init__.py` 用**模块级共享的**
`zstandard.ZstdDecompressor()`，而它不是线程安全的 —— 两个导出线程并发解压时
表现为"返回空"甚至**访问违规（0xC0000005）崩进程**。
修法：`get_zstd_decompressor()` 给每个线程一个独立上下文。
"""
import os
import sys
import threading

import pytest
import zstandard

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.services.message import _zstd_decompress_raw  # noqa: E402
from engine.services.message.decode import (decompress_content,  # noqa: E402
                                            get_zstd_decompressor)

PAYLOAD = zstandard.ZstdCompressor().compress(b'voice-export-thread-safety' * 128)
EXPECTED = b'voice-export-thread-safety' * 128


def test_zstd_context_is_per_thread():
    """同一线程内复用；不同线程拿到不同实例（这就是修法的核心）。"""
    main_ctx = get_zstd_decompressor()
    assert main_ctx is get_zstd_decompressor()
    seen = {}

    def worker():
        seen['ctx'] = get_zstd_decompressor()

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert seen['ctx'] is not None
    assert seen['ctx'] is not main_ctx, '每个线程必须拿到独立的解压上下文'


def test_concurrent_decompression_never_returns_empty():
    """并发解压必须**每次都成功**（修复前这里会随机返回 None/空 —— 真机上则是崩溃）。"""
    results = []
    errors = []

    def worker(idx):
        try:
            for _ in range(150):
                raw = _zstd_decompress_raw(PAYLOAD)
                xml = decompress_content(PAYLOAD)
                if not raw or not xml:
                    results.append('empty@%d' % idx)
                    return
                if raw != EXPECTED or xml != EXPECTED:
                    results.append('wrong@%d' % idx)
                    return
        except Exception as exc:                      # noqa: BLE001
            errors.append('%d: %r' % (idx, exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [], errors
    assert results == [], '并发解压出现空/错结果：%r' % results[:5]


def test_real_collect_is_safe_when_run_concurrently(tmp_path, monkeypatch):
    """并发跑两次采集（真机崩的就是这一步）：结果必须一致且不丢数据。"""
    import sqlite3
    import hashlib
    from engine.services.voice_export.collect import collect_voice_items

    dec = tmp_path / 'decrypted'
    (dec / 'message').mkdir(parents=True)
    chat = 'wxid_demo_1a2b'
    table = 'Msg_' + hashlib.md5(chat.encode()).hexdigest()
    xml = zstandard.ZstdCompressor().compress(b'<msg><voicemsg voicelength="1000" /></msg>')
    conn = sqlite3.connect(str(dec / 'message' / 'message_0.db'))
    conn.execute('CREATE TABLE Name2Id (rowid INTEGER PRIMARY KEY, user_name TEXT)')
    conn.execute('INSERT INTO Name2Id VALUES (1, ?)', (chat,))
    conn.execute('INSERT INTO Name2Id VALUES (2, ?)', ('wxid_grandpa_3c4d',))
    conn.execute('CREATE TABLE [%s] (local_id INTEGER, local_type INTEGER, create_time INTEGER,'
                 ' real_sender_id INTEGER, origin_source INTEGER, message_content BLOB)' % table)
    for lid in range(1, 61):
        conn.execute('INSERT INTO [%s] VALUES (?, 34, ?, 2, 0, ?)' % table,
                     (lid, 1614933012 + lid, xml))
    conn.commit()
    conn.close()

    counts, errors = [], []

    def worker():
        try:
            items, _missing = collect_voice_items(dec, chats=[chat], extract=False)
            counts.append(len(items))
        except Exception as exc:                      # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [], errors
    assert counts == [60, 60], counts
