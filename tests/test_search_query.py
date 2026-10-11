"""全局搜索的查询语法解析（纯函数，无 IO）。

语法见 docs/superpowers/specs/2026-09-21-global-search-design.md 第 4 节。
同字段重复 = OR，跨字段 = AND，空查询由调用方返回 400。
"""
import os
import re
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

from engine.constants import TZ
from engine.services import search_query as sq


class TestBareKeywords:
    def test_single_keyword(self):
        got = sq.parse_query('维修')
        assert got['keywords'] == ['维修']

    def test_space_is_and(self):
        got = sq.parse_query('维修 电话')
        assert got['keywords'] == ['维修', '电话']

    def test_chinese_fullwidth_colon_not_treated_as_field(self):
        got = sq.parse_query('测试：逗号')
        assert got['keywords'] == ['测试：逗号']

    def test_quoted_phrase(self):
        got = sq.parse_query('"服务器 宕机"')
        assert got['phrases'] == ['服务器 宕机']
        assert got['keywords'] == []


class TestExclude:
    def test_dash_prefix(self):
        got = sq.parse_query('维修 -退货')
        assert got['keywords'] == ['维修']
        assert got['exclude'] == ['退货']

    def test_lone_dash_is_a_keyword(self):
        assert sq.parse_query('-').get('exclude') == []

    def test_negated_field_filter_is_an_error(self):
        got = sq.parse_query('-会话:张三')
        assert got['chats'] == []
        assert got['exclude'] == []
        assert got['errors']

    def test_negated_field_is_never_applied_positively(self):
        got = sq.parse_query('-类型:图片')
        assert got['types'] == []
        assert got['errors']


class TestOrGroups:
    def test_or_creates_group(self):
        got = sq.parse_query('维修 OR 报修')
        assert got['or_groups'] == [['维修', '报修']]
        assert got['keywords'] == []

    def test_or_is_case_insensitive(self):
        assert sq.parse_query('a or b')['or_groups'] == [['a', 'b']]

    def test_pipe_is_or(self):
        assert sq.parse_query('a | b')['or_groups'] == [['a', 'b']]

    def test_or_chain(self):
        assert sq.parse_query('a OR b OR c')['or_groups'] == [['a', 'b', 'c']]

    def test_leading_or_is_an_error(self):
        got = sq.parse_query('OR 维修')
        assert got['errors']
        assert got['keywords'] == ['维修']

    def test_or_after_field_filter_reports_unified_message(self):
        got = sq.parse_query('会话:x OR a')
        assert got['or_groups'] == []
        assert len(got['errors']) == 1
        assert 'OR 需要左右两侧都是关键词' in got['errors'][0]['message']

    def test_trailing_or_is_an_error(self):
        got = sq.parse_query('a OR')
        assert got['or_groups'] == []
        assert got['errors']

    def test_or_with_phrase_is_an_error(self):
        got = sq.parse_query('a OR "b c"')
        assert got['or_groups'] == []
        assert got['phrases'] == ['b c']
        assert got['errors']

    def test_or_with_regex_is_an_error(self):
        got = sq.parse_query('a OR /x/')
        assert got['or_groups'] == []
        assert got['errors']

    def test_or_with_field_filter_is_an_error(self):
        got = sq.parse_query('a OR 会话:x')
        assert got['or_groups'] == []
        assert got['errors']


class TestFieldFilters:
    def test_chat(self):
        assert sq.parse_query('会话:张三')['chats'] == ['张三']

    def test_sender(self):
        assert sq.parse_query('发送者:我')['senders'] == ['我']

    def test_label(self):
        assert sq.parse_query('标签:only_work')['labels'] == ['only_work']

    def test_comma_is_or_within_field(self):
        assert sq.parse_query('标签:non_work,only_work')['labels'] == ['non_work', 'only_work']

    def test_repeated_field_is_or(self):
        got = sq.parse_query('会话:张三 会话:李四')
        assert got['chats'] == ['张三', '李四']

    def test_field_and_keyword_combine(self):
        got = sq.parse_query('维修 会话:张三')
        assert got['keywords'] == ['维修']
        assert got['chats'] == ['张三']

    def test_fullwidth_colon_accepted(self):
        assert sq.parse_query('会话：张三')['chats'] == ['张三']

    def test_unknown_field_is_treated_as_keyword(self):
        got = sq.parse_query('颜色:红')
        assert got['keywords'] == ['颜色:红']
        assert got['errors'] == []


class TestTokenizeQuotedFieldValues:
    """需求 1 的直接契约：'字段:"…"' 必须是**单个** term token，未闭合必须是 error token。"""

    def test_field_quoted_value_is_one_term_token(self):
        assert sq._tokenize('会话:"my chat"') == [('term', '会话:"my chat"')]

    def test_single_quoted_segment_consumes_to_closing_quote(self):
        assert sq._tokenize('会话:"a b" 维修') == [('term', '会话:"a b"'), ('term', '维修')]

    def test_unclosed_field_quote_is_an_error_token(self):
        assert sq._tokenize('会话:"my chat') == [('error', '会话:"my chat')]

    def test_fullwidth_colon_is_recognised(self):
        assert sq._tokenize('会话："my chat"') == [('term', '会话："my chat"')]

    def test_plain_phrase_is_not_hijacked(self):
        assert sq._tokenize('"报修 电话"') == [('phrase', '报修 电话')]

    def test_unknown_field_is_not_treated_as_quoted_field(self):
        # '颜色' 不是合法字段：与既有行为一致，仍按空白切分（不劫持、不报错）。
        assert sq._tokenize('颜色:"a b"') == [('term', '颜色:"a'), ('term', 'b"')]


class TestQuotedFieldValues:
    """字段取值可用双引号包裹，以包含空格与逗号（Task 9 的 quoteIfNeeded() 会产出这种形式）。

    修前的失败形态：'会话:"my chat"' 在空格外被切开 → chats == ['"my'] 且多出一个
    关键词 'chat"'，用户实际搜的是「会话名 = "my」+「关键词 = chat"」→ 0 条结果，
    UI 显示"没有找到相关消息"（静默假阴性）。
    """

    def test_value_with_space_is_one_value(self):
        got = sq.parse_query('会话:"my chat"')
        assert got['chats'] == ['my chat']
        assert got['keywords'] == []
        assert got['phrases'] == []
        assert got['errors'] == []

    def test_value_with_space_after_other_condition(self):
        got = sq.parse_query('维修 会话:"my chat"')
        assert got['keywords'] == ['维修']
        assert got['chats'] == ['my chat']

    def test_two_quoted_field_tokens_are_or_ed(self):
        got = sq.parse_query('会话:"a b" 会话:"c d"')
        assert got['chats'] == ['a b', 'c d']

    def test_comma_inside_quotes_is_not_a_separator(self):
        got = sq.parse_query('会话:"a,b"')
        assert got['chats'] == ['a,b']

    def test_comma_outside_quotes_still_separates(self):
        assert sq.parse_query('类型:图片,视频')['types'] == [3, 43]

    def test_each_quoted_segment_is_a_value(self):
        assert sq.parse_query('类型:"图片","视频"')['types'] == [3, 43]

    def test_each_quoted_segment_may_contain_spaces(self):
        got = sq.parse_query('会话:"my chat","your chat"')
        assert got['chats'] == ['my chat', 'your chat']

    def test_label_with_space(self):
        assert sq.parse_query('标签:"a b"')['labels'] == ['a b']

    def test_sender_with_space(self):
        assert sq.parse_query('发送者:"张 三"')['senders'] == ['张 三']

    def test_fullwidth_colon_with_quoted_value(self):
        assert sq.parse_query('会话："my chat"')['chats'] == ['my chat']

    def test_quoted_date_range_parses(self):
        got = sq.parse_query('日期:"2026-01..2026-03"')
        assert got['date_from'] is not None and got['date_to'] is not None

    def test_unclosed_quote_is_an_error_without_garbage_value(self):
        got = sq.parse_query('会话:"abc')
        assert got['errors']
        assert got['chats'] == []

    def test_unclosed_quote_containing_space_is_an_error(self):
        got = sq.parse_query('会话:"my chat')
        assert got['errors']
        assert got['chats'] == []

    def test_empty_quoted_value_is_an_error_not_a_quoted_empty_string(self):
        got = sq.parse_query('会话:""')
        assert got['errors']
        assert got['chats'] == []

    def test_plain_phrase_still_works(self):
        got = sq.parse_query('"报修 电话"')
        assert got['phrases'] == ['报修 电话']
        assert got['keywords'] == []


class TestTypeFilter:
    def test_chinese_name(self):
        assert sq.parse_query('类型:图片')['types'] == [3]

    def test_multiple_names(self):
        assert sq.parse_query('类型:图片,语音')['types'] == [3, 34]

    def test_numeric_base_type(self):
        assert sq.parse_query('类型:3')['types'] == [3]

    def test_unknown_type_is_error(self):
        got = sq.parse_query('类型:彩虹')
        assert got['types'] == []
        assert got['errors']

    def test_app_alias_maps_to_49(self):
        assert sq.parse_query('类型:链接')['types'] == [49]


class TestDateFilter:
    def test_single_day_range_is_that_day(self):
        lo, hi = sq.parse_date_value('2026-03-05')
        from datetime import datetime
        assert lo == int(datetime(2026, 3, 5, 0, 0, 0, tzinfo=TZ).timestamp())
        assert hi == int(datetime(2026, 3, 5, 23, 59, 59, tzinfo=TZ).timestamp())

    def test_month_covers_whole_month(self):
        lo, hi = sq.parse_date_value('2026-02')
        from datetime import datetime
        assert lo == int(datetime(2026, 2, 1, tzinfo=TZ).timestamp())
        assert hi == int(datetime(2026, 2, 28, 23, 59, 59, tzinfo=TZ).timestamp())

    def test_year_covers_whole_year(self):
        lo, hi = sq.parse_date_value('2026')
        from datetime import datetime
        assert lo == int(datetime(2026, 1, 1, tzinfo=TZ).timestamp())
        assert hi == int(datetime(2026, 12, 31, 23, 59, 59, tzinfo=TZ).timestamp())

    def test_explicit_range(self):
        lo, hi = sq.parse_date_value('2026-01-01..2026-03-31')
        from datetime import datetime
        assert lo == int(datetime(2026, 1, 1, tzinfo=TZ).timestamp())
        assert hi == int(datetime(2026, 3, 31, 23, 59, 59, tzinfo=TZ).timestamp())

    def test_range_open_start(self):
        lo, hi = sq.parse_date_value('..2026-03-31')
        assert lo is None and hi is not None

    def test_range_open_end(self):
        lo, hi = sq.parse_date_value('2026-01-01..')
        assert lo is not None and hi is None

    def test_unparseable_date_is_error(self):
        got = sq.parse_query('日期:昨天')
        assert got['errors']
        assert got['date_from'] is None

    def test_field_wiring(self):
        got = sq.parse_query('日期:2026-03')
        assert got['date_from'] is not None and got['date_to'] is not None


class TestRegex:
    def test_invalid_regex_is_an_error_and_is_not_kept(self):
        got = sq.parse_query('/(/')
        assert got['errors']
        assert got['regexes'] == []

    def test_valid_regex_is_kept(self):
        got = sq.parse_query('/报修|维修/')
        assert got['errors'] == []
        assert got['regexes'] == ['报修|维修']

    def test_huge_repeat_count_is_an_error_not_a_crash(self):
        # CPython 对超大重复次数抛 OverflowError，它必须进 errors 而不是逃逸。
        for raw in ('/a{99999999999}/', '/a{4294967296}/'):
            got = sq.parse_query(raw)
            assert got['regexes'] == []
            assert len(got['errors']) >= 1

    def test_deeply_nested_regex_is_an_error_not_a_crash(self):
        got = sq.parse_query('/' + '(' * 500 + ')' * 500 + '/')
        assert got['regexes'] == []
        assert len(got['errors']) >= 1


class TestEmpty:
    def test_blank_query(self):
        assert sq.is_empty(sq.parse_query(''))
        assert sq.is_empty(sq.parse_query('   '))

    def test_errors_only_is_empty(self):
        got = sq.parse_query('类型:彩虹')
        assert sq.is_empty(got)

    def test_any_condition_is_not_empty(self):
        assert not sq.is_empty(sq.parse_query('维修'))
        assert not sq.is_empty(sq.parse_query('日期:2026'))
        assert not sq.is_empty(sq.parse_query('/x/'))


class TestCharTokenization:
    def test_lowercase_and_strip_whitespace(self):
        assert sq.to_char_token_text('  Hello World  ') == 'h e l l o w o r l d'

    def test_chinese_each_char_is_a_token(self):
        assert sq.to_char_token_text('维修') == '维 修'

    def test_empty(self):
        assert sq.to_char_token_text('') == ''
        assert sq.to_char_token_text(None) == ''

    def test_matches_wrapped_adapter_implementation(self):
        """必须与年度报告用的 _to_char_token_text 完全一致，否则将来无法共用索引。"""
        from web.reports.wrapped.adapter import _to_char_token_text as theirs
        for s in ('维修服务器', 'Hello World', '  a  b ', '混合 Mixed 文本 123',
                  '表情😀与换行\n', '', None, '　全角空格　'):
            assert sq.to_char_token_text(s) == theirs(s), repr(s)


class TestCharPhrase:
    def test_two_char_word(self):
        assert sq.char_phrase('维修') == '"维 修"'

    def test_whitespace_removed_so_phrase_is_contiguous(self):
        assert sq.char_phrase('服务器 宕机') == '"服 务 器 宕 机"'

    def test_lowercased(self):
        assert sq.char_phrase('Disk') == '"d i s k"'

    def test_empty_returns_empty(self):
        assert sq.char_phrase('   ') == ''

    def test_inner_quote_is_doubled(self):
        """FTS5 用 "" 表示短语内的一个引号；不双写就是未闭合短语（语法错误）。"""
        assert sq.char_phrase('a"b') == '"a "" b"'
        assert sq.char_phrase('"') == '"' * 4           # 开引号 + `"` 的双写 + 闭引号
        assert sq.char_phrase('""') == '""" """'


class TestToFtsExpr:
    def test_keywords_are_and_ed(self):
        assert sq.to_fts_expr(sq.parse_query('维修 电话')) == '"维 修" AND "电 话"'

    def test_or_group(self):
        assert sq.to_fts_expr(sq.parse_query('维修 OR 报修')) == '("维 修" OR "报 修")'

    def test_exclude_becomes_infix_not(self):
        """FTS5 的 NOT 只能是**中缀**二元运算符：'AND NOT "x"' 是语法错误。"""
        assert sq.to_fts_expr(sq.parse_query('维修 -退货')) == '"维 修" NOT "退 货"'

    def test_multiple_excludes_are_appended_infix(self):
        assert (sq.to_fts_expr(sq.parse_query('维修 -退货 -报修'))
                == '"维 修" NOT "退 货" NOT "报 修"')

    def test_or_group_precedes_infix_not(self):
        assert (sq.to_fts_expr(sq.parse_query('维修 OR 报修 -报修'))
                == '("维 修" OR "报 修") NOT "报 修"')

    def test_excludes_without_any_positive_part_return_none(self):
        """'NOT "x"' 单独出现是语法错误，没有合法形态 → 返回 None 让调用方全扫。"""
        for query in ('-退货', '-退货 -报修', '--', '会话:x -y'):
            assert sq.to_fts_expr(sq.parse_query(query)) is None, query

    def test_phrase_joins_keywords(self):
        got = sq.to_fts_expr(sq.parse_query('"服务器 宕机" 维修'))
        assert got == '"服 务 器 宕 机" AND "维 修"'

    def test_no_text_condition_returns_none(self):
        assert sq.to_fts_expr(sq.parse_query('类型:图片')) is None
        assert sq.to_fts_expr(sq.parse_query('日期:2026')) is None

    def test_regex_only_returns_none(self):
        """正则需要先抽字面量再走 FTS，不能直接下推。"""
        assert sq.to_fts_expr(sq.parse_query('/报修/')) is None


# --------------------------------------------------------------------------
# FTS5 可执行性：字符串断言抓不到非法语法，必须把表达式真的喂给 FTS5
# --------------------------------------------------------------------------

# 对抗性 token：引号（短语未闭合）、FTS5 运算符字符、被词法器吃掉一半的语法
_ADVERSARIAL_TOKENS = [
    'a"b', 'say "hi"', "'quote'", 'a*b', 'a(b)', 'a:b', 'a^b', 'a-b', '50%',
    '"', '""', '*', '-', '--', '维修*', '维修 OR', '维修 -',
]

# 覆盖表：普通 / 多关键词 / 短语 / OR 组 / 单个与多个排除词 / 仅排除词 /
# 关键词+OR+排除词组合 / 字段过滤器混用 / 对抗性输入
_FTS_QUERIES = [
    '维修', '维修 电话', '"服务器 宕机"',
    '维修 OR 报修',
    '维修 -退货', '-退货', '维修 -退货 -报修',
    '维修 OR 报修 -报修',
    '维修 -退货 电话', '维修 "报 修" -退货', '会话:x 维修 -y z',
    '类型:图片', '类型:图片 维修 -退货', '日期:2026 维修', '会话:x -y',
    'a"b', 'say "hi"', "'quote'", 'a*b', 'a(b)', 'a:b', 'a^b', 'a-b', '50%',
    '"', '""', '*', '-', '--', '维修*', '维修 OR', '- 维修', '维修 -"报 修"',
]


_FTS_ROWS = ('维 修 电 话', '报 修 电 话', '退 货 记 录', '维 修 报 修 记 录',
             'a b', 'a " b', 's a y')


@pytest.fixture(scope='module')
def fts5_conn():
    """内存 FTS5 表（正文按 char-token 存，与真实索引一致）。

    性质测试的意义：字符串断言只能证明「不是我以为的串」，**真跑一次 MATCH** 才能证明
    「FTS5 语法合法」。本轮的两个 Critical（NOT 写成前缀、短语里的引号未转义）
    都只被真执行抓到 —— 测试只比对字符串，所以连过 5 轮评审。
    """
    conn = sqlite3.connect(':memory:')
    conn.execute("CREATE VIRTUAL TABLE t USING fts5(text, tokenize='unicode61')")
    conn.executemany('INSERT INTO t(text) VALUES (?)', [(x,) for x in _FTS_ROWS])
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def _fts_count(conn, expr):
    """执行一次 MATCH。表达式非法时 sqlite3 抛 OperationalError（这就是断言）。"""
    return conn.execute('SELECT COUNT(*) FROM t WHERE t MATCH ?', (expr,)).fetchone()[0]


class TestToFtsExprIsExecutable:
    """性质测试：`to_fts_expr` 产出的**每一个非 None 表达式**都必须能被 FTS5 执行。"""

    def test_every_expression_executes_against_fts5(self, fts5_conn):
        executed = []
        for query in _FTS_QUERIES:
            expr = sq.to_fts_expr(sq.parse_query(query))
            if expr is None:
                continue
            _fts_count(fts5_conn, expr)         # 非法表达式在此抛 OperationalError
            executed.append((query, expr))
        # 非空转守卫：非 None 的表达式必须足够多，否则本用例可能什么都没验就变绿
        assert len(executed) >= 20, executed

    @pytest.mark.parametrize('query,pred', [
        ('维修 -退货', lambda r: '维 修' in r and '退 货' not in r),
        ('维修 -退货 -报修',
         lambda r: '维 修' in r and '退 货' not in r and '报 修' not in r),
        ('维修 OR 报修 -报修',
         lambda r: ('维 修' in r or '报 修' in r) and '报 修' not in r),
        ('维修 OR 报修 -报修 -退货',
         lambda r: (('维 修' in r or '报 修' in r)
                    and '报 修' not in r and '退 货' not in r)),
    ])
    def test_infix_not_has_the_intended_semantics(self, fts5_conn, query, pred):
        """以**独立算出的** Python 期望集为对照，验证中缀 NOT 的语义 = 「并且不包含」。

        只管「能执行」是不够的：'A NOT x AND B' 会先算 (A NOT x) 再 AND B，
        语义写错同样会静默给出错误结果集，所以这里逐行比对结果。
        """
        expr = sq.to_fts_expr(sq.parse_query(query))
        assert expr is not None, query
        got = sorted(r[0] for r in
                     fts5_conn.execute('SELECT text FROM t WHERE t MATCH ?', (expr,)))
        expected = sorted(r for r in _FTS_ROWS if pred(r))
        assert got == expected, (query, expr, got, expected)
        assert expected, query            # 非空转守卫：期望集不能是空的

    @pytest.mark.parametrize('token', _ADVERSARIAL_TOKENS)
    def test_adversarial_token_is_executable_in_every_position(self, fts5_conn, token):
        """同一个 token 放在正向位、短语位、OR 组、排除位都必须产出合法表达式。"""
        base = sq.parse_query('维修')

        def with_(**kw):
            parsed = dict(base)
            parsed.update(kw)
            return parsed

        exprs = [
            sq.to_fts_expr(with_(keywords=[token], phrases=[], or_groups=[])),
            sq.to_fts_expr(with_(phrases=[token], keywords=['维修'], or_groups=[])),
            sq.to_fts_expr(with_(keywords=['维修'], or_groups=[[token, '报修']])),
            sq.to_fts_expr(with_(keywords=['维修'], exclude=[token])),
            sq.to_fts_expr(with_(keywords=['维修'], exclude=[token, '报修'])),
            sq.to_fts_expr(with_(keywords=[token], exclude=[token])),
        ]
        for expr in exprs:
            if expr is None:
                continue
            _fts_count(fts5_conn, expr)

    @pytest.mark.parametrize('token', _ADVERSARIAL_TOKENS)
    def test_char_phrase_is_always_a_legal_phrase(self, fts5_conn, token):
        phrase = sq.char_phrase(token)
        if phrase == '':
            return
        assert phrase.startswith('"') and phrase.endswith('"'), phrase
        assert len(phrase) >= 3, phrase          # 两个定界引号 + 至少一个 token 字符
        _fts_count(fts5_conn, phrase)


class TestRegexLiterals:
    def test_alternation(self):
        assert sq.extract_regex_literals('报修|维修') == ['报修', '维修']

    def test_char_class_gives_single_char_literal(self):
        assert sq.extract_regex_literals('售[后前]') == ['售']

    def test_no_literal_is_empty_string(self):
        assert sq.extract_regex_literals('.+') == ['']

    def test_escaped_metachar_is_literal(self):
        assert sq.extract_regex_literals(r'1\.5匹') == ['1.5匹']

    def test_charclass_shorthand_is_not_literal(self):
        assert sq.extract_regex_literals(r'\d{11}') == ['']

    def test_quantifier_ends_run(self):
        assert sq.extract_regex_literals('ab+cdef') == ['cdef']

    def test_degraded_detection(self):
        assert sq.regex_is_degraded(['报修', '维修']) is False
        assert sq.regex_is_degraded(['']) is True
        assert sq.regex_is_degraded(['报修', '']) is True
        assert sq.regex_is_degraded([]) is True


class TestTypeFilterNonDecimalDigits:
    """`int()` 只吃 Unicode 十进制数字，`str.isdigit()` 不是它的判据。

    '²'(U+00B2)、'①'(U+2460) 等 isdigit() 为真但 int() 抛 ValueError，
    跳过这道闸就会让 parse_query 抛异常，违反「任何输入都不抛异常」的约定。
    """

    def test_superscript_digit_is_an_error_not_a_crash(self):
        got = sq.parse_query('类型:²')
        assert got['types'] == []
        assert len(got['errors']) >= 1

    def test_circled_digit_is_an_error_not_a_crash(self):
        got = sq.parse_query('类型:①')
        assert got['types'] == []
        assert len(got['errors']) >= 1

    def test_ascii_digit_still_converts(self):
        assert sq.parse_query('类型:3')['types'] == [3]

    def test_fullwidth_digit_still_converts(self):
        # '３'(U+FF13) 是 Unicode 十进制数字，int() 能转换，必须继续走数字分支。
        assert sq.parse_query('类型:３')['types'] == [3]


class TestRegexPrefilterSuperset:
    """契约：抽出的字面量是**超集预筛** —— 任一真匹配必然包含其中至少一个。

    否则把它当硬预筛条件会静默漏结果（用户既看不到结果也没有降级提示）。
    """

    def test_alternation_inside_group_is_split(self):
        lits = sq.extract_regex_literals('(报修|维修)电话')
        assert '报修' in lits
        assert '维修' in lits

    def test_star_weakened_literal_is_dropped(self):
        assert sq.extract_regex_literals('服务器*宕机') == ['宕机']

    def test_optional_literal_is_dropped(self):
        lits = sq.extract_regex_literals('x?(y|z)w')
        assert 'x' not in lits
        assert 'y' in lits and 'z' in lits      # 双边断言：[] / [''] 不能蒙混过关

    def test_plain_groups_take_the_fast_path(self):
        """只放行普通组 `(...)` 与非捕获组 `(?:...)`，它们必须保持非退化。

        组内（任意深度）的 `|` 都要切分支、组被零次量词修饰时组内字面量要削弱；
        这是本任务保留下来的核心能力，退化收紧后不能被误伤。
        """
        lits = sq.extract_regex_literals('(报修|维修)电话')
        assert lits == ['报修', '维修']
        assert sq.regex_is_degraded(lits) is False
        lits = sq.extract_regex_literals('(?:ab)*c')
        assert lits == ['c']
        assert sq.regex_is_degraded(lits) is False


class TestRegexPrefilterExtensionGroups:
    """任何 `(?…` 扩展组都让**整条模式**退化 → 调用方全量扫描。

    三轮修复各建模一种扩展组，每轮都在同一处又开出一个「非退化但错误」的静默漏结果口子：
    (?i) 作用域被上层标志重置、(?x) 的忽略空白与注释、(?x:…) 跳过扫描的注释边界、
    (?#…) 注释里的括号。手写扩展组词法本身就是缺陷来源，所以现在只放行普通组 `(...)`
    与非捕获组 `(?:...)`：见到 `(?` 后面不是 `:`（含误报）就整条退化。
    取舍方向固定：宁可退化去全量扫描（慢但正确），绝不漏结果。
    """

    @pytest.mark.parametrize('pattern', [
        '(?#x)ab',                  # 注释：CPython 见到第一个 ')' 就结束注释
        '(?#a)b(?#c)d',
        '(?i)ab',                   # 全局内联标志
        '(?m)ab',
        '(?m)(?s)ab',
        '(?x) 报 修',
        '(?i:ab)c',                 # 作用域内联标志
        '(?x:ab)c',
        '(?x:报 修)电话',
        '(?i)a(?-i:b)',
        '(?-i)ab',
        '(?=ab)c',                  # 前瞻
        '(?!ab)c',                  # 负前瞻
        '(?<=a)bc',                 # 后顾
        '(?<!a)bc',                 # 负后顾
        '(?>ab)c',                  # 原子组
        '(?P<n>ab)c',               # 命名组
        '(?P=n)c',                  # 反向引用（本身编译不过，也不许抛异常）
        '(?L)ab',                   # 非法标志
        'ab(?#(x)|c',               # 注释里的括号：模式其实是 'ab|c'
        'a(?#(a)|',                 # 同上：模式其实是 'a|'
        '(?x:a#)b(\n c)',           # VERBOSE 组 + '#)' 注释
        '(?x:报修#) 已修复 (\n 电话)',
        '(?x:a#)\nb)c',
    ])
    def test_extension_group_degrades_the_whole_pattern(self, pattern):
        lits = sq.extract_regex_literals(pattern)
        assert lits == [''], pattern
        assert sq.regex_is_degraded(lits) is True, pattern

    @pytest.mark.parametrize('pattern,texts', [
        ('ab(?#(x)|c', ['ab', 'c']),          # 'ab|c'：修前抽出 ['ab'] → 漏 'c'
        ('a(?#(a)|', ['a', '']),              # 'a|'：修前抽出 ['a'] → 漏空分支
    ])
    def test_comment_with_paren_would_lose_a_real_match(self, pattern, texts):
        """CPython 的 (?#…) 到**第一个** ')' 就结束；数括号的扫描会跑进注释体。

        修前实测：这两条都是 degraded=False 且抽出错误的字面量，而下面的真匹配
        （'c' 与 ''）不含它 —— 静默丢结果。
        """
        for text in texts:
            assert re.search(pattern, text) is not None      # 反例都是活模式
        lits = sq.extract_regex_literals(pattern)
        assert lits == ['']
        assert sq.regex_is_degraded(lits) is True

    @pytest.mark.parametrize('pattern,text', [
        ('(?i)ab', 'AB'),               # 大小写不敏感：'ab' 会匹配 'AB'
        ('(?i)(?m)ab', 'AB'),           # (?m) 不得重置外层 (?i)
        ('(?i)(?s)ab', 'aB'),
        ('(?i:(?s:ab))', 'Ab'),
        ('(?i)a(?-i:b)', 'Ab'),         # 作用域内 'b' 已被收窄成大小写敏感
        ('(?m)(?s)ab', 'ab'),
        ('(?x) 报 修', '报修'),          # VERBOSE 忽略空白
        ('(?x:报 修)电话', '报修电话'),   # VERBOSE 组：修前抽出 ' 报 修'
        ('(?x:a#)b(\n c)', 'ac'),       # '#)' 注释吞掉 ')'：修前抽出 '\n c'
        ('(?#x)ab', 'ab'),
    ])
    def test_live_flagged_pattern_is_fully_untrusted(self, pattern, text):
        """迁移自属性测试/旧标志作用域测试的用例：这些模式都必须退化为整条不信。

        先断言模式确实匹配（活模式），再断言一个字面量都不给 —— 退化 → 全量扫描 → 不可能漏。
        """
        assert re.search(pattern, text) is not None
        lits = sq.extract_regex_literals(pattern)
        assert lits == [''], pattern
        assert sq.regex_is_degraded(lits) is True, pattern


class TestRegexPrefilterPreservedAndRobust:
    """退化收紧后必须保住的能力，以及「任何输入都不抛异常、畸形正则只能退化」。"""

    def test_group_alternation_with_plus_keeps_both_branches(self):
        lits = sq.extract_regex_literals('(ab|cd)+e')     # '(' 组 + '+' 不削弱
        assert lits == ['ab', 'cd']
        assert sq.regex_is_degraded(lits) is False

    @pytest.mark.parametrize('pattern', ['a(', '(a', 'a)', ')a(', '(?:a', '('])
    def test_unbalanced_group_degrades_instead_of_raising(self, pattern):
        lits = sq.extract_regex_literals(pattern)
        assert lits == [''], pattern
        assert sq.regex_is_degraded(lits) is True, pattern

    @pytest.mark.parametrize('pattern', [
        None, '', '(', 'a(', '(a', 'a)', ')a(', 'a{2', 'a{2,', '(?', '(?P<',
        '(?#', '\\', '[', '(?:', 'a|', '|a', '售[后前', '[(?]', '\\(\\?',
    ])
    def test_never_raises_for_junk_input(self, pattern):
        lits = sq.extract_regex_literals(pattern)         # 不抛异常
        assert isinstance(lits, list)
        assert isinstance(sq.regex_is_degraded(lits), bool)
        sq.parse_query('/%s/' % (pattern or ''))          # parse_query 也不抛
        sq.parse_query(pattern or '')

    @pytest.mark.parametrize('pattern,texts', [
        ('a{2,', ['a{2,', 'xa{2,y']),     # '{2,' 未配对时是普通字符，跳掉 '{' 只丢前缀
        ('x{a}y', ['x{a}y']),
        ('ab{1', ['ab{1']),
    ])
    def test_literal_brace_patterns_keep_a_necessary_literal(self, pattern, texts):
        """丢的只能是 run 的前缀，绝不造假字面量 —— 所以这些模式仍可安全预筛。"""
        lits = sq.extract_regex_literals(pattern)
        assert sq.regex_is_degraded(lits) is False, pattern
        usable = [x for x in lits if x]
        assert usable, pattern
        for text in texts:
            assert re.search(pattern, text) is not None
            assert any(l in text for l in usable), (pattern, text, usable)


@pytest.mark.parametrize('pattern,texts', [
    ('(报修|维修)电话', ['维修电话', '报修电话']),
    ('服务器*宕机', ['服务宕机', '服务器宕机']),
    ('(a|b)c', ['ac', 'bc']),
    ('x?(y|z)w', ['yw', 'zw', 'xyw', 'xzw']),
    ('报修|维修', ['这里有报修', '需要维修']),
    ('售[后前]', ['售后', '售前']),
    ('(?:报修|维修)电话', ['报修电话', '维修电话']),
    ('(?:ab)*c', ['abc', 'c']),
])
def test_prefilter_never_misses_a_real_match(pattern, texts):
    """不变式：真匹配必然包含某抽出的字面量（否则硬预筛会静默漏结果）。"""
    raw = sq.extract_regex_literals(pattern)
    lits = [x for x in raw if x]
    # 这些模式都是已知「非退化」的：断言在此，免得将来全退化时本用例空转即绿。
    assert not sq.regex_is_degraded(raw), pattern
    assert lits, pattern
    for text in texts:
        assert re.search(pattern, text) is not None      # 前提：确实匹配
        assert any(l in text for l in lits), (pattern, text, lits)
