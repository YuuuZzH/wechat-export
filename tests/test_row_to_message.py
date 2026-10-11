"""Tests for _row_to_message — core message row to API dict conversion."""
import pytest
from engine.services.message import _row_to_message


def _make_row(local_id=1, ltype=1, origin=1, ts=1700000000,
              status=3, content='hello', sender_id=0, packed_info=None):
    """Helper: build a Msg_ table row tuple in the expected column order."""
    return (local_id, ltype, origin, ts, status, content, sender_id, packed_info)


class TestRowToMessageBasic:
    """Basic message type conversion tests — no real data needed."""

    def test_text_from_me(self):
        msg = _row_to_message(_make_row(content='你好世界', origin=1),
                              chat_id='wxid_abc')
        assert msg['id'] == 1
        assert msg['msg_type'] == 1
        assert msg['is_sender'] is True
        assert msg['sender_name'] == '我'
        assert msg['content'] == '你好世界'
        assert 'display_html' not in msg
        assert '你好世界' in msg['content']

    def test_text_from_other(self):
        msg = _row_to_message(_make_row(content='hello', origin=0),
                              chat_id='wxid_abc')
        assert msg['is_sender'] is False
        assert msg['sender_name'] == 'wxid_abc'

    def test_group_text_from_me(self):
        msg = _row_to_message(_make_row(content='group hello', origin=1),
                              chat_id='room@chatroom')
        assert msg['is_sender'] is True
        assert msg['sender_name'] == '我'

    def test_group_text_from_other_with_prefix(self):
        content = 'sender_wxid:\nactual group message'
        msg = _row_to_message(_make_row(content=content, origin=0),
                              chat_id='room@chatroom')
        assert msg['is_sender'] is False
        assert msg['sender_name'] == 'sender_wxid'
        # 展示用 content 必须剥掉 "sender_wxid:\n" 前缀：发送者已经由 sender_name
        # 单独呈现，正文里再留一份 wxid 是重复且泄漏身份的（且换行渲染开启后它会独占一行）。
        assert msg['content'] == 'actual group message'
        # 原始字符串仍保留在 content_raw 里，便于需要原样的消费方取用
        assert msg['content_raw'] == content

    def test_group_text_body_newline_is_preserved(self):
        """正文内部的换行必须原样保留（Web UI 用 white-space:pre-wrap 渲染）。"""
        content = 'sender_wxid:\n第一行\n第二行\n\n第四行'
        msg = _row_to_message(_make_row(content=content, origin=0),
                              chat_id='room@chatroom')
        assert msg['content'] == '第一行\n第二行\n\n第四行'
        assert msg['content'].count('\n') == 3

    def test_direct_text_newline_is_preserved(self):
        """一对一聊天无前缀，换行同样必须保留。"""
        msg = _row_to_message(_make_row(content='第一行\n第二行', origin=0),
                              chat_id='wxid_abc')
        assert msg['content'] == '第一行\n第二行'

    def test_image_type(self):
        msg = _row_to_message(_make_row(ltype=3, content=''),
                              chat_id='wxid_abc')
        assert msg['msg_type'] == 3
        assert 'display_html' not in msg

    def test_video_type(self):
        msg = _row_to_message(_make_row(ltype=43, content=''),
                              chat_id='wxid_abc')
        assert msg['msg_type'] == 43

    def test_file_type(self):
        msg = _row_to_message(_make_row(ltype=6, content=''),
                              chat_id='wxid_abc')
        assert msg['msg_type'] == 6

    def test_voice_type(self):
        msg = _row_to_message(_make_row(ltype=34, content=''),
                              chat_id='wxid_abc')
        assert msg['msg_type'] == 34

    def test_system_message_type(self):
        msg = _row_to_message(_make_row(ltype=10000, content='sys msg'),
                              chat_id='wxid_abc')
        assert msg['msg_type'] == 10000

    def test_group_system_message(self):
        content = 'sender:\nsystem content here'
        msg = _row_to_message(_make_row(ltype=10000, content=content, origin=0),
                              chat_id='room@chatroom')
        assert msg['is_sender'] is False
        assert msg['sender_name'] == '系统消息'

    def test_bytes_content_utf8(self):
        content = '你好世界'.encode('utf-8')
        msg = _row_to_message(_make_row(content=content),
                              chat_id='wxid_abc')
        assert isinstance(msg['content'], str)
        assert '你好世界' in msg['content']

    def test_bytes_content_latin1(self):
        content = b'hello world'
        msg = _row_to_message(_make_row(content=content),
                              chat_id='wxid_abc')
        assert isinstance(msg['content'], str)
        assert 'hello world' in msg['content']

    def test_none_content(self):
        msg = _row_to_message(_make_row(content=None),
                              chat_id='wxid_abc')
        assert msg['content'] is None or msg['content'] == ''

    def test_create_time_preserved(self):
        msg = _row_to_message(_make_row(ts=1704067200),
                              chat_id='wxid_abc')
        assert msg['create_time'] == 1704067200

    def test_local_id_preserved(self):
        msg = _row_to_message(_make_row(local_id=42),
                              chat_id='wxid_abc')
        assert msg['id'] == 42

    def test_group_text_without_prefix(self):
        """Group message from other without the 'sender:\n' prefix."""
        msg = _row_to_message(_make_row(content='plain message', origin=0),
                              chat_id='room@chatroom')
        assert msg['is_sender'] is False

    def test_emoji_type(self):
        msg = _row_to_message(_make_row(ltype=47, content=''),
                              chat_id='wxid_abc')
        assert msg['msg_type'] == 47

    def test_location_type(self):
        msg = _row_to_message(_make_row(ltype=48, content=''),
                              chat_id='wxid_abc')
        assert msg['msg_type'] == 48

    def test_contact_card_type(self):
        msg = _row_to_message(_make_row(ltype=42, content=''),
                              chat_id='wxid_abc')
        assert msg['msg_type'] == 42

    def test_voip_type(self):
        msg = _row_to_message(_make_row(ltype=50, content=''),
                              chat_id='wxid_abc')
        assert msg['msg_type'] == 50

    def test_ltype_masked_with_high_bits(self):
        """local_type with high bits set should be masked to base type."""
        ltype_raw = 1 | (0x10000)  # text with extra flags in high bits
        msg = _row_to_message(_make_row(ltype=ltype_raw, content='masked'),
                              chat_id='wxid_abc')
        assert msg['msg_type'] == 1

    def test_parse_xml_disabled(self):
        msg = _row_to_message(_make_row(ltype=49, content='<msg><appmsg><title>T</title></appmsg></msg>'),
                              chat_id='wxid_abc', parse_xml=False)
        assert msg['msg_type'] == 49

    @pytest.mark.parametrize('ltype,label_kw', [
        (1, '文本'),
        (3, '图片'),
        (6, '文件'),
        (34, '语音'),
        (42, '名片'),
        (43, '视频'),
        (47, '表情'),
        (48, '位置'),
        (50, '网络电话'),
    ])
    def test_all_common_message_types(self, ltype, label_kw):
        msg = _row_to_message(_make_row(ltype=ltype, content='test'),
                              chat_id='wxid_abc')
        assert msg['msg_type'] == ltype
        assert 'id' in msg
        assert 'is_sender' in msg
        assert 'create_time' in msg
        assert 'display_html' not in msg
