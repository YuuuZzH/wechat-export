"""前端搜索页（Task 9 Phase 2）—— `/search` 的**静态契约**与**防撞名护栏**。

为什么单独一个文件（而不是追加进 `tests/test_search_api.py`）：
`test_search_api.py` 正被另一个任务（修 flaky 线程测试）编辑，两个写者碰同一个
文件必然互相覆盖。页面级断言因此单独成文件。

本文件能测什么、不能测什么（这一点必须说清楚，否则就是"只查静态 HTML 的假测试"）：

  * **能测**：`/search` 的 HTTP 契约、控件 id、服务端渲染的类型选项、脚本加载顺序、
    以及"页面脚本没有踩到撞名/未定义全局"这类**加载期**缺陷。
  * **不能测**：DOM 渲染后的行为（pytest 不执行 JS）。那部分交给
    `tests/test_search_js.py` 的 **classic-script load guard**：它在一个 vm realm 里
    按页面真实顺序加载全部五个文件（含 `search-app.js`），断言初始化真的跑完、
    两个命名空间存在，并把索引失败 / 文本索引缺失时的界面判定也断言在 DOM 上。

为什么"脚本顺序"和"经典脚本加载"是**行为性**断言而不是装饰 —— 本项目刚发生过
两次同类事故：
  ① 计划缺陷 #15：`search-app.js` 引用了项目里根本不存在的全局 `TYPES`，
     `ReferenceError` 让整页初始化中断，页面只剩静态 HTML，而
     "`'search-type' in html`" 这种断言**照样通过**；
  ② Phase 1 实测：`utils.js` 顶层有 `const escapeHtml`，若 `search-render.js` 用
     顶层 `function escapeHtml` 声明，浏览器在**实例化阶段**抛
     `SyntaxError: Identifier 'escapeHtml' has already been declared`，整个文件不执行，
     `SearchRender.*` 全 undefined —— 而所有 `require()` 断言全绿
     （CommonJS 把模块包进函数作用域，撞名根本不会发生）。

因此本文件断言脚本顺序**恰好等于**那 5 个文件（顺序常量与
`tests/test_search_js.py` 的加载守卫**同源**，不各写一份），并用源码级断言钉住
"页面只以命名空间形态引用 phase-1 的纯函数"。

全部使用**合成**夹具，绝不读取真实微信数据、`backup/`。
"""
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

from web.app import create_app
from tests.test_search_index import _make_shard, _make_fts_db
from tests.test_search_js import SCRIPT_ORDER, _node_or_skip, _run_node
from engine.services import media as media_mod
from engine.services import search_index as si
from engine.services.search_query import TYPE_ALIASES

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(ROOT, 'src', 'web', 'static', 'js')
CSS_PATH = os.path.join(ROOT, 'src', 'web', 'static', 'css', 'app.css')

# T7-A7①/T9-A6：类型下拉的 12 个规范名（服务端从 `TYPE_ALIASES` 每基类型取首个别名）
CANONICAL_TYPE_NAMES = ('文本', '图片', '文件', '语音', '名片', '视频',
                        '表情', '位置', '链接', '网络电话', '系统', '撤回')

# 页面必须存在的控件（`search-app.js` 用 `getElementById` 逐个取；少一个就是
# `els.X === null` → 初始化时 TypeError → 整页功能全死）
REQUIRED_CONTROL_IDS = (
    'search-input', 'search-chat', 'search-sender', 'search-type',
    'search-date-from', 'search-date-to', 'search-label', 'search-run',
    'search-syntax', 'search-help', 'search-help-toggle',
    'search-index-bar', 'search-results',
)

# 只列规范名、**不**列同义词（同义词由后端 `TYPE_ALIASES` 负责解析）
TYPE_SYNONYMS = ('照片', '音频', '文字', '定位', '应用', '通话', '系统消息',
                 '链接/应用', '视频通话', '群聊', '红包')

# `search-app.js` 必须用命名空间调用相位 1 的纯函数（Phase 1 报告 §1.1）；
# 裸调用在浏览器里就是 `ReferenceError: formToSyntax is not defined`
NAMESPACE_REQUIRED = ('SearchSyntax.', 'SearchRender.')

# 页面必须真的用到的命名空间成员（T16 追加 `truncatedPageNotice`：空页分级判据）
NAMESPACE_MEMBERS_USED = ('SearchRender.truncatedPageNotice',)

# 这些名字**只**以命名空间成员存在（IIFE 内部）+ 与 utils.js 顶层撞名的名字
NAMESPACED_NAMES = ('formToSyntax', 'syntaxToForm', 'tokenizeQuery', 'quoteIfNeeded',
                    'statusNotice', 'slowNotice', 'truncatedPageNotice',
                    'formatResult', 'highlightHtml',
                    'normalizeSpans', 'formatTimeText')


def _js(name):
    with open(os.path.join(JS_DIR, name), encoding='utf-8') as fh:
        return fh.read()


def _css():
    with open(CSS_PATH, encoding='utf-8') as fh:
        return fh.read()


def _canonical_type_names():
    """与实现**同一条**推导（`TYPE_ALIASES` 每基类型取首个别名），不另写硬编码列表。"""
    seen, out = set(), []
    for name, base in TYPE_ALIASES.items():
        if base in seen:
            continue
        seen.add(base)
        out.append(name)
    return out


def _strip_comments_and_strings(src):
    """去掉注释与字符串字面量（`//`、`/* */`、`'`、`"`、`` ` ``、正则字面量）。

    纯文本层面（不是 JS 词法器）的小状态机，只服务于本文件的源码扫描，目的有两个：
      * 不被注释里的示例代码骗到 —— 例如文件头注释里提到 `TYPES` 这个**不存在的
        全局**时，`test_type_option_list_is_not_built_in_js` 不该因此假红；
      * 不被中文文案里的 `formToSyntax` 之类字样骗到。

    正则字面量必须识别：`escapeHtml` 里就有 `/'/g`，把那个 `/` 当成除法、
    再把它后面的 `'` 当成字符串开头，会让状态机**失步**，其后的所有扫描结果都不可信。
    判据用常见启发式：`/` 的前一个有效字符属于
    `( , = : [ ! & | ? { } ;`（或文件开头）时按正则字面量处理，否则按除法。
    """
    out = []
    i, n = 0, len(src)
    state = 'code'
    prev_sig = ''
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ''
        if state == 'code':
            if c == '/' and nxt == '/':
                state = 'line'; i += 2; continue
            if c == '/' and nxt == '*':
                state = 'block'; i += 2; continue
            if c in ('"', "'", '`'):
                state = c; i += 1; out.append(' '); continue
            if c == '/' and prev_sig in ('', '(', ',', '=', ':', '[', '!', '&', '|',
                                         '?', '{', '}', ';'):
                # 正则字面量：跳到未被转义、且不在字符类里的收尾 `/`
                i += 1
                in_class = False
                while i < n:
                    ch = src[i]
                    if ch == '\\':
                        i += 2; continue
                    if ch == '[':
                        in_class = True
                    elif ch == ']':
                        in_class = False
                    elif ch == '/' and not in_class:
                        i += 1
                        break
                    elif ch == '\n':
                        break          # 正则不跨行：说明上面的判断错了，别把文件吞掉
                    i += 1
                out.append(' ')
                prev_sig = '/'
                continue
            out.append(c)
            if not c.isspace():
                prev_sig = c
            i += 1
            continue
        if state == 'line':
            if c == '\n':
                state = 'code'; out.append(c)
            i += 1; continue
        if state == 'block':
            if c == '*' and nxt == '/':
                state = 'code'; i += 2; out.append(' '); continue
            i += 1; continue
        # 字符串状态
        if c == '\\':
            i += 2; continue
        if c == state:
            state = 'code'; i += 1; out.append(' '); prev_sig = state; continue
        i += 1
    return ''.join(out)


def _make_decrypted(root):
    d = root / 'dec'
    (d / 'message').mkdir(parents=True)
    _make_shard(str(d / 'message' / 'message_0.db'),
                [('wxid_alpha', [(1, 1, 1600000000, 1)])])
    return str(d)


@pytest.fixture(autouse=True)
def no_wxid_detection(monkeypatch):
    """屏蔽 `_detect_wxid` —— 这不是"以防万一"。

    `create_app` 里是 `wxid or _detect_wxid(decrypted_dir)`；本文件虽然总是显式传
    `wxid=`，但只要有人把 fixture 改成不传，`_detect_wxid(<tmp>)` 就会返回本机
    `D:\\xwechat_files` 里**真实**存在的账号目录名（另一个任务实测过）。那会读用户
    目录、让测试结果随机器变化，违反"绝不使用真实数据"。这里无条件钉住它。
    """
    monkeypatch.setattr(media_mod, '_detect_wxid', lambda decrypted_dir: None)


@pytest.fixture
def app_env(tmp_path):
    return _make_decrypted(tmp_path)


@pytest.fixture
def client(app_env):
    app = create_app(app_env, wxid='wxid_owner')
    app.config['TESTING'] = True
    # 证明上面的 monkeypatch 确实生效、且这里用的是显式 wxid（没有走探测）
    assert app.config['WXID'] == 'wxid_owner'
    return app.test_client()


@pytest.fixture
def html(client):
    r = client.get('/search')
    assert r.status_code == 200
    return r.data.decode('utf-8')


# ==========================================================================
# 1. 静态契约：页面本身
# ==========================================================================

class TestPageStaticContract:
    def test_search_page_returns_200(self, client):
        assert client.get('/search').status_code == 200

    @pytest.mark.parametrize('control_id', REQUIRED_CONTROL_IDS)
    def test_required_control_id_present(self, html, control_id):
        """每个控件都必须存在。

        页面对每个控件都做 `getElementById` 并随即调用方法，缺一个就是
        `TypeError: Cannot read properties of null` → 整页初始化中断
        （本项目的 #15 就是这么死的）。逐个 parametrize，失败时能直接看出缺的是哪个。
        """
        assert 'id="%s"' % control_id in html, control_id

    def test_scripts_are_loaded_in_exactly_the_page_order(self, html):
        """T7-A7/T9：顺序**恰好**是这 5 个，且与 `test_search_js.py` 的加载守卫同源。

        「都包含」对顺序错误无能为力；而顺序错的后果是
        `ReferenceError: SearchSyntax is not defined` —— 页面看着在、功能全死。
        这里刻意复用 `test_search_js.py` 的 `SCRIPT_ORDER`：一份常量，两处断言
        （此处断言它与**真实页面**一致，那边断言按它加载**不抛错**）。
        两份常量各写一遍必然漂移。
        """
        order = tuple(re.findall(r'js/([A-Za-z0-9_\-]+\.js)', html))
        assert order == SCRIPT_ORDER, '页面脚本顺序=%r，期望=%r' % (order, SCRIPT_ORDER)

    def test_the_load_guard_covers_all_five_page_files(self, html):
        """显式钉住"加载守卫覆盖的就是页面加载的那 5 个文件"（含 `search-app.js`）。

        这一条把"守卫的文件清单"与"真实页面的清单"锁在一起：守卫少加载一个文件
        （比如漏掉 `search-app.js`）而页面已经引入它时，这里会红。
        """
        assert len(SCRIPT_ORDER) == 5, SCRIPT_ORDER
        assert len(set(SCRIPT_ORDER)) == 5, SCRIPT_ORDER
        assert 'search-app.js' in SCRIPT_ORDER
        for name in SCRIPT_ORDER:
            assert ('js/%s' % name) in html, name
            assert os.path.isfile(os.path.join(JS_DIR, name)), name

    def test_scripts_come_after_the_content_placeholders(self, html):
        """脚本标签必须在内容占位符**之后**（否则 `getElementById` 拿到 null）。

        经典脚本在解析到 `<script>` 时立即执行；`search-app.js` 在定义期就抓控件，
        所以 `<div id="search-results">` 必须出现在脚本之前。
        """
        assert html.index('id="search-results"') < html.index('js/api.js')
        assert html.index('id="search-index-bar"') < html.index('js/api.js')


# ==========================================================================
# 2. 类型下拉：服务端渲染、恰好 12 个规范名（T9-A6）
# ==========================================================================

class TestServerRenderedTypeOptions:
    def _type_options(self, html):
        select = re.search(r'<select[^>]*id="search-type"[^>]*>(.*?)</select>',
                           html, re.S)
        assert select, '页面里找不到 #search-type 下拉'
        return re.findall(r'<option value="([^"]*)">([^<]*)</option>', select.group(1))

    def test_exactly_the_12_canonical_type_names(self, html):
        """① 字面上的 12 个规范名全在、顺序一致（T7-A7①）。"""
        options = self._type_options(html)
        assert tuple(value for value, _ in options) == CANONICAL_TYPE_NAMES

    def test_matches_type_aliases_derivation(self, html):
        """② 与「`TYPE_ALIASES` 每基类型取首个别名」这条**唯一事实源**推导一致。

        只断言字面元组不够：它可能是前端另抄的一份列表，与解析器接受的
        名字慢慢漂移（那正是"两份列表必然漂移"的老问题）。
        """
        labels = [value for value, _ in self._type_options(html)]
        assert labels == _canonical_type_names()

    def test_option_value_and_text_are_the_same_canonical_name(self, html):
        """语法框里发的是**中文规范名**（`类型:图片,视频`），所以 value 必须等于可见文本。"""
        options = self._type_options(html)
        assert options, '类型下拉是空的'
        assert all(value == text for value, text in options), options

    @pytest.mark.parametrize('synonym', TYPE_SYNONYMS)
    def test_synonyms_are_not_rendered_as_options(self, html, synonym):
        """同义词（`照片`/`音频`…）不得出现在下拉里 —— 下拉只列规范名。"""
        labels = [value for value, _ in self._type_options(html)]
        assert synonym not in labels, synonym

    def test_type_option_list_is_not_built_in_js(self):
        """T9-A6（Critical）：`search-app.js` 里**不得**再有 `TYPES` 这种前端列表。

        计划模板用了一个项目里根本不存在的全局 `TYPES` → 加载后
        `ReferenceError: TYPES is not defined` → 表单⇄语法同步、搜索、结果渲染、
        索引状态条**全部失效**，页面只剩静态 HTML；而"只查静态 HTML"的测试照样通过。

        判据：剥离注释与字符串后，源码里不得出现独立标识符 `TYPES`。
        （注释里提到 `TYPES` 这个缺陷名不算 —— 那正是本条要记录的教训。
        真正的实现若引用它，剥离后**照样在**。）
        """
        hits = re.findall(r'\bTYPES\b', _strip_comments_and_strings(_js('search-app.js')))
        assert not hits, ('search-app.js 里出现了 TYPES（前端第二份类型列表）：%r；'
                          '类型选项必须由服务端渲染' % hits)

    def test_type_select_is_multiple_and_server_filled(self, html):
        m = re.search(r'<select[^>]*id="search-type"[^>]*>', html)
        assert m, '页面里找不到 #search-type 下拉'
        assert 'multiple' in m.group(0), m.group(0)


# ==========================================================================
# 3. 防撞名 / 防未定义全局（Phase 1 §1.1 与 #15 的核心教训）
# ==========================================================================

class TestAntiCollisionGuard:
    def test_search_app_calls_the_namespaced_api(self):
        """`search-app.js` 必须走 `SearchSyntax.*` / `SearchRender.*`。

        Phase 1 把两个纯函数文件包成了 IIFE，只暴露命名空间（原因见
        `search-render.js` 头部注释：顶层 `function escapeHtml` 与 `utils.js` 的
        `const escapeHtml` 撞名 → 实例化阶段 SyntaxError → 整文件不执行）。
        所以页面对它们的**唯一**合法引用形态就是命名空间成员。
        """
        src = _js('search-app.js')
        for token in NAMESPACE_REQUIRED + NAMESPACE_MEMBERS_USED:
            assert token in src, 'search-app.js 缺少命名空间调用: %s' % token

    @pytest.mark.parametrize('name', NAMESPACED_NAMES)
    def test_no_bare_reference_to_namespaced_helpers(self, name):
        """不得**裸引用**这些只存在于命名空间里的名字。

        形如 `formToSyntax(els)` 的裸调用在浏览器里是
        `ReferenceError: formToSyntax is not defined`；而在 Node 里 `require()`
        根本测不出来（CommonJS 的作用域把"撞名"和"缺失"都盖住了）。所以这里在
        **源码层**再钉一道，与 `test_search_js.py` 的经典脚本加载守卫互为双保险。

        例外：对象字面量的**键**（`{ formToSyntax: onFormEdited }`）不是引用，
        它是页面给自己导出的测试用别名（加载守卫就是用它调同步函数的），
        所以用 `(?!\\s*:)` 排除掉。注释与字符串已被剥离，不会误报。
        """
        src = _strip_comments_and_strings(_js('search-app.js'))
        bare = re.findall(r'(?<![.\w])%s\b(?!\s*:)' % name, src)
        assert not bare, ('search-app.js 裸引用了 %s（%d 处）：必须写成命名空间成员，'
                          '否则浏览器里是 ReferenceError' % (name, len(bare)))

    def test_top_level_is_a_single_iife(self):
        """整个文件必须包在一个 IIFE 里：顶层不得出现标识符绑定。

        经典脚本共用一个全局名字空间，顶层 `const escapeHtml` 这类声明与 utils.js
        撞名就是"整个文件不执行"。源码级断言 + `test_search_js.py` 的加载守卫
        （在同一 realm 里加载全部 5 个文件）互为双保险。
        """
        stripped = _strip_comments_and_strings(_js('search-app.js'))
        body = stripped.strip()
        assert re.match(r'^\(function\s*\(', body), \
            'search-app.js 的第一条顶层语句不是 IIFE：%r' % body[:80]
        assert body.endswith('})();'), \
            'search-app.js 没有以 IIFE 调用收尾：%r' % body[-40:]
        # 顶层声明必须在第 0 列；缩进的行属于 IIFE 内部，不在禁止之列
        top_decls = re.findall(r'(?m)^(?:var|let|const|function|class|async\s+function)\s+\w+',
                               stripped)
        assert not top_decls, '顶层声明会造成全局撞名风险：%r' % top_decls
        # 非空守卫：证明扫描真的读到了内容（不是"空文件也通过"）
        assert len(body) > 500, '剥离后的源码只剩 %d 字符，扫描逻辑失效' % len(body)

    def test_api_js_exposes_the_three_search_methods(self):
        src = _js('api.js')
        for name in ('search(', 'searchStatus(', 'searchBuild('):
            assert name in src, 'api.js 缺少 %s' % name

    def test_page_script_does_not_shadow_api_js_globals(self):
        """`search-app.js` 不得在 IIFE 内声明与 `api.js` 顶层同名的局部变量并**误用**。

        `api.js` 的 `api` 是 `const api = {...}`（全局词法绑定，**不是** `window.api`
        属性），所以页面只能用裸标识符 `api` 引用它。若在 IIFE 内写
        `var api = ...`，就会静默遮蔽掉真正的客户端 → 所有请求变成 `undefined`。
        这里钉住这个阴影（其它局部名如 `$` / `els` 不受影响）。
        """
        stripped = _strip_comments_and_strings(_js('search-app.js'))
        shadow = re.findall(r'(?m)^\s*(?:var|let|const)\s+api\b', stripped)
        assert not shadow, 'search-app.js 用局部变量遮蔽了 api.js 的 api：%r' % shadow

    def test_page_script_guards_against_a_missing_api_client(self):
        """`api` 用 `typeof` 检查后再用：api.js 未加载时要**大声失败**而不是抛 TypeError。"""
        src = _js('search-app.js')
        assert re.search(r"typeof\s+api\s*[!=]=", src), \
            'search-app.js 没有对 api.js 的 api 做存在性检查'


# ==========================================================================
# 4. 样式（页面可用性）
# ==========================================================================

class TestSearchPageStyles:
    @pytest.mark.parametrize('selector', [
        '.search-result-list', '.search-result', '.sr-head', '.sr-snippet',
        '.sr-snippet mark', '.sr-actions', '#search-type', '#search-help',
        '#search-index-bar',
    ])
    def test_search_page_selectors_exist(self, selector):
        assert selector in _css(), 'app.css 缺少 %s' % selector

    def test_snippet_line_clamping_is_declared(self):
        block = re.search(r'\.sr-snippet\s*\{[^}]*\}', _css(), re.S)
        assert block, 'app.css 里找不到 .sr-snippet 规则'
        assert 'line-clamp' in block.group(0), \
            '摘要行没有行数上限，长文本会把结果列表撑爆：%r' % block.group(0)

    def test_mark_highlight_is_styled(self):
        block = re.search(r'\.sr-snippet\s+mark\s*\{[^}]*\}', _css(), re.S)
        assert block, 'app.css 里找不到 .sr-snippet mark 规则'
        body = block.group(0)
        assert 'background' in body and 'color' in body, body

    def test_index_bar_and_progress_are_styled(self):
        css = _css()
        for selector in ('#search-index-bar', '#search-build-progress',
                         '.progress-bar-outer', '.progress-bar-inner'):
            assert re.search(re.escape(selector) + r'\s*[,{]', css), \
                'app.css 缺少 %s 的样式' % selector


# ==========================================================================
# 5. Phase 1 的纯函数层仍是本页的前置条件
# ==========================================================================

class TestPhaseOnePureFunctionsAreStillWired:
    def test_namespaced_files_export_the_namespace_for_this_page(self):
        """页面调用的命名空间，正是 phase-1 文件在浏览器侧暴露的那两个。"""
        assert 'window.SearchSyntax = API' in _js('search-syntax.js')
        assert 'window.SearchRender = API' in _js('search-render.js')

    def test_namespaces_expose_every_name_the_page_uses(self):
        """页面用到的每个命名空间成员，都必须在对应的导出对象里**存在**。

        少接一个就是加载期/调用时的 `TypeError: SR.xxx is not a function`
        —— 而"文件存在、命名空间存在"的断言照样通过。
        """
        app_src = _strip_comments_and_strings(_js('search-app.js'))
        used = set(re.findall(r'\bS[SR]\.(\w+)', app_src))
        assert used, 'search-app.js 一处命名空间成员都没用到，正则或实现有问题'

        exports = {}
        for fname, ns in (('search-syntax.js', 'SS'), ('search-render.js', 'SR')):
            body = _strip_comments_and_strings(_js(fname))
            api_block = re.search(r'var\s+API\s*=\s*\{(.*?)\};', body, re.S)
            assert api_block, '%s 里找不到 API 导出对象' % fname
            exports[ns] = set(re.findall(r'(\w+)\s*:', api_block.group(1)))

        for member in sorted(used):
            ns = 'SS' if member in exports['SS'] else (
                'SR' if member in exports['SR'] else None)
            assert ns, ('search-app.js 用到了未导出的成员 %r（可用：SS=%r, SR=%r）'
                        % (member, sorted(exports['SS']), sorted(exports['SR'])))

    def test_search_app_exposes_an_initialized_marker(self):
        """依赖缺失时必须**大声失败**，不能安静地只剩静态 HTML。

        `window.SearchApp.initialized` 是 `test_search_js.py` 加载守卫读的标记：
        只有初始化真正跑完才为 `true`。若写成"依赖缺失就 `return`"而不置标记，
        守卫里的 `initialized === true` 立刻红 —— 这正是 #15 那类"页面看着在、
        功能全死"缺陷的可观测点。
        """
        src = _js('search-app.js')
        assert 'window.SearchApp' in src, '页面脚本没有暴露 SearchApp 标记'
        assert re.search(r'initialized\s*:\s*true', src), \
            '页面脚本没有在初始化完成后置 initialized: true'


# ==========================================================================
# 6.5 T16：构建按钮文案（实测 51.5 s 热 / 91.1 s 冷 / 并发 163–246 s）
# ==========================================================================

BUILD_BUTTON_WORDING = '（约 1 分钟，视机器与负载）'


class TestBuildButtonCopy:
    """文案是本轮契约的一部分（控制方逐字给定），而它只存在于 `search-app.js`。

    ⚠️ 这四处**不是**同一个按钮：`重试构建` / `构建索引` / `一键构建`（两个入口），
    所以这里断言的是"时长口径"这一句，而不是把四个标签压成一个。
    """

    def test_no_stale_fifty_second_estimate_anywhere(self, html):
        """旧的"约 50 秒"必须被彻底换掉（实测 51.5s 热 / 91.1s 冷，偏乐观）。"""
        assert '约 50 秒' not in _js('search-app.js')
        assert '约 50 秒' not in html        # 服务端渲染的页面里也不许残留

    def test_every_build_button_uses_the_measured_wording(self):
        src = _js('search-app.js')
        labels = re.findall(r"buildButton\('([^']*)'\)", src)
        assert len(labels) == 4, '构建按钮的处数变了（现在 %d 处）：%r' % (len(labels), labels)
        for label in labels:
            assert label.endswith(BUILD_BUTTON_WORDING), label
        assert labels.count('一键构建' + BUILD_BUTTON_WORDING) == 2
        assert '重试构建' + BUILD_BUTTON_WORDING in labels
        assert '构建索引' + BUILD_BUTTON_WORDING in labels

    def test_the_label_is_actually_rendered_by_the_index_bar(self):
        """渲染路径也要有：文案写在函数里、却没被任何分支调用，是这类改动的经典假绿。"""
        src = _js('search-app.js')
        assert 'buildButton(' in src.split('function barHtml()')[1], \
            'barHtml 一个构建按钮都没渲染，上面两条断言就只是"字符串在某些函数里"'


# ==========================================================================
# 6. 真实 API 响应 → phase-1 纯函数
#
# 补的是 Phase 1 留下的一段真空：那边的 Node 用例用手写 status / 手写结果项验证了
# `statusNotice` / `formatResult`，但**没有任何东西**保证真实
# `/api/search/status` 与 `/api/search` 的响应还喂得进这两个函数。字段一改名
# （比如 `text_ready` → `textReady`），页面就会把"索引没建"显示成"索引就绪"、
# 把 0 条显示成"没有这条消息"，而两侧的单测各自全绿。
#
# 这里把**真实**响应（合成夹具产生）交给页面实际调用的那份纯函数，把缺口补上。
# 需要 node；没有 node 时 skip（与 `test_search_js.py` 同一口径，缺 Node 不算缺陷）。
# ==========================================================================

_JS_DIR_ABS = os.path.join(ROOT, 'src', 'web', 'static', 'js')
_RENDER_PATH_JS = os.path.join(_JS_DIR_ABS, 'search-render.js').replace('\\', '/')

# Node 程序约定：argv[2] = 输入 JSON 路径，argv[3] = 输出文本路径
_STATUS_NOTICE_PROGRAM = (
    "const fs = require('fs');\n"
    "const m = require(%r);\n"
    "const st = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));\n"
    "fs.writeFileSync(process.argv[3], m.statusNotice(st), 'utf8');\n"
) % _RENDER_PATH_JS

_FORMAT_RESULT_PROGRAM = (
    "const fs = require('fs');\n"
    "const m = require(%r);\n"
    "const body = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));\n"
    "const out = (body.results || []).map(function (r) { return m.formatResult(r); });\n"
    "fs.writeFileSync(process.argv[3], JSON.stringify(out), 'utf8');\n"
) % _RENDER_PATH_JS


def _run_node_program(tmp_path, program, payload, name):
    """把 payload 写成 JSON → 跑 Node 程序 → 把它写出的文本读回来。

    结果走**文件**而不是 stdout：Windows 上子进程管道的编码不受控，中文诊断容易
    变乱码（`_run_node` 已显式按 UTF-8 解码，但这里连换行与 BOM 都不想要）。
    """
    exe = _node_or_skip()
    in_path = tmp_path / ('%s.json' % name)
    out_path = tmp_path / ('%s.out' % name)
    script = tmp_path / ('%s.js' % name)
    in_path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    script.write_text(program, encoding='utf-8')

    code, out, err = _run_node([exe, str(script), str(in_path), str(out_path)], cwd=ROOT)
    assert code == 0, 'node 程序失败:\n--- stdout ---\n%s\n--- stderr ---\n%s' % (out, err)
    assert out_path.is_file(), 'node 程序没有写出结果文件:\n%s' % out
    return out_path.read_text(encoding='utf-8')


class TestRealApiResponseDrivesThePagePureFunctions:
    """真实响应 → 页面纯函数。合成夹具，绝不读真实数据。"""

    # 第二条正文刻意含 HTML：验证**真实**结果项的转义路径（页面把它塞 innerHTML）
    _ROWS = [
        ('维修服务器', 1, 1600000000000, 1, 1, 1, 1600000000),
        ('维修<b>警报</b>', 2, 1600000100000, 1, 1, 1, 1600000100),
    ]

    @pytest.fixture
    def built_env(self, tmp_path):
        d = tmp_path / 'dec'
        (d / 'message').mkdir(parents=True)
        _make_shard(str(d / 'message' / 'message_0.db'),
                    [('wxid_alpha', [(1, 1, 1600000000, 1),
                                     (2, 1, 1600000100, 1)])])
        _make_fts_db(str(d / 'message' / 'message_fts.db'), self._ROWS)
        return str(d)

    def _client(self, env):
        app = create_app(env, wxid='wxid_owner')
        app.config['TESTING'] = True
        return app.test_client()

    def test_real_status_of_a_fresh_install_produces_no_alarm(self, built_env, tmp_path):
        """装好就能用的索引：真实 status 喂进 `statusNotice` 必须是**空串**。

        非空 = 页面对每次正常安装永久显示"索引不完整"（T9-A1 明确要防的形态；
        真机 `fts_coverage = 0.8615` 的差额是合法的）。这是"不得误报"这一侧。
        """
        si.build_index(built_env)
        st = self._client(built_env).get('/api/search/status').get_json()
        # 非空守卫：夹具真的建出了可用索引（否则空串毫无意义）
        assert st['ready'] is True and st['text_ready'] is True and st['fts_rows'] == 2
        notice = _run_node_program(tmp_path, _STATUS_NOTICE_PROGRAM, st, 'status_fresh')
        assert notice == '', '正常安装被报成索引不完整：%r' % notice

    def test_real_status_before_build_blocks_the_no_such_message_claim(self, built_env, tmp_path):
        """索引没建：真实 status 必须让页面能识别"关键词搜索不可信"。"""
        st = self._client(built_env).get('/api/search/status').get_json()
        assert st['ready'] is False and st['text_ready'] is False   # 夹具非空守卫
        notice = _run_node_program(tmp_path, _STATUS_NOTICE_PROGRAM, st, 'status_none')
        assert notice, '未构建时 statusNotice 返回空串 → 页面会把 0 条当成"没有这条消息"'
        assert '构建' in notice

    def test_real_status_of_a_meta_only_build_is_not_mistaken_for_ready(self, built_env, tmp_path):
        """`build_index(text=False, meta=True)`：`ready` 仍为 True，但文本索引是空的。

        这是 T9-A1 里最危险的"看起来正常"现场：真实响应必须能被区分出来，
        否则关键词查询静默 0 条，而页面显示"索引就绪 / 没有这条消息"。
        """
        si.build_index(built_env, text=False, meta=True, force=True)
        st = self._client(built_env).get('/api/search/status').get_json()
        # 非空守卫：确实复现了"ready=True 但文本索引为空"
        assert st['ready'] is True and st['fts_rows'] == 0
        assert st['text_ready'] is False
        notice = _run_node_program(tmp_path, _STATUS_NOTICE_PROGRAM, st, 'status_meta')
        assert notice, 'meta-only 构建后 statusNotice 返回空串 → 页面会说"索引就绪"'
        assert '关键词' in notice

    def test_real_search_hits_render_escaped_and_highlighted(self, built_env, tmp_path):
        """真实 `/api/search` 的结果项喂进页面实际调用的 `formatResult`。

        断言的是**真实**链路上的两件事：
          * `match_spans` 是相对 snippet 的偏移 → 必须真的产生 `<mark>`（偏移对不上
            就一处都标不出来；phase-1 的手写夹具钉不住这一点）；
          * 正文里的 HTML（夹具第二条含 `<b>`）必须被转义 —— 页面把 `html` 塞
            `innerHTML`，原样插入就是消息正文破坏页面结构 / XSS。
        """
        si.build_index(built_env)
        body = self._client(built_env).get(
            '/api/search', query_string={'q': '维修'}).get_json()
        assert body['total'] == 2, body.get('total')          # 夹具非空守卫
        assert all(r.get('snippet') for r in body['results'])

        rendered = json.loads(_run_node_program(
            tmp_path, _FORMAT_RESULT_PROGRAM, body, 'search_body'))
        assert len(rendered) == len(body['results'])

        assert any('<mark>' in r['html'] for r in rendered), (
            '没有任何结果产生 <mark>：match_spans 与 snippet 的偏移对不上\n%s'
            % json.dumps(rendered, ensure_ascii=False))
        # 反空跑：夹具里那条含 HTML 的正文必须真的回了、且真的被转义（不是"没走到那个分支"）
        assert any('&lt;b&gt;' in r['html'] for r in rendered), (
            '含 HTML 的正文没有被转义为实体（或压根没回结果，本条断言就成了空转）\n%s'
            % json.dumps(rendered, ensure_ascii=False))

        for r in rendered:
            assert '<b>' not in r['html'], (
                '消息正文里的 HTML 被原样插入 innerHTML：%r' % r['html'])
            assert r['html'].count('<mark>') == r['html'].count('</mark>'), r['html']
            assert r['spans'], '有命中却一个高亮区间都没有：%r' % r['snippet']
            for a, b in r['spans']:
                assert 0 <= a < b <= len(r['snippet']), (a, b, r['snippet'])
            assert r['href'].startswith('/chat?open=')
            assert r['typeLabel'] and r['chatName']


# ==========================================================================
# 7. 跨语言**同源夹具**校验：`statusNotice` ⇄ 服务端 `_index_incompleteness`
#
# 判据有两份实现（JS 的 `statusNotice` 与服务端的 `_index_incompleteness`）——
# 这是"判定必须能在 Node 里被测住"的必要代价，代价就是**漂移风险**：给服务端加一个
# 新的真信号，JS 那份不会跟着变，UI 又开始漏报或误报，而两边测试**各自都通过**。
#
# 落地方式（控制方追加要求）：
#   * 夹具是一个 **JSON** 文件：`tests/fixtures/search_status_cases.json`；
#   * **Node 侧**在 `tests/js/search_render_notice.js` 末尾读它断言 `statusNotice`；
#   * **本文件**读**同一个**文件断言服务端的 `_index_incompleteness`；
#   * 任一侧改口径而没跟另一边 → 必有一侧红。
#
# ⚠️ 实测得到的口径关系（**不是**同一个谓词，必须写清楚，否则这套校验会自欺）：
#       expect_warn === (expect_incomplete or not status.text_ready)
#   服务端 `_index_incompleteness` **有意不看** `text_ready` —— 它是 HTTP 层单独暴露的
#   字段（`search_api._text_ready`）。所以"meta-only 构建"那一格是
#   incomplete=False（服务端那 6 条判据都不成立）但**页面必须告警**。
#   控制方原话是"对每条调用 `_index_incompleteness`，断言同样的期望"；照字面做在
#   `text_ready=false` 这条上必然红，因为服务端那份根本不看这个字段。所以这里：
#     ① 每条都断言 `_index_incompleteness` == `expect_incomplete`（服务端的精确谓词）；
#     ② 每条都断言 `_text_ready(st) == st['text_ready']`（夹具与 HTTP 层推导一致）；
#     ③ 非差异条目断言上面那个**并集关系**成立 —— 这条就是把两份判据锁在一起的契约。
#   另外发现一处**已知口径差异**（缺 `msg_text_rows` 时服务端会报"行数不一致"、JS 因
#   有 `_has` 守卫而不报），夹具里显式标注为 `known_divergence` 并各自钉住，见 §7.3。
# ==========================================================================

FIXTURE_PATH = os.path.join(ROOT, 'tests', 'fixtures', 'search_status_cases.json')

# status 对象的键集合：每个 case 必须**同一套**（两端各自假设哪些字段在场，
# 键一漂移判定就会分叉）
_STATUS_KEYS = frozenset((
    'exists', 'ready', 'stale', 'schema_ok', 'source_shards', 'source_rows',
    'meta_rows', 'meta_coverage', 'fts_rows', 'msg_text_rows', 'fts_coverage',
    'fts_skipped_no_chat', 'fts_skipped_empty_text', 'duplicates_dropped',
    'fts_rows_without_meta', 'built_at', 'text_ready',
))


def _load_status_cases():
    """读同源夹具；文件缺失/坏掉时返回 `[]`（由 §7.1 的那条断言负责大声报错）。"""
    try:
        with open(FIXTURE_PATH, encoding='utf-8') as fh:
            return json.load(fh).get('cases') or []
    except (OSError, ValueError):
        return []


_CASES = _load_status_cases()


class TestStatusNoticeCrossLanguageParity:
    def test_shared_fixture_is_readable_and_discriminating(self):
        """夹具本身：可读、条目够多、且**两个方向都有**（不能全告警或全不告警）。"""
        assert _CASES, ('同源夹具读不出来或没有条目：%s；Node 侧读的是同一个文件，'
                        '缺了它两边的交叉校验就都成了空转' % FIXTURE_PATH)
        assert len(_CASES) >= 6, len(_CASES)
        warn = [c for c in _CASES if c['expect_warn']]
        silent = [c for c in _CASES if not c['expect_warn']]
        assert warn and silent, '夹具必须两个方向都有（warn=%d silent=%d)' % (len(warn), len(silent))

    def test_fixture_covers_every_signal_each_side_looks_at(self):
        """夹具必须**逐条**覆盖两侧看的所有字段 —— 这是"能发现漂移"的前提。

        少覆盖一个字段，将来只改那一个字段的口径就不会被抓住。所以对每条真信号，
        都要求存在一个"只有该字段异常"的 case，且期望为告警。
        """
        signals = {
            'schema_ok': lambda s: s['schema_ok'] is False,
            'source_shards': lambda s: s['source_shards'] == 0,
            'meta_coverage': lambda s: s['meta_coverage'] < 1,
            'fts_skipped_no_chat': lambda s: s['fts_skipped_no_chat'] > 0,
            'msg_text_rows': lambda s: s['msg_text_rows'] != s['fts_rows'],
            'duplicates_dropped': lambda s: s['duplicates_dropped'] > 0,
            'text_ready': lambda s: s['text_ready'] is False,
        }
        covered = []
        for field, pred in signals.items():
            hit = [c for c in _CASES if c['expect_warn'] and pred(c['status'])]
            # "其余字段都正常"的那一跳：source_shards=0 会连带把 fts_rows 等置 0，
            # 所以只要求"存在一个告警 case 触发该字段"，不要求它是唯一异常项。
            if hit:
                covered.append(field)
        missing = sorted(set(signals) - set(covered))
        assert not missing, '这些真信号在夹具里没有任何告警样本，漂移就抓不住：%r' % missing

        # "不得误报"这一侧也必须被覆盖：至少一条"只有合法差额"的静默样本
        legit = [c for c in _CASES
                 if not c['expect_warn'] and c['status']['fts_coverage'] < 1
                 and c['status']['meta_coverage'] == 1]
        assert legit, '缺少"fts_coverage<1 但合法"的静默样本（真机 0.8615 那一格）'

    @pytest.mark.parametrize('case', _CASES or [None],
                             ids=[c['name'] for c in _CASES] or ['<empty fixture>'])
    def test_server_incompleteness_matches_the_shared_expectation(self, case):
        """服务端那份判据必须与**同一份夹具**的期望一致。

        参数化到每一条（失败时能直接看出是哪个 case），而不是"整体跑一遍"。
        """
        assert case is not None, '同源夹具为空：%s' % FIXTURE_PATH
        from web.routes import search_api

        st = case['status']
        assert set(st) == set(_STATUS_KEYS), (
            'case %r 的 status 键集合与约定不一致：多=%r 少=%r'
            % (case['name'], sorted(set(st) - _STATUS_KEYS),
               sorted(_STATUS_KEYS - set(st))))

        is_incomplete, reasons = search_api._index_incompleteness(st)
        assert bool(is_incomplete) is bool(case['expect_incomplete']), (
            'case %r: _index_incompleteness=%r，夹具期望 %r（reason=[] 说明漏报，'
            'reason 非空说明误报）' % (case['name'], is_incomplete, case['expect_incomplete']))

        joined = ' | '.join(reasons)
        for needle in case['server_reasons_contains']:
            assert needle in joined, (
                'case %r: 服务端理由里找不到 %r，实际=%r'
                % (case['name'], needle, joined))
        if not case['expect_incomplete']:
            # 不告警时不得给出任何理由（半开的告警同样会误导）
            assert reasons == [], 'case %r: 不告警却给了理由 %r' % (case['name'], reasons)

    @pytest.mark.parametrize('case', _CASES or [None],
                             ids=[c['name'] for c in _CASES] or ['<empty fixture>'])
    def test_fixture_text_ready_matches_the_http_layer_derivation(self, case):
        """夹具里的 `text_ready` 必须等于 HTTP 层自己的推导（`schema_ok and fts_rows`）。

        这条看似多余，其实是**前提**：整套交叉校验都建立在"夹具的 status 是自洽的"上。
        夹具把 `text_ready` 写成了别的值，两个方向就都会以错误的前提通过。
        """
        assert case is not None, '同源夹具为空：%s' % FIXTURE_PATH
        from web.routes import search_api

        st = case['status']
        assert search_api._text_ready(st) is st['text_ready'], (
            'case %r: 夹具写 text_ready=%r，但 `schema_ok and fts_rows` 推出 %r'
            % (case['name'], st['text_ready'], search_api._text_ready(st)))

    @pytest.mark.parametrize('case', _CASES or [None],
                             ids=[c['name'] for c in _CASES] or ['<empty fixture>'])
    def test_page_contract_relation_locks_the_two_predicates_together(self, case):
        """把两份判据锁在一起的**唯一**那条契约：

            expect_warn == (expect_incomplete or not status.text_ready)

        左侧是"页面会告警"（Node 侧断言 `statusNotice != ''`），右侧完全由服务端
        两个字段算出。**任一侧新增/删除真信号而另一边没跟，这条或上一条就会红。**

        已知口径差异（缺 `msg_text_rows`）在夹具里显式标注，不参与这条关系 ——
        见 §7.3 的说明。
        """
        assert case is not None, '同源夹具为空：%s' % FIXTURE_PATH
        if case.get('known_divergence'):
            assert isinstance(case['known_divergence'], str) \
                and len(case['known_divergence']) > 20, case['name']
            return
        expect = bool(case['expect_incomplete']) or case['status']['text_ready'] is False
        assert bool(case['expect_warn']) is expect, (
            'case %r: expect_warn=%r，但（incomplete or not text_ready）=%r'
            % (case['name'], case['expect_warn'], expect))

    def test_the_node_side_reads_the_same_fixture(self):
        """确认**另一侧真的读了同一个文件**。

        否则"交叉校验"会退化成"只有 pytest 在读夹具"：JS 那份判据没人用夹具钉住，
        服务端改了口径、JS 没跟，而 pytest 侧全绿。
        """
        node_src = os.path.join(ROOT, 'tests', 'js', 'search_render_notice.js')
        with open(node_src, encoding='utf-8') as fh:
            src = fh.read()
        assert 'search_status_cases.json' in src, \
            'Node 侧没有读同源夹具（%s）' % node_src
        assert 'fixtures' in src, 'Node 侧没有从 tests/fixtures 读夹具'

    def test_known_divergences_are_explicit_and_bounded(self, capsys):
        """口径差异的**记账簿**：允许 0 条，但存在时必须真的是一处差异、带理由、且不超过上限。

        历史：`partial response missing msg_text_rows` 曾经是一处真实差异 —— 服务端第 5 条
        判据没有"字段是否存在"的守卫（缺 `msg_text_rows` 时按 `None != N` 报"行数不一致"），
        而 JS 侧有 `_has` 守卫故不告警。当时**没有**去改别人的文件，而是把它显式记成
        `known_divergence` 并上报。Task 7 fix round 2 判定"应当修服务端"并加了
        `if msg_text_rows is not None and ...`，于是这处差异消失、**账销掉**（见报告 §8）。

        这条断言的形状也随之改了：以前要求"至少有一条差异"（那会在销账后**新红**，
        逼人为了绿灯保留一笔假账），现在改成**自清理**的口径 ——
        每条 `known_divergence` 必须**真的**破坏并集关系
        `expect_warn == (incomplete or not text_ready)`；两侧其实一致却还挂着这个键，
        就会红并直接告诉你"账该销了"。这比"删了它就红"更不容易骗人。
        """
        diverging = [c for c in _CASES if c.get('known_divergence')]
        if not diverging:
            # 允许 0 条：销账之后本来就该是 0，不该为了保住一条断言去留一笔假差异
            print('同源夹具当前**无**已知差异：两侧判据在全部 %d 条 case 上一致'
                  % len(_CASES))
            return

        for name in diverging:
            case = next(c for c in _CASES if c['name'] == name)
            assert isinstance(case['known_divergence'], str) \
                and len(case['known_divergence']) > 20, (
                    'case %r: known_divergence 必须带一句能看懂的理由' % name)
            # 它必须**真的**是一处差异：否则这笔记账已经到期，删掉它
            union = bool(case['expect_incomplete']) or case['status']['text_ready'] is False
            assert bool(case['expect_warn']) is not union, (
                'case %r 标了 known_divergence，但两侧判据其实**一致**'
                '（expect_warn=%r，(incomplete or not text_ready)=%r）'
                '—— 这笔记账到期了，请改夹具并删掉 known_divergence，不要留着假账'
                % (name, case['expect_warn'], union))

        assert len(diverging) <= 2, ('已知差异被扩大到 %d 条：%r —— 差异是债，'
                                     '要么修掉要么明确记录理由' % (len(diverging), diverging))
