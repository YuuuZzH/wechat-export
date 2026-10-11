# -*- coding: utf-8 -*-
"""issue #24 第④步：导出时按 (会话, create_time, local_id) 取回语音文字。

`make_asr_lookup()` 是导出侧与转写存储之间的桥：**只读存储、绝不触发识别**（导出不该隐式跑模型）。
"""
import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chat_export import make_asr_lookup  # noqa: E402
from engine.services import asr_store  # noqa: E402

CHAT = 'wxid_demo_chat'
H1 = hashlib.sha1(b'a').hexdigest()[:16]


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(asr_store, 'store_path', lambda: str(tmp_path / 't.db'))
    s = asr_store.AsrStore(str(tmp_path / 't.db'))
    yield s
    s.close()


def test_lookup_returns_text_for_known_voice(store):
    store.put(H1, '已经转好的文字', chat_id=CHAT, create_time=1614933012, local_id=7)
    lookup = make_asr_lookup(CHAT, store=store)
    assert lookup(1614933012, 7) == '已经转好的文字'


def test_lookup_returns_none_for_unknown_voice(store):
    lookup = make_asr_lookup(CHAT, store=store)
    assert lookup(1614933012, 7) is None


def test_lookup_is_scoped_to_chat(store):
    store.put(H1, '别的会话', chat_id='wxid_other', create_time=1614933012, local_id=7)
    lookup = make_asr_lookup(CHAT, store=store)
    assert lookup(1614933012, 7) is None


def test_lookup_survives_bad_input(store):
    """导出的边界情况（None/怪值）不能把整个导出搞崩。"""
    lookup = make_asr_lookup(CHAT, store=store)
    assert lookup(None, None) is None
    assert lookup('x', 'y') is None


def test_export_chat_accepts_include_asr_flag():
    """导出入口要接受开关（默认关闭 ⇒ 不改变既有输出）。"""
    import inspect

    from chat_export import export_chat
    sig = inspect.signature(export_chat)
    assert 'include_asr' in sig.parameters
    assert sig.parameters['include_asr'].default is False
    assert 'asr_lookup' in sig.parameters
