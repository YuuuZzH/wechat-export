"""索引构建/刷新/状态 —— 全部用合成目录，绝不使用真实微信数据。"""
import hashlib
import os
import pathlib
import re
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

from engine.services import search_index as si


def _make_shard(path, chats):
    """造一个 message_N.db：含 Name2Id + 每个会话一张 Msg_<md5> 表。

    chats: [(username, [(local_id, local_type, create_time, real_sender_id), ...]), ...]
    """
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE Name2Id (user_name TEXT)')
    # Name2Id 的 rowid 就是 real_sender_id
    for username, _ in chats:
        con.execute('INSERT INTO Name2Id (user_name) VALUES (?)', (username,))
    for username, rows in chats:
        h = hashlib.md5(username.encode()).hexdigest()
        con.execute('CREATE TABLE [Msg_%s] (local_id INTEGER, local_type INTEGER,'
                    ' create_time INTEGER, real_sender_id INTEGER, message_content BLOB)' % h)
        con.executemany(
            'INSERT INTO [Msg_%s] VALUES (?,?,?,?,?)' % h,
            [(lid, lt, ct, sid, b'') for lid, lt, ct, sid in rows])
    con.commit()
    con.close()


@pytest.fixture
def decrypted(tmp_path):
    """两个消息分片，含个人/群聊，覆盖文本/图片/语音/系统类型与高位标志。"""
    d = tmp_path / 'dec'
    (d / 'message').mkdir(parents=True)
    _make_shard(str(d / 'message' / 'message_0.db'), [
        ('wxid_alpha', [
            (1, 1, 1600000000, 1),
            (2, 3, 1600000100, 2),
            (3, 1, 1600000200, 1),
        ]),
        ('111@chatroom', [
            (1, 1, 1600000300, 2),
            (2, 34, 1600000400, 3),
        ]),
    ])
    _make_shard(str(d / 'message' / 'message_1.db'), [
        ('wxid_alpha', [
            (10, 244813135921, 1600000500, 1),   # 高位标志 → 基类型 49
        ]),
        ('wxid_beta', [
            (1, 10000, 1600000600, 1),
        ]),
    ])
    return str(d)


class TestDiscovery:
    def test_shards_found_and_sorted(self, decrypted):
        got = si.discover_message_shards(decrypted)
        assert [os.path.basename(p) for p in got] == ['message_0.db', 'message_1.db']

    def test_fts_db_excluded(self, decrypted):
        # 造一个 fts 库，确认不会被当成消息分片
        open(os.path.join(decrypted, 'message', 'message_fts.db'), 'wb').close()
        got = si.discover_message_shards(decrypted)
        # 断言精确集合：漏掉 message_0.db 也必须失败（原来只查 'fts' not in 名字，太弱）
        assert got == [os.path.join(decrypted, 'message', 'message_0.db'),
                       os.path.join(decrypted, 'message', 'message_1.db')]

    def test_relative_dir_returns_absolute_paths(self, decrypted, monkeypatch):
        """docstring 承诺绝对路径 —— as_uri() 也依赖这一点。"""
        monkeypatch.chdir(decrypted)
        got = si.discover_message_shards('message')
        assert all(os.path.isabs(p) for p in got)
        assert got == [os.path.join(decrypted, 'message', 'message_0.db'),
                       os.path.join(decrypted, 'message', 'message_1.db')]

    def test_unreadable_shard_dir_does_not_raise(self, decrypted, monkeypatch):
        """PermissionError 必须被容忍：模块文档承诺坏源不能中断整次构建。"""
        real_listdir = os.listdir

        def fake_listdir(p):
            if os.path.basename(str(p)) == 'message':
                raise PermissionError(13, 'denied')
            return real_listdir(p)

        monkeypatch.setattr(si.os, 'listdir', fake_listdir)
        assert si.discover_message_shards(decrypted) == []

    def test_missing_dir_returns_empty(self, tmp_path):
        assert si.discover_message_shards(str(tmp_path / 'nope')) == []

    def test_fts_content_tables_discovered_dynamically(self, tmp_path):
        p = tmp_path / 'message_fts.db'
        con = sqlite3.connect(p)
        for i in (0, 1, 7):
            con.execute('CREATE TABLE message_fts_v4_%d_content (id INTEGER, c0 TEXT)' % i)
        con.commit()
        assert si.discover_fts_content_tables(con) == [
            'message_fts_v4_0_content', 'message_fts_v4_1_content', 'message_fts_v4_7_content']
        con.close()

    def test_fts_content_tables_rejects_wildcard_false_positives(self, tmp_path):
        """`_` / `%` 在 LIKE 里同样是通配符，必须再用精确正则校验。

        与 `_msg_tables` 的 `MsgX…` 是同一个缺陷类；Task 4 会拿这些表名做
        `INSERT … SELECT`，误收野表就会写错数据。
        """
        p = tmp_path / 'fts_loose.db'
        con = sqlite3.connect(p)
        for name in ('message_fts_v4_0_content',
                     'message_fts_v4_12_content',
                     'message_fts_v4_X_content',   # LIKE 命中，但分段不是数字
                     'message_fts_v4__content'):   # `%` 可匹配空串，也命中 LIKE
            con.execute('CREATE TABLE [%s] (id INTEGER, c0 TEXT)' % name)
        con.commit()
        assert si.discover_fts_content_tables(con) == [
            'message_fts_v4_0_content', 'message_fts_v4_12_content']
        con.close()

    def test_chat_map(self, decrypted):
        m = si.build_chat_map(si.discover_message_shards(decrypted))
        assert m[hashlib.md5(b'wxid_alpha').hexdigest()] == 'wxid_alpha'
        assert m[hashlib.md5(b'111@chatroom').hexdigest()] == '111@chatroom'
        assert len(m) == 3


class TestMsgMeta:
    def _conn(self, tmp_path):
        con = sqlite3.connect(str(tmp_path / 'idx.db'))
        si.create_schema(con)
        return con

    def test_row_count(self, decrypted, tmp_path):
        con = self._conn(tmp_path)
        n = si.build_msg_meta(con, si.discover_message_shards(decrypted),
                              si.build_chat_map(si.discover_message_shards(decrypted)))
        assert n == 7
        assert con.execute('SELECT COUNT(*) FROM msg_meta').fetchone()[0] == 7

    def test_local_type_normalised_to_base(self, decrypted, tmp_path):
        con = self._conn(tmp_path)
        si.build_msg_meta(con, si.discover_message_shards(decrypted),
                          si.build_chat_map(si.discover_message_shards(decrypted)))
        lt = con.execute("SELECT local_type FROM msg_meta WHERE chat_id='wxid_alpha'"
                         ' AND local_id=10').fetchone()[0]
        assert lt == 49

    def test_sender_resolved_per_shard(self, decrypted, tmp_path):
        """real_sender_id 是分片内序号，必须按分片解析。"""
        con = self._conn(tmp_path)
        si.build_msg_meta(con, si.discover_message_shards(decrypted),
                          si.build_chat_map(si.discover_message_shards(decrypted)))
        s = con.execute("SELECT sender_username FROM msg_meta WHERE chat_id='111@chatroom'"
                        ' AND local_id=1').fetchone()[0]
        assert s == '111@chatroom'      # 该分片 Name2Id 的第 2 行
        s2 = con.execute("SELECT sender_username FROM msg_meta WHERE chat_id='wxid_alpha'"
                         ' AND local_id=2').fetchone()[0]
        assert s2 == '111@chatroom'     # message_0 的第 2 行也是它

    def test_db_stem_recorded(self, decrypted, tmp_path):
        con = self._conn(tmp_path)
        si.build_msg_meta(con, si.discover_message_shards(decrypted),
                          si.build_chat_map(si.discover_message_shards(decrypted)))
        stems = {r[0] for r in con.execute('SELECT DISTINCT db_stem FROM msg_meta')}
        assert stems == {'message_0', 'message_1'}

    def test_idempotent_rebuild(self, decrypted, tmp_path):
        con = self._conn(tmp_path)
        args = (si.discover_message_shards(decrypted),
                si.build_chat_map(si.discover_message_shards(decrypted)))
        si.build_msg_meta(con, *args)
        si.build_msg_meta(con, *args)
        assert con.execute('SELECT COUNT(*) FROM msg_meta').fetchone()[0] == 7

    def test_cross_shard_same_local_id_does_not_collapse(self, tmp_path):
        """回归：同一会话在两个分片里都有 local_id=1，但它们是**不同消息**，必须都保留。

        真实数据实测：`local_id` 每个分片都从 1 重数，于是
        distinct(chat, local_id)=461,455 而总行数 1,035,292 —— 若主键只用
        (chat_id, local_id)，INSERT OR REPLACE 会静默丢掉 55.4% 的消息。
        """
        d = tmp_path / 'collapse'
        (d / 'message').mkdir(parents=True)
        # 同一会话 wxid_alpha 出现在两个分片，local_id 都从 1 开始，create_time 不同
        _make_shard(str(d / 'message' / 'message_0.db'), [
            ('wxid_alpha', [(1, 1, 1600000000, 1), (2, 1, 1600000100, 1)]),
        ])
        _make_shard(str(d / 'message' / 'message_1.db'), [
            ('wxid_alpha', [(1, 1, 1700000000, 1), (2, 1, 1700000100, 1)]),
        ])
        con = self._conn(tmp_path)
        shards = si.discover_message_shards(str(d))
        si.build_msg_meta(con, shards, si.build_chat_map(shards))
        rows = con.execute('SELECT COUNT(*) FROM msg_meta').fetchone()[0]
        assert rows == 4, '跨分片同 (chat, local_id) 的不同消息被折叠了（实际 %d 行）' % rows
        # 同键的四条必须靠 create_time 区分，且都应能查到
        times = sorted(r[0] for r in con.execute(
            "SELECT create_time FROM msg_meta WHERE chat_id='wxid_alpha' AND local_id=1"))
        assert times == [1600000000, 1700000000]

    def test_uri_metacharacter_dir_shard_is_indexed(self, tmp_path):
        """回归：路径含 `#` / `%` 时绝不能靠字符串拼接生成 file: URI。

        `'file:%s?mode=ro' % path` 会在 `#` 处被当成 fragment 截断，连 `?mode=ro`
        一起丢掉 → 连接被**以可写方式**打开（在真实分片旁落地 0 字节垃圾文件），
        读取失败又被 `except sqlite3.Error` 吞掉 → 整个分片静默贡献 0 行。
        `%XX` 则会被百分号解码成另一个路径。必须用 pathlib 的 as_uri()。
        """
        base = tmp_path / 'probe#hash' / 'pct%20dir'
        (base / 'message').mkdir(parents=True)
        _make_shard(str(base / 'message' / 'message_0.db'), [
            ('wxid_alpha', [(1, 1, 1600000000, 1)]),
        ])
        shards = si.discover_message_shards(str(base))
        assert shards == [os.path.join(str(base), 'message', 'message_0.db')]
        m = si.build_chat_map(shards)
        assert m[hashlib.md5(b'wxid_alpha').hexdigest()] == 'wxid_alpha'
        con = self._conn(tmp_path)
        assert si.build_msg_meta(con, shards, m) == 1
        # 拼接形式会把 'probe' 当库名，在真实分片旁创建垃圾文件
        assert not os.path.exists(os.path.join(str(tmp_path), 'probe'))

    def test_duplicate_triple_last_writer_wins_and_is_counted(self, tmp_path):
        """文档化的假设：跨分片**完全相同**的三元组按「最后一个分片胜出」，只存 1 行。

        返回值必须是**实际落库行数**（COUNT(*))，否则这类折叠无法被任何代码发现：
        旧实现返回尝试写入数 2，而表里只有 1 行。
        """
        d = tmp_path / 'dupe'
        (d / 'message').mkdir(parents=True)
        _make_shard(str(d / 'message' / 'message_0.db'), [
            ('wxid_alpha', [(1, 1, 1600000000, 1)]),
        ])
        _make_shard(str(d / 'message' / 'message_1.db'), [
            ('wxid_alpha', [(1, 34, 1600000000, 1)]),
        ])
        con = self._conn(tmp_path)
        shards = si.discover_message_shards(str(d))
        n = si.build_msg_meta(con, shards, si.build_chat_map(shards))
        assert n == 1
        assert con.execute('SELECT COUNT(*) FROM msg_meta').fetchone()[0] == 1
        # message_1 后处理，覆盖 message_0 的同键行
        assert con.execute('SELECT local_type, db_stem FROM msg_meta').fetchone() == (
            34, 'message_1')

    def test_null_local_id_tolerated(self, tmp_path):
        """NULL local_id 必须被容忍。

        否则 executemany 会在 `DELETE FROM msg_meta` **之后**抛 IntegrityError，
        留下半张表 —— 比整库重建失败更难排查。
        """
        d = tmp_path / 'nullid'
        (d / 'message').mkdir(parents=True)
        _make_shard(str(d / 'message' / 'message_0.db'), [
            ('wxid_alpha', [(None, 1, 1600000000, 1), (7, 1, 1600000100, 1)]),
        ])
        con = self._conn(tmp_path)
        shards = si.discover_message_shards(str(d))
        assert si.build_msg_meta(con, shards, si.build_chat_map(shards)) == 2
        assert sorted(r[0] for r in con.execute('SELECT local_id FROM msg_meta')) == [0, 7]


# ==========================================================================
# Task 4: message_fts 构建 / build_index / index_status / refresh_index / open_index
# ==========================================================================

def _make_fts_db(path, rows, usernames=('wxid_alpha', '111@chatroom', 'wxid_beta')):
    """造微信结构的 message_fts.db：content 表为普通表，列序 c0..c6。

    rows: [(text, local_id, sort_seq_ms, local_type, session_id, sender_id, create_time)]
    usernames: name2id 的内容（rowid 即 content 表里 session_id 的取值）。
      name2id 为空 = 模拟「微信改了表名/列名/id 语义」导致会话解析全失效。
    """
    con = sqlite3.connect(path)
    for i in (0, 1):
        con.execute('CREATE TABLE message_fts_v4_%d_content (id INTEGER PRIMARY KEY,'
                    ' c0 TEXT, c1 INTEGER, c2 INTEGER, c3 INTEGER, c4 INTEGER,'
                    ' c5 INTEGER, c6 INTEGER)' % i)
        con.execute('CREATE TABLE message_fts_v4_%d_aux (message_local_id INTEGER,'
                    ' sort_seq INTEGER, session_id INTEGER)' % i)
    con.execute('CREATE TABLE name2id (username TEXT)')
    con.executemany('INSERT INTO name2id (username) VALUES (?)',
                    [(u,) for u in usernames])
    half = len(rows) // 2
    for i, chunk in enumerate((rows[:half], rows[half:])):
        con.executemany(
            'INSERT INTO message_fts_v4_%d_content VALUES (NULL,?,?,?,?,?,?,?)' % i, chunk)
    con.commit()
    con.close()


@pytest.fixture
def decrypted_with_fts(decrypted):
    """在已有多分片目录上补一个微信 FTS 库，session_id 与 name2id rowid 对齐。"""
    # 1 server_id 1/2/3 对应上面 name2id 的 rowid 1/2/3
    rows = [
        ('维修服务器', 1, 1600000000000, 1, 1, 1, 1600000000),
        ('发票已开', 2, 1600000100000, 1, 1, 2, 1600000100),
        ('报修电话', 3, 1600000200000, 1, 1, 1, 1600000200),
        ('hello disk', 1, 1600000300000, 1, 2, 2, 1600000300),
        ('语音消息', 2, 1600000400000, 34, 2, 3, 1600000400),
        ('链接分享', 10, 1600000500000, 244813135921, 1, 1, 1600000500),
    ]
    _make_fts_db(os.path.join(decrypted, 'message', 'message_fts.db'), rows)
    return decrypted


def _fts_db_of(decrypted_dir):
    return os.path.join(decrypted_dir, 'message', 'message_fts.db')


def _pk_columns(path, table='msg_meta'):
    con = sqlite3.connect(path)
    try:
        return {r[1] for r in con.execute('PRAGMA table_info([%s])' % table) if r[5]}
    finally:
        con.close()


class TestFtsBuild:
    def test_row_count(self, decrypted_with_fts, tmp_path):
        con = sqlite3.connect(str(tmp_path / 'idx.db'))
        si.create_schema(con)
        n = si.build_fts(con, os.path.join(decrypted_with_fts, 'message', 'message_fts.db'))
        assert n == 6

    def test_chinese_two_char_word_is_searchable(self, decrypted_with_fts, tmp_path):
        """trigram 会漏双字词，char-token 必须命中。"""
        con = sqlite3.connect(str(tmp_path / 'idx.db'))
        si.create_schema(con)
        si.build_fts(con, os.path.join(decrypted_with_fts, 'message', 'message_fts.db'))
        n = con.execute('SELECT COUNT(*) FROM message_fts WHERE message_fts MATCH ?',
                        ('"维 修"',)).fetchone()[0]
        assert n == 1

    def test_single_char_and_english_prefix(self, decrypted_with_fts, tmp_path):
        con = sqlite3.connect(str(tmp_path / 'idx.db'))
        si.create_schema(con)
        si.build_fts(con, os.path.join(decrypted_with_fts, 'message', 'message_fts.db'))
        assert con.execute('SELECT COUNT(*) FROM message_fts WHERE message_fts MATCH ?',
                           ('"票"',)).fetchone()[0] == 1
        assert con.execute('SELECT COUNT(*) FROM message_fts WHERE message_fts MATCH ?',
                           ('"d i s k"',)).fetchone()[0] == 1

    def test_session_id_mapped_to_username(self, decrypted_with_fts, tmp_path):
        con = sqlite3.connect(str(tmp_path / 'idx.db'))
        si.create_schema(con)
        si.build_fts(con, os.path.join(decrypted_with_fts, 'message', 'message_fts.db'))
        chats = {r[0] for r in con.execute('SELECT DISTINCT chat_id FROM message_fts')}
        assert chats == {'wxid_alpha', '111@chatroom'}

    def test_no_fts_db_returns_zero(self, decrypted, tmp_path):
        con = sqlite3.connect(str(tmp_path / 'idx.db'))
        si.create_schema(con)
        assert si.build_fts(con, os.path.join(decrypted, 'message', 'message_fts.db')) == 0

    def test_fts_db_found_in_second_candidate_location(self, decrypted, tmp_path):
        """A2：`discover_message_shards` 在 `message/` 与解密根目录两处找分片，
        message_fts.db 也必须按**同样的候选顺序**解析 —— 硬编码
        `<dir>/message/message_fts.db` 会在另一种布局上静默得到 0 行文本。"""
        d = tmp_path / 'flat'
        d.mkdir()
        _make_shard(str(d / 'message_0.db'), [('wxid_alpha', [(1, 1, 1600000000, 1)])])
        _make_fts_db(str(d / 'message_fts.db'),
                     [('维修', 1, 1600000000000, 1, 1, 1, 1600000000)])
        res = si.build_index(str(d))
        assert res['meta_rows'] == 1
        assert res['fts_rows'] == 1

    def test_explicit_rowid_insert_is_used(self, decrypted_with_fts, tmp_path):
        """A8：rowid 必须由自己的计数器显式指定（两张表写同一个 rowid）。"""
        con = sqlite3.connect(str(tmp_path / 'idx.db'))
        si.create_schema(con)
        si.build_fts(con, os.path.join(decrypted_with_fts, 'message', 'message_fts.db'))
        fts = [r[0] for r in con.execute('SELECT rowid FROM message_fts ORDER BY rowid')]
        txt = [r[0] for r in con.execute('SELECT rowid FROM msg_text ORDER BY rowid')]
        assert fts == [1, 2, 3, 4, 5, 6]
        assert txt == fts


class TestFtsBuildVisibility:
    """A3：静默丢弃必须可见。name2id 失效时「索引 ready、全文永远 0 结果」是最危险的
    状态 —— 用户会以为「没有这条消息」，而不是「索引坏了」。"""

    def test_unresolved_session_count_exposed(self, tmp_path):
        d = tmp_path / 'noid'
        (d / 'message').mkdir(parents=True)
        _make_fts_db(str(d / 'message' / 'message_fts.db'), [
            ('维修服务器', 1, 1600000000000, 1, 1, 1, 1600000000),
            ('发票已开', 2, 1600000100000, 1, 1, 2, 1600000100),
            ('报修电话', 3, 1600000200000, 1, 1, 1, 1600000200),
        ], usernames=())
        con = sqlite3.connect(str(tmp_path / 'idx.db'))
        si.create_schema(con)
        stats = si.build_fts_ex(con, str(d / 'message' / 'message_fts.db'))
        assert stats['rows'] == 0
        assert stats['skipped_no_chat'] == 3
        assert stats['skipped_empty_text'] == 0
        assert stats['tables'] == 2

    def test_empty_text_rows_counted(self, tmp_path):
        d = tmp_path / 'blank'
        (d / 'message').mkdir(parents=True)
        _make_fts_db(str(d / 'message' / 'message_fts.db'), [
            ('维修', 1, 1600000000000, 1, 1, 1, 1600000000),
            (None, 2, 1600000100000, 1, 1, 1, 1600000100),
            ('   ', 3, 1600000200000, 1, 1, 1, 1600000200),
        ])
        con = sqlite3.connect(str(tmp_path / 'idx.db'))
        si.create_schema(con)
        stats = si.build_fts_ex(con, str(d / 'message' / 'message_fts.db'))
        assert stats['rows'] == 1
        assert stats['skipped_empty_text'] == 2
        assert stats['skipped_no_chat'] == 0

    def test_ready_index_with_zero_text_coverage_is_visible(self, tmp_path):
        """最危险的状态：index_status 报 ready=True，而全文覆盖率是 0。"""
        d = tmp_path / 'noid2'
        (d / 'message').mkdir(parents=True)
        _make_shard(str(d / 'message' / 'message_0.db'),
                    [('wxid_alpha', [(1, 1, 1600000000, 1)])])
        _make_fts_db(str(d / 'message' / 'message_fts.db'),
                     [('维修', 1, 1600000000000, 1, 1, 1, 1600000000)], usernames=())
        res = si.build_index(str(d))
        assert res['meta_rows'] == 1 and res['fts_rows'] == 0
        assert res['fts_skipped_no_chat'] == 1
        st = si.index_status(str(d))
        assert st['ready'] is True
        assert st['fts_skipped_no_chat'] == 1
        assert st['fts_coverage'] == 0.0


class TestFtsBuildDuplicateKeys:
    """A6：FTS5 虚表**没有主键**，源里完全相同的三元组会真的插入两行 →
    与 msg_meta 的一行不匹配 → Task 5 join 出重复命中。"""

    def test_return_is_stored_count_and_keys_unique(self, tmp_path):
        d = tmp_path / 'dup'
        (d / 'message').mkdir(parents=True)
        # 前两条三元组完全相同（local_id/create_time/会话都一样），第三条不同
        _make_fts_db(str(d / 'message' / 'message_fts.db'), [
            ('第一条', 1, 1600000000000, 1, 1, 1, 1600000000),
            ('重复的第二条', 1, 1600000000000, 1, 1, 1, 1600000000),
            ('第三条', 2, 1600000100000, 1, 1, 1, 1600000100),
        ])
        con = sqlite3.connect(str(tmp_path / 'idx.db'))
        si.create_schema(con)
        n = si.build_fts(con, str(d / 'message' / 'message_fts.db'))
        stored = con.execute('SELECT COUNT(*) FROM message_fts').fetchone()[0]
        dupes = con.execute('SELECT COUNT(*) FROM (SELECT chat_id, local_id, create_time'
                            ' FROM message_fts GROUP BY 1, 2, 3'
                            ' HAVING COUNT(*) > 1)').fetchone()[0]
        text_rows = con.execute('SELECT COUNT(*) FROM msg_text').fetchone()[0]
        orphan = con.execute('SELECT COUNT(*) FROM message_fts f LEFT JOIN msg_text t'
                             ' ON t.rowid = f.rowid WHERE t.rowid IS NULL').fetchone()[0]
        assert n == stored == 2, '返回值必须是实存行数（尝试写入 3 行）'
        assert dupes == 0, 'message_fts 上出现了重复三元组，Task 5 join 会出重复命中'
        assert text_rows == 2 and orphan == 0


class TestMsgText:
    """A8：原文单独存普通表 + UNIQUE 索引，rowid 与 message_fts 一一对应。"""

    def test_rowid_correspondence(self, decrypted_with_fts):
        si.build_index(decrypted_with_fts)
        con = sqlite3.connect(si.index_path(decrypted_with_fts))
        try:
            assert con.execute('SELECT COUNT(*) FROM msg_text').fetchone()[0] == 6
            orphan_fts = con.execute(
                'SELECT COUNT(*) FROM message_fts f LEFT JOIN msg_text t'
                ' ON t.rowid = f.rowid WHERE t.rowid IS NULL').fetchone()[0]
            orphan_txt = con.execute(
                'SELECT COUNT(*) FROM msg_text t LEFT JOIN message_fts f'
                ' ON f.rowid = t.rowid WHERE f.rowid IS NULL').fetchone()[0]
            assert orphan_fts == 0, 'message_fts 有 msg_text 里不存在的 rowid（错位）'
            assert orphan_txt == 0, 'msg_text 有 message_fts 里不存在的 rowid（错位）'
        finally:
            con.close()

    def test_triple_matches_per_rowid(self, decrypted_with_fts):
        """同 rowid 的两行必须是**同一条消息**（不然摘要会串到别的消息上）。"""
        si.build_index(decrypted_with_fts)
        con = sqlite3.connect(si.index_path(decrypted_with_fts))
        try:
            bad = con.execute(
                'SELECT COUNT(*) FROM message_fts f JOIN msg_text t ON t.rowid = f.rowid'
                ' WHERE f.chat_id <> t.chat_id OR f.local_id <> t.local_id'
                ' OR f.create_time <> t.create_time').fetchone()[0]
            assert bad == 0
        finally:
            con.close()

    def test_unique_key_index_exists(self, decrypted_with_fts):
        si.build_index(decrypted_with_fts)
        con = sqlite3.connect(si.index_path(decrypted_with_fts))
        try:
            names = {r[1] for r in con.execute('PRAGMA index_list(msg_text)')}
            cols = [r[2] for r in con.execute('PRAGMA index_info(idx_text_key)')]
        finally:
            con.close()
        assert 'idx_text_key' in names
        assert cols == ['chat_id', 'local_id', 'create_time']


class TestBuildIndex:
    def test_full_build(self, decrypted_with_fts):
        res = si.build_index(decrypted_with_fts)
        assert res['meta_rows'] == 7
        assert res['fts_rows'] == 6
        assert res['msg_text_rows'] == 6
        assert res['source_rows'] == 7
        assert os.path.isfile(si.index_path(decrypted_with_fts))

    def test_progress_stages_reported(self, decrypted_with_fts):
        stages = []
        si.build_index(decrypted_with_fts,
                       progress=lambda stage, msg, pct: stages.append(stage))
        for expected in (si.STAGE_META, si.STAGE_TEXT, si.STAGE_OPTIMIZE, si.STAGE_DONE):
            assert expected in stages

    def test_meta_only(self, decrypted_with_fts):
        res = si.build_index(decrypted_with_fts, text=False)
        assert res['meta_rows'] == 7 and res['fts_rows'] == 0

    def test_text_only(self, decrypted_with_fts):
        res = si.build_index(decrypted_with_fts, meta=False)
        assert res['fts_rows'] == 6 and res['meta_rows'] == 0

    def test_rebuild_is_idempotent(self, decrypted_with_fts):
        si.build_index(decrypted_with_fts)
        res = si.build_index(decrypted_with_fts, force=True)
        assert res['fts_rows'] == 6

    def test_build_index_skips_when_already_fresh(self, decrypted_with_fts):
        """`force=False` 且索引已正好是这次要的东西 → 不再重建（真实数据一次构建 ~50s，
        不该被随手调用触发）；`force=True` 才是无条件重建。"""
        si.build_index(decrypted_with_fts)
        res = si.build_index(decrypted_with_fts)
        assert res.get('skipped') is True
        assert res['fts_rows'] == 6 and res['meta_rows'] == 7
        res2 = si.build_index(decrypted_with_fts, force=True)
        assert 'skipped' not in res2

    def test_build_index_does_not_skip_when_requested_part_is_empty(self, decrypted_with_fts):
        """「已新鲜」不等于「这次要的部件都在」：只有 meta 的索引不能顶替一次全文构建。"""
        si.build_index(decrypted_with_fts, text=False)
        assert si.index_status(decrypted_with_fts)['fts_rows'] == 0
        res = si.build_index(decrypted_with_fts)
        assert 'skipped' not in res
        assert res['fts_rows'] == 6 and res['meta_rows'] == 7

    def test_meta_only_clears_stale_text(self, decrypted_with_fts):
        """text=False 是「不建文本」，不是「留着上次的文本」——
        留着会让 open_index 的摘要和 msg_meta 来自两批不同的数据。"""
        si.build_index(decrypted_with_fts)
        si.build_index(decrypted_with_fts, text=False, force=True)
        con = sqlite3.connect(si.index_path(decrypted_with_fts))
        try:
            assert con.execute('SELECT COUNT(*) FROM message_fts').fetchone()[0] == 0
            assert con.execute('SELECT COUNT(*) FROM msg_text').fetchone()[0] == 0
        finally:
            con.close()

    def test_build_index_reports_meta_skipped_rows(self, tmp_path):
        """A3：msg_meta 侧的静默丢弃（会话名解析不出 → 整张 Msg_ 表被跳过）也要计数。"""
        d = tmp_path / 'orphan'
        (d / 'message').mkdir(parents=True)
        _make_shard(str(d / 'message' / 'message_0.db'), [
            ('wxid_alpha', [(1, 1, 1600000000, 1)]),
        ])
        # 手工塞一张「会话名在 Name2Id 里找不到」的 Msg_ 表
        con = sqlite3.connect(str(d / 'message' / 'message_0.db'))
        con.execute('CREATE TABLE [Msg_%s] (local_id INTEGER, local_type INTEGER,'
                    ' create_time INTEGER, real_sender_id INTEGER, message_content BLOB)'
                    % hashlib.md5(b'wxid_ghost').hexdigest())
        con.execute('INSERT INTO [Msg_%s] VALUES (?,?,?,?,?)'
                    % hashlib.md5(b'wxid_ghost').hexdigest(), (1, 1, 1600000009, 1, b''))
        con.commit()
        con.close()
        res = si.build_index(str(d))
        assert res['meta_rows'] == 1
        assert res['meta_skipped_unresolved_tables'] == 1
        assert res['meta_skipped_unresolved_rows'] == 1
        st = si.index_status(str(d))
        assert st['meta_skipped_unresolved_tables'] == 1


class TestSchemaMigration:
    """A1（Critical）：`create_schema` 全是 `CREATE TABLE IF NOT EXISTS`，表已存在时
    **整条语句是 no-op**，旧主键原样保留；而 `build_index` 会照常写新的
    `schema_version` → `index_status` 报 schema_ok=True/ready=True —— **缺陷自我掩盖**，
    索引看起来健康、实际只剩 44.6% 的消息，且此后永远检测不出来。
    所以必须：读既有库的 schema_version **并校验实际主键列集合**，不符即 DROP 重建。"""

    def _collide_dir(self, tmp_path):
        """同一会话 (chat_id, local_id) 相同、create_time 不同的 3 条消息。

        真实数据形态：`local_id` 每个分片从 1 重数，所以跨分片同键是很常见的
        （实测 distinct(chat,local_id)=461,455 vs 总行数 1,035,292）。
        """
        d = tmp_path / 'collide'
        (d / 'message').mkdir(parents=True)
        _make_shard(str(d / 'message' / 'message_0.db'), [
            ('wxid_alpha', [(1, 1, 1600000000, 1), (2, 1, 1600000100, 1)]),
        ])
        _make_shard(str(d / 'message' / 'message_1.db'), [
            ('wxid_alpha', [(1, 1, 1700000000, 1)]),
        ])
        return str(d)

    def _make_legacy_index(self, path, version, pk='two', looks_healthy=False):
        """造一个「旧版」索引库（Task 3 修掉的主键折叠 bug 的形态）。

        looks_healthy=True 时把 meta_rows/fts_rows 也写成非零 —— 即
        **「索引自称健康」的自我掩盖形态**：版本号是本版本、行数非零，只有主键是旧的。
        """
        con = sqlite3.connect(path)
        cols = ('chat_id TEXT NOT NULL, local_id INTEGER NOT NULL,'
                ' create_time INTEGER NOT NULL, local_type INTEGER NOT NULL,'
                ' sender_username TEXT, db_stem TEXT')
        key = ('PRIMARY KEY (chat_id, local_id)' if pk == 'two'
               else 'PRIMARY KEY (chat_id, local_id, create_time)')
        con.execute('CREATE TABLE msg_meta (%s, %s) WITHOUT ROWID' % (cols, key))
        con.execute('CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT)')
        con.execute("INSERT INTO index_meta (key, value) VALUES ('schema_version', ?)",
                    (str(version),))
        if looks_healthy:
            con.execute("INSERT INTO index_meta (key, value) VALUES ('meta_rows', '3')")
            con.execute("INSERT INTO index_meta (key, value) VALUES ('fts_rows', '3')")
        con.commit()
        con.close()

    def test_legacy_two_column_pk_is_migrated(self, tmp_path):
        d = self._collide_dir(tmp_path)
        self._make_legacy_index(si.index_path(d), version='0')
        res = si.build_index(d)
        pk = _pk_columns(si.index_path(d))
        con = sqlite3.connect(si.index_path(d))
        rows = con.execute('SELECT COUNT(*) FROM msg_meta').fetchone()[0]
        con.close()
        assert pk == {'chat_id', 'local_id', 'create_time'}, \
            '旧 2 列主键没有被迁移掉（实际主键列：%s）' % sorted(pk)
        assert res['meta_rows'] == 3
        assert rows == 3, '旧主键下只剩 %d 行（源 3 行）—— 消息被折叠' % rows
        st = si.index_status(d)
        assert st['schema_ok'] is True and st['ready'] is True

    def test_structure_check_catches_lying_schema_version(self, tmp_path):
        """**不能只信 schema_version 数字**：版本号写成本版本、主键仍是 2 列时也要重建。

        本次事故的根因就是「相信声明、不验证实际结构」。
        """
        d = self._collide_dir(tmp_path)
        self._make_legacy_index(si.index_path(d), version=str(si.SCHEMA_VERSION))
        res = si.build_index(d)
        pk = _pk_columns(si.index_path(d))
        assert pk == {'chat_id', 'local_id', 'create_time'}, \
            'schema_version 声明为本版本，2 列主键被当成健康库复用（实际：%s）' % sorted(pk)
        assert res['meta_rows'] == 3

    def test_missing_schema_version_means_rebuild(self, tmp_path):
        d = self._collide_dir(tmp_path)
        self._make_legacy_index(si.index_path(d), version='0')
        con = sqlite3.connect(si.index_path(d))
        con.execute('DELETE FROM index_meta')
        con.commit()
        con.close()
        assert si.build_index(d)['meta_rows'] == 3
        assert _pk_columns(si.index_path(d)) == {'chat_id', 'local_id', 'create_time'}

    def test_status_reports_not_ok_for_self_declared_healthy_legacy_index(self, tmp_path):
        """**自我掩盖形态**：索引自称健康（版本号=本版本、行数非零），但主键是旧的。

        此时 `index_status` 必须报 schema_ok=False/ready=False（→ 走降级或重建），
        而不是把「只剩 44.6% 消息」的索引当成可用。
        """
        d = self._collide_dir(tmp_path)
        self._make_legacy_index(si.index_path(d), version=str(si.SCHEMA_VERSION),
                               looks_healthy=True)
        st = si.index_status(d)
        assert st['schema_ok'] is False
        assert st['ready'] is False
        assert st['meta_rows'] == 3 and st['fts_rows'] == 3   # 自称的行数确实是读到了

    def test_unreadable_or_junk_index_is_rebuilt(self, tmp_path):
        """索引库是垃圾（不是 sqlite / 没有元数据表）时也不能卡死。"""
        d = self._collide_dir(tmp_path)
        with open(si.index_path(d), 'wb') as fh:
            fh.write(b'not a sqlite database')
        res = si.build_index(d)
        assert res['meta_rows'] == 3
        assert si.index_status(d)['ready'] is True

    def test_refresh_rebuilds_when_schema_version_mismatch(self, decrypted_with_fts):
        """A1 尾注：schema_ok=False 时 refresh_index 必须**真正重建**（不是跳过）。"""
        si.build_index(decrypted_with_fts)
        con = sqlite3.connect(si.index_path(decrypted_with_fts))
        con.execute("UPDATE index_meta SET value='99' WHERE key='schema_version'")
        con.execute('DELETE FROM msg_meta')
        con.commit()
        con.close()
        res = si.refresh_index(decrypted_with_fts)
        assert 'skipped' not in res, 'schema 不匹配时 refresh 居然跳过了'
        assert res['meta_rows'] == 7
        assert si.index_status(decrypted_with_fts)['ready'] is True


class TestStatusAndRefresh:
    def test_status_before_build(self, decrypted_with_fts):
        st = si.index_status(decrypted_with_fts)
        assert st['exists'] is False and st['ready'] is False
        assert st['schema_ok'] is False and st['stale'] is False
        assert st['fts_rows'] == 0 and st['msg_text_rows'] == 0

    def test_status_after_build(self, decrypted_with_fts):
        si.build_index(decrypted_with_fts)
        st = si.index_status(decrypted_with_fts)
        assert st['exists'] and st['ready'] and st['schema_ok']
        assert st['fts_rows'] == 6 and st['meta_rows'] == 7
        assert st['built_at'] > 0
        assert st['stale'] is False
        assert st['msg_text_rows'] == 6
        assert st['meta_coverage'] == 1.0
        assert 0.0 < st['fts_coverage'] < 1.0
        assert st['fts_skipped_no_chat'] == 0

    def test_schema_version_mismatch_means_not_ready(self, decrypted_with_fts):
        si.build_index(decrypted_with_fts)
        con = sqlite3.connect(si.index_path(decrypted_with_fts))
        con.execute("UPDATE index_meta SET value='99' WHERE key='schema_version'")
        con.commit()
        con.close()
        assert si.index_status(decrypted_with_fts)['ready'] is False

    def test_status_does_not_scan_source_tables(self, decrypted_with_fts, monkeypatch):
        """A4：`index_status` 的热路径**不得**打开源分片（也就不可能逐表
        `COUNT(*)/MAX(create_time)`）。

        实测真实数据（7 分片 / 4,675 张 Msg_ 表 / 1,018,918 行）：全表扫描一次
        **1.40s（全热缓存）～ 6.29s（冷缓存）**（控制方在更冷的机器上测得 13.4s），
        而改造后的整个 `index_status` 只要 **3.0–3.7ms**。搜索页每次加载都调
        index_status，付不起几秒。

        判别方式与实现无关：拦住**所有** sqlite3.connect，只放行指向索引自身的连接。
        任何「顺手扫一遍源」的写法（无论用哪个 helper）都会在这里炸。
        """
        si.build_index(decrypted_with_fts)
        real_connect = sqlite3.connect

        def spy(target, *args, **kwargs):
            if si.INDEX_FILENAME not in str(target):
                raise AssertionError('index_status 打开了源数据：%r' % (target,))
            return real_connect(target, *args, **kwargs)

        monkeypatch.setattr(si.sqlite3, 'connect', spy)
        st = si.index_status(decrypted_with_fts)
        assert st['ready'] is True and st['stale'] is False

    def test_index_status_opens_index_through_connect_ro(self, decrypted_with_fts,
                                                         monkeypatch):
        """A2：连接**只有一个**构造点 `_connect_ro`（pathlib as_uri 转义）。

        手写 `'file:%s?mode=ro' % path` 在含 `#`/`%` 的路径上会连到别的库、
        丢掉 `?mode=ro`（变可写）、并静默丢数据。
        """
        si.build_index(decrypted_with_fts)
        seen = []
        real = si._connect_ro

        def spy(path):
            seen.append(os.path.abspath(path))
            return real(path)

        monkeypatch.setattr(si, '_connect_ro', spy)
        assert si.index_status(decrypted_with_fts)['ready'] is True
        assert seen == [os.path.abspath(si.index_path(decrypted_with_fts))]

    def test_stale_after_source_append(self, decrypted_with_fts):
        """A4：不许为了变快而丢掉 stale 检测能力。"""
        si.build_index(decrypted_with_fts)
        assert si.index_status(decrypted_with_fts)['stale'] is False
        shard = os.path.join(decrypted_with_fts, 'message', 'message_1.db')
        con = sqlite3.connect(shard)
        h = hashlib.md5(b'wxid_beta').hexdigest()
        con.execute('INSERT INTO [Msg_%s] VALUES (?,?,?,?,?)' % h, (9, 1, 1600009999, 1, b''))
        con.commit()
        con.close()
        assert si.index_status(decrypted_with_fts)['stale'] is True

    def test_empty_wal_appearing_or_vanishing_is_not_staleness(self, tmp_path):
        """**空的 `-wal` 不得影响 stale 判定**（回归：我自己第一版指纹的缺陷）。

        实测：空的 `-wal` 是**连接本身**的产物 —— 只读连接挂上分片就会留下一个 0 字节
        `-wal`，之后再开一个读写连接并关闭，它连同 `-shm` 一起消失，而 `.db` 一动没动。
        若把 `-wal:0` 也算进指纹，真实索引会在「什么都没变」时被判 stale
        （实测：构建时 `-wal` 存在 0 字节，事后被删 → `index_status()['stale']` 变 True，
        于是每次都会触发一次 ~50s 的无意义全量重建）。
        """
        d = tmp_path / 'emptywal'
        (d / 'message').mkdir(parents=True)
        shard = str(d / 'message' / 'message_0.db')
        _make_shard(shard, [('wxid_alpha', [(1, 1, 1600000000, 1)])])
        con = sqlite3.connect(shard)
        assert con.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        con.close()
        # 造一个 0 字节 -wal（真实只读连接就会造出这种文件）
        with open(shard + '-wal', 'wb'):
            pass
        si.build_index(str(d))
        os.remove(shard + '-wal')                # 它消失（实测连接关闭时会被删掉）
        assert si.index_status(str(d))['stale'] is False, \
            '空的 -wal 存/亡被当成了源数据变化'

    def test_stale_detected_for_wal_append_held_by_open_writer(self, tmp_path):
        """A4 的残余盲区实测：微信分片是 **WAL 模式**，写者未关闭时追加内容只在
        `-wal` 里 —— `message_N.db` 的 **size 与 mtime 完全不变**。

        实测（真实分片副本）：
            before write    db size=27013120 mtime_ns=...841000 | wal size=0
            after commit    db size=27013120 mtime_ns=...841000 | wal size=24752
        指纹若只看 .db 就会判 stale=False → 索引**静默缺消息**。所以指纹必须含 `-wal`。
        （`-shm` 则相反：只读连接挂上分片就会写它，放进指纹会让刚构建完的索引立刻 stale。）
        """
        d = tmp_path / 'wal'
        (d / 'message').mkdir(parents=True)
        _make_shard(str(d / 'message' / 'message_0.db'),
                    [('wxid_alpha', [(1, 1, 1600000000, 1)])])
        con = sqlite3.connect(str(d / 'message' / 'message_0.db'))
        con.execute('PRAGMA wal_autocheckpoint=0')
        assert con.execute('PRAGMA journal_mode=WAL').fetchone()[0] == 'wal'
        con.commit()
        try:
            si.build_index(str(d))
            assert si.index_status(str(d))['stale'] is False
            db_path = str(d / 'message' / 'message_0.db')
            before = (os.stat(db_path).st_size, os.stat(db_path).st_mtime_ns)
            h = hashlib.md5(b'wxid_alpha').hexdigest()
            con.execute('INSERT INTO [Msg_%s] VALUES (?,?,?,?,?)' % h,
                        (2, 1, 1600009999, 1, b''))
            con.commit()
            after = (os.stat(db_path).st_size, os.stat(db_path).st_mtime_ns)
            assert after == before, '-wal 里持有追加时 .db 本来就不该变（本测试的前提）'
            assert os.stat(db_path + '-wal').st_size > 0
            assert si.index_status(str(d))['stale'] is True, \
                'WAL 里的追加没被察觉 —— 索引会静默缺消息'
        finally:
            con.close()

    def test_refresh_appends_new_messages(self, decrypted_with_fts):
        si.build_index(decrypted_with_fts)
        # 追加一条新消息（时间更晚）
        shard = os.path.join(decrypted_with_fts, 'message', 'message_1.db')
        con = sqlite3.connect(shard)
        h = hashlib.md5(b'wxid_beta').hexdigest()
        con.execute('INSERT INTO [Msg_%s] VALUES (?,?,?,?,?)' % h, (2, 1, 1600009999, 1, b''))
        con.commit()
        con.close()
        res = si.refresh_index(decrypted_with_fts)
        assert res['meta_rows'] == 8
        st = si.index_status(decrypted_with_fts)
        assert st['stale'] is False

    def test_refresh_without_changes_is_noop(self, decrypted_with_fts):
        si.build_index(decrypted_with_fts)
        res = si.refresh_index(decrypted_with_fts)
        assert res.get('skipped') is True

    def test_refresh_refuses_to_wipe_index_when_source_vanished(self, decrypted_with_fts):
        """源分片全不见了（盘未挂载/目录被清空）时**不许**把好索引重建为空索引。

        否则用户看到的是「搜索没结果」，而不是「源不见了」—— 又是静默错状态。
        """
        si.build_index(decrypted_with_fts)
        msg_dir = os.path.join(decrypted_with_fts, 'message')
        for name in os.listdir(msg_dir):
            if name.startswith('message_') and name.endswith('.db') \
                    and name != 'message_fts.db':
                os.remove(os.path.join(msg_dir, name))
        res = si.refresh_index(decrypted_with_fts)
        assert res.get('skipped') is True
        assert res.get('reason') == 'no_source'
        con = si.open_index(decrypted_with_fts)
        try:
            assert con.execute('SELECT COUNT(*) FROM msg_meta').fetchone()[0] == 7
        finally:
            con.close()


class TestOpenIndex:
    def test_open_missing_returns_none(self, decrypted_with_fts):
        assert si.open_index(decrypted_with_fts) is None

    def test_open_existing(self, decrypted_with_fts):
        si.build_index(decrypted_with_fts)
        con = si.open_index(decrypted_with_fts)
        assert con is not None
        assert con.execute('SELECT COUNT(*) FROM msg_meta').fetchone()[0] == 7
        con.close()

    def test_open_index_is_read_only(self, decrypted_with_fts):
        """索引连接必须只读：读写连接会在用户目录里落地 -wal/-shm 并可能被写坏。"""
        si.build_index(decrypted_with_fts)
        con = si.open_index(decrypted_with_fts)
        try:
            with pytest.raises(sqlite3.OperationalError):
                con.execute('CREATE TABLE nope (a INTEGER)')
        finally:
            con.close()

    def test_index_in_uri_metacharacter_dir(self, tmp_path):
        """A2 回归：索引自身的连接也必须走 `_connect_ro`/as_uri()。

        `'file:%s?mode=ro' % path` 在含 `#` 的路径上于 fragment 处截断，
        连 `?mode=ro` 一起丢掉 → 连到**另一个（不存在的）库**、变成可写、
        在用户目录里落地垃圾文件，随后 `_get_meta` 抛的 OperationalError 没人接。
        """
        base = tmp_path / 'probe#hash' / 'pct%20dir'
        (base / 'message').mkdir(parents=True)
        _make_shard(str(base / 'message' / 'message_0.db'),
                    [('wxid_alpha', [(1, 1, 1600000000, 1)])])
        si.build_index(str(base))
        assert si.index_status(str(base))['ready'] is True
        con = si.open_index(str(base))
        assert con is not None
        try:
            assert con.execute('SELECT COUNT(*) FROM msg_meta').fetchone()[0] == 1
        finally:
            con.close()
        # 拼接形式会在 `probe#hash/` 下创建名为 `probe` 的垃圾库
        assert list(pathlib.Path(tmp_path).rglob('probe')) == []


# --------------------------------------------------------------------------
# A5：对抗性夹具 —— 跨表键碰撞 + 单侧存在行（Task 5 join 语义的契约）
# --------------------------------------------------------------------------

@pytest.fixture
def decrypted_adv(tmp_path):
    """A5/A8 对抗性夹具（独立于 `decrypted_with_fts`，不动它的行数断言）。

    消息侧（message_0.db，会话 wxid_alpha）：
      (local_id=1,  type=1, ct=100)  文本 —— content 侧有同键同行
      (local_id=1,  type=1, ct=200)  文本 —— 同上：**同 (chat, local_id)、不同 create_time**
      (local_id=98, type=3, ct=400)  图片 —— content 侧**没有**对应行（无正文）
    content 侧（message_fts.db，session_id=1 → wxid_alpha）：
      (local_id=1,  ct=100)  raw='Order A1234\\nok'
      (local_id=1,  ct=200)  raw='Second  ä 维修'
      (local_id=99, ct=300)  raw='Only in fts'   <-- 消息侧**没有**对应行
    """
    d = tmp_path / 'adv'
    (d / 'message').mkdir(parents=True)
    _make_shard(str(d / 'message' / 'message_0.db'), [
        ('wxid_alpha', [
            (1, 1, 100, 1),
            (1, 1, 200, 1),
            (98, 3, 400, 1),
        ]),
    ])
    _make_fts_db(str(d / 'message' / 'message_fts.db'), [
        ('Order A1234\nok', 1, 100000, 1, 1, 1, 100),
        ('Second  ä 维修', 1, 200000, 1, 1, 1, 200),
        ('Only in fts', 99, 300000, 1, 1, 1, 300),
    ], usernames=('wxid_alpha',))
    return str(d)


class TestAdversarialJoinKeys:
    def test_same_key_different_create_time_kept_on_both_sides(self, decrypted_adv):
        """A5-1：两侧同键（chat_id+local_id 相同、create_time 不同）必须各留 2 行。

        任何把 create_time 从键里丢掉的实现（主键 / join 键）都会在这里折叠成 1 行。
        """
        si.build_index(decrypted_adv)
        con = sqlite3.connect(si.index_path(decrypted_adv))
        try:
            meta_times = sorted(r[0] for r in con.execute(
                "SELECT create_time FROM msg_meta WHERE chat_id='wxid_alpha' AND local_id=1"))
            text_times = sorted(r[0] for r in con.execute(
                "SELECT create_time FROM msg_text WHERE chat_id='wxid_alpha' AND local_id=1"))
            fts_times = sorted(r[0] for r in con.execute(
                "SELECT create_time FROM message_fts WHERE chat_id='wxid_alpha' AND local_id=1"))
        finally:
            con.close()
        assert meta_times == [100, 200]
        assert text_times == [100, 200]
        assert fts_times == [100, 200]

    def test_one_side_only_rows_are_stored_per_side(self, decrypted_adv):
        """A5-2：仅一侧存在的行，各自留在自己那侧（不做任何「补齐」）。"""
        si.build_index(decrypted_adv)
        con = sqlite3.connect(si.index_path(decrypted_adv))
        try:
            assert con.execute('SELECT COUNT(*) FROM msg_meta').fetchone()[0] == 3
            assert con.execute('SELECT COUNT(*) FROM message_fts').fetchone()[0] == 3
            assert con.execute('SELECT COUNT(*) FROM msg_meta WHERE local_id=99'
                               ).fetchone()[0] == 0
            assert con.execute('SELECT COUNT(*) FROM message_fts WHERE local_id=99'
                               ).fetchone()[0] == 1
            assert con.execute('SELECT COUNT(*) FROM msg_meta WHERE local_id=98'
                               ).fetchone()[0] == 1
            assert con.execute('SELECT COUNT(*) FROM message_fts WHERE local_id=98'
                               ).fetchone()[0] == 0
        finally:
            con.close()

    def test_fts_only_rows_are_counted(self, decrypted_adv):
        """「有正文、没有 msg_meta」的行数必须**可见**（fts_rows_without_meta）。

        真实数据里这类行有 125 条（占正文行 0.014%），关键词内连接会**静默**丢掉它们。
        0.014% 不值得重构 join 语义，但静默不行：哪天微信改了 content 表的会话/键语义，
        这个数字会变大，而症状只是「搜索少了一些结果」—— 正是本方案一直在堵的静默丢失。
        """
        res = si.build_index(decrypted_adv)
        st = si.index_status(decrypted_adv)
        con = sqlite3.connect(si.index_path(decrypted_adv))
        try:
            inner = con.execute(
                'SELECT COUNT(*) FROM msg_meta m JOIN message_fts f'
                ' ON f.chat_id = m.chat_id AND f.local_id = m.local_id'
                ' AND f.create_time = m.create_time').fetchone()[0]
            meta_only = con.execute(
                'SELECT COUNT(*) FROM msg_meta m LEFT JOIN msg_text t'
                ' ON t.chat_id = m.chat_id AND t.local_id = m.local_id'
                ' AND t.create_time = m.create_time WHERE t.rowid IS NULL').fetchone()[0]
        finally:
            con.close()
        assert res['fts_rows_without_meta'] == 1
        assert st['fts_rows_without_meta'] == 1
        # 计数必须正好等于「内连接会丢掉的那些」，而不是随手一个数
        assert st['fts_rows'] - st['fts_rows_without_meta'] == inner == 2
        # 它只描述正文侧的孤儿行，与「有 meta 无正文」是两回事
        assert meta_only == 1

    def test_fts_rows_without_meta_is_zero_when_all_text_has_meta(self, decrypted_with_fts):
        """没有这类行时必须是 0（不是「未知」也不是缺键）。"""
        si.build_index(decrypted_with_fts)
        st = si.index_status(decrypted_with_fts)
        assert st['fts_rows_without_meta'] == 0
        assert st['fts_rows'] == 6 and st['meta_rows'] == 7

    def test_join_semantics_contract_for_task5(self, decrypted_adv):
        """**Task 5 必须采用的 join 语义**（本测试即契约，键盘是三元组）：

        - 关键词/短语/正则路径：`msg_meta INNER JOIN message_fts` ON 三元组
          → 只保留两侧都有的键。图片/语音行（无 content 行）永远不可能命中关键词，
            被内连接丢掉是**正确**行为；反过来「只有 FTS、没有 msg_meta」的行也必须丢，
            因为 msg_meta 是 100% 覆盖的权威表，类型/日期/发送者筛选只能由它提供。
        - 纯筛选路径（无关键词）：`msg_meta LEFT JOIN msg_text` ON 三元组
          → 无正文消息保留、`raw_text IS NULL`（spec 第 280 行只承认无正文消息无摘要）。
        - 两处都**必须**用三列键：只用 (chat_id, local_id) 会把本夹具的 2 行命中
          放大成 4 行（重复命中 + 摘要串行）。
        """
        si.build_index(decrypted_adv)
        con = sqlite3.connect(si.index_path(decrypted_adv))
        try:
            keyword = con.execute(
                'SELECT COUNT(*) FROM msg_meta m JOIN message_fts f'
                ' ON f.chat_id = m.chat_id AND f.local_id = m.local_id'
                ' AND f.create_time = m.create_time').fetchone()[0]
            filt = con.execute(
                'SELECT COUNT(*) FROM msg_meta m LEFT JOIN msg_text t'
                ' ON t.chat_id = m.chat_id AND t.local_id = m.local_id'
                ' AND t.create_time = m.create_time').fetchone()[0]
            no_body = con.execute(
                'SELECT COUNT(*) FROM msg_meta m LEFT JOIN msg_text t'
                ' ON t.chat_id = m.chat_id AND t.local_id = m.local_id'
                ' AND t.create_time = m.create_time WHERE t.rowid IS NULL').fetchone()[0]
            pair_only = con.execute(
                'SELECT COUNT(*) FROM msg_meta m JOIN msg_text t'
                ' ON t.chat_id = m.chat_id AND t.local_id = m.local_id').fetchone()[0]
        finally:
            con.close()
        assert keyword == 2, '关键词路径应命中两侧都有的 2 条'
        assert filt == 3, '纯筛选路径应保留全部 3 条（含无正文的图片）'
        assert no_body == 1, '图片行应保留且 raw_text IS NULL'
        assert pair_only == 4, '只用两列键会把 2 条命中放大成 4 条（Task 5 必须用三列）'


class TestRawText:
    """A8：原文必须原样保留（摘要显示 / 高亮 / 正则在原文上确认）。"""

    def test_raw_text_verbatim_while_fts_text_normalised(self, decrypted_adv):
        si.build_index(decrypted_adv)
        con = sqlite3.connect(si.index_path(decrypted_adv))
        try:
            raw = con.execute('SELECT raw_text FROM msg_text WHERE create_time=100').fetchone()[0]
            tok = con.execute('SELECT text FROM message_fts WHERE create_time=100').fetchone()[0]
        finally:
            con.close()
        assert raw == 'Order A1234\nok'
        assert tok == 'o r d e r a 1 2 3 4 o k'

    def test_regex_needs_raw_text(self, decrypted_adv):
        """A8 的根因：正则在 char-token 文本上确认会**静默假阴性**
        （已建索引搜不到、降级路径搜得到 → 同一查询因为「后来建了索引」结果变少）。"""
        si.build_index(decrypted_adv)
        con = sqlite3.connect(si.index_path(decrypted_adv))
        try:
            raws = [r[0] for r in con.execute(
                'SELECT raw_text FROM msg_text WHERE raw_text IS NOT NULL')]
        finally:
            con.close()
        assert sorted(raws) == ['Only in fts', 'Order A1234\nok', 'Second  ä 维修']
        assert [t for t in raws if re.search(r'[A-Z]\d{4}', t)] == ['Order A1234\nok']

    def test_image_row_left_join_gives_null_raw_text(self, decrypted_adv):
        si.build_index(decrypted_adv)
        con = sqlite3.connect(si.index_path(decrypted_adv))
        try:
            row = con.execute(
                'SELECT m.local_type, t.raw_text FROM msg_meta m LEFT JOIN msg_text t'
                ' ON t.chat_id = m.chat_id AND t.local_id = m.local_id'
                ' AND t.create_time = m.create_time WHERE m.local_id = 98').fetchone()
        finally:
            con.close()
        assert row[0] == 3 and row[1] is None
