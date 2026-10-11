"""前端纯函数的 pytest 包装（Task 9 Phase 1，控制方裁决 T9-A7）
+ Phase 2 的**经典脚本加载守卫**（页面真实加载路径）。

pytest **不执行 JS**，本仓库也没有 JS 测试框架。所以"页面级断言"不能写成
"渲染后查 DOM"——那样只能得到"只查静态 HTML 的假测试"（本项目刚发生过
`TYPES` 未定义导致整页初始化 ReferenceError、而静态页面测试照样通过）。

落地方式分两层：

1. **纯函数层（Phase 1）**：判定逻辑抽成纯函数（`src/web/static/js/search-syntax.js`、
   `src/web/static/js/search-render.js`，定义期不碰 DOM），用 Node 脚本断言，
   本文件跑 `node <script>`、断言退出码 0、且输出里没有 `FAIL`。

2. **经典脚本加载守卫（Phase 2，`PAGE_GUARD_JS`）**：在一个 **vm realm** 里按页面
   真实顺序加载**全部五个文件**（`api.js → utils.js → search-syntax.js →
   search-render.js → search-app.js`），配一个按 `search.html` 真实 id 生成的假 DOM。
   这一段是本文件存在的最强理由：经典脚本共用一个全局名字空间，顶层同名声明
   （`utils.js` 的 `const escapeHtml` vs 某个文件里的 `function escapeHtml`）会让
   **整个文件在实例化阶段抛 SyntaxError、一行都不执行**，而所有 `require()` 断言
   照样全绿 —— CommonJS 把模块包进函数作用域，撞名根本不会发生。
   顺势在同一 realm 里把页面的**判定落点**也断言掉（索引失败不得显示"索引就绪"、
   文本索引缺失时 0 条不得显示"没有匹配的消息"、慢查询提示必须渲染等）。

`node` 不存在时一律 `skip`（不要 fail）：本仓库大量测试与 JS 无关，缺 Node
不该把 CI 变红。
"""
import os
import re
import shutil
import subprocess

import pytest

# 仓库根目录从 __file__ 推导，绝不依赖 cwd
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(ROOT, 'tests', 'js')

# Node 用例脚本（一个用例文件 = 一个 pytest 用例，失败时能一眼看出是哪个）
NODE_TEST_SCRIPTS = (
    'search_syntax_roundtrip.js',   # T9-A5 表单⇄语法 往返 + 幂等
    'search_render_notice.js',      # T9-A7 提示判定 + 结果格式化
)

# Phase 2 要把这些**纯函数**接进 `search-app.js`：少一个就是加载期 ReferenceError
SYNTAX_EXPORTS = ('formToSyntax', 'syntaxToForm', 'tokenizeQuery', 'quoteIfNeeded')
RENDER_EXPORTS = ('statusNotice', 'slowNotice', 'truncatedPageNotice', 'formatResult',
                  'escapeHtml', 'highlightHtml')

# `search.html` 的脚本顺序（T7-A7 的契约）。这是**唯一**一份顺序常量：
# `tests/test_search_page.py` 断言真实页面里抽出来的顺序与它逐项相等，
# 本文件则按它把五个文件当经典脚本加载进同一个 realm。
# 一份常量、两处断言 ⇒ 守卫覆盖的文件必然就是页面加载的文件，不可能漂移。
SCRIPT_ORDER = ('api.js', 'utils.js', 'search-syntax.js', 'search-render.js',
                'search-app.js')

_RESULT_RE = re.compile(r'RESULT:\s*(\d+)\s+passed,\s*(\d+)\s+failed')


def _node_or_skip():
    """→ node 可执行文件路径；没有就 skip（缺 Node 不是本仓库的缺陷）。"""
    exe = shutil.which('node')
    if not exe:
        pytest.skip('未找到 node：跳过 JS 纯函数断言（本仓库的 Python 测试不受影响）')
    return exe


def _run_node(args, cwd):
    """跑 node 并**显式按 UTF-8 解码**。

    不能用 `text=True`：Windows 上它按区域编码（GBK）解码，而 Node 往管道里写的
    是 UTF-8，中文诊断会变成乱码（`FAIL`/`PASS` 是 ASCII 不受影响，但报告里要看
    得懂）。这里拿 bytes 自己解，读不出来的字符用 replacement 兜住，绝不因为
    输出编码而误报失败。
    """
    proc = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
    out = proc.stdout.decode('utf-8', 'replace')
    err = proc.stderr.decode('utf-8', 'replace')
    return proc.returncode, out, err


@pytest.mark.parametrize('script', NODE_TEST_SCRIPTS)
def test_node_pure_function_suite_passes(script):
    """Node 断言脚本必须退出 0、输出里没有 `FAIL`、且**真的跑了用例**。"""
    exe = _node_or_skip()
    path = os.path.join(JS_DIR, script)
    assert os.path.isfile(path), '缺少 Node 用例脚本: %s' % path

    code, out, err = _run_node([exe, path], cwd=JS_DIR)
    combined = out + err
    assert code == 0, ('node %s 退出码 %s\n--- stdout ---\n%s\n--- stderr ---\n%s'
                       % (script, code, out, err))
    assert 'FAIL' not in combined, 'node %s 输出里有 FAIL:\n%s' % (script, combined)

    # 反"空跑"：脚本必须打出 RESULT 行，且 passed > 0（用例被清空/提前 return 时能发现）
    m = _RESULT_RE.search(out)
    assert m, 'node %s 没有输出 RESULT 行，可能根本没跑用例:\n%s' % (script, out)
    passed, failed = int(m.group(1)), int(m.group(2))
    assert failed == 0, 'node %s 报告 %d 条失败:\n%s' % (script, failed, combined)
    assert passed > 0, 'node %s 一条用例都没跑:\n%s' % (script, out)


@pytest.mark.parametrize('name,exports', [
    ('search-syntax.js', SYNTAX_EXPORTS),
    ('search-render.js', RENDER_EXPORTS),
])
def test_pure_modules_load_in_node_and_export_api(name, exports):
    """两个纯函数文件必须能被 Node `require`（= 定义期不碰 DOM）并导出约定 API。

    这个断言是**行为性**的：文件若在定义期访问 `document`/`window`/`localStorage`
    或依赖别的全局，`require` 会直接抛错；Phase 2 少接一个函数也会在这里红。
    """
    exe = _node_or_skip()
    path = os.path.join(ROOT, 'src', 'web', 'static', 'js', name)
    assert os.path.isfile(path), '缺少纯函数文件: %s' % path

    program = (
        'const m = require(%r);'
        'const need = %r;'
        'const missing = need.filter(function (n) { return typeof m[n] !== "function"; });'
        'if (missing.length) { console.log("FAIL missing exports: " + missing.join(",")); '
        'process.exit(1); }'
        'console.log("PASS exports: " + need.join(","));'
    ) % (path.replace('\\', '/'), list(exports))

    code, out, err = _run_node([exe, '-e', program], cwd=JS_DIR)
    assert code == 0, ('require(%s) 失败（定义期访问了 DOM 或缺少导出）:\n%s\n%s'
                       % (name, out, err))
    assert 'FAIL' not in (out + err), out + err
    assert 'PASS exports:' in out, out


# ===========================================================================
# Phase 2：经典脚本加载守卫（浏览器真实加载路径）
#
# 这段 JS 不在仓库里单独放文件，而是随本测试一起交付 —— 它的**被测对象**是
# `SCRIPT_ORDER` 这五个文件，而那个常量就在这里，紧挨着断言，不会被误改漏改。
# ===========================================================================

PAGE_GUARD_JS = r'''// Task 9 Phase 2 —— 经典脚本加载守卫（浏览器真实加载路径）
//
// 用法: node page_guard.js <JS_DIR>
//
// 为什么必须有这段：脚本是**经典脚本**，五个文件共用同一个全局名字空间。
//   ① 顶层同名声明 → 浏览器在**实例化阶段**抛
//      SyntaxError: Identifier 'X' has already been declared → 那个文件**一行都不执行**
//      （`utils.js` 顶层的 `const escapeHtml` 就是活靶子，Phase 1 实测过）；
//   ② 引用项目里不存在的全局（计划缺陷 #15 的 `TYPES`）→ 初始化中断 →
//      "表单⇄语法同步、搜索、结果渲染、索引状态条"全死，页面只剩静态 HTML。
// 这两种形态在 `require()` 下一次都测不出来（CommonJS 把每个模块包进自己的函数
// 作用域，撞名与缺失都不复现），只有在同一个 realm 里按页面顺序加载才会。
//
// 假 DOM 的 id 取自 `search.html` 的**真实** id 集合：`getElementById` 对未知 id
// 返回 `null`（真页面就是这样），所以页面里写错一个控件 id 会在这里立刻炸出来。
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS_DIR = process.argv[2];
const TEMPLATE = path.join(JS_DIR, '..', '..', 'templates', 'search.html');

// 页面脚本顺序（与 tests/test_search_js.py 的 SCRIPT_ORDER 同源）
const SCRIPT_ORDER = ['api.js', 'utils.js', 'search-syntax.js', 'search-render.js',
                      'search-app.js'];
// 服务端渲染的 12 个规范类型名 —— 只用来让假 DOM 像真页面
const CANONICAL_TYPES = ['文本', '图片', '文件', '语音', '名片', '视频', '表情',
                         '位置', '链接', '网络电话', '系统', '撤回'];

let pass = 0, fail = 0;
const unhandled = [];
process.on('unhandledRejection', function (e) {
  unhandled.push(String((e && e.message) || e));
});

function check(label, ok, extra) {
  if (ok) { pass++; console.log('  PASS  ' + label); }
  else { fail++; console.log('  FAIL  ' + label + (extra ? '\n        ' + extra : '')); }
}

function tick(ms) {
  return new Promise(function (resolve) { setTimeout(resolve, ms || 0); });
}

// ---------------------------------------------------------------------------
// 假 DOM
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
    style: {}, dataset: {}, options: [], children: [], checked: false,
    _html: '', _listeners: {},
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
    querySelectorAll: function () { return []; },
    querySelector: function () { return null; },
    setAttribute: function () {},
    removeAttribute: function () {},
    focus: function () {},
    click: function () {},
    classList: {
      add: function () {}, remove: function () {}, toggle: function () {},
      contains: function () { return false; },
    },
  };
  Object.defineProperty(el, 'innerHTML', {
    get: function () { return el._html; },
    set: function (v) {
      el._html = (v === undefined || v === null) ? '' : String(v);
      // 页面对"自己用 innerHTML 造出来的控件"（构建按钮/进度条）随后会 getElementById，
      // 真浏览器会解析 HTML 并让它们存在 —— 这里用注册 id 的方式模拟。
      collectIds(el._html, doc._known);
    },
  });
  return el;
}

function makeDocument() {
  const known = collectIds(fs.readFileSync(TEMPLATE, 'utf8'), {});
  const cache = {};
  const doc = {
    _known: known,
    getElementById: function (id) {
      if (!known[id]) return null;      // 真页面里不存在的 id → null（暴露拼写错误）
      if (!cache[id]) cache[id] = makeElement(doc, id);
      return cache[id];
    },
    createElement: function (tag) {
      const el = makeElement(doc, '');
      el.tagName = String(tag).toUpperCase();
      return el;
    },
    addEventListener: function () {},
    querySelectorAll: function () { return []; },
  };
  const type = doc.getElementById('search-type');
  if (type) {
    type.options = CANONICAL_TYPES.map(function (n) {
      return { value: n, textContent: n, selected: false, tagName: 'OPTION' };
    });
  }
  const label = doc.getElementById('search-label');
  if (label) {
    label.options = [{ value: '', textContent: '全部标签', selected: true,
                       tagName: 'OPTION' }];
  }
  return doc;
}

// ---------------------------------------------------------------------------
// realm：把若干文件当**经典脚本**依次加载进同一个上下文
// ---------------------------------------------------------------------------

function makeFetch(mode, calls) {
  return function (url, opts) {
    const rec = { url: String(url), opts: opts || {} };
    if (calls) calls.push(rec);
    if (mode === 'reject') return Promise.reject(new Error('synthetic fetch failure'));
    return new Promise(function () {});        // 永不落定：模拟"状态查询很慢/挂了"
  };
}

function loadRealm(files, fetchImpl) {
  const sandbox = {
    console: console,
    document: makeDocument(),
    fetch: fetchImpl,
    AbortController: AbortController,
    TextDecoder: TextDecoder,
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
  };
  sandbox.window = sandbox;
  const ctx = vm.createContext(sandbox);
  const loaded = [], errors = [], deltas = [];
  files.forEach(function (f) {
    const before = Object.keys(sandbox).slice();
    try {
      vm.runInContext(fs.readFileSync(path.join(JS_DIR, f), 'utf8'), ctx,
                      { filename: f });
      loaded.push(f);
    } catch (e) {
      errors.push(f + ' -> ' + e.name + ': ' + e.message);
      return;
    }
    deltas.push({ file: f, added: Object.keys(sandbox).filter(function (k) {
      return before.indexOf(k) === -1; }) });
  });
  return { ctx: ctx, sandbox: sandbox, doc: sandbox.document, loaded: loaded,
           errors: errors, deltas: deltas };
}

async function main() {
  const ev = vm.runInContext.bind(vm);

  // =========================================================================
  console.log('--- classic script load: all five page files, one realm ---');
  // =========================================================================
  const realm = loadRealm(SCRIPT_ORDER, makeFetch('pending', null));
  const ctx = realm.ctx;
  check('all five page files load with no load-time throw',
        realm.errors.length === 0, realm.errors.join('\n        '));
  check('every file in the page order actually executed',
        realm.loaded.length === SCRIPT_ORDER.length,
        'loaded=' + JSON.stringify(realm.loaded));
  if (realm.errors.length) {
    console.log('\nRESULT: ' + pass + ' passed, ' + fail + ' failed');
    process.exit(1);
  }

  const appDelta = (realm.deltas.filter(function (d) {
    return d.file === 'search-app.js'; })[0] || {}).added || null;
  check('search-app.js adds no new global object property except SearchApp',
        appDelta !== null && appDelta.filter(function (k) {
          return k !== 'SearchApp'; }).length === 0,
        'delta=' + JSON.stringify(appDelta));

  check('utils.js global escapeHtml is intact after loading all five files',
        ev('escapeHtml("<b>")', ctx) === '&lt;b&gt;');
  check('api.js global api object is intact',
        ev('typeof api === "object" && typeof api.contacts === "function"', ctx) === true);

  // =========================================================================
  console.log('--- namespaces + client API are reachable from the page realm ---');
  // =========================================================================
  check('window.SearchSyntax namespace exists',
        ev('typeof window.SearchSyntax === "object"', ctx) === true);
  check('window.SearchRender namespace exists',
        ev('typeof window.SearchRender === "object"', ctx) === true);
  ['formToSyntax', 'syntaxToForm', 'tokenizeQuery', 'quoteIfNeeded'].forEach(
    function (n) {
      check('window.SearchSyntax.' + n + ' is a function',
            ev('typeof window.SearchSyntax.' + n, ctx) === 'function');
    });
  ['statusNotice', 'slowNotice', 'truncatedPageNotice', 'formatResult', 'escapeHtml',
   'highlightHtml']
    .forEach(function (n) {
      check('window.SearchRender.' + n + ' is a function',
            ev('typeof window.SearchRender.' + n, ctx) === 'function');
    });
  ['search', 'searchStatus', 'searchBuild'].forEach(function (n) {
    check('api.' + n + ' is a function',
          ev('typeof api === "undefined" ? "missing" : typeof api.' + n, ctx)
          === 'function');
  });
  // 页面**不得**依赖裸全局形态（Phase 1 把两个文件包成 IIFE，只暴露命名空间）
  check('no bare formToSyntax global leaked into the realm',
        ev('typeof formToSyntax', ctx) === 'undefined');
  check('no bare statusNotice global leaked into the realm',
        ev('typeof statusNotice', ctx) === 'undefined');

  // =========================================================================
  console.log('--- page initialization ran to completion ---');
  // =========================================================================
  const app = ev('window.SearchApp', ctx);
  check('window.SearchApp exists', !!app && typeof app === 'object');
  check('search-app.js finished initializing (initialized === true)',
        !!app && app.initialized === true,
        'SearchApp=' + JSON.stringify(app && Object.keys(app)));
  check('initialization did not silently bail out',
        !!app && !app.error, 'error=' + (app && String(app.error)));
  check('the page grabbed the real results container',
        !!app && app.els && app.els.results === realm.doc.getElementById('search-results'));
  if (!app || app.initialized !== true) {
    console.log('\nRESULT: ' + pass + ' passed, ' + fail + ' failed');
    process.exit(1);
  }

  // 表单 → 语法：空表单 ⇒ 空语法串（证明同步真的接上了纯函数）
  app.els.input.value = '维修';
  app.formToSyntax();
  check('form → syntax sync writes the canonical query into #search-syntax',
        app.els.syntax.value === '维修', 'syntax=' + JSON.stringify(app.els.syntax.value));
  app.els.input.value = '';
  app.formToSyntax();
  check('clearing the form clears the syntax box',
        app.els.syntax.value === '', 'syntax=' + JSON.stringify(app.els.syntax.value));

  // =========================================================================
  // T9-A1：索引状态条
  // =========================================================================
  console.log('--- T9-A1: index bar ---');
  const HEALTHY = {
    exists: true, ready: true, stale: false, schema_ok: true, source_shards: 7,
    source_rows: 1018918, meta_rows: 1018918, meta_coverage: 1.0,
    fts_rows: 877712, msg_text_rows: 877712, fts_coverage: 0.8615,
    fts_skipped_no_chat: 0, fts_skipped_empty_text: 141206, duplicates_dropped: 0,
    built_at: 1600000000, text_ready: true,
  };
  function withStatus(over) {
    const st = {};
    Object.keys(HEALTHY).forEach(function (k) { st[k] = HEALTHY[k]; });
    Object.keys(over || {}).forEach(function (k) { st[k] = over[k]; });
    return st;
  }
  function barHtml() { return String(app.els.bar.innerHTML || ''); }
  function resultsHtml() { return String(app.els.results.innerHTML || ''); }

  app.renderStatus(HEALTHY);
  check('healthy index renders 索引就绪',
        barHtml().indexOf('索引就绪') !== -1, barHtml());
  check('healthy index renders NO 索引不完整 alarm',
        barHtml().indexOf('索引不完整') === -1, barHtml());
  check('index bar is made visible', app.els.bar.style.display === 'block',
        String(app.els.bar.style.display));

  app.renderStatus(withStatus({ meta_coverage: 0.9 }));
  check('meta_coverage<1 renders the 索引不完整 alarm',
        barHtml().indexOf('索引不完整') !== -1, barHtml());
  check('meta_coverage<1 does NOT render 索引就绪',
        barHtml().indexOf('索引就绪') === -1, barHtml());

  app.renderStatus(withStatus({ fts_skipped_no_chat: 3 }));
  check('fts_skipped_no_chat>0 renders the 索引不完整 alarm',
        barHtml().indexOf('索引不完整') !== -1, barHtml());

  app.renderStatus(withStatus({ fts_skipped_empty_text: 141206 }));
  check('the legitimate fts_coverage 0.8615 shortfall stays 索引就绪',
        barHtml().indexOf('索引就绪') !== -1
        && barHtml().indexOf('索引不完整') === -1, barHtml());

  app.renderStatus(withStatus({ fts_rows: 0, msg_text_rows: 0, fts_coverage: 0,
                                text_ready: false }));
  check('meta-only build (ready=true, text_ready=false) does NOT render 索引就绪',
        barHtml().indexOf('索引就绪') === -1, barHtml());
  check('meta-only build points at the missing full-text index',
        barHtml().indexOf('全文索引') !== -1, barHtml());
  // 判别性：这一句**只**能来自页面自己的 text_ready 分支（`SearchRender.statusNotice`
  // 的通用告警里没有这个说法）。于是"把 text_ready 判据换成 ready"这类改动必定变红，
  // 而不是被通用告警顺手兜住、怎么改都绿。
  check('meta-only build uses the page\'s own text_ready wording (keyword search 静默返回 0 条)',
        barHtml().indexOf('静默返回 0 条') !== -1, barHtml());
  app.renderStatus(HEALTHY);
  check('guard: the text_ready wording is absent for a healthy index (not a constant string)',
        barHtml().indexOf('静默返回 0 条') === -1, barHtml());

  // =========================================================================
  // T9-A1：构建失败后**不得**显示"索引就绪"
  // =========================================================================
  console.log('--- T9-A1: a failed build must never look ready ---');
  app.buildCompleted();
  app.renderStatus(HEALTHY);
  check('guard: a healthy status DOES render 索引就绪 (so the failure checks are not vacuous)',
        barHtml().indexOf('索引就绪') !== -1, barHtml());

  app.buildFailed('synthetic build failure');
  check('failed build shows the failure reason',
        barHtml().indexOf('synthetic build failure') !== -1, barHtml());
  check('failed build does NOT render 索引就绪',
        barHtml().indexOf('索引就绪') === -1, barHtml());
  check('failed build offers a retry button (#search-build)',
        barHtml().indexOf('id="search-build"') !== -1, barHtml());

  app.renderStatus(HEALTHY);
  check('a later status refresh must not resurrect 索引就绪 while the failure stands',
        barHtml().indexOf('索引就绪') === -1, barHtml());
  check('the failure reason + retry still visible after a status refresh',
        barHtml().indexOf('synthetic build failure') !== -1
        && barHtml().indexOf('id="search-build"') !== -1, barHtml());

  app.buildStarted();
  check('a running build does not claim 索引就绪',
        barHtml().indexOf('索引就绪') === -1, barHtml());

  // ---- SSE 事件流（构建进度）：最容易写错、也最难在浏览器里发现的一段 ----
  app.sse('id: 1\nevent: progress\ndata: {"stage":"progress","detail":"扫描分片 3/7","progress":0.5}');
  const barEl = realm.doc.getElementById('search-build-bar');
  check('a progress event moves the progress bar',
        barEl && String(barEl.style.width) === '50%', barEl && String(barEl.style.width));
  check('a progress event shows the server detail',
        String(realm.doc.getElementById('search-build-detail').textContent).indexOf('扫描分片 3/7') !== -1,
        String(realm.doc.getElementById('search-build-detail').textContent));

  // 心跳载荷里没有 progress：绝不能被当成 0% 而把进度条打回去
  app.sse('id: 2\nevent: heartbeat\ndata: {"stage":"heartbeat"}');
  check('a heartbeat must NOT reset the progress bar',
        barEl && String(barEl.style.width) === '50%', barEl && String(barEl.style.width));

  app.sse('id: 3\nevent: error\ndata: {"stage":"error","message":"synthetic sse failure","progress":0}');
  check('an SSE error event shows the failure reason',
        barHtml().indexOf('synthetic sse failure') !== -1, barHtml());
  check('an SSE error event never leaves 索引就绪 behind',
        barHtml().indexOf('索引就绪') === -1, barHtml());

  app.buildStarted();
  app.sse('id: 4\nevent: done\ndata: {"stage":"done","detail":"","progress":1.0,"result":{}}');
  app.renderStatus(HEALTHY);
  check('after a successful build 索引就绪 becomes reachable again',
        barHtml().indexOf('索引就绪') !== -1, barHtml());

  // =========================================================================
  // T9-A1：0 条结果不得伪装成"没有这条消息"
  // =========================================================================
  console.log('--- T9-A1: 0 hits vs a missing text index ---');
  const EMPTY_BODY = {
    results: [], total: 0, page: 1, per_page: 50, total_pages: 0,
    parsed: { errors: [], warnings: [] }, warnings: [],
    used_fallback: false, regex_degraded: false, scan_mode: 'fts', elapsed_ms: 4.2,
    slow_query_hint: null, candidate_rows: 0,
    sender_filter_unsupported: false, fallback_text_only: false,
  };
  app.renderStatus(HEALTHY);
  app.renderResults(EMPTY_BODY);
  check('guard: with a healthy status, 0 hits DOES render 没有匹配的消息',
        resultsHtml().indexOf('没有匹配的消息') !== -1, resultsHtml());

  app.renderStatus(withStatus({ fts_rows: 0, msg_text_rows: 0, fts_coverage: 0,
                                text_ready: false }));
  app.renderResults(EMPTY_BODY);
  check('text index missing: 0 hits must NOT be presented as 没有匹配的消息',
        resultsHtml().indexOf('没有匹配的消息') === -1, resultsHtml());
  check('text index missing: the user is told to build the index',
        resultsHtml().indexOf('全文索引') !== -1
        && resultsHtml().indexOf('构建') !== -1, resultsHtml());
  check('text index missing: the results area uses the page\'s own text_ready wording',
        resultsHtml().indexOf('静默返回 0 条') !== -1, resultsHtml());
  // T19（R41 裁决 #5）：用户可见文案里的 `**` 会显示成**字面星号** —— 页面走
  // `escapeHtml`，它只转义 HTML 特殊字符，不会把 `**` 变成强调。所以这里断言
  // "用户真正读到的那段 HTML 里没有 `**`"，并且**先**证明这段话真的渲染了。
  check('guard: the text-index-missing copy really rendered (the asterisk check is not vacuous)',
        resultsHtml().indexOf('无法确认') !== -1, resultsHtml());
  check('no literal ** in the text-index-missing copy the user reads',
        resultsHtml().indexOf('**') === -1, resultsHtml());
  app.renderStatus(HEALTHY);
  app.renderResults(EMPTY_BODY);
  check('guard: that wording is absent when the index is healthy (not a constant string)',
        resultsHtml().indexOf('静默返回 0 条') === -1, resultsHtml());

  // =========================================================================
  // T16：显式截断 —— 空页不得伪装成"没有匹配的消息"（**禁止静默截断**）
  // =========================================================================
  console.log('--- T16: a truncated empty page is never 没有匹配的消息 ---');

  function truncatedBody(over) {
    const b = {
      results: [], total: 1018563, page: 500, per_page: 50, total_pages: 20372,
      parsed: { errors: [] },
      warnings: ['结果集过大（共 1018563 条），只保留了前 50000 条用于分页 ——'
                 + ' 请增加筛选条件（日期范围 / 会话 / 类型）缩小范围。'],
      used_fallback: false, regex_degraded: false, scan_mode: 'filter',
      elapsed_ms: 12000, slow_query_hint: null, candidate_rows: 1018918,
      sender_filter_unsupported: false, fallback_text_only: false,
      truncated: true, retained_rows: 50000,
    };
    Object.keys(over || {}).forEach(function (k) { b[k] = over[k]; });
    return b;
  }

  app.renderStatus(HEALTHY);
  const truncBody = truncatedBody({});
  app.renderResults(truncBody);
  const truncHtml = resultsHtml();
  check('a truncated empty page does NOT claim 没有匹配的消息',
        truncHtml.indexOf('没有匹配的消息') === -1, truncHtml);
  check('a truncated empty page explains the retained window',
        truncHtml.indexOf('超出保留范围') !== -1, truncHtml);
  // 期望值取 phase-1 纯函数的返回值（判据在 `SearchRender.truncatedPageNotice`）——
  // 否则这里就是"另抄一份文案"，文案改了会假红/假绿。
  const truncText = ev('window.SearchRender.truncatedPageNotice', ctx)(truncBody);
  check('guard: truncatedPageNotice() returns a non-empty string for an empty truncated page',
        typeof truncText === 'string' && truncText.length > 0, JSON.stringify(truncText));
  check('renderResults embeds exactly the string truncatedPageNotice() returned',
        truncHtml.indexOf(truncText) !== -1, truncHtml);
  check('the engine truncation warning (warnings[]) is surfaced in the results area',
        truncHtml.indexOf('结果集过大') !== -1, truncHtml);

  // 判别性守卫：同一批字段，只把 `truncated` 拨回 false ⇒ 必须回到"没有匹配的消息"分支
  app.renderResults(truncatedBody({ truncated: false, retained_rows: 0, total: 0,
                                    total_pages: 0, page: 1, warnings: [] }));
  check('guard: an untruncated empty page still says 没有匹配的消息',
        resultsHtml().indexOf('没有匹配的消息') !== -1
        && resultsHtml().indexOf('超出保留范围') === -1, resultsHtml());

  // 有结果的页（即便 truncated=true）也不该出现"超出保留范围"
  app.renderResults(truncatedBody({ results: [{ chat_id: 'wxid_alpha',
      chat_display_name: 'wxid_alpha', snippet: 'x', match_spans: [],
      create_time: 1600000000, type_label: '文本' }], page: 1 }));
  check('a truncated page that still has rows does NOT show the boundary wording',
        resultsHtml().indexOf('超出保留范围') === -1, resultsHtml());
  check('a truncated page still renders its rows',
        resultsHtml().indexOf('search-result') !== -1, resultsHtml());

  // =========================================================================
  // T19：分页条的**可翻页范围**必须压在保留窗口之内
  //
  // 契约要求 `total_pages` 按**精确 total** 算（API 不改），真实数据 `-退货` 是
  // 20,372 页，而引擎只保留前 50,000 条（= 第 1..1000 页有数据）。于是"能点进去、
  // 但必然是空页"必须由**界面**收口：下一页到 `ceil(retained_rows / per_page)` 为止。
  // 这一节同时给出两个防空转守卫：①先证明夹具**确实是截断态**；②未截断时
  // **照旧全部可点**（否则"全都不可点"也能让断言通过）。
  // =========================================================================
  console.log('--- T19: the pagination bar stops at the retained window ---');
  const RETAINED = 50000, BIG_TOTAL = 1018563, BIG_PAGES = 20372, ROW_PER_PAGE = 50;
  const LAST_IN_WINDOW = Math.ceil(RETAINED / ROW_PER_PAGE);        // 1000
  const ONE_ROW = { chat_id: 'wxid_alpha', chat_display_name: 'wxid_alpha', snippet: 'x',
                    match_spans: [], create_time: 1600000000, type_label: '文本' };
  function capBody(over) {
    const b = truncatedBody({ results: [ONE_ROW], page: 1, per_page: ROW_PER_PAGE,
                              total: BIG_TOTAL, total_pages: BIG_PAGES,
                              retained_rows: RETAINED });
    Object.keys(over || {}).forEach(function (k) { b[k] = over[k]; });
    return b;
  }
  // 分页条里的两个按钮：`‹`（前）与 `›`（后）。只解析这两种 —— 页面上没有页码列表。
  function pagButtons(html) {
    const out = [];
    const re = /<button class="pagination-btn" data-page="(-?\d+)"( disabled)?>/g;
    let m;
    while ((m = re.exec(html))) out.push({ page: parseInt(m[1], 10), disabled: !!m[2] });
    return out;
  }
  function nextBtn() { const b = pagButtons(resultsHtml()); return b.length ? b[b.length - 1] : null; }

  app.renderStatus(HEALTHY);
  const baseCap = capBody({});
  check('guard: the T19 fixture really is truncated (truncated && retained < total)',
        baseCap.truncated === true && baseCap.retained_rows < baseCap.total,
        JSON.stringify({ truncated: baseCap.truncated, retained: baseCap.retained_rows,
                         total: baseCap.total }));
  check('guard: the T19 fixture really has pages past the window (else the cap is a no-op)',
        baseCap.total_pages > Math.ceil(baseCap.retained_rows / baseCap.per_page),
        JSON.stringify({ pages: baseCap.total_pages,
                         inWindow: Math.ceil(baseCap.retained_rows / baseCap.per_page) }));

  // 窗口内的倒数第二页（999）：下一页（1000）**仍可点** —— 证明不是"一律禁用"
  app.renderResults(capBody({ page: LAST_IN_WINDOW - 1 }));
  let nb = nextBtn();
  check('inside the window (page 999/20372): 下一页 is still clickable',
        !!nb && nb.page === LAST_IN_WINDOW && nb.disabled === false, JSON.stringify(nb));

  // 窗口内最后一页（1000）：下一页指向 1001 —— 它必然是空页，必须**不可点**
  app.renderResults(capBody({ page: LAST_IN_WINDOW }));
  nb = nextBtn();
  check('the first page past the window (1001) is NOT clickable from page 1000',
        !!nb && nb.page === LAST_IN_WINDOW + 1 && nb.disabled === true, JSON.stringify(nb));
  const capHtml = resultsHtml();
  check('the cap is stated next to the bar (仅前 N 页可翻 + 命中数 + 保留数)',
        capHtml.indexOf('仅前 ' + LAST_IN_WINDOW + ' 页可翻') !== -1
        && capHtml.indexOf('共 ' + BIG_TOTAL + ' 条命中') !== -1
        && capHtml.indexOf('只保留了前 ' + RETAINED + ' 条') !== -1, capHtml);
  check('total_pages semantics are unchanged: the bar still shows 第 1000/20372 页',
        capHtml.indexOf('第 ' + LAST_IN_WINDOW + '/' + BIG_PAGES + ' 页') !== -1, capHtml);

  // 用户可能经 **URL / 浏览器后退**到达窗口之外的页（Task 16 那句"超出保留范围"就是为它写的）：
  // 那时 `‹` 必须**仍可点** —— 否则用户被卡在一个必然为空的页上，只能手改地址栏。
  app.renderResults(capBody({ page: LAST_IN_WINDOW + 500 }));
  const farBtns = pagButtons(resultsHtml());
  check('an out-of-window page reached by URL (1500) can still go BACK (‹ is clickable)',
        farBtns.length === 2 && farBtns[0].page === LAST_IN_WINDOW + 499
        && farBtns[0].disabled === false, JSON.stringify(farBtns));
  check('and its 下一页 stays disabled (no way further into the empty range)',
        farBtns.length === 2 && farBtns[1].page === LAST_IN_WINDOW + 501
        && farBtns[1].disabled === true, JSON.stringify(farBtns));

  // 上限按**响应里的** per_page 算，不是写死 50（per_page=100 ⇒ 上限 500：第 501 页不可点）
  app.renderResults(capBody({ per_page: 100, page: Math.ceil(RETAINED / 100) }));
  nb = nextBtn();
  check('the cap follows the response\'s per_page (per_page=100 -> page 501 is disabled)',
        !!nb && nb.page === 501 && nb.disabled === true, JSON.stringify(nb));

  // 防空转②：未截断时**照旧**全部可点，且**没有**任何"仅前 N 页"的标注
  app.renderResults(capBody({ truncated: false, retained_rows: 0, page: LAST_IN_WINDOW }));
  nb = nextBtn();
  check('guard: an untruncated body keeps 下一页 clickable past the window',
        !!nb && nb.page === LAST_IN_WINDOW + 1 && nb.disabled === false, JSON.stringify(nb));
  check('guard: no cap wording when the body is not truncated',
        resultsHtml().indexOf('仅前 ') === -1, resultsHtml());

  // 旧引擎（字段缺失）：行为必须与改造前**完全一致**（防误报）
  const legacyBody = capBody({ page: LAST_IN_WINDOW });
  delete legacyBody.truncated;
  delete legacyBody.retained_rows;
  app.renderResults(legacyBody);
  nb = nextBtn();
  check('guard: a legacy body without the truncation fields behaves exactly as before',
        !!nb && nb.page === LAST_IN_WINDOW + 1 && nb.disabled === false, JSON.stringify(nb));
  check('guard: a legacy body shows no cap wording either',
        resultsHtml().indexOf('仅前 ') === -1, resultsHtml());

  // 未截断时最后一页仍按精确 total 禁用（避免"永不禁用"掩盖回归）
  app.renderResults(capBody({ truncated: false, retained_rows: 0, page: BIG_PAGES }));
  nb = nextBtn();
  check('guard: on the very last page 下一页 is still disabled as before',
        !!nb && nb.page === BIG_PAGES + 1 && nb.disabled === true, JSON.stringify(nb));

  app.renderStatus(withStatus({ meta_coverage: 0.9 }));
  app.renderResults(EMPTY_BODY);
  check('incomplete index: 0 hits is qualified by the 索引不完整 warning',
        resultsHtml().indexOf('索引不完整') !== -1
        && resultsHtml().indexOf('没有匹配的消息') === -1, resultsHtml());
  check('guard: the incomplete-index copy really rendered (the asterisk check is not vacuous)',
        resultsHtml().indexOf('不能据此判断') !== -1, resultsHtml());
  check('no literal ** in the incomplete-index copy the user reads',
        resultsHtml().indexOf('**') === -1, resultsHtml());

  // =========================================================================
  // T9-A4 / 结果渲染：慢提示、转义、高亮
  // =========================================================================
  console.log('--- T9-A4 + result rendering ---');
  app.buildCompleted();
  app.renderStatus(HEALTHY);

  const slowBody = {
    results: [], total: 0, page: 1, per_page: 50, total_pages: 0,
    parsed: { errors: [] }, warnings: [],
    used_fallback: false, regex_degraded: true, scan_mode: 'filter',
    elapsed_ms: 12034, candidate_rows: 1018918,
    slow_query_hint: 'synthetic slow query hint',
    sender_filter_unsupported: false, fallback_text_only: false,
  };
  // 期望值直接取 phase-1 的纯函数返回值 —— 这样断言的是"页面把返回值渲染出来了"，
  // 而不是另一份抄过来的文案（文案改了也不会让这条断言假红/假绿）
  const slowText = ev('window.SearchRender.slowNotice', ctx)(slowBody);
  check('guard: slowNotice() returns a non-empty string for scan_mode=filter',
        typeof slowText === 'string' && slowText.length > 0, JSON.stringify(slowText));
  app.renderResults(slowBody);
  check('renderResults embeds exactly the string slowNotice() returned',
        resultsHtml().indexOf(slowText) !== -1, resultsHtml());
  const slowHtml = resultsHtml();
  app.renderResults(EMPTY_BODY);
  check('the fast path does NOT embed the slow notice',
        resultsHtml().indexOf(slowText) === -1, resultsHtml());
  check('scan_mode=filter + slow_query_hint are surfaced before the user thinks it hung',
        slowHtml.indexOf('synthetic slow query hint') !== -1, slowHtml);

  // 降级 / 假阴性信号必须出现在结果区
  const warnBody = {
    results: [], total: 0, page: 1, per_page: 50, total_pages: 0,
    parsed: { errors: [{ token: '类型:彩虹', message: 'synthetic parse error' }] },
    warnings: ['synthetic engine warning'],
    used_fallback: true, regex_degraded: false, scan_mode: 'fallback',
    elapsed_ms: 10150, slow_query_hint: null, candidate_rows: 1018918,
    sender_filter_unsupported: true, fallback_text_only: true,
  };
  app.renderResults(warnBody);
  const warnHtml = resultsHtml();
  check('parsed.errors are surfaced', warnHtml.indexOf('synthetic parse error') !== -1, warnHtml);
  check('engine warnings are surfaced', warnHtml.indexOf('synthetic engine warning') !== -1, warnHtml);
  check('used_fallback is surfaced', warnHtml.indexOf('降级') !== -1, warnHtml);
  check('sender_filter_unsupported is surfaced',
        warnHtml.indexOf('发送者') !== -1, warnHtml);
  check('fallback_text_only is surfaced',
        warnHtml.indexOf('正文') !== -1, warnHtml);
  // T19：这几条 fallback 提示是**用户可见**文案，里面的 `**` 会显示成字面星号。
  check('guard: the fallback notes really rendered their own wording',
        warnHtml.indexOf('降级扫描只看得到有正文的消息') !== -1
        && warnHtml.indexOf('降级路径不支持「发送者」筛选') !== -1, warnHtml);
  check('no literal ** in the fallback notes the user reads',
        warnHtml.indexOf('**') === -1, warnHtml);

  // 转义：消息正文/显示名一律不得作为 HTML 注入
  const snip = '<img src=x>维修';
  const hitBody = {
    results: [{
      chat_id: 'wxid_alpha', chat_display_name: '<b>x</b>', is_group: false,
      create_time: 1600000000, type_label: '文本', sender_display_name: '华为 张',
      snippet: snip, match_spans: [[snip.indexOf('维修'), snip.indexOf('维修') + 2]],
    }],
    total: 1, page: 1, per_page: 50, total_pages: 1,
    parsed: { errors: [] }, warnings: [], used_fallback: false, regex_degraded: false,
    scan_mode: 'fts', elapsed_ms: 12, slow_query_hint: null, candidate_rows: 1,
    sender_filter_unsupported: false, fallback_text_only: false,
  };
  app.renderResults(hitBody);
  const hitHtml = resultsHtml();
  check('message text is escaped, never injected as HTML',
        hitHtml.indexOf('<img') === -1 && hitHtml.indexOf('&lt;img src=x&gt;') !== -1,
        hitHtml);
  check('snippet is highlighted with <mark> at the matched span',
        hitHtml.indexOf('&lt;img src=x&gt;<mark>维修</mark>') !== -1, hitHtml);
  check('chat display name is escaped too',
        hitHtml.indexOf('<b>x</b>') === -1
        && hitHtml.indexOf('&lt;b&gt;x&lt;/b&gt;') !== -1, hitHtml);
  check('sender display name + type label are rendered',
        hitHtml.indexOf('华为 张') !== -1 && hitHtml.indexOf('文本') !== -1, hitHtml);
  check('a hit offers a link to open the chat',
        hitHtml.indexOf('/chat?open=wxid_alpha') !== -1, hitHtml);

  // =========================================================================
  // T9-A2：状态端点失败/挂住不得阻塞输入与搜索
  // =========================================================================
  console.log('--- T9-A2: a failing status endpoint must not block the page ---');
  const badRealm = loadRealm(SCRIPT_ORDER, makeFetch('reject', null));
  check('page still loads when every request fails',
        badRealm.errors.length === 0, badRealm.errors.join('\n        '));
  const badApp = ev('window.SearchApp', badRealm.ctx);
  check('page still initialized when every request fails',
        !!badApp && badApp.initialized === true,
        'SearchApp=' + JSON.stringify(badApp && Object.keys(badApp)));
  await tick(20);
  await tick(20);
  check('no unhandled promise rejection escaped the page code',
        unhandled.length === 0, JSON.stringify(unhandled));
  if (badApp) {
    check('the index bar stays hidden when the status request fails',
          String(badApp.els.bar.style.display) !== 'block',
          String(badApp.els.bar.style.display));
    badApp.els.syntax.value = '维修';
    badApp.search(1);
    check('search is still wired and shows progress immediately',
          String(badApp.els.results.innerHTML).length > 0,
          String(badApp.els.results.innerHTML));
    await tick(20);
    check('a failed search surfaces an error instead of throwing',
          String(badApp.els.results.innerHTML).indexOf('synthetic fetch failure') !== -1,
          String(badApp.els.results.innerHTML));
  }

  // =========================================================================
  // T9-A2 + api.js 契约：searchStatus() 不得取消正在进行的搜索
  // =========================================================================
  console.log('--- T9-A2: searchStatus() is independent of the search request ---');
  const apiCalls = [];
  const apiRealm = loadRealm(['api.js'], makeFetch('pending', apiCalls));
  check('api.js loads standalone', apiRealm.errors.length === 0,
        apiRealm.errors.join('\n        '));
  const api = ev('api', apiRealm.ctx);
  check('api object exists in the api realm', !!api && typeof api === 'object');

  api.search('维修', { page: 2, per_page: 50 });
  check('api.search() builds the documented URL',
        apiCalls[0] && apiCalls[0].url
        === '/api/search?q=' + encodeURIComponent('维修') + '&page=2&per_page=50',
        apiCalls[0] && apiCalls[0].url);
  const searchSignal = apiCalls[0].opts.signal;
  check('api.search() passes an abort signal', !!searchSignal
        && typeof searchSignal.aborted === 'boolean');

  api.searchStatus();
  check('api.searchStatus() hits /api/search/status',
        apiCalls[1] && apiCalls[1].url === '/api/search/status',
        apiCalls[1] && apiCalls[1].url);
  check('api.searchStatus() does NOT cancel the in-flight search',
        searchSignal.aborted === false,
        'search signal aborted=' + searchSignal.aborted);
  check('api.searchStatus() uses its own signal (never cancelPending())',
        apiCalls[1].opts.signal !== searchSignal);

  api.search('报修');
  check('a new search still cancels the previous one (cancelPending preserved)',
        searchSignal.aborted === true, 'aborted=' + searchSignal.aborted);

  api.searchBuild({ action: 'refresh' });
  const buildCall = apiCalls[apiCalls.length - 1];
  check('api.searchBuild() POSTs JSON to /api/search/index',
        buildCall.url === '/api/search/index' && buildCall.opts.method === 'POST'
        && buildCall.opts.body === JSON.stringify({ action: 'refresh' })
        && String(buildCall.opts.headers['Content-Type']).indexOf('application/json') !== -1,
        JSON.stringify({ url: buildCall.url, method: buildCall.opts.method }));
  check('api.searchBuild() sends no abort signal (a build must not be cancelled)',
        buildCall.opts.signal === undefined, String(buildCall.opts.signal));

  api.search('x', { page: 1, per_page: '', sort: null, junk: undefined });
  check('empty / null / undefined params are dropped from the query string',
        apiCalls[apiCalls.length - 1].url === '/api/search?q=x&page=1',
        apiCalls[apiCalls.length - 1].url);
  check('the api realm really did observe every call (guard: not an empty log)',
        apiCalls.length === 5, 'calls=' + apiCalls.length);

  console.log('\nRESULT: ' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail ? 1 : 0);
}

main().catch(function (e) {
  console.log('FAIL  page guard crashed: ' + ((e && e.stack) || e));
  console.log('\nRESULT: ' + pass + ' passed, ' + (fail + 1) + ' failed');
  process.exit(1);
});
'''


def test_page_files_load_as_classic_scripts_in_one_realm(tmp_path):
    """五个页面文件必须能在**同一个 realm** 里按页面顺序当经典脚本加载成功。

    这是 `escapeHtml` 那一类"顶层撞名 → 整文件不执行 → 页面看着正常、功能全死"
    缺陷的唯一可观测点：`require()` 永远测不出来（CommonJS 各自一个作用域）。
    同一个 vm realm 里加载全部五个文件，任何顶层同名声明都会在**实例化阶段**抛
    `SyntaxError`，`search-app.js` 引用不存在的全局（#15 的 `TYPES`）也会立刻抛。

    同一个 realm 里还顺势断言了页面的**判定落点**（T9-A1 索引失败/文本索引缺失、
    T9-A4 慢查询提示、结果转义与高亮、T9-A2 状态端点失败不阻塞页面与搜索、
    `api.searchStatus()` 不得取消正在进行的搜索）。
    """
    exe = _node_or_skip()

    script = tmp_path / 'page_load_guard.js'
    script.write_text(PAGE_GUARD_JS, encoding='utf-8')

    js_dir = os.path.join(ROOT, 'src', 'web', 'static', 'js')
    for name in SCRIPT_ORDER:
        assert os.path.isfile(os.path.join(js_dir, name)), \
            '页面加载了 %s，但文件不存在（浏览器里就是一个 404 + 后续 ReferenceError）' % name

    code, out, err = _run_node([exe, str(script), js_dir], cwd=ROOT)
    combined = out + err
    assert code == 0, ('经典脚本加载守卫失败（页面在浏览器里就是坏的）:\n'
                       '--- stdout ---\n%s\n--- stderr ---\n%s' % (out, err))
    assert 'FAIL' not in combined, '加载守卫输出里有 FAIL:\n%s' % combined

    m = _RESULT_RE.search(out)
    assert m, '加载守卫没有输出 RESULT 行，可能根本没跑用例:\n%s' % out
    passed, failed = int(m.group(1)), int(m.group(2))
    assert failed == 0, '加载守卫报告 %d 条失败:\n%s' % (failed, combined)
    assert passed > 0, '加载守卫一条用例都没跑:\n%s' % out


def test_script_order_constant_covers_the_five_page_files():
    """`SCRIPT_ORDER` 就是这个页面契约（顺序 + 文件集合），不许被顺手改短。

    它同时驱动上面的加载守卫和 `tests/test_search_page.py` 里"页面真实顺序 ==
    SCRIPT_ORDER"的断言；改短了（比如漏掉 `search-app.js`）守卫就会变成
    一条什么都守不住的装饰。
    """
    assert SCRIPT_ORDER == ('api.js', 'utils.js', 'search-syntax.js',
                            'search-render.js', 'search-app.js'), SCRIPT_ORDER
    assert SCRIPT_ORDER[0] == 'api.js'
    assert SCRIPT_ORDER[1] == 'utils.js'
    assert SCRIPT_ORDER[-1] == 'search-app.js'
    js_dir = os.path.join(ROOT, 'src', 'web', 'static', 'js')
    for name in SCRIPT_ORDER:
        assert os.path.isfile(os.path.join(js_dir, name)), name

