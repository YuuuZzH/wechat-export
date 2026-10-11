# -*- coding: utf-8 -*-
"""把转写存储里的文字回填到语音导出条目（issue #24 第⑤步）。

语音导出层（`VoiceItem.transcript`、清单的 `transcript` 列、独立 HTML 的文字稿渲染与
"文字稿关键字"过滤）早就支持，只缺数据来源。这里补上这一环，并且**只读存储、不触发识别**：
导出不该偷偷跑模型（本地 Whisper 几秒/条，几百条会卡很久）。
"""
import os


def apply_transcripts(items, store=None) -> int:
    """按 (chat_id, create_time, local_id) 批量回填 `item.transcript`；返回填了几条。"""
    items = list(items or [])
    if not items:
        return 0
    from .. import asr_store as _s
    own = store is None
    st = store or _s.AsrStore()
    filled = 0
    try:
        by_chat = {}
        for it in items:
            by_chat.setdefault(it.chat_id or '', []).append(it)
        for chat_id, group in by_chat.items():
            pairs = [(it.create_time, it.local_id) for it in group]
            rows = _s.lookup_for_chat(chat_id, pairs, store=st)
            for it in group:
                row = rows.get('%d:%d' % (int(it.create_time), int(it.local_id)))
                text = (row or {}).get('text') or ''
                if text:
                    it.transcript = text
                    filled += 1
    finally:
        if own:
            st.close()
    return filled
