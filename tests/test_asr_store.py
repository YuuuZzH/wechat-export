# -*- coding: utf-8 -*-
"""转写结果存储层（issue #24）：语音转出来的文字要能留下、下次直接带出。

设计（已与用户确认）：
  * 存用户目录 `%LOCALAPPDATA%\\WeChatEXP\\transcripts.db`（跨备份/跨账号共享）；
  * 主键是**语音内容哈希** ⇒ 同一段语音在任何备份里都能命中；
  * 另存 (chat_id, create_time, local_id) 供按消息反查；
  * 只有**非空且成功**的结果才入库（宁可没有，也不存坏数据）；
  * 重复写入幂等（后写覆盖，但保留 created_at、刷新 updated_at）。
测试全部用合成文本，绝不写真实聊天内容。
"""
import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.services import asr_store  # noqa: E402

H1 = hashlib.sha1(b'voice-one').hexdigest()[:16]
H2 = hashlib.sha1(b'voice-two').hexdigest()[:16]


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(asr_store, 'store_path', lambda: str(tmp_path / 't.db'))
    s = asr_store.AsrStore(str(tmp_path / 't.db'))
    yield s
    s.close()


def test_put_then_get_roundtrip(store):
    store.put(H1, '合成文字一', chat_id='wxid_demo', create_time=1614933012, local_id=7,
              engine='local', model='whisper-base', language='zh', duration_s=3.5)
    row = store.get(H1)
    assert row['text'] == '合成文字一'
    assert row['engine'] == 'local' and row['duration_s'] == 3.5
    assert row['create_time'] == 1614933012 and row['local_id'] == 7
    assert row['manual'] == 0


def test_get_missing_returns_none(store):
    assert store.get(H1) is None


def test_get_many_returns_only_hits(store):
    store.put(H1, '甲')
    store.put(H2, '乙')
    got = store.get_many([H1, H2, 'ffffffffffffffff'])
    assert got[H1]['text'] == '甲' and got[H2]['text'] == '乙'
    assert 'ffffffffffffffff' not in got


def test_put_is_idempotent_and_keeps_created_at(store):
    store.put(H1, '第一版')
    first = store.get(H1)
    store.put(H1, '第二版')
    second = store.get(H1)
    assert second['text'] == '第二版'
    assert second['created_at'] == first['created_at'], 'created_at 应保留'
    assert store.stats()['total'] == 1, '同一哈希只应有一条'


def test_blank_text_is_not_stored(store):
    """空/空白结果不入库（失败或空识别不该污染存储）。"""
    store.put(H1, '')
    store.put(H2, '   \n')
    assert store.get(H1) is None and store.get(H2) is None
    assert store.stats()['total'] == 0


def test_manual_flag_can_be_set_and_read(store):
    store.put(H1, '手改过的文字', manual=True)
    assert store.get(H1)['manual'] == 1


def test_delete_and_stats(store):
    store.put(H1, '甲')
    store.put(H2, '乙')
    assert store.delete(H1) == 1
    assert store.get(H1) is None
    assert store.stats()['total'] == 1


def test_reopen_existing_db_keeps_rows(tmp_path):
    path = str(tmp_path / 'persist.db')
    s1 = asr_store.AsrStore(path)
    s1.put(H1, '留下的文字')
    s1.close()
    s2 = asr_store.AsrStore(path)
    assert s2.get(H1)['text'] == '留下的文字'
    s2.close()


def test_lookup_by_message_ids(store):
    """按 (chat_id, create_time, local_id) 反查（查看器载入会话时用）。"""
    store.put(H1, '甲', chat_id='wxid_demo', create_time=100, local_id=1)
    store.put(H2, '乙', chat_id='wxid_demo', create_time=200, local_id=2)
    rows = store.find_by_messages('wxid_demo', [(100, 1), (200, 2)])
    assert rows[(100, 1)]['text'] == '甲'
    assert rows[(200, 2)]['text'] == '乙'
