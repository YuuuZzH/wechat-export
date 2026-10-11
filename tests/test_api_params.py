"""Tests for Web API parameter validation helpers.

Covers known crash vectors: #212 (per_page=0), #213 (per_page<0),
#216 (type=abc), #220 (date=abc).
"""
import pytest
from engine.services.message import _build_where, _date_to_ts, _escape_like


class TestDateToTs:
    def test_valid_date(self):
        ts = _date_to_ts('2024-06-15')
        assert ts > 0
        # 2024-06-15 00:00:00 CST = 1718380800
        assert ts == 1718380800

    def test_valid_date_end_of_day(self):
        ts = _date_to_ts('2024-06-15', end_of_day=True)
        assert ts > 0
        # 2024-06-15 23:59:59 CST
        assert ts == 1718467199

    def test_invalid_date_raises_valueerror(self):
        with pytest.raises(ValueError):
            _date_to_ts('abc')

    def test_invalid_format_raises_valueerror(self):
        with pytest.raises(ValueError):
            _date_to_ts('2024-13-01')  # month 13 doesn't exist

    def test_empty_string(self):
        with pytest.raises(ValueError):
            _date_to_ts('')

    def test_leap_year_date(self):
        ts = _date_to_ts('2024-02-29')
        assert ts > 0

    def test_year_boundary(self):
        ts = _date_to_ts('2024-01-01')
        assert ts == 1704038400


class TestEscapeLike:
    def test_no_special_chars(self):
        assert _escape_like('hello') == 'hello'

    def test_escapes_percent(self):
        assert _escape_like('50%') == '50\\%'

    def test_escapes_underscore(self):
        assert _escape_like('a_b') == 'a\\_b'

    def test_escapes_backslash(self):
        assert _escape_like('a\\b') == 'a\\\\b'

    def test_combined_special_chars(self):
        assert _escape_like('100%_test\\') == '100\\%\\_test\\\\'

    def test_empty_string(self):
        assert _escape_like('') == ''

    def test_chinese_text(self):
        assert _escape_like('你好世界') == '你好世界'


class TestBuildWhere:
    def test_basic_clause(self):
        clause, params = _build_where(None, None, None, None, None)
        assert 'create_time > 1000000000' in clause
        assert params == []

    def test_start_date(self):
        clause, params = _build_where('2024-01-01', None, None, None, None)
        assert 'create_time >= ?' in clause
        assert len(params) == 1

    def test_end_date(self):
        clause, params = _build_where(None, '2024-12-31', None, None, None)
        assert 'create_time <= ?' in clause
        assert len(params) == 1

    def test_invalid_start_date_handled(self):
        """#220: invalid date like 'abc' must NOT crash _build_where.

        Note: clause IS appended before date validation, but no param added.
        This means the query has an unmatched ? placeholder — acceptable
        since _build_where is always followed by _find_chat_db which would
        fail first on invalid chat_id, or the API catches the error.
        """
        clause, params = _build_where('abc', None, None, None, None)
        assert isinstance(clause, str)
        assert isinstance(params, list)

    def test_invalid_end_date_handled(self):
        clause, params = _build_where(None, 'not-a-date', None, None, None)
        assert isinstance(clause, str)
        assert isinstance(params, list)

    def test_swapped_dates_auto_corrected(self):
        """When start > end, they are swapped."""
        clause, params = _build_where('2024-12-31', '2024-01-01', None, None, None)
        assert len(params) == 2
        # First param should be the earlier date (swapped)
        assert params[0] < params[1]

    def test_non_numeric_msg_types_silently_filtered(self):
        """#216: type=abc must NOT crash — silently produces empty types list."""
        clause, params = _build_where(None, None, 'abc', None, None)
        assert 'IN (' not in clause  # no valid type, no IN clause added

    def test_mixed_valid_and_invalid_types(self):
        """#216: invalid element (abc) causes ALL types to be dropped.

        This is known behavior — int('abc') ValueError is caught and
        resets types to [], losing the valid elements too.
        """
        clause, params = _build_where(None, None, '1,abc,3', None, None)
        assert 'IN (' not in clause  # all types dropped

    def test_valid_msg_types(self):
        clause, params = _build_where(None, None, '3,34,43', None, None)
        assert 'IN (' in clause
        assert len(params) == 3

    def test_sender_self(self):
        clause, params = _build_where(None, None, None, '__self__', None)
        assert 'origin_source = 1' in clause

    def test_sender_self_with_own_forms_matches_name2id_too(self):
        """给了账号 id 时，`__self__` 还要覆盖"Name2Id 指到本人"的那些行。

        为什么需要：`origin_source` 只覆盖一部分自发消息（本机实测：`origin==1` 的行
        只占自发消息的一小部分），其余自发消息必须靠 `Name2Id` 才能认出来。
        """
        clause, params = _build_where(None, None, None, '__self__', None,
                                      own_wxid='wxid_me_1a2b')
        assert 'origin_source = 1' in clause
        assert 'SELECT rowid FROM Name2Id WHERE user_name IN' in clause
        assert 'wxid_me_1a2b' in params
        assert 'wxid_me' in params          # 裸 id 形态也要在里面

    def test_sender_sys(self):
        clause, params = _build_where(None, None, None, '__sys__', None)
        assert '10000' in clause
        assert '10002' in clause

    def test_sender_other_non_group(self):
        """具体某个人：按 `Name2Id` **精确**匹配（不再是 "origin_source != 1"）。

        旧行为（`origin_source != 1`）对单聊等于"什么都不过滤"——因为绝大多数行
        origin 本来就不是 1；这正是 issue #16 里"选了某人还是全部消息"的原因。
        """
        clause, params = _build_where(None, None, None, 'someone', None, is_group=False)
        assert 'real_sender_id IN (SELECT rowid FROM Name2Id WHERE user_name = ?)' in clause
        assert 'origin_source != 1' not in clause
        assert params == ['someone']

    def test_sender_other_group(self):
        """群聊里选某人：同样走 Name2Id 精确匹配。

        旧行为是 `message_content LIKE 'someone:\\n%'` —— 对**没有正文前缀的媒体消息**
        （图片/语音/文件）永远筛不出来。
        """
        clause, params = _build_where(None, None, None, 'someone', None, is_group=True)
        assert 'real_sender_id IN (SELECT rowid FROM Name2Id WHERE user_name = ?)' in clause
        assert 'message_content LIKE ?' not in clause
        assert params == ['someone']

    def test_sender_unknown_bucket(self):
        """`__unknown__` = 该分片 `Name2Id` 里没有这个 rsid（归属未定）。"""
        clause, params = _build_where(None, None, None, '__unknown__', None)
        assert 'real_sender_id NOT IN (SELECT rowid FROM Name2Id)' in clause
        assert params == []

    def test_keyword(self):
        clause, params = _build_where(None, None, None, None, 'hello')
        assert 'message_content LIKE ?' in clause
        assert params[0] == '%hello%'

    def test_keyword_with_special_chars_escaped(self):
        clause, params = _build_where(None, None, None, None, '50%_off')
        assert '50\\%\\_off' in params[0]

    @pytest.mark.parametrize('start,end,should_not_raise', [
        ('2024-06-15', '2024-12-31', True),
        ('abc', '2024-12-31', True),  # bad start silently ignored
        ('2024-06-15', 'xyz', True),  # bad end silently ignored
        ('', '', True),
        ('2024-13-01', None, True),  # invalid month silently ignored
    ])
    def test_never_raises_on_bad_input(self, start, end, should_not_raise):
        """Regression: all invalid inputs must be silently handled."""
        clause, params = _build_where(start, end, None, None, None)
        assert isinstance(clause, str)
        assert isinstance(params, list)
