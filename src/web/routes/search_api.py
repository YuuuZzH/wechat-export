"""全局搜索 API（Task 7）—— `/api/search` 的三个端点。

    GET  /api/search          查询：把 `search_messages` 的**整个 body** 原样透传
    GET  /api/search/status   状态：把 `index_status()` 原样透传 + `index_path` / `text_ready`
    POST /api/search/index    SSE 构建 / 刷新索引（进程内单飞锁）

⚠️ 「原样透传」是**契约**而不是省事（T16 之后更关键）：`truncated` / `retained_rows`
    这两个显式截断字段就是靠它出去的。本层**绝不允许**改成字段白名单 ——
    漏一个 `truncated`，页面就只能显示"没有匹配的消息"，一次静默截断当场发生。
    （`tests/test_search_api.py::test_body_is_passed_through_verbatim` 用
    `set(body) == set(直接调用引擎的返回值)` 钉住这一点，并逐键列出 `ENGINE_BODY_KEYS`。）

前缀**只在模块内声明一次**（`Blueprint(..., url_prefix='/api/search')`），因此
`app.py` 注册时**不得**再传 `url_prefix` —— 两处都写会得到
`/api/search/api/search` 而全部 404。本任务选的是「模块内声明」这一条。

异常契约（T7-A1）：
    `ValueError`        查询为空 / 语法错误          → 400 + `parsed`
    `SearchIndexError`  索引或降级路径的 SQL 失败    → `e.http_status`（500）+ `e.as_dict()`
⚠️ 索引失败**绝不能**变成「200 + 空结果」：静默假阴性比报错糟得多。
`TextIndexUnavailableError` 自 Task 6 起**不再被抛出**（引擎改为降级直扫 +
`used_fallback=True` + `warnings`），所以这里不为它写分支；它的父类
`SearchIndexError` 分支已经覆盖它，写了就是死代码。
"""
import os
import sys
import threading

from flask import Blueprint, current_app, jsonify, request

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from engine.services import search_index
from engine.services.search import DEFAULT_PER_PAGE, SearchIndexError, search_messages
from engine.services.search_query import TYPE_ALIASES, parse_query
from web.sse import create_sse_progress, sse_response

search_bp = Blueprint('search_api', __name__, url_prefix='/api/search')

# 同一时刻只允许一个构建：两个 `POST /index` 会同时写同一个索引库
# （进程内锁；多进程部署下无效，见 Task 7 报告 §并发取舍）
_BUILD_LOCK = threading.Lock()

# T7-A1：文案要点明"可能需要重建索引"，并给出可用的修复动作
_INDEX_ERROR_HINT = '重建索引：POST /api/search/index（或 CLI build-search-index）'

# `sse.py::create_sse_progress` 的**保留**事件名：结构体里 stage 取这些值就变成终止事件。
# ⚠️ 引擎的进度 stage 名（`meta` / `text` / `optimize` / **`done`**）是**另一个命名空间**，
# 直接透传会让 `build_index` 收尾时那句 `progress(STAGE_DONE, '完成', 1.0)` 变成
# 一个**过早的 `event: done`**（实测：流里出现两个 done，客户端"完成"判据失效，
# 且它出现在放锁之前 → "已拿到 done 也能被 409 拒"）。保留名一律降级成普通 progress。
_RESERVED_SSE_STAGES = ('done', 'error', 'select')


def _cfg():
    return (current_app.config.get('DECRYPTED_DIR', ''),
            current_app.config.get('WXID'))


def _int_arg(name, default):
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


def _search_type_options():
    """基类型 → 规范中文名：`TYPE_ALIASES` 里每个基类型取**第一个**别名。

    类型下拉**由服务端渲染**（T7-A7 / 计划缺陷 #15）：前端若自己维护一份类型
    列表，用项目里并不存在的全局 `TYPES` 就会抛 `ReferenceError`，整页初始化
    崩溃；而"静态页面断言"照样通过。`TYPE_ALIASES` 是**唯一事实源**，也正是
    解析器接受的名字（`_apply_field` 走 `TYPE_ALIASES[v]`）。

    含同义词（文本/文字、图片/照片…）→ 下拉只列规范名，恰好 12 项。
    模板里 `value` 用中文规范名，因为语法框发的就是中文名。
    """
    seen, out = set(), []
    for name, base in TYPE_ALIASES.items():
        if base in seen:
            continue
        seen.add(base)
        out.append({'label': name, 'value': base})
    return out


def _text_ready(st):
    """文本搜索是否真的可用（T7-A2）。

    `index_status.ready = schema_ok and (fts_rows or meta_rows)`，而
    `build_index(text=False)` 之后 `fts_rows=0` / `meta_rows>0` → `ready` 仍为 True，
    但 `message_fts` 是空的，关键词 `MATCH` 只会得到 0 条 —— 一个"看起来正常"的
    静默假阴性。`index_status` 没有专用布尔字段（Task 4 已确认），所以唯一的
    判据就是这一个表达式，由本层显式暴露给 UI，免得 Task 9 再各写一份。
    """
    return bool(st.get('schema_ok') and st.get('fts_rows'))


def _index_incompleteness(st):
    """→ (is_incomplete, reasons) —— T7-A2 口径：只有**真信号**才算不完整。

    `fts_coverage < 1` **不算**：差额是合法的（`meta_rows` 覆盖全部消息，
    `fts_rows` 只含有正文的消息，图片/语音等没有正文）。真机实测
    `fts_coverage = 0.8615` 完全正常；把它当告警会让每次正常安装都永久显示
    "索引不完整"，反而训练用户忽略提示。
    """
    reasons = []
    if not st.get('schema_ok'):
        reasons.append('索引 schema 校验未通过（旧版本或结构不符，需整库重建）')
    if not st.get('source_shards'):
        reasons.append('未发现消息分片（源目录未挂载或路径不对）')
    if st.get('source_rows') and (st.get('meta_coverage') or 0) < 1:
        reasons.append('元数据只覆盖 %.2f%% 的消息（完整时应为 100%%）'
                       % round((st.get('meta_coverage') or 0) * 100, 2))
    if st.get('fts_skipped_no_chat'):
        reasons.append('%d 条文本因会话映射失效被跳过（最危险的一类丢失）'
                       % st['fts_skipped_no_chat'])
    # 存在性守卫（**对称**）：`msg_text_rows` / `fts_rows` **任一缺失或为 None** → 该条不成立
    # （不报警、不进 reasons）。与 JS 侧 `search-render.js::statusNotice` 的
    # `_has(status,'msg_text_rows') && _has(status,'fts_rows')` 对齐：判据语义是"两个行数不相等"，
    # **任一数不在场都无法判定**，直接 `!=` 会把 `None` 打进文案（"msg_text(877712) 与
    # message_fts(None) 行数不一致"）并把"缺字段"误报成"两表不一致"。
    # ⚠️ 只放过 None/缺失，**不放过 0**：`index_status()` 把这些键预填成 0、`_meta_int`
    # 也永不返回 None，所以"老索引缺该 meta 键"得到的是 0 → `0 != fts_rows` 照旧告警（真信号）。
    # 也因此这条守卫**不可能**抑制任何生产告警（生产路径上这两个键恒为 int）。
    msg_text_rows = st.get('msg_text_rows')
    fts_rows = st.get('fts_rows')
    if msg_text_rows is not None and fts_rows is not None and msg_text_rows != fts_rows:
        reasons.append('msg_text(%s) 与 message_fts(%s) 行数不一致'
                       % (msg_text_rows, fts_rows))
    if st.get('duplicates_dropped'):
        reasons.append('构建时丢弃了 %d 条重复行' % st['duplicates_dropped'])
    return bool(reasons), reasons


@search_bp.route('')
def search():
    """GET /api/search?q=...&page=&per_page=&sort="""
    q = request.args.get('q', '') or ''
    decrypted_dir, own_wxid = _cfg()
    if not q.strip():
        return jsonify({'error': '缺少查询参数 q'}), 400
    try:
        body = search_messages(
            decrypted_dir, q,
            page=_int_arg('page', 1),
            per_page=_int_arg('per_page', DEFAULT_PER_PAGE),
            sort=request.args.get('sort', 'time_desc'),
            own_wxid=own_wxid)
    except SearchIndexError as e:
        # T7-A1：结构化 5xx。**绝不能**在这里 return results=[] —— 那会把
        # "索引坏了" 变成 "没有找到相关消息"（本方案最危险的静默假阴性）。
        out = e.as_dict()
        out.setdefault('hint', _INDEX_ERROR_HINT)
        return jsonify(out), e.http_status
    except ValueError as e:
        # 查询为空 / 语法错误。二次解析只为让 UI 能标出错处；它自己若抛错也
        # **不能**把 400 变成 500（计划缺陷：`类型:²` 曾从 `int()` 逃逸，
        # 而这里为了附带 `parsed` 会再调一次解析器）。
        try:
            parsed = parse_query(q)
        except Exception:
            parsed = None
        return jsonify({'error': str(e), 'parsed': parsed}), 400
    return jsonify(body)


@search_bp.route('/status')
def status():
    """GET /api/search/status

    热路径，实测 3.0–3.7ms：**绝不**逐表 `COUNT(*)/MAX(create_time)`
    （那一段真机实测 1.40s 热 / 13.4s 冷，而搜索页每次加载都会调这里）。
    本路由只调 `index_status()`（1 次只读连接 + N 次 os.stat）。
    """
    decrypted_dir, _ = _cfg()
    st = dict(search_index.index_status(decrypted_dir))
    st['index_path'] = search_index.index_path(decrypted_dir)
    st['text_ready'] = _text_ready(st)
    st['incomplete'], st['incomplete_reasons'] = _index_incompleteness(st)
    return jsonify(st)


@search_bp.route('/index', methods=['POST'])
def build():
    """POST /api/search/index —— SSE 构建 / 刷新索引。

    并发（T7-A4）：第二个请求拿到 **409** 而不是与第一个同时写索引库。
    锁在**构建写入结束时**释放（不是流结束时），所以即使客户端提前断开，
    锁也不会泄漏。

    ⚠️ 释放顺序是契约（无竞态护栏）：**先 `release()`，再推终止事件**。
    反过来（先 `push.done` 再 `release`）会让客户端在"已经拿到 done"之后仍可能
    被下一个请求 **409 误拒** —— 命中与否取决于 GIL 何时切到工作线程，表现为
    **偶发**失败。先释放后推送，则由队列的 happens-before 保证：
    **终止事件可见 ⇒ 锁已空闲**（`test_lock_is_free_as_soon_as_the_stream_reports_done`）。
    """
    data = request.get_json(silent=True) or {}
    action = data.get('action', 'build')
    want_text = bool(data.get('text', True))
    want_meta = bool(data.get('meta', True))
    decrypted_dir, _ = _cfg()

    if not _BUILD_LOCK.acquire(blocking=False):
        return jsonify({'error': '索引正在构建中，请等待当前构建结束后重试',
                        'code': 'index_build_in_progress'}), 409

    push, gen = create_sse_progress()

    def _progress(stage, message, pct):
        """引擎进度 → SSE 事件。

        stage 名只在**非保留**时才透传（UI 用它区分 meta/text/optimize 阶段）；
        `done`/`error`/`select` 是 `sse.py` 的保留终止名，一律降级成 progress ——
        否则构建收尾的 `STAGE_DONE` 进度会冒充终止事件（见 `_RESERVED_SSE_STAGES`）。
        """
        name = 'progress' if stage in _RESERVED_SSE_STAGES else stage
        push(name, str(message), float(pct))

    def _do_build():
        if action == 'refresh':
            return search_index.refresh_index(decrypted_dir, progress=_progress)
        return search_index.build_index(decrypted_dir, text=want_text,
                                        meta=want_meta, force=True,
                                        progress=_progress)

    def _run():
        payload, failure = None, None
        try:
            try:
                res = _do_build()
                st = search_index.index_status(decrypted_dir)
                payload = {'meta_rows': res.get('meta_rows'),
                           'fts_rows': res.get('fts_rows'),
                           'built_at': res.get('built_at'),
                           'skipped': bool(res.get('skipped')),
                           'index': st}
            finally:
                # 见 docstring：**必须在推终止事件之前**释放（写入已结束）。
                _BUILD_LOCK.release()
        except Exception as e:               # noqa: BLE001 —— 必须变成 SSE error 事件
            failure = str(e)

        # T7-A4：失败沿 SSE 报出去（`event: error`），并且**不**推 done。
        # 状态由 `/status` 反映（构建没写成功就不会 ready=True）。
        if failure is None:
            push.done(payload)
        else:
            push.error(failure)

    try:
        threading.Thread(target=_run, daemon=True).start()
    except Exception:
        _BUILD_LOCK.release()
        raise
    return sse_response(gen)
