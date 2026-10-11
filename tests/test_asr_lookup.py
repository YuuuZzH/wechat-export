# -*- coding: utf-8 -*-
"""issue #24 第③步（后端）：查看器载入会话时批量取回已存文字。

前端知道的是「会话 + (create_time, local_id)」，不知道内容哈希，
所以存储层要提供按消息批量查询的入口（不做 N 次单条查询）。
"""
import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.services import asr_store  # noqa: E402

H1 = hashlib.sha1(b'v1').hexdigest()[:16]
H2 = hashlib.sha1(b'v2').hexdigest()[:16]
H3 = hashlib.sha1(b'v3').hexdigest()[:16]
CHAT = 'wxid_demo_chat'


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(asr_store, 'store_path', lambda: str(tmp_path / 't.db'))
    s = asr_store.AsrStore(str(tmp_path / 't.db'))
    yield s
    s.close()


def test_lookup_for_chat_returns_keyed_by_message(store):
    store.put(H1, '甲', chat_id=CHAT, create_time=100, local_id=1)
    store.put(H2, '乙', chat_id=CHAT, create_time=200, local_id=2)
    got = asr_store.lookup_for_chat(CHAT, [(100, 1), (200, 2)], store=store)
    assert got['100:1']['text'] == '甲'
    assert got['200:2']['text'] == '乙'


def test_lookup_for_chat_ignores_other_chats(store):
    store.put(H1, '别的会话', chat_id='wxid_other', create_time=100, local_id=1)
    assert asr_store.lookup_for_chat(CHAT, [(100, 1)], store=store) == {}


def test_lookup_for_chat_skips_unknown_items(store):
    store.put(H1, '甲', chat_id=CHAT, create_time=100, local_id=1)
    got = asr_store.lookup_for_chat(CHAT, [(100, 1), (999, 999)], store=store)
    assert list(got) == ['100:1']


def test_lookup_for_chat_empty_items(store):
    store.put(H1, '甲', chat_id=CHAT, create_time=100, local_id=1)
    assert asr_store.lookup_for_chat(CHAT, [], store=store) == {}
    assert asr_store.lookup_for_chat('', [(100, 1)], store=store) == {}


def test_lookup_exposes_cached_flag_fields(store):
    """前端要据此显示「已转写 / 重新识别」，因此要带上 manual 标记。"""
    store.put(H3, '手改', chat_id=CHAT, create_time=300, local_id=3, manual=True)
    row = asr_store.lookup_for_chat(CHAT, [(300, 3)], store=store)['300:3']
    assert row['manual'] == 1
    assert row['text'] == '手改'
