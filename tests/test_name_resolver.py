"""Tests for name_resolver — single source of truth for wxid → display_name."""
import pytest
import sqlite3
import os
import tempfile
from engine.services.name_resolver import resolve_wxid, pick_display_name


class TestPickDisplayName:
    """Most tests move from test_display_name.py — verify same behavior."""

    def test_remark_takes_priority_when_different(self):
        result = pick_display_name('wxid_abc', 'Mom', 'Nickname', 'AliasName', 'wxid_abc')
        assert result == 'Mom'

    def test_remark_skipped_when_equals_username(self):
        result = pick_display_name('wxid_abc', 'wxid_abc', 'Nickname', 'AliasName', 'wxid_abc')
        assert result == 'Nickname'

    def test_nick_skipped_when_equals_username(self):
        result = pick_display_name('wxid_abc', None, 'wxid_abc', 'AliasName', 'wxid_abc')
        assert result == 'AliasName'

    def test_alias_skipped_when_equals_username(self):
        result = pick_display_name('wxid_abc', None, None, 'wxid_abc', 'wxid_abc')
        assert result == 'wxid_abc'

    def test_all_fields_none_returns_username(self):
        result = pick_display_name('wxid_abc', None, None, None, 'wxid_abc')
        assert result == 'wxid_abc'

    def test_all_fields_empty_returns_username(self):
        result = pick_display_name('wxid_abc', '', '', '', 'wxid_abc')
        assert result == 'wxid_abc'

    def test_returns_alias_when_remark_and_nick_are_none(self):
        result = pick_display_name('wxid_1', None, None, 'Alias', 'wxid_1')
        assert result == 'Alias'

    def test_wxid_fallback_when_db_username_is_none(self):
        result = pick_display_name('wxid_xyz', None, None, None, None)
        assert result == 'wxid_xyz'

    def test_whitespace_fields_treated_as_empty(self):
        result = pick_display_name('wxid_abc', '   ', '\t', '\n', 'wxid_abc')
        assert result == 'wxid_abc'

    def test_db_username_overrides_wxid_for_skip_check(self):
        result = pick_display_name('wxid_abc', 'special_name', None, None, 'db_user_123')
        assert result == 'special_name'

    def test_remark_equals_db_username_is_skipped(self):
        result = pick_display_name('wxid_abc', 'db_user_123', 'Nick', None, 'db_user_123')
        assert result == 'Nick'

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
        result = pick_display_name('wxid_1', remark, nick, alias, db_uname)
        assert result == expected


class TestResolveWxid:
    """Tests that require an in-memory contact.db."""

    @pytest.fixture
    def contact_db_path(self):
        tmp = tempfile.mkdtemp()
        db_path = os.path.join(tmp, 'contact', 'contact.db')
        os.makedirs(os.path.dirname(db_path))
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE contact (id INTEGER, username TEXT, remark TEXT, nick_name TEXT, alias TEXT, small_head_url TEXT)")
        conn.execute("INSERT INTO contact VALUES (1, 'wxid_zhangsan', '张三', '小张', 'zs_alias', '')")
        conn.execute("INSERT INTO contact VALUES (2, 'wxid_lisi', NULL, '李四', NULL, '')")
        conn.execute("INSERT INTO contact VALUES (3, 'wxid_wangwu', 'wxid_wangwu', 'wxid_wangwu', 'wxid_wangwu', '')")
        conn.commit()
        conn.close()
        yield tmp
        import shutil
        shutil.rmtree(tmp)

    @pytest.fixture(autouse=True)
    def clear_cache(self):
        from engine.services.name_resolver import _name_cache
        _name_cache.clear()

    def test_resolve_by_remark(self, contact_db_path):
        result = resolve_wxid(contact_db_path, 'wxid_zhangsan')
        assert result == '张三'

    def test_resolve_by_nick_when_no_remark(self, contact_db_path):
        result = resolve_wxid(contact_db_path, 'wxid_lisi')
        assert result == '李四'

    def test_resolve_all_fields_equal_returns_wxid(self, contact_db_path):
        result = resolve_wxid(contact_db_path, 'wxid_wangwu')
        assert result == 'wxid_wangwu'

    def test_resolve_unknown_returns_wxid(self, contact_db_path):
        result = resolve_wxid(contact_db_path, 'wxid_nonexistent')
        assert result == 'wxid_nonexistent'

    def test_resolve_empty_string(self, contact_db_path):
        result = resolve_wxid(contact_db_path, '')
        assert result == ''

    def test_cache_returns_same_result(self, contact_db_path):
        a = resolve_wxid(contact_db_path, 'wxid_zhangsan')
        b = resolve_wxid(contact_db_path, 'wxid_zhangsan')
        assert a == b == '张三'

    def test_resolve_chatroom_fuzzy_match(self, contact_db_path):
        """@chatroom suffix stripped, LIKE fuzzy match on base name."""
        result = resolve_wxid(contact_db_path, 'wxid_zhangsan@chatroom')
        assert result == '张三'

    def test_resolve_openim_fuzzy_match(self, contact_db_path):
        """@openim suffix stripped, LIKE fuzzy match on base name."""
        result = resolve_wxid(contact_db_path, 'wxid_zhangsan@openim')
        assert result == '张三'

    def test_resolve_no_contact_db(self, tmp_path):
        """No contact.db exists — returns the wxid as-is."""
        result = resolve_wxid(str(tmp_path), 'wxid_zhangsan')
        assert result == 'wxid_zhangsan'

    def test_resolve_corrupt_db_returns_wxid(self, tmp_path):
        """Corrupt contact.db — graceful degradation, returns wxid."""
        db_dir = tmp_path / 'contact'
        db_dir.mkdir()
        db_file = db_dir / 'contact.db'
        db_file.write_bytes(b'this is not a valid sqlite database')
        result = resolve_wxid(str(tmp_path), 'wxid_test')
        assert result == 'wxid_test'

    def test_cache_size_limit(self, contact_db_path):
        """Cache stays under max; oldest entries evicted."""
        from engine.services.name_resolver import _name_cache, _NAME_CACHE_MAX
        _name_cache.clear()
        for i in range(_NAME_CACHE_MAX + 1):
            resolve_wxid(contact_db_path, f'wxid_dummy_{i:04d}')
        assert len(_name_cache) <= _NAME_CACHE_MAX
        first_key = f'{contact_db_path}:wxid_dummy_0000'
        assert first_key not in _name_cache
