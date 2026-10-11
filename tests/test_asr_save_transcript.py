# -*- coding: utf-8 -*-
"""issue #24 第③步：前端把**本地识别**得到的文字回传入库（默认引擎跑在浏览器里，不经服务端）。

回传后要能被 `GET /api/voice/transcripts` 按会话取回，导出（④⑤）也才看得到。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.services import asr_store  # noqa: E402

CHAT = 'wxid_demo_chat'


@pytest.fixture()
def env(tmp_path, monkeypatch):
    dec = tmp_path / 'decrypted'
    d = dec / 'media' / 'voice'
    d.mkdir(parents=True)
    (d / 'demo.silk').write_bytes(b'SILK-DEMO')
    monkeypatch.setattr(asr_store, 'store_path', lambda: str(tmp_path / 't.db'))
    return str(dec)


def test_save_then_lookup_by_chat(env):
    h = asr_store.save_transcript_for_voice(env, 'demo.silk', '本地识别出来的文字',
                                            chat=CHAT, create_time=100, local_id=1,
                                            engine='local', model='whisper-base')
    assert h, '应返回内容哈希'
    got = asr_store.lookup_for_chat(CHAT, [(100, 1)])
    assert got['100:1']['text'] == '本地识别出来的文字'
    assert got['100:1']['engine'] == 'local'


def test_blank_text_not_saved(env):
    assert asr_store.save_transcript_for_voice(env, 'demo.silk', '   ', chat=CHAT,
                                               create_time=100, local_id=1) == ''
    assert asr_store.lookup_for_chat(CHAT, [(100, 1)]) == {}


def test_missing_voice_file_returns_blank(tmp_path, monkeypatch):
    monkeypatch.setattr(asr_store, 'store_path', lambda: str(tmp_path / 't.db'))
    h = asr_store.save_transcript_for_voice(str(tmp_path), 'nope.silk', '文字', chat=CHAT,
                                            create_time=1, local_id=2)
    assert h == ''


def test_same_content_different_name_hits_same_row(env):
    """内容相同 ⇒ 同一哈希 ⇒ 只存一条（不同文件名/不同备份目录都能命中）。"""
    asr_store.save_transcript_for_voice(env, 'demo.silk', '第一次', chat=CHAT,
                                        create_time=100, local_id=1)
    d = os.path.join(env, 'media', 'voice')
    with open(os.path.join(d, 'copy.silk'), 'wb') as f:
        f.write(b'SILK-DEMO')
    asr_store.save_transcript_for_voice(env, 'copy.silk', '第二次', chat=CHAT,
                                        create_time=100, local_id=1)
    st = asr_store.AsrStore()
    assert st.get(asr_store._hash_of_path(os.path.join(d, 'demo.silk')))['text'] == '第二次'
    assert st.stats()['total'] == 1
    st.close()
