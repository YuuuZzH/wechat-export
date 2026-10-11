# -*- coding: utf-8 -*-
"""issue #24 第④步收口：export_chat 能**自己**从 chat_info 推出会话 id 并建立转写查询。

这样所有调用方（`export_all_contacts` / Web 导出页 / CLI / 员工报表）只需传一个
`include_asr=True`，不必各自去拼 chat_id 与 lookup —— 接入口径统一在一处。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chat_export import _chat_id_of  # noqa: E402


def test_chat_id_from_dict_keys():
    """chat_info 是 dict 时，按常见键名取会话 id。"""
    assert _chat_id_of({'username': 'wxid_a'}) == 'wxid_a'
    assert _chat_id_of({'chat_id': 'wxid_b'}) == 'wxid_b'
    assert _chat_id_of({'user_name': 'wxid_c'}) == 'wxid_c'


def test_chat_id_from_object_attr():
    class C:
        username = 'wxid_obj'

    assert _chat_id_of(C()) == 'wxid_obj'


def test_chat_id_empty_returns_blank():
    assert _chat_id_of({}) == ''
    assert _chat_id_of(None) == ''
