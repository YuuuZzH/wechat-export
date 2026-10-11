"""全局搜索执行层：索引路径 + 降级直扫路径（Task 6 落地）+ 结果整形。

对 Task 7（HTTP 层）的绑定契约
-----------------------------
索引路径失败**不会**变成"0 结果"（T5-A1）——它抛 `SearchIndexError`：

    from engine.services.search import SearchIndexError, search_messages
    try:
        body = search_messages(decrypted_dir, q, ...)
    except ValueError:                     # 查询为空 → 400
        ...
    except SearchIndexError as e:          # 索引损坏/SQL 失败 → 非 200
        return jsonify(e.as_dict()), e.http_status      # 500

`SearchIndexError.as_dict()` → `{'error', 'code', 'http_status', 'detail', 'hint'}`。
`TextIndexUnavailableError` 是它的子类（T5-A8：`build_index(text=False)` 之后文本索引
为空而 `index_status.ready` 仍为 True）。两者都**不是** `ValueError`（避免被误判成 400）。

⚠️ T5-A8 的后果在 Task 6 已升级：**文本索引为空不再抛错，而是降级直扫**
（`used_fallback=True` + `scan_mode='fallback'` + 一条 `warnings`）。
`TextIndexUnavailableError` 仅为兼容 Task 5/Task 7 的导入而保留，`search_messages`
**不再抛它**。「关键词查询绝不静默返回空」的不变式由真实降级实现保证。

两条执行路径（互斥）
-------------------
1. **索引路径**（`index_status.ready` 且文本索引非空）：
   有关键词/正则字面量 → `message_fts MATCH ?` 驱动；纯筛选 → `msg_meta` 直查。
2. **降级路径**（索引不存在/不可读，或文本索引为空而查询需要文本）：
   `_execute_fallback` 直接 LIKE 扫微信 `message_fts_v4_*_content`。
   返回**同形状**的 6 元组、第 6 列同样是**原文**，因此摘要/高亮/正则确认在两条
   路径上一致（见 `_execute_fallback` 的已知差异说明）。

为什么正文一律用原文（T5-A2 / Task 4 的 A8）：`message_fts.text` 是
char-token（小写化、去空白），既不能用于显示（用户看到 `grouphellodisk`），
也不能用于正则确认（`/A\\d{4}/`、`/报修\\s*电话/` 会**静默假阴性**）。
**绝不** `LEFT JOIN message_fts` 取正文：它的三个列是 UNINDEXED，
没有 `MATCH` 预筛时是两侧交叉扫描（101 万 × 89 万）。
"""
import os
import re
import sqlite3
import time

from engine.services import search_index
from engine.utils import bare_wxid
from engine.services.search_query import (char_phrase, extract_regex_literals,
                                          is_empty, parse_query,
                                          regex_is_degraded, to_fts_expr)

DEFAULT_PER_PAGE = 50
MAX_PER_PAGE = 200
SNIPPET_WIDTH = 30          # 命中点左右各取多少个字符

# --------------------------------------------------------------------------
# T16：**显式截断**的保留上限（只限制"物化下来用于分页的幸存行条数"）
# --------------------------------------------------------------------------
# 为什么需要它（真实数据实测，`output/decrypted`，1,018,918 条消息）：
#   * 含正则或排除词的形态**必须**把幸存行物化在内存里才能分页（顺序由 Python 侧定）；
#   * 当"幸存行 ≈ 全体候选"时，流式扫描只能省掉"不必保留的那部分"，救不了它 ——
#     纯排除词 `-退货` 的幸存行 = 1,018,563 / 候选 1,018,918 = **99.965%**，
#     Task 13 流式化前后峰值工作集**都是 509 MiB**（一分不降，结构性）。
# 上限就是那道硬安全阀：宁可**明确告诉用户**只保留了前 N 条，也不许无上限地吃内存。
#
# ⚠️⚠️ 它**只**截"保留行数"。`candidate_rows` 与 `total` 照旧**精确统计**
# （扫描循环必须走完，见 `_Retention`）—— 为了让数字好看而截断 total 是最坏的缺陷：
# 用户会以为"一共只有这么多条"，而真相是"我们只保留了这么多条"。
# ⚠️ **绝不允许静默截断**：任何"少给结果"都必须在结构化字段（`truncated` /
# `retained_rows`）与人话 `warnings` 两个层面同时可见。
MAX_RETAINED_ROWS = 50000


class _Retention:
    """幸存行的**保留窗口**：只物化前 `limit` 条，但把**全部**幸存行精确计数。

    * `count` = 见过的幸存行总数 ⇒ 正是精确 `total` 的来源；
    * `rows`  = 实际保留（物化）的幸存行，最多 `limit` 条。

    为什么不是"够了就提前 `break`"：那会让 `total` / `total_pages` 变成"保留条数"
    （= 截断污染计数），页面上"共 N 条"就变成谎话。所以**扫描循环照旧走完**，
    只是把窗口之外的行丢弃而**不物化** —— 内存的来源正是那些被物化的行
    （6 元组 + `chat_id`/`sender`/`raw_text` 三批 str 对象）。

    `limit=None` ⇒ 不截断（既有调用方与"上限调高"的对照实验走这条路）。
    """

    __slots__ = ('limit', 'rows', 'count')

    def __init__(self, limit=None):
        self.limit = None if limit is None else max(0, int(limit))
        self.rows = []
        self.count = 0

    def offer(self, row):
        """记一行幸存行：**永远计数**，只在窗口未满时物化。"""
        self.count += 1
        if self.limit is None or len(self.rows) < self.limit:
            self.rows.append(row)

# 列出查询形态（返回结构里的 `scan_mode`）
SCAN_FTS = 'fts'            # 索引路径：FTS 预筛（关键词 / 非退化正则字面量）
SCAN_FILTER = 'filter'      # 索引路径：msg_meta 直查（纯筛选 / 退化正则的全量扫描）
SCAN_FALLBACK = 'fallback'  # 索引不可用：降级直扫（Task 6）

TYPE_NAMES = {
    1: '文本', 3: '图片', 6: '文件', 34: '语音', 42: '名片', 43: '视频',
    47: '表情', 48: '位置', 49: '链接/应用', 50: '网络电话',
    10000: '系统消息', 10002: '系统消息',
}

_SELF_TOKENS = ('我', '自己', 'me')


def type_label(local_type):
    return TYPE_NAMES.get(int(local_type or 0), '其他')


# --------------------------------------------------------------------------
# 错误：SQL 失败与"真的没有结果"必须可区分（T5-A1 / T5-A8）
# --------------------------------------------------------------------------

class SearchIndexError(RuntimeError):
    """索引路径失败 —— 必须转成非 200 响应，**绝不允许**被吞成空结果。

    `detail` 保留原始 SQLite 错误原文（例如 `no such table: msg_meta`），
    使"索引坏了"与"没有这条消息"在日志和 UI 上都可区分。
    """
    code = 'index_error'
    http_status = 500

    def __init__(self, message, *, code=None, detail=None, hint=None, http_status=None):
        super().__init__(message)
        self.code = code or type(self).code
        self.http_status = int(http_status if http_status is not None
                               else type(self).http_status)
        self.detail = detail
        self.hint = hint

    def as_dict(self):
        out = {'error': str(self), 'code': self.code, 'http_status': self.http_status}
        if self.detail:
            out['detail'] = self.detail
        if self.hint:
            out['hint'] = self.hint
        return out


class TextIndexUnavailableError(SearchIndexError):
    """文本索引缺失/为空时曾抛出的错误（T5-A8）。

    `build_index(text=False)` 会清空 `message_fts`/`msg_text`，而
    `index_status.ready = schema_ok and (fts_rows or meta_rows)` 仍为 True
    → 关键词查询若对着空 FTS 表 `MATCH` 会得到 0 行，UI 却显示"索引就绪"。

    Task 5 阶段用"抛这个错"堵住静默空结果（当时 `_execute_fallback` 还是占位）。
    **Task 6 起 `search_messages` 不再抛它** —— 真实降级路径可用，改为降级直扫
    （`used_fallback=True` + `scan_mode='fallback'` + `warnings` 留痕）。
    类本身为兼容既有导入（Task 5 / Task 7）而保留：「绝不静默返回空」这一不变式
    现在由降级实现保证，而不是由异常保证。
    """
    code = 'text_index_unavailable'
    http_status = 503


# --------------------------------------------------------------------------
# 作用域解析（把 ParsedQuery 的语义值落到具体 chat_id / wxid / epoch）
# --------------------------------------------------------------------------

def _contact_names(decrypted_dir):
    """{wxid: display_name}，用于把会话/发送者关键字模糊匹配成 wxid、并回填显示名。

    通讯录不可用时返回 {}：**过滤器仍然生效**（见 `_resolve_names` 的兜底），
    只是退化为「只按原始 wxid 精确匹配」，不会把筛选条件悄悄丢掉。
    """
    try:
        from engine.services.address_book import get_all_contacts
        return {c['wxid']: (c.get('display_name') or c['wxid'])
                for c in get_all_contacts(decrypted_dir)}
    except Exception:
        return {}


def _resolve_names(names_map, wanted):
    """→ (候选 wxid 列表, 未解析出的原始输入列表)。

    解析不出时**把原始输入原样留下**并计入未解析：绝不能返回空集 ——
    调用方把空集理解为「该字段不限制」，于是一次精确查询会被悄悄放大成
    「全部会话/全部发送者」（T5-A1 是同族缺陷的反方向：那里是假阴性，这里是假阳性）。
    原样留下是安全的：`chat_id IN ('张三')` 是**精确等值**，不会放宽结果。
    """
    resolved, unresolved = [], []
    for raw in wanted or []:
        token = (raw or '').strip()
        if not token:
            continue
        low = token.lower()
        if token in names_map:
            hits = [token]
        else:
            hits = [wxid for wxid, disp in names_map.items()
                    if low in (wxid or '').lower() or low in (disp or '').lower()]
        if hits:
            resolved.extend(hits)
        else:
            unresolved.append(token)
            resolved.append(token)          # 原样当 wxid 用（精确等值）
    return sorted(set(resolved)), unresolved


def _label_chats(decrypted_dir, labels):
    """标签 → 该标签下的 wxid 集合；无标签条件返回 None（= 不限制）。

    标签名一个都匹配不上时返回**空集**（= 没有结果），而不是 None（= 不过滤）：
    「限制不了」绝不能变成「不限制」。
    """
    if not labels:
        return None
    try:
        from engine.services.address_book import get_all_contacts
        contacts = get_all_contacts(decrypted_dir)
    except Exception:
        return set()
    want = {l.strip().lower() for l in labels if l and l.strip()}
    out = set()
    for c in contacts or []:
        for name in (c.get('labels') or []):
            if (name or '').lower() in want:
                out.add(c['wxid'])
                break
    return out


def _is_self_token(token):
    """`我` / `自己` / `me` —— **保留语义** token（大小写不敏感），与通讯录无关。"""
    return (token or '').strip().lower() in _SELF_TOKENS


# Critical #17 的诚实兜底：规范化之后索引里仍然一行都找不到「本人」时，`发送者:我`
# 会返回 0 —— 必须说出来，不能静默（真实数据里裸 wxid 有 222,012 行，所以一般不触发）。
_SELF_SENDER_MISSING_WARNING = (
    '索引中找不到「本人」的发送者身份（own_wxid 及其规范化形式都不在索引的'
    ' sender_username 里）：本次「发送者:我」会返回 0 条。该索引可能是用另一个账号构建的，'
    '建议重建索引后再试。')


def _bare_wxid(value):
    """账号**目录名** → 消息侧使用的**裸 wxid**（Critical #17）。

    4.x 的 `config_file.get_backup_wxid()` 存的是账号**目录名**，形如
    `<裸 wxid>_<4 位十六进制>`（真实值 24 字符，`wxid_xxxxx_10e8`），
    而索引里 `msg_meta.sender_username` / 分片 `Name2Id` 存的是**裸 wxid**（19 字符）。
    真实数据只读实测（只报计数）：
        24 字符整串在 `msg_meta.sender_username` 精确匹配 **0** 行
        去掉尾部 `_10e8` 后的裸 wxid 精确匹配 **222,012** 行
        分片 `Name2Id`：含裸 wxid True；含 24 字符整串 False
        → `发送者:我` 用裸 wxid = 222,485 ✓ ／ 用目录名 = 473 ✗（≈完全没过滤）
    即索引**本来就能表达"我发的消息"**，只是传进来的值从不匹配。

    ⚠️ **实现只有一份**：真正的判据在 `engine.utils.bare_wxid`（会话列表侧
    `chat._own_wxid_forms` 用的是同一个函数，见 `known-issues.md` #20）。
    这里只保留历史名字做**委托**，因为 Task 6 的测试与本项目台账都引用 `_bare_wxid`；
    语义细节（严格判据、`None`→`''`、幂等）见那边的 docstring —— **不要在这里再实现一遍**。
    """
    return bare_wxid(value)


def _resolve_scope(decrypted_dir, parsed, own_wxid):
    """→ 可直接用于 SQL 的作用域 + 面向用户的 warnings。

    keys: chats / senders / types / date_from / date_to / label_chats / warnings
      chats/senders  空集 = 不限制（**不是**"没有匹配"）
      label_chats    None = 不限制；空集 = 没有匹配
      warnings       未解析出的筛选词（原文保留、精确匹配）等提示
    """
    names_map = _contact_names(decrypted_dir)
    warnings = []
    chats, un_chats = _resolve_names(names_map, parsed.get('chats'))

    raw_senders = [(t or '').strip() for t in (parsed.get('senders') or [])
                   if (t or '').strip()]
    demanded_self = [t for t in raw_senders if _is_self_token(t)]
    # ⚠️ 保留 token（我/自己/me）**绝不能**交给 `_resolve_names` 的**模糊子串**匹配：
    # 真实数据只读实测，`'我'` 会子串命中 **130 个联系人**（昵称/备注里含「我」的人极多），
    # 于是 `发送者:我` 变成"这 130 人的消息"（返回 473 行**别人的**消息），本人一条都不在其中；
    # 没有 own_wxid 时更糟：它被解析成某个模糊命中的**陌生人**（`IN ('wxid_xxx')`），
    # 语义从"我发的"静默变成"那个人发的"。保留 token 走下面这条独立通路。
    senders, un_senders = _resolve_names(
        names_map, [t for t in raw_senders if not _is_self_token(t)])

    if demanded_self:
        if own_wxid:
            # 「我」是已知语义：解析成本人 wxid。
            #
            # ⚠️⚠️ 这个分支的闸门（`demanded_self`）**不能去掉**（2026-09 Critical）：
            # `own_wxid` 是**默认参数**（HTTP 层从 `_cfg()` 取、CLI 用 `get_backup_wxid()`），
            # 而这里曾经无条件把 `own_wxid` 并进 `scope['senders']` → SQL 里凭空多出
            # `m.sender_username IN (<本人 wxid>)`。真实索引实测：`msg_meta` 里
            # `sender_username == own_wxid` 的行数 = **0**（本人 wxid 不在任何分片 Name2Id 的
            # 15,220 行里），于是**任何**查询都归零：`维修` 1389→0、`类型:图片` 87941→0、
            # `维修 OR 报修` 1815→0，而 `warnings` 是空的、`index.ready=True`
            # —— "索引就绪 + 搜什么都 0 条"是本功能最坏的静默失败形态。
            # 只有用户**确实写了「发送者:我」**时才允许注入本人 wxid；否则 `scope['senders']`
            # 必须保持空集（= 不限制，见本函数 docstring）。
            # —— 值形式（Critical #17）：配置里给的是**账号目录名**（`<裸wxid>_10e8`），
            # 索引里存的是**裸 wxid**。只注入原值会让 `发送者:我` 静默返回 0
            # （实测：目录名在 msg_meta 匹配 0 行、裸 wxid 匹配 222,012 行）。
            # 同时接受两种形式：`IN` 里多一个不存在的值是无害的，
            # 这样对"配置里到底存哪种形式"不敏感，避免以后再踩同一个坑。
            forms = {own_wxid, _bare_wxid(own_wxid)}
            senders = sorted(set(senders) | {f for f in forms if f})
        else:
            # 没给 own_wxid：保留原样 token（`IN ('我')` 匹配 0 行），并给出提示。
            # **绝不能**返回空集：空集会被调用方读成"该字段不限制"，
            # 一次精确查询被悄悄放大成「全部发送者」（见 `_resolve_names` 的 docstring）。
            senders = sorted(set(senders) | set(demanded_self))
    for field, tokens in (('会话', un_chats), ('发送者', un_senders)):
        for token in tokens:
            warnings.append('%s筛选 "%s" 未匹配到通讯录联系人，已按原始 wxid 精确匹配'
                            % (field, token))
    if demanded_self and not own_wxid:
        warnings.append('发送者:我 需要 own_wxid（未提供），无法解析成本人 wxid')

    label_chats = _label_chats(decrypted_dir, parsed.get('labels') or [])
    if label_chats is not None and not label_chats:
        warnings.append('标签筛选未匹配到任何会话（标签名不存在或通讯录不可用）')

    return {
        'chats': set(chats),
        'senders': set(senders),
        'types': set(parsed.get('types') or []),
        'date_from': parsed.get('date_from'),
        'date_to': parsed.get('date_to'),
        'label_chats': label_chats,
        # 查询里**显式**写了「发送者:我」吗（用于 Critical #17 的兜底探测：
        # 只有用户真的要「我」时才需要对索引做"本人身份是否存在"的判定）
        'demanded_self': bool(demanded_self),
        'warnings': warnings,
    }


# --------------------------------------------------------------------------
# 摘要与高亮（作用在**原文** raw_text 上）
# --------------------------------------------------------------------------

def _folded_span(raw_text, term):
    """在**折叠后**的原文里定位 `term`，把区间映射回原文下标；找不到返回 None。

    匹配语义是「折叠后连续」（见 `_fold_text`），所以短语 `服务器 宕机` 命中
    原文 `服务器宕机` 时**字面 `find` 找不到** —— 没有这一步，这条命中的行在摘要里
    就没有任何高亮（用户看到命中却没有黄底）。两条路径共用本函数，因此是**对称**的。
    """
    folded_term = _fold_text(term)
    if not folded_term or not raw_text:
        return None
    chars, index_map = [], []
    for pos, ch in enumerate(raw_text):
        low = ch.lower()
        if low.isalnum():
            chars.append(low)
            index_map.append(pos)
    at = ''.join(chars).find(folded_term)
    if at == -1:
        return None
    return index_map[at], index_map[at + len(folded_term) - 1] + 1


def find_spans(raw_text, parsed, regexes):
    """在原文里找出所有命中区间，返回 [(start, end)]（已合并重叠）。

    关键词/短语/OR 分支先用大小写不敏感**字面**子串定位（与 FTS 的 char-token
    子串语义一致）；字面找不到时再退到「折叠后连续」定位（`_folded_span`）——
    索引路径与降级路径都靠 char-token 短语匹配，`服务器 宕机` 命中 `服务器宕机`
    这类情况必须同样给出高亮。正则用 `re` 精确匹配。
    排除词不进高亮（它是"不该出现"的词）。
    """
    spans = []
    if not raw_text:
        return []
    terms = list(parsed.get('keywords') or []) + list(parsed.get('phrases') or [])
    for grp in parsed.get('or_groups') or []:
        terms.extend(grp)
    low = raw_text.lower()
    for term in terms:
        t = (term or '').lower()
        if not t:
            continue
        before = len(spans)
        start = 0
        while True:
            i = low.find(t, start)
            if i == -1:
                break
            spans.append((i, i + len(t)))
            start = i + 1
        if len(spans) == before:
            found = _folded_span(raw_text, term)
            if found:
                spans.append(found)
    for pattern in regexes or []:
        try:
            for m in re.finditer(pattern, raw_text):
                if m.end() > m.start():
                    spans.append((m.start(), m.end()))
        except re.error:
            continue
    if not spans:
        return []
    spans.sort()
    merged = [list(spans[0])]
    for a, b in spans[1:]:
        if a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def build_snippet(raw_text, spans, width=SNIPPET_WIDTH):
    """围绕命中点截取摘要；返回 (snippet, 相对 snippet 的 spans)。"""
    if not raw_text:
        return '', []
    if not spans:
        head = raw_text[:width * 2]
        return (head + '…') if len(raw_text) > len(head) else head, []
    lo = max(0, spans[0][0] - width)
    hi = min(len(raw_text), spans[-1][1] + width)
    snippet = raw_text[lo:hi]
    prefix = '…' if lo > 0 else ''
    suffix = '…' if hi < len(raw_text) else ''
    rel = [[max(0, a - lo) + len(prefix), max(0, b - lo) + len(prefix)]
           for a, b in spans if b > lo and a < hi]
    return prefix + snippet + suffix, rel


# --------------------------------------------------------------------------
# 文本索引可用性探测（T5-A8）
# --------------------------------------------------------------------------

def _probe_text_index(conn):
    """返回 '' 表示 `message_fts` 与 `msg_text` 实存且非空；否则返回原因文案。

    以**实存数据**为准，不只看 `index_meta.fts_rows`：后者可能滞后于真实内容
    （表被清空/删除时它仍报旧值）。`LIMIT 1` 探测是 O(1)：
    FTS5 的 `SELECT rowid ... LIMIT 1` 取到第一行即停，不做全表扫描。
    """
    for table in ('message_fts', 'msg_text'):
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name=? AND type IN ('table','view')",
                (table,)).fetchone()
            if exists is None:
                return '索引库缺少 %s 表' % table
            if conn.execute('SELECT rowid FROM [%s] LIMIT 1' % table).fetchone() is None:
                return '%s 为空' % table
        except sqlite3.Error as exc:
            return '探测 %s 失败：%s: %s' % (table, type(exc).__name__, exc)
    return ''


def _text_index_reason(conn, status):
    """返回 '' 表示文本索引实存且非空；否则返回不可用的原因文案（T5-A8）。

    判定逻辑与 Task 5 完全一致，**变的是判定的后果**：Task 5 抛
    `TextIndexUnavailableError`（当时 `_execute_fallback` 还是返回 `[]` 的占位，
    「降级」等于把「索引坏了」变成「看起来正常的空结果」）；Task 6 起真正的降级
    路径可用，于是后果改为**降级直扫并置 `used_fallback=True`**（功能可用性优先）。
    「绝不静默返回空」这一不变式不靠异常实现，靠降级实现。
    """
    reason = _probe_text_index(conn)
    if not reason:
        return ''
    if not status.get('fts_rows'):
        reason = '%s；index_status.fts_rows=0（可能由 build_index(text=False) 造成）' % reason
    return reason


# --------------------------------------------------------------------------
# 索引路径：两条互斥的 SQL（T5-A1）
# --------------------------------------------------------------------------

# 6 元组：(chat_id, local_id, local_type, create_time, sender_username, raw_text)
_FILTER_COLUMNS = ('m.chat_id, m.local_id, m.local_type, m.create_time,'
                   ' m.sender_username, t.raw_text')

# 两条路径的 FROM 片段集中在这里，保证「计数 / 分页 / 全量」三种用途
# 用的是**完全同一套连接语义**（否则 total 与当页行会来自不同的行集）。
_KEYWORD_FROM = ('FROM message_fts f'
                 ' JOIN msg_text t ON t.rowid = f.rowid'
                 ' JOIN msg_meta m ON m.chat_id = t.chat_id AND m.local_id = t.local_id'
                 ' AND m.create_time = t.create_time')
_FILTER_FROM = ('FROM msg_meta m'
                ' LEFT JOIN msg_text t ON t.chat_id = m.chat_id'
                ' AND t.local_id = m.local_id AND t.create_time = m.create_time')
# 纯筛选路径的计数不需要 JOIN msg_text：idx_text_key 是**唯一索引**，
# 每条消息至多命中 1 行 → 行数与 JOIN 无关，省掉百万级索引查找。
_FILTER_COUNT_FROM = 'FROM msg_meta m'


def _scope_where(scope):
    """作用域 → (WHERE 片段列表, 参数列表)。所有值都走占位符，不做字符串插值。"""
    where, params = [], []
    if scope['chats']:
        where.append('m.chat_id IN (%s)' % ','.join('?' * len(scope['chats'])))
        params.extend(sorted(scope['chats']))
    if scope['label_chats'] is not None:
        if not scope['label_chats']:
            return None, None          # 标签上没有会话 → 结果必然为空
        where.append('m.chat_id IN (%s)' % ','.join('?' * len(scope['label_chats'])))
        params.extend(sorted(scope['label_chats']))
    if scope['types']:
        where.append('m.local_type IN (%s)' % ','.join('?' * len(scope['types'])))
        params.extend(sorted(scope['types']))
    if scope['senders']:
        where.append('m.sender_username IN (%s)' % ','.join('?' * len(scope['senders'])))
        params.extend(sorted(scope['senders']))
    if scope['date_from'] is not None:
        where.append('m.create_time >= ?')
        params.append(scope['date_from'])
    if scope['date_to'] is not None:
        where.append('m.create_time <= ?')
        params.append(scope['date_to'])
    return where, params


def _build_sql(scope, fts_expr, *, count_only=False, order=None, limit=None):
    """构造索引路径的 SQL —— **两条互斥的路径**（T5-A1）。

    形态 1（有关键词/正则字面量）：`_KEYWORD_FROM` —— FTS 命中驱动。
      `rowid` 关联 `msg_text`（O(1)，同一个 rowid 保证是同一条消息）；
      三元组关联 `msg_meta`（走它的主键；**绝不能只用 (chat_id, local_id)**：
      真实数据实测两列键会 3.5× 放大 → 重复命中 + 摘要串行）。
    形态 2（无关键词，纯筛选）：`_FILTER_FROM` —— 只查 `msg_meta`。
      本路径**不得**出现 `message_fts` 的任何引用（表名或别名 `f`）：
      它没有被 JOIN 进 FROM，引用它就是 `no such column: f.text`（旧实现在这里
      把异常吞成 0 结果，于是"类型/日期/发送者组合筛选"在索引可用时恒返回空）。
      也不能为了取正文而 `LEFT JOIN message_fts`（三个 UNINDEXED 列 → 交叉扫描）。

    ⚠️⚠️ MATCH 的左操作数必须是**表名 `message_fts`**，不能写成别名 `f`。实测：
        f MATCH ?             -> OperationalError: no such column: f
        message_fts MATCH ?   -> 正确过滤（命中返回该行、不命中返回 []）
    SQLite 的 MATCH 左操作数只接受表名。若"顺手清理"成别名形式，关键词路径会立刻
    变成运行时错误 → 全文搜索整体失效。
    """
    where, params = _scope_where(scope)
    if where is None:
        return None, None
    if fts_expr:
        where.insert(0, 'message_fts MATCH ?')
        params.insert(0, fts_expr)
        from_clause = _KEYWORD_FROM
        columns = 'COUNT(*)' if count_only else _FILTER_COLUMNS
    else:
        from_clause = _FILTER_COUNT_FROM if count_only else _FILTER_FROM
        columns = 'COUNT(*)' if count_only else _FILTER_COLUMNS
    return _assemble(from_clause, where, params, columns=columns,
                     order=order if order is not None else _ORDER_ASC, limit=limit)


def _assemble(from_clause, where, params, *, columns, order='', limit=None):
    sql = 'SELECT %s %s%s%s' % (columns, from_clause,
                                (' WHERE ' + ' AND '.join(where)) if where else '',
                                order)
    if limit:
        sql += ' LIMIT ? OFFSET ?'
        params = list(params) + [limit[0], limit[1]]
    return sql, list(params)


# 全量/内部使用的稳定顺序（`_execute_indexed` 的契约：升序）
_ORDER_ASC = ' ORDER BY m.create_time ASC, m.local_id ASC'


def _order_sql(sort):
    """与整形阶段的 Python 排序**完全一致**的顺序（SQLite 侧分页必须同序）。

    Python 侧：time_desc → `(create_time, local_id)` reverse；time_asc → 同键升序；
    chat → `(chat_id.lower(), create_time)` 升序。chat_id 是 ASCII（wxid_/gh_/@chatroom），
    故 SQLite 的 `LOWER()` 与 Python 的 `str.lower()` 等价。
    """
    if sort == 'time_asc':
        return ' ORDER BY m.create_time ASC, m.local_id ASC'
    if sort == 'chat':
        return ' ORDER BY LOWER(m.chat_id) ASC, m.create_time ASC'
    return ' ORDER BY m.create_time DESC, m.local_id DESC'


# ⚠️ T13：这条路径是**流式**的 —— `for row in conn.execute(sql, params)` 逐行确认，
# **只把幸存行 append 进结果 list**。改造前它会先把整条 JOIN 的全部候选行
# （真实数据 1,018,918 行 × 6 列，其中 877,837 条是原文 str）`fetchall()` 进 Python
# 再逐行确认，实测 `PeakWorkingSetSize` **1,024.9 MiB**（退化正则）/ **1,171.4 MiB**（纯排除词）。
# **不要**为了"顺手简化"退回 `fetchall()` —— 那会把内存峰值重新变成「全量候选 + 幸存行」。
# 也**不要**改成 keyset 分页：`ORDER BY m.create_time, m.local_id` **不是唯一序**
# （主键是 `(chat_id, local_id, create_time)`，不同会话可以落在同一个
# `(create_time, local_id)` 上），加第三列做 tiebreak 会改变并列行的相对顺序。
# 流式则完全沿用同一条 SQL / 同一个 `ORDER BY`，顺序与改造前逐元素一致。


def _compile_patterns(regexes):
    """正则表 → 已编译模式列表（**唯一一份**编译口径，流式扫描与 `_confirm_regexes` 共用）。"""
    patterns = []
    for pattern in regexes or ():
        try:
            patterns.append(re.compile(pattern))
        except re.error:
            continue          # 非法正则在 parse_query 已被拒；这里只是防御
    return patterns


def _exclude_terms(parsed):
    """排除词 → 规范化后的原文子串列表（**唯一一份**规范化口径）。

    降级路径的 `_apply_excludes` 与索引路径的流式扫描共用它。
    """
    return [t.strip().lower() for t in ((parsed or {}).get('exclude') or [])
            if t and t.strip()]


def _keeps_row(row, patterns, terms):
    """**唯一一份**逐行判定：正则 OR 命中 **且** 未被任一排除词排除。

    与「先 `_confirm_regexes` 再 `_apply_excludes`」两次过滤**逐行等价**：
    正则先算（原实现也是先正则后排除，连"哪些行会被喂给正则"都相同），
    排除词在**原文**上做大小写不敏感子串匹配；`raw_text IS NULL` 的行不含任何文本，
    因此不会被排除词丢掉（它们本来也没有可被排除的内容）。
    """
    raw = row[5]
    if patterns:
        text = raw or ''
        if not any(p.search(text) for p in patterns):
            return False
    if terms:
        low = (raw or '').lower()
        for term in terms:
            if term in low:
                return False
    return True


def _confirm_regexes(rows, regexes):
    """正则**只在原文 `raw_text` 上确认**（T5-A2）。

    多个正则之间取 OR（与 Task 6 的降级路径一致：`any(p.search(raw))`）。
    判定口径由 `_keeps_row` 拥有；本函数只是它的"整批行"形态。
    """
    if not regexes:
        return rows
    patterns = _compile_patterns(regexes)
    if not patterns:
        return rows
    return [row for row in rows if _keeps_row(row, patterns, ())]


def _scan_indexed(conn, scope, fts_expr, regexes, exclude_terms=(), retention=None):
    """**流式**扫描候选行 → `(SQL 候选行数, 幸存行)`。SQL 错误**不吞**（T5-A1）。

    * `candidate_rows` 语义不变：= SQL 预筛出的候选行数，**确认之前**的计数
      （确认阶段被剔除的行照样算进去）⇒ 逐行 +1，**不能**用 `len(幸存行)` 代替。
    * 顺序不变：仍按 `(create_time, local_id)` 升序（同一条 SQL、同一个 ORDER BY）。
    * 非法正则（编译失败）的行为不变：`patterns` 为空 → 不做正则过滤。
    * **T16（保留窗口）**：传入 `retention`（`_Retention`）时，本函数只把前
      `retention.limit` 条幸存行**物化**进 `retention.rows`，但**照旧走完全部候选行**
      并把全部幸存行计入 `retention.count` ⇒ 返回的 `candidate_rows` 与
      `retention.count`（= 精确 total）都**不被截断污染**。不传 `retention` 时行为
      与 Task 13 逐字相同（返回全部幸存行），既有内部契约锚因此不需要改。
    """
    sql, params = _build_sql(scope, fts_expr)
    if sql is None:
        return 0, []          # 标签上没有会话 → 结果必然为空（与旧实现一致）
    window = _Retention() if retention is None else retention
    patterns = _compile_patterns(regexes)
    terms = list(exclude_terms or ())
    candidate_rows = 0
    for row in conn.execute(sql, params):
        candidate_rows += 1
        if _keeps_row(row, patterns, terms):
            window.offer(row)
    return candidate_rows, window.rows


def _execute_indexed(conn, scope, fts_expr, regexes):
    """索引路径的通用入口（T5-A7 的契约：**返回单个列表**，不分页）。

    → `[(chat_id, local_id, local_type, create_time, sender_username, raw_text), ...]`
      按 `(create_time, local_id)` 升序，**未分页**；`raw_text` 是原文（可能为 None）。

    用途：需要**先确认再分页**的形态（含正则或排除词）——此时 SQL 行数不是最终结果数，
    不能下推 LIMIT/OFFSET（否则当页会被"确认阶段剔除的行"打出空洞，total 也会错）。
    无正则、无排除词的常见形态走 `_page_indexed`（SQL 侧计数 + 分页）。

    错误语义：SQL 失败抛 `sqlite3.Error`（由 `search_messages` 转成 `SearchIndexError`），
    **绝不**返回空列表来冒充"没有匹配结果"（T5-A1）。

    ⚠️ T13：排除词**不**在这里应用（与改造前一致 —— 调用方负责），正则确认则是流式的。
    """
    return _scan_indexed(conn, scope, fts_expr, regexes)[1]


def _page_indexed(conn, scope, fts_expr, *, page, per_page, sort):
    """SQL 侧计数 + 分页 → (页内行, total)。**仅用于无正则、无排除词的查询**。

    为什么不能只靠整形阶段切片：真实数据实测把全部命中行物化到 Python
    （`类型:图片` 87,941 行 / `日期:2026` 119,497 行 / 单字关键词 26 万行）
    单次查询要 **2.1s / 1.9s / 4.5s**，而 spec 第 5 节对纯筛选的预期是 <50ms。
    改成 `COUNT(*)` + `ORDER BY … LIMIT ? OFFSET ?` 后只取当页（≤200 行）。

    ⚠️ T16：**本路径有意不受 `MAX_RETAINED_ROWS` 约束**（截断只落在"必须把幸存行
    物化在内存里"的两条路径上，见 `search_messages` 的 docstring）。理由：这里的内存
    占用恒等于**当页**（≤ `MAX_PER_PAGE` 行），既没有"必须保留的 100 万行"，也就没有
    可截的对象；反过来若把上限也套在这里，纯筛选（`类型:` / `日期:`）与关键词查询
    就会**变得不可完整翻页**，而它们本来完全不缺内存。
    所以本路径恒为 `truncated=False` / `retained_rows == total`，永远不会有
    "当前页超出保留范围"这种事。
    """
    count_sql, count_params = _build_sql(scope, fts_expr, count_only=True,
                                         order='')
    if count_sql is None:
        return [], 0
    total = conn.execute(count_sql, count_params).fetchone()[0] or 0
    if not total:
        return [], 0
    sql, params = _build_sql(scope, fts_expr, order=_order_sql(sort),
                             limit=(per_page, (page - 1) * per_page))
    return conn.execute(sql, params).fetchall(), total


# --------------------------------------------------------------------------
# 文本条件的组装与整形阶段的排除词（T5-A1 的第二条通路）
# --------------------------------------------------------------------------

def _positive_fts_expr(parsed):
    """正向条件的 FTS 表达式（排除词交给 `_apply_excludes`，**不**写进 MATCH）。

    ⚠️ 先破除一个已经过时的前提：这里**不是**在绕「`to_fts_expr` 会产出非法表达式」的坑。
    那是 2026-09 之前的状态 —— 当时它拼的是前缀 `'NOT ' + phrase`，于是
    `("维 修" OR "报 修") AND NOT "报 修"` 会 `fts5: syntax error near "NOT"`。
    **该缺陷已由 Task 2 的修复消除**：现在的 `to_fts_expr` 产出合法**中缀**形态
    （`维修 -退货` → `"维 修" NOT "退 货"`；只有排除词时返回 `None`）。
    独立复验（探针 `D:\\dsh_tmp\\t6_probe6.py`，把表达式真的喂给内存 FTS5 表）：
    6 个产出表达式的排除/组合查询 **非法数 0**，`-退货` 返回 `None`。
    **不要**再据「它会产生语法错误」这个旧前提改掉或"优化"掉下面这段逻辑。

    保留本函数（= 把排除词从 MATCH 里摘出来、改在整形阶段按**原文子串**排除）的真实理由：

    1. **跨路径一致性（首要）**：Task 6 的降级路径没有 FTS 可用，本来就是在 Python 里
       按原文子串排除。排除词若走 MATCH，同一件事就有了**两套实现**，而它们对同一行
       可以给出**相反**的答案。实测（同一探针）：原文 `退 货 已处理`（词中间有空白）
       在 MATCH 侧被 `"退 货"` 命中并排除，而 Python 的 `'退货' in low` 为 `False`（保留）
       —— 用户看到的症状是「建了索引之后，同一条排除词的行为变了」。
    2. **敏感度只保留一份定义**：`_apply_excludes` 在原文上做大小写不敏感子串匹配，
       索引路径与降级路径**共用**它；把大小写/空白的判定交给 MATCH（char-token 语义）
       等于给同一语义再养一份实现，日后必然分叉。

    背景事实（理解此处设计要用，也是 FTS5 的硬约束）：FTS5 的 `NOT` 是**中缀**二元运算符，
    排除只能写成 `(正向) NOT "排除"`，没有合法的前缀形态 —— 这既解释了「只有排除词」的
    查询为何在 MATCH 里无表达式可用（`to_fts_expr` 返回 `None`），也正是上述两种排除实现
    天然难以对齐的根源。
    """
    if not parsed:
        return None
    if not parsed.get('exclude'):
        return to_fts_expr(parsed)
    stripped = dict(parsed)
    stripped['exclude'] = []
    return to_fts_expr(stripped)


def _literal_prefilter(literals):
    """非退化正则的字面量超集预筛（`regex_is_degraded` 为 False 时才可用）。"""
    phrases = [char_phrase(x) for x in dict.fromkeys(literals or []) if x]
    phrases = [p for p in phrases if p]
    return '(' + ' OR '.join(phrases) + ')' if phrases else None


def _apply_excludes(rows, parsed):
    """排除词：在**原文**上做大小写不敏感子串确认（索引/降级两条路径共用）。

    无正文的行（图片/语音/视频，`raw_text IS NULL`）不含任何文本，
    因此不会被排除词丢弃（它们本来也没有可被排除的内容）。

    T13：规范化与逐行判定分别收拢到 `_exclude_terms` / `_keeps_row` —— 索引路径的
    **流式扫描**用的就是同一份，所以"排除词"的判定口径仍然**只有一处定义**
    （Task 6 的裁决）。本函数只是它的"整批行"形态，语义一字未改。
    """
    terms = _exclude_terms(parsed)
    if not terms:
        return rows
    return [row for row in rows if _keeps_row(row, (), terms)]


# 降级语料「只有正文」的上限文案（控制方追加项：不许静默）。
# 索引缺失时按类型/日期等筛选会少给甚至给空，用户会误以为「库里没有」——
# 所以既要人类可读的文案，也要结构化字段（`fallback_text_only`）供 UI/CLI 判断。
_FALLBACK_TEXT_ONLY_WARNING = (
    '本次查询走了降级路径（索引未构建，或文本索引不可用）：降级只扫描微信的文本表，'
    '因此只能看到有正文的消息。本次查询含类型/日期/会话/标签/发送者筛选，'
    '图片、语音等无正文消息不会出现在结果里 —— 这不代表库里没有这些消息。'
    '建议先构建索引。')

# T16：**显式截断**的人话说明（文案由控制方逐字给定）。
# 与 `truncated` / `retained_rows` 两个结构化字段**必须同时出现**：
# 只有字段 → UI 不同版本会漏话；只有文案 → 程序侧无法判断"该翻页还是该缩小范围"。
# 注意 `total` 是**精确命中数**、`retained_rows` 才是"我们实际留下来分页的条数"。
_TRUNCATED_WARNING = (
    '结果集过大（共 %d 条），只保留了前 %d 条用于分页 —— '
    '请增加筛选条件（日期范围 / 会话 / 类型）缩小范围。')


def _has_non_text_filter(scope):
    """查询是否含「文本之外」的筛选条件（类型 / 日期 / 会话 / 标签 / 发送者）。

    这些条件的目标集合在索引路径由 `msg_meta` 承担（100% 覆盖，含图片/语音/视频），
    而降级路径的语料只有**有正文**的消息 —— 于是它们是「降级可见性」必须显式告知的
    触发条件（见 `_FALLBACK_TEXT_ONLY_WARNING`）。
    纯关键词/短语/正则/排除词**不算**：它们在降级路径本来就是逐条文本匹配，
    不依赖 `msg_meta`，加这条提示就是误报。
    """
    return bool(scope['types'] or scope['chats'] or scope['senders']
                or scope['label_chats'] is not None
                or scope['date_from'] is not None or scope['date_to'] is not None)


def _has_positive_text_condition(parsed):
    """查询是否有「**要求命中文本**」的正向条件（关键词 / 短语 / OR 组 / 正则）。

    没有它（= 只有排除词，如 `-报修`）时，结果集是"所有消息 − 命中排除词的那些"，
    而**无正文**消息本来**应当**出现在结果里（它们不可能包含被排除的词）。
    降级语料里这些行根本不存在 —— 所以"只有排除词"的查询在降级路径同样不完整：
    真实数据 `-报修` 索引 1,018,462 ／ 降级 877,386（少 141,076 行）。
    控制方裁决（§5.3）：这种形态也要触发 `fallback_text_only`。
    """
    return bool(parsed.get('keywords') or parsed.get('phrases')
                or parsed.get('or_groups') or parsed.get('regexes'))


def _own_wxid_in_index(conn, own_wxid):
    """索引里是否存在「本人」的发送者行（Critical #17 的兜底判定）。

    真实数据（裸 wxid）：`msg_meta.sender_username` 命中 **222,012** 行
    （群聊 36,382 / 非群聊 185,630，涉及 981 个非群聊会话）——索引**能**表达"我发的消息"，
    `发送者:我` 只需要正确的值形式（见 `_bare_wxid`）。
    本函数只回答"规范化之后仍然一行都没有吗"：那是换账号 / 索引由别的账号构建的症状，
    调用方据此**显式告知**用户，而不是静默返回 0（见 `_SELF_SENDER_MISSING_WARNING`）。
    走 `idx_meta_sender`（建索引时建的 sender 索引）的 `LIMIT 1` 探测，O(log n)。
    探测失败（表缺失等）返回 True = **不报警**：宁可少说，也不误报。
    """
    forms = [f for f in {own_wxid, _bare_wxid(own_wxid)} if f]
    if not forms:
        return True
    sql = ('SELECT 1 FROM msg_meta WHERE sender_username IN (%s) LIMIT 1'
           % ','.join('?' * len(forms)))
    try:
        return conn.execute(sql, forms).fetchone() is not None
    except sqlite3.Error:
        return True


def _needed_by(parsed, fts_expr, degraded):
    """给 T5-A8 的降级文案用：这次查询为什么需要文本索引。"""
    what = []
    if fts_expr:
        what.append('关键词')
    if parsed.get('regexes'):
        what.append('正则' if not degraded else '正则（已退化，需逐行确认原文）')
    if parsed.get('exclude'):
        what.append('排除词')
    return '、'.join(dict.fromkeys(what)) or '搜索'


def _slow_query_hint(parsed, degraded, scan_mode, candidate_rows):
    """T5-A6：退化 = 没用上预筛的慢路径，必须在返回结构里对用户可见。"""
    if not degraded:
        return None
    if scan_mode == SCAN_FILTER:
        return ('该正则较复杂（无法抽取必要字面量），已跳过索引预筛并全量扫描 %d 条消息，'
                '结果可能较慢' % candidate_rows)
    if scan_mode == SCAN_FTS:
        return ('该正则较复杂（无法抽取必要字面量），未用于预筛（本次仅按关键词预筛了 %d 条），'
                '确认阶段较慢' % candidate_rows)
    return '该正则较复杂（无法抽取必要字面量），本次查询退化为全量扫描，可能较慢'


def _normalize_per_page(per_page):
    """非正数一律当"未指定"（0/负数 → DEFAULT_PER_PAGE），上限 MAX_PER_PAGE。"""
    try:
        value = int(per_page)
    except (TypeError, ValueError):
        return DEFAULT_PER_PAGE
    if value <= 0:
        return DEFAULT_PER_PAGE
    return min(MAX_PER_PAGE, value)


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------

def search_messages(decrypted_dir, q, *, page=1, per_page=DEFAULT_PER_PAGE,
                    sort='time_desc', own_wxid=None, index_status=None,
                    max_retained=None, include_full_text=False, export_all=False):
    """执行搜索。返回 dict，键见 spec 第 7 节 + 本模块新增的可见性字段。

    返回结构：
      results          [{chat_id, chat_display_name, is_group, local_id, create_time,
                         local_type, type_label, sender_username, sender_display_name,
                         snippet, match_spans[, text]}, ...]
                         `text` 仅在 include_full_text=True 时出现（导出走全文）
      total/page/per_page/total_pages
      parsed           ParsedQuery（含 errors）
      index            index_status 的**原样**透传（Task 7 要把它整体给 UI）
      used_fallback    bool  是否走了降级路径（索引不存在/不可读，或文本索引为空）
      regex_degraded   bool  正则是否退化（未用上预筛 = 慢路径，T5-A6）
      scan_mode        'fts' | 'filter' | 'fallback'（本次实际走的形态）
      elapsed_ms       float 本次查询耗时（含 index_status）
      slow_query_hint  str | None  退化时的可读提示（T5-A6）
      candidate_rows   int  预筛出的候选行数（降级路径 = 语料行数；退化时 = 全量扫描规模）
      sender_filter_unsupported  bool  降级路径无法解析发送者（Task 6）
      fallback_text_only  bool  降级路径**只看得到有正文的消息**，而本次查询含
                               类型/日期/会话/标签/发送者筛选 → 图片/语音等可能缺失或
                               整体为空；**不代表库里没有**（Task 6 追加项）。
                               纯关键词/正则/排除词查询恒为 False（它们在降级路径完整）。
      truncated        bool  T16：是否有幸存行**因保留上限被丢弃**（结果被截断）
      retained_rows    int   T16：实际保留下来用于分页的幸存行条数
                             （未截断时 == `total`；被截断时 == 上限）
      warnings         [str] 未解析出的筛选词、文本索引不可用而降级、
                             降级语料只有正文、结果被截断等（不静默）

    参数 `max_retained`（T16）：
      * `None`（默认）⇒ 用模块常量 `MAX_RETAINED_ROWS`；
      * 传入正整数 ⇒ 用它当上限。**这是给测试与"对照实验"用的注入点**：
        夹具上把上限调到几条就能测截断，不必造 5 万行，也**不必**去猴子补丁改常量
        （改常量的测试测的是补丁本身，不是行为）。

    截断的适用范围（**有意设计**，不是漏做）
    ----------------------------------------
    只有**必须把幸存行物化在内存里**的两条路径受 `max_retained` 约束：
      1. 索引路径的 `_scan_indexed` 分支（含正则或排除词 → 必须先确认再分页）；
      2. 降级路径 `_execute_fallback`（索引不可用时直扫微信文本表）。
    **SQL 侧分页路径（`_page_indexed`）不受影响**：它只取当页（`LIMIT ? OFFSET ?`，
    ≤ `MAX_PER_PAGE` 行），内存本来就小 ⇒ 纯筛选（`类型:` / `日期:`）与关键词查询
    **仍可完整翻页**、也永远不会出现"超出保留范围"。截断的意义正是"把上限落在
    真正吃内存的形态上"，而不是把所有查询都变成不可翻页。

    截断的语义（**这是本轮最关键的不变式**）
    ----------------------------------------
    * `candidate_rows` 与 `total` **照旧精确统计**：扫描循环**走完全部候选行**，
      只是窗口之外的行不再物化（见 `_Retention`）。**绝不允许**为了让数字好看而
      截断 `total` —— 那会让用户把"我们只保留了这么多"读成"一共只有这么多"。
    * `total_pages` 仍按**精确 total** 计算。
    * `truncated=True` 时，页起点 ≥ `retained_rows` 的那些页返回**空 `results`**；
      调用方（UI/CLI）必须据此显示"当前页超出保留范围"，**不得**显示"没有匹配的消息"。
    * **绝不允许静默截断**：`truncated` / `retained_rows` 与人话 `warnings`
      必须同时出现；未截断时**不得**出现任何截断文案（防误报）。

    异常：
      ValueError                查询为空/无效（API 层 → 400）
      SearchIndexError          索引路径/降级路径的 SQL 失败（API 层 → 非 200，见类文档）

    ⚠️ T5-A8（Task 6 升级）：文本索引为空（`build_index(text=False)` / 表被清空）
    **不再抛 `TextIndexUnavailableError`**，而是降级直扫 + `used_fallback=True`
    + 一条 warnings；纯筛选在该状态下仍走 `msg_meta`（`used_fallback=False`、摘要为空）。
    """
    started = time.perf_counter()
    parsed = parse_query(q)
    if is_empty(parsed):
        raise ValueError('查询为空：请至少给出关键词、正则或一个筛选条件')

    page = max(1, int(page or 1))
    if export_all:
        # 导出：一次取保留窗口内全部命中（绕过 MAX_PER_PAGE=200）
        cap = MAX_RETAINED_ROWS if max_retained is None else max(0, int(max_retained))
        per_page = max(1, cap)
        page = 1
    else:
        per_page = _normalize_per_page(per_page)
    if sort not in ('time_desc', 'time_asc', 'chat'):
        sort = 'time_desc'

    scope = _resolve_scope(decrypted_dir, parsed, own_wxid)
    warnings = list(scope['warnings'])
    status = index_status or search_index.index_status(decrypted_dir)

    degraded = False
    literals = []
    if parsed['regexes']:
        for pattern in parsed['regexes']:
            literals.extend(extract_regex_literals(pattern))
        degraded = regex_is_degraded(literals)

    fts_expr = _positive_fts_expr(parsed)
    if not fts_expr and literals and not degraded:
        fts_expr = _literal_prefilter(literals)
    # 需要文本索引的形态：关键词/正则字面量预筛（fts_expr）、正则确认、排除词确认
    needs_text = bool(fts_expr or parsed['regexes'] or parsed['exclude'])

    conn = search_index.open_index(decrypted_dir) if status.get('ready') else None
    if conn is not None and needs_text:
        # T5-A8（Task 6 升级）：文本索引**实存但为空**（`build_index(text=False)` 之后
        # fts_rows=0 而 ready 仍为 True），或 `message_fts`/`msg_text` 被清空/掉表。
        # 此时对着空 FTS 表 `MATCH` 只会得到 0 行（一个"看起来正常"的空结果）。
        # 处理：关掉索引连接 → 走下面 `conn is None` 的降级直扫分支，并在 warnings 里留痕。
        text_index_reason = _text_index_reason(conn, status)
        if text_index_reason:
            conn.close()
            conn = None
            warnings.append(
                '文本索引不可用（%s），无法%s；本次已降级为直接扫描微信文本表'
                % (text_index_reason, _needed_by(parsed, fts_expr, degraded)))
    if conn is not None and own_wxid and scope.get('demanded_self'):
        # Critical #17 的诚实兜底：用户**确实**要「发送者:我」，但规范化后索引里
        # 一行本人发送者都找不到（换账号 / 索引由别的账号构建）→ 该查询必然返回 0。
        # 真实数据上不触发（裸 wxid 有 222,012 行）；触发时绝不能是"看起来正常的空结果"。
        if not _own_wxid_in_index(conn, own_wxid):
            warnings.append(_SELF_SENDER_MISSING_WARNING)
    used_fallback = False
    candidate_rows = 0
    page_rows = []
    total = 0
    rows = []
    # SQL 侧直接分页的条件：结果集**不由 Python 二次筛选**。
    # 含正则或排除词时必须先确认再分页（否则当页会被剔除的行打出空洞、total 也会错）。
    # 降级路径一律在 Python 侧分页（它本来就把行物化在内存里）。
    paged_in_sql = bool(conn is not None
                        and not parsed['regexes'] and not parsed['exclude'])
    # T16：保留窗口。**只在需要物化幸存行的形态上生效**（见 docstring）：
    # `_page_indexed`（SQL 侧 LIMIT 分页）连 `rows` 都不建，自然与上限无关。
    # 窗口**只限制物化条数**，计数（`window.count`）照旧精确。
    window = _Retention(MAX_RETAINED_ROWS if max_retained is None
                        else max(0, int(max_retained)))
    exact_total = 0
    try:
        if conn is None:
            rows = _execute_fallback(decrypted_dir, scope, parsed, literals, window)
            used_fallback = True
            scan_mode = SCAN_FALLBACK
            candidate_rows = window.count
            kept = _apply_excludes(rows, parsed)
            # 降级路径的 `_fallback_keep` 已经用**同一份**排除词口径做了早退
            # （`_exclude_terms` + `raw.lower()` 子串匹配），所以这里再跑一遍是**幂等**的
            # （有测试钉住：`test_fallback_exclude_early_out_is_idempotent`）。
            # 仍然把那点差额扣掉：万一将来两边分叉，精确 total 至少不会被悄悄抬高。
            exact_total = window.count - (len(rows) - len(kept))
            rows = kept
        else:
            scan_mode = SCAN_FTS if fts_expr else SCAN_FILTER
            if paged_in_sql:
                page_rows, total = _page_indexed(conn, scope, fts_expr,
                                                 page=page, per_page=per_page, sort=sort)
                candidate_rows = total
                exact_total = total
            else:
                # 正则确认 + 排除词确认都在**流式扫描内部**逐行完成，只留幸存行。
                # 排除词**不再**在事后重跑一遍：纯排除词形态的幸存行 ≈ 全量
                # （真实数据 1,018,563 / 1,018,918），再整批过滤一次纯属浪费；
                # 判定口径仍是同一份 `_keeps_row`（排除词的定义只有一条）。
                candidate_rows, rows = _scan_indexed(conn, scope, fts_expr,
                                                     parsed['regexes'],
                                                     _exclude_terms(parsed), window)
                exact_total = window.count
    except sqlite3.Error as exc:
        # T5-A1：SQL 失败必须与"真的没有结果"可区分 —— 向上抛结构化错误，绝不 return []
        raise SearchIndexError(
            '索引查询失败，可能需要重建索引',
            code='index_query_failed' if not used_fallback else 'fallback_query_failed',
            detail='%s: %s' % (type(exc).__name__, exc),
            hint='重建索引：POST /api/search/index（或 CLI build-search-index）') from exc
    finally:
        if conn is not None:
            conn.close()

    # 控制方追加项（§5.3 裁决，含"无正向文本条件"的扩展）：降级语料只有「有正文」的
    # 消息，而索引路径的纯筛选语料 `msg_meta` 是 100% 覆盖 → 索引缺失时「按类型/日期筛选」
    # 会少给甚至给空，用户会误以为「库里没有」。**只有排除词**（`-报修`）同理：
    # 无正文消息本应在结果里，降级路径却看不到它们（真实数据 1,018,462 → 877,386）。
    # 本轮不修语料，但**绝不允许静默**：结构化字段 + 人类可读文案同时给出；
    # 有正向文本条件（关键词/短语/OR/正则）且无非文本筛选时不触发（那是完整查询）。
    fallback_text_only = bool(used_fallback and (
        _has_non_text_filter(scope) or not _has_positive_text_condition(parsed)))
    if fallback_text_only:
        warnings.append(_FALLBACK_TEXT_ONLY_WARNING)

    if not paged_in_sql:
        # 非 SQL 分页的两条路径（降级路径、需要 Python 确认的索引路径）在这里**先整体排序、
        # 再切片**；顺序必须与 `_order_sql` 一致，否则同一查询在两种形态下顺序会变。
        if sort == 'time_asc':
            rows.sort(key=lambda r: (r[3] or 0, r[1] or 0))
        elif sort == 'chat':
            rows.sort(key=lambda r: ((r[0] or '').lower(), r[3] or 0))
        else:
            rows.sort(key=lambda r: (r[3] or 0, r[1] or 0), reverse=True)
        # T16：`total` 取**精确计数**（`window.count`，含因上限被丢弃的幸存行），
        # **不是** `len(rows)` —— 后者是"保留窗口内的条数"，拿它当 total 就是
        # 把截断污染成"一共只有这么多条"（本轮明令禁止的缺陷形态）。
        total = exact_total
        retained_rows = len(rows)
        start = (page - 1) * per_page
        page_rows = rows[start:start + per_page]
    else:
        # SQL 侧分页：普通查询当页即全集片段，无窗口截断；
        # export_all 时 LIMIT=per_page，若 total 更大则视为导出截断。
        retained_rows = len(page_rows) if export_all else total

    # T16：截断的**唯一**判据 = "实际保留的幸存行少于全部幸存行"。
    # 未截断时 `retained_rows == total` ⇒ `truncated=False`。
    truncated = bool(retained_rows < total)
    if truncated:
        # 结构化字段（上面 return 里的两个键）与人话文案**同时**给出（禁止静默截断）。
        warnings.append(_TRUNCATED_WARNING % (total, retained_rows))

    names = _contact_names(decrypted_dir)

    results = []
    for chat_id, local_id, local_type, create_time, sender, raw_text in page_rows:
        raw = raw_text or ''
        if export_all and include_full_text:
            # 导出走全文，跳过摘要高亮以节省时间
            snippet = raw[:200] + ('…' if len(raw) > 200 else '')
            rel = []
        else:
            spans = find_spans(raw, parsed, parsed['regexes'])
            snippet, rel = build_snippet(raw, spans)
        item = {
            'chat_id': chat_id,
            'chat_display_name': names.get(chat_id) or chat_id,
            'is_group': bool(chat_id and chat_id.endswith('@chatroom')),
            'local_id': local_id,
            'create_time': create_time,
            'local_type': local_type,
            'type_label': type_label(local_type),
            'sender_username': sender,
            'sender_display_name': names.get(sender) or sender,
            'snippet': snippet,
            'match_spans': rel,
        }
        if include_full_text:
            item['text'] = raw
        results.append(item)

    return {
        'results': results,
        'total': total,
        'page': page,
        'per_page': per_page,
        'total_pages': max(1, (total + per_page - 1) // per_page),
        'parsed': parsed,
        'index': status,
        'used_fallback': used_fallback,
        'regex_degraded': degraded,
        'scan_mode': scan_mode,
        'elapsed_ms': round((time.perf_counter() - started) * 1000.0, 3),
        'slow_query_hint': _slow_query_hint(parsed, degraded, scan_mode, candidate_rows),
        'candidate_rows': candidate_rows,
        'sender_filter_unsupported': bool(used_fallback and scope['senders']),
        'fallback_text_only': fallback_text_only,
        'truncated': truncated,
        'retained_rows': retained_rows,
        'warnings': warnings,
    }


# --------------------------------------------------------------------------
# 降级路径（索引未构建 / 文本索引为空时使用）
# --------------------------------------------------------------------------

_LIKE_ESCAPE = " ESCAPE '\\'"


def _fold_text(value):
    """→ 只保留**字母/数字**字符并小写 = FTS 侧短语的"有效 token 序列"。

    索引路径的短语走 `char_phrase()`：`to_char_token_text` 把每个**非空白**字符变成
    一个 token，而 FTS5 的 `unicode61` 分词器会把**标点**当分隔符丢掉。于是
    `char_phrase(p)` 的 MATCH 语义 = 「`_fold_text(原文)` 包含 `_fold_text(p)`」——
    与空白无关、也不受标点影响。

    降级路径必须用**同一把尺子**判定，否则两路径在短语上分叉（实测：`"服务器 宕机"`
    索引 73 / 降级 0）。`test_fold_matches_the_char_phrase_token_stream` 把
    "本函数 == `char_phrase` 的 token 序列投影"这条镜像关系锁住。

    ⚠️ 注意它比"只去空白"更强：`'服务器，宕机'` 与 `'服务器 宕机'` 折叠后相同，
    这正是索引侧的行为（`unicode61` 丢标点）。只去空白会漏掉前者的命中。
    """
    return ''.join(ch for ch in str(value or '').lower() if ch.isalnum())


def _make_like_pattern(word):
    """关键词/字面量 → LIKE 模式（转义 `\\` `%` `_`，配合 `ESCAPE '\\'`）。"""
    return '%' + (word or '').replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def _folded_like_pattern(word):
    """**折叠语义**的 LIKE 模式：逐字符 + `%` 间隔，与空白/标点无关（正确性**超集**）。

    `'服务器 宕机'` → `'%服%务%器%宕%机%'`；关键词 `'维修'` → `'%维%修%'`（同一套机制）。

    * 为什么不用"去掉空白后的字面量"`%服务器宕机%`：那会漏掉"**消息里带空白**"的形式
      （原文 `服务器 宕机` 在索引路径是命中的，字面 LIKE 却不命中）。
    * 为什么是超集：真匹配要求这些字符在原文里**按序出现**（中间的空白/标点被折叠掉），
      而模式允许字符之间夹着别的字符。精确判定交给 `_fold_text` 的包含确认。
      超集性有**穷举证明**：`TestPhrasePrefilterIsASuperSet` 用 6⁴ = 1296 个变体逐个断言。
    * 字符表用 `isalnum()`（与 `_fold_text` 同一把尺子），因此模式里只剩字面字符与 `%`，
      不需要 `ESCAPE` 转义。
    折叠后为空（纯标点/纯空白的词）→ 返回 `None`：它没有任何可匹配的 token，
    调用方必须把它当成"匹配 0 行"，**绝不能**退化成"匹配全部"。
    """
    chars = [ch for ch in str(word or '') if ch.isalnum()]
    if not chars:
        return None
    return '%' + '%'.join(chars) + '%'


def _fallback_text_groups(parsed):
    """正向文本条件 → `[('kw'|'phrase'|'or', [word, ...]), ...]`（**跨项 AND**）。

    这份**结构**有两个消费者，且必须完全一致：
      * SQL 预筛（`_fallback_text_where`）—— 只负责"不丢真命中"；
      * Python 精确确认（`_fallback_keep` + `_fallback_text_confirms`）—— 权威判定。
    两份实现一旦各拼各的，AND/OR 语义就会分叉（brief 模板的 OR 缺陷正是这么来的）。
    """
    groups = []
    for word in (parsed.get('keywords') or []):
        if word and str(word).strip():
            groups.append(('kw', [word]))
    for phrase in (parsed.get('phrases') or []):
        if phrase and str(phrase).strip():
            groups.append(('phrase', [phrase]))
    for group in (parsed.get('or_groups') or []):
        # OR 组只可能由关键词组成（parse_query 的 `_drop_pending_or` 把短语/正则挡在外面）
        words = [w for w in group if w and str(w).strip()]
        if words:
            groups.append(('or', words))
    return groups


def _fallback_text_where(parsed):
    """正向文本条件 → (WHERE 片段, 参数)；没有正向文本条件时返回 ('', [])。

    **每个 AND 项一个 LIKE；`OR 组` 内部用 OR 连接**（跨项才是 AND）。

    ⚠️ 绝不能像 brief 模板那样把所有词平铺成一个 `AND` 列表：
    `维修 OR 电话` 的 `or_groups` 展开后会被拼成 `c0 LIKE '%维修%' AND c0 LIKE '%电话%'`，
    于是"只含其中一个词"的消息被静默丢掉（漏召回 = 本方案最危险的一类缺陷）。
    同字段语义在 `search_query.to_fts_expr` 里也是这个形状：短语/关键词 AND、
    OR 组内部 OR。

    **关键词、短语、OR 成员一律用 `_folded_like_pattern`**（逐字符超集，与空白无关）——
    控制方 §15.5 裁决：关键词收紧到与索引路径的 char-token 语义一致
    （实测代价 0.52s → 0.50s，两路径在真实数据上逐行相等）。
    """
    clauses, params = [], []
    for _kind, words in _fallback_text_groups(parsed):
        patterns = []
        for word in words:
            pattern = _folded_like_pattern(word)
            if pattern is None:
                return '0', []          # 纯标点词：没有任何 token 可匹配 → 0 行（不放宽）
            patterns.append(pattern)
        clauses.append('(' + ' OR '.join(['c0 LIKE ?' + _LIKE_ESCAPE] * len(patterns)) + ')')
        params.extend(patterns)
    return ' AND '.join(clauses), params


def _fallback_text_confirms(folded_raw, folded_groups):
    """折叠后的原文是否满足**全部**正向文本条件（跨组 AND；`or` 组内部 OR）。"""
    for kind, folded_words in folded_groups:
        if kind == 'or':
            if not any(word in folded_raw for word in folded_words):
                return False
        elif not all(word in folded_raw for word in folded_words):
            return False
    return True


def _fallback_literal_where(literals):
    """非退化正则的字面量超集预筛（`c0 LIKE` 的 OR 组）。

    与索引路径的 `_literal_prefilter` 同义：字面量只是**超集**，最终由
    Python `re` 在原文上精确确认（见 `_execute_fallback`）。
    """
    words = [x for x in dict.fromkeys(literals or []) if x]
    if not words:
        return '', []
    return ('(' + ' OR '.join(['c0 LIKE ?' + _LIKE_ESCAPE] * len(words)) + ')',
            [_make_like_pattern(w) for w in words])


def _fallback_keep(row, scope, pats, exclude_terms, folded_groups=()):
    """一行 content 记录是否保留（作用域筛选 + 正向文本/正则确认 + 排除词早退）。"""
    raw, local_id, base_type, session_id, create_time = row
    if raw is None or session_id is None:
        return None
    raw = raw.decode('utf-8', 'replace') if isinstance(raw, (bytes, bytearray)) else str(raw)
    low = raw.lower()
    if any(t in low for t in exclude_terms):
        return None                      # 早退：语义所有者是 search_messages._apply_excludes
    if folded_groups:
        # 正向文本条件的**权威判定**：关键词/短语/OR 成员都按"折叠后包含"（= 索引路径的
        # char-token 语义）。SQL 预筛只是超集，这里才是精确判定 —— 少了它，`服务器` 会把
        # 只含这些字符但顺序不对的行也返回；只做字面预筛则漏掉带空白的形式（1387 vs 1389）。
        if not _fallback_text_confirms(_fold_text(raw), folded_groups):
            return None
    if pats and not any(p.search(raw) for p in pats):
        return None
    chat = scope['_sessions'].get(session_id)
    if not chat:
        return None                      # 解析不出会话 → 丢行（与 build_fts 的 skipped_no_chat 一致）
    if scope['chats'] and chat not in scope['chats']:
        return None
    if scope['label_chats'] is not None and chat not in scope['label_chats']:
        return None
    if scope['types'] and base_type not in scope['types']:
        return None
    if scope['date_from'] is not None and create_time < scope['date_from']:
        return None
    if scope['date_to'] is not None and create_time > scope['date_to']:
        return None
    return (chat, local_id, base_type, create_time, None, raw)


def _execute_fallback(decrypted_dir, scope, parsed, literals=None, retention=None):
    """降级路径：直接扫微信的 `message_fts_v4_*_content`（普通表）。

    索引未构建、或索引在但文本索引为空时使用。真实数据只读实测（877,842 行 / 4 张
    content 分表）：关键词 0.95–2.5s、两个关键词 AND 1.0–1.2s、`A OR B` 1.9–2.0s、
    可信正则 1.0s；**无预筛可用**的形态（纯筛选、纯排除词、退化正则）要全量过 Python
    实测 **11–28s**（`日期:2026` 11.6s、`类型:文本` 11.7s、退化正则 16s、`-词` 17–28s），
    而索引路径分别是 158ms / 426ms / 37.1s / 32.4s。慢但语义完整 —— 功能不依赖索引。
    筛选（会话/类型/日期/标签）与排除词在这里用 Python 完成：content 表的数值列没有
    B-Tree 索引，下推给 SQL 反而更慢。

    返回与 `_execute_indexed` **同形状**的行：
    `(chat_id, local_id, local_type, create_time, sender_username, raw_text)`
      升序不保证（由 `search_messages` 统一排序后分页）；
      `local_type` 已归一化为基类型；
      **第 6 列是原文**（`msg_text.raw_text` 同源语义：保留大小写/空白/换行）。
      绝不能返回 `to_char_token_text(raw)` —— 那样索引路径与降级路径的摘要/高亮/
      正则确认会静默分叉（A8 已把原文留在 `msg_text` 就是为了两边一致）。
      `sender_username` 恒为 `None`：content 表只有**分片内** sender_id，
      解析它要再扫一遍消息分片（`sender_filter_unsupported` 会显式告知调用方）。

    职责边界（Task 6 裁决）：
      * **正则必须在这里确认** —— `search_messages` 的降级分支不做二次确认，
        而字面量预筛只是超集，不确认就会返回「含字面量但不匹配正则」的行。
      * 排除词只做**早退**（避免把 87 万行物化进内存再丢掉）；语义所有者仍是
        `search_messages._apply_excludes`，行返回后它还会再确认一次（幂等）。
      * **不分页** —— 排序与切片都在 `search_messages`（两条路径同序，见 `_order_sql`）。

    T16（保留窗口）：与 `_scan_indexed` 同一套语义 —— 传入 `retention` 时只物化前
    `retention.limit` 条，但**照旧扫完全部行**并把幸存行总数记在 `retention.count`
    （`total` 的精确来源）。本路径同样把幸存行物化在内存里（真实数据 87 万行级），
    所以它**必须**受同一个上限约束：只在索引路径截断、忘了降级路径，等于漏一半。

    已知语义差异（**不是**本实现引入的，见报告 §已知差异）：LIKE 是**字面子串**，
    索引路径的 FTS char-token 短语**忽略空白**（并且会丢掉被 unicode61 视为分隔符的
    标点）。因此「原文本里插了空白的词」在两条路径上会不一致。真实数据只读实测：
    关键词命中 indexed=1389 / LIKE=1387 / 去空白子串=1388。
    代价对比：完全精确地复刻 FTS 语义要放弃 SQL 预筛 → 877,842 行全量搬到 Python
    实测 **16–34s**（vs 预筛 2.5s），因此保留预筛并把这个差异显式记录/报告。
    """
    if not decrypted_dir:
        return []
    # 布局必须与 `discover_message_shards` 一致（分片在 <dir>/message 或 <dir> 两种布局）
    fts_db = search_index._fts_db_path(decrypted_dir)
    if not fts_db or not os.path.isfile(fts_db):
        return []
    try:
        # 必须用 `_connect_ro`（pathlib.as_uri 转义）：含 `#`/`%` 的路径手写
        # `'file:%s?mode=ro'` 会截断成另一个路径、还会丢掉只读（见其文档）。
        conn = search_index._connect_ro(fts_db)
    except sqlite3.Error:
        return []
    try:
        try:
            tables = search_index.discover_fts_content_tables(conn)
        except sqlite3.Error:
            return []
        if not tables:
            return []
        sessions = {}
        try:
            for rid, uname in conn.execute('SELECT rowid, username FROM name2id'):
                if uname is None:
                    continue
                sessions[rid] = (uname.decode('utf-8', 'replace')
                                 if isinstance(uname, (bytes, bytearray)) else str(uname))
        except sqlite3.Error:
            sessions = {}                # 会话解析不了 → 所有行都会被丢掉（不猜、不编造 chat_id）
        scope = dict(scope)
        scope['_sessions'] = sessions

        where, params = _fallback_text_where(parsed)
        if not where and parsed.get('regexes') and not regex_is_degraded(literals or []):
            where, params = _fallback_literal_where(literals)

        pats = []
        for pattern in (parsed.get('regexes') or []):
            try:
                pats.append(re.compile(pattern))
            except re.error:
                continue                 # parse_query 已挡住非法正则；这里只是防御
        exclude_terms = [t.strip().lower() for t in (parsed.get('exclude') or [])
                         if t and t.strip()]
        # 正向文本条件的折叠形式（关键词/短语/OR 成员），结构与 SQL 预筛**同一份**
        # （`_fallback_text_groups`）。折叠后为空的词（纯标点）已被预筛压成 0 行，这里丢弃。
        folded_groups = []
        for kind, words in _fallback_text_groups(parsed):
            folded = [f for f in (_fold_text(w) for w in words) if f]
            if folded:
                folded_groups.append((kind, folded))

        out = _Retention() if retention is None else retention
        for table in tables:
            sql = 'SELECT c0, c1, c3, c4, c6 FROM [%s]' % table
            if where:
                sql += ' WHERE ' + where
            try:
                cursor = conn.execute(sql, params)
            except sqlite3.Error:
                continue                 # 单表读不了不影响其他分表（与 build_fts_ex 同取向）
            for row in cursor:
                base_type = (row[2] or 0) & 0xFFFFFFFF
                kept = _fallback_keep((row[0], row[1] or 0, base_type, row[3], row[4] or 0),
                                      scope, pats, exclude_terms, folded_groups)
                if kept is not None:
                    out.offer(kept)
        return out.rows
    finally:
        conn.close()
