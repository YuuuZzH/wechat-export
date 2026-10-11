"""Tests for _pick_display_name — the canonical display-name resolver."""
import pytest
from engine.services.name_resolver import pick_display_name as _pick_display_name


class TestPickDisplayName:
    def test_remark_takes_priority_when_different(self):
        result = _pick_display_name('wxid_abc', 'Mom', 'Nickname', 'AliasName', 'wxid_abc')
        assert result == 'Mom'

    def test_remark_skipped_when_equals_username(self):
        result = _pick_display_name('wxid_abc', 'wxid_abc', 'Nickname', 'AliasName', 'wxid_abc')
        assert result == 'Nickname'

    def test_nick_skipped_when_equals_username(self):
        result = _pick_display_name('wxid_abc', None, 'wxid_abc', 'AliasName', 'wxid_abc')
        assert result == 'AliasName'

    def test_alias_skipped_when_equals_username(self):
        result = _pick_display_name('wxid_abc', None, None, 'wxid_abc', 'wxid_abc')
        assert result == 'wxid_abc'

    def test_all_fields_none_returns_username(self):
        result = _pick_display_name('wxid_abc', None, None, None, 'wxid_abc')
        assert result == 'wxid_abc'

    def test_all_fields_empty_returns_username(self):
        result = _pick_display_name('wxid_abc', '', '', '', 'wxid_abc')
        assert result == 'wxid_abc'

    def test_whitespace_fields_treated_as_empty(self):
        result = _pick_display_name('wxid_abc', '   ', '\t', '\n', 'wxid_abc')
        assert result == 'wxid_abc'

    def test_db_username_overrides_wxid_for_skip_check(self):
        result = _pick_display_name('wxid_abc', 'special_name', None, None, 'db_user_123')
        assert result == 'special_name'

    def test_remark_equals_db_username_is_skipped(self):
        result = _pick_display_name('wxid_abc', 'db_user_123', 'Nick', None, 'db_user_123')
        assert result == 'Nick'

    def test_returns_nick_when_remark_is_none(self):
        result = _pick_display_name('wxid_abc', None, 'Nick', None, 'wxid_abc')
        assert result == 'Nick'

    def test_returns_alias_when_remark_and_nick_are_none(self):
        result = _pick_display_name('wxid_abc', None, None, 'Alias', 'wxid_abc')
        assert result == 'Alias'

    def test_wxid_fallback_when_db_username_is_none(self):
        result = _pick_display_name('wxid_xyz', None, None, None, None)
        assert result == 'wxid_xyz'

    @pytest.mark.parametrize('remark,nick,alias,db_uname,expected', [
        ('Mom', 'Nick', 'Alias', 'wxid_1', 'Mom'),
        ('wxid_1', 'Nick', 'Alias', 'wxid_1', 'Nick'),
        (None, 'wxid_1', 'Alias', 'wxid_1', 'Alias'),
        (None, None, 'wxid_1', 'wxid_1', 'wxid_1'),
        ('Rem', None, None, 'wxid_1', 'Rem'),
        ('wxid_1', 'wxid_1', 'wxid_1', 'wxid_1', 'wxid_1'),
        ('  ', 'Nick', None, 'wxid_1', 'Nick'),
        ('wxid_1', '  ', 'Alias', 'wxid_1', 'Alias'),
        ('wxid_1', 'wxid_1', '  ', 'wxid_1', 'wxid_1'),
    ])
    def test_fallback_chain(self, remark, nick, alias, db_uname, expected):
        result = _pick_display_name('wxid_1', remark, nick, alias, db_uname)
        assert result == expected
