"""全局搜索的 HTTP 层（Task 7）—— `/api/search` 三个端点 + `/search` 页面。

全部用**合成**夹具（`tests/test_search_index.py` 的 `_make_shard` / `_make_fts_db`），
绝不读取真实微信数据、`backup/`、也不含真实姓名/wxid/手机号/正文/密钥。

控制方增补 T7-A1–T7-A7 的判别性覆盖点在此逐条落地：
  * T7-A1 索引坏了必须是**结构化 5xx**，绝不能退化成「200 + 空结果」
  * T7-A2 整个 body / 整个 status 原样透传；`text_ready` 由本层显式计算
  * T7-A3 `/status` 不得付逐表 COUNT(*)/MAX(create_time) 的代价
  * T7-A4 构建失败要推 SSE error 事件、且状态不得报 ready；并发构建要 409
  * T7-A6 `own_wxid` 不得变成隐性「发送者=我」筛选（Critical #16 回归护栏）
  * T7-A7 页面脚本加载顺序必须**恰好**是那 5 个文件
"""
import os
import re
import sqlite3
import sys
import threading

import pytest
from werkzeug.test import create_environ

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

from web.app import create_app
from web.routes import search_api as api_module
from tests.test_search_index import _make_shard, _make_fts_db
from engine.services import media as media_mod
from engine.services import search_index as si
from engine.services import search as search_mod
from engine.services.search import search_messages
from engine.services.search_query import TYPE_ALIASES

# T7-A7：`search.html` 必须按这个顺序引入；后两个文件由 Task 9 创建。
#
# ⚠️ **只定义一处**（2026-09-22 收敛）：顺序常量的事实源在 `tests/test_search_js.py`，
# 这里改为**导入**而不是再抄一份 —— 本项目已被"两处定义同一契约"咬过多次
# （`to_char_token_text` 的口径、`statusNotice` 的判据都出过这种问题）。
# `tests/test_search_page.py` 也是同样导入，所以三处断言共用同一个元组，漂移在结构上不可能发生。
from tests.test_search_js import SCRIPT_ORDER  # noqa: E402

# T7-A7①：类型下拉的 12 个规范名（与 TYPE_ALIASES「每基类型取首个别名」一致）
CANONICAL_TYPE_NAMES = ('文本', '图片', '文件', '语音', '名片', '视频',
                        '表情', '位置', '链接', '网络电话', '系统', '撤回')

# T7-A1：`/api/search` 必须原样透传的引擎契约键（T7-A2 表格 + spec 第 7 节）
ENGINE_BODY_KEYS = (
    'results', 'total', 'page', 'per_page', 'total_pages', 'parsed', 'index',
    'used_fallback', 'regex_degraded', 'scan_mode', 'elapsed_ms', 'slow_query_hint',
    'candidate_rows', 'sender_filter_unsupported', 'fallback_text_only', 'warnings',
    # T16：显式截断的两个结构化字段（少一个 UI 就分不清"没有匹配"和"超出保留范围"）
    'truncated', 'retained_rows',
)

# 合成正文：`维修服务器`（个人会话）与 `群里的语音`（群会话）
_FTS_ROWS = [
    ('维修服务器', 1, 1600000000000, 1, 1, 1, 1600000000),
    ('群里的语音', 1, 1600000300000, 1, 2, 2, 1600000300),
]
_SHARD_CHATS = [
    ('wxid_alpha', [(1, 1, 1600000000, 1)]),
    ('111@chatroom', [(1, 1, 1600000300, 2)]),
]


def _canonical_type_names():
    """与实现**同一条**推导：TYPE_ALIASES 里每个基类型取第一个别名。

    测试里也跑一遍，是为了避免「另写一份硬编码列表」——两份列表必然漂移。
    """
    seen, out = set(), []
    for name, base in TYPE_ALIASES.items():
        if base in seen:
            continue
        seen.add(base)
        out.append(name)
    return out


def _make_decrypted(root):
    d = root / 'dec'
    (d / 'message').mkdir(parents=True)
    _make_shard(str(d / 'message' / 'message_0.db'), _SHARD_CHATS)
    _make_fts_db(os.path.join(str(d), 'message', 'message_fts.db'), _FTS_ROWS)
    return str(d)


@pytest.fixture(autouse=True)
def _build_lock_is_never_leaked():
    """T7-A4 的构建锁是**模块级全局状态** —— 用例之间会互相污染。

    这条夹具把"锁被漏在已持有状态"变成**确定性失败**（并指明是上一个用例泄漏的），
    而不是让下一个用例偶发地收到 409。偶发失败正是本文件被并行任务报"随机失败"
    的那类噪声：它把真回归淹掉。
    """
    assert api_module._BUILD_LOCK.locked() is False, \
        '用例开始时构建锁已被持有 —— 上一个用例泄漏了 _BUILD_LOCK'
    yield
    assert api_module._BUILD_LOCK.locked() is False, \
        '用例结束时构建锁仍被持有 —— 会把 409 泄漏给下一个用例'


@pytest.fixture
def app_env(tmp_path):
    return _make_decrypted(tmp_path)


@pytest.fixture
def client(app_env):
    app = create_app(app_env, wxid='wxid_owner')
    app.config['TESTING'] = True
    return app.test_client()


@pytest.fixture
def no_wxid_detection(monkeypatch):
    """屏蔽 `_detect_wxid` —— 这不是"以防万一"，而是本机**必须**做的。

    实测（合成夹具目录）：`_detect_wxid(<tmp>/dec)` 返回本机 `D:\\xwechat_files`
    里真实存在的**24 字符账号目录名**（`D:\\xwechat_files` 实测存在）。也就是说
    `create_app(app_env, wxid=None)` 得到的 `WXID` **不是 None**，而是真实账号、
    还读了用户目录。

    后果有两层，两层都必须堵住：
      * 测试会读真实微信数据、结果随机器变化（违反"绝不使用真实数据"）；
      * T7-A6 的对照会退化成「带 wxid 的 app 与**同样带 wxid** 的 app 相比」——
        一条永远通过、什么也没测的空转断言（本轮第三次同族教训）。
    """
    monkeypatch.setattr(media_mod, '_detect_wxid', lambda decrypted_dir: None)


def _app_and_client(app_env, wxid):
    app = create_app(app_env, wxid=wxid)
    app.config['TESTING'] = True
    return app, app.test_client()


def _client_for(app_env, wxid):
    return _app_and_client(app_env, wxid)[1]


def _search(client, q, **params):
    params['q'] = q
    return client.get('/api/search', query_string=params)


def _sse(client, data=None):
    r = client.post('/api/search/index', json=(data if data is not None else {}))
    return r, r.data.decode('utf-8')


# ==========================================================================
# GET /api/search/status
# ==========================================================================

class TestStatusEndpoint:
    def test_status_before_build(self, client):
        body = client.get('/api/search/status').get_json()
        assert body['ready'] is False
        assert 'index_path' in body
        # 索引不存在时这几个开关也必须是**真布尔**（不是 None / 不是缺键 / 不是 0）
        assert body['text_ready'] is False and isinstance(body['text_ready'], bool)
        assert body['incomplete'] is True and isinstance(body['incomplete'], bool)
        assert isinstance(body['incomplete_reasons'], list) and body['incomplete_reasons']

    def test_status_after_build(self, client, app_env):
        si.build_index(app_env)
        body = client.get('/api/search/status').get_json()
        assert body['ready'] is True and body['fts_rows'] == 2

    def test_status_passes_engine_status_through_verbatim(self, client, app_env):
        """T7-A2：`index_status()` 的**全部**键都要在响应里（不挑字段）。"""
        si.build_index(app_env)
        body = client.get('/api/search/status').get_json()
        engine = si.index_status(app_env)
        assert set(body) == set(engine) | {'index_path', 'text_ready',
                                           'incomplete', 'incomplete_reasons'}
        assert body['meta_rows'] == engine['meta_rows']
        assert body['msg_text_rows'] == engine['msg_text_rows']
        assert body['meta_coverage'] == engine['meta_coverage']
        assert body['fts_coverage'] == engine['fts_coverage']
        assert body['source_shards'] == engine['source_shards']
        assert body['schema_ok'] is True and body['stale'] is False
        assert 'fts_skipped_no_chat' in body and 'duplicates_dropped' in body
        # UI 要直接拿这几个开关做判断 → 必须是**真布尔**（`is True/False`，不是 0/1/None）
        for key in ('ready', 'text_ready', 'incomplete', 'stale', 'schema_ok', 'exists'):
            assert isinstance(body[key], bool), key
        assert isinstance(body['incomplete_reasons'], list)
        assert isinstance(body['meta_rows'], int) and isinstance(body['fts_rows'], int)
        assert isinstance(body['meta_coverage'], float)

    def test_text_ready_true_for_text_index(self, client, app_env):
        si.build_index(app_env)
        body = client.get('/api/search/status').get_json()
        assert body['text_ready'] is True

    def test_text_ready_false_for_meta_only_build(self, client, app_env):
        """T7-A2：`build_index(text=False)` 后 `ready` 仍为 True，但文本搜索是空的。

        `text_ready = schema_ok and fts_rows` —— 这正是本层必须显式暴露的判据，
        用 `ready` 判断会漏掉这个静默假阴性（关键词 MATCH 空表 → 0 条）。
        """
        si.build_index(app_env, text=False, meta=True, force=True)
        body = client.get('/api/search/status').get_json()
        assert body['ready'] is True
        assert body['fts_rows'] == 0 and body['meta_rows'] == 2
        assert body['text_ready'] is False

    def test_status_reports_incompleteness_without_flagging_legal_coverage_gap(
            self, client, app_env):
        """T7-A2 口径：`fts_coverage < 1` 是**合法**差额（无正文消息），不得算作不完整。"""
        si.build_index(app_env)
        body = client.get('/api/search/status').get_json()
        assert body['meta_coverage'] == 1.0
        assert body['incomplete'] is False
        assert body['incomplete_reasons'] == []

    def test_status_does_not_pay_for_full_table_scan(self, client, app_env, monkeypatch):
        """T7-A3（Critical）：`/status` 一旦逐表 `COUNT(*)/MAX(create_time)` 就极慢。

        真机实测 13.4s（冷）/ 1.40s（热），而搜索页每次加载都调它。这里让那个
        昂贵函数**被调用即抛异常**：热路径上一旦有人把它加回来，这条测试立刻红。
        """
        si.build_index(app_env)

        def boom(shards):
            raise AssertionError('_source_stats 被 /status 调用了：热路径不允许全表扫描')

        monkeypatch.setattr(si, '_source_stats', boom)
        r = client.get('/api/search/status')
        assert r.status_code == 200
        body = r.get_json()
        for key in ('schema_ok', 'stale', 'ready', 'text_ready', 'source_shards',
                    'meta_rows', 'fts_rows', 'meta_coverage', 'fts_coverage'):
            assert key in body, key


# ==========================================================================
# GET /api/search
# ==========================================================================

class TestSearchEndpoint:
    def test_without_index_uses_fallback(self, client):
        r = _search(client, '维修')
        assert r.status_code == 200
        body = r.get_json()
        assert body['used_fallback'] is True
        assert body['scan_mode'] == 'fallback'
        assert body['total'] == 1

    def test_with_index(self, client, app_env):
        si.build_index(app_env)
        body = _search(client, '维修').get_json()
        assert body['used_fallback'] is False
        assert body['scan_mode'] == 'fts'
        assert body['total'] == 1
        hit = body['results'][0]
        assert hit['chat_id'] == 'wxid_alpha'
        assert hit['chat_display_name']
        assert hit['type_label'] == '文本'
        assert isinstance(hit['match_spans'], list)

    def test_empty_query_is_400(self, client):
        r = _search(client, '')
        assert r.status_code == 400
        assert 'error' in r.get_json()

    def test_missing_q_is_400(self, client):
        assert client.get('/api/search').status_code == 400

    def test_bad_type_is_400_with_parsed_errors(self, client):
        r = _search(client, '类型:彩虹')
        assert r.status_code == 400
        assert r.get_json()['parsed']['errors']

    def test_pagination_params(self, client, app_env):
        si.build_index(app_env)
        body = _search(client, '维修', per_page=1, page=1).get_json()
        assert body['per_page'] == 1
        assert body['page'] == 1

    def test_invalid_sort_falls_back_to_default(self, client, app_env):
        si.build_index(app_env)
        body = _search(client, '维修', sort='bogus').get_json()
        assert body['results'] is not None
        assert body['total'] == 1

    def test_body_is_passed_through_verbatim(self, client, app_env):
        """T7-A2：不是挑字段，而是整个 body 原样出去（少一个键 UI 就没法提示）。"""
        si.build_index(app_env)
        direct = search_messages(app_env, '维修', own_wxid='wxid_owner')
        assert isinstance(direct, dict) and direct['total'] > 0   # 不是两个 error dict 在比
        body = _search(client, '维修').get_json()
        assert set(body) == set(direct)
        for key in ENGINE_BODY_KEYS:
            assert key in body, key
        assert body['results'] == direct['results']
        assert body['total'] == direct['total']
        assert body['per_page'] == direct['per_page']
        assert body['total_pages'] == direct['total_pages']
        assert body['parsed'] == direct['parsed']
        assert body['warnings'] == direct['warnings']
        assert body['index'] == direct['index']

    def test_new_contract_fields_have_the_right_types(self, client, app_env):
        """T7-A2：这些键必须**存在且类型正确**（不是缺键/undefined）。"""
        si.build_index(app_env)
        body = _search(client, '维修').get_json()
        assert body['scan_mode'] in ('fts', 'filter', 'fallback')
        assert isinstance(body['elapsed_ms'], float) and body['elapsed_ms'] >= 0
        assert body['slow_query_hint'] is None or isinstance(body['slow_query_hint'], str)
        assert isinstance(body['candidate_rows'], int)
        assert body['sender_filter_unsupported'] is False
        assert body['fallback_text_only'] is False
        assert isinstance(body['warnings'], list)
        assert isinstance(body['regex_degraded'], bool)
        assert isinstance(body['used_fallback'], bool)
        assert isinstance(body['index'], dict)

    def test_400_survives_a_raising_second_parse(self, client, monkeypatch):
        """400 分支里的二次 `parse_query` **不得**把 400 变成 500。

        计划缺陷（progress.md:134-143）：`parse_query('类型:²')` 曾从 `int()` 逃逸，
        而 400 分支为了附带 `parsed` 会**再**调一次 —— 二次抛错就成了不透明的 500。
        `parse_query` 的契约是"任何输入都不抛"，所以让解析器抛错只可能是回归；
        这里注入一个抛错的解析器，断言契约（400）仍然成立。

        ⚠️ 前置条件自检（Task 8 §0.8 的同族教训：**补丁没生效的用例会变成空转**，
        或者报出看不懂的断言失败）：记录被调用情况，断言路由**真的调用了**被 patch
        的那个函数、且传的就是原查询串 —— 否则说明 patch 的目标对象不是路由所用的那个。
        """
        from web.routes import search_api as api_module

        calls = []

        def boom(q):
            calls.append(q)
            raise ValueError('synthetic parser regression')

        monkeypatch.setattr(api_module, 'parse_query', boom)
        r = _search(client, '类型:彩虹')

        assert calls == ['类型:彩虹'], \
            '路由没有调用被 patch 的 parse_query（patch 目标与你以为的不是同一个对象）'
        assert r.status_code == 400
        assert 'error' in r.get_json()
        assert r.get_json()['parsed'] is None       # 解析器回归时也不得把 400 变 500

    def test_broken_index_is_structured_5xx_never_empty_200(self, client, app_env):
        """T7-A1（Critical）：索引坏了 → 结构化 5xx，**绝不能**是「200 + 空结果」。

        配方：把 `message_fts` 换成**同名的普通表**（`test_search.py` 已锁定的同一
        手法）。此时 `index_status.schema_ok` 仍为 True、`_probe_text_index` 也过，
        但 `message_fts MATCH ?` 会抛 `no such column: message_fts` —— 一个真实的
        「索引库坏了」现场（旧实现把它吞成 0 结果，用户看到「没有找到相关消息」）。
        """
        si.build_index(app_env)
        con = sqlite3.connect(si.index_path(app_env))
        try:
            con.execute('DROP TABLE message_fts')
            con.execute('CREATE TABLE message_fts (rowid INTEGER PRIMARY KEY, x TEXT)')
            con.execute("INSERT INTO message_fts (rowid, x) VALUES (1, 'zz')")
            con.commit()
        finally:
            con.close()
        assert si.index_status(app_env)['ready'] is True      # 状态仍以为索引可用

        r = _search(client, '维修')
        assert r.status_code != 200
        assert r.status_code >= 500
        body = r.get_json()
        assert body['error']
        assert body['detail'] and 'message_fts' in body['detail']
        assert body['hint']
        # 显式排除本增补要防的那种形态
        assert not (r.status_code == 200 and body.get('results') == [])

    def test_dropped_fts_table_degrades_instead_of_erroring(self, client, app_env):
        """T7-A1 原始配方（只 DROP `message_fts`）在本引擎上**不是**错误路径。

        实测：`_probe_text_index` 会发现表没了 → 关掉索引连接 → 降级直扫
        `message_fts_v4_*_content`（Task 6 的 T5-A8 设计：功能可用性优先）。
        这是**真实的正确行为**，所以这里钉住它，并让上面那条测试负责错误路径。
        """
        si.build_index(app_env)
        con = sqlite3.connect(si.index_path(app_env))
        try:
            con.execute('DROP TABLE message_fts')
            con.commit()
        finally:
            con.close()

        r = _search(client, '维修')
        assert r.status_code == 200
        body = r.get_json()
        assert body['used_fallback'] is True
        assert body['scan_mode'] == 'fallback'
        assert body['total'] == 1              # 真实命中仍在，不是静默 0 条

    def test_keyword_query_on_meta_only_index_degrades_with_warnings(self, client, app_env):
        """T7-A2 追加项：文本索引为空时关键词查询**不得**是「200 + 空结果」。

        控制方更正版口径：正确行为是降级直扫（`used_fallback=True` + `warnings`
        留痕 + 真实命中），**不是**报错。
        """
        si.build_index(app_env, text=False, meta=True, force=True)
        body = _search(client, '维修').get_json()
        assert body['used_fallback'] is True
        assert body['scan_mode'] == 'fallback'
        assert body['warnings'], '降级原因必须留痕，不能静默'
        assert body['total'] == 1
        assert body['results'][0]['snippet']


# ==========================================================================
# T16：显式截断的两个结构化字段必须原样透传（HTTP 层不做字段白名单）
# ==========================================================================

class TestTruncationFieldsGoThroughVerbatim:
    """判据：把引擎返回体换成一个"确实被截断"的 body，断言响应 JSON 与它**逐键相等**。

    一旦本层变成"挑字段"，`truncated` / `retained_rows` 就会被静默丢掉 ——
    页面于是只能显示"没有匹配的消息"，那正是本轮明令禁止的静默截断。
    """

    def test_truncated_body_survives_the_route_untouched(self, client, monkeypatch):
        from web.routes import search_api as api_module

        truncated_body = {
            'results': [],
            'total': 1018563,
            'page': 500,
            'per_page': 50,
            'total_pages': 20372,
            'parsed': {'errors': []},
            'index': {'ready': True},
            'used_fallback': False,
            'regex_degraded': False,
            'scan_mode': 'filter',
            'elapsed_ms': 1234.5,
            'slow_query_hint': None,
            'candidate_rows': 1018918,
            'sender_filter_unsupported': False,
            'fallback_text_only': False,
            'truncated': True,
            'retained_rows': 50000,
            'warnings': ['结果集过大（共 1018563 条），只保留了前 50000 条用于分页'],
        }

        calls = []

        def fake(decrypted_dir, q, **kw):        # 前置条件自检：patch 目标真的被调用
            calls.append(q)
            return dict(truncated_body)

        monkeypatch.setattr(api_module, 'search_messages', fake)
        body = _search(client, '-退货').get_json()
        assert calls == ['-退货'], '路由没有调用被 patch 的 search_messages（patch 目标不对）'
        assert body == truncated_body
        assert body['truncated'] is True and body['retained_rows'] == 50000
        assert body['total'] == 1018563           # total 不许被截断污染

    def test_untrucated_response_reports_false_and_no_warning(self, client, app_env):
        """**防误报**：夹具上的普通查询不许出现任何截断迹象。"""
        si.build_index(app_env)
        body = _search(client, '维修').get_json()
        assert body['truncated'] is False
        assert body['retained_rows'] == body['total'] == 1
        assert not [w for w in body['warnings'] if '只保留了前' in w]


# ==========================================================================
# T7-A6：`own_wxid` 不得变成隐性筛选（Critical #16 回归护栏）
# ==========================================================================

class TestOwnWxidIsNotAnImplicitFilter:
    """HTTP 契约层的护栏：本夹具**总是**带 `own_wxid`（`create_app(wxid=...)`）。

    真实事故（Critical #16）：`_resolve_scope` 曾无条件把 `own_wxid` 并进
    `scope['senders']`，而索引里自己发的消息 `sender_username` 为空 →
    **任何**查询都被隐性加上「发送者=我」→ 全部 0 条，且 `warnings` 为空。
    """

    QUERIES = ('维修', '类型:文本', '日期:2020')

    def _totals(self, client):
        out = {}
        for q in self.QUERIES:
            r = _search(client, q)
            assert r.status_code == 200, (q, r.status_code)
            out[q] = r.get_json()
        return out

    def test_identical_totals_with_and_without_own_wxid(self, app_env, no_wxid_detection):
        si.build_index(app_env)
        with_app, with_client = _app_and_client(app_env, 'wxid_owner')
        without_app, without_client = _app_and_client(app_env, None)
        # ① 先证明两条对照**确实不同**（否则这是"自己和自己比"的空转断言：
        #    `wxid or _detect_wxid(...)` 会把 None 自动填成真实账号目录名）
        assert with_app.config['WXID'] == 'wxid_owner'      # 非空
        assert without_app.config['WXID'] is None           # 真 None
        assert with_app.config['WXID'] != without_app.config['WXID']
        # ② 再证明样本**非空**（否则"两边都是 0"也会通过）
        with_wxid = self._totals(with_client)
        without = self._totals(without_client)
        for q in self.QUERIES:
            assert with_wxid[q]['total'] > 0, q
            assert with_wxid[q]['used_fallback'] is False
        # ③ 最后才是"完全相同"
        assert {q: b['total'] for q, b in with_wxid.items()} == \
               {q: b['total'] for q, b in without.items()}

    def test_both_requests_really_carry_different_own_wxid(
            self, app_env, monkeypatch, no_wxid_detection):
        """证明"两条路径确实不同"：记录引擎实际收到的 `own_wxid`。

        只断言两个 app 的 `config['WXID']` 不同还不够 —— 有可能 HTTP 层压根没把它
        传下去（那样"total 相同"同样是空转）。这里用 `_resolve_scope` 的 spy 直接
        看引擎收到了什么：必须是 `{'wxid_owner', None}` 两个**不同**的值。
        """
        si.build_index(app_env)
        seen = []
        real = search_mod._resolve_scope

        def spy(decrypted_dir, parsed, own_wxid):
            seen.append(own_wxid)
            return real(decrypted_dir, parsed, own_wxid)

        monkeypatch.setattr(search_mod, '_resolve_scope', spy)
        for wxid in ('wxid_owner', None):
            assert _search(_client_for(app_env, wxid), '维修').status_code == 200
        assert seen == ['wxid_owner', None]        # 真的分别传了不同的上下文
        assert len(set(seen)) == 2                 # 而它们**不相同**

    def test_no_sender_wording_when_query_has_no_sender_filter(
            self, app_env, no_wxid_detection):
        si.build_index(app_env)
        for wxid in ('wxid_owner', None):
            for q in self.QUERIES:
                body = _search(_client_for(app_env, wxid), q).get_json()
                assert body['parsed']['senders'] == []
                assert not [w for w in body['warnings'] if '发送者' in w]
                assert body['sender_filter_unsupported'] is False

    def test_guard_is_not_vacuous(self, app_env, monkeypatch, no_wxid_detection):
        """把 #16 的 bug 注回去 → `total` 必须**变**，证明上面的「完全相同」有判别力。

        否则「两个 app 的 total 相同」可能只是因为 own_wxid 根本没被传下去。
        """
        si.build_index(app_env)
        baseline = _search(_client_for(app_env, 'wxid_owner'), '维修').get_json()['total']
        assert baseline == 1

        real = search_mod._resolve_scope

        def buggy(decrypted_dir, parsed, own_wxid):
            scope = real(decrypted_dir, parsed, own_wxid)
            if own_wxid:
                scope['senders'] = set(scope['senders']) | {own_wxid}
            return scope

        monkeypatch.setattr(search_mod, '_resolve_scope', buggy)
        mutated = _search(_client_for(app_env, 'wxid_owner'), '维修').get_json()['total']
        assert mutated != baseline
        assert mutated == 0        # #16 的现场：任何查询归零
        # 而没有 wxid 的那条通路不受影响 → 两条通路**本应**相同，bug 让它们分叉
        assert _search(_client_for(app_env, None), '维修').get_json()['total'] == baseline


# ==========================================================================
# POST /api/search/index（SSE）
# ==========================================================================

class TestBuildEndpoint:
    def test_build_streams_sse_and_creates_index(self, client, app_env):
        r, payload = _sse(client, {'text': True, 'meta': True})
        assert r.status_code == 200
        assert r.mimetype == 'text/event-stream'
        assert 'event: done' in payload
        assert si.index_status(app_env)['ready'] is True
        assert si.index_status(app_env)['fts_rows'] == 2

    def test_refresh_action(self, client, app_env):
        si.build_index(app_env)
        r, payload = _sse(client, {'action': 'refresh'})
        assert r.status_code == 200
        assert 'event: error' not in payload
        assert si.index_status(app_env)['ready'] is True

    def test_failure_streams_error_event_and_status_is_not_ready(self, client, app_env,
                                                                monkeypatch):
        """T7-A4：构建抛错时 SSE 必须有 `error` 事件，且状态不得报 `ready=True`。

        让真实 `build_index` 在**第一处昂贵步骤**（建库之前）抛错。
        """
        def boom(shards):
            raise RuntimeError('synthetic build failure')

        monkeypatch.setattr(si, '_source_stats', boom)
        # 前置条件自检：patch 必须落在**路由真正调用的那个模块对象**上，
        # 否则这条用例会因为"补丁没生效"而空转/报出看不懂的失败（Task 8 同族教训）
        assert api_module.search_index is si
        r, payload = _sse(client, {'text': True, 'meta': True})
        assert r.status_code == 200
        assert 'event: error' in payload
        assert 'synthetic build failure' in payload
        assert 'event: done' not in payload

        body = client.get('/api/search/status').get_json()
        assert body['ready'] is False
        assert body['text_ready'] is False
        assert body['exists'] is False

    def test_done_event_is_single_and_fires_only_when_the_build_is_finished(self, client, app_env):
        """`event: done` 必须是**唯一**的终止事件，且出现时构建已结束、锁已空闲。

        实测到的缺陷（两个，互相放大）：
          1. **过早的 `event: done`**：`build_index` 收尾时会调
             `progress(STAGE_DONE, '完成', 1.0)`，而该 stage 名被我的 `_progress`
             原样透传，`sse.py` 又把 `stage == 'done'` 当成终止事件 → 流里出现
             **两个** `event: done`。客户端的"完成"判据随即失效（UI 会在索引还在
             收尾时就报"完成"）。
          2. **锁在终止事件之后才释放** → "已拿到 done" 之后紧接着发的请求仍可能被
             409 误拒（命中与否取决于 GIL 何时切到工作线程 → 偶发、单独跑难复现）。

        这里直接调 WSGI app 拿**惰性迭代器**，对每个 `done` 事件采样锁状态：
        期望「恰好 1 个 done，且那一刻锁已空闲」。
        """
        environ = create_environ('/api/search/index', method='POST',
                                 json={'text': True, 'meta': True})
        app_iter = client.application(environ, lambda *args: None)
        done_events = []
        try:
            for chunk in app_iter:
                text = chunk.decode('utf-8', 'replace')
                if '\nevent: done\n' in text:
                    done_events.append(api_module._BUILD_LOCK.locked())
        finally:
            close = getattr(app_iter, 'close', None)
            if close:
                close()

        assert len(done_events) == 1, \
            'event: done 出现了 %d 次（构建进度 stage 名不得冒充终止事件）' % len(done_events)
        assert done_events == [False], \
            '终止事件可见时构建锁仍被持有 → 下一个请求会被 409 误拒'
        # 终止事件确实意味着"构建已结束"：那一刻索引已经就绪
        assert si.index_status(app_env)['ready'] is True
        assert api_module._BUILD_LOCK.locked() is False

        # 常规客户端路径同样成立：POST 返回即锁已空闲，可以紧接着再构建一次
        r, payload = _sse(client, {'text': True, 'meta': True})
        assert r.status_code == 200 and 'event: done' in payload
        assert api_module._BUILD_LOCK.locked() is False

    def test_concurrent_build_is_rejected_with_409(self, client, monkeypatch):
        """T7-A4：两个 `POST /index` 同时进来会同时写同一个索引库 → 必须拒绝第二个。

        必须在**另一个线程**里发第一个请求：`client.post()` 会把 SSE 流读到底
        （即等到构建结束），同一个线程里根本构造不出"正在构建中"的时刻。
        """
        entered = threading.Event()
        release = threading.Event()
        finished = {}

        def slow_build(decrypted_dir, **kwargs):
            entered.set()
            release.wait(30)
            return {'meta_rows': 0, 'fts_rows': 0, 'built_at': None}

        def first_request():
            c = client.application.test_client()
            r = c.post('/api/search/index', json={'text': True, 'meta': True})
            finished['status'] = r.status_code
            finished['payload'] = r.get_data(as_text=True)

        monkeypatch.setattr(si, 'build_index', slow_build)
        # 前置条件自检：patch 必须落在**路由真正调用的那个模块对象**上
        assert api_module.search_index is si
        worker = threading.Thread(target=first_request, daemon=True)
        worker.start()
        try:
            assert entered.wait(30), '构建线程没有进入'
            second = client.post('/api/search/index', json={'text': True, 'meta': True})
            assert second.status_code == 409
            body = second.get_json()
            assert body['error']
            assert body['code'] == 'index_build_in_progress'
            # 被拒绝的请求不得启动第二次写入（构建函数只应被调用一次）
            assert entered.is_set()
        finally:
            # **无论断言是否失败都要 join**：否则测试结束时线程还在跑，
            # monkeypatch 会在它运行期间被撤销（读到真实函数/被删的 tmp_path）。
            release.set()
            worker.join(30)
        assert not worker.is_alive()
        assert finished['status'] == 200
        assert 'event: done' in finished['payload']

    def test_lock_is_released_so_the_next_build_works(self, client, app_env):
        """锁必须在构建结束后释放，否则搜索页从此再也不能重建索引。"""
        for _ in range(2):
            r, payload = _sse(client, {'text': True, 'meta': True})
            assert r.status_code == 200
            assert 'event: error' not in payload


# ==========================================================================
# GET /search（页面）
# ==========================================================================

class TestSearchPage:
    def test_page_renders_controls(self, client):
        r = client.get('/search')
        assert r.status_code == 200
        html = r.data.decode('utf-8')
        for token in ('search-input', 'search-syntax', 'search-run',
                      'search-index-bar', 'search-results', 'search-type',
                      'search-chat', 'search-sender'):
            assert token in html, token

    def test_script_load_order_is_exact(self, client):
        """T7-A7：顺序**恰好**是这 5 个 —— 「都包含」对顺序错误无能为力。

        顺序错（或 search-app.js 提前）会出现加载期 ReferenceError，
        整页初始化崩溃，而静态页面断言照样通过（计划缺陷 #15 的孪生风险）。
        """
        html = client.get('/search').data.decode('utf-8')
        order = re.findall(r'js/([A-Za-z0-9_\-]+\.js)', html)
        assert tuple(order) == SCRIPT_ORDER

    def _type_labels(self, html):
        select = re.search(r'<select[^>]*id="search-type"[^>]*>(.*?)</select>', html, re.S)
        assert select, '页面里找不到 #search-type 下拉'
        options = re.findall(r'<option value="([^"]*)">([^<]*)</option>', select.group(1))
        return options

    def test_type_options_are_server_rendered_from_type_aliases(self, client):
        html = client.get('/search').data.decode('utf-8')
        options = self._type_labels(html)
        labels = [value for value, _ in options]
        # ① 12 个规范名全在
        assert tuple(labels) == CANONICAL_TYPE_NAMES
        # ② 与「TYPE_ALIASES 每基类型取首个别名」这条推导一致（不是第二份硬编码列表）
        assert labels == _canonical_type_names()
        # 选项的 value 与可见文本都是**中文规范名**（语法框发的就是中文名）
        assert all(value == text for value, text in options)

    def test_type_options_exclude_synonyms(self, client):
        html = client.get('/search').data.decode('utf-8')
        labels = [value for value, _ in self._type_labels(html)]
        for synonym in ('照片', '音频', '文字', '定位', '应用', '通话', '系统消息',
                        '链接/应用'):
            assert synonym not in labels, synonym

    def test_dashboard_has_entry(self, client):
        assert '/search' in client.get('/').data.decode('utf-8')


# ==========================================================================
# `_index_incompleteness` 第 5 条判据的**存在性守卫**（fix round 2 附）
# ==========================================================================

class TestIncompletenessRowCountGuard:
    """第 5 条判据（`msg_text_rows != fts_rows`）必须有"字段在不在"的守卫。

    跨语言同源夹具 `tests/fixtures/search_status_cases.json` 的 case
    **partial response missing msg_text_rows (known divergence)** 把两侧口径差异钉住了：
    JS 的 `statusNotice` 用 `_has(status, 'msg_text_rows')`（缺失/`None` 不算不一致），
    服务端却直接 `!=` → `None != 877712` 成立 → 报「行数不一致」，并把 **`None`
    打进告警文案**。字段缺失 ≠ 行数不一致：误报会同时误导用户与排查者（`known-issues`
    #21 同族的"看起来有信号、其实是噪声"）。

    ⚠️ **真信号不受影响**（这是本判据不能顺手放宽的边界）：`index_status()` 用
    `_STATUS_INT_KEYS` 把这些字段**预填成 0**，再用 `_meta_int`（`default=0`，异常也
    回落 0，**永不返回 None**）覆盖。所以"老索引缺少该 meta 键"拿到的是 **0** 而不是
    `None` → `0 != fts_rows` **照旧告警**，那才是真信号。本文件为此留了反向断言
    （`test_zero_rows_from_an_old_index_still_alarms`），防止有人把守卫写成"falsy 即放过"。
    """

    @staticmethod
    def _st(**overrides):
        """一条**完整**的 `/status` 载荷（借真机 0.8615 那一格的真实形状）。"""
        st = {
            'exists': True, 'ready': True, 'stale': False, 'schema_ok': True,
            'source_shards': 7, 'source_rows': 1018918,
            'meta_rows': 1018918, 'meta_coverage': 1.0,
            'fts_rows': 877712, 'msg_text_rows': 877712,
            'fts_skipped_no_chat': 0, 'fts_skipped_empty_text': 141206,
            'duplicates_dropped': 0, 'fts_rows_without_meta': 0,
            'built_at': 1600000000, 'text_ready': True,
        }
        st.update(overrides)
        return st

    def test_self_consistent_payload_is_not_incomplete(self):
        """夹具自检：字段齐全且两表一致时**不得**有任何理由（防空转）。"""
        assert api_module._index_incompleteness(self._st()) == (False, [])

    def test_missing_msg_text_rows_is_not_a_row_count_mismatch(self):
        """字段**缺键** → 该条不成立。原实现给 `msg_text(None)…行数不一致`。"""
        st = self._st()
        del st['msg_text_rows']
        incomplete, reasons = api_module._index_incompleteness(st)
        assert (incomplete, reasons) == (False, []), (
            '缺键被当成了"行数不一致"：reasons=%r（这正是 JS 侧 _has 守卫拦下的那类误报）'
            % (reasons,))

    def test_none_msg_text_rows_is_not_a_row_count_mismatch(self):
        """字段显式为 `None`（夹具里就是这一格）→ 同样不成立。"""
        incomplete, reasons = api_module._index_incompleteness(self._st(msg_text_rows=None))
        assert (incomplete, reasons) == (False, []), (
            'None 被当成了"行数不一致"：reasons=%r' % (reasons,))

    def test_real_row_count_mismatch_still_alarms(self):
        """**反空转**：两个数都在场且真的不等 → 必须照旧告警（守卫不得放过真信号）。"""
        incomplete, reasons = api_module._index_incompleteness(self._st(msg_text_rows=800000))
        assert incomplete is True
        assert any('行数不一致' in r and '800000' in r and '877712' in r for r in reasons), reasons

    def test_zero_rows_from_an_old_index_still_alarms(self):
        """`0` 不是"缺失"：老索引没有该 meta 键时 `index_status` 给的就是 0。

        守卫若写成"falsy 即放过"（`if st.get('msg_text_rows') and ...`），这一条会红 ——
        而它代表的正是**真信号**（索引自身不自洽 / 版本过旧）。
        """
        incomplete, reasons = api_module._index_incompleteness(self._st(msg_text_rows=0))
        assert incomplete is True, '0 行被当成"字段缺失"放过了 —— 真信号被守卫吞掉'
        assert any('行数不一致' in r for r in reasons), reasons

    # ---- 镜像方向（裁决 1）：`fts_rows` 必须有**对称**的存在性守卫 ----

    def test_missing_fts_rows_is_not_a_row_count_mismatch(self):
        """镜像：`fts_rows` **缺键** → 该条同样不成立。

        只给 `msg_text_rows` 加守卫时，把字段反过来就复现同一条缺陷
        （`877712 != None` → 报"行数不一致"并把 `None` 打进文案）。
        判据语义是"两个行数不相等"，**任一数缺失都无法判定**，所以必须对称。
        """
        st = self._st()
        del st['fts_rows']
        incomplete, reasons = api_module._index_incompleteness(st)
        assert (incomplete, reasons) == (False, []), (
            'fts_rows 缺键被当成了"行数不一致"：reasons=%r' % (reasons,))

    def test_none_fts_rows_is_not_a_row_count_mismatch(self):
        """镜像：`fts_rows` 显式为 `None` → 同样不成立（JS 侧 `_has` 两个方向都要求非空）。"""
        incomplete, reasons = api_module._index_incompleteness(self._st(fts_rows=None))
        assert (incomplete, reasons) == (False, []), (
            'fts_rows=None 被当成了"行数不一致"：reasons=%r' % (reasons,))

    def test_zero_fts_rows_with_text_rows_still_alarms(self):
        """镜像方向的**反向断言**：`fts_rows == 0` 而文本表有行 → 仍是真不一致。

        这条钉住"只放过 `None`、不放过 `0`"在 **fts_rows 侧**同样成立：
        守卫若写成 `if st.get('fts_rows') and ...`（falsy 即放过），
        `fts_rows=0` 会被当成"缺失"，这条真信号（文本表有行但 FTS 报告 0 行）被吞掉。
        """
        incomplete, reasons = api_module._index_incompleteness(
            self._st(msg_text_rows=877712, fts_rows=0))
        assert incomplete is True, 'fts_rows=0 被当成"字段缺失"放过了 —— 真信号被吞掉'
        assert any('行数不一致' in r and '877712' in r and '0' in r for r in reasons), reasons

    def test_status_response_reasons_never_contain_none(self, client, app_env):
        """端到端：真实 `/status` 的 `incomplete_reasons` 里不得出现 `None` 字样。

        覆盖**两个方向 × 两种缺失形态**（缺键 / `None`）：在真实载荷的基础上分别抽掉
        `msg_text_rows`、`fts_rows`，reasons 里都不许出现 `None`。
        """
        si.build_index(app_env)
        engine = client.get('/api/search/status').get_json()
        assert not [r for r in engine['incomplete_reasons'] if 'None' in r], \
            engine['incomplete_reasons']

        for key in ('msg_text_rows', 'fts_rows'):
            for form in ('missing', 'none'):
                st = {k: v for k, v in engine.items() if k != key}
                if form == 'none':
                    st = dict(st, **{key: None})
                incomplete, reasons = api_module._index_incompleteness(st)
                assert not [r for r in reasons if 'None' in r], (
                    '%s 为 %s 时 reasons 里出现了 None：%r' % (key, form, reasons))
                assert incomplete is False, (
                    '%s 为 %s 时不该判为不完整：reasons=%r' % (key, form, reasons))
