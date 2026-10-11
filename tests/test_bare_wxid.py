"""Task 14 / known-issues #20：wxid **值形式**（账号目录名 vs 裸 wxid）的规范化。

三层：
1. 单元级 —— `engine.utils.bare_wxid()` 的参数化语义（含全部边界），语义必须与
   `engine.services.search._bare_wxid()` **完全一致**；
2. 跨实现等价性 —— 两份实现逐个输入逐字比较（防止将来漂移；Task 13 交回后
   `search._bare_wxid` 会改成一行委托，这条测试仍然有效）；
3. 行为级 —— 合成夹具（`data/chats.db` fast path + `Msg_` 慢路径）构造「本人会话」，
   用**裸 wxid** 与**24 字符账号目录名**两种形式分别调用，断言两种形式都把它排除掉，
   且**非本人会话仍在结果里**（否则"全过滤成 0 条"也能让"排除了本人"通过）。

⚠️ known-issues #19：本文件的 autouse fixture 把 `engine.services.media._detect_wxid`
换成会**抛异常**的桩 —— 任何路径一旦去扫真实微信目录就立刻失败，而不是静默读用户数据。
本文件所有标识符都是合成的，不读任何真实微信数据。
"""
import hashlib
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

from engine import utils
from engine.services import chat, search

# --- 合成标识（19 字符裸 wxid / 24 字符账号目录名，与真实数据同形但全部虚构） ---
OWN_BARE = 'wxid_' + 'a' * 14          # 19 字符：Name2Id / chats.db 里存的形式
OWN_DIR = OWN_BARE + '_10e8'           # 24 字符：app.config['WXID'] 里存的形式
FRIEND = 'wxid_' + 'b' * 14
ROOM = '11112222@chatroom'

assert len(OWN_BARE) == 19 and len(OWN_DIR) == 24 and OWN_BARE != OWN_DIR


def bare_wxid_sut():
    """取被测函数（延迟取，缺失时给出**说明原因**的失败，而不是一句 ImportError）。"""
    fn = getattr(utils, 'bare_wxid', None)
    assert callable(fn), (
        'engine.utils.bare_wxid 不存在：Task 14 要求把 wxid 规范化助手提到共享位置 '
        '（engine/utils.py），并且用**自证式命名** bare_wxid（不要叫 normalize）。'
        'chat.py 的两处"本人会话"比较必须复用它，不能各写一份。')
    return fn


# (输入, 期望输出, 这条覆盖的边界)
BARE_CASES = [
    (OWN_DIR, OWN_BARE, '账号目录名 → 去掉尾部 4 位十六进制段'),
    (OWN_BARE, OWN_BARE, '已是裸 wxid（2 段）→ 原样返回'),
    ('wxid_abcdefghijklm_10E8', 'wxid_abcdefghijklm', '尾段大写十六进制同样去掉'),
    ('wxid_abc_def_0000', 'wxid_abc_def', '尾段 0000 也是十六进制 → 去掉'),
    ('wxid_abcdefghijklm_10e', 'wxid_abcdefghijklm_10e', '尾段 3 字符 → 原样（绝不乱截）'),
    ('wxid_abcdefghijklm_10e88', 'wxid_abcdefghijklm_10e88', '尾段 5 字符 → 原样（绝不乱截）'),
    ('wxid_abcdefghijklm_10g8', 'wxid_abcdefghijklm_10g8', '尾段 4 字符但含非十六进制 → 原样'),
    ('wxid_abcdefghijklm_', 'wxid_abcdefghijklm_', '尾段为空串（长度 0）→ 原样'),
    ('a_b', 'a_b', '只有 1 个下划线（2 段）→ 原样'),
    ('a_b_10e8', 'a_b', '3 段且尾段合规 → 只去掉最后一段（前面的段必须保留）'),
    ('wxid_abc_def_10e8', 'wxid_abc_def', '4 段且尾段合规 → 只去掉最后一段'),
    ('a__10e8', 'a_', '3 段、中间是空段：仍按"去掉最后一段"处理（与现有实现一致）'),
    ('wxid_a10e8', 'wxid_a10e8', '无下划线的账号串完全不受影响'),
    ('', '', '空串 → 空串'),
    (None, '', 'None → 空串（与现状一致，调用方无须先判空）'),
    ('   ', '', '全空白 → 空串'),
    ('  ' + OWN_DIR + '  ', OWN_BARE, '两侧空白先 strip 再判定'),
]

BARE_VALUES = [case[0] for case in BARE_CASES]


# --------------------------------------------------------------------------
# 1. 单元级
# --------------------------------------------------------------------------

@pytest.mark.parametrize('value,expected,why', BARE_CASES,
                         ids=[c[2] for c in BARE_CASES])
def test_bare_wxid_semantics(value, expected, why):
    got = bare_wxid_sut()(value)
    assert got == expected, (
        'bare_wxid(%r) 应为 %r（%s），实际 %r' % (value, expected, why, got))
    assert isinstance(got, str), 'bare_wxid 必须总返回 str（None 也返回空串），实际 %r' % (got,)


@pytest.mark.parametrize('value', BARE_VALUES,
                         ids=[c[2] for c in BARE_CASES])
def test_bare_wxid_is_idempotent(value):
    once = bare_wxid_sut()(value)
    twice = bare_wxid_sut()(once)
    assert twice == once, (
        'bare_wxid 不幂等：bare_wxid(%r)=%r，再作用一次得 %r' % (value, once, twice))


def test_case_table_is_not_vacuous():
    """先证明样本**非空且有区分度**：既有被改写的输入，也有被原样保留的输入。

    没有这一步，"两份实现相等"这类断言可以被一个全靠原样返回的假实现满足
    （ADR-0012 过程教训 4：凡"应该相同"的断言，必须先证明样本确实被区分）。
    """
    fn = bare_wxid_sut()
    changed = [v for v in BARE_VALUES if fn(v) != (v or '').strip()]
    kept = [v for v in BARE_VALUES
            if fn(v) == (v or '').strip() and (v or '').strip() != '']
    assert len(changed) >= 3, '用例表里"被改写"的输入太少（%d）' % len(changed)
    assert len(kept) >= 5, '用例表里"被原样保留"的输入太少（%d）' % len(kept)


# --------------------------------------------------------------------------
# 2. 跨实现等价性（Task 13 正在改 search.py，本轮不得碰它 → 用测试锁住两份实现）
# --------------------------------------------------------------------------

@pytest.mark.parametrize('value', BARE_VALUES + [OWN_DIR.upper(), '_10e8', 'a_b_c_d1e2'],
                         ids=None)
def test_utils_bare_wxid_matches_search_bare_wxid(value):
    mine = bare_wxid_sut()(value)
    theirs = search._bare_wxid(value)
    assert mine == theirs, (
        '两份实现漂移：utils.bare_wxid(%r)=%r，search._bare_wxid(%r)=%r'
        % (value, mine, value, theirs))


def test_search_implementation_is_the_reference():
    """把 `search._bare_wxid` 的既有语义钉在测试里（它是 Critical #17 的修法）。

    这条同时是"参考实现非空转"的证据：参考实现对目录名确实做了改写。
    """
    assert search._bare_wxid(OWN_DIR) == OWN_BARE
    assert search._bare_wxid(OWN_BARE) == OWN_BARE


# --------------------------------------------------------------------------
# 3. 行为级：合成夹具
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def no_real_wechat_scan(monkeypatch):
    """known-issues #19：任何路径都不得在测试里扫真实微信目录。"""
    from engine.services import media

    def _boom(*_a, **_k):
        raise AssertionError(
            '测试路径调用了 engine.services.media._detect_wxid —— 它会全局扫描并返回'
            '本机真实账号目录名（known-issues #19），等于读用户数据。'
            '本文件的夹具必须自己显式传入 wxid。')

    monkeypatch.setattr(media, '_detect_wxid', _boom)
    # 显式断言 patch 生效：否则将来 monkeypatch 目标改名，这条防线会静默失效。
    assert media._detect_wxid is _boom


CHAT_ROWS = [
    # chat_id, display_name, message_count, last_msg_time, is_group
    (OWN_BARE, '本人', 999, 1789999999, 0),
    (FRIEND, '联系人甲', 12, 1789000000, 0),
    (ROOM, '群聊甲', 5, 1788000000, 1),
]


def _write_chats_db(root):
    os.makedirs(os.path.join(root, 'data'), exist_ok=True)
    con = sqlite3.connect(os.path.join(root, 'data', 'chats.db'))
    con.execute('CREATE TABLE chats (chat_id TEXT, display_name TEXT,'
                ' message_count INTEGER, last_msg_time INTEGER, is_group INTEGER)')
    con.executemany('INSERT INTO chats VALUES (?,?,?,?,?)', CHAT_ROWS)
    con.commit()
    con.close()


def _write_contact_db(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE contact (id INTEGER, username TEXT, remark TEXT,'
                ' nick_name TEXT, alias TEXT, small_head_url TEXT)')
    con.executemany('INSERT INTO contact VALUES (?,?,?,?,?,?)', [
        (1, OWN_BARE, '本人', '', '', ''),
        (2, FRIEND, '联系人甲', '', '', ''),
        (3, ROOM, '群聊甲', '', '', ''),
    ])
    con.execute('CREATE TABLE chat_room (username TEXT, owner TEXT, ext_buffer BLOB)')
    con.execute('INSERT INTO chat_room VALUES (?,?,?)', (ROOM, OWN_BARE, b''))
    con.commit()
    con.close()


def _write_message_db(root):
    os.makedirs(os.path.join(root, 'message'), exist_ok=True)
    path = os.path.join(root, 'message', 'message_0.db')
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE Name2Id (user_name TEXT)')
    con.executemany('INSERT INTO Name2Id VALUES (?)',
                    [(OWN_BARE,), (FRIEND,), (ROOM,)])
    for i, uname in enumerate((OWN_BARE, FRIEND, ROOM), start=1):
        tname = 'Msg_' + hashlib.md5(uname.encode()).hexdigest()
        con.execute('CREATE TABLE [%s] (create_time INTEGER, message_content TEXT)'
                    % tname)
        con.execute('INSERT INTO [%s] VALUES (?,?)' % tname, (1780000000 + i, None))
    con.commit()
    con.close()


@pytest.fixture
def fast_dir(tmp_path):
    """fast path：只有 data/chats.db。"""
    root = str(tmp_path / 'fast')
    _write_chats_db(root)
    return root


@pytest.fixture
def slow_dir(tmp_path):
    """slow path：没有 chats.db，走 message_*.db 的 Msg_/Name2Id 全扫。"""
    root = str(tmp_path / 'slow')
    _write_contact_db(os.path.join(root, 'contact', 'contact.db'))
    os.makedirs(os.path.join(root, 'session'), exist_ok=True)
    con = sqlite3.connect(os.path.join(root, 'session', 'session.db'))
    con.execute('CREATE TABLE SessionTable (username TEXT, summary TEXT)')
    con.commit()
    con.close()
    _write_message_db(root)
    return root


def _ids(contacts):
    return {c['id'] for c in contacts}


# ---- fast path（chat.py:191 `_load_from_chats_db`） ----

@pytest.mark.parametrize('form', [OWN_BARE, OWN_DIR],
                         ids=['bare', 'account-dir'])
def test_load_from_chats_db_excludes_own_session(fast_dir, form):
    ids = _ids(chat._load_from_chats_db(fast_dir, form))
    assert OWN_BARE not in ids, (
        'wxid=%r（%d 字符）时本人会话 %r 仍出现在会话列表里' % (form, len(form), OWN_BARE))
    # 防退化成 0 条：非本人会话必须还在
    assert FRIEND in ids and ROOM in ids, '过滤过度：非本人会话被一并丢掉（%r）' % (sorted(ids),)


def test_load_from_chats_db_two_forms_agree(fast_dir):
    by_bare = _ids(chat._load_from_chats_db(fast_dir, OWN_BARE))
    by_dir = _ids(chat._load_from_chats_db(fast_dir, OWN_DIR))
    assert by_bare == by_dir, (
        '两种 wxid 形式结果不一致：裸 wxid %r vs 目录名 %r'
        % (sorted(by_bare), sorted(by_dir)))
    assert len(by_bare) == 2, '期望剩 2 个会话，实际 %d：%r' % (len(by_bare), sorted(by_bare))


@pytest.mark.parametrize('wxid', [None, ''],
                         ids=['none', 'empty'])
def test_load_from_chats_db_without_wxid_filters_nothing(fast_dir, wxid):
    """对照组：不给 wxid 时三条都在 —— 证明"少了一条"确实是过滤造成的，不是夹具本身少。"""
    ids = _ids(chat._load_from_chats_db(fast_dir, wxid))
    assert ids == {OWN_BARE, FRIEND, ROOM}, '不给 wxid 时不应过滤任何会话，实际 %r' % (sorted(ids),)


@pytest.mark.parametrize('form', [OWN_BARE, OWN_DIR],
                         ids=['bare', 'account-dir'])
def test_get_contacts_fast_path_excludes_own_session(fast_dir, form):
    ids = _ids(chat.get_contacts(fast_dir, form))
    assert OWN_BARE not in ids, (
        'get_contacts(dir, %r) 仍把本人会话泄漏进会话列表' % (form,))
    assert FRIEND in ids and ROOM in ids, '过滤过度：非本人会话被一并丢掉（%r）' % (sorted(ids),)


def test_get_contacts_fast_path_two_forms_agree(fast_dir):
    bare = _ids(chat.get_contacts(fast_dir, OWN_BARE))
    as_dir = _ids(chat.get_contacts(fast_dir, OWN_DIR))
    assert bare == as_dir, ('两种形式条数/内容不一致：裸 wxid %r vs 目录名 %r'
                            % (sorted(bare), sorted(as_dir)))
    assert len(bare) == 2, '期望剩 2 个会话，实际 %d：%r' % (len(bare), sorted(bare))


def test_get_contacts_fast_path_control_without_wxid(fast_dir):
    assert len(chat.get_contacts(fast_dir, None)) == 3


# ---- slow path（chat.py:84 `get_contacts` 全扫） ----

@pytest.mark.parametrize('form', [OWN_BARE, OWN_DIR],
                         ids=['bare', 'account-dir'])
def test_get_contacts_slow_path_excludes_own_session(slow_dir, form):
    ids = _ids(chat.get_contacts(slow_dir, form))
    assert OWN_BARE not in ids, (
        '慢路径 get_contacts(dir, %r) 仍把本人会话泄漏进会话列表' % (form,))
    assert FRIEND in ids and ROOM in ids, '过滤过度：非本人会话被一并丢掉（%r）' % (sorted(ids),)


def test_get_contacts_slow_path_two_forms_agree(slow_dir):
    bare = _ids(chat.get_contacts(slow_dir, OWN_BARE))
    as_dir = _ids(chat.get_contacts(slow_dir, OWN_DIR))
    assert bare == as_dir, ('慢路径两种形式不一致：裸 wxid %r vs 目录名 %r'
                            % (sorted(bare), sorted(as_dir)))
    assert len(bare) == 2, '期望剩 2 个会话，实际 %d：%r' % (len(bare), sorted(bare))


def test_get_contacts_slow_path_control_without_wxid(slow_dir):
    """对照组：证明慢路径夹具本身能扫出 3 个会话（否则"少了 1 条"无从谈起）。"""
    assert _ids(chat.get_contacts(slow_dir, None)) == {OWN_BARE, FRIEND, ROOM}
