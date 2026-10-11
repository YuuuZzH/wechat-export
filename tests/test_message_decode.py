"""Tests for message/decode.py — zstd decompression and sender extraction."""
import pytest
from engine.services.message.decode import decompress_content, split_sender_prefix


class TestSplitSenderPrefix:
    def test_group_message_with_prefix(self):
        raw = 'wxid_abc:\nhello world'
        sender, body = split_sender_prefix(raw, is_group=True, is_sender=False)
        assert sender == 'wxid_abc'
        assert body == 'hello world'

    def test_own_message_no_split(self):
        raw = 'hello world'
        sender, body = split_sender_prefix(raw, is_group=True, is_sender=True)
        assert sender == ''
        assert body == 'hello world'

    def test_non_group_no_split(self):
        raw = 'wxid_abc:\nhello'
        sender, body = split_sender_prefix(raw, is_group=False, is_sender=False)
        assert sender == ''
        assert body == 'wxid_abc:\nhello'

    def test_no_colon_newline(self):
        raw = 'plain message without prefix'
        sender, body = split_sender_prefix(raw, is_group=True, is_sender=False)
        assert sender == ''
        assert body == 'plain message without prefix'


class TestDecompressContent:
    def test_non_zstd_passthrough(self):
        data = b'plain text'
        result = decompress_content(data)
        assert result == data

    def test_none_passthrough(self):
        result = decompress_content(None)
        assert result is None
