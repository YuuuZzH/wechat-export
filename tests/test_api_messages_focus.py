"""Task 18：搜索结果「点进去定位到那条消息」（known-issues #29）。

契约（见 `task-18-brief.md`）：

- `GET /api/messages` 新增两个**可选**参数 `focus_local_id` / `focus_create_time`；
- 给了 focus 时**忽略 page**，返回**包含该消息的那一页**，并新增 `focused` 块
  `{found, page, local_id, create_time, ...}`；
- `found=False` 必须有明确原因（绝不静默返回第 1 页）；
- focus 参数非法（非整数 / 只给一个）→ **400**；
- 不带 focus 的响应**逐字段不变**（向后兼容）。

判别性（防空转）是本文件的重点：

  ① 夹具**必须先证明有判别力** —— 会话有多页（`total_pages >= 3`）、样本非空、
     目标**不在第 1 页**；否则"第 1 页就是它"会让 `focused.page == 3` 之外的断言恒真；
  ② 目标落在**第 3 页**（k=3 ≥ 3），断言 `focused.page == 3`；
  ③ 夹具里**故意放了同 `local_id` 的另一条消息**（不同分片、不同 `create_time`，落在第 1 页）——
     只用 `local_id` 定位的实现会返回第 1 页，本条断言立刻变红；
  ④ 断言 focus 返回的那一页与**不带 focus 的同一页**逐条相同（focus 只选页，不改分页语义）。

全部使用**合成**夹具（tmp_path），绝不读取真实微信数据。
"""
import hashlib
import os

import pytest

from engine.services.message import query_messages
from engine.services import media as media_mod
from web.app import create_app

CHAT = 'wxid_focus_target'
PER_PAGE = 5

# 夹具设计（create_time 升序 = 消息由旧到新；分页语义是"最新在第 1 页"）：
#   18 行（17 条唯一 + 1 条重号）→ total_pages = 4
#   DESC 序下标 → 页号：index // PER_PAGE + 1
#     0..4   → 第 1 页（最新）
#     5..9   → 第 2 页
#     10..14 → 第 3 页  ← 目标在这里（index 12）
#     15..17 → 第 4 页
TARGET_LOCAL_ID = 404
TARGET_CREATE_TIME = 1700000400          # index 12 → 第 3 页
TARGET_PAGE = 3
# 同 local_id 的另一条（另一个分片、不同 create_time）：index 0 → 第 1 页。
# "只用 local_id 定位" 的实现会命中它并返回第 1 页 —— 这就是判别点。
DECOY_CREATE_TIME = 1700001600
DECOY_PAGE = 1
TOTAL_ROWS = 18
TOTAL_PAGES = 4

MISSING_LOCAL_ID = 999999
MISSING_CREATE_TIME = 1700000000


def _rows_a():
    """第 0 分片：create_time 1700000000..1700000900（旧的一半）。"""
    out = []
    for i in range(0, 10):
        lid = TARGET_LOCAL_ID if i == 4 else 100 + i
        out.append((lid, 1, 1, 1700000000 + 100 * i, 3, 'synthetic body %d' % i, 0, None))
    return out


def _rows_b():
    """第 1 分片：重号诱饵 + create_time 1700001000..1700001600（新的一半）。"""
    out = [(TARGET_LOCAL_ID, 1, 0, DECOY_CREATE_TIME, 3, 'synthetic decoy', 0, None)]
    for i in range(10, 17):
        out.append((100 + i, 1, 1, 1700000000 + 100 * i, 3, 'synthetic body %d' % i, 0, None))
    return out


def _make_shard(path, rows, chat_id=CHAT):
    """一个 message_N.db：Name2Id + 该会话的 Msg_<md5> 表（列序同真实库）。"""
    import sqlite3
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE Name2Id (user_name TEXT)')
    con.execute('INSERT INTO Name2Id (user_name) VALUES (?)', (chat_id,))
    tname = 'Msg_' + hashlib.md5(chat_id.encode()).hexdigest()
    con.execute(
        'CREATE TABLE [%s] (local_id INTEGER, local_type INTEGER, origin_source INTEGER,'
        ' create_time INTEGER, status INTEGER, message_content BLOB,'
        ' real_sender_id INTEGER, packed_info_data BLOB)' % tname)
    con.executemany('INSERT INTO [%s] VALUES (?,?,?,?,?,?,?,?)' % tname, rows)
    con.commit()
    con.close()


@pytest.fixture
def decrypted(tmp_path):
    d = tmp_path / 'dec'
    (d / 'message').mkdir(parents=True)
    _make_shard(str(d / 'message' / 'message_0.db'), _rows_a())
    _make_shard(str(d / 'message' / 'message_1.db'), _rows_b())
    return str(d)


@pytest.fixture(autouse=True)
def no_wxid_detection(monkeypatch):
    """`create_app` 里是 `wxid or _detect_wxid(...)`；无条件钉住，绝不探测真实目录。"""
    monkeypatch.setattr(media_mod, '_detect_wxid', lambda decrypted_dir: None)


@pytest.fixture
def client(decrypted):
    app = create_app(decrypted, wxid='wxid_owner')
    app.config['TESTING'] = True
    assert app.config['WXID'] == 'wxid_owner'
    return app.test_client()


def _ids(page):
    """页内逐条指纹：id + create_time（顺序敏感）。"""
    return [(m['id'], m['create_time']) for m in page['messages']]


# ===========================================================================
# 0. 夹具自身的判别力 —— 先证明"第 1 页不是它"，否则后面全是恒真断言
# ===========================================================================

def test_fixture_is_discriminating(decrypted):
    base = query_messages(decrypted, CHAT, wxid='wxid_owner', page=1, per_page=PER_PAGE)
    assert base['pagination']['total'] == TOTAL_ROWS, '样本必须非空且行数符合夹具设计'
    assert base['pagination']['total_pages'] == TOTAL_PAGES >= 3, '必须有多页'
    assert (TARGET_LOCAL_ID, TARGET_CREATE_TIME) not in _ids(base), \
        '目标必须**不在**第 1 页，否则本文件的断言会退化成恒真'
    # 重号诱饵确实在第 1 页（否则"只用 local_id 会命中它"这个判别点不成立）
    assert (TARGET_LOCAL_ID, DECOY_CREATE_TIME) in _ids(base)
    # 目标确实在第 3 页（手算）
    p3 = query_messages(decrypted, CHAT, wxid='wxid_owner', page=TARGET_PAGE, per_page=PER_PAGE)
    assert (TARGET_LOCAL_ID, TARGET_CREATE_TIME) in _ids(p3)


# ===========================================================================
# 1. 引擎层：focus 选中那一页
# ===========================================================================

def test_focus_returns_the_page_containing_the_target(decrypted):
    r = query_messages(decrypted, CHAT, wxid='wxid_owner', page=1, per_page=PER_PAGE,
                       focus_local_id=TARGET_LOCAL_ID, focus_create_time=TARGET_CREATE_TIME)
    assert r['focused']['found'] is True
    assert r['focused']['page'] == TARGET_PAGE, '页号必须是算出来的，不是传进来的'
    assert r['focused']['local_id'] == TARGET_LOCAL_ID
    assert r['focused']['create_time'] == TARGET_CREATE_TIME
    assert (TARGET_LOCAL_ID, TARGET_CREATE_TIME) in _ids(r), '返回的那一页必须真的含目标'
    assert r['pagination']['page'] == TARGET_PAGE


def test_focus_result_is_identical_to_the_plain_page(decrypted):
    """focus **只选页**：与不带 focus 的同一页逐条相同（分页语义不变）。"""
    focused = query_messages(decrypted, CHAT, wxid='wxid_owner', page=1, per_page=PER_PAGE,
                             focus_local_id=TARGET_LOCAL_ID,
                             focus_create_time=TARGET_CREATE_TIME)
    plain = query_messages(decrypted, CHAT, wxid='wxid_owner', page=TARGET_PAGE,
                           per_page=PER_PAGE)
    assert _ids(focused) == _ids(plain)
    assert focused['messages'] == plain['messages']
    assert focused['pagination'] == plain['pagination']


def test_focus_ignores_the_page_argument(decrypted):
    """给了 focus 时 `page` 被忽略（否则"点进去落在第 1 页"会重新出现）。"""
    r = query_messages(decrypted, CHAT, wxid='wxid_owner', page=1, per_page=PER_PAGE,
                       focus_local_id=TARGET_LOCAL_ID, focus_create_time=TARGET_CREATE_TIME)
    assert r['pagination']['page'] == TARGET_PAGE


def test_focus_matches_local_id_AND_create_time(decrypted):
    """同 local_id 的另一条（第 1 页）不得被误当成目标。

    只用 `local_id` 定位的实现会命中诱饵 → page=1 → 本条变红。
    """
    decoy = query_messages(decrypted, CHAT, wxid='wxid_owner', page=1, per_page=PER_PAGE,
                           focus_local_id=TARGET_LOCAL_ID,
                           focus_create_time=DECOY_CREATE_TIME)
    assert decoy['focused']['found'] is True
    assert decoy['focused']['page'] == DECOY_PAGE
    assert (TARGET_LOCAL_ID, DECOY_CREATE_TIME) in _ids(decoy)

    target = query_messages(decrypted, CHAT, wxid='wxid_owner', page=1, per_page=PER_PAGE,
                            focus_local_id=TARGET_LOCAL_ID,
                            focus_create_time=TARGET_CREATE_TIME)
    assert target['focused']['page'] == TARGET_PAGE
    assert target['focused']['page'] != decoy['focused']['page']


def test_focus_found_is_false_with_explicit_reason(decrypted):
    """消息不在该会话里：**不得**静默当成"定位成功"。"""
    r = query_messages(decrypted, CHAT, wxid='wxid_owner', page=1, per_page=PER_PAGE,
                       focus_local_id=MISSING_LOCAL_ID,
                       focus_create_time=MISSING_CREATE_TIME)
    assert r['focused']['found'] is False
    assert r['focused']['page'] == 1, 'found=False 仍要给一个合理页号'
    assert r['focused']['reason'], '必须给出机器可读原因'
    assert r['focused']['message'], '必须给出人话文案（不得静默）'
    # 未定位成功时仍返回一个可用的页（不是空列表/异常状态）
    plain1 = query_messages(decrypted, CHAT, wxid='wxid_owner', page=1, per_page=PER_PAGE)
    assert _ids(r) == _ids(plain1)


def test_focus_respects_the_same_filtered_row_set(decrypted):
    """focus 在**筛选后**的结果集里定位（与"同一页逐条相同"一致）。"""
    # keyword 只留不含 target 的行 → 目标被筛掉 → found=False（而不是跑到另一页）
    r = query_messages(decrypted, CHAT, wxid='wxid_owner', page=1, per_page=PER_PAGE,
                       keyword='synthetic decoy',
                       focus_local_id=TARGET_LOCAL_ID, focus_create_time=TARGET_CREATE_TIME)
    assert r['focused']['found'] is False


def test_focus_with_large_per_page_clamps_to_page_1(decrypted):
    r = query_messages(decrypted, CHAT, wxid='wxid_owner', page=1, per_page=200,
                       focus_local_id=TARGET_LOCAL_ID, focus_create_time=TARGET_CREATE_TIME)
    assert r['focused']['found'] is True
    assert r['focused']['page'] == 1
    assert (TARGET_LOCAL_ID, TARGET_CREATE_TIME) in _ids(r)


def test_no_focus_keeps_the_existing_response_shape(decrypted):
    """向后兼容（引擎层）：不给 focus 时**不得**多出 `focused` 键。"""
    r = query_messages(decrypted, CHAT, wxid='wxid_owner', page=1, per_page=PER_PAGE)
    assert set(r.keys()) == {'messages', 'pagination'}
    assert set(r['pagination'].keys()) == {'page', 'per_page', 'total', 'total_pages'}


# ===========================================================================
# 2. HTTP 契约层
# ===========================================================================

def _get(client, **params):
    qs = '&'.join('%s=%s' % (k, v) for k, v in params.items())
    return client.get('/api/messages?%s' % qs)


def test_api_focus_returns_the_target_page(client):
    r = _get(client, chat_id=CHAT, page=1, per_page=PER_PAGE,
             focus_local_id=TARGET_LOCAL_ID, focus_create_time=TARGET_CREATE_TIME)
    assert r.status_code == 200
    body = r.get_json()
    assert body['focused']['found'] is True
    assert body['focused']['page'] == TARGET_PAGE
    assert (TARGET_LOCAL_ID, TARGET_CREATE_TIME) in \
        [(m['id'], m['create_time']) for m in body['messages']]


def test_api_focus_response_equals_plain_page_plus_focused_block(client):
    focus = _get(client, chat_id=CHAT, page=1, per_page=PER_PAGE,
                 focus_local_id=TARGET_LOCAL_ID, focus_create_time=TARGET_CREATE_TIME).get_json()
    plain = _get(client, chat_id=CHAT, page=TARGET_PAGE, per_page=PER_PAGE).get_json()
    assert {k: v for k, v in focus.items() if k != 'focused'} == plain, \
        'focus 只能**加**一个 focused 块，其余字段必须逐字段相同'


def test_api_without_focus_has_no_focused_key(client):
    body = _get(client, chat_id=CHAT, page=3, per_page=PER_PAGE).get_json()
    assert 'focused' not in body
    assert set(body.keys()) == {'messages', 'pagination'}


def test_api_focus_not_found_is_explicit(client):
    body = _get(client, chat_id=CHAT, page=1, per_page=PER_PAGE,
                focus_local_id=MISSING_LOCAL_ID,
                focus_create_time=MISSING_CREATE_TIME).get_json()
    assert body['focused']['found'] is False
    assert body['focused']['reason'] == 'not_in_chat'
    assert isinstance(body['focused']['message'], str) and body['focused']['message']
    assert body['pagination']['page'] == 1


@pytest.mark.parametrize('params,why', [
    ({'focus_local_id': TARGET_LOCAL_ID}, '只给了 local_id'),
    ({'focus_create_time': TARGET_CREATE_TIME}, '只给了 create_time'),
    ({'focus_local_id': 'abc', 'focus_create_time': TARGET_CREATE_TIME}, 'local_id 非整数'),
    ({'focus_local_id': TARGET_LOCAL_ID, 'focus_create_time': 'xyz'}, 'create_time 非整数'),
    ({'focus_local_id': '1.5', 'focus_create_time': TARGET_CREATE_TIME}, 'local_id 是小数'),
    ({'focus_local_id': '', 'focus_create_time': TARGET_CREATE_TIME}, 'local_id 是空串'),
    ({'focus_local_id': '-1', 'focus_create_time': TARGET_CREATE_TIME}, 'local_id 是负数'),
    ({'focus_local_id': TARGET_LOCAL_ID, 'focus_create_time': ''}, 'create_time 是空串'),
])
def test_api_invalid_focus_returns_400(client, params, why):
    q = dict(chat_id=CHAT, page=1, per_page=PER_PAGE)
    q.update(params)
    r = _get(client, **q)
    assert r.status_code == 400, '%s 必须 400（不得静默忽略）' % why
    body = r.get_json()
    assert body.get('error'), '400 必须带错误说明'


def test_api_valid_focus_boundary_zero_is_accepted(client):
    """0 是合法整数（只是不会命中）→ 200 + found=False，而不是 400。"""
    body = _get(client, chat_id=CHAT, page=1, per_page=PER_PAGE,
                focus_local_id=0, focus_create_time=0).get_json()
    assert body['focused']['found'] is False
    assert body['focused']['page'] == 1


def test_api_focus_on_unknown_chat_is_still_404(client):
    r = _get(client, chat_id='wxid_does_not_exist', page=1, per_page=PER_PAGE,
             focus_local_id=TARGET_LOCAL_ID, focus_create_time=TARGET_CREATE_TIME)
    assert r.status_code == 404


def test_api_plain_request_response_is_unchanged(client):
    """回归护栏：不带 focus 的请求体与旧契约逐字段相同（含 404 形态）。"""
    ok = _get(client, chat_id=CHAT, page=2, per_page=PER_PAGE).get_json()
    assert set(ok.keys()) == {'messages', 'pagination'}
    assert set(ok['pagination'].keys()) == {'page', 'per_page', 'total', 'total_pages'}
    assert ok['pagination']['page'] == 2
    bad = client.get('/api/messages')
    assert bad.status_code == 400
