# -*- coding: utf-8 -*-
"""issue #24 第⑤步：把转写文字填进语音导出的条目里。

语音导出层（`VoiceItem.transcript` / 清单的 `transcript` 列 / 独立 HTML 的文字稿渲染与过滤）
本来就已支持，只是**没有数据来源**。这里补上"从转写存储批量回填"这一步：
  * 只读存储、**不触发识别**；
  * 没有转写过的语音 transcript 保持空串（导出里显示"未转写"由 HTML 侧处理）。
"""
import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.services import asr_store  # noqa: E402
from engine.services.voice_export.model import VoiceItem  # noqa: E402
from engine.services.voice_export.transcripts import apply_transcripts  # noqa: E402

CHAT = 'wxid_demo_chat'
H1 = hashlib.sha1(b'a').hexdigest()[:16]


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(asr_store, 'store_path', lambda: str(tmp_path / 't.db'))
    s = asr_store.AsrStore(str(tmp_path / 't.db'))
    yield s
    s.close()


def _item(ct, lid, chat=CHAT):
    return VoiceItem(chat_id=chat, chat_name=chat, local_id=lid, create_time=ct,
                     sender_name='甲', silk_path='x.silk')


def test_apply_transcripts_fills_matching_items(store):
    store.put(H1, '已经转好的文字', chat_id=CHAT, create_time=100, local_id=1)
    items = [_item(100, 1), _item(200, 2)]
    n = apply_transcripts(items, store=store)
    assert n == 1
    assert items[0].transcript == '已经转好的文字'
    assert items[1].transcript == '', '没转写过的保持空'


def test_apply_transcripts_is_scoped_to_chat(store):
    store.put(H1, '别的会话', chat_id='wxid_other', create_time=100, local_id=1)
    items = [_item(100, 1)]
    assert apply_transcripts(items, store=store) == 0
    assert items[0].transcript == ''


def test_apply_transcripts_empty_input(store):
    assert apply_transcripts([], store=store) == 0


def test_apply_transcripts_does_not_trigger_recognition(store, monkeypatch):
    """绝不能因为导出而偷偷跑识别（导出应是只读的）。"""
    import engine.services.media as media

    def boom(*a, **k):
        raise AssertionError('导出不应触发识别')

    monkeypatch.setattr(media, 'transcribe_voice', boom)
    monkeypatch.setattr(media, 'transcribe_voice_cached', boom)
    apply_transcripts([_item(100, 1)], store=store)
