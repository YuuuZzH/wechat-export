"""全局搜索的查询语法解析 —— 纯函数，无 IO。

语法（详见 docs/superpowers/specs/2026-09-21-global-search-design.md 第 4 节）：
    维修              关键词（任意长度中文子串）
    维修 电话          空格 = AND
    "服务器 宕机"      引号 = 短语
    维修 OR 报修       布尔 OR（或 |）；两侧都必须是关键词
    维修 -退货         - 前缀 = 排除（仅限关键词，不支持 -字段:值）
    /报修|维修/        斜杠包裹 = 正则
    会话:张三          会话（模糊匹配或原始 wxid）
    发送者:我          发送者；"我" 在 search.py 中解析为本人 wxid
    类型:图片,语音      消息类型（中文名或数字基类型）
    日期:2026-01-01..2026-03-31
    标签:only_work     通讯录标签
    会话:"my chat"     字段取值可用双引号包裹，以包含空格与逗号

同字段重复取 OR（会话:张三 会话:李四），跨字段取 AND。

字段取值可整段用双引号包裹，以便包含空格与逗号：

    会话:"my chat"      → chats == ['my chat']（一个值，含空格）
    会话:"a,b"          → chats == ['a,b']（一个值，引号内的逗号不切分）
    标签:"a b"          → labels == ['a b']
    类型:"图片","视频"   → types == [3, 43]（逗号在引号外仍是分隔符）
    会话:"a b","c d"    → chats == ['a b', 'c d']（UI 对每个含空格的值各自加引号）

引号内的逗号不是分隔符；引号内的空格属于值本身。
引号**不支持转义**：取值里不能再出现 '"'，所以「名字本身带引号」的会话无法用本语法表达。
未闭合的引号（如 '会话:"abc'）产出结构化 error，字段**不会**被塞入半截值。
"""
import calendar
import re
from datetime import datetime

from engine.constants import TZ

# 消息类型基类型 → 中文名（与 engine/services/message MSG_TYPE_LABELS 语义一致）
TYPE_ALIASES = {
    '文本': 1, '文字': 1,
    '图片': 3, '照片': 3,
    '文件': 6,
    '语音': 34, '音频': 34,
    '名片': 42,
    '视频': 43,
    '表情': 47,
    '位置': 48, '定位': 48,
    '链接': 49, '应用': 49, '链接/应用': 49,
    '网络电话': 50, '通话': 50,
    '系统': 10000, '系统消息': 10000,
    '撤回': 10002,
}

VALID_FIELDS = ('会话', '发送者', '类型', '日期', '标签')

_FIELD_RE = re.compile(r'^(%s)[:：](.+)$' % '|'.join(VALID_FIELDS))
# 同 VALID_FIELDS，但在串内任意位置识别 '字段:"'：用 .match(q, i) 锚定在 i，
# 所以不能带 '^'（'^' 只锚在串首）。结尾是前瞻，故 m.end() 正好是那个引号的下标。
_FIELD_QUOTED_RE = re.compile(r'(%s)[:：](?=")' % '|'.join(VALID_FIELDS))
_OR_TOKENS = ('or', '|')

# OR 只有两侧都是关键词才有意义：字段过滤器/短语/正则不参与 OR，
# 出现这类组合时宁可报错，也不静默降级成 AND。
_OR_TERM_REQUIRED = ('OR 需要左右两侧都是关键词（不支持字段过滤器参与 OR），'
                     '右侧请用关键词或改用空格 AND')

_EMPTY = {
    'keywords': [], 'phrases': [], 'or_groups': [], 'exclude': [],
    'regexes': [], 'chats': [], 'senders': [], 'types': [],
    'date_from': None, 'date_to': None, 'labels': [], 'errors': [],
}


def _new_query():
    return {k: (list(v) if isinstance(v, list) else v) for k, v in _EMPTY.items()}


def _err(token, message):
    return {'token': token, 'message': message}


# --------------------------------------------------------------------------
# 日期
# --------------------------------------------------------------------------

def _period_start(s):
    """'2026-03-05' / '2026-03' / '2026' → 区间起点的 epoch 秒。"""
    s = (s or '').strip()
    for fmt in ('%Y-%m-%d', '%Y-%m', '%Y'):
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return int(dt.replace(tzinfo=TZ).timestamp())
    return None


def _period_end(s):
    """同上 → 区间终点（含该日/月/年最后一秒）的 epoch 秒。"""
    s = (s or '').strip()
    for fmt in ('%Y-%m-%d', '%Y-%m', '%Y'):
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if fmt == '%Y-%m-%d':
            end = dt
        elif fmt == '%Y-%m':
            end = dt.replace(day=calendar.monthrange(dt.year, dt.month)[1])
        else:
            end = dt.replace(month=12, day=31)
        end = end.replace(hour=23, minute=59, second=59, tzinfo=TZ)
        return int(end.timestamp())
    return None


def parse_date_value(text):
    """支持 '2026-03-05'、'2026-03'、'2026'、'A..B'、'..B'、'A..'。

    返回 (date_from, date_to)，无法识别的一侧为 None。
    """
    text = (text or '').strip()
    if not text:
        return None, None
    if '..' in text:
        left, right = text.split('..', 1)
        lo = _period_start(left) if left.strip() else None
        hi = _period_end(right) if right.strip() else None
        return lo, hi
    return _period_start(text), _period_end(text)


# --------------------------------------------------------------------------
# tokenize
# --------------------------------------------------------------------------

def _scan_quoted_field(q, start):
    """q[start] 是字段取值的开引号 → 返回该 token 结束的下标（闭引号之后一位）。

    支持 '字段:"a b"'，也支持 UI 会给每个含空格的值各自加引号的
    '字段:"a b","c d"' —— 后半段必须并进**同一个** token，否则 ',' 开头的
    片段会被 _FIELD_RE 拒掉、变成自由关键词（静默错查询）。
    未闭合返回 None，调用方据此产出 error token（不静默截断）。
    """
    j = start
    while True:
        end = q.find('"', j + 1)
        if end == -1:
            return None
        j = end + 1
        if q[j:j + 2] != ',"':      # 只有「逗号 + 引号」才是同字段的下一个取值
            return j
        j += 1                      # 跳过逗号，q[j] 重新指向下一段的开引号


def _tokenize(q):
    """→ [(kind, value)]，kind ∈ {'term','phrase','regex','error'}。

    引号与斜杠内的空白不切分；'字段:"带 空格的值"' 也是单个 term；
    引号未闭合时产出 error token（含字段引号那一支）。
    """
    tokens = []
    i = 0
    n = len(q)
    while i < n:
        ch = q[i]
        if ch.isspace():
            i += 1
            continue
        if ch == '"':
            j = q.find('"', i + 1)
            if j == -1:
                tokens.append(('error', q[i:]))
                break
            tokens.append(('phrase', q[i + 1:j]))
            i = j + 1
            continue
        if ch == '/':
            j = i + 1
            buf = []
            closed = False
            while j < n:
                if q[j] == '\\' and j + 1 < n:
                    buf.append(q[j:j + 2])
                    j += 2
                    continue
                if q[j] == '/':
                    closed = True
                    break
                buf.append(q[j])
                j += 1
            if not closed:
                tokens.append(('error', q[i:]))
                break
            tokens.append(('regex', ''.join(buf)))
            i = j + 1
            continue
        m = _FIELD_QUOTED_RE.match(q, i)
        if m:
            end = _scan_quoted_field(q, m.end())
            if end is None:
                tokens.append(('error', q[i:]))
                break
            tokens.append(('term', q[i:end]))
            i = end
            continue
        j = i
        while j < n and not q[j].isspace():
            j += 1
        tokens.append(('term', q[i:j]))
        i = j
    return tokens


# --------------------------------------------------------------------------
# 字段过滤器
# --------------------------------------------------------------------------

def _unquote_value(s):
    """去掉一段取值的外层引号（'"a b"' → 'a b'），再去掉首尾空白。"""
    s = (s or '').strip()
    if len(s) >= 2 and s.startswith('"') and s.endswith('"'):
        return s[1:-1].strip()
    return s


def _split_field_values(raw):
    """把字段原始取值切成多个值：**只在引号外**的逗号处切分，再逐段去掉外层引号。

    '图片,视频'         → ['图片', '视频']
    '"a,b"'             → ['a,b']            （引号内的逗号属于值本身）
    '"图 片","视 频"'    → ['图 片', '视 频']  （每段各自去引号）
    '""' / 'a,,b'       → 空段被丢弃，全空时为 []（调用方报「缺少取值」）

    引号不支持转义，故值里不能含 '"'。
    """
    segments = []
    buf = []
    in_quotes = False
    for ch in raw or '':
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ',' and not in_quotes:
            segments.append(''.join(buf))
            buf = []
            continue
        buf.append(ch)
    segments.append(''.join(buf))
    return [v for v in (_unquote_value(s) for s in segments) if v]


def _apply_field(parsed, field, raw, token):
    """把一个 '字段:值' 落到 parsed 上。返回是否成功识别。"""
    values = _split_field_values(raw)
    if not values:
        parsed['errors'].append(_err(token, '字段 "%s" 缺少取值' % field))
        return True

    if field == '会话':
        parsed['chats'].extend(values)
    elif field == '发送者':
        parsed['senders'].extend(values)
    elif field == '标签':
        parsed['labels'].extend(values)
    elif field == '类型':
        for v in values:
            # isdecimal() 才是 int() 能吃的那一类：isdigit() 对上标/带圈数字
            # （'²'、'①'）也返回 True，随后 int() 会抛 ValueError 逃出 parse_query。
            if v.isdecimal():
                parsed['types'].append(int(v))
                continue
            base = TYPE_ALIASES.get(v)
            if base is None:
                parsed['errors'].append(
                    _err(token, '未知消息类型 "%s"（可用：%s）'
                         % (v, '、'.join(sorted(set(TYPE_ALIASES))))))
            else:
                parsed['types'].append(base)
    elif field == '日期':
        lo, hi = parse_date_value(values[0] if len(values) == 1 else raw)
        if lo is None and hi is None:
            parsed['errors'].append(
                _err(token, '无法解析日期 "%s"（支持 2026-03-05 / 2026-03 / 2026 / A..B）' % raw))
        else:
            if lo is not None:
                parsed['date_from'] = lo if parsed['date_from'] is None else max(parsed['date_from'], lo)
            if hi is not None:
                parsed['date_to'] = hi if parsed['date_to'] is None else min(parsed['date_to'], hi)
    return True


# --------------------------------------------------------------------------
# 主解析
# --------------------------------------------------------------------------

def _drop_pending_or(parsed, items):
    """挂起的 OR 组右侧不是自由文本词 → 记错并丢弃该组。

    不丢弃的话，后面任意一个关键词都会悄悄把空格 AND 变成 OR。
    """
    if items and items[-1][0] == 'or' and items[-1][1] and items[-1][1][-1] == '':
        items.pop()
        parsed['errors'].append(_err('OR', _OR_TERM_REQUIRED))


def parse_query(q):
    """把查询串解析为结构化 ParsedQuery。任何输入都不抛异常，问题进 errors。"""
    parsed = _new_query()
    if not q or not q.strip():
        return parsed

    items = []          # [('kw', str)] 或 [('or', [str, ...])]
    for kind, value in _tokenize(q):
        if kind == 'error':
            parsed['errors'].append(_err(value, '引号或正则未闭合'))
            continue
        if kind == 'regex':
            _drop_pending_or(parsed, items)
            if not value:
                parsed['errors'].append(_err('/', '空正则'))
                continue
            try:
                re.compile(value)
            except Exception as exc:
                # 这里的 compile 只为校验：任何异常都等价于「Python 编译不了这个模式」，
                # 一律转成结构化错误（逐个列举会漏：re.error / RecursionError / OverflowError 都遇到过）。
                parsed['errors'].append(
                    _err(value, '正则语法错误：%s: %s' % (type(exc).__name__, exc)))
                continue
            parsed['regexes'].append(value)
            continue
        if kind == 'phrase':
            _drop_pending_or(parsed, items)
            if value.strip():
                parsed['phrases'].append(value)
            continue

        # kind == 'term'
        if value.lower() in _OR_TOKENS:
            if not items:
                parsed['errors'].append(_err(value, _OR_TERM_REQUIRED))
                continue
            last = items.pop()
            if last[0] == 'or':
                items.append(('or', last[1] + ['']))
            else:
                items.append(('or', [last[1], '']))
            continue

        negated = value.startswith('-') and len(value) > 1
        body = value[1:] if negated else value
        if negated and _FIELD_RE.match(body):
            _drop_pending_or(parsed, items)
            parsed['errors'].append(_err(value, '不支持排除字段过滤器，请改用 -关键词 排除内容'))
            continue
        m = _FIELD_RE.match(body)
        if m:
            _drop_pending_or(parsed, items)
            _apply_field(parsed, m.group(1), m.group(2), value)
            continue
        if negated:
            _drop_pending_or(parsed, items)
            parsed['exclude'].append(body)
            continue
        if items and items[-1][0] == 'or' and items[-1][1] and items[-1][1][-1] == '':
            items[-1] = ('or', items[-1][1][:-1] + [body])
            continue
        items.append(('kw', body))

    for kind, value in items:
        if kind == 'kw':
            parsed['keywords'].append(value)
        else:
            if value and value[-1] == '':
                parsed['errors'].append(_err('OR', _OR_TERM_REQUIRED))
            group = [w for w in value if w]
            if len(group) >= 2:
                parsed['or_groups'].append(group)
            elif group:
                parsed['keywords'].append(group[0])
            else:
                parsed['errors'].append(_err('OR', 'OR 两侧缺少词项'))
    return parsed


def is_empty(parsed):
    """没有任何有效条件时为 True（调用方据此返回 400，而不是倾倒全表）。"""
    if not parsed:
        return True
    for key in ('keywords', 'phrases', 'or_groups', 'exclude', 'regexes',
                'chats', 'senders', 'types', 'labels'):
        if parsed.get(key):
            return False
    return parsed.get('date_from') is None and parsed.get('date_to') is None


# --------------------------------------------------------------------------
# FTS 表达式构造
# --------------------------------------------------------------------------

def to_char_token_text(s):
    """把文本转成空格分隔的单字符 token 串。

    必须与 src/web/reports/wrapped/adapter.py::_to_char_token_text 完全一致
    （有测试锁定）。char-token 让标准 unicode61 分词器能对中文做任意长度子串匹配 ——
    trigram 分词器要求 >=3 字符，双字词（"维修"）会全部漏掉。
    """
    t = str(s or '').strip()
    if not t:
        return ''
    chars = [ch for ch in t.lower() if not ch.isspace()]
    return ' '.join(chars)


def char_phrase(word):
    """产出一个 FTS5 短语查询，如 '维修' → '"维 修"'。

    内部空白被去掉，因此短语语义 = 原文中的连续子串。

    短语里的 '"' 必须按 FTS5 规则**双写**（FTS5 用 `""` 表示字符串内的一个引号）：
    'a"b' 的 token 串是 'a " b'，不双写就产出 '"a " b"' —— 一个**未闭合**的短语，
    整个 MATCH 表达式随即报 `unterminated string` 语法错误。双写后是 '"a "" b"'。
    """
    tokens = to_char_token_text(word)
    if not tokens:
        return ''
    return '"%s"' % tokens.replace('"', '""')


def to_fts_expr(parsed):
    """把 ParsedQuery 的文本类条件组装成 FTS5 MATCH 表达式。

    只处理 keywords / phrases / or_groups / exclude；正则与字段过滤器不在这里
    （正则需先抽字面量，字段过滤器由 msg_meta 承担）。无文本条件时返回 None。

    **排除词是中缀 `NOT`**（FTS5 里 NOT 是二元中缀运算符，优先级高于 AND）：

        维修 -退货          → '"维 修" NOT "退 货"'
        维修 -退货 -报修      → '"维 修" NOT "退 货" NOT "报 修"'
        维修 OR 报修 -报修    → '("维 修" OR "报 修") NOT "报 修"'

    即 `(正向表达式) NOT "排除"`，语义 = A ∧ ¬x。写成前缀或 `AND NOT`
    （本函数 2026-09 之前的实现）是**非法表达式**，实测：

        '… AND NOT "退 货"'  -> fts5: syntax error near "NOT"
        'NOT "退 货"'        -> fts5: syntax error near "NOT"

    只有排除词、没有任何正向条件时**返回 None**：此时不存在合法形态（见上），
    调用方应全量扫描，或在原文上做排除。

    调用方若为跨路径一致性选择在整形阶段用**原文子串**排除（Task 5 的
    `_positive_fts_expr` + `_apply_excludes` 即如此），可把 `exclude` 置空的
    ParsedQuery 传进来，本函数就只组装正向条件。一般不建议把排除词交给 MATCH：
    那样索引路径与降级路径的排除语义会不一致。
    """
    if not parsed:
        return None
    parts = []
    for word in parsed.get('phrases') or []:
        p = char_phrase(word)
        if p:
            parts.append(p)
    for word in parsed.get('keywords') or []:
        p = char_phrase(word)
        if p:
            parts.append(p)
    for group in parsed.get('or_groups') or []:
        subs = [char_phrase(w) for w in group]
        subs = [s for s in subs if s]
        if len(subs) >= 2:
            parts.append('(' + ' OR '.join(subs) + ')')
        elif subs:
            parts.append(subs[0])
    if not parts:
        # 没有任何正向条件（例如只有 '-退货'）：FTS5 没有前置 NOT，无法组成合法表达式。
        # 返回 None 让调用方全量扫描 —— 绝不返回非法字符串。
        return None
    expr = ' AND '.join(parts)
    for word in parsed.get('exclude') or []:
        p = char_phrase(word)
        if p:
            # 中缀追加：NOT 优先级高于 AND，所以 'A AND B NOT "x"' ≡ A ∧ B ∧ ¬x
            expr += ' NOT ' + p
    return expr


# --------------------------------------------------------------------------
# 正则字面量抽取（FTS 超集预筛用）
# --------------------------------------------------------------------------

_REGEX_META = set('.^$*+?{}[]()|\\')


def _has_extension_group(pattern):
    """模式里是否出现 '(?…' 形式的扩展组（非捕获组 '(?:' 除外）。

    只做**明文扫描**，不解析正则词法：真扩展组在模式文本里就是字面的 '(' '?'
    两个字符，所以「逐个检查每一处 '(?'」不可能漏掉任何一个真扩展组 ——
    唯一被放行的是 '(?:'，它不是扩展组。扫描故意允许误报（字符类里的 '[(?]'、
    转义过的 '\\(\\?' 也会被抓到）：误报的后果只是退化为全量扫描（慢但正确），
    漏报才会静默丢结果。取舍方向固定，见 extract_regex_literals 的契约说明。
    """
    j = pattern.find('(?')
    while j != -1:
        if pattern[j + 2:j + 3] != ':':
            return True
        j = pattern.find('(?', j + 2)
    return False


def _quantifier_min(text, i):
    """text[i:] 是量词时返回它的最小重复次数，否则返回 None。

    返回 0 表示「可以一次都不出现」：被它修饰的字面量不必然出现在匹配里，
    不能当预筛条件。认不出来的 `{...}`（Python 当字面量）也按 0 处理 ——
    宁可退化去全量扫描，也不冒漏结果的风险。
    """
    if i >= len(text):
        return None
    ch = text[i]
    if ch == '*' or ch == '?':
        return 0
    if ch == '+':
        return 1
    if ch != '{':
        return None
    j = text.find('}', i + 1)
    if j == -1:
        return None
    head = text[i + 1:j].split(',', 1)[0]
    if not head.isdecimal():
        return 0
    return int(head)


def _skip_char_class(text, i):
    """从 text[i] == '[' 跳过整个字符类，返回其后的下标。

    类内首个字符可以是 '^'（取反）或 ']'（普通字符），类内的 '\\' 转义有效。
    未闭合（无效正则）时保守地只跳过 '[' 本身。
    """
    j = i + 1
    if j < len(text) and text[j] == '^':
        j += 1
    if j < len(text) and text[j] == ']':
        j += 1
    while j < len(text):
        if text[j] == '\\':
            j += 2
            continue
        if text[j] == ']':
            return j + 1
        j += 1
    return i + 1


def _skip_quantifier(text, i):
    """从 text[i] == '{' 跳过量词体（如 {2} / {2,5} / {2,}），返回其后的下标。

    没有配对的 '}' 时只跳过 '{' 本身。
    """
    j = text.find('}', i + 1)
    return i + 1 if j == -1 else j + 1


def extract_regex_literals(pattern):
    r"""返回正则**每个 `|` 分支**的最长可用字面量；抽不到的分支为 ''。

    契约（有不变式测试锁定）：**超集预筛** —— 任一真匹配都必然包含至少一个抽出的
    字面量。满足它，结果才能安全地 OR 起来做 FTS 预筛、再由 Python `re` 精确确认；
    返回 '' 的分支意味着无法预筛（regex_is_degraded 为 True，调用方退化为全量扫描）。
    取舍方向：宁可退化去全量扫描（慢但正确），绝不漏结果。

    **只信任普通组 `(...)` 与非捕获组 `(?:...)`。** 模式里出现任何别的 `(?…`
    扩展组 —— (?= (?! (?<= (?<! (?> (?P< (?P= (?# (?i (?i: (?x (?m (?s (?a (?u (?-
    等等 —— 整条模式一律不信，各分支返回 '' → regex_is_degraded 为 True →
    调用方全量扫描。见 `_has_extension_group`（只做明文扫描，允许误报）。

    为什么不再逐个建模扩展组：这些组会改变「模式文本怎么被解释」（词法边界、
    大小写作用域、VERBOSE 的忽略空白与注释），三轮修复各建模一种就各漏一种 ——
    (?i) 作用域被上层白名单标志重置、(?x) 的忽略空白与 `#` 注释、(?x:…) 跳过扫描
    的注释边界、(?#…) 注释里的括号（CPython 到**第一个** ')' 就结束注释，
    数括号的扫描会跑进注释体）。四次丢结果都落在同一处：解析 `(?…)`。既然手写
    这套词法本身就是缺陷来源，就不要再写——退化的代价只是慢，漏结果的代价是错。

    为满足不变式，这里做四件事：
      1. 任意深度的 `|`（字符类与转义之外）都切分支：'(报修|维修)电话' →
         ['报修', '维修']，两种匹配各被一个分支覆盖；'(a|b)c' → ['a', 'b']。
      2. 被零次量词削弱的字面量丢弃：'服务器*宕机' 里 '服务器' 可选（'服务宕机'
         也匹配）→ 弃之；'宕机' 必然出现 → ['宕机']。'*' '?' '{0,...}' 算削弱；
         '+' 是一次以上，不算；组被零次量词修饰时组内字面量一并削弱。
      3. 字符类、量词体、转义标识（含 \xNN \uNNNN \1 的参数）都不是字面量。
      4. 无效正则（括号不配等，parse_query 已挡住）一律保守判为削弱 → 退化为全量扫描。

    本函数只看模式文本；调用方若另行用 re.IGNORECASE / re.VERBOSE 等编译标志，
    需自行承担相应语义（这类模式请让调用方直接走全量扫描）。
    """
    pattern = pattern or ''
    if _has_extension_group(pattern):
        return ['']
    branches = [[]]         # branches[b] = [['字面量', 是否被削弱], ...]
    flat = []               # 与 branches 共享同一批 list 对象，便于按区间标记削弱
    stack = []              # 未闭合 '(' 处的 flat 下标（统一处理 (...) 与 (?:...)）
    run = []
    i = 0
    n = len(pattern)

    def flush(next_i):
        """收掉当前 run；被零次量词（必须可省略）修饰的候选记为被削弱。"""
        if not run:
            return
        entry = [''.join(run), _quantifier_min(pattern, next_i) == 0]
        branches[-1].append(entry)
        flat.append(entry)
        run.clear()

    while i < n:
        ch = pattern[i]
        if ch == '\\' and i + 1 < n:
            # 转义字母数字都不是字面量，而且带参数的转义必须把参数一起吞掉 ——
            # 否则 '\x61' 里的 '61'、'\u7ef4' 里的 '7ef4' 会被当成字面量：
            #   \d\w\s\b 是字符类/断言，\n\t 是控制字符，
            #   \1..\99 是反向引用/八进制（后面紧跟的数字属于这个转义），
            #   \xNN \uNNNN \UNNNNNNNN 是码点转义（\N{...} 的 {..} 由量词分支跳过）。
            # 只有「转义的非字母数字」（\. \* \- \ 等）才等于该字符本身。
            nxt = pattern[i + 1]
            if nxt.isdecimal():
                i += 2
                while i < n and pattern[i].isdecimal():
                    i += 1
                run.clear()
                continue
            if nxt in 'xuU':
                i = min(n, i + 2 + {'x': 2, 'u': 4, 'U': 8}[nxt])
                run.clear()
                continue
            if nxt.isalnum():
                run.clear()
            else:
                run.append(nxt)
            i += 2
            continue
        if ch == '[':
            flush(i)
            i = _skip_char_class(pattern, i)
            continue
        if ch == '{':
            flush(i)
            i = _skip_quantifier(pattern, i)
            continue
        if ch == '(':
            # 扩展组已在函数入口整条退化；这里只剩 '(' 与 '(?:' 两种。'(?:' 的
            # '?' 与 ':' 和组体一样不是字面量，整段跳过（i+2 是那个 ':'）。
            flush(i)
            stack.append(len(flat))
            i += 3 if pattern[i + 2:i + 3] == ':' else 1
            continue
        if ch == ')':
            flush(i)
            if stack:
                start = stack.pop()
                if _quantifier_min(pattern, i + 1) == 0:
                    for entry in flat[start:]:
                        entry[1] = True
            else:
                # 多余的 ')'（无效正则）：保守判为全部削弱
                for entry in flat:
                    entry[1] = True
            i += 1
            continue
        if ch == '|':
            flush(i)
            branches.append([])
            i += 1
            continue
        if ch in _REGEX_META:
            flush(i)
            i += 1
            continue
        run.append(ch)
        i += 1
    flush(n)
    if stack:
        # 未闭合的 '('（无效正则）：保守判为全部削弱
        for entry in flat:
            entry[1] = True

    return [max((text for text, weakened in cands if not weakened), key=len, default='')
            for cands in branches]


def regex_is_degraded(literals):
    """任一分支抽不到字面量 → 只能全量扫描，调用方应提示用户。"""
    if not literals:
        return True
    return any(not lit for lit in literals)
