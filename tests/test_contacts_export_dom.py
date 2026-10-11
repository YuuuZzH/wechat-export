# -*- coding: utf-8 -*-
"""Task 12：`/contacts` 页「页面点导出」的**交互**验证（vm 单 realm 守卫 + 变异测试）。

为什么需要它（本项目已经踩过两次同类坑）：

  ① **加载期 `ReferenceError` 让整页初始化崩溃，而"只查静态 HTML"的断言照样通过**
     （计划缺陷 #15：`search-app.js` 引用了不存在的全局 `TYPES`）；
  ② **Node 的 `require()` 通过不能证明浏览器能加载**（CommonJS 把每个模块包进函数
     作用域，把**顶层撞名**藏住了）：`utils.js` 的 `const escapeHtml` 与另一个文件顶层的
     同名声明相撞时，浏览器在**实例化阶段**抛 `SyntaxError`，那个文件**一行都不执行**，
     而所有 `require` 断言全绿。
     （见 `.docs/agent-context/08-risks/known-issues.md` #23 与
      `02-decisions/ADR-0012-global-search-index.md` 关于"命名空间 vs 裸全局"的结论。）

本页的导出接线（**选格式 → 点导出 → 发出带正确参数的请求**）此前只有 **HTTP 层**覆盖
（`tests/test_contacts_export_route.py` 直接请求 `/api/address-book/export`）：
"页面到底有没有把下拉框选的值读进去"这件事**没有任何自动化验证** —— 一个写死的
`format: 'csv'` 或一个没挂上的 `click` handler 会让导出要么永远导出 CSV、要么点了没反应，
而 HTTP 层测试与静态 HTML 断言**全都是绿的**。

落地方式与 `tests/test_search_js.py` 的**经典脚本加载守卫**同源：
在一个 `vm` realm 里按 `contacts.html` 的**真实顺序**加载本页引入的全部 7 个脚本，
配一个**按真实 id 生成**的假 DOM（未知 id 返回 `null` ⇒ 写错控件 id 会立刻暴露），
再用假 `fetch` 记录每一次请求，模拟"选格式 → 点导出"，并**逐项**断言 URL 与参数。

⚠️ **边界（必须说清楚，否则就是自欺）**：`vm` **不是真浏览器**。
  * **覆盖**：加载期缺陷（顶层撞名 / 未定义全局 / 顺序错）+ 请求构造
    （点导出到底发了什么 URL、按钮的前后状态、下载分支的调用）。
  * **不覆盖**：视觉渲染（CSS 布局、控件在真浏览器里的外观/可点性）、
    原生文件下载对话框（`a.click()` 之后浏览器把文件真正落到"下载"目录的整条链路）、
    以及真浏览器的事件模型细节（本守卫用假 DOM 派发监听器，`this` 绑定是我们模拟的）。
  所以本文件**不**声称"真实浏览器里的下载验证完成"，只声称"页面接线与请求构造验证完成"。

`node` 不存在时一律 `skip`（与 `test_search_js.py` 同一口径：缺 Node 不算本仓库的缺陷）。
"""
import hashlib
import json
import os
import re
import shutil
import subprocess

import pytest

# 仓库根目录从 __file__ 推导，绝不依赖 cwd
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, 'src', 'web', 'templates', 'contacts.html')
JS_DIR = os.path.join(ROOT, 'src', 'web', 'static', 'js')
API_ROUTE_PATH = os.path.join(ROOT, 'src', 'web', 'routes', 'api.py')

# `contacts.html` 的脚本顺序（**唯一**一份顺序常量）：
# 一份常量、两处断言 —— 下面 `test_template_script_order_is_exactly_this_constant`
# 断言它与**真实模板**逐项相等，加载守卫则按它把 7 个文件当经典脚本加载进同一个 realm。
# 顺序本身是**行为契约**：`components/component.js` 提供 `class Component`，
# `components/address-book-list.js` 在类定义期就 `extends Component`（TDZ），
# 放到后面就是 `ReferenceError: Cannot access 'Component' before initialization`。
CONTACTS_SCRIPT_ORDER = (
    'api.js',
    'utils.js',
    'components/component.js',
    'address-book-store.js',
    'components/address-book-list.js',
    'components/contact-detail.js',
    'contacts-app.js',
)

# 只扫**本页自有**的三个文件里的 `getElementById(...)`：它们的 id 必须存在于
# `contacts.html`。刻意**不**扫 `utils.js` —— 那里的 `getElementById('message-list')` /
# `'lightbox'` 属于聊天页（`chat.html`），在通讯录页上不该存在。
PAGE_OWN_JS = (
    'contacts-app.js',
    'components/address-book-list.js',
    'components/contact-detail.js',
)

# `/api/address-book/export` 接受的查询参数全集（事实源是路由源码，见
# `test_export_route_contract_matches_the_page_contract`：它直接从 api.py 里把
# 这 7 个名字抠出来比对，所以路由新增一个参数、这里没跟，就会红）。
ROUTE_EXPORT_QUERY_KEYS = frozenset(
    ('format', 'q', 'sort', 'has_chat', 'letter', 'kind', 'label'))

_RESULT_RE = re.compile(r'RESULT:\s*(\d+)\s+passed,\s*(\d+)\s+failed')
_FAILED_LINE_RE = re.compile(r'^FAILED:\s*(.*)$', re.M)
_CHECK_FAIL_RE = re.compile(r'^\s*FAIL\s\s(\S+)', re.M)

_ID_RE = re.compile(r'\bid="([A-Za-z0-9_\-]+)"')
_SCRIPT_RE = re.compile(r"""filename='js/([A-Za-z0-9_\-/.]+\.js)'""")
_GET_BY_ID_RE = re.compile(r"""getElementById\(\s*['"]([^'"]+)['"]\s*\)""")
_ROUTE_ARG_RE = re.compile(r"""request\.args\.get\(\s*['"]([^'"]+)['"]""")


# ==========================================================================
# 工具
# ==========================================================================

def _node_or_skip():
    """→ node 可执行文件路径；没有就 skip（缺 Node 不是本仓库的缺陷）。"""
    exe = shutil.which('node')
    if not exe:
        pytest.skip('未找到 node：跳过 /contacts 导出交互守卫（Python 测试不受影响）')
    return exe


def _run_node(args, cwd):
    """跑 node 并**显式按 UTF-8 解码**（Windows 上 `text=True` 会按 GBK 解码 Node 的 UTF-8 输出）。"""
    proc = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
    return (proc.returncode,
            proc.stdout.decode('utf-8', 'replace'),
            proc.stderr.decode('utf-8', 'replace'))


def _read_text(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


def _sha256(path):
    with open(path, 'rb') as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _template_script_order(template=TEMPLATE):
    hits = _SCRIPT_RE.findall(_read_text(template))
    assert hits, '从 %s 里一个脚本都没抠出来，抽取逻辑失效' % template
    return tuple(hits)


def _template_ids(template=TEMPLATE):
    return set(_ID_RE.findall(_read_text(template)))


def _page_queried_ids(js_dir=JS_DIR):
    """本页自有脚本里 `getElementById(...)` 用到的**全部**控件 id。"""
    ids = set()
    for rel in PAGE_OWN_JS:
        path = os.path.join(js_dir, rel)
        assert os.path.isfile(path), '页面脚本不存在: %s' % path
        ids.update(_GET_BY_ID_RE.findall(_read_text(path)))
    assert ids, '一个控件 id 都没扫到，扫描逻辑失效'
    return ids


def _spec_for_root(root):
    """守卫的输入：脚本顺序 + 页面查询的控件 id，**都从被测的那棵树里现读**。

    变异测试里传的是变异树的根：这样"页面把控件 id 写错了"才能被
    `load/every-queried-control-id-exists` 这条断言抓到（否则 id 清单会来自未变异的源码，
    守卫就在测一个陈旧的前提 —— 这正是"变异测试自己空转"的经典形态）。
    """
    tpl = os.path.join(root, 'src', 'web', 'templates', 'contacts.html')
    js = os.path.join(root, 'src', 'web', 'static', 'js')
    return {'order': list(_template_script_order(tpl)),
            'queried_ids': sorted(_page_queried_ids(js))}


def _write_guard(tmp_path, name='contacts_page_guard.js'):
    path = os.path.join(str(tmp_path), name)
    with open(path, 'w', encoding='utf-8', newline='') as fh:
        fh.write(CONTACTS_PAGE_GUARD_JS)
    return path


def _run_guard(script, root, spec):
    """跑一次守卫；返回 (退出码, stdout, stderr, 期望的 spec 与实测一致的 json 路径)。"""
    exe = _node_or_skip()
    spec_path = os.path.join(os.path.dirname(script), 'spec.json')
    with open(spec_path, 'w', encoding='utf-8') as fh:
        json.dump(spec, fh, ensure_ascii=False)
    code, out, err = _run_node([exe, script, root, spec_path], cwd=ROOT)
    return code, out, err


def _parsed_result(out):
    m = _RESULT_RE.search(out)
    assert m, '守卫没有输出 RESULT 行，可能根本没跑用例:\n%s' % out
    return int(m.group(1)), int(m.group(2))


def _failed_ids(out):
    """守卫最后一行 `FAILED: id1,id2`（无失败时是 `<none>`）。"""
    m = _FAILED_LINE_RE.search(out)
    if not m:
        return set(_CHECK_FAIL_RE.findall(out))
    raw = m.group(1).strip()
    if not raw or raw == '<none>':
        return set()
    return set(part for part in raw.split(',') if part)


# ==========================================================================
# 1. 静态契约：守卫覆盖的**就是**页面加载的那 7 个文件（顺序 + 集合）
# ==========================================================================

def test_template_script_order_is_exactly_this_constant():
    """`contacts.html` 的真实脚本顺序**恰好**等于 `CONTACTS_SCRIPT_ORDER`。

    单看"都包含"对顺序错误无能为力，而顺序错的后果是
    `ReferenceError: Cannot access 'Component' before initialization`
    （`address-book-list.js` 在类定义期 `extends Component`）→ 页面看着在、功能全死。
    """
    assert _template_script_order() == CONTACTS_SCRIPT_ORDER, \
        'contacts.html 脚本顺序=%r，期望=%r' % (_template_script_order(),
                                                CONTACTS_SCRIPT_ORDER)


def test_guard_covers_every_file_the_page_loads():
    """守卫加载的 7 个文件必须真实存在、不重复、且都以 `contacts-app.js` 收尾。"""
    assert len(CONTACTS_SCRIPT_ORDER) == 7, CONTACTS_SCRIPT_ORDER
    assert len(set(CONTACTS_SCRIPT_ORDER)) == 7, CONTACTS_SCRIPT_ORDER
    assert CONTACTS_SCRIPT_ORDER[0] == 'api.js'
    assert CONTACTS_SCRIPT_ORDER[-1] == 'contacts-app.js'
    for name in CONTACTS_SCRIPT_ORDER:
        assert os.path.isfile(os.path.join(JS_DIR, name)), \
            '页面加载了 %s，但文件不存在（浏览器里就是 404 + 后续 ReferenceError）' % name


def test_every_control_id_the_page_queries_exists_in_the_template():
    """本页自有脚本查询的**每一个**控件 id 都必须真的在 `contacts.html` 里。

    这是"防静默失效"的关键：`contacts-app.js` 里导出接线是
    `if (exportBtn && exportFormatEl) { ... }` —— 控件 id 写错一个字母，
    `getElementById` 返回 `null`，**接线被整段跳过、页面不报错、点了没反应**，
    而"`'contacts-export-btn' in html`"这类断言照样通过（它查的是模板，不是脚本）。
    """
    queried = _page_queried_ids()
    known = _template_ids()
    missing = sorted(queried - known)
    assert not missing, ('这些控件 id 被页面脚本查询，但 contacts.html 里不存在（接线会被静默跳过）: %r；'
                         '模板里有: %r' % (missing, sorted(known)))


def test_export_route_contract_matches_the_page_contract():
    """页面允许发送的查询参数集合 == 路由真正读取的集合（两份都从源码抠出来比）。

    事实源是 `src/web/routes/api.py` 的 `address_book_export()`；这里把它读的
    `request.args.get('X')` 名字抠出来，既比对 Python 常量、也比对**守卫 JS 里那份**
    （`CONTRACT_KEYS`）：路由新增一个参数而守卫没跟，这条就会红。
    """
    body = re.search(r'def address_book_export\(\):(.*?)(?=\n@api_bp|\Z)',
                     _read_text(API_ROUTE_PATH), re.S)
    assert body, 'api.py 里找不到 address_book_export()（路由改名了？）'
    from_route = set(_ROUTE_ARG_RE.findall(body.group(1)))
    assert from_route == set(ROUTE_EXPORT_QUERY_KEYS), \
        '路由读的参数=%r，常量=%r' % (sorted(from_route), sorted(ROUTE_EXPORT_QUERY_KEYS))

    in_guard = set(re.search(r'const CONTRACT_KEYS = \[(.*?)\];',
                             CONTACTS_PAGE_GUARD_JS, re.S).group(1)
                   .replace("'", '').replace('"', '').split(','))
    in_guard = set(x.strip() for x in in_guard if x.strip())
    assert in_guard == from_route, \
        '守卫里的 CONTRACT_KEYS=%r，路由读的是=%r' % (sorted(in_guard), sorted(from_route))


def test_page_still_has_no_sort_letter_has_chat_controls():
    """**覆盖缺口的行程开关**（不是"产品缺陷断言"）。

    路由的导出契约有 7 个参数，但 `/contacts` 页目前**只有** `format` / 搜索词 / `label` /
    `kind` 四个来源：`sort` / `has_chat` / `letter` **在页面上根本没有控件**，
    所以"把它们的取值正确转发"这件事**无对象可言**（Task 12 报告 §覆盖矩阵里已如实记录）。
    `.docs/agent-context/04-state/in-progress.md` 的待办里正有"补 letter / has_chat / sort 控件"。

    这条断言的作用是：**哪天真的补上控件了，它会红**，提示去 `CONTACTS_PAGE_GUARD_JS`
    里加上对应的转发断言（而不是让新控件默默没有验证）。

    注意"控件"的判据必须精确到**标识符边界**：`letterGroups` 是 store 的字段名、不是控件，
    用 `'letter' in src` 这种子串判断会**假红**（本文件实测踩过）。所以判据是
    `id="…letter…"` 或**独立标识符** `\\bletter\\b`；下面 `_detect_gap_controls` 自带
    一条正样本自检，免得判据写歪了变成"永远绿的开关"。
    """
    absent = sorted(_detect_gap_controls())
    assert not absent, (
        '页面已经出现 sort/has_chat/letter 控件（或 contacts-app.js 开始转发它们）：%r。'
        '请把对应的"取值被正确转发"断言加进 CONTACTS_PAGE_GUARD_JS，再更新这条行程开关。'
        % absent)


# 每个参数可能的写法（标识符里的下划线/驼峰）
_GAP_KEYS = {'sort': ('sort',), 'has_chat': ('has_chat', 'hasChat'), 'letter': ('letter',)}


def _detect_gap_controls(html=None, app_js=None):
    """→ 页面上已经出现"控件"的那些导出参数名（空集 = 仍然是本文件记录的缺口状态）。"""
    html = _read_text(TEMPLATE) if html is None else html
    app_js = (_read_text(os.path.join(JS_DIR, 'contacts-app.js'))
              if app_js is None else app_js)
    found = set()
    for key, names in _GAP_KEYS.items():
        for name in names:
            if re.search(r'id="[^"]*%s[^"]*"' % re.escape(name), html) or \
               re.search(r'\b%s\b' % re.escape(name), app_js):
                found.add(key)
                break
    return found


def test_gap_control_detector_is_not_vacuous():
    """上面那条行程开关的**正样本自检**：喂进一个带控件的页面，判据必须报出来。

    少了这条，把正则写歪（比如只匹配 `id="sort"` 这种不存在的形式）就会得到一个
    "永远绿的开关"—— 那正好是本文件通篇在防的空转。
    """
    assert _detect_gap_controls(
        html='<select id="contacts-letter-filter"></select>',
        app_js='data.activeLetter') == {'letter'}
    assert _detect_gap_controls(
        html='<select id="contacts-sort"></select>',
        app_js='sort: Store.data.sort') == {'sort'}
    assert _detect_gap_controls(
        html='<input id="contacts-has-chat">',
        app_js='hasChat: true') == {'has_chat'}
    # 负样本：`letterGroups` 是 store 字段，不是控件，不得被当成缺口
    assert _detect_gap_controls(
        html='<div id="contacts-list"></div>',
        app_js='letterGroups: AddressBookStore.letterGroups()') == set()


# ==========================================================================
# 2. 核心：点击导出的交互守卫（vm 单 realm）
# ==========================================================================

def test_click_export_dom_interaction_guard(tmp_path):
    """在**一个 vm realm** 里按页面顺序加载 7 个脚本，模拟"选格式 → 点导出"。

    断言（详见 `CONTACTS_PAGE_GUARD_JS` 里的 check id）：
      * **加载守卫**：加载期不抛异常、7 个文件都真的执行了、命名空间/全局可达、
        页面查询的每一个控件 id 都能解析（假 DOM 的 id 集合取自真实模板）；
      * **点导出**：`xlsx` / `csv` / `html` 三种格式**各**发出**一条**请求，
        且 URL 的 `path` / `format` / `kind` 参数逐项正确；
      * **防空转**：格式下拉至少有 3 个 option、三个 option 取值两两不同、
        并在断言"三个 URL 两两不同"**之前**先证明"三个格式确实不同"；
      * **转发**：选中的 `label` 被带上、不选时不带、搜索词 `q` 被带上并正确编码、
        群聊页签下 `kind=groups`；
      * **下载分支**（页面真实实现是 `fetch` + blob + 隐藏 `<a>`）：
        由 `Content-Disposition` 解出文件名、`a.click()` 真的发生、锚点被移出 DOM；
      * **失败路径**：HTTP 500 时把服务端错误显示给用户，且按钮**不会**永远停在"导出中..."。
    """
    script = _write_guard(tmp_path)
    code, out, err = _run_guard(script, ROOT, _spec_for_root(ROOT))
    assert code == 0, ('/contacts 导出交互守卫失败:\n--- stdout ---\n%s\n--- stderr ---\n%s'
                       % (out, err))
    passed, failed = _parsed_result(out)
    assert failed == 0, '守卫报告 %d 条失败:\n%s' % (failed, out + err)
    # 逐条核对：不靠"输出里没有 FAIL 字样"（守卫自己就会打 `FAILED: <none>`，
    # 那个子串匹配是**假红** —— 实测踩过），而是解析 `FAILED:` 那一行
    assert _failed_ids(out) == set(), '守卫报告的失败断言: %r' % sorted(_failed_ids(out))
    assert passed >= 80, ('守卫只跑了 %d 条断言 —— 用例被清空/提前 return 时会这样，'
                          '说明这份守卫正在空转' % passed)


# ==========================================================================
# 3. 判别力：变异测试（注回缺陷 → **恰好对应的**断言变红 → 按字节还原）
#
# 硬约束是"不许改任何 src/** 产品代码"，而变异必须注回到产品文件里才有意义。
# 两者唯一的相容解法：**把被测的那棵树复制到临时目录，只在副本上注入**。
# 于是仓库里的 src/** 从头到尾没被写过一次（测试末尾重新哈希原文件自查），
# 而"按字节还原"这件事仍然被**证明**：副本上做逆替换后逐字节等于原文、SHA-256 相同。
# ==========================================================================

# 变异锚点全部用**逐字节**锚定（源文件是 CRLF；文本模式读写会把 CRLF 折成 LF，
# 那就不是"按字节还原"了，所以下面一律二进制读写）。
MUTATIONS = (
    dict(
        name='format-binding-hardcoded-to-csv',
        why='格式下拉没被读进去（`format` 写死 csv）→ 三种格式导出同一个文件，且用户看不出',
        file='contacts-app.js',
        old="format: exportFormatEl.value,",
        new="format: 'csv',",
        expect_failed_exact={
            'export/xlsx/format-param',
            'export/xlsx/uses-the-selected-option',
            'export/html/format-param',
            'export/html/uses-the-selected-option',
            'export/three-urls-pairwise-distinct',
        },
    ),
    dict(
        name='export-click-handler-detached',
        why='导出按钮的 handler 没挂上 → 点了完全没反应（没有任何请求）',
        file='contacts-app.js',
        old="exportBtn.addEventListener('click', function() {",
        new="if (false) exportBtn.addEventListener('click', function() {",
        expect_failed_contains={
            'export/click-handler-attached',
            'export/xlsx/exactly-one-request',
            'export/three-urls-pairwise-distinct',
            'pending/click-handler-attached',
            'error/request-fired',
            'guard/at-least-one-export-request-observed',
        },
        must_stay_pass={
            'load/no-load-time-throw',
            'load/init-rendered-after-api-round-trip',
            'guard/format-dropdown-has-3-options',
            'guard/format-options-are-exactly-xlsx-csv-html',
            'label/change-reaches-the-store',
            'search/debounced-input-reaches-the-store',
        },
    ),
    dict(
        name='export-kind-hardcoded-to-contacts',
        why='群聊页签导出时仍然按联系人导出（`kind` 写死）',
        file='contacts-app.js',
        old="kind: AddressBookStore.data.activeTab === 'groups' ? 'groups' : 'contacts',",
        new="kind: 'contacts',",
        expect_failed_exact={'kind/groups-tab-sends-kind-groups'},
    ),
    dict(
        name='label-filter-dropped-from-the-export',
        why='标签筛选没被带进导出（`label` 写死空串）→ 导出的不是用户此刻看到的内容',
        file='contacts-app.js',
        old=("q: AddressBookStore.data.searchQuery,\r\n"
             "        label: AddressBookStore.data.labelFilter,\r\n"
             "        // 联系人页排除群聊"),
        new=("q: AddressBookStore.data.searchQuery,\r\n"
             "        label: '',\r\n"
             "        // 联系人页排除群聊"),
        expect_failed_exact={'label/selected-label-is-forwarded'},
    ),
    dict(
        name='export-button-control-id-typo',
        why='控件 id 写错一个字母 → `if (exportBtn && ...)` 静默跳过整段接线（页面不报错）',
        file='contacts-app.js',
        old="var exportBtn = document.getElementById('contacts-export-btn');",
        new="var exportBtn = document.getElementById('contacts-export-button');",
        expect_failed_contains={
            'load/every-queried-control-id-exists',
            'export/click-handler-attached',
            'export/xlsx/exactly-one-request',
            'guard/at-least-one-export-request-observed',
        },
        must_stay_pass={
            'load/no-load-time-throw',
            'load/init-rendered-after-api-round-trip',
            'guard/format-dropdown-has-3-options',
        },
    ),
    dict(
        name='load-time-referenceerror-like-issue-15',
        why='加载期引用了不存在的全局（#15 的 TYPES）→ 初始化中断；这正是"静态断言照样全绿"的形态',
        file='contacts-app.js',
        old="  var listEl = document.getElementById('contacts-list');",
        new=("  var _legacySeed = LEGACY_TYPES.length;\r\n"
             "  var listEl = document.getElementById('contacts-list');"),
        expect_failed_contains={
            'load/no-load-time-throw',
            'load/all-page-files-executed',
            'load/init-rendered-after-api-round-trip',
            'export/xlsx/exactly-one-request',
        },
        must_stay_pass={
            # 这几条只依赖假 DOM 的播种，与页面脚本有没有跑完无关：
            # 它们绿着而行为断言全红，正是"为什么必须有加载守卫"的现场证据
            'load/fake-dom-id-set-nonempty',
            'guard/export-button-has-real-label',
            'guard/format-dropdown-has-3-options',
            'guard/format-options-distinct',
        },
    ),
    dict(
        name='top-level-name-collision-like-issue-23',
        why='顶层 `function escapeHtml` 与 utils.js 的 `const escapeHtml` 撞名 → 该文件实例化期 SyntaxError、一行都不执行',
        file='components/contact-detail.js',
        old="// contact-detail.js",
        new="function escapeHtml(s) { return s; }\r\n// contact-detail.js",
        expect_failed_contains={
            'load/no-load-time-throw',
            'load/all-page-files-executed',
            'export/xlsx/exactly-one-request',
        },
        must_stay_pass={
            'load/fake-dom-id-set-nonempty',
            'guard/format-dropdown-has-3-options',
        },
    ),
)


def _copy_page_tree(dest_root):
    """把被测的**页面相关**文件复制到临时目录（只有产品代码，没有任何真实数据）。"""
    dest_js = os.path.join(dest_root, 'src', 'web', 'static', 'js')
    os.makedirs(os.path.dirname(dest_js), exist_ok=True)
    shutil.copytree(JS_DIR, dest_js)
    dest_tpl = os.path.join(dest_root, 'src', 'web', 'templates')
    os.makedirs(dest_tpl, exist_ok=True)
    shutil.copy2(TEMPLATE, os.path.join(dest_tpl, 'contacts.html'))
    return dest_root


def _assert_expectations(name, observed, mut):
    """把"变红了"细化成"**恰好**是这几条变红"（否则"全都红"也能骗过变异测试）。"""
    if 'expect_failed_exact' in mut:
        assert observed == set(mut['expect_failed_exact']), (
            '变异 %r：期望**恰好**这些断言变红\n  期望=%r\n  实际=%r\n  多红=%r\n  漏红=%r'
            % (name, sorted(mut['expect_failed_exact']), sorted(observed),
               sorted(observed - set(mut['expect_failed_exact'])),
               sorted(set(mut['expect_failed_exact']) - observed)))
        return
    missing = set(mut['expect_failed_contains']) - observed
    assert not missing, ('变异 %r：这些断言**应当**变红却绿着（断言没有判别力）: %r\n  实际红的=%r'
                         % (name, sorted(missing), sorted(observed)))
    leaked = set(mut['must_stay_pass']) & observed
    assert not leaked, ('变异 %r：这些断言**不该**跟着红（说明它们与本次缺陷耦合，'
                        '不能用来定位问题）: %r' % (name, sorted(leaked)))


def test_guard_is_discriminating_under_mutation(tmp_path):
    """把缺陷注回**副本**，要求**恰好对应的**断言变红；并给出按字节还原的哈希证据。

    还原证据是**断言**出来的，不是写在报告里的说法：
      * `mutant.replace(new, old, 1) == original`（逆替换逐字节回到原文）；
      * 副本还原后 `sha256` == 原文 `sha256` == 变异前记录的哈希；
      * 整个用例结束后**仓库里的产品文件**哈希与开始前一致（证明 src/** 从未被写过）。
    """
    orig_bytes = open(os.path.join(JS_DIR, 'contacts-app.js'), 'rb').read()
    repo_hashes_before = {name: _sha256(os.path.join(JS_DIR, name))
                          for name in CONTACTS_SCRIPT_ORDER}

    mut_root = _copy_page_tree(os.path.join(str(tmp_path), 'mutant'))
    mut_js_dir = os.path.join(mut_root, 'src', 'web', 'static', 'js')
    script = _write_guard(tmp_path)

    # --- 基线：副本必须与真树行为完全一致（否则下面的"变红"说明不了任何事） ----
    code, out, err = _run_guard(script, mut_root, _spec_for_root(mut_root))
    assert code == 0, ('未变异的副本自己就没过（说明副本不是忠实拷贝）:\n%s\n%s'
                       % (out, err))
    base_passed, base_failed = _parsed_result(out)
    assert base_failed == 0 and base_passed >= 80, (base_passed, base_failed)

    print('\n=== 变异测试：基线（副本，未注入）= %d passed, %d failed ===' % (base_passed, base_failed))
    print('=== 仓库原文件 SHA-256（开始前）===')
    for name in CONTACTS_SCRIPT_ORDER:
        print('    %-36s %s' % (name, repo_hashes_before[name]))

    for mut in MUTATIONS:
        target = os.path.join(mut_js_dir, mut['file'])
        old = mut['old'].encode('utf-8')
        new = mut['new'].encode('utf-8')

        # 每个变异都从**原文**起手：先按字节写回原文，再注入
        with open(target, 'wb') as fh:
            fh.write(orig_bytes if mut['file'] == 'contacts-app.js'
                     else open(os.path.join(JS_DIR, mut['file']), 'rb').read())
        pristine = open(target, 'rb').read()
        n = pristine.count(old)
        assert n == 1, ('变异锚点在 %s 里出现 %d 次（要求恰好 1 次）—— 产品源码被改过，'
                        '请更新变异锚点：%r' % (mut['file'], n, mut['old'][:60]))
        mutant = pristine.replace(old, new, 1)
        with open(target, 'wb') as fh:
            fh.write(mutant)
        sha_mutant = _sha256(target)
        assert sha_mutant != hashlib.sha256(pristine).hexdigest()

        code, out, err = _run_guard(script, mut_root, _spec_for_root(mut_root))
        observed = _failed_ids(out)
        assert code != 0 or observed, ('变异 %r 没有让守卫变红 —— 这条缺陷测不出来：%s'
                                       % (mut['name'], mut['why']))
        _assert_expectations(mut['name'], observed, mut)

        # --- 按字节还原（副本上做逆替换，并要求逐字节回到原文） ---
        restored = mutant.replace(new, old, 1)
        assert restored == pristine, '逆替换没有逐字节回到原文（变异不是纯单点注入）'
        with open(target, 'wb') as fh:
            fh.write(restored)
        sha_restored = _sha256(target)
        assert sha_restored == hashlib.sha256(pristine).hexdigest(), (
            '还原后哈希不一致：%s != %s' % (sha_restored, hashlib.sha256(pristine).hexdigest()))
        if mut['file'] == 'contacts-app.js':
            assert sha_restored == repo_hashes_before['contacts-app.js'], (
                '副本还原后的哈希与仓库原文件不一致（副本本身被污染了）')
        # 还原后守卫必须重新全绿 —— 证明"红"确实来自那次注入
        code2, out2, _ = _run_guard(script, mut_root, _spec_for_root(mut_root))
        assert code2 == 0 and not _failed_ids(out2), (
            '还原后仍然失败，说明副本已被污染:\n%s' % out2)

        print('\n=== 变异 %-42s ===' % mut['name'])
        print('    注入                : %s' % mut['why'])
        print('    文件                : src/web/static/js/%s' % mut['file'])
        print('    变异后 SHA-256      : %s' % sha_mutant)
        print('    还原后 SHA-256      : %s' % sha_restored)
        print('    原文  SHA-256       : %s' % hashlib.sha256(pristine).hexdigest())
        print('    变红的断言 (%2d)     : %s' % (len(observed), ', '.join(sorted(observed)) or '<none>'))
        print('    守卫退出码          : %d → 还原后 %d' % (code, code2))

    # --- 收尾自查：仓库里的产品文件一个字节都没变 ---
    repo_hashes_after = {name: _sha256(os.path.join(JS_DIR, name))
                         for name in CONTACTS_SCRIPT_ORDER}
    assert repo_hashes_after == repo_hashes_before, (
        '仓库里的产品文件被改动了！before=%r after=%r' % (repo_hashes_before, repo_hashes_after))
    print('\n=== 收尾自查：src/** 全部 %d 个页面文件的 SHA-256 与开始前完全一致 ==='
          % len(repo_hashes_before))


# ==========================================================================
# 4. 已知口径差异（**记账**，不是"缺陷断言"）：
#    「导出所见 == 屏幕所见」在**群聊页签**上有一条语义缺口
#
# 页面自己的承诺是"导出的就是用户此刻看到的内容"（`contacts-app.js:125`）。
# 但对**群聊页签**：
#   * 屏幕上的过滤是**客户端**做的（`AddressBookStore.filteredContacts()`，字段只查
#     display_name / remark / nick_name / alias / wxid）；
#   * 导出请求发到服务端，由 `filter_contacts(_match)` 过滤 —— 它的 `q` **还查
#     `phone` 与 `description`**（`src/engine/services/address_book.py:348-354`）。
# 于是存在这样一条查询：**屏幕上一行都不显示，导出的文件里却有这个人**。
# 这一条把差异**当场跑出来**（两侧都跑**真实**实现：Python 侧 `filter_contacts`、
# Node 侧在 vm 里加载真正的 `address-book-store.js`），并顺带钉住它是有界的
# （按名字匹配时两侧一致 ⇒ 不是"两个过滤器完全不相干"）。
#
# 口径：**只报告不修**（属于产品代码，Task 12 的硬约束禁止改 `src/**`）。
# 自清理：这条差异被修掉（两侧口径统一）之后本用例会红并告诉你去销账，
# 理由是"不要为保住绿灯而留一笔假账"（与 `tests/test_search_page.py` §7.3 同一口径）。
# ==========================================================================

_GROUP_QUERY_FIXTURE = (
    # 只有 description 含查询词：屏幕侧看不见，导出侧会带上（**导出比屏幕多**）
    {'wxid': 'room_alpha@chatroom', 'display_name': 'room-alpha', 'is_group': True,
     'remark': '', 'nick_name': '', 'alias': '', 'labels': [], 'msg_count': 5,
     'description': '维修'},
    # 名字里含查询词：两侧都应当命中（有界性的正样本）
    {'wxid': 'room_beta@chatroom', 'display_name': 'alpha-beta', 'is_group': True,
     'remark': '', 'nick_name': '', 'alias': '', 'labels': [], 'msg_count': 7,
     'description': ''},
    # 完全不匹配
    {'wxid': 'room_gamma@chatroom', 'display_name': 'room-gamma', 'is_group': True,
     'remark': '', 'nick_name': '', 'alias': '', 'labels': [], 'msg_count': 0,
     'description': ''},
)

# Node 侧探针：在 vm 里加载**真的** `address-book-store.js`，只用它自己的
# `filteredContacts()` 算"群聊页签屏幕上会显示哪些人"（绝不重写一遍过滤逻辑）
STORE_FILTER_PROBE_JS = r'''// 用法: node store_filter_probe.js <js_dir> <payload.json> <out.json>
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const jsDir = process.argv[2];
const payload = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const sandbox = {};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(jsDir, 'address-book-store.js'), 'utf8'),
                sandbox, { filename: 'address-book-store.js' });
const store = vm.runInContext('AddressBookStore', sandbox);
store.data.activeTab = payload.activeTab;
store.data.groups = payload.groups;
store.data.contacts = payload.contacts || [];
store.data.searchQuery = payload.q;
store.data.labelFilter = payload.label || '';
const out = store.filteredContacts().map(function (c) { return c.wxid; });
fs.writeFileSync(process.argv[4], JSON.stringify(out), 'utf8');
'''


def _client_visible_group_wxids(tmp_path, q, label=''):
    """群聊页签上**屏幕上会显示**的 wxid —— 跑真实 `address-book-store.js` 得到。"""
    exe = _node_or_skip()
    script = os.path.join(str(tmp_path), 'store_filter_probe.js')
    with open(script, 'w', encoding='utf-8', newline='') as fh:
        fh.write(STORE_FILTER_PROBE_JS)
    payload = os.path.join(str(tmp_path), 'probe_in.json')
    out = os.path.join(str(tmp_path), 'probe_out.json')
    with open(payload, 'w', encoding='utf-8') as fh:
        json.dump({'activeTab': 'groups', 'groups': list(_GROUP_QUERY_FIXTURE),
                   'contacts': [], 'q': q, 'label': label}, fh, ensure_ascii=False)
    code, stdout, stderr = _run_node([exe, script, JS_DIR, payload, out], cwd=ROOT)
    assert code == 0, 'Node 探针失败:\n%s\n%s' % (stdout, stderr)
    with open(out, encoding='utf-8') as fh:
        return json.load(fh)


def _server_exported_group_wxids(q, label=None):
    """`/api/address-book/export?kind=groups` 会导出哪些 wxid —— 跑真实 `filter_contacts`。"""
    from engine.services.address_book import filter_contacts
    rows = filter_contacts([dict(r) for r in _GROUP_QUERY_FIXTURE], q=q, sort='name',
                           has_chat=None, letter='', kind='groups', label=label)
    return [r['wxid'] for r in rows]


def test_known_divergence_group_tab_query_semantics(tmp_path):
    """**已知差异（记账）**：群聊页签上"屏幕所见"与"导出所得"的 `q` 语义不等价。

    期望（按页面注释的承诺）：两者应当一致。
    实际（跑出来的 → 见断言失败信息）：只有 `description` 命中查询词的群聊
    **屏幕上一行都不显示，却会被导出**。

    控制方：`src/engine/services/address_book.py` 的 `filter_contacts._match`
    （多查了 `phone` / `description`）与
    `src/web/static/js/address-book-store.js` 的 `filteredContacts()`
    （只查 5 个字段）。Task 12 **只报告不修**（硬约束：不许改 `src/**`）。
    """
    # --- 有界性正样本：按名字匹配时两侧**必须一致**（证明不是"两个过滤器毫不相干"）---
    by_name_client = _client_visible_group_wxids(tmp_path, 'alpha')
    by_name_server = _server_exported_group_wxids('alpha')
    assert sorted(by_name_client) == sorted(by_name_server) \
        == ['room_alpha@chatroom', 'room_beta@chatroom'], \
        '按名字匹配时两侧不一致（%r vs %r）—— 差异已经不止"多查两个字段"了，请重新评估' \
        % (by_name_client, by_name_server)

    # --- 差异本体：只有 description 命中 ---
    client = _client_visible_group_wxids(tmp_path, '维修')
    server = _server_exported_group_wxids('维修')
    assert client == [], client          # 非空守卫：屏幕侧确实看不见（不是"两边都空"的假差异）
    assert server == ['room_alpha@chatroom'], server
    assert client != server, (
        '两侧口径已经一致（屏幕=%r，导出=%r）—— 这笔账到期了，请删掉本用例'
        '（不要为了保住绿灯而留一笔假账）' % (client, server))
    # 差异方向必须如实说清楚：**导出比屏幕多**（用户看到 0 行，却导出 1 个人）
    assert sorted(set(server) - set(client)) == ['room_alpha@chatroom']
    assert set(client) - set(server) == set()
    print('\n[已知差异·记账] 群聊页签 q=%r：屏幕可见=%r，导出含=%r → 导出比屏幕多 %r'
          % ('维修', client, server, sorted(set(server) - set(client))))
    print('[已知差异·有界] 按名字 q=%r：两侧一致=%r' % ('alpha', sorted(by_name_client)))


# ==========================================================================
# 守卫本体（随测试一起交付，不单独放文件）
#
# 用法: node contacts_page_guard.js <REPO_ROOT> <spec.json>
#   spec.json = {"order": [...页面脚本顺序...], "queried_ids": [...页面查询的控件 id...]}
# 退出码 0 = 全绿；非 0 = 有 FAIL。最后一行 `FAILED: id1,id2` 供变异测试做**精确**映射。
# ==========================================================================

CONTACTS_PAGE_GUARD_JS = r'''// Task 12 —— /contacts 页「点击导出」交互守卫（浏览器真实加载路径，vm 单 realm）
//
// 为什么必须有这段：脚本是**经典脚本**，7 个文件共用同一个全局名字空间。
//   ① 顶层同名声明 → 浏览器在**实例化阶段**抛 SyntaxError → 那个文件**一行都不执行**
//      （known-issues #23）；`require()` 永远测不出来（CommonJS 各自一个函数作用域）；
//   ② 引用项目里不存在的全局 → 初始化中断（计划缺陷 #15 的 `TYPES`），
//      页面只剩静态 HTML，而"只查静态 HTML"的断言照样通过；
//   ③ 控件 id 写错一个字母 → `getElementById` 返回 null → 接线被静默跳过、点了没反应。
// 本守卫把 ①②③ 与"点导出到底发了什么请求"一起钉住。
//
// 边界：这是**假 DOM + 假 fetch**，不是真浏览器 —— 不覆盖视觉渲染与原生下载对话框。
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = process.argv[2];
const SPEC = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const JS_DIR = path.join(ROOT, 'src', 'web', 'static', 'js');
const TEMPLATE = path.join(ROOT, 'src', 'web', 'templates', 'contacts.html');

// 路由 `/api/address-book/export` 接受的查询参数全集（与 tests/test_contacts_export_dom.py
// 的 ROUTE_EXPORT_QUERY_KEYS 同源，由那条测试从 api.py 里抠出来比对）
const CONTRACT_KEYS = ['format', 'q', 'sort', 'has_chat', 'letter', 'kind', 'label'];
const EXPORT_PATH = '/api/address-book/export';
const FORMATS = ['xlsx', 'csv', 'html'];
// 假响应里的文件名**刻意固定**（不随请求的 format 变）：这样这几条断言测的是
// "页面从 Content-Disposition 里解文件名"这一件事；"每种格式真的给出不同扩展名"
// 是服务端的职责，由 tests/test_contacts_export_route.py 覆盖。
const HEADER_FILENAME = 'synthetic-contacts-export.csv';
// 服务端标签列表（合成值，绝不用真实标签/姓名）
const SERVER_LABELS = ['alpha-tag', 'beta-tag'];
const BUSY_LABEL = '导出中...';
const BUTTON_LABEL = '导出';

let pass = 0, fail = 0;
const failedIds = [];
const unhandled = [];
process.on('unhandledRejection', function (e) {
  unhandled.push(String((e && e.message) || e));
});

function brief(s) {
  s = (s === undefined || s === null) ? '' : String(s);
  return s.replace(/\s+/g, ' ').slice(0, 200);
}

function check(id, ok, extra) {
  if (ok) { pass++; console.log('  PASS  ' + id); }
  else { fail++; failedIds.push(id); console.log('  FAIL  ' + id + '  :: ' + brief(extra)); }
}

function finish() {
  console.log('\nRESULT: ' + pass + ' passed, ' + fail + ' failed');
  console.log('FAILED: ' + (failedIds.length ? failedIds.join(',') : '<none>'));
  process.exit(fail ? 1 : 0);
}

function tick(ms) { return new Promise(function (r) { setTimeout(r, ms || 0); }); }

// ---------------------------------------------------------------------------
// 假 DOM：id 集合取自 contacts.html 的**真实** id（未知 id 一律返回 null）
// ---------------------------------------------------------------------------

const ID_RE = /\bid="([A-Za-z0-9_\-]+)"/g;

function collectIds(src, into) {
  ID_RE.lastIndex = 0;
  let m;
  while ((m = ID_RE.exec(src))) into[m[1]] = true;
  return into;
}

function makeElement(doc, id) {
  const el = {
    id: id || '', tagName: 'DIV', value: '', textContent: '', disabled: false,
    href: '', download: '', title: '', src: '',
    style: {}, dataset: {}, options: [], children: [], checked: false,
    _html: '', _listeners: {}, _clicked: 0,
    addEventListener: function (ev, fn) {
      (el._listeners[ev] = el._listeners[ev] || []).push(fn);
      return el;
    },
    removeEventListener: function () {},
    appendChild: function (child) {
      el.children.push(child);
      if (child && child.tagName === 'OPTION') el.options.push(child);
      return child;
    },
    removeChild: function (child) {
      const i = el.children.indexOf(child);
      if (i === -1) throw new Error('removeChild: #' + child.id + ' 不是 body 的子节点');
      el.children.splice(i, 1);
      return child;
    },
    querySelectorAll: function () { return []; },
    querySelector: function () { return null; },
    closest: function () { return null; },
    setAttribute: function (k, v) { el[k] = v; },
    removeAttribute: function () {},
    focus: function () {},
    click: function () { el._clicked += 1; doc._clicks.push(el); },
    classList: {
      add: function () {}, remove: function () {}, toggle: function () {},
      contains: function () { return false; },
    },
  };
  Object.defineProperty(el, 'innerHTML', {
    get: function () { return el._html; },
    set: function (v) {
      el._html = (v === undefined || v === null) ? '' : String(v);
      // 页面对"自己用 innerHTML 造出来的控件"随后会 getElementById
      collectIds(el._html, doc._known);
    },
  });
  return el;
}

// 真浏览器会把 `<select>` 的第一个 `<option>` 默认选中 —— 这里必须照做，
// 否则"默认格式"这类断言全落在空值上（正是本项目反复强调的"空转"）。
function selectOptionsFromTemplate(html, id) {
  const re = new RegExp('<select[^>]*id="' + id + '"[^>]*>([\\s\\S]*?)</select>');
  const m = re.exec(html);
  if (!m) return null;
  const out = [];
  const ore = /<option[^>]*value="([^"]*)"[^>]*>([^<]*)<\/option>/g;
  let om;
  while ((om = ore.exec(m[1]))) {
    out.push({ value: om[1], textContent: om[2], selected: false, tagName: 'OPTION' });
  }
  return out;
}

function seedSelect(doc, html, id) {
  const el = doc.getElementById(id);
  if (!el) return;
  const opts = selectOptionsFromTemplate(html, id);
  if (!opts) return;
  el.options = opts;
  el.value = opts.length ? opts[0].value : '';   // 真浏览器：第一个 option 默认选中
}

// 按钮的文案也要从模板播种：页面用 `exportBtn.textContent` 存原值、置"导出中..."再还原
function seedText(doc, html, id) {
  const re = new RegExp('<\\w+[^>]*id="' + id + '"[^>]*>([^<]*)</\\w+>');
  const m = re.exec(html);
  const el = doc.getElementById(id);
  if (m && el) el.textContent = m[1];
}

function makeDocument() {
  const html = fs.readFileSync(TEMPLATE, 'utf8');
  const known = collectIds(html, {});
  const cache = {};
  const doc = {
    _known: known, _clicks: [], _created: [],
    getElementById: function (id) {
      if (!known[id]) return null;      // 真页面里不存在的 id → null（暴露拼写错误）
      if (!cache[id]) cache[id] = makeElement(doc, id);
      return cache[id];
    },
    createElement: function (tag) {
      const el = makeElement(doc, '');
      el.tagName = String(tag).toUpperCase();
      doc._created.push(el);
      return el;
    },
    addEventListener: function () {},
    querySelectorAll: function () { return []; },
  };
  doc.body = makeElement(doc, 'body');
  seedSelect(doc, html, 'contacts-export-format');
  seedSelect(doc, html, 'contacts-label-filter');
  seedText(doc, html, 'contacts-export-btn');
  return doc;
}

// 派发监听器：真浏览器的 `this` 是元素本身（页面的防抖 input 处理器用了 `this.value`）
function fire(el, type, ev) {
  const list = (el && el._listeners && el._listeners[type]) || [];
  const event = Object.assign({ type: type, target: el, preventDefault: function () {} }, ev || {});
  list.forEach(function (fn) { fn.call(el, event); });
  return list.length;
}

// ---------------------------------------------------------------------------
// 假 fetch
// ---------------------------------------------------------------------------

function jsonResponse(payload) {
  return Promise.resolve({
    ok: true, status: 200,
    headers: { get: function () { return null; } },
    json: function () { return Promise.resolve(payload); },
    blob: function () { return Promise.resolve({ __blob: true }); },
  });
}

function exportResponse() {
  return Promise.resolve({
    ok: true, status: 200,
    headers: {
      get: function (h) {
        return String(h).toLowerCase() === 'content-disposition'
          ? 'attachment; filename="' + HEADER_FILENAME + '"' : null;
      },
    },
    json: function () { return Promise.resolve({}); },
    blob: function () { return Promise.resolve({ __blob: true, size: 12345 }); },
  });
}

function exportErrorResponse() {
  return Promise.resolve({
    ok: false, status: 500,
    headers: { get: function () { return null; } },
    json: function () { return Promise.resolve({ error: 'synthetic export failure' }); },
  });
}

function makeFetch(mode, calls) {
  return function (url, opts) {
    const rec = { url: String(url), opts: opts || {} };
    calls.push(rec);
    const u = rec.url;
    if (u.indexOf(EXPORT_PATH) === 0) {
      if (mode === 'pending-export') return new Promise(function () {});
      if (mode === 'export-500') return exportErrorResponse();
      return exportResponse();
    }
    if (u.indexOf('/api/address-book/labels') === 0) return jsonResponse({ labels: SERVER_LABELS });
    if (u.indexOf('/api/address-book/groups') === 0) return jsonResponse({ groups: [], total: 0 });
    if (u.indexOf('/api/address-book') === 0) {
      return jsonResponse({ contacts: [], total: 0, page: 1, per_page: 100, total_pages: 1 });
    }
    return jsonResponse({});
  };
}

function loadRealm(mode) {
  const calls = [], logs = [], alerts = [];
  const urlCalls = { created: [], revoked: [] };
  const sandbox = {
    console: {
      log: function (m) { logs.push('log: ' + m); },
      warn: function (m) { logs.push('warn: ' + m); },
      error: function (m) { logs.push('error: ' + m); },
    },
    document: makeDocument(),
    fetch: makeFetch(mode, calls),
    AbortController: AbortController,
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    alert: function (m) { alerts.push(String(m)); },
    URL: {
      createObjectURL: function (blob) {
        const u = 'blob:fake/' + (urlCalls.created.length + 1);
        urlCalls.created.push({ url: u, blob: blob });
        return u;
      },
      revokeObjectURL: function (u) { urlCalls.revoked.push(u); },
    },
  };
  sandbox.window = sandbox;
  const ctx = vm.createContext(sandbox);
  const loaded = [], errors = [];
  SPEC.order.forEach(function (f) {
    const p = path.join(JS_DIR, f);
    if (!fs.existsSync(p)) {
      errors.push(f + ' -> 文件不存在（浏览器里是 404 + 后续 ReferenceError）');
      return;
    }
    try {
      vm.runInContext(fs.readFileSync(p, 'utf8'), ctx, { filename: f });
      loaded.push(f);
    } catch (e) {
      errors.push(f + ' -> ' + e.name + ': ' + e.message);
    }
  });
  return { ctx: ctx, sandbox: sandbox, doc: sandbox.document, calls: calls, logs: logs,
           alerts: alerts, urlCalls: urlCalls, loaded: loaded, errors: errors };
}

function ev(ctx, expr) { return vm.runInContext(expr, ctx); }

function paramsOf(url) {
  try { return new URL('http://guard.local' + String(url)).searchParams; }
  catch (e) { return new URLSearchParams(); }
}

function keysOf(url) {
  const out = [];
  paramsOf(url).forEach(function (v, k) { out.push(k); });
  return out.sort();
}

function exportCalls(realm) {
  return realm.calls.filter(function (c) { return c.url.indexOf(EXPORT_PATH) === 0; });
}

async function clickExportOne(realm, fmt) {
  const sel = realm.doc.getElementById('contacts-export-format');
  if (fmt) sel.value = fmt;
  const before = exportCalls(realm).length;
  fire(realm.doc.getElementById('contacts-export-btn'), 'click');
  await tick(30);
  const fresh = exportCalls(realm).slice(before);
  return fresh.length ? fresh[fresh.length - 1].url : '';
}

// ===========================================================================
async function main() {
  const tplIds = collectIds(fs.readFileSync(TEMPLATE, 'utf8'), {});

  // -------------------------------------------------------------------------
  console.log('--- A. classic script load guard: ' + SPEC.order.length + ' page files, one realm ---');
  // -------------------------------------------------------------------------
  const A = loadRealm('ok');
  await tick(60);
  check('load/no-load-time-throw', A.errors.length === 0, A.errors.join(' | '));
  check('load/all-page-files-executed', A.loaded.length === SPEC.order.length,
        'loaded=' + JSON.stringify(A.loaded));
  check('load/page-order-matches-contacts-html',
        A.loaded.length === SPEC.order.length &&
        A.loaded.every(function (f, i) { return f === SPEC.order[i]; }),
        'expected=' + JSON.stringify(SPEC.order));
  check('load/store-reachable', ev(A.ctx, 'typeof AddressBookStore') === 'object',
        ev(A.ctx, 'typeof AddressBookStore'));
  check('load/component-base-class-reachable', ev(A.ctx, 'typeof Component') === 'function',
        ev(A.ctx, 'typeof Component'));
  check('load/api-client-reachable',
        ev(A.ctx, 'typeof api === "object" && typeof api.addressBookExportUrl === "function"') === true);
  check('load/utils-globals-intact-after-all-' + SPEC.order.length,
        ev(A.ctx, 'escapeHtml("<b>")') === '&lt;b&gt;', ev(A.ctx, 'escapeHtml("<b>")'));
  check('load/page-exposes-goto-page', ev(A.ctx, 'typeof window._contactsGoToPage') === 'function',
        ev(A.ctx, 'typeof window._contactsGoToPage'));
  check('load/fake-dom-id-set-nonempty', Object.keys(A.doc._known).length >= 8,
        'ids=' + Object.keys(A.doc._known).length);
  const missingIds = SPEC.queried_ids.filter(function (id) {
    return A.doc.getElementById(id) === null;
  });
  check('load/every-queried-control-id-exists', missingIds.length === 0,
        '页面查询但模板里不存在的 id=' + JSON.stringify(missingIds));
  check('load/init-rendered-after-api-round-trip',
        String(A.doc.getElementById('contacts-list').innerHTML).indexOf('未找到联系人') !== -1,
        A.doc.getElementById('contacts-list').innerHTML);
  check('load/loading-placeholder-replaced',
        String(A.doc.getElementById('contacts-list').innerHTML).indexOf('加载通讯录') === -1,
        A.doc.getElementById('contacts-list').innerHTML);
  const labelOpts = A.doc.getElementById('contacts-label-filter').options.map(
    function (o) { return o.value; });
  check('load/label-select-filled-from-server',
        SERVER_LABELS.every(function (n) { return labelOpts.indexOf(n) !== -1; }),
        'options=' + JSON.stringify(labelOpts));
  check('load/no-console-error-during-init', A.logs.length === 0, JSON.stringify(A.logs));
  check('load/no-unhandled-rejection', unhandled.length === 0, JSON.stringify(unhandled));

  // -------------------------------------------------------------------------
  console.log('--- B. 防空转：控件与样本本身必须成立 ---');
  // -------------------------------------------------------------------------
  const btn = A.doc.getElementById('contacts-export-btn');
  const fmtSel = A.doc.getElementById('contacts-export-format');
  check('guard/export-button-has-real-label', btn.textContent === BUTTON_LABEL,
        'textContent=' + JSON.stringify(btn.textContent));
  check('guard/format-dropdown-has-3-options', fmtSel.options.length === 3,
        'options=' + JSON.stringify(fmtSel.options.map(function (o) { return o.value; })));
  const optVals = fmtSel.options.map(function (o) { return o.value; });
  check('guard/format-options-distinct', new Set(optVals).size === optVals.length,
        JSON.stringify(optVals));
  check('guard/format-options-are-exactly-xlsx-csv-html',
        JSON.stringify(optVals) === JSON.stringify(FORMATS), JSON.stringify(optVals));

  // -------------------------------------------------------------------------
  console.log('--- C. 核心：选格式 → 点导出，三种格式各一条请求且 URL 两两不同 ---');
  // -------------------------------------------------------------------------
  const urls = {};
  for (let i = 0; i < FORMATS.length; i++) {
    const fmt = FORMATS[i];
    fmtSel.value = fmt;
    // 先证明"下拉框真的被切到了这一格"。这一条是**测试自身**的防空转：
    // 漏掉上面那行赋值时，三次点击会全用播种下来的默认值 xlsx，
    // 而 `uses-the-selected-option`（拿 URL 与当前下拉值比）**照样全绿** —— 实测踩过。
    check('export/' + fmt + '/dropdown-actually-set-to-this-format', fmtSel.value === fmt,
          'dropdown=' + JSON.stringify(fmtSel.value));
    // 真浏览器不会把 click 派发给 disabled 的按钮：这里先钉住"上一轮已经收尾"，
    // 否则三次点击会在按钮还禁用着的时候被硬塞进去（假 DOM 会照收，就失真了）
    check('export/' + fmt + '/button-enabled-before-click', btn.disabled === false,
          'disabled=' + btn.disabled + ' text=' + JSON.stringify(btn.textContent));
    const before = exportCalls(A).length;
    const listeners = fire(btn, 'click');
    if (i === 0) {
      check('export/click-handler-attached', listeners >= 1, 'listeners=' + listeners);
    }
    await tick(30);
    const fresh = exportCalls(A).slice(before);
    check('export/' + fmt + '/exactly-one-request', fresh.length === 1,
          '新请求数=' + fresh.length + ' URL=' + (fresh[0] && fresh[0].url));
    const rec = fresh[0] || { url: '', opts: {} };
    urls[fmt] = rec.url;
    const p = paramsOf(rec.url);
    check('export/' + fmt + '/path', rec.url.split('?')[0] === EXPORT_PATH, rec.url);
    check('export/' + fmt + '/format-param', p.get('format') === fmt,
          'format=' + JSON.stringify(p.get('format')) + ' URL=' + rec.url);
    check('export/' + fmt + '/uses-the-selected-option',
          p.get('format') === fmtSel.value,
          'URL 里的 format=' + JSON.stringify(p.get('format')) +
          ' 下拉框的值=' + JSON.stringify(fmtSel.value));
    check('export/' + fmt + '/kind-param', p.get('kind') === 'contacts',
          'kind=' + JSON.stringify(p.get('kind')) + ' URL=' + rec.url);
    check('export/' + fmt + '/query-keys-within-contract',
          keysOf(rec.url).every(function (k) { return CONTRACT_KEYS.indexOf(k) !== -1; }),
          'keys=' + JSON.stringify(keysOf(rec.url)));
    check('export/' + fmt + '/no-label-param-when-none-selected', !p.has('label'),
          'URL=' + rec.url);
    check('export/' + fmt + '/button-restored',
          btn.disabled === false && btn.textContent === BUTTON_LABEL,
          'disabled=' + btn.disabled + ' text=' + JSON.stringify(btn.textContent));
    if (fmt === 'xlsx') {
      check('export/xlsx/fetch-called-with-just-the-url',
            Object.keys(rec.opts).length === 0, JSON.stringify(rec.opts));
    }
  }
  check('export/three-urls-pairwise-distinct', new Set(FORMATS.map(function (f) {
    return urls[f]; })).size === 3, JSON.stringify(urls));

  // ---- 下载分支（页面真实实现：fetch → blob → 隐藏 <a>.click()） ----
  const anchors = A.doc._clicks;
  check('export/download/anchor-clicked-per-format', anchors.length === FORMATS.length,
        'clicks=' + anchors.length);
  check('export/download/blob-created-per-format',
        A.urlCalls.created.length === FORMATS.length, 'created=' + A.urlCalls.created.length);
  FORMATS.forEach(function (fmt, i) {
    const a = anchors[i];
    check('export/' + fmt + '/download-filename-from-header',
          !!a && a.download === HEADER_FILENAME,
          a ? 'download=' + JSON.stringify(a.download) : 'no anchor');
    check('export/' + fmt + '/anchor-href-is-object-url',
          !!a && !!A.urlCalls.created[i] && a.href === A.urlCalls.created[i].url,
          a ? 'href=' + JSON.stringify(a.href) : 'no anchor');
  });
  check('export/download/anchor-detached-from-body', A.doc.body.children.length === 0,
        'body.children=' + A.doc.body.children.length);
  check('export/no-alert-on-the-success-path', A.alerts.length === 0,
        JSON.stringify(A.alerts));

  // -------------------------------------------------------------------------
  console.log('--- D. label：选中的标签被带上、不选时不带 ---');
  // -------------------------------------------------------------------------
  const labelSel = A.doc.getElementById('contacts-label-filter');
  check('label/server-option-present-in-the-dropdown',
        labelSel.options.map(function (o) { return o.value; }).indexOf(SERVER_LABELS[0]) !== -1,
        JSON.stringify(labelSel.options.map(function (o) { return o.value; })));
  labelSel.value = SERVER_LABELS[0];
  const labelListeners = fire(labelSel, 'change');
  await tick(40);
  check('label/change-handler-attached', labelListeners >= 1, 'listeners=' + labelListeners);
  check('label/change-reaches-the-store',
        ev(A.ctx, 'AddressBookStore.data.labelFilter') === SERVER_LABELS[0],
        ev(A.ctx, 'AddressBookStore.data.labelFilter'));
  const withLabel = await clickExportOne(A, 'csv');
  check('label/selected-label-is-forwarded',
        paramsOf(withLabel).get('label') === SERVER_LABELS[0],
        'label=' + JSON.stringify(paramsOf(withLabel).get('label')) + ' URL=' + withLabel);
  check('label/format-still-honoured-with-a-label',
        paramsOf(withLabel).get('format') === 'csv', withLabel);
  labelSel.value = '';
  fire(labelSel, 'change');
  await tick(40);
  check('label/clearing-the-label-reaches-the-store',
        ev(A.ctx, 'AddressBookStore.data.labelFilter') === '',
        ev(A.ctx, 'AddressBookStore.data.labelFilter'));
  const noLabel = await clickExportOne(A, 'csv');
  check('label/no-label-param-when-nothing-selected', !paramsOf(noLabel).has('label'), noLabel);

  // -------------------------------------------------------------------------
  console.log('--- E. 搜索词：防抖后的 q 被带上且正确编码 ---');
  // -------------------------------------------------------------------------
  const searchEl = A.doc.getElementById('contacts-search');
  searchEl.value = '维修';
  const searchListeners = fire(searchEl, 'input');
  check('search/input-handler-attached', searchListeners >= 1, 'listeners=' + searchListeners);
  await tick(400);                       // 页面用的是 300ms 防抖
  check('search/debounced-input-reaches-the-store',
        ev(A.ctx, 'AddressBookStore.data.searchQuery') === '维修',
        JSON.stringify(ev(A.ctx, 'AddressBookStore.data.searchQuery')));
  const withQ = await clickExportOne(A, 'xlsx');
  check('search/query-is-forwarded', paramsOf(withQ).get('q') === '维修',
        'q=' + JSON.stringify(paramsOf(withQ).get('q')) + ' URL=' + withQ);
  check('search/query-is-url-encoded',
        withQ.indexOf('q=' + encodeURIComponent('维修')) !== -1, withQ);
  check('search/query-keys-within-contract',
        keysOf(withQ).every(function (k) { return CONTRACT_KEYS.indexOf(k) !== -1; }),
        'keys=' + JSON.stringify(keysOf(withQ)));

  // -------------------------------------------------------------------------
  console.log('--- F. kind：群聊页签导出的是群聊 ---');
  // -------------------------------------------------------------------------
  ev(A.ctx, 'AddressBookStore.set("activeTab", "groups")');
  await tick(60);
  check('kind/active-tab-switch-reaches-the-store',
        ev(A.ctx, 'AddressBookStore.data.activeTab') === 'groups',
        ev(A.ctx, 'AddressBookStore.data.activeTab'));
  const groupsClick = await clickExportOne(A, 'html');
  check('kind/groups-tab-sends-kind-groups', paramsOf(groupsClick).get('kind') === 'groups',
        'kind=' + JSON.stringify(paramsOf(groupsClick).get('kind')) + ' URL=' + groupsClick);

  // -------------------------------------------------------------------------
  console.log('--- G. 请求挂住：按钮进入"导出中..."且确实发出了请求 ---');
  // -------------------------------------------------------------------------
  const P = loadRealm('pending-export');
  await tick(60);
  const pBtn = P.doc.getElementById('contacts-export-btn');
  const pListeners = fire(pBtn, 'click');
  check('pending/click-handler-attached', pListeners >= 1, 'listeners=' + pListeners);
  await tick(40);
  check('pending/request-fired', exportCalls(P).length === 1,
        'calls=' + exportCalls(P).length + ' URL=' + (exportCalls(P)[0] && exportCalls(P)[0].url));
  check('pending/request-uses-the-export-endpoint',
        exportCalls(P).length === 1 &&
        exportCalls(P)[0].url.split('?')[0] === EXPORT_PATH,
        exportCalls(P)[0] && exportCalls(P)[0].url);
  check('pending/button-disabled-while-in-flight', pBtn.disabled === true,
        'disabled=' + pBtn.disabled);
  check('pending/button-text-is-busy-label', pBtn.textContent === BUSY_LABEL,
        JSON.stringify(pBtn.textContent));

  // -------------------------------------------------------------------------
  console.log('--- H. 失败路径：服务端错误显示给用户，按钮不得停在"导出中..." ---');
  // -------------------------------------------------------------------------
  const E = loadRealm('export-500');
  await tick(60);
  const eBtn = E.doc.getElementById('contacts-export-btn');
  fire(eBtn, 'click');
  await tick(60);
  check('error/request-fired', exportCalls(E).length === 1, 'calls=' + exportCalls(E).length);
  check('error/server-error-is-surfaced-to-the-user',
        E.alerts.length === 1 && E.alerts[0].indexOf('导出失败') !== -1 &&
        E.alerts[0].indexOf('synthetic export failure') !== -1,
        JSON.stringify(E.alerts));
  check('error/button-not-left-disabled',
        eBtn.disabled === false && eBtn.textContent === BUTTON_LABEL,
        'disabled=' + eBtn.disabled + ' text=' + JSON.stringify(eBtn.textContent));
  check('error/no-download-attempted-on-http-error',
        E.urlCalls.created.length === 0 && E.doc._clicks.length === 0,
        'created=' + E.urlCalls.created.length + ' clicks=' + E.doc._clicks.length);

  // -------------------------------------------------------------------------
  console.log('--- I. 反空转：整场真的观测到了请求 ---');
  // -------------------------------------------------------------------------
  const totalExport = exportCalls(A).length + exportCalls(P).length + exportCalls(E).length;
  check('guard/at-least-one-export-request-observed', totalExport >= 1,
        'total=' + totalExport);
  check('guard/export-request-count-matches-the-clicks', totalExport === 9,
        'total=' + totalExport + '（期望 9 = 3 种格式 + label 选/清 2 次 + 搜索词 1 次 + 群聊 1 次 + 挂住 1 次 + 失败 1 次）');
  check('guard/fetch-log-nonempty-across-realms',
        A.calls.length > 0 && P.calls.length > 0 && E.calls.length > 0,
        'A=' + A.calls.length + ' P=' + P.calls.length + ' E=' + E.calls.length);
  check('load/no-unhandled-rejection-at-the-end', unhandled.length === 0,
        JSON.stringify(unhandled));

  finish();
}

main().catch(function (e) {
  check('guard/crashed', false, (e && e.stack) || e);
  finish();
});
'''


if __name__ == '__main__':   # pragma: no cover - 便于手工单跑
    raise SystemExit(pytest.main([__file__, '-v']))
