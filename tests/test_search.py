"""查询执行（索引路径）：组合筛选、摘要/高亮、正则、原文保真、空文本索引闸门。

全部使用合成夹具 —— 绝不读取真实微信数据、`backup/` 或任何真实姓名/wxid/正文。

本文件的手算期望值一律由夹具逐行推出（见 `_make_rich_dir` 的注释），
不使用「非空即通过」这类弱断言：T5-A1（无关键词时生成非法 SQL 并被吞成 0 结果）、
T5-A2/A8（正文必须取自 msg_text.raw_text）、T5-A8（文本索引为空时不得静默返回空）
都需要**精确行集合**才能杀死。
"""
import contextlib
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

from engine.services import search as s
from engine.services import search_index as si
from engine.services import search_query as sq
from tests.test_search_index import _make_fts_db, _make_shard

# --------------------------------------------------------------------------
# 夹具常量（手算期望值的依据）
# --------------------------------------------------------------------------
T0 = 1600000000          # 2020-09-13 20:26:40 +08:00
DAY = 86400
DAY_START = 1599926400   # 2020-09-13 00:00:00 +08:00（TZ=UTC+8）
DAY_END = 1600012799     # 2020-09-13 23:59:59 +08:00

RAW_ORDER = 'Order A1234\n已发货'
RAW_REPAIR = '报修 电话 已记录'
RAW_FIX = '维修服务器离线'
RAW_DISK = 'group hello disk'
RAW_VOICE = '语音转文字失败'


def _make_contact_db(path, contacts, labels):
    """合成通讯录：contact(username, remark, nick_name, alias, extra_buffer) + contact_label。

    contacts: [(username, remark, [label_id, ...])]
    labels:   {label_id: label_name}

    标签 id 走 `extra_buffer` 的 **field 30**（wire type 2）：
    key = (30 << 3) | 2 = 242 = 0xF2 0x01 —— 与 `contact_extra.parse_extra_buffer` 一致。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute('CREATE TABLE contact (username TEXT, remark TEXT, nick_name TEXT,'
                    ' alias TEXT, extra_buffer BLOB)')
        con.execute('CREATE TABLE contact_label (label_id_ INTEGER, label_name_ TEXT)')
        for username, remark, label_ids in contacts:
            blob = None
            if label_ids:
                text = ','.join(str(i) for i in label_ids).encode('utf-8')
                blob = b'\xf2\x01' + bytes([len(text)]) + text
            con.execute('INSERT INTO contact VALUES (?,?,?,?,?)',
                        (username, remark, '', '', blob))
        con.executemany('INSERT INTO contact_label VALUES (?,?)', list(labels.items()))
        con.commit()
    finally:
        con.close()


def _make_rich_dir(tmp_path):
    r"""合成目录：原文含大小写/空白/换行、含无正文消息、含通讯录标签。

    消息侧（message_0.db，Name2Id 按下面 chats 的顺序给 rowid）：
      rowid 1 = wxid_beta, rowid 2 = wxid_alpha, rowid 3 = 111@chatroom
      wxid_alpha:  (1, 文本, T0)        正文 RAW_ORDER  —— 大小写 + 换行
                   (2, 图片, T0+100)    无正文
                   (3, 文本, T0+200)    正文 RAW_REPAIR —— 中间有空格（钉 regex \s）
                   (4, 文本, T0+300)    正文 RAW_FIX
                   (5, 语音, T0+400)    无正文
      111@chatroom:(1, 文本, T0+1000)   正文 RAW_DISK
                   (2, 图片, T0+1100)   无正文
      wxid_beta:   (11, 文本, T0+DAY)   正文 RAW_VOICE（第二天，用于日期边界）

    发送者（按 sender_id → Name2Id rowid 解析）：
      wxid_alpha   的消息：1→wxid_alpha, 2→wxid_beta, 3→wxid_alpha, 4→wxid_beta, 5→wxid_alpha
      111@chatroom 的消息：1→wxid_beta, 2→wxid_beta
      wxid_beta    的消息：11→wxid_beta

    因此 msg_meta 共 8 行、message_fts/msg_text 各 5 行（3 条无正文消息）。
    标签：wxid_alpha → only_work、111@chatroom → non_work。
    """
    d = tmp_path / 'rich'
    (d / 'message').mkdir(parents=True)
    _make_shard(str(d / 'message' / 'message_0.db'), [
        ('wxid_beta', [
            (11, 1, T0 + DAY, 1),
        ]),
        ('wxid_alpha', [
            (1, 1, T0, 2),
            (2, 3, T0 + 100, 1),
            (3, 1, T0 + 200, 2),
            (4, 1, T0 + 300, 1),
            (5, 34, T0 + 400, 2),
        ]),
        ('111@chatroom', [
            (1, 1, T0 + 1000, 1),
            (2, 3, T0 + 1100, 1),
        ]),
    ])
    _make_fts_db(str(d / 'message' / 'message_fts.db'), [
        (RAW_ORDER, 1, T0 * 1000, 1, 1, 1, T0),
        (RAW_REPAIR, 3, (T0 + 200) * 1000, 1, 1, 2, T0 + 200),
        (RAW_FIX, 4, (T0 + 300) * 1000, 1, 1, 1, T0 + 300),
        (RAW_DISK, 1, (T0 + 1000) * 1000, 1, 2, 1, T0 + 1000),
        (RAW_VOICE, 11, (T0 + DAY) * 1000, 1, 3, 1, T0 + DAY),
    ])
    _make_contact_db(str(d / 'contact' / 'contact.db'),
                     [('wxid_alpha', '阿尔法', [7]), ('111@chatroom', '', [8])],
                     {7: 'only_work', 8: 'non_work'})
    return str(d)


@pytest.fixture
def many(tmp_path):
    """150 条消息（120 文本 + 30 图片，均属 wxid_alpha）+ 已建索引。

    用来验证分页边界与「SQL 侧分页」：120 条命中 vs per_page=50 → 3 页（50/50/20）。
    正文 'msg 0001' 的 char-token 含 'm s g'，因此关键词 'msg' 命中全部 120 条文本。
    """
    d = tmp_path / 'many'
    (d / 'message').mkdir(parents=True)
    texts = [(i, 1, T0 + i, 1) for i in range(1, 121)]
    images = [(200 + i, 3, T0 + i, 1) for i in range(1, 31)]
    _make_shard(str(d / 'message' / 'message_0.db'), [('wxid_alpha', texts + images)])
    _make_fts_db(str(d / 'message' / 'message_fts.db'),
                 [('msg %04d' % i, i, (T0 + i) * 1000, 1, 1, 1, T0 + i)
                  for i in range(1, 121)])
    si.build_index(str(d))
    return str(d)


class _RecordingConn:
    """记录 SQL 与 `fetchall()` 行数 —— 钉住「SQL 侧分页」而不是「全量物化后切片」。

    T13：`_Cur` 必须是**忠实的**游标替身 —— 索引路径的流式扫描用的是
    `for row in conn.execute(sql, params)`（`sqlite3.Cursor` 本来就是可迭代的），
    所以这里也要支持迭代，否则"代理对象"会让改造后的代码在测试里假失败。
    """

    def __init__(self, conn):
        self._conn = conn
        self.calls = []          # [(sql, 行数)]

    def execute(self, sql, params=()):
        cur = self._conn.execute(sql, params)
        outer = self

        class _Cur:
            def fetchone(self):
                return cur.fetchone()

            def fetchall(self):
                rows = cur.fetchall()
                outer.calls.append((sql, len(rows)))
                return rows

            def __iter__(self):
                while True:
                    row = cur.fetchone()
                    if row is None:
                        return
                    yield row

        return _Cur()

    def close(self):
        self._conn.close()


@pytest.fixture
def built(tmp_path):
    """brief 的合成目录 + 已构建索引（关键词路径用，无通讯录）。"""
    d = tmp_path / 'dec'
    (d / 'message').mkdir(parents=True)
    _make_shard(str(d / 'message' / 'message_0.db'), [
        ('wxid_alpha', [(1, 1, 1600000000, 1), (2, 3, 1600000100, 2),
                        (3, 1, 1600000200, 1)]),
        ('111@chatroom', [(1, 1, 1600000300, 2), (2, 34, 1600000400, 3)]),
    ])
    _make_fts_db(os.path.join(str(d), 'message', 'message_fts.db'), [
        ('维修服务器', 1, 1600000000000, 1, 1, 1, 1600000000),
        ('发票已开', 2, 1600000100000, 1, 1, 2, 1600000100),
        ('报修电话', 3, 1600000200000, 1, 1, 1, 1600000200),
        ('hello disk', 1, 1600000300000, 1, 2, 2, 1600000300),
        ('语音消息', 2, 1600000400000, 34, 2, 3, 1600000400),
    ])
    si.build_index(str(d))
    return str(d)


@pytest.fixture
def rich(tmp_path):
    """`_make_rich_dir` + 完整索引（含 msg_text 原文）。"""
    d = _make_rich_dir(tmp_path)
    si.build_index(d)
    return d


@pytest.fixture
def rich_meta_only(tmp_path):
    """T5-A8 的前提：`text=False` 构建 → fts_rows=0 / msg_text 空，而 ready 仍为 True。"""
    d = _make_rich_dir(tmp_path)
    si.build_index(d, text=False)
    return d


def _ids(res):
    """结果里的 (chat_id, local_id) 顺序列表。"""
    return [(r['chat_id'], r['local_id']) for r in res['results']]


def _id_set(res):
    return set(_ids(res))


def _snippet_of(res, chat_id, local_id):
    for r in res['results']:
        if (r['chat_id'], r['local_id']) == (chat_id, local_id):
            return r['snippet']
    raise AssertionError('%s/%s 不在结果里：%s' % (chat_id, local_id, _ids(res)))


# --------------------------------------------------------------------------
# 等价性测试的基础设施（Task 6 的控制方必做增补）
# --------------------------------------------------------------------------

@contextlib.contextmanager
def _index_hidden(decrypted_dir):
    """把索引库改名让开路径 → `index_status.ready=False` → 强制走降级路径。

    走的是**真实路由**（不是注入一个假的 index_status）：改名后 `index_status`
    自己就会报 `exists=False/ready=False`，因此降级路径的入口条件属实。
    """
    path = si.index_path(decrypted_dir)
    hidden = path + '.hidden'
    os.replace(path, hidden)
    try:
        assert not os.path.exists(path)
        yield
    finally:
        os.replace(hidden, path)


def _comparable(res):
    """两条路径应当**逐字段相同**的部分。

    排除 `sender_username`/`sender_display_name`：降级的 content 表只有分片内
    发送者序号，解析不出 wxid（brief 明示的已知差异）。
    snippet/match_spans 必须相同 —— 这正是「第 6 列是原文而不是 char-token」
    比「行集合相同」更强的判别。
    """
    return [(r['chat_id'], r['local_id'], r['local_type'], r['create_time'],
             r['snippet'], r['match_spans']) for r in res['results']]


def _both_paths(decrypted_dir, q, **kw):
    """同一查询跑两次（索引 / 降级），并断言二者**完全等价**。"""
    indexed = s.search_messages(decrypted_dir, q, **kw)
    assert indexed['used_fallback'] is False, '有索引时不该走降级'
    with _index_hidden(decrypted_dir):
        fallback = s.search_messages(decrypted_dir, q, **kw)
    assert fallback['used_fallback'] is True, '索引让开后必须走降级'
    assert _ids(indexed) == _ids(fallback), (
        '两路径行集合/顺序不一致\n query=%r\n indexed=%s\n fallback=%s'
        % (q, _ids(indexed), _ids(fallback)))
    assert indexed['total'] == fallback['total'], (
        '两路径 total 不一致 query=%r indexed=%s fallback=%s'
        % (q, indexed['total'], fallback['total']))
    assert _comparable(indexed) == _comparable(fallback), (
        '两路径逐行内容（含摘要/高亮）不一致 query=%r' % q)
    return indexed, fallback


# ==========================================================================
# 1. 关键词路径（brief Step 1 的用例；两处期望值已按实测数据修正，见报告）
# ==========================================================================

class TestIndexedKeywordSearch:
    def test_chinese_two_char_word(self, built):
        res = s.search_messages(built, '维修')
        assert res['total'] == 1
        assert res['results'][0]['chat_id'] == 'wxid_alpha'
        assert res['used_fallback'] is False

    def test_and_of_two_keywords(self, built):
        assert s.search_messages(built, '维修 服务器')['total'] == 1
        assert s.search_messages(built, '维修 发票')['total'] == 0

    def test_or_group(self, built):
        assert s.search_messages(built, '维修 OR 报修')['total'] == 2

    def test_exclude(self, built):
        assert s.search_messages(built, '维修 OR 报修 -报修')['total'] == 1

    def test_english_prefix_via_char_tokens(self, built):
        assert s.search_messages(built, 'disk')['total'] == 1

    def test_type_filter_joined_from_meta(self, built):
        res = s.search_messages(built, '维修 类型:图片')
        assert res['total'] == 0
        res2 = s.search_messages(built, '语音 类型:语音')
        assert res2['total'] == 1

    def test_date_filter(self, built):
        """brief 原文期望 `维修 日期:2020-09-13` == 0，但**实测夹具里就是 1 条**：
        '维修服务器' 的 create_time=1600000000 = 2020-09-13 20:26:40（UTC+8，UTC 下也是同一天）。
        期望值按真实数据语义修正，并加一条真正落在第二天的反例。
        """
        assert s.search_messages(built, '维修 日期:2020-09-13')['total'] == 1
        assert s.search_messages(built, '维修 日期:2020-09-14')['total'] == 0
        assert s.search_messages(built, '维修 日期:..2030-01-01')['total'] == 1

    def test_chat_filter_by_username(self, built):
        assert s.search_messages(built, '语音 会话:111@chatroom')['total'] == 1
        assert s.search_messages(built, '语音 会话:wxid_alpha')['total'] == 0

    def test_keyword_and_filter_combination_is_hand_computed(self, rich):
        """关键词 × 类型 × 会话：只有 111@chatroom 的 'group hello disk' 命中 'disk'。"""
        res = s.search_messages(rich, 'disk 类型:文本 会话:111@chatroom')
        assert _ids(res) == [('111@chatroom', 1)]
        assert _snippet_of(res, '111@chatroom', 1) == RAW_DISK

    def test_empty_query_raises(self, built):
        with pytest.raises(ValueError):
            s.search_messages(built, '   ')

    def test_parsed_echoed(self, built):
        res = s.search_messages(built, '维修 类型:图片')
        assert res['parsed']['keywords'] == ['维修']
        assert res['parsed']['types'] == [3]

    def test_index_status_passed_through(self, rich):
        res = s.search_messages(rich, '维修')
        assert res['index']['ready'] is True
        assert res['index']['fts_rows'] == 5
        assert res['index']['meta_rows'] == 8
        assert res['index']['schema_ok'] is True
        assert res['index']['built_at'] > 0


# ==========================================================================
# 2. 纯筛选路径（无关键词）—— T5-A1 的判别性测试
# ==========================================================================

class TestPureFilterPath:
    """无关键词时**不允许**碰 message_fts（brief 模板在此生成非法 SQL 并吞成 0 结果）。"""

    def test_type_date_sender_filter_hand_computed(self, rich):
        """类型:文本 + 日期:2020-09-13 + 发送者:wxid_beta → 恰好 2 条。

        逐行推导：
          类型=1 的行：alpha/1, alpha/3, alpha/4, group/1, beta/11
          日期 2020-09-13（[1599926400, 1600012799]）：剩 alpha/1, alpha/3, alpha/4, group/1
          发送者 wxid_beta：alpha/4（sender_id=1）、group/1（sender_id=1）→ 2 条
        """
        res = s.search_messages(rich, '类型:文本 日期:2020-09-13 发送者:wxid_beta')
        assert res['total'] == 2
        assert _id_set(res) == {('wxid_alpha', 4), ('111@chatroom', 1)}
        assert res['used_fallback'] is False

    def test_type_date_sender_filter_without_keyword_leaves_no_doubt(self, rich):
        """同一查询加一个**必然命中**的关键词，把「过滤器坏了」与「关键词坏了」分开。"""
        res = s.search_messages(rich, '维修 类型:文本 日期:2020-09-13 发送者:wxid_beta')
        assert _ids(res) == [('wxid_alpha', 4)]

    def test_label_filter_hand_computed(self, rich):
        """标签:only_work → 会话集合 {wxid_alpha}；类型 图片,语音 → alpha/2 与 alpha/5。"""
        res = s.search_messages(rich, '标签:only_work 类型:图片,语音')
        assert res['total'] == 2
        assert _id_set(res) == {('wxid_alpha', 2), ('wxid_alpha', 5)}

    def test_label_filter_other_label(self, rich):
        res = s.search_messages(rich, '标签:non_work 类型:图片')
        assert _ids(res) == [('111@chatroom', 2)]

    def test_unknown_label_returns_nothing_not_everything(self, rich):
        """标签名不存在 → 空集合，绝不能把「限制不了」当成「不限制」。"""
        assert s.search_messages(rich, '标签:nope 类型:文本')['total'] == 0

    def test_chat_scope_hand_computed(self, rich):
        # 默认 time_desc：alpha/4(T0+300) → alpha/3(T0+200) → alpha/1(T0)
        res = s.search_messages(rich, '会话:wxid_alpha 类型:文本')
        assert _ids(res) == [('wxid_alpha', 4), ('wxid_alpha', 3), ('wxid_alpha', 1)]
        res2 = s.search_messages(rich, '会话:111@chatroom 类型:文本')
        assert _ids(res2) == [('111@chatroom', 1)]

    def test_chat_scope_by_display_name(self, rich):
        """通讯录里的显示名要能解析成 wxid（'阿尔法' → wxid_alpha）。"""
        res = s.search_messages(rich, '会话:阿尔法 类型:文本')
        assert _ids(res) == [('wxid_alpha', 4), ('wxid_alpha', 3), ('wxid_alpha', 1)]

    def test_unresolvable_chat_filter_is_not_silently_dropped(self, rich):
        """T5-A1 的同族缺陷：解析不出名字时**绝不能把过滤器丢掉**（那会返回全部 8 条）。"""
        res = s.search_messages(rich, '会话:wxid_nobody')
        assert res['total'] == 0
        assert any('wxid_nobody' in w for w in res['warnings'])

    def test_non_text_messages_survive_type_filter(self, rich):
        """图片/语音没有正文，但必须仍在「类型筛选」结果里（spec 第 280 行）。"""
        res = s.search_messages(rich, '类型:图片,语音')
        assert res['total'] == 3
        assert _id_set(res) == {('wxid_alpha', 2), ('wxid_alpha', 5), ('111@chatroom', 2)}
        assert all(r['snippet'] == '' for r in res['results'])
        assert {r['type_label'] for r in res['results']} <= {'图片', '语音'}

    def test_date_only_filter(self, rich):
        """只有日期（连类型都没有）也要走通：2020-09-14 只有 beta/11 一条。"""
        res = s.search_messages(rich, '日期:2020-09-14')
        assert _ids(res) == [('wxid_beta', 11)]
        assert s.search_messages(rich, '日期:..2019-01-01')['total'] == 0

    def test_sender_me_resolves_to_own_wxid(self, rich):
        """发送者:我 → 本人 wxid（own_wxid 由调用方传入）。"""
        res = s.search_messages(rich, '类型:文本 发送者:我', own_wxid='wxid_alpha')
        assert _ids(res) == [('wxid_alpha', 3), ('wxid_alpha', 1)]


# ==========================================================================
# 3. 摘要与原文保真（T5-A2 / A8）
# ==========================================================================

class TestRawTextAndSnippet:
    def test_pure_filter_snippets_are_raw_text(self, rich):
        """纯筛选的摘要必须来自 msg_text.raw_text，且逐字等于原文（含换行）。"""
        res = s.search_messages(rich, '类型:文本 会话:wxid_alpha')
        assert _snippet_of(res, 'wxid_alpha', 1) == RAW_ORDER
        assert _snippet_of(res, 'wxid_alpha', 3) == RAW_REPAIR
        assert _snippet_of(res, 'wxid_alpha', 4) == RAW_FIX
        assert all(r['snippet'] for r in res['results'])

    def test_pure_filter_snippet_keeps_case_and_whitespace(self, rich):
        """A8 判别：不得是小写化/去空白的 char-token 形态。"""
        res = s.search_messages(rich, '类型:文本 会话:wxid_alpha')
        snip = _snippet_of(res, 'wxid_alpha', 1)
        assert 'A1234' in snip
        assert '\n' in snip
        assert 'a 1 2 3 4' not in snip
        assert 'order' not in snip

    def test_keyword_path_snippet_is_raw_text(self, rich):
        """关键词路径的摘要同样取 raw_text（列 6 是原文，不是 f.text）。"""
        res = s.search_messages(rich, '维修')
        assert _ids(res) == [('wxid_alpha', 4)]
        assert _snippet_of(res, 'wxid_alpha', 4) == RAW_FIX
        assert res['results'][0]['match_spans'] == [[0, 2]]

    def test_snippet_highlights_are_relative_offsets(self, rich):
        res = s.search_messages(rich, 'disk')
        hit = res['results'][0]
        start, end = hit['match_spans'][0]
        assert hit['snippet'][start:end].lower() == 'disk'

    def test_non_text_row_has_empty_snippet(self, rich):
        res = s.search_messages(rich, '类型:图片 会话:wxid_alpha')
        assert _snippet_of(res, 'wxid_alpha', 2) == ''


# ==========================================================================
# 4. 正则：索引路径上的大小写/空白敏感确认（T5-A2 的 A8 判别测试）
# ==========================================================================

class TestExcludeSemantics:
    """`-词` 的排除必须**真的生效**，且**两条路径同一套语义**（原文子串，大小写不敏感）。

    排除词**不写进 MATCH**（见 `search._positive_fts_expr`）：它改由 `_apply_excludes`
    在原文上做子串排除，索引路径与 Task 6 的降级路径共用这一份实现。
    这里钉的是**行为**，不是「某个表达式会不会语法错误」——
    `to_fts_expr` 的 `AND NOT` 缺陷早已由 Task 2 修掉（现在产出合法的中缀
    `("维 修" OR "报 修") NOT "报 修"`，只有排除词时返回 `None`），
    所以别再拿「MATCH 里的 NOT 非法」当这些用例的前提。
    """

    def test_exclude_removes_hit_on_raw_text(self, rich):
        """'维修 OR 电话 -报修'：候选 = 维修行 + 电话行，排除后只剩维修行。"""
        res = s.search_messages(rich, '维修 OR 电话 -报修')
        assert _ids(res) == [('wxid_alpha', 4)]

    def test_exclude_case_insensitive(self, rich):
        res = s.search_messages(rich, '/A\\d{4}/ -a1234')
        assert res['total'] == 0

    def test_exclude_only_query_scans_and_keeps_textless_rows(self, rich):
        """只有排除词（没有正向条件）→ 走 msg_meta 全扫 8 行，排除命中正文的 1 行。

        无正文的 3 行（图片/语音）不含任何文本，不因排除词被丢弃 → 8 - 1 = 7。
        """
        res = s.search_messages(rich, '-报修')
        assert res['total'] == 7
        assert ('wxid_alpha', 3) not in _id_set(res)
        assert res['scan_mode'] == 'filter'

    def test_exclude_term_is_not_a_highlight(self, rich):
        ids = _ids(s.search_messages(rich, '/A\\d{4}/ -zzz'))
        assert ids == [('wxid_alpha', 1)]

    def test_exclude_query_degrades_to_scan(self, rich_meta_only):
        """排除词也要在原文上确认 → 文本索引为空时**降级直扫**（Task 6 的 T5-A8 升级）。

        降级路径的语料只有「有正文的消息」（微信 content 表），因此这里恒为 5 - 1 = 4 行，
        而不是索引路径的 8 - 1 = 7 行 —— 见 `TestFallbackStructuralLimits` 里被钉住的
        已知结构差异（无正文消息对降级路径不可见）。
        """
        res = s.search_messages(rich_meta_only, '-报修')
        assert res['used_fallback'] is True
        assert res['scan_mode'] == 'fallback'
        assert ('wxid_alpha', 3) not in _id_set(res)
        assert res['total'] == 4


class TestRegexOnIndexedPath:
    def test_regex_with_whitespace_class_prefilters_and_confirms_on_raw(self, rich):
        """`/报修\\s*电话/`：字面量 '电话' 做 FTS 预筛，再在**原文**上确认。

        原文 '报修 电话 已记录' 命中；char-token 形态 '报 修 电 话 已 记 录' 不命中 ——
        这正是 A8 要堵的「建了索引反而搜不到」。
        """
        res = s.search_messages(rich, '/报修\\s*电话/')
        assert res['regex_degraded'] is False
        assert res['scan_mode'] == 'fts'
        assert _ids(res) == [('wxid_alpha', 3)]
        assert _snippet_of(res, 'wxid_alpha', 3) == RAW_REPAIR

    def test_uppercase_regex_hits_raw_text(self, rich):
        """`/A\\d{4}/`（含大写）必须在原文上确认。

        注意：Task 2 的字面量抽取器遇到 `\\d` 会丢弃已累积的 run（`A`），
        于是本模式被判为**退化**（全量扫描 + Python 确认）—— 见 search_query 的
        deferred minor。退化不影响正确性：命中仍必须是那 1 条，绝不能是 0 条。
        """
        assert sq.extract_regex_literals('A\\d{4}') == ['']
        res = s.search_messages(rich, '/A\\d{4}/')
        assert _ids(res) == [('wxid_alpha', 1)]
        assert res['results'][0]['match_spans'] == [[6, 11]]
        assert _snippet_of(res, 'wxid_alpha', 1)[6:11] == 'A1234'
        assert res['regex_degraded'] is True
        assert res['scan_mode'] == 'filter'

    def test_regex_prefilter_via_literals(self, built):
        res = s.search_messages(built, '/报修|维修/')
        assert res['total'] == 2
        assert res['regex_degraded'] is False

    def test_regex_and_keyword_are_anded(self, rich):
        assert s.search_messages(rich, '维修 /离线/')['total'] == 1
        assert s.search_messages(rich, '维修 /记录/')['total'] == 0


class TestDegradedRegex:
    """退化正则（抽不出必要字面量）必须**真的全量扫描**，而不是返回空。"""

    DEGRADED = '/(?=.*(维修|报修))/'

    def test_degraded_pattern_really_is_degraded(self):
        lits = sq.extract_regex_literals('(?=.*(维修|报修))')
        assert sq.regex_is_degraded(lits) is True

    def test_degraded_regex_full_scans_and_finds_rows(self, rich):
        """判别：旧实现在此生成非法 SQL（无 f 别名却选 f.text）→ 吞成 0 条。"""
        res = s.search_messages(rich, self.DEGRADED)
        assert res['total'] == 2
        assert _id_set(res) == {('wxid_alpha', 3), ('wxid_alpha', 4)}
        assert res['regex_degraded'] is True
        assert res['used_fallback'] is False

    def test_degraded_regex_snippets_come_from_raw_text(self, rich):
        res = s.search_messages(rich, self.DEGRADED)
        assert _snippet_of(res, 'wxid_alpha', 3) == RAW_REPAIR
        assert _snippet_of(res, 'wxid_alpha', 4) == RAW_FIX


class TestSlowQueryVisibility:
    """T5-A6：退化 = 慢路径，必须在返回结构里对用户可见。"""

    def test_degraded_regex_exposes_slow_path(self, rich):
        res = s.search_messages(rich, TestDegradedRegex.DEGRADED)
        assert res['scan_mode'] == 'filter'
        assert res['slow_query_hint']
        assert isinstance(res['elapsed_ms'], float) and res['elapsed_ms'] >= 0.0
        # 全量扫描的候选行数 = msg_meta 全部 8 行（不是 2 条命中）
        assert res['candidate_rows'] == 8

    def test_fast_regex_uses_prefilter_and_reports_no_hint(self, rich):
        res = s.search_messages(rich, '/报修\\s*电话/')
        assert res['scan_mode'] == 'fts'
        assert res['slow_query_hint'] in (None, '')
        assert res['candidate_rows'] == 1     # 字面量 '电话' 只在 RAW_REPAIR 里出现

    def test_keyword_query_reports_fts_scan_mode(self, rich):
        res = s.search_messages(rich, '维修')
        assert res['scan_mode'] == 'fts'
        assert res['slow_query_hint'] in (None, '')

    def test_pure_filter_reports_filter_scan_mode(self, rich):
        res = s.search_messages(rich, '类型:文本')
        assert res['scan_mode'] == 'filter'
        assert res['slow_query_hint'] in (None, '')


# ==========================================================================
# 5. T5-A8：文本索引为空时，关键词/正则查询不得静默返回空
# ==========================================================================

class TestTextIndexGuard:
    """T5-A8 的闸门在 Task 6 由「抛错」升级为「降级直扫」（控制方追加）。

    不变式不变：**关键词查询绝不静默返回空**。只是从「报错」换成「真的去扫」。
    判别性在于断言**真实命中**（不是空），而不是断言异常类型。
    """

    def test_precondition_meta_only_index_is_ready_but_textless(self, rich_meta_only):
        st = si.index_status(rich_meta_only)
        assert st['ready'] is True
        assert st['fts_rows'] == 0
        assert st['msg_text_rows'] == 0
        assert st['meta_rows'] == 8

    def test_keyword_query_degrades_and_returns_real_hits(self, rich_meta_only):
        """判别性（T5-A8 → Task 6）：`text=False` 的索引上搜关键词必须**返回真实命中**。

        旧行为（Task 5）：`message_fts` 为空时 `MATCH` 命中 0 行 → 「看起来正常的空结果」。
        Task 5 的过渡是抛 `TextIndexUnavailableError`；本任务有真降级路径，改为直扫。
        """
        res = s.search_messages(rich_meta_only, '维修')
        assert res['used_fallback'] is True
        assert res['scan_mode'] == 'fallback'
        assert res['total'] == 1, '关键词查询静默返回空 = T5-A8 的不变式退回'
        assert _ids(res) == [('wxid_alpha', 4)]
        assert _snippet_of(res, 'wxid_alpha', 4) == RAW_FIX

    def test_keyword_query_warns_about_the_textless_index(self, rich_meta_only):
        """降级不是无声的：必须有一条 warning 说明文本索引不可用。"""
        res = s.search_messages(rich_meta_only, '维修')
        assert any('文本索引' in w for w in res['warnings']), res['warnings']

    def test_regex_query_degrades_and_returns_real_hits(self, rich_meta_only):
        """正则确认必须落在原文上；没有原文时同样不能装作「没有匹配」。"""
        res = s.search_messages(rich_meta_only, '/(?=.*维修)/')
        assert res['used_fallback'] is True
        assert _ids(res) == [('wxid_alpha', 4)]

    def test_emptied_fts_table_degrades_to_scan(self, rich):
        """`index_meta.fts_rows` 说 5 条，但表被清空 → 也必须降级直扫（status 不是权威）。

        这里 `msg_text` 仍有原文，但降级路径读的是**微信的 content 表**（不是本地索引），
        所以照样拿得到命中。
        """
        con = sqlite3.connect(si.index_path(rich))
        try:
            con.execute('DELETE FROM message_fts')
            con.commit()
        finally:
            con.close()
        assert si.index_status(rich)['ready'] is True     # 状态仍在撒谎
        res = s.search_messages(rich, '维修')
        assert res['used_fallback'] is True
        assert res['total'] == 1

    def test_pure_filter_still_works_without_text_index(self, rich_meta_only):
        """纯筛选走 msg_meta，不受文本索引影响：行集合与有索引时**完全一致**，摘要为空。"""
        res = s.search_messages(rich_meta_only, '类型:文本 日期:2020-09-13 发送者:wxid_beta')
        assert res['total'] == 2
        assert _id_set(res) == {('wxid_alpha', 4), ('111@chatroom', 1)}
        assert res['used_fallback'] is False
        assert all(r['snippet'] == '' for r in res['results'])

    def test_pure_filter_without_text_index_keeps_pagination(self, rich_meta_only):
        res = s.search_messages(rich_meta_only, '类型:文本', per_page=2, page=2)
        assert res['total'] == 5
        assert res['total_pages'] == 3
        assert len(res['results']) == 2


# ==========================================================================
# 6. SQL 失败必须与「真的没有结果」可区分（T5-A1 的后半句）
# ==========================================================================

class _ProbeOkConn:
    """假连接：文本索引探测放行，主查询抛 SQL 错误。

    用来钉住「SQL 失败必须向上抛成结构化错误」这一契约（T5-A1 的后半句），
    与具体是哪个表坏掉无关。`index_status` 可注入，所以不必真破坏索引。
    """

    def __init__(self, error):
        self._error = error
        self.closed = False

    def execute(self, sql, params=()):
        if 'sqlite_master' in sql or 'LIMIT 1' in sql:
            return _ProbeOkRow((1,))
        raise self._error

    def close(self):
        self.closed = True


class _ProbeOkRow:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class TestIndexFailureIsVisible:
    def test_keyword_path_sql_failure_raises_with_original_error_text(self, rich):
        """把 `message_fts` 换成同名的普通表：`schema_ok` 仍为 True、探测也过，
        但 `message_fts MATCH ?` 会抛 `no such column: message_fts`。

        旧实现在这里 `except sqlite3.Error: return []` → 用户看到「没有找到相关消息」。
        """
        con = sqlite3.connect(si.index_path(rich))
        try:
            con.execute('DROP TABLE message_fts')
            con.execute('CREATE TABLE message_fts (rowid INTEGER PRIMARY KEY, x TEXT)')
            con.execute("INSERT INTO message_fts (rowid, x) VALUES (1, 'zz')")
            con.commit()
        finally:
            con.close()
        assert si.index_status(rich)['schema_ok'] is True      # 结构校验查不到这张表
        with pytest.raises(s.SearchIndexError) as ei:
            s.search_messages(rich, '维修')
        body = ei.value.as_dict()
        assert 'message_fts' in (body.get('detail') or '')
        assert 'OperationalError' in (body.get('detail') or '')
        assert body['error'] and body['http_status'] >= 500
        assert 'reindex' not in str(body).lower() or body['hint']

    def test_filter_path_sql_failure_is_not_swallowed_as_empty(self, rich, monkeypatch):
        """纯筛选路径的 SQL 失败同样必须抛错，不能变成 `total=0`。"""
        status = dict(si.index_status(rich))
        status['ready'] = True
        boom = sqlite3.OperationalError('no such column: boom')
        monkeypatch.setattr(s.search_index, 'open_index', lambda d: _ProbeOkConn(boom))
        with pytest.raises(s.SearchIndexError) as ei:
            s.search_messages(rich, '类型:文本', index_status=status)
        assert 'boom' in ei.value.as_dict()['detail']

    def test_fts_path_sql_failure_is_wrapped_the_same_way(self, rich, monkeypatch):
        status = dict(si.index_status(rich))
        status['ready'] = True
        monkeypatch.setattr(s.search_index, 'open_index',
                            lambda d: _ProbeOkConn(sqlite3.OperationalError('fts boom')))
        with pytest.raises(s.SearchIndexError):
            s.search_messages(rich, '维修', index_status=status)

    def test_corrupt_index_that_fails_readiness_routes_to_fallback(self, rich):
        """`msg_meta` 被删 → `schema_ok=False` → 不做索引查询，走降级路由。

        Task 5 阶段降级还是占位（返回 []），所以这里只钉**路由**：
        `used_fallback=True`，而不是「索引路径把这个错误吞了」。
        """
        con = sqlite3.connect(si.index_path(rich))
        try:
            con.execute('DROP TABLE msg_meta')
            con.commit()
        finally:
            con.close()
        assert si.index_status(rich)['ready'] is False
        res = s.search_messages(rich, '维修')
        assert res['used_fallback'] is True
        assert res['scan_mode'] == 'fallback'


# ==========================================================================
# 7. `_execute_indexed` 的单元契约（T5-A1 / A2 / A7）
# ==========================================================================

class TestExecuteIndexedContract:
    """分页在主入口、`_execute_indexed` 返回**全部**匹配行的 6 元组列表（T5-A7 裁决）。"""

    def test_filter_only_path_returns_raw_text_rows(self, rich):
        con = si.open_index(rich)
        try:
            scope = s._resolve_scope(rich, sq.parse_query('类型:文本'), None)
            rows = s._execute_indexed(con, scope, None, [])
        finally:
            con.close()
        assert isinstance(rows, list)
        assert all(len(r) == 6 for r in rows)
        # (create_time, local_id) 升序：alpha/1(T0), alpha/3, alpha/4, group/1, beta/11
        assert [(r[0], r[1]) for r in rows] == [
            ('wxid_alpha', 1), ('wxid_alpha', 3), ('wxid_alpha', 4),
            ('111@chatroom', 1), ('wxid_beta', 11)]
        raws = {r[5] for r in rows}
        assert RAW_ORDER in raws and 'o r d e r' not in ' '.join(str(x) for x in raws)

    def test_keyword_path_returns_raw_text_rows(self, rich):
        con = si.open_index(rich)
        try:
            parsed = sq.parse_query('维修')
            scope = s._resolve_scope(rich, parsed, None)
            rows = s._execute_indexed(con, scope, sq.to_fts_expr(parsed), [])
        finally:
            con.close()
        assert [(r[0], r[1], r[5]) for r in rows] == [('wxid_alpha', 4, RAW_FIX)]

    def test_execute_indexed_has_no_pagination_parameters(self):
        import inspect
        params = list(inspect.signature(s._execute_indexed).parameters)
        assert params == ['conn', 'scope', 'fts_expr', 'regexes']

    def test_filter_only_path_touches_neither_fts_nor_its_alias(self, rich):
        """把 message_fts 整张删掉 → 纯筛选路径必须**完全不受影响**（T5-A1 的机械化判别）。

        这条不依赖对 SQL 文本的断言：只要无关键词的路径引用了 `message_fts` 或别名 `f`，
        就会 `no such table/column` —— 而旧实现把这个异常吞成 0 结果，于是断言失败。
        """
        con = sqlite3.connect(si.index_path(rich))
        try:
            con.execute('DROP TABLE message_fts')
            con.commit()
        finally:
            con.close()
        assert si.index_status(rich)['ready'] is True      # 状态仍以为索引可用
        res = s.search_messages(rich, '类型:文本 日期:2020-09-13 发送者:wxid_beta')
        assert res['total'] == 2
        assert _id_set(res) == {('wxid_alpha', 4), ('111@chatroom', 1)}


# ==========================================================================
# 8. `_resolve_scope` 的单元契约
# ==========================================================================

class TestScopeResolution:
    def _scope(self, rich, q, own=None):
        return s._resolve_scope(rich, sq.parse_query(q), own)

    def test_keys(self, rich):
        sc = self._scope(rich, '维修')
        assert set(sc) >= {'chats', 'senders', 'types', 'date_from', 'date_to', 'label_chats',
                           'warnings'}

    def test_types_and_dates_hand_computed(self, rich):
        sc = self._scope(rich, '类型:文本,图片 日期:2020-09-13')
        assert sc['types'] == {1, 3}
        assert sc['date_from'] == DAY_START
        assert sc['date_to'] == DAY_END

    def test_label_chats_from_contact_db(self, rich):
        assert self._scope(rich, '标签:only_work')['label_chats'] == {'wxid_alpha'}
        assert self._scope(rich, '标签:only_work,non_work')['label_chats'] == {
            'wxid_alpha', '111@chatroom'}
        assert self._scope(rich, '维修')['label_chats'] is None      # 无标签条件 = 不限制
        assert self._scope(rich, '标签:nope')['label_chats'] == set()  # 不存在的标签 = 空集

    def test_unresolved_filter_keeps_raw_token(self, rich):
        sc = self._scope(rich, '会话:wxid_nobody 发送者:wxid_ghost')
        assert sc['chats'] == {'wxid_nobody'}
        assert sc['senders'] == {'wxid_ghost'}
        assert len(sc['warnings']) == 2

    def test_resolved_display_name_maps_to_wxid(self, rich):
        assert self._scope(rich, '会话:阿尔法')['chats'] == {'wxid_alpha'}

    def test_me_resolves_to_own_wxid(self, rich):
        sc = self._scope(rich, '发送者:我', own='wxid_alpha')
        assert sc['senders'] == {'wxid_alpha'}
        assert sc['warnings'] == []          # 「我」是已知语义，不该报警

    def test_me_without_own_wxid_is_reported(self, rich):
        sc = self._scope(rich, '发送者:我')
        assert 'wxid_alpha' not in sc['senders']
        assert sc['warnings']

    def test_empty_scope_means_unrestricted(self, rich):
        sc = self._scope(rich, '维修')
        assert sc['chats'] == set() and sc['senders'] == set()
        assert sc['types'] == set() and sc['date_from'] is None and sc['date_to'] is None


# ==========================================================================
# 9. 排序与分页
# ==========================================================================

class TestSortAndPagination:
    def test_pagination(self, built):
        res = s.search_messages(built, '维修 OR 报修', per_page=1)
        assert res['per_page'] == 1
        assert len(res['results']) == 1
        assert res['total'] == 2
        assert res['total_pages'] == 2
        page2 = s.search_messages(built, '维修 OR 报修', per_page=1, page=2)
        assert _id_set(page2) != _id_set(res)
        assert page2['page'] == 2

    def test_pagination_beyond_last_page_is_empty(self, built):
        res = s.search_messages(built, '维修 OR 报修', per_page=1, page=9)
        assert res['results'] == []
        assert res['total'] == 2

    def test_per_page_is_clamped(self, built):
        # 上限截断
        assert s.search_messages(built, '维修', per_page=99999)['per_page'] == s.MAX_PER_PAGE
        # 非正数 = 「未指定」→ 默认值（0 是 API 里"没传"的常见形态）
        assert s.search_messages(built, '维修', per_page=0)['per_page'] == s.DEFAULT_PER_PAGE
        assert s.search_messages(built, '维修', per_page=-5)['per_page'] == s.DEFAULT_PER_PAGE
        assert s.search_messages(built, '维修', per_page=1)['per_page'] == 1

    def test_page_below_one_is_clamped(self, built):
        assert s.search_messages(built, '维修', page=0)['page'] == 1
        assert s.search_messages(built, '维修', page=-3)['page'] == 1

    def test_invalid_sort_falls_back_to_default(self, built):
        res = s.search_messages(built, '维修 OR 报修', sort='bogus')
        assert res['results'] is not None
        times = [r['create_time'] for r in res['results']]
        assert times == sorted(times, reverse=True)

    def test_sort_time_asc(self, built):
        res = s.search_messages(built, '维修 OR 报修', sort='time_asc')
        times = [r['create_time'] for r in res['results']]
        assert times == sorted(times)

    def test_sort_orders_hand_computed(self, rich):
        desc = s.search_messages(rich, '类型:文本', sort='time_desc')
        assert _ids(desc) == [('wxid_beta', 11), ('111@chatroom', 1),
                              ('wxid_alpha', 4), ('wxid_alpha', 3), ('wxid_alpha', 1)]
        asc = s.search_messages(rich, '类型:文本', sort='time_asc')
        assert _ids(asc) == list(reversed(_ids(desc)))
        chat = s.search_messages(rich, '类型:文本', sort='chat')
        assert _ids(chat) == [('111@chatroom', 1), ('wxid_alpha', 1),
                              ('wxid_alpha', 3), ('wxid_alpha', 4), ('wxid_beta', 11)]

    def test_sql_paged_and_python_confirmed_paths_order_identically(self, rich):
        """两条索引路径必须给出**完全一致**的顺序（三种排序）。

        `类型:文本` 走 SQL 侧 ORDER BY + LIMIT（无正则无排除词），
        `类型:文本 -zzz` 走 Python 排序 + 切片（排除词需在原文上确认）。
        两者顺序若分叉，用户会看到"同一查询换种写法就换了顺序"。
        """
        for sort in ('time_desc', 'time_asc', 'chat'):
            paged = s.search_messages(rich, '类型:文本', sort=sort)
            confirmed = s.search_messages(rich, '类型:文本 -zzz', sort=sort)
            assert _ids(paged) == _ids(confirmed), sort

    def test_pure_filter_is_paged_in_sql_not_materialised(self, many, monkeypatch):
        """无正则无排除词时，SQL 必须带 LIMIT 且只取当页 —— 不是把 12 万行搬到 Python。

        真实数据上「全量物化」实测 2.1s（类型:图片）/1.9s（日期:2026）/4.5s（单字高频词），
        而 spec 对纯筛选的预期是 <50ms。这条测试让"退回全量物化"立刻失败。
        """
        real_open = s.search_index.open_index
        holder = {}

        def spy(decrypted_dir):
            holder['conn'] = _RecordingConn(real_open(decrypted_dir))
            return holder['conn']

        monkeypatch.setattr(s.search_index, 'open_index', spy)
        res = s.search_messages(many, '类型:文本', per_page=10, page=3)
        assert res['total'] == 120
        assert len(res['results']) == 10
        limit_calls = [(sql, n) for sql, n in holder['conn'].calls if 'LIMIT' in sql]
        assert limit_calls, '纯筛选查询没有在 SQL 里分页（退回了全量物化）'
        assert [n for _, n in limit_calls] == [10], \
            'SQL 侧分页只应取当页 10 行，实际 %s' % [n for _, n in limit_calls]

    def test_keyword_query_is_paged_in_sql(self, many, monkeypatch):
        real_open = s.search_index.open_index
        holder = {}

        def spy(decrypted_dir):
            holder['conn'] = _RecordingConn(real_open(decrypted_dir))
            return holder['conn']

        monkeypatch.setattr(s.search_index, 'open_index', spy)
        res = s.search_messages(many, 'msg', per_page=25, page=2)
        assert res['total'] == 120
        assert [n for sql, n in holder['conn'].calls if 'LIMIT' in sql] == [25]
        assert _ids(res)[0] == ('wxid_alpha', 95)      # 第 2 页首行（desc：120..96 是第 1 页）

    def test_regex_query_is_not_paged_in_sql(self, rich, monkeypatch):
        """含正则时必须先确认再分页（SQL 里不能加 LIMIT，否则当页会被打进空洞）。"""
        real_open = s.search_index.open_index
        holder = {}

        def spy(decrypted_dir):
            holder['conn'] = _RecordingConn(real_open(decrypted_dir))
            return holder['conn']

        monkeypatch.setattr(s.search_index, 'open_index', spy)
        res = s.search_messages(rich, '/维修/', per_page=1)
        assert res['total'] == 1
        assert not [sql for sql, _ in holder['conn'].calls if 'LIMIT' in sql]


class TestPaginationBoundaries:
    def test_pages_cover_all_rows_exactly_once(self, many):
        seen = []
        for page in (1, 2, 3):
            res = s.search_messages(many, '类型:文本', per_page=50, page=page)
            assert len(res['results']) == 50 if page < 3 else True
            seen.extend(_ids(res))
        assert len(seen) == 120
        assert len(set(seen)) == 120          # 无重复、无遗漏
        assert res['total'] == 120
        assert res['total_pages'] == 3

    def test_last_page_is_partial(self, many):
        res = s.search_messages(many, '类型:文本', per_page=50, page=3)
        assert len(res['results']) == 20

    def test_page_past_end_is_empty_but_total_stays(self, many):
        res = s.search_messages(many, '类型:文本', per_page=50, page=99)
        assert res['results'] == []
        assert res['total'] == 120
        assert res['total_pages'] == 3

    def test_non_text_rows_are_not_in_text_type_filter(self, many):
        assert s.search_messages(many, '类型:图片')['total'] == 30
        assert s.search_messages(many, '类型:文本')['total'] == 120

    def test_pagination_with_exclude_uses_python_path(self, many):
        """排除词形态同样必须分页正确（只是分页在 Python 侧）。"""
        res = s.search_messages(many, '类型:文本 -zzz', per_page=50, page=3)
        assert res['total'] == 120
        assert len(res['results']) == 20

    def test_pagination_with_degraded_regex(self, many):
        """退化正则的全量扫描 + Python 分页也必须正确。"""
        res = s.search_messages(many, '/(?=.*msg)/', per_page=40, page=2)
        assert res['total'] == 120
        assert res['total_pages'] == 3
        assert len(res['results']) == 40
        assert res['candidate_rows'] >= res['total']


# ==========================================================================
# 10. 返回结构（Task 7/9/10 消费的字段）
# ==========================================================================

class TestResultShape:
    def test_required_keys_present(self, rich):
        res = s.search_messages(rich, '维修')
        for key in ('results', 'total', 'page', 'per_page', 'total_pages', 'parsed',
                    'index', 'used_fallback', 'regex_degraded'):
            assert key in res, key
        for key in ('scan_mode', 'elapsed_ms', 'slow_query_hint', 'candidate_rows',
                    'sender_filter_unsupported', 'fallback_text_only', 'warnings'):
            assert key in res, key

    def test_result_item_keys(self, rich):
        hit = s.search_messages(rich, '维修')['results'][0]
        for key in ('chat_id', 'chat_display_name', 'is_group', 'local_id', 'create_time',
                    'local_type', 'type_label', 'sender_username', 'sender_display_name',
                    'snippet', 'match_spans'):
            assert key in hit, key
        assert hit['chat_display_name'] == '阿尔法'
        assert hit['sender_display_name'] == 'wxid_beta'   # alpha/4 的 real_sender_id=1 → Name2Id rowid 1
        assert hit['is_group'] is False
        assert hit['type_label'] == '文本'

    def test_group_result_marked(self, rich):
        hit = [r for r in s.search_messages(rich, '类型:图片')['results']
               if r['chat_id'] == '111@chatroom'][0]
        assert hit['is_group'] is True
        assert hit['chat_display_name'] == '群聊(111)'

    def test_type_label_fallback(self):
        assert s.type_label(3) == '图片'
        assert s.type_label(34) == '语音'
        assert s.type_label(999) == '其他'
        assert s.type_label(None) == '其他'

    def test_used_fallback_false_on_indexed_path(self, rich):
        assert s.search_messages(rich, '维修')['used_fallback'] is False
        assert s.search_messages(rich, '维修')['sender_filter_unsupported'] is False

    def test_parse_errors_are_echoed_not_hidden(self, rich):
        """语法错误随 parsed.errors 回传（API 层据此 400）。"""
        res = s.search_messages(rich, '维修 类型:彩虹')
        assert res['parsed']['errors']


# ==========================================================================
# 11. 降级路径：真实实现（Task 6 替换 Task 5 的占位）
# ==========================================================================

class TestFallbackRouting:
    """路由：索引不可用 → 降级。占位实现返回 `[]` 时这里只能钉「路由」。"""

    def test_missing_index_routes_to_fallback(self, tmp_path):
        """索引库不存在 → 走降级。

        本夹具**连文本语料也没有**（没有 `message_fts.db`），所以降级只能诚实地返回空：
        `total=0` 在这里是「真的没有语料」，不是「静默假阴性」。
        有语料时的降级命中见下一条与 `TestFallbackPath`。
        """
        d = tmp_path / 'noidx'
        (d / 'message').mkdir(parents=True)
        _make_shard(str(d / 'message' / 'message_0.db'),
                    [('wxid_alpha', [(1, 1, 1600000000, 1)])])
        res = s.search_messages(str(d), '维修')
        assert res['used_fallback'] is True
        assert res['index']['ready'] is False
        assert res['scan_mode'] == 'fallback'
        assert res['total'] == 0

    def test_missing_index_with_corpus_returns_real_hits(self, tmp_path):
        """同一份语料只把索引删掉 → 结果必须与有索引时**一致**（Task 6 的核心承诺）。"""
        d = tmp_path / 'noidx2'
        (d / 'message').mkdir(parents=True)
        _make_shard(str(d / 'message' / 'message_0.db'),
                    [('wxid_alpha', [(1, 1, T0, 1)])])
        _make_fts_db(str(d / 'message' / 'message_fts.db'),
                     [('维修服务器离线', 1, T0 * 1000, 1, 1, 1, T0)],
                     usernames=('wxid_alpha',))
        res = s.search_messages(str(d), '维修')
        assert res['used_fallback'] is True
        assert res['index']['exists'] is False
        assert _ids(res) == [('wxid_alpha', 1)]
        assert _snippet_of(res, 'wxid_alpha', 1) == '维修服务器离线'


class TestFallbackPath:
    """索引未构建时必须仍可用（只是慢），共用语法的全部语义。

    夹具语料（`message_fts_v4_0_content`，会话由 name2id 解析）：
      id=1 c0='维修服务器'      c1=1 c3=1  c4=1(→wxid_alpha)   c6=1600000000
      id=2 c0='发票已开'        c1=2 c3=1  c4=1(→wxid_alpha)   c6=1600000100
      id=3 c0='群里的语音'      c1=1 c3=34 c4=2(→111@chatroom) c6=1600000300
    因此 `类型:文本` = 2 行、`类型:语音` = 1 行。
    """

    @pytest.fixture
    def not_built(self, tmp_path):
        d = tmp_path / 'nofb'
        (d / 'message').mkdir(parents=True)
        _make_shard(str(d / 'message' / 'message_0.db'), [
            ('wxid_alpha', [(1, 1, 1600000000, 1), (2, 1, 1600000100, 1)]),
            ('111@chatroom', [(1, 1, 1600000300, 2)]),
        ])
        import sqlite3 as _s
        con = _s.connect(str(d / 'message' / 'message_fts.db'))
        con.execute('CREATE TABLE message_fts_v4_0_content (id INTEGER PRIMARY KEY,'
                    ' c0 TEXT, c1 INTEGER, c2 INTEGER, c3 INTEGER, c4 INTEGER,'
                    ' c5 INTEGER, c6 INTEGER)')
        con.execute('CREATE TABLE name2id (username TEXT)')
        con.executemany('INSERT INTO name2id (username) VALUES (?)',
                        [('wxid_alpha',), ('111@chatroom',)])
        con.execute('INSERT INTO message_fts_v4_0_content VALUES'
                    " (NULL,'维修服务器',1,1600000000000,1,1,1,1600000000)")
        con.execute('INSERT INTO message_fts_v4_0_content VALUES'
                    " (NULL,'发票已开',2,1600000100000,1,1,2,1600000100)")
        con.execute('INSERT INTO message_fts_v4_0_content VALUES'
                    " (NULL,'群里的语音',1,1600000300000,34,2,2,1600000300)")
        con.commit()
        con.close()
        return str(d)

    def test_fallback_used_when_index_missing(self, not_built):
        res = s.search_messages(not_built, '维修')
        assert res['used_fallback'] is True
        assert res['index']['ready'] is False
        assert res['total'] == 1

    def test_fallback_type_filter(self, not_built):
        assert s.search_messages(not_built, '语音 类型:语音')['total'] == 1
        assert s.search_messages(not_built, '语音 类型:图片')['total'] == 0

    def test_fallback_chat_filter(self, not_built):
        assert s.search_messages(not_built, '语音 会话:111@chatroom')['total'] == 1

    def test_fallback_date_filter(self, not_built):
        assert s.search_messages(not_built, '维修 日期:2026')['total'] == 0

    def test_fallback_regex(self, not_built):
        res = s.search_messages(not_built, '/报修|维修/')
        assert res['used_fallback'] is True
        assert res['total'] == 1

    def test_fallback_no_keyword_only_filters(self, not_built):
        """无关键词时也要能用（走 msg_meta 不可用时退化为全量文本扫描）。

        三行内容里 c3 分别是 1（文本）、1（文本）、34（语音），
        所以 `类型:文本` 命中 2 条。
        """
        res = s.search_messages(not_built, '类型:文本')
        assert res['used_fallback'] is True
        assert res['total'] == 2

    def test_fallback_marks_sender_filter_unsupported(self, not_built):
        """降级路径无法解析发送者（content 表只有分片内序号），必须显式告知。"""
        res = s.search_messages(not_built, '语音 发送者:某某')
        assert res['used_fallback'] is True
        assert res['sender_filter_unsupported'] is True


class TestFallbackRowContract:
    """第 6 列必须是**原文**，不是 char-token —— 否则摘要/高亮/正则会静默分叉。"""

    @pytest.fixture
    def rich_no_index(self, tmp_path):
        """与 `rich` **同一份合成语料**，只是不建索引 → 天然走降级路径。"""
        return _make_rich_dir(tmp_path)

    def test_rows_are_six_tuples_of_raw_text(self, rich_no_index):
        parsed = sq.parse_query('类型:文本')
        scope = s._resolve_scope(rich_no_index, parsed, None)
        rows = s._execute_fallback(rich_no_index, scope, parsed, [])
        assert all(len(r) == 6 for r in rows)
        assert all(r[4] is None for r in rows)          # 发送者：content 表只有序号
        raws = [r[5] for r in rows]
        assert sorted(raws) == sorted([RAW_ORDER, RAW_REPAIR, RAW_FIX, RAW_DISK, RAW_VOICE])
        # 判别：char-token 形态（小写化 + 逐字符空格）绝不能出现在第 6 列
        assert not any('o r d e r' in (raw or '').lower() for raw in raws)
        assert RAW_ORDER in raws and '\n' in RAW_ORDER

    def test_pure_filter_snippet_keeps_case_and_whitespace(self, rich_no_index):
        res = s.search_messages(rich_no_index, '类型:文本 会话:wxid_alpha')
        assert _snippet_of(res, 'wxid_alpha', 1) == RAW_ORDER
        assert '\n' in _snippet_of(res, 'wxid_alpha', 1)

    def test_keyword_snippet_matches_the_indexed_path(self, tmp_path):
        d = _make_rich_dir(tmp_path)
        si.build_index(d)
        indexed = s.search_messages(d, 'disk')
        with _index_hidden(d):
            fallback = s.search_messages(d, 'disk')
        assert _comparable(indexed) == _comparable(fallback)
        assert _snippet_of(fallback, '111@chatroom', 1) == RAW_DISK

    def test_regex_is_confirmed_on_the_fallback_path(self, tmp_path):
        """字面量预筛是**超集**，必须在降级路径上用原文再确认一次。

        `/报修\\s+电话/` 抽出的字面量是 `['电话']`（**不退化**）→ LIKE '%电话%'
        会选中原文 `报修电话`（没有空白）这一行；正则要求真空白，确认后必须丢弃。
        只预筛不确认的话这里就是 1 行（实测：字面量预筛在索引里命中 1 行候选，见报告 §8.2）。
        另一半是正例：`/报修\\s*电话/` 允许零空白 → 必须命中。
        """
        d = tmp_path / 'rx'
        (d / 'message').mkdir(parents=True)
        _make_fts_db(str(d / 'message' / 'message_fts.db'),
                     [('报修电话', 1, T0 * 1000, 1, 1, 1, T0)],
                     usernames=('wxid_alpha',))
        assert sq.extract_regex_literals('报修\\s+电话') == ['电话']
        res = s.search_messages(str(d), '/报修\\s+电话/')
        assert res['used_fallback'] is True
        assert res['regex_degraded'] is False
        assert res['total'] == 0, '字面量预筛被当成了结果（漏了原文确认）'
        hit = s.search_messages(str(d), '/报修\\s*电话/')
        assert _ids(hit) == [('wxid_alpha', 1)]
        assert _snippet_of(hit, 'wxid_alpha', 1) == '报修电话'

    def test_rows_without_resolvable_chat_are_skipped(self, tmp_path):
        """content 表的 session_id 解析不出会话（name2id 变了）→ 该行丢弃。

        与 `search_index.build_fts_ex` 的 `skipped_no_chat` 同一取向：`chat_id=None`
        的垃圾行进结果比丢行更糟。
        """
        d = tmp_path / 'bad'
        (d / 'message').mkdir(parents=True)
        _make_fts_db(str(d / 'message' / 'message_fts.db'), [
            ('known one', 1, T0 * 1000, 1, 1, 1, T0),
            ('orphan text', 2, (T0 + 100) * 1000, 1, 99, 1, T0 + 100),
        ], usernames=('wxid_alpha',))
        assert s.search_messages(str(d), 'orphan')['total'] == 0
        assert s.search_messages(str(d), 'known')['total'] == 1


class TestIndexedFallbackEquivalence:
    """控制方必做增补（T5-A4 结转）：同一批查询在两条路径上必须给出**同一行集合**。

    每条用例都断言**手算期望行**，而不是「两路径相等」——否则「两路径都返回空」
    会让整批测试假绿（这正是 T5-A1 那类静默假阴性逃过单路径测试的方式）。

    已知且被 brief 承认的差异只有 `发送者:`（见 `TestFallbackStructuralLimits`），
    因此它不在这批严格等价的查询里，而是单独断言 `sender_filter_unsupported`。
    """

    QUERIES = [
        ('维修', {('wxid_alpha', 4)}),
        ('维修 服务器', {('wxid_alpha', 4)}),
        ('维修 发票', set()),
        ('"Order A1234"', {('wxid_alpha', 1)}),
        ('维修 OR 报修', {('wxid_alpha', 3), ('wxid_alpha', 4)}),
        ('维修 OR 电话 -报修', {('wxid_alpha', 4)}),
        ('类型:文本', {('wxid_alpha', 1), ('wxid_alpha', 3), ('wxid_alpha', 4),
                     ('111@chatroom', 1), ('wxid_beta', 11)}),
        ('类型:文本 日期:2020-09-13 会话:wxid_alpha',
         {('wxid_alpha', 1), ('wxid_alpha', 3), ('wxid_alpha', 4)}),
        ('类型:文本 标签:only_work', {('wxid_alpha', 1), ('wxid_alpha', 3), ('wxid_alpha', 4)}),
        ('类型:文本 标签:non_work', {('111@chatroom', 1)}),
        ('会话:111@chatroom 类型:文本', {('111@chatroom', 1)}),
        ('日期:2020-09-14', {('wxid_beta', 11)}),
        ('日期:2020-09-13 类型:文本',
         {('wxid_alpha', 1), ('wxid_alpha', 3), ('wxid_alpha', 4), ('111@chatroom', 1)}),
        ('/报修\\s*电话/', {('wxid_alpha', 3)}),
        ('/(?=.*(维修|报修))/', {('wxid_alpha', 3), ('wxid_alpha', 4)}),
        ('disk 类型:文本 会话:111@chatroom', {('111@chatroom', 1)}),
    ]

    def test_battery_is_not_all_empty(self):
        """护栏：这批用例里必须有过半是非空期望，否则等价性测试会退化成空转。"""
        non_empty = [q for q, exp in self.QUERIES if exp]
        assert len(non_empty) >= 12, non_empty

    @pytest.mark.parametrize('q,expected', QUERIES, ids=[q for q, _ in QUERIES])
    def test_row_sets_are_identical(self, rich, q, expected):
        indexed, fallback = _both_paths(rich, q)
        assert _id_set(indexed) == expected, '手算期望不符（索引路径）'
        assert _id_set(fallback) == expected, '手算期望不符（降级路径）'
        assert indexed['scan_mode'] in ('fts', 'filter')
        assert fallback['scan_mode'] == 'fallback'

    @pytest.mark.parametrize('sort', ['time_desc', 'time_asc', 'chat'])
    def test_sort_variants_are_identical(self, rich, sort):
        indexed, fallback = _both_paths(rich, '类型:文本', sort=sort)
        assert len(indexed['results']) == 5

    def test_pagination_boundaries_are_identical(self, many):
        """分页边界：索引路径在 SQL 侧 `LIMIT/OFFSET`，降级路径在 Python 侧切片。

        两种切法的页码/页大小/行序必须逐页一致（含越界页）。
        夹具：120 条有正文的文本消息，`类型:文本` 与关键词 'msg' 都命中 120 条。
        """
        for q in ('类型:文本', 'msg'):
            # (page, per_page, 当页行数, total_pages)
            for page, per_page, n_page, n_pages in ((1, 50, 50, 3), (2, 50, 50, 3),
                                                    (3, 50, 20, 3), (4, 50, 0, 3),
                                                    (2, 25, 25, 5), (5, 25, 20, 5),
                                                    (99, 50, 0, 3)):
                indexed, fallback = _both_paths(many, q, per_page=per_page, page=page)
                assert indexed['total'] == fallback['total'] == 120
                assert indexed['total_pages'] == fallback['total_pages'] == n_pages, \
                    (q, page, per_page)
                assert len(indexed['results']) == len(fallback['results']) == n_page, \
                    (q, page, per_page)

    def test_no_index_at_all_uses_fallback_everywhere(self, rich):
        """整批查询都必须在降级路径上可用（不是只支持关键词）。"""
        with _index_hidden(rich):
            for q, expected in self.QUERIES:
                res = s.search_messages(rich, q)
                assert res['used_fallback'] is True
                assert _id_set(res) == expected, q


class TestFallbackStructuralLimits:
    """降级路径的**结构性上限**（已知差异，本任务的实现范围不修 —— 需控制方裁决）。

    降级语料 = 微信 `message_fts_v4_*_content`：只有**有正文**的消息。
    索引路径的纯筛选语料 = `msg_meta`：100% 覆盖（含图片/语音/视频）。
    这条差异**不是**本任务引入的，而是「扫描微信文本表」这一降级策略的固有上限；
    修它需要降级路径同时扫 `Msg_*` 分片表（等于在内存里重建 msg_meta）。
    """

    def test_textless_messages_are_invisible_to_fallback(self, rich):
        indexed = s.search_messages(rich, '类型:图片,语音')
        assert indexed['total'] == 3            # msg_meta 全覆盖：alpha/2, alpha/5, group/2
        with _index_hidden(rich):
            fallback = s.search_messages(rich, '类型:图片,语音')
        assert fallback['total'] == 0, (
            '降级路径看不到任何无正文消息（已报告，勿默默改成期望 0）')

    def test_exclude_only_query_sees_only_text_rows_on_fallback(self, rich):
        """`-报修`：索引路径 8 - 1 = 7（无正文消息不含文本，不被排除），降级路径 5 - 1 = 4。"""
        assert s.search_messages(rich, '-报修')['total'] == 7
        with _index_hidden(rich):
            fallback = s.search_messages(rich, '-报修')
        assert fallback['total'] == 4
        assert ('wxid_alpha', 3) not in _id_set(fallback)

    def test_sender_filter_is_not_applied_on_fallback(self, rich):
        """brief 明示的已知差异：降级路径解析不出发送者 → 条件不生效但**必须显式告知**。

        逐行推导（发送者由 real_sender_id → 分片 Name2Id rowid 解析，1=wxid_beta）：
          类型=文本 的 5 行：alpha/1(beta? 否，rowid2=alpha)、alpha/3(alpha)、
          alpha/4(beta)、group/1(beta)、beta/11(beta) → 发送者 wxid_beta 命中 3 行。
        """
        indexed = s.search_messages(rich, '类型:文本 发送者:wxid_beta')
        assert _id_set(indexed) == {('wxid_alpha', 4), ('111@chatroom', 1),
                                    ('wxid_beta', 11)}
        with _index_hidden(rich):
            fallback = s.search_messages(rich, '类型:文本 发送者:wxid_beta')
        assert fallback['sender_filter_unsupported'] is True
        assert fallback['total'] == 5, '发送者条件未生效（已知差异，已在响应里标注）'

    @pytest.mark.parametrize('raw_text', ['维 修服务器', '维，修服务器'])
    def test_term_matching_now_agrees_on_inner_separators(self, tmp_path, raw_text):
        """**§15.5 收紧后**：关键词的匹配语义两路径一致（差异已消除，不再"接受"）。

        本用例原来叫 `test_term_matching_semantics_differ_on_inner_separators`，断言
        降级路径返回 **0**（字面 LIKE 漏掉空白/标点切断的形式）。控制方 §15.5 裁决收紧关键词后，
        同一夹具下两路径都是 **1** —— 所以这里改成**相等断言**，并把旧的"接受差异"结论撤回
        （撤回原因：当时认为"要一致得放弃 SQL 预筛、代价 7–13 倍"，只对"全量搬 Python"成立；
        逐字符超集 + 折叠确认实测 0.52s → 0.50s）。
        `维 修服务器` 是**空白**切断、`维，修服务器` 是**标点**切断（`unicode61` 丢标点）。
        """
        d = tmp_path / 'ws'
        (d / 'message').mkdir(parents=True)
        _make_shard(str(d / 'message' / 'message_0.db'),
                    [('wxid_alpha', [(1, 1, T0, 1)])])
        _make_fts_db(str(d / 'message' / 'message_fts.db'),
                     [(raw_text, 1, T0 * 1000, 1, 1, 1, T0)],
                     usernames=('wxid_alpha',))
        si.build_index(d)
        indexed = s.search_messages(d, '维修')
        assert _ids(indexed) == [('wxid_alpha', 1)], '样本必须非空'
        with _index_hidden(d):
            fallback = s.search_messages(d, '维修')
        assert _ids(fallback) == [('wxid_alpha', 1)], _ids(fallback)


# ==========================================================================
# 12. 降级语料的「只有正文」上限必须是**显式**的（控制方追加项）
# ==========================================================================

class TestFallbackTextOnlyVisibility:
    """索引缺失时，按类型/日期等筛选会少给甚至给空 —— 这不等于「库里没有」。

    降级语料只有**有正文**的消息，而索引路径的纯筛选语料 `msg_meta` 是 100% 覆盖。
    本轮不修语料（要修得在降级路径重建 msg_meta），但**不允许静默**：
    响应里同时给出人类可读的 warning 与结构化字段 `fallback_text_only`。

    反例同样重要：**纯关键词**查询在降级路径是完整的（它本来就是文本查询），
    加了这条 warning 就是误报 —— 用户会被引向"结果不全"的错误结论。
    """

    WARN_MARK = '有正文'

    def test_type_filter_on_fallback_says_the_universe_is_text_only(self, rich):
        indexed = s.search_messages(rich, '类型:图片')
        assert indexed['total'] == 2                     # msg_meta 全覆盖：alpha/2, group/2
        with _index_hidden(rich):
            res = s.search_messages(rich, '类型:图片')
        assert res['used_fallback'] is True
        assert res['fallback_text_only'] is True
        assert res['total'] == 0, '降级路径看不到无正文消息（这正是要解释的假阴性）'
        assert any(self.WARN_MARK in w for w in res['warnings']), res['warnings']
        assert any('索引' in w or '降级' in w for w in res['warnings'])

    @pytest.mark.parametrize('q', [
        '类型:图片', '日期:2020-09-14', '会话:111@chatroom',
        '标签:non_work', '发送者:wxid_beta',
    ])
    def test_every_non_text_filter_flags_it(self, rich, q):
        """类型 / 日期 / 会话 / 标签 / 发送者 —— 控制方列的五类都要触发。"""
        with _index_hidden(rich):
            res = s.search_messages(rich, q)
        assert res['used_fallback'] is True, q
        assert res['fallback_text_only'] is True, q
        assert any(self.WARN_MARK in w for w in res['warnings']), (q, res['warnings'])

    def test_pure_keyword_query_is_not_flagged(self, rich):
        """纯关键词查询在降级路径是**完整**的 → 绝不能误报。"""
        with _index_hidden(rich):
            res = s.search_messages(rich, '维修')
        assert res['used_fallback'] is True
        assert res['fallback_text_only'] is False
        assert _ids(res) == [('wxid_alpha', 4)]
        assert not any(self.WARN_MARK in w for w in res['warnings']), res['warnings']

    def test_keyword_plus_phrase_is_not_flagged(self, rich):
        with _index_hidden(rich):
            res = s.search_messages(rich, '维修 服务器')
        assert res['fallback_text_only'] is False
        assert not any(self.WARN_MARK in w for w in res['warnings'])

    def test_exclude_only_query_is_flagged(self, rich):
        """控制方裁决（§5.3）：**只有排除词**的查询在降级路径同样不完整，必须触发。

        `-报修` 的结果集是"所有消息 − 命中排除词的那些"，而无正文消息**应当**在结果里
        （它们不可能包含被排除的词）。降级语料里这些行根本不存在：
        真实数据 `-报修` 索引 1,018,462 ／ 降级 877,386（**少 141,076 行**），夹具 7 / 4。
        """
        indexed = s.search_messages(rich, '-报修')
        assert indexed['total'] == 7
        with _index_hidden(rich):
            res = s.search_messages(rich, '-报修')
        assert res['used_fallback'] is True
        assert res['total'] == 4, '差集就是这个提示要解释的东西'
        assert res['fallback_text_only'] is True
        assert any(self.WARN_MARK in w for w in res['warnings']), res['warnings']

    def test_exclude_query_with_a_keyword_is_not_flagged(self, rich):
        """有正向文本条件（关键词）时降级路径对文本查询是完整的 → 不该触发。"""
        with _index_hidden(rich):
            res = s.search_messages(rich, '维修 -报修')
        assert res['fallback_text_only'] is False, res['warnings']

    def test_regex_only_query_is_not_flagged(self, rich):
        """正则本身要求命中文本 → 降级路径对它是完整的，不触发（防误报）。"""
        with _index_hidden(rich):
            res = s.search_messages(rich, '/维修/')
        assert res['total'] == 1
        assert res['fallback_text_only'] is False, res['warnings']

    def test_indexed_path_never_flags_it(self, rich):
        """索引路径 100% 覆盖 → 同一条查询既不该降级、也不该出现这个字段为真。"""
        res = s.search_messages(rich, '类型:图片')
        assert res['used_fallback'] is False
        assert res['fallback_text_only'] is False
        assert res['total'] == 2
        assert not any(self.WARN_MARK in w for w in res['warnings']), res['warnings']

    def test_flag_is_in_the_documented_contract(self, rich):
        res = s.search_messages(rich, '维修')
        assert 'fallback_text_only' in res
        assert isinstance(res['fallback_text_only'], bool)


# ==========================================================================
# 13. Critical：`own_wxid` 是**默认参数**，绝不能变成筛选条件
# ==========================================================================

class TestOwnWxidIsNeverAFilter:
    """`own_wxid` 只用来解析「发送者:我」，**不能**在用户没写 `发送者:` 时注入 sender 约束。

    缺陷（控制方在真实索引上实测）：`_resolve_scope` 无条件把 `own_wxid` 并进
    `scope['senders']`，于是 SQL 里多出 `m.sender_username IN (<本人 wxid>)`。
    而真实 `msg_meta` 里 **`sender_username == own_wxid` 的行数 = 0**
    （实测；本人消息不落在这个列里）→ **任何**查询都归零：
    `维修` 1389→0、`类型:图片` 87941→0、`维修 OR 报修` 1815→0。
    严重性在于 `own_wxid` 是正常调用路径的默认参数（Task 7 的 HTTP 层从 `_cfg()` 取、
    Task 8 的 CLI 用 `get_backup_wxid()`），即**索引建好后 Web/CLI 每次搜索都返回 0 条**，
    而状态栏显示"索引就绪"、`warnings` 为空 —— 静默的，最坏形态。

    夹具 `own_index` 特意镜像真实数据的那条性质：**没有任何一行的 sender_username 等于本人
    wxid**，而且留了一条 `real_sender_id = 0`（→ NULL）的本人消息。
    """

    OWN = 'wxid_me'
    QUERIES = ['维修', '类型:图片', '维修 OR 报修', '维修 OR 报修 -报修',
               '类型:文本', '日期:2020-09-13']

    @pytest.fixture
    def own_index(self, tmp_path):
        """镜像真实数据：本人消息的 sender_username 不是 own_wxid（甚至为 NULL）。

        msg_meta 的发送者由分片 Name2Id[real_sender_id] 解析：
          rowid 1 = wxid_peer（对方）
          rowid 0 不存在 → real_sender_id = 0 解析成 NULL（**本人消息**在真实数据里的形态）
        4 条消息：3 条文本（前两条对方发、第二条本人发）+ 1 条图片；
        文本里 '维修服务器' / '报修电话' 用来构造 OR 与排除词用例。
        """
        d = tmp_path / 'own'
        (d / 'message').mkdir(parents=True)
        _make_shard(str(d / 'message' / 'message_0.db'), [
            ('wxid_peer', [
                (1, 1, T0, 1),          # 对方发的文本
                (2, 1, T0 + 1, 0),      # **本人**发的文本 → sender_username NULL
                (3, 1, T0 + 2, 1),      # 对方发的文本
                (4, 3, T0 + 3, 1),      # 对方发的图片（无正文）
            ]),
        ])
        _make_fts_db(str(d / 'message' / 'message_fts.db'), [
            ('维修服务器', 1, T0 * 1000, 1, 1, 1, T0),
            ('本人发的文本', 2, (T0 + 1) * 1000, 1, 1, 1, T0 + 1),
            ('报修电话', 3, (T0 + 2) * 1000, 1, 1, 1, T0 + 2),
        ], usernames=('wxid_peer',))
        si.build_index(d)
        return d

    def test_fixture_has_no_row_whose_sender_is_me(self, own_index):
        """先把夹具的真实性钉住：索引里不存在「发送者 = 本人 wxid」的行。

        这正是真实数据实测到的性质（`sender_username == own_wxid` 的行数 = 0），
        也是"把 own_wxid 并进 senders = 全表归零"的机制。若哪天夹具变了，
        下面的回归测试就不再具备判别力。
        """
        con = sqlite3.connect(si.index_path(own_index))
        try:
            mine = con.execute('SELECT COUNT(*) FROM msg_meta WHERE sender_username = ?',
                               (self.OWN,)).fetchone()[0]
            nulls = con.execute('SELECT COUNT(*) FROM msg_meta'
                                ' WHERE sender_username IS NULL').fetchone()[0]
        finally:
            con.close()
        assert mine == 0
        assert nulls == 1                  # 本人那条消息

    @pytest.mark.parametrize('q', QUERIES, ids=QUERIES)
    def test_own_wxid_does_not_change_the_result_set(self, own_index, q):
        """回归主测试：同一个查询传不传 `own_wxid`，`total` 与行集合必须**完全相同**。"""
        without = s.search_messages(own_index, q)
        with_own = s.search_messages(own_index, q, own_wxid=self.OWN)
        assert without['total'] > 0, '夹具本身应当有命中，否则这条测试会退化成 0==0'
        assert with_own['total'] == without['total'], (
            'own_wxid 被当成了筛选条件！q=%r without=%s with=%s'
            % (q, without['total'], with_own['total']))
        assert _id_set(with_own) == _id_set(without)
        assert _ids(with_own) == _ids(without)

    def test_the_four_query_shapes_against_the_placeholder_era_default(self, own_index):
        """把控制方的实测数字在夹具上重演一遍：没有 own_wxid 时各形态都非空。"""
        expected = {
            '维修': 1,                    # 只有 '维修服务器'
            '类型:图片': 1,               # 那条无正文的图片（msg_meta 全覆盖）
            '维修 OR 报修': 2,            # '维修服务器' + '报修电话'
            '维修 OR 报修 -报修': 1,      # 排除 '报修电话'
        }
        for q, n in expected.items():
            res = s.search_messages(own_index, q)
            assert res['total'] == n, q
            assert s.search_messages(own_index, q, own_wxid=self.OWN)['total'] == n, q

    def test_no_sender_constraint_is_injected(self, own_index):
        """不注入：给了 `own_wxid` 但查询没有 `发送者:` → `scope['senders']` 必须是**空集**。

        空集 = 不限制（见 `_resolve_scope` 的 docstring）；非空集会在 SQL 里变成
        `m.sender_username IN (?...)`，那正是这次归零的机制。
        """
        scope = s._resolve_scope(own_index, sq.parse_query('维修'), self.OWN)
        assert scope['senders'] == set()
        # 与不传 own_wxid 时**一致**（这才是"默认参数不改变行为"）
        assert scope['senders'] == s._resolve_scope(
            own_index, sq.parse_query('维修'), None)['senders']

    def test_me_still_resolves_to_my_wxid(self, own_index):
        """「发送者:我」被显式要求时**仍然**要注入本人 wxid（闸门不能把功能一起关掉）。"""
        scope = s._resolve_scope(own_index, sq.parse_query('发送者:我 维修'), self.OWN)
        assert scope['senders'] == {self.OWN}
        assert scope['warnings'] == [], '「我」是已知语义，不该报警'

    def test_me_matches_a_row_whose_sender_is_me(self, rich):
        """带 `own_wxid` 时 `发送者:我 <词>` 能定位本人消息。

        `rich` 夹具里 owner 的消息 sender_username 就是 `wxid_alpha`
        （`类型:文本` 里 alpha/1 与 alpha/3 是本人发的）。
        """
        scope = s._resolve_scope(rich, sq.parse_query('发送者:我 类型:文本'), 'wxid_alpha')
        assert scope['senders'] == {'wxid_alpha'}, '本用例要求「我」解析成唯一本人 wxid'
        res = s.search_messages(rich, '类型:文本 发送者:我', own_wxid='wxid_alpha')
        assert _ids(res) == [('wxid_alpha', 3), ('wxid_alpha', 1)]

    def test_me_without_own_wxid_still_warns(self, own_index):
        """没给 own_wxid 时「发送者:我」仍然按原样提示，不能悄悄变成"不限制"。

        `_resolve_names` 的既定语义：解析不出的 token **原样留下**（精确等值，不放宽结果）
        —— 所以这里 `senders == {'我'}`（`IN ('我')` 匹配 0 行），而不是空集；
        关键是有 warning 把"需要 own_wxid"讲清楚，不静默。
        """
        scope = s._resolve_scope(own_index, sq.parse_query('发送者:我'), None)
        assert scope['senders'] == {'我'}
        assert any('own_wxid' in w for w in scope['warnings']), scope['warnings']

    def test_me_does_not_widen_when_no_row_matches(self, own_index):
        """夹具里没有任何一行的 `sender_username == 本人` → `发送者:我` 必须精确返回 0。

        这条钉的是**过滤是精确等值、不会放宽**：宁可为空，也绝不能变成"别人的消息"。
        """
        assert s.search_messages(own_index, '发送者:我 维修', own_wxid=self.OWN)['total'] == 0
        assert s.search_messages(own_index, '维修', own_wxid=self.OWN)['total'] == 1

    def test_me_warns_when_the_index_has_no_self_sender(self, own_index):
        """**Critical #17 的诚实兜底**：规范化之后索引里仍然一行都找不到本人身份时，
        `发送者:我` 会返回 0 —— 这种情况必须**显式告知**（换账号/索引由别的账号构建），
        不能静默返回"看起来正常的空结果"。

        真实数据上裸 wxid 有 222,012 行，所以这条分支在正常数据上**不会**触发；
        这里用一个"发送者列里没有本人"的夹具把它逼出来。
        """
        res = s.search_messages(own_index, '发送者:我 维修', own_wxid=self.OWN)
        assert res['total'] == 0
        assert any('本人' in w for w in res['warnings']), res['warnings']

    def test_no_self_warning_when_the_query_has_no_sender_token(self, own_index):
        """没有 `发送者:` 时不得出现这条提示（否则是误报）。"""
        res = s.search_messages(own_index, '维修', own_wxid=self.OWN)
        assert not any('本人' in w for w in res['warnings']), res['warnings']

    @pytest.fixture
    def noisy_contacts(self, tmp_path):
        """通讯录里有一个昵称含「我」的联系人 —— 真实的模糊命中来源。

        真实数据实测：`_resolve_names` 对 token `'我'` 做**子串**模糊匹配，
        命中 130 个联系人（昵称/备注里含「我」的人极多）。
        """
        d = tmp_path / 'noisy'
        _make_contact_db(str(d / 'contact' / 'contact.db'),
                         [('wxid_noisy', '我朋友', []), ('wxid_me', '本人', [])], {})
        return str(d)

    def test_self_token_is_not_fuzzy_matched(self, noisy_contacts):
        """「我」是**保留语义 token**：只能解析成本人，绝不能被当成通讯录模糊搜索词。

        判别：`发送者:我` 的 scope 必须**恰好**是 `{own_wxid}`，不能把昵称含「我」的
        `wxid_noisy`（真实数据里是 130 个联系人）一起并进来 —— 那是**别人的**消息。
        """
        sc = s._resolve_scope(noisy_contacts, sq.parse_query('发送者:我'), 'wxid_me')
        assert sc['senders'] == {'wxid_me'}, (
            '「我」被拿去模糊匹配通讯录了：%s' % sorted(sc['senders']))

    def test_self_token_without_own_wxid_is_kept_raw_not_widened(self, noisy_contacts):
        """没有 own_wxid 时也不能放宽：保留原样 token（`IN ('我')` → 0 行）+ 明确提示。

        绝**不能**返回空集 —— 空集会被调用方读成"该字段不限制"，
        一次精确查询被悄悄放大成「全部发送者」。
        """
        sc = s._resolve_scope(noisy_contacts, sq.parse_query('发送者:我'), None)
        assert sc['senders'] == {'我'}
        assert any('own_wxid' in w for w in sc['warnings']), sc['warnings']


# ==========================================================================
# 14. 🔴 Critical #17：`own_wxid` 的「值形式」与索引里的形式不一致
# ==========================================================================

class TestOwnWxidFormNormalization:
    """配置里存的是**账号目录名**（`<裸 wxid>_<4位十六进制>`），而索引里存的是**裸 wxid**。

    真实数据实测（只读，只报计数）：
        配置值（24 字符，形如 `wxid_xxxxx_10e8`）在 `msg_meta.sender_username` 精确匹配 **0** 行
        去掉尾部 `_10e8` 后的裸 wxid（19 字符）精确匹配 **222,012** 行
        分片 `Name2Id`：含裸 wxid True；含 24 字符整串 False
        行为：`发送者:我` 用裸 wxid = 222,485 ✓ ／ 用 24 字符配置值 = 473 ✗（≈完全没过滤）
    即索引**完全能表达"我发的消息"**，只是传进来的值永远匹配不上。

    这是与 Critical #16（无条件注入）**独立**的缺陷：#16 让所有查询归零，#17 让
    `发送者:我` 即使闸门正确也仍然返回 0。
    """

    BARE = 'wxid_' + 'a' * 14          # 19 字符 —— 消息侧 sender_username 的形式
    DIRNAME = BARE + '_10e8'           # 24 字符 —— config_file.get_backup_wxid() 的形式

    def test_shape_assumptions_match_the_real_values(self):
        """先把形状钉住（真实值：24 字符 / 去尾段 19 字符 / 含 2 个下划线）。"""
        assert len(self.BARE) == 19
        assert len(self.DIRNAME) == 24
        assert self.DIRNAME.count('_') == 2
        assert self.DIRNAME.startswith('wxid_')

    @pytest.fixture
    def self_index(self, tmp_path):
        """本人消息的 `sender_username` 是**裸 wxid**（镜像真实索引）。

        Name2Id：rowid 1 = wxid_peer（对方）、rowid 2 = 裸 wxid（本人）→
        真实数据里正是如此：`msg_meta` 有 222,012 行 `sender_username == 裸 wxid`。
        消息：1) 我发的文本 '维修服务器'  2) 对方发的文本 '维修空调'  3) 我发的图片（无正文）
        """
        d = tmp_path / 'selfform'
        (d / 'message').mkdir(parents=True)
        _make_shard(str(d / 'message' / 'message_0.db'), [
            ('wxid_peer', [
                (1, 1, T0, 2),          # real_sender_id=2 → 本人
                (2, 1, T0 + 1, 1),      # 对方
                (3, 3, T0 + 2, 2),      # 本人发的图片
            ]),
        ])
        # Name2Id 的 rowid 顺序由 _make_shard 按 chats 顺序决定；这里再补一行让 rowid 2 = 本人
        con = sqlite3.connect(str(d / 'message' / 'message_0.db'))
        con.execute("UPDATE Name2Id SET user_name = ? WHERE rowid = 1", ('wxid_peer',))
        con.execute("INSERT INTO Name2Id (user_name) VALUES (?)", (self.BARE,))
        con.commit()
        con.close()
        _make_fts_db(str(d / 'message' / 'message_fts.db'), [
            ('维修服务器', 1, T0 * 1000, 1, 1, 1, T0),
            ('维修空调', 2, (T0 + 1) * 1000, 1, 1, 1, T0 + 1),
        ], usernames=('wxid_peer',))
        si.build_index(d)
        return d

    def test_fixture_stores_the_bare_form(self, self_index):
        """夹具的真实性：索引里本人消息的 sender_username 是**裸 wxid**，而不是目录名。"""
        con = sqlite3.connect(si.index_path(self_index))
        try:
            bare = con.execute('SELECT COUNT(*) FROM msg_meta WHERE sender_username = ?',
                               (self.BARE,)).fetchone()[0]
            dirname = con.execute('SELECT COUNT(*) FROM msg_meta WHERE sender_username = ?',
                                  (self.DIRNAME,)).fetchone()[0]
        finally:
            con.close()
        assert bare == 2, '本人两条消息（含图片）都应以裸 wxid 记录'
        assert dirname == 0, '目录名形式在索引里不存在（这正是 #17 的根因）'

    @pytest.mark.parametrize('value,expected', [
        # 账号目录名 → 裸 wxid
        ('wxid_abcdefghijklm_10e8', 'wxid_abcdefghijklm'),
        ('wxid_a_b_10E8', 'wxid_a_b'),          # 大写十六进制也认
        ('wxid_a_b_0000', 'wxid_a_b'),
        # 已经是裸 wxid / 不符合形状 → **原样返回**（不乱截）
        ('wxid_abcdefghijklm', 'wxid_abcdefghijklm'),
        ('wxid_abc', 'wxid_abc'),               # 只有 1 个下划线
        ('wxid_a_b_10e', 'wxid_a_b_10e'),       # 尾段 3 字符
        ('wxid_a_b_10e8x', 'wxid_a_b_10e8x'),   # 尾段 5 字符
        ('wxid_a_b_zzzz', 'wxid_a_b_zzzz'),     # 尾段不是十六进制
        ('customuser', 'customuser'),
        ('', ''),
        (None, ''),
    ])
    def test_bare_wxid_normalization(self, value, expected):
        assert s._bare_wxid(value) == expected

    def test_realistic_pair_normalizes(self):
        """用与真实值同形状的一对值验证判据。"""
        assert s._bare_wxid(self.DIRNAME) == self.BARE
        assert s._bare_wxid(self.BARE) == self.BARE

    def test_both_forms_find_the_same_rows(self, self_index):
        """**主测试**：目录名形式与裸 wxid 形式必须返回**同一组行**。

        当前代码下目录名形式匹配 0 行（RED）；修复后两者一致。
        """
        with_bare = s.search_messages(self_index, '发送者:我 维修', own_wxid=self.BARE)
        with_dir = s.search_messages(self_index, '发送者:我 维修', own_wxid=self.DIRNAME)
        assert _ids(with_bare) == [('wxid_peer', 1)], '本人那条应当命中'
        assert _ids(with_dir) == _ids(with_bare), (
            '目录名形式没被规范化：dir=%s bare=%s'
            % (_ids(with_dir), _ids(with_bare)))
        assert with_dir['total'] == with_bare['total'] == 1

    def test_me_returns_my_messages_with_either_form(self, self_index):
        """`发送者:我`（无关键词）在两种形式下都返回本人的消息（含无正文的图片）。"""
        for own in (self.BARE, self.DIRNAME):
            res = s.search_messages(self_index, '发送者:我', own_wxid=own)
            assert _id_set(res) == {('wxid_peer', 1), ('wxid_peer', 3)}, (own, _ids(res))

    def test_scope_accepts_both_forms(self, self_index):
        """设计选择：**同时**接受原值与规范值（`IN` 多一个不存在的值是无害的）。"""
        sc = s._resolve_scope(self_index, sq.parse_query('发送者:我'), self.DIRNAME)
        assert sc['senders'] == {self.DIRNAME, self.BARE}
        sc2 = s._resolve_scope(self_index, sq.parse_query('发送者:我'), self.BARE)
        assert sc2['senders'] == {self.BARE}, '已经是裸 wxid 时不该多出任何东西'

    def test_no_sender_constraint_without_the_token(self, self_index):
        """与 #16 一致：查询里没有 `发送者:` 时，两种形式都不得注入任何 sender 约束。"""
        for own in (self.BARE, self.DIRNAME):
            sc = s._resolve_scope(self_index, sq.parse_query('维修'), own)
            assert sc['senders'] == set(), own

    def test_me_does_not_warn_when_the_self_sender_is_present(self, self_index):
        """反向用例：索引里**有**本人行时，绝不能出现"找不到本人身份"的提示。

        用**目录名形式**传 `own_wxid` —— 这一条同时验证兜底探测也走规范化：
        若探测只看原值，就会误报（而真实数据里目录名一行都匹配不上）。
        """
        res = s.search_messages(self_index, '发送者:我 维修', own_wxid=self.DIRNAME)
        assert res['total'] == 1
        assert not any('本人' in w for w in res['warnings']), res['warnings']


# ==========================================================================
# 15. 🔴 Critical #18：带空格的**短语**在降级路径整类归零
# ==========================================================================

class TestPhraseWhitespaceSemantics:
    """索引路径的短语语义 = 「**折叠成连续 token 序列**」，降级路径必须同义。

    真实数据实测（修复前）：`"服务器 宕机"` 索引 73 ／ 降级 **0**（整类查询归零）。

    两条路径的权威判定都在 Python（`_fallback_keep` 的 `folded_phrases`），
    SQL 只负责**不丢真命中**的预筛。预筛的正确性要求是「**严格超集**」——
    见 `TestPhrasePrefilterIsASuperSet` 的穷举证明。
    """

    # 折叠后都包含 '服务器宕机' 的五种形态 + 两条对照
    RAW_MIXED = '维修服务器宕机'      # 同时含关键词「维修」
    RAW_SPACED = '服务器 宕机'        # 短语里带空白 ↔ 原文里也带空白
    RAW_NEWLINE = '服务器\n宕机'      # 换行
    RAW_PUNCT = '服务器，宕机'        # 标点（unicode61 当分隔符丢掉 → 索引侧命中）
    RAW_SPLIT = '服务 器宕机'         # 空白在**词内部**
    RAW_BAOXIU = '报修电话'           # 只含「报修」（反向用例）
    RAW_OTHER = '服务器风扇'          # 只含「服务器」（对照）

    ROWS = None      # 由 _rows() 生成

    @classmethod
    def _rows(cls):
        return [(cls.RAW_MIXED, 1), (cls.RAW_SPACED, 2), (cls.RAW_NEWLINE, 3),
                (cls.RAW_PUNCT, 4), (cls.RAW_SPLIT, 5), (cls.RAW_BAOXIU, 6),
                (cls.RAW_OTHER, 7)]

    @pytest.fixture
    def phrase_dir(self, tmp_path):
        d = tmp_path / 'phrase'
        (d / 'message').mkdir(parents=True)
        _make_shard(str(d / 'message' / 'message_0.db'), [
            ('wxid_alpha', [(lid, 1, T0 + lid, 1) for _raw, lid in self._rows()]),
        ])
        _make_fts_db(str(d / 'message' / 'message_fts.db'),
                     [(raw, lid, (T0 + lid) * 1000, 1, 1, 1, T0 + lid)
                      for raw, lid in self._rows()], usernames=('wxid_alpha',))
        si.build_index(d)
        return d

    def test_indexed_path_matches_all_folded_forms(self, phrase_dir):
        """先把索引路径的语义钉住（五种形态全中），后面的对照才有意义。"""
        res = s.search_messages(phrase_dir, '"服务器 宕机"')
        assert res['total'] == 5, _ids(res)
        assert _id_set(res) == {('wxid_alpha', i) for i in (1, 2, 3, 4, 5)}
        assert res['used_fallback'] is False

    def test_phrase_with_inner_space_is_not_zero_on_the_fallback(self, phrase_dir):
        """**主测试**：两路径必须返回同样的 5 条（修复前降级只有 1 条）。

        覆盖控制方要求的三条（无空白 / 空格 / 换行），外加**词内部空白**与**标点**两种
        —— 后两种分别证明「逐字符超集预筛」与「折叠确认（而非仅去空白）」是必需的：
          `服务 器宕机`  → 字面 `LIKE %服务器%` 类预筛会漏
          `服务器，宕机` → 只去空白的确认会漏（标点没被去掉）
        """
        indexed, fallback = _both_paths(phrase_dir, '"服务器 宕机"')
        assert indexed['total'] == 5, '样本必须非空，否则这条测试是空转'
        assert fallback['total'] == 5, _ids(fallback)

    def test_single_word_phrase_without_space_still_works(self, phrase_dir):
        """反向用例（控制方第 3 条要求）：不带空格的短语不能被弄坏。

        `"报修"` + `"服务器"` 两条，其中 `服务器` 还要**跨越** `服务 器宕机` 里的空白。
        """
        indexed, fallback = _both_paths(phrase_dir, '"报修"')
        assert indexed['total'] == 1, _ids(indexed)
        assert fallback['total'] == 1
        indexed2, fallback2 = _both_paths(phrase_dir, '"服务器"')
        assert indexed2['total'] == 6, _ids(indexed2)
        assert fallback2['total'] == 6, _ids(fallback2)

    def test_phrase_and_keyword_are_anded(self, phrase_dir):
        """混合用例：`维修 "服务器 宕机"` 两路径一致（关键词预筛 + 短语确认）。"""
        indexed, fallback = _both_paths(phrase_dir, '维修 "服务器 宕机"')
        assert indexed['total'] == 1, _ids(indexed)
        assert fallback['total'] == 1, _ids(fallback)

    def test_phrase_highlight_covers_the_whitespace_forms(self, phrase_dir):
        """命中要**有高亮**：折叠命中的那几条也必须给出正确的区间（见 §15.4）。

        每个命中行的区间切出来的片段，折叠后必须恰好等于短语的折叠形式 —— 这正是
        "命中却没有高亮"那个显示层缺陷的判别。
        """
        indexed, fallback = _both_paths(phrase_dir, '"服务器 宕机"')
        for res in (indexed, fallback):
            assert res['total'] == 5
            for hit in res['results']:
                assert hit['match_spans'], (hit['local_id'], hit['snippet'])
                start, end = hit['match_spans'][0]
                assert s._fold_text(hit['snippet'][start:end]) == '服务器宕机', (
                    hit['local_id'], hit['snippet'], hit['match_spans'])

    def test_fold_matches_the_char_phrase_token_stream(self):
        """`_fold_text` 必须等于 `char_phrase()` **token 序列**的字母数字投影。

        索引路径的短语语义就建立在它上面（`unicode61` 丢标点、char-token 丢空白）；
        两者一旦分叉，等价性会再次静默失效。
        """
        for phrase in ('服务器 宕机', '宕机。', 'Order A1234', '"A.B"', '服务 器宕机'):
            tokens = sq.char_phrase(phrase).strip('"').split(' ')
            assert s._fold_text(phrase) == ''.join(t for t in tokens if t.isalnum()), phrase

    @pytest.mark.parametrize('value,expected', [
        ('服务器 宕机', '服务器宕机'),
        ('服务 器宕机', '服务器宕机'),
        ('服务器，宕机', '服务器宕机'),      # 标点也被丢掉（unicode61 的行为）
        ('Order A1234', 'ordera1234'),
        ('  宕机\t\n', '宕机'),
        ('', ''), ('   ', ''), ('。，！', ''),
    ])
    def test_fold_text_examples(self, value, expected):
        assert s._fold_text(value) == expected

    def test_punctuation_only_phrase_yields_no_rows_not_everything(self, phrase_dir):
        """纯标点短语没有任何 token → 必须 0 行，绝不能退化成"匹配全部"。"""
        assert s.search_messages(phrase_dir, '"。"')['total'] == 0
        with _index_hidden(phrase_dir):
            assert s.search_messages(phrase_dir, '"。"')['total'] == 0

    def test_phrase_with_trailing_punctuation_matches_both_paths(self, phrase_dir):
        """短语里带标点（`"宕机。"`）两路径同义（五条形态都含「宕机」）。

        `unicode61` 把标点当分隔符丢掉 → 索引侧等价于"去掉非字母数字后的连续序列"，
        降级侧同样按**折叠后**确认（只去空白会漏掉 `服务器，宕机` 那条）。
        """
        indexed, fallback = _both_paths(phrase_dir, '"宕机。"')
        assert indexed['total'] == 5, _ids(indexed)
        assert fallback['total'] == 5, _ids(fallback)

    def test_keyword_span_bridging_whitespace_now_matches_on_both_paths(self, phrase_dir):
        """**控制方 §15.5 裁决**：关键词也收紧到 char-token 语义 —— 差异不再被接受。

        原状（已撤回）：关键词 `服务器` 走字面 `LIKE`，在 `服务 器宕机`（字面被空白切断）上
        索引命中、降级不命中，当时被列为"已知且接受"的差异；理由（"要一致得放弃 SQL 预筛、
        代价 7–13 倍"）只对"全量搬 Python"那条路成立 —— 用同一套逐字符超集 + 折叠确认，
        实测 0.52s → 0.50s、且两路径逐行相等，所以裁决改为**收紧**。
        """
        indexed, fallback = _both_paths(phrase_dir, '服务器')
        assert indexed['total'] == 6, _ids(indexed)
        assert fallback['total'] == 6, _ids(fallback)
        assert ('wxid_alpha', 5) in _id_set(indexed)
        assert ('wxid_alpha', 5) in _id_set(fallback)      # `服务 器宕机` 两边都要有


class TestKeywordTokenSemantics:
    """§15.5：关键词 / OR 成员与短语**同一套**收紧（超集预筛 + 折叠确认）。

    真实数据验收（收紧后两路径逐行相等）：`维修` **1389/1389**、`维修 OR 报修` 1815/1815、
    `维修 -退货` 相等、`服务 器宕机` 相等（见报告 §15.7）。
    """

    ROWS = [('服务器宕机', 1), ('服务器 宕机', 2), ('服务 器宕机', 3), ('报修电话', 4)]

    @pytest.fixture
    def kw_dir(self, tmp_path):
        """控制方指定的夹具：三条 `服务器`（含**词内部空白**那条）+ 一条真反例。

        反例用 `报修电话`（**不含**「服务器」）—— 若沿用短语那条用的 `服务器风扇`，
        它**字面就含「服务器」**，因此对关键词而言是**正例**不是反例（总数会变 4）。
        """
        d = tmp_path / 'kw'
        (d / 'message').mkdir(parents=True)
        _make_shard(str(d / 'message' / 'message_0.db'),
                    [('wxid_alpha', [(lid, 1, T0 + lid, 1) for _r, lid in self.ROWS])])
        _make_fts_db(str(d / 'message' / 'message_fts.db'),
                     [(r, lid, (T0 + lid) * 1000, 1, 1, 1, T0 + lid)
                      for r, lid in self.ROWS], usernames=('wxid_alpha',))
        si.build_index(d)
        return d

    def test_indexed_path_matches_all_three_forms(self, kw_dir):
        """先钉住索引路径 == 3，才能排除"两路径都是 0"的空转通过。"""
        res = s.search_messages(kw_dir, '服务器')
        assert res['total'] == 3, _ids(res)
        assert res['used_fallback'] is False

    def test_keyword_matches_across_inner_whitespace_on_both_paths(self, kw_dir):
        """**主测试**：关键词 `服务器` 两路径都必须命中三条（含 `服务 器宕机`）。"""
        indexed, fallback = _both_paths(kw_dir, '服务器')
        assert indexed['total'] == 3, '样本必须非空'
        assert fallback['total'] == 3, _ids(fallback)
        assert ('wxid_alpha', 3) in _id_set(fallback)

    def test_keyword_and_keyword_still_ands(self, kw_dir):
        """跨关键词仍是 AND（收紧后不能变成 OR）。"""
        both = _both_paths(kw_dir, '服务器 宕机')
        assert both[0]['total'] == 3, _ids(both[0])       # 四条里三条同时含「服务器」「宕机」
        none = _both_paths(kw_dir, '服务器 报修')
        assert none[0]['total'] == 0
        assert none[1]['total'] == 0

    @pytest.fixture
    def or_dir(self, tmp_path):
        """OR 组的夹具：一条连写、一条**词内部空白**、一条只含另一个成员。"""
        rows = [('服务器宕机', 1), ('服务 器宕机', 2), ('报修电话', 3)]
        d = tmp_path / 'orfix'
        (d / 'message').mkdir(parents=True)
        _make_shard(str(d / 'message' / 'message_0.db'),
                    [('wxid_alpha', [(lid, 1, T0 + lid, 1) for _r, lid in rows])])
        _make_fts_db(str(d / 'message' / 'message_fts.db'),
                     [(r, lid, (T0 + lid) * 1000, 1, 1, 1, T0 + lid)
                      for r, lid in rows], usernames=('wxid_alpha',))
        si.build_index(d)
        return d

    def test_or_group_is_still_or_on_both_paths(self, or_dir):
        """OR 组内部仍是 OR，且成员被空白切断时也要两边一致。

        `服务器 OR 报修`：`服务器` 命中 1、2（第 2 条**字面**被空白切断），`报修` 命中 3
        → 合计 **3** 条。若把 OR 平铺成 AND，会只剩 0 条；若成员仍用字面预筛，会只剩 2 条。
        """
        indexed, fallback = _both_paths(or_dir, '服务器 OR 报修')
        assert indexed['total'] == 3, _ids(indexed)
        assert fallback['total'] == 3, _ids(fallback)
        assert ('wxid_alpha', 2) in _id_set(fallback)

    def test_keyword_with_exclude_is_unchanged_scope(self, kw_dir):
        """排除词本轮**不动**：两条路径都在 `_apply_excludes` 里按**原文子串**做，彼此一致。

        （已知偏移：`_apply_excludes` 用的是原文子串语义，而关键词匹配现在是 token 语义 ——
        被空白/标点切断的形式在排除词上仍然"不算命中"。但这是**两条路径共有**的行为，
        不是路径之间的差异，所以按裁决本轮不动。）
        """
        indexed, fallback = _both_paths(kw_dir, '服务器 -电话')
        assert indexed['total'] == 3, _ids(indexed)
        assert fallback['total'] == 3, _ids(fallback)

    def test_folded_pattern_superset_holds_for_keywords_too(self):
        """关键词的预筛同样是**严格超集**（与短语同一把尺子、同一份穷举证明）。"""
        pattern = s._folded_like_pattern('服务器')
        assert pattern == '%服%务%器%'
        con = sqlite3.connect(':memory:')
        try:
            con.execute('CREATE TABLE t (c0 TEXT)')
            variants = ['服务器宕机', '服务 器宕机', '服\t务 器', '服务，器', '服务器']
            con.executemany('INSERT INTO t (c0) VALUES (?)', [(v,) for v in variants])
            matched = {r[0] for r in con.execute('SELECT c0 FROM t WHERE c0 LIKE ?', (pattern,))}
        finally:
            con.close()
        true_hits = {v for v in variants if '服务器' in s._fold_text(v)}
        assert true_hits == set(variants)
        assert not (true_hits - matched), sorted(true_hits - matched)


class TestPhrasePrefilterIsASuperSet:
    """证明：短语的 SQL 预筛**严格是超集**，所以 Python 确认永远不会"捡不回来"。

    控制方要求"不许用可能漏的预筛"。这里不做论证式声明，而是**穷举验证**：
    把 `服务器宕机` 的每个字符间隙用 `['', ' ', '\\t', '\\n', '，', '  ']` 的任意组合
    填满（6^4 = 1296 种变体），对**每一个**"折叠后包含短语"的变体断言：
    `变体 LIKE 预筛模式` 在真实 SQLite 里必须为真。任何一个真命中被预筛丢掉，本测试立刻失败。

    这正是"逐字符 + `%` 间隔"模式的超集性来源：折叠命中 ⟹ 短语的字符在原文里**按序出现**
    ⟹ 每个 `%` 吸收字符之间的空白/标点 ⟹ LIKE 命中。
    （反例说明另一条路线为何不行：**字面**去空白模式 `%服务器宕机%` 要求字面相邻，
    变体 `服务器 宕机` 就漏了 —— 控制方最初的担心对那条路线成立。）
    """

    PHRASE = '服务器 宕机'
    GAPS = ['', ' ', '\t', '\n', '，', '  ']

    def test_gap_tolerant_pattern_covers_every_folded_variant(self):
        base = '服务器宕机'
        variants = set()
        for a in self.GAPS:
            for b in self.GAPS:
                for c in self.GAPS:
                    for d in self.GAPS:
                        variants.add(base[0] + a + base[1] + b + base[2] + c + base[3] + d + base[4])
        folded = s._fold_text(self.PHRASE)
        pattern = s._folded_like_pattern(self.PHRASE)
        assert pattern is not None

        con = sqlite3.connect(':memory:')
        try:
            con.execute('CREATE TABLE t (c0 TEXT)')
            con.executemany('INSERT INTO t (c0) VALUES (?)', [(v,) for v in variants])
            matched = {r[0] for r in con.execute(
                "SELECT c0 FROM t WHERE c0 LIKE ? ESCAPE '\\'", (pattern,))}
        finally:
            con.close()

        true_hits = {v for v in variants if folded in s._fold_text(v)}
        assert len(variants) == len(self.GAPS) ** 4          # 穷举，不是抽样
        assert len(true_hits) > 100, true_hits               # 样本必须有意义
        missed = true_hits - matched
        assert not missed, (
            '预筛漏掉真命中（不再是超集）：%r → %s' % (pattern, sorted(missed)[:5]))

    def test_literal_dewhitespaced_pattern_would_break_the_superset(self):
        """把"字面去空白"路线也测一遍：它**确实**会漏（记录证据，防止有人改回去）。"""
        literal = '%' + s._fold_text(self.PHRASE) + '%'
        con = sqlite3.connect(':memory:')
        try:
            con.execute('CREATE TABLE t (c0 TEXT)')
            con.executemany('INSERT INTO t (c0) VALUES (?)',
                            [(v,) for v in ('服务器宕机', '服务器 宕机', '服务器\n宕机')])
            seen = {r[0] for r in con.execute("SELECT c0 FROM t WHERE c0 LIKE ?", (literal,))}
        finally:
            con.close()
        assert seen == {'服务器宕机'}, seen      # 漏掉带空白/换行的两种 → 所以不能用它


# ==========================================================================
# 15. T13：索引路径「非 SQL 分页」形态的流式扫描（**语义必须逐条不变**）
# ==========================================================================
#
# 本轮是**纯性能改造**（把 `_scan_indexed` 从 `fetchall()` 全量物化改成逐行流式），
# 没有"新功能"可 RED。所以这里的 RED 形态是**行为锚**：
# 同一批查询的 `total` **与完整有序 `(chat_id, local_id)` 序列**逐条不变。
# 这些期望值一开始就是绿的（如实说明），判别力由 §变异测试证明 ——
# "只处理前 N 行"/"排除词只对前 N 行生效"/"漏掉最后一行"三种注入都会让它们变红。
#
# 期望值全部由**夹具规则手推**（见 `_stream_rows` 的注释），不是从实现里抄的。

_STREAM_CHAT = 'wxid_stream'
_STREAM_N = 60


def _stream_rows():
    """T13 长夹具的**唯一事实源**：60 行（local_id 1..60，create_time = T0 + i）。

      i % 7 == 0              → 文本 `zzz drop %03d`（命中排除词 `zzz`，共 8 行）
      i % 5 == 0（且非 7 的倍数）→ 图片（**无正文**：不得被排除词丢掉，共 11 行）
      其余                    → 文本 `keep %03d`（共 41 行）

    为什么要 60 行：本轮要改的正是"全量扫描"那条路，而典型缺陷是**截断/丢尾**
    （"只处理前 N 行"、"排除词只对前 N 行生效"、"漏掉最后一行"）。
    8 行的 `rich` 夹具上这些注入"活不下来也死不掉"（前 N 行就覆盖了全部行）。
    60 行 + 第 51..60 行里有 **9 条幸存行**、第 56 行是被排除行
    ⇒ 上述三种注入都必然改变 total 或整条序列（自检见
    `test_fixture_has_discriminating_power_for_truncation`）。
    """
    out = []
    for i in range(1, _STREAM_N + 1):
        if i % 7 == 0:
            out.append((i, 1, T0 + i, 'zzz drop %03d' % i))
        elif i % 5 == 0:
            out.append((i, 3, T0 + i, None))
        else:
            out.append((i, 1, T0 + i, 'keep %03d' % i))
    return out


def _not_zzz(i):
    """`-zzz` 的幸存行（正文里没有 'zzz'；无正文的图片行也幸存）。"""
    return i % 7 != 0


def _keep_row(i):
    """退化正则 `/(?=.*keep)/` 的幸存行（正文是 `keep %03d` 的那些）。"""
    return i % 7 != 0 and i % 5 != 0


def _stream_ids(pred):
    """夹具规则 → 默认排序（time_desc）下的**完整有序**序列。

    本夹具 create_time = T0 + i 互不相同 ⇒ time_desc 就是 i 降序，
    与 `search_messages` 的整形排序（`(create_time, local_id)` reverse）逐位相同。
    """
    return [(_STREAM_CHAT, i)
            for i in reversed([i for i in range(1, _STREAM_N + 1) if pred(i)])]


@pytest.fixture
def stream(tmp_path):
    """60 行合成目录 + 已建索引（T13 长夹具，规则见 `_stream_rows`）。"""
    d = tmp_path / 'stream'
    (d / 'message').mkdir(parents=True)
    rows = _stream_rows()
    _make_shard(str(d / 'message' / 'message_0.db'), [
        (_STREAM_CHAT, [(i, lt, ct, 1) for i, lt, ct, _ in rows])])
    _make_fts_db(str(d / 'message' / 'message_fts.db'),
                 [(txt, i, ct * 1000, 1, 1, 1, ct)
                  for i, lt, ct, txt in rows if txt],
                 usernames=(_STREAM_CHAT,))
    si.build_index(str(d))
    return str(d)


class _NoFullFetchConn:
    """**`LIMIT` / `COUNT(*)` 之外的 SQL 不许整批 `fetchall()`**。

    钉住 T13 的核心形状：非 SQL 分页的形态必须**流式逐行**扫描。
    一旦退回"先把候选行全量搬进 Python 再切片"，`fetchall` 在这里就被抓个正着 ——
    真实数据上那正是 1,018,918 行 × 6 列（含 877,837 条原文 str）物化的来源。
    """

    def __init__(self, conn):
        self._conn = conn
        self.bulk = []          # 记录"不该整批取"的 SQL 前缀

    def execute(self, sql, params=()):
        cur = self._conn.execute(sql, params)
        outer = self

        class _Cur:
            def fetchone(self):
                return cur.fetchone()

            def fetchall(self):
                if 'LIMIT' not in sql and 'COUNT(*)' not in sql:
                    outer.bulk.append(sql[:24])
                return cur.fetchall()

            def __iter__(self):
                while True:
                    row = cur.fetchone()
                    if row is None:
                        return
                    yield row

        return _Cur()

    def close(self):
        self._conn.close()


class TestStreamingScanIsNotMaterialised:
    def test_scan_path_is_streamed_not_materialised(self, stream, monkeypatch):
        """含排除词的查询必须**逐行**扫描（有 `candidate_rows` 但要能算对）。"""
        real_open = s.search_index.open_index
        holder = {}

        def spy(decrypted_dir):
            holder['conn'] = _NoFullFetchConn(real_open(decrypted_dir))
            return holder['conn']

        monkeypatch.setattr(s.search_index, 'open_index', spy)
        res = s.search_messages(stream, '-zzz', per_page=s.MAX_PER_PAGE)
        assert res['total'] == 52
        assert res['candidate_rows'] == _STREAM_N
        assert holder['conn'].bulk == [], (
            '非 SQL 分页的扫描路径做了整批 fetchall（应逐行流式）：%s'
            % holder['conn'].bulk)

    def test_degraded_regex_path_is_streamed_too(self, stream, monkeypatch):
        """退化正则（`scan_mode='filter'`，1M 行级全量扫描）同样不许整批物化。"""
        real_open = s.search_index.open_index
        holder = {}

        def spy(decrypted_dir):
            holder['conn'] = _NoFullFetchConn(real_open(decrypted_dir))
            return holder['conn']

        monkeypatch.setattr(s.search_index, 'open_index', spy)
        res = s.search_messages(stream, '/(?=.*keep)/', per_page=s.MAX_PER_PAGE)
        assert res['scan_mode'] == 'filter'
        assert res['regex_degraded'] is True
        assert res['candidate_rows'] == _STREAM_N
        assert res['total'] == 41
        assert holder['conn'].bulk == []

    def test_sql_paged_path_still_batches_the_page_only(self, stream, monkeypatch):
        """无正则无排除词的形态**不变**：仍是 SQL 侧 `LIMIT` 分页（只取当页）。"""
        real_open = s.search_index.open_index
        holder = {}

        def spy(decrypted_dir):
            holder['conn'] = _NoFullFetchConn(real_open(decrypted_dir))
            return holder['conn']

        monkeypatch.setattr(s.search_index, 'open_index', spy)
        res = s.search_messages(stream, '类型:文本', per_page=7, page=2)
        assert res['total'] == 49                    # 60 − 11 张无正文图片
        assert len(res['results']) == 7
        assert holder['conn'].bulk == []


class TestStreamingScanEquivalence:
    """**完整有序序列**的锚（不是"只看首页 50 条"）。"""

    def test_fixture_has_discriminating_power_for_truncation(self, stream):
        """夹具自检：先证明"开关真的被拨动了"（R29 的教训）。

        「只处理前 50 行」/「漏掉最后一行」/「排除词只对前 50 行生效」要能被抓住，
        前提是**第 51..60 行里既有幸存行、也有被排除行**。这里把前提显式断言出来，
        免得将来改夹具（例如缩短行数）把判别力悄悄拿掉。
        """
        rows = _stream_rows()
        assert len(rows) == _STREAM_N == 60
        tail = [(i, txt) for i, _, _, txt in rows if i > 50]
        assert len(tail) == 10
        survivors = [i for i, txt in tail if txt is None or 'zzz' not in txt]
        assert survivors == [51, 52, 53, 54, 55, 57, 58, 59, 60]     # 尾段有 9 条幸存行
        assert [i for i, txt in tail if txt and 'zzz' in txt] == [56]  # 且还有被排除行
        assert rows[-1][3] is None                       # 最后一行是幸存行（图片）

    def test_pure_exclude_full_ordered_sequence(self, stream):
        """纯排除词（`scan_mode='filter'`，候选 = 语料全量）：total 与**整条序列**。"""
        res = s.search_messages(stream, '-zzz', per_page=s.MAX_PER_PAGE)
        assert res['scan_mode'] == 'filter'
        assert res['used_fallback'] is False
        assert res['candidate_rows'] == _STREAM_N == 60
        assert res['total'] == 52
        assert _ids(res) == _stream_ids(_not_zzz)

    def test_pure_exclude_sequence_under_every_sort(self, stream):
        for sort, want in (('time_desc', _stream_ids(_not_zzz)),
                           ('time_asc', list(reversed(_stream_ids(_not_zzz)))),
                           ('chat', list(reversed(_stream_ids(_not_zzz))))):
            res = s.search_messages(stream, '-zzz', per_page=s.MAX_PER_PAGE, sort=sort)
            assert _ids(res) == want, sort

    def test_degraded_regex_full_ordered_sequence(self, stream):
        res = s.search_messages(stream, '/(?=.*keep)/', per_page=s.MAX_PER_PAGE)
        assert res['scan_mode'] == 'filter'
        assert res['regex_degraded'] is True
        assert res['candidate_rows'] == 60
        assert res['total'] == 41
        assert _ids(res) == _stream_ids(_keep_row)

    def test_regex_plus_exclude_mixed_sequence(self, stream):
        """混合形态：正则确认与排除词**在同一次流式扫描里**逐行都生效。

        排除词 `003` 只出现在 i=3 的正文 `keep 003` 里（其余行的编号不含 `003`）
        ⇒ 41 − 1 = 40。这条同时钉住"两个判定都在流式循环内"（少一个就多给一行）。
        """
        res = s.search_messages(stream, '/(?=.*keep)/ -003', per_page=s.MAX_PER_PAGE)
        assert res['candidate_rows'] == 60
        assert res['total'] == 40
        assert _ids(res) == _stream_ids(lambda i: _keep_row(i) and i != 3)

    def test_keyword_plus_exclude_candidates_come_from_fts_prefilter(self, stream):
        """关键词 + 排除词：候选行数来自 **FTS 预筛**（41），不是语料全量。"""
        res = s.search_messages(stream, 'keep -003', per_page=s.MAX_PER_PAGE)
        assert res['scan_mode'] == 'fts'
        assert res['candidate_rows'] == 41
        assert res['total'] == 40
        assert _ids(res) == _stream_ids(lambda i: _keep_row(i) and i != 3)

    def test_candidate_rows_is_the_pre_confirmation_count(self, rich):
        """`candidate_rows` = **确认之前**的候选行数（UI 的"扫描规模"提示）。

        `-维修` 在 `rich` 上：候选 8（msg_meta 全量），幸存 7（alpha/4 的正文含"维修"）。
        流式改造最容易在这里犯错 —— 把 `len(幸存行)` 当成候选数上报（那样 Hint 会说谎）。
        """
        res = s.search_messages(rich, '-维修')
        assert res['candidate_rows'] == 8
        assert res['total'] == 7
        assert ('wxid_alpha', 4) not in _id_set(res)

    def test_scan_indexed_splits_candidates_from_survivors(self, rich):
        """内部契约：`_scan_indexed` 同时给出**候选计数**与**幸存行**（升序、6 元组）。"""
        con = si.open_index(rich)
        try:
            parsed = sq.parse_query('类型:文本 -维修')
            scope = s._resolve_scope(rich, parsed, None)
            cand, rows = s._scan_indexed(con, scope, None, [],
                                         s._exclude_terms(parsed))
        finally:
            con.close()
        assert cand == 5                      # 类型=文本 的 5 行（含将被排除的那一行）
        assert [(r[0], r[1]) for r in rows] == [
            ('wxid_alpha', 1), ('wxid_alpha', 3), ('111@chatroom', 1), ('wxid_beta', 11)]
        assert all(len(r) == 6 for r in rows)
        assert all(r[5] != RAW_FIX for r in rows)      # 含"维修"的那行已按原文排除

    def test_uncompilable_regex_is_defensively_ignored_not_fatal(self, rich):
        """直传非法正则：**不抛**、且不做正则过滤（与改造前 `_confirm_regexes` 一致）。"""
        con = si.open_index(rich)
        try:
            scope = s._resolve_scope(rich, sq.parse_query('类型:文本'), None)
            cand, rows = s._scan_indexed(con, scope, None, ['('])
        finally:
            con.close()
        assert cand == 5
        assert len(rows) == 5

    def test_exclude_terms_normalisation_is_shared(self, rich):
        """排除词的规范化只有一处定义（`_exclude_terms`）：strip + lower。"""
        assert s._exclude_terms({'exclude': [' 退货 ', 'ABC']}) == ['退货', 'abc']
        assert s._exclude_terms({'exclude': []}) == []
        assert s._exclude_terms({}) == []
        assert s._exclude_terms({'exclude': ['   ']}) == []

    def test_apply_excludes_keeps_its_batch_contract(self, rich):
        """`_apply_excludes` 的旧契约不许变：无排除词**原样返回同一个 list 对象**。"""
        rows = [('wxid_alpha', 1, 1, T0, 'wxid_alpha', RAW_ORDER),
                ('wxid_alpha', 3, 1, T0 + 200, 'wxid_alpha', RAW_REPAIR)]
        assert s._apply_excludes(rows, {}) is rows
        assert s._apply_excludes(rows, {'exclude': []}) is rows
        kept = s._apply_excludes(rows, {'exclude': ['报修']})
        assert [r[1] for r in kept] == [1]

    def test_scan_indexed_returns_ok_when_scope_is_empty(self, rich):
        """标签上没有任何会话 → 候选 0 / 幸存 0（与旧实现的 `[]` 一致，不是异常）。"""
        con = si.open_index(rich)
        try:
            scope = s._resolve_scope(rich, sq.parse_query('标签:nope'), None)
            assert scope['label_chats'] == set()
            cand, rows = s._scan_indexed(con, scope, None, [])
        finally:
            con.close()
        assert (cand, rows) == (0, [])


# ==========================================================================
# 16. T16：**显式截断**（保留窗口）—— 计数精确，只截"保留的幸存行条数"
# ==========================================================================
#
# 契约（控制方 R39，逐字）：
#   * `candidate_rows` 与 `total` **照旧精确**（绝对不许为了让数字好看而截断 total）；
#   * 只有"保留下来用于分页的幸存行"被限制在 `MAX_RETAINED_ROWS` 条以内；
#   * 结构化字段 `truncated` / `retained_rows` 与人话 warning **必须同时出现**；
#   * **未截断时不得出现任何截断文案**（防误报）；
#   * SQL 分页路径（`_page_indexed`，纯筛选 / 关键词）**不受影响**，仍可完整翻页；
#   * 分页语义：`total_pages` 仍按精确 total；页起点 ≥ 保留条数 → 空 rows（页面据此
#     显示"超出保留范围"，而不是"没有匹配的消息"）。
#
# 判别性来自**小夹具 + 注入上限**（`search_messages(..., max_retained=N)`）：
# 绝不用"构造 5 万行"或"猴子补丁改常量"来测 —— 前者慢到没人跑，后者测的是补丁本身。
# 期望值全部由 `_stream_rows` / `_make_rich_dir` 的规则手推（见 §15 与 `_make_rich_dir`）。

# `-zzz` 在 `stream` 上：候选 60（语料全量）、幸存 52（`_not_zzz`）
_TRUNC_QUERY = '-zzz'
_TRUNC_TOTAL = 52
_TRUNC_CAND = _STREAM_N          # 60

# 未截断的基准：上限取到语料规模之上（= 恒不截断），用来拿"精确 total"
_NO_CAP = 10 ** 9


def _truncation_warnings(res):
    """`warnings` 里的截断文案（判定用子串，避免把整句抄成第二份事实源）。"""
    return [w for w in (res.get('warnings') or []) if '只保留了前' in w]


class TestTruncationKeepsCountsExact:
    """**本轮最关键的不变式**：`total` / `candidate_rows` 精确，只有保留行数被截。"""

    def test_response_always_carries_the_structured_fields(self, rich):
        """没超上限的查询也必须带结构化字段（否则调用方只能去解析人话）。"""
        res = s.search_messages(rich, '-维修')
        assert res['truncated'] is False
        assert res['retained_rows'] == res['total'] == 7

    def test_cap_truncates_retained_rows_only(self, stream):
        """上限 10 < 幸存 52 ⇒ `truncated=True` / `retained_rows==10`，计数**不变**。"""
        res = s.search_messages(stream, _TRUNC_QUERY, max_retained=10,
                                per_page=s.MAX_PER_PAGE)
        assert res['truncated'] is True
        assert res['retained_rows'] == 10
        assert res['total'] == _TRUNC_TOTAL, 'total 被截断污染了（只允许截"保留行数"）'
        assert res['candidate_rows'] == _TRUNC_CAND, 'candidate_rows 被截断污染了'
        assert len(res['results']) == 10

    def test_total_is_bit_for_bit_the_uncapped_total(self, stream):
        """**判别性**：同一条查询，截断与不截断的 `total` 必须逐位相同。"""
        capped = s.search_messages(stream, _TRUNC_QUERY, max_retained=10)
        uncapped = s.search_messages(stream, _TRUNC_QUERY, max_retained=_NO_CAP)
        assert (capped['total'], capped['candidate_rows']) == \
               (uncapped['total'], uncapped['candidate_rows']) == (_TRUNC_TOTAL, _TRUNC_CAND)
        assert capped['retained_rows'] == 10 and uncapped['retained_rows'] == _TRUNC_TOTAL

    def test_total_pages_is_computed_from_the_exact_total(self, stream):
        """`total_pages` 按**精确 total** 算（截断不许把它改小）。"""
        res = s.search_messages(stream, _TRUNC_QUERY, max_retained=10, per_page=4)
        assert res['total'] == _TRUNC_TOTAL
        assert res['total_pages'] == 13          # ceil(52/4)，而不是 ceil(10/4)=3

    def test_retained_window_keeps_the_earliest_rows(self, stream):
        """保留的是 SQL 顺序（`create_time, local_id` 升序）里**最早的 N 条**。

        夹具 create_time = T0 + i 且互不相同 ⇒ 升序 = i 升序；
        `_not_zzz` 的前 10 个幸存行 = [1,2,3,4,5,6,8,9,10,11]。
        """
        earliest = [i for i in range(1, _STREAM_N + 1) if _not_zzz(i)][:10]
        assert earliest == [1, 2, 3, 4, 5, 6, 8, 9, 10, 11]
        asc = s.search_messages(stream, _TRUNC_QUERY, max_retained=10, sort='time_asc',
                                per_page=s.MAX_PER_PAGE)
        assert _ids(asc) == [(_STREAM_CHAT, i) for i in earliest]
        desc = s.search_messages(stream, _TRUNC_QUERY, max_retained=10, sort='time_desc',
                                 per_page=s.MAX_PER_PAGE)
        assert _ids(desc) == [(_STREAM_CHAT, i) for i in reversed(earliest)]


class TestTruncationPaginationBoundaries:
    """分页边界：第 `ceil(N/per_page)` 页**有**数据，下一页**空但 `truncated=True`**。"""

    def test_last_page_inside_the_window_has_rows(self, stream):
        res = s.search_messages(stream, _TRUNC_QUERY, max_retained=10, per_page=4, page=3)
        assert res['truncated'] is True
        assert res['retained_rows'] == 10 and res['total'] == _TRUNC_TOTAL
        # 保留窗口里按 time_desc 是 [11,10,9,8,6,5,4,3,2,1]；第 3 页 = 最后 2 条
        assert _ids(res) == [(_STREAM_CHAT, 2), (_STREAM_CHAT, 1)]

    def test_first_page_beyond_the_window_is_empty_but_still_truncated(self, stream):
        res = s.search_messages(stream, _TRUNC_QUERY, max_retained=10, per_page=4, page=4)
        assert res['results'] == []
        assert res['truncated'] is True, '空页必须能自证"超出保留范围"，不能只有空列表'
        assert res['retained_rows'] == 10
        assert res['total'] == _TRUNC_TOTAL and res['total_pages'] == 13

    def test_the_very_last_page_is_empty_too(self, stream):
        """第 13/13 页（按精确 total 存在）同样是空页 + `truncated`。"""
        res = s.search_messages(stream, _TRUNC_QUERY, max_retained=10, per_page=4, page=13)
        assert res['results'] == [] and res['truncated'] is True
        assert res['total'] == _TRUNC_TOTAL and res['page'] == 13


class TestTruncationVisibility:
    """**绝不允许静默截断**：结构化字段 + 人话 warning 同时出现。"""

    def test_truncated_warning_carries_both_numbers(self, stream):
        res = s.search_messages(stream, _TRUNC_QUERY, max_retained=10)
        words = _truncation_warnings(res)
        assert len(words) == 1, '截断 warning 必须恰好一条：%r' % (res['warnings'],)
        assert '结果集过大（共 %d 条）' % _TRUNC_TOTAL in words[0]
        assert '只保留了前 %d 条' % 10 in words[0]
        assert '缩小范围' in words[0]

    def test_no_truncation_means_no_truncation_wording(self, stream):
        """**防误报**：未超上限的查询里不得出现任何截断文案。"""
        for res in (s.search_messages(stream, _TRUNC_QUERY),          # 52 < 50000
                    s.search_messages(stream, '类型:文本')):            # SQL 分页路径
            assert res['truncated'] is False
            assert res['retained_rows'] == res['total']
            assert _truncation_warnings(res) == []
            assert not [w for w in res['warnings'] if '结果集过大' in w]

    def test_sql_paged_path_is_never_truncated_even_with_a_tiny_cap(self, stream):
        """纯筛选走 SQL 侧 `LIMIT`（只取当页）⇒ 上限**不适用**，仍可完整翻页。

        这是契约定死的**有意设计**：截断只落在"必须物化幸存行"的形态上。
        """
        res = s.search_messages(stream, '类型:文本', max_retained=3, per_page=5, page=2)
        assert res['truncated'] is False
        assert res['retained_rows'] == res['total'] == 49     # 60 − 11 张无正文图片
        assert len(res['results']) == 5                       # 第 2 页照常满
        assert res['total_pages'] == 10
        assert _truncation_warnings(res) == []
        # 最后一页（第 10 页）也仍有数据：保留窗口根本没参与这条路径
        last = s.search_messages(stream, '类型:文本', max_retained=3, per_page=5, page=10)
        assert len(last['results']) == 4 and last['truncated'] is False
        assert last['retained_rows'] == last['total'] == 49


class TestFallbackPathTruncation:
    """降级路径（`_execute_fallback`）同样必须截断 —— 它也是把幸存行物化在内存里的。"""

    @pytest.fixture
    def no_index(self, tmp_path):
        """与 `rich` 同一份语料，只是不建索引 → 必然走降级路径。"""
        return _make_rich_dir(tmp_path)

    def test_fallback_is_truncated_too(self, no_index):
        """`类型:文本` 在降级路径命中 5 条；上限 2 ⇒ 保留 2 条，total 仍是 5。"""
        res = s.search_messages(no_index, '类型:文本', max_retained=2, per_page=2)
        assert res['used_fallback'] is True and res['scan_mode'] == 'fallback'
        assert res['truncated'] is True
        assert res['retained_rows'] == 2
        assert res['total'] == 5, '降级路径的 total 被截断污染了'
        assert res['candidate_rows'] == 5, '降级路径的 candidate_rows 被截断污染了'
        assert len(_truncation_warnings(res)) == 1
        assert res['total_pages'] == 3                       # ceil(5/2)，精确 total
        beyond = s.search_messages(no_index, '类型:文本', max_retained=2, per_page=2, page=3)
        assert beyond['results'] == [] and beyond['truncated'] is True

    def test_fallback_total_matches_the_uncapped_run(self, no_index):
        """**判别性**：上限小 vs 上限无穷，`total` 与 `candidate_rows` 逐位相同。"""
        capped = s.search_messages(no_index, '类型:文本', max_retained=1)
        uncapped = s.search_messages(no_index, '类型:文本', max_retained=_NO_CAP)
        assert (capped['total'], capped['candidate_rows']) == \
               (uncapped['total'], uncapped['candidate_rows'])
        assert capped['retained_rows'] == 1 and uncapped['retained_rows'] == uncapped['total']

    def test_fallback_without_truncation_says_nothing(self, no_index):
        res = s.search_messages(no_index, '类型:文本')
        assert res['truncated'] is False and res['retained_rows'] == res['total'] == 5
        assert _truncation_warnings(res) == []

    def test_fallback_exclude_early_out_is_idempotent(self, no_index):
        """**精确 total 的前提**：降级路径的排除词早退与 `_apply_excludes` 口径相同。

        `total` 是"扫描过的幸存行条数"（含被丢弃的那部分），只有 `_apply_excludes`
        对**保留行**是幂等的，那个数才等于最终结果集大小。这里把它钉住。
        """
        for query in ('类型:文本', '维修', '-维修', '维修 -电话'):
            parsed = sq.parse_query(query)
            scope = s._resolve_scope(no_index, parsed, None)
            rows = s._execute_fallback(no_index, scope, parsed, [])
            kept = s._apply_excludes(rows, parsed)
            assert len(kept) == len(rows), '%s：早退口径与 _apply_excludes 不一致' % query
            res = s.search_messages(no_index, query)
            assert res['total'] == len(rows)


class TestUserVisibleWordingHasNoMarkdownAsterisks:
    """T19（R41 裁决 #5）：**用户可见**文案里的 `**` 会显示成字面星号，必须清掉。

    这些字符串在页面里走 `escapeHtml`、在 CLI 里直接 `print` ⇒ 用户看到的是
    `**只保留了前 50000 条**`。判据是「用户可见的字符串里没有 `**`」，
    **不是**「源码里没有 `**`」—— 注释里的 `**` 是 Markdown 强调，必须原样保留。

    ⚠️ 防空转：每条都先断言「这句话真的被我拿到了」（子串判定）再断言没有星号，
    否则把常量改成空串也能让「没有星号」通过。
    """

    def test_fallback_text_only_warning_has_no_markdown_asterisks(self):
        w = s._FALLBACK_TEXT_ONLY_WARNING
        assert '只能看到有正文的消息' in w, w
        assert '**' not in w, w

    def test_truncated_warning_has_no_markdown_asterisks(self):
        w = s._TRUNCATED_WARNING % (1018563, 50000)
        assert '结果集过大（共 1018563 条）' in w, w
        assert '只保留了前 50000 条' in w, w
        assert '**' not in w, w

    def test_truncated_query_warnings_carry_no_asterisks(self, stream):
        """端到端：真的跑出截断，再看响应里给用户的那串 warnings。"""
        res = s.search_messages(stream, _TRUNC_QUERY, max_retained=10)
        assert res['truncated'] is True
        assert any('只保留了前 10 条' in w for w in res['warnings']), res['warnings']
        assert not [w for w in res['warnings'] if '**' in w], res['warnings']

    def test_fallback_text_only_warnings_carry_no_asterisks(self, rich):
        """端到端：降级路径的「只扫描文本表」warning（Task 6 留下的那条）。"""
        with _index_hidden(rich):
            res = s.search_messages(rich, '类型:图片')
        assert res['fallback_text_only'] is True, res['warnings']
        assert any('只能看到有正文的消息' in w for w in res['warnings']), res['warnings']
        assert not [w for w in res['warnings'] if '**' in w], res['warnings']

