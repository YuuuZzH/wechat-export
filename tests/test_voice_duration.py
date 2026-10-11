"""语音时长解析回归测试（48″ 假时长 bug）。

背景：界面上每条语音都显示同一个时长（例如 48″），而真实音频只有 11 秒，
导致用户以为「48 秒的长语音转写被截断成一行」。
根因：媒体信息里的 duration 取自 packed_info 的字段 1（与真实长度无关），
而真实时长在 <voicemsg voicelength="毫秒"> 里。
"""
import pytest

from engine.parsers.types import parse_voice
from engine.services.message import _row_to_message

VOICE_XML = (
    '<msg><voicemsg endflag="1" cancelflag="0" forwardflag="0" voiceformat="4" '
    'voicelength="11462" length="18554" bufid="0" aeskey="39a31f275b0b20e6db64666552f46600" '
    'voiceurl="7f0c00080220d46d1929d754046c7bfda44878048102de24ee6acef8a7a8882ab656bf2aed7c03" '
    'voicemd5="" clientmsgid="x" fromusername="wxid_example12345" silklength="0" /></msg>'
)

# packed_info protobuf：字段 1 = 48（真实数据里这个值对所有语音都一样，不是时长）
PACKED_BOGUS_DURATION = bytes([0x08, 0x30])


def _row(ltype=34, content=VOICE_XML, packed=None, lid=1585, ts=1789531971):
    return (lid, ltype, 0, ts, 3, content, 48, packed)


class TestParseVoiceDuration:
    def test_milliseconds_converted_to_seconds(self):
        parsed = parse_voice(VOICE_XML.encode('utf-8'))
        assert parsed['duration'] == 11, parsed
        assert parsed['duration_ms'] == 11462, parsed

    def test_short_voice_rounds_up_to_seconds(self):
        xml = VOICE_XML.replace('voicelength="11462"', 'voicelength="2600"')
        assert parse_voice(xml.encode('utf-8'))['duration'] == 3

    def test_long_voice(self):
        xml = VOICE_XML.replace('voicelength="11462"', 'voicelength="32296"')
        parsed = parse_voice(xml.encode('utf-8'))
        assert parsed['duration'] == 32
        assert parsed['duration_ms'] == 32296

    def test_missing_voicelength_is_none(self):
        xml = VOICE_XML.replace('voicelength="11462" ', '')
        parsed = parse_voice(xml.encode('utf-8'))
        assert parsed['duration'] is None
        assert parsed['duration_ms'] is None

    def test_length_is_not_used_as_seconds(self):
        """length 是字节数，绝不能当秒用。"""
        xml = VOICE_XML.replace('voicelength="11462" ', '')
        parsed = parse_voice(xml.encode('utf-8'))
        assert parsed['duration'] != 18554


class TestRowToMessageVoiceDuration:
    def test_xml_duration_overrides_packed_info(self):
        msg = _row_to_message(_row(packed=PACKED_BOGUS_DURATION), chat_id='wxid_abc')
        assert msg['msg_type'] == 34
        assert msg['xml_parsed']['duration'] == 11
        # 关键：媒体信息里的假时长（48）必须被真实时长覆盖
        assert msg['media_info']['duration'] == 11, msg['media_info']

    def test_media_info_created_when_packed_info_missing(self):
        msg = _row_to_message(_row(packed=None), chat_id='wxid_abc')
        assert msg['media_info']['duration'] == 11
        assert msg['media_info']['media_type'] == 34

    def test_voice_path_still_exposed(self):
        msg = _row_to_message(_row(packed=None), chat_id='wxid_abc')
        assert msg['xml_parsed']['voice_path'].startswith('7f0c0008')

    def test_other_types_untouched(self):
        video = _row_to_message(_row(ltype=43, content=''), chat_id='wxid_abc')
        assert video['msg_type'] == 43
