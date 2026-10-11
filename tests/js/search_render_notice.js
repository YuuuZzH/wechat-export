// tests/js/search_render_notice.js
//
// T9-A7：把"页面级断言"落到**纯函数**上 —— `statusNotice` / `slowNotice` / `formatResult`。
//
// pytest 不执行 JS，也没有 JS 测试框架，所以"渲染后断 DOM"在这个项目里做不到，
// 写了也只是"只查静态 HTML 的假测试"（那正是 `TYPES` 未定义能溜过评审的原因）。
// 办法：把**判定逻辑**抽成返回字符串/普通对象的纯函数，用 Node 直接断言；
// DOM 层只负责把返回值塞进 innerHTML/textContent。
//
// 判别性要求（T9-A1 的控制方更正 + T9-A4）：
//   * `fts_coverage = 0.8615`（真机实测的正常值）**不得**产生任何提示 ——
//     若告警，每次正常安装都会永久显示"索引不完整"，反而训练用户忽略提示；
//   * 真信号（meta_coverage<1 / fts_skipped_no_chat>0 / 行数不一致 /
//     duplicates_dropped>0 / schema_ok=false / source_shards=0 / text_ready=false）**必须**产生；
//   * `scan_mode='fts'` 不得有慢查询提示；`'filter'` / `'fallback'` 必须有。
//
// 控制台可能是 GBK：PASS/FAIL 标签全用 ASCII（中文只出现在 JSON 诊断里）。
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS_DIR = path.join(__dirname, '..', '..', 'src', 'web', 'static', 'js');
const MODULE_PATH = path.join(JS_DIR, 'search-render.js');

let pass = 0, fail = 0;

function check(label, ok, extra) {
  if (ok) {
    pass++;
    console.log('  PASS  ' + label);
  } else {
    fail++;
    console.log('  FAIL  ' + label + (extra ? '\n        ' + extra : ''));
  }
}

let mod;
try {
  mod = require(MODULE_PATH);
} catch (e) {
  console.log('FAIL  cannot require ' + MODULE_PATH + ': ' + e.message);
  process.exit(1);
}

const statusNotice = mod.statusNotice;
const slowNotice = mod.slowNotice;
const truncatedPageNotice = mod.truncatedPageNotice;
const formatResult = mod.formatResult;
const escapeHtml = mod.escapeHtml;
const highlightHtml = mod.highlightHtml;

console.log('=== harness selftest ===');
check('statusNotice is a function', typeof statusNotice === 'function');
check('slowNotice is a function', typeof slowNotice === 'function');
check('truncatedPageNotice is a function', typeof truncatedPageNotice === 'function');
check('formatResult is a function', typeof formatResult === 'function');
check('escapeHtml is a function', typeof escapeHtml === 'function');
check('highlightHtml is a function', typeof highlightHtml === 'function');

// ---------------------------------------------------------------------------
// statusNotice
// ---------------------------------------------------------------------------
// 真机实测的**健康**索引（Task 4：meta_rows 101.9 万 = source_rows，
// fts_rows 87.8 万，fts_coverage 0.8615）。差额合法：图片/语音等没有正文。
const HEALTHY = {
  exists: true, ready: true, stale: false, schema_ok: true, source_shards: 7,
  source_rows: 1018918, meta_rows: 1018918, meta_coverage: 1.0,
  fts_rows: 877712, msg_text_rows: 877712, fts_coverage: 0.8615,
  fts_skipped_no_chat: 0, fts_skipped_empty_text: 141206, duplicates_dropped: 0,
  fts_rows_without_meta: 0, built_at: 1600000000, text_ready: true,
};

function withStatus(over) {
  const st = {};
  Object.keys(HEALTHY).forEach(function (k) { st[k] = HEALTHY[k]; });
  Object.keys(over || {}).forEach(function (k) { st[k] = over[k]; });
  return st;
}

const STATUS_WARN_CASES = [
  { id: 'meta_coverage < 1', st: withStatus({ meta_coverage: 0.9 }) },
  { id: 'fts_skipped_no_chat > 0', st: withStatus({ fts_skipped_no_chat: 1 }) },
  { id: 'msg_text_rows != fts_rows', st: withStatus({ msg_text_rows: 800000 }) },
  { id: 'duplicates_dropped > 0', st: withStatus({ duplicates_dropped: 3 }) },
  { id: 'schema_ok = false', st: withStatus({ schema_ok: false }) },
  { id: 'source_shards = 0', st: withStatus({ source_shards: 0 }) },
  { id: 'text_ready = false', st: withStatus({ text_ready: false }) },
  // meta-only 构建后的真实形态（Task 4）：ready 仍为 true，但关键词查询必然 0 条
  { id: 'meta-only build (fts_rows=0)', st: withStatus({
      fts_rows: 0, msg_text_rows: 0, fts_coverage: 0, text_ready: false }) },
];

const STATUS_CLEAN_CASES = [
  { id: 'healthy real-world install', st: withStatus({}) },
  // 这一条是本文件的**核心判别性**：fts_coverage<1 是合法的，不得告警
  { id: 'fts_coverage 0.8615 only (legitimate shortfall)', st: withStatus({
      fts_coverage: 0.8615, fts_skipped_empty_text: 141206, text_ready: true }) },
  { id: 'no source_rows yet (nothing to compare)', st: withStatus({
      source_rows: 0, meta_coverage: 0, meta_rows: 0, fts_rows: 0,
      msg_text_rows: 0, fts_coverage: 0, text_ready: true }) },
];

console.log('--- statusNotice: must warn ---');
let warnNonEmpty = 0;
STATUS_WARN_CASES.forEach(function (c) {
  let out = '';
  let err = '';
  try { out = statusNotice(c.st); } catch (e) { err = String(e && e.message || e); }
  const ok = err === '' && typeof out === 'string' && out.length > 0;
  if (ok) warnNonEmpty++;
  check('warns: ' + c.id, ok, err ? ('threw: ' + err) : ('notice=' + JSON.stringify(out)));
});

console.log('--- statusNotice: must stay silent ---');
let cleanEmpty = 0;
STATUS_CLEAN_CASES.forEach(function (c) {
  let out = '(threw)';
  let err = '';
  try { out = statusNotice(c.st); } catch (e) { err = String(e && e.message || e); }
  const ok = err === '' && out === '';
  if (ok) cleanEmpty++;
  check('silent: ' + c.id, ok, err ? ('threw: ' + err) : ('notice=' + JSON.stringify(out)));
});

// 非空守卫：两个方向都必须有判别力（否则"恒返回空串"或"恒返回提示"都能全过）
check('guard: suite exercises both directions (warn + silent)',
      STATUS_WARN_CASES.length > 0 && STATUS_CLEAN_CASES.length > 0);
check('guard: every warn case actually produced a notice',
      warnNonEmpty === STATUS_WARN_CASES.length,
      warnNonEmpty + '/' + STATUS_WARN_CASES.length);
check('guard: every silent case actually stayed silent',
      cleanEmpty === STATUS_CLEAN_CASES.length,
      cleanEmpty + '/' + STATUS_CLEAN_CASES.length);

// text_ready=false 的文案必须点明"关键词搜索不可用 + 要构建"，不能只说"没结果"（T9-A1）
(function () {
  const n = statusNotice(withStatus({ fts_rows: 0, msg_text_rows: 0, text_ready: false }));
  check('text index missing: notice mentions keyword search is unusable',
        n.indexOf('关键词') !== -1 && n.indexOf('构建') !== -1, 'notice=' + JSON.stringify(n));
})();

// 容错：null / 空对象 / undefined 不得抛
console.log('--- statusNotice: tolerance ---');
[[null], [undefined], [{}], [{ schema_ok: true }]].forEach(function (args, i) {
  let err = '', out = '';
  try { out = statusNotice(args[0]); } catch (e) { err = String(e && e.message || e); }
  check('tolerates partial status #' + i, err === '' && typeof out === 'string',
        err ? ('threw: ' + err) : ('notice=' + JSON.stringify(out)));
});

// ---------------------------------------------------------------------------
// slowNotice
// ---------------------------------------------------------------------------
console.log('--- slowNotice ---');

const SLOW_WARN_CASES = [
  { id: 'scan_mode=filter (full scan)', body: {
      scan_mode: 'filter', elapsed_ms: 12034, candidate_rows: 1018918,
      slow_query_hint: '该正则较复杂（无法抽取必要字面量），已跳过索引预筛并全量扫描 1018918 条消息，结果可能较慢' } },
  { id: 'scan_mode=fallback (no index)', body: {
      scan_mode: 'fallback', used_fallback: true, elapsed_ms: 10150, candidate_rows: 1018918,
      slow_query_hint: null } },
  { id: 'scan_mode=filter without hint (still slow)', body: {
      scan_mode: 'filter', elapsed_ms: 10000, candidate_rows: 1018918 } },
  // 主计划 T9-A5/后端契约：'fts' + 退化正则（未做预筛，确认阶段较慢，实测约 3.3s）
  { id: 'scan_mode=fts but engine provided slow_query_hint', body: {
      scan_mode: 'fts', elapsed_ms: 3300, candidate_rows: 264000,
      slow_query_hint: '该正则较复杂（无法抽取必要字面量），未用于预筛（本次仅按关键词预筛了 264000 条），确认阶段较慢' } },
];

const SLOW_CLEAN_CASES = [
  { id: 'scan_mode=fts (fast path)', body: {
      scan_mode: 'fts', elapsed_ms: 42, candidate_rows: 12, slow_query_hint: null } },
  { id: 'scan_mode=fts undefined hint field', body: { scan_mode: 'fts', elapsed_ms: 42 } },
];

let slowWarn = 0, slowClean = 0;
SLOW_WARN_CASES.forEach(function (c) {
  let out = '', err = '';
  try { out = slowNotice(c.body); } catch (e) { err = String(e && e.message || e); }
  const ok = err === '' && typeof out === 'string' && out.length > 0;
  if (ok) slowWarn++;
  check('warns slow: ' + c.id, ok, err ? ('threw: ' + err) : ('notice=' + JSON.stringify(out)));
});
SLOW_CLEAN_CASES.forEach(function (c) {
  let out = '(threw)', err = '';
  try { out = slowNotice(c.body); } catch (e) { err = String(e && e.message || e); }
  const ok = err === '' && out === '';
  if (ok) slowClean++;
  check('silent: ' + c.id, ok, err ? ('threw: ' + err) : ('notice=' + JSON.stringify(out)));
});

check('guard: every slow case produced a notice', slowWarn === SLOW_WARN_CASES.length,
      slowWarn + '/' + SLOW_WARN_CASES.length);
check('guard: every fast case stayed silent', slowClean === SLOW_CLEAN_CASES.length,
      slowClean + '/' + SLOW_CLEAN_CASES.length);

// 提示里要带上用户看得懂的量：已扫描条数 / 耗时（T9-A4 的 §契约）
(function () {
  const n = slowNotice(SLOW_WARN_CASES[0].body);
  check('slow notice cites candidate_rows (thousands-separated)',
        n.indexOf('1,018,918') !== -1, 'notice=' + JSON.stringify(n));
  check('slow notice cites elapsed seconds', /\d+(\.\d+)?\s*秒/.test(n),
        'notice=' + JSON.stringify(n));
})();

// 容错
console.log('--- slowNotice: tolerance ---');
[[null], [undefined], [{}]].forEach(function (args, i) {
  let err = '', out = '';
  try { out = slowNotice(args[0]); } catch (e) { err = String(e && e.message || e); }
  check('tolerates empty body #' + i, err === '' && out === '',
        err ? ('threw: ' + err) : ('notice=' + JSON.stringify(out)));
});

// ---------------------------------------------------------------------------
// truncatedPageNotice（T16）
//
// 判据是**结构化字段**（`truncated` / `retained_rows`），不是解析 warning 文案：
//   * `truncated === true` 且当页没有结果 ⇒ 这一页落在**保留窗口之外**，
//     页面**不得**显示"没有匹配的消息"（那是静默少给结果）；
//   * 其余情况一律返回 ''，仍由原有分支（没有匹配的消息 / 文本索引不可用）说话 ——
//     未截断时空串这条是最重要的**防误报**锚。
// ---------------------------------------------------------------------------
console.log('--- truncatedPageNotice ---');

const TRUNC_EMPTY = { truncated: true, retained_rows: 50000, total: 1018563,
                      page: 500, results: [] };
const TRUNC_NOT_EMPTY = { truncated: true, retained_rows: 50000, total: 1018563,
                          page: 1, results: [{ chat_id: 'wxid_alpha' }] };
const NOT_TRUNC_EMPTY = { truncated: false, retained_rows: 0, total: 0,
                          page: 1, results: [] };

(function () {
  let out = '(threw)', err = '';
  try { out = truncatedPageNotice(TRUNC_EMPTY); } catch (e) { err = String(e && e.message || e); }
  check('truncated empty page returns a notice', err === '' && typeof out === 'string' && out.length > 0,
        err ? ('threw: ' + err) : ('notice=' + JSON.stringify(out)));
  check('the notice says the page is beyond the retained window',
        String(out).indexOf('超出保留范围') !== -1, 'notice=' + JSON.stringify(out));
  check('the notice tells the user to narrow the query',
        String(out).indexOf('缩小') !== -1, 'notice=' + JSON.stringify(out));
  check('the notice cites retained_rows (thousands-separated)',
        String(out).indexOf('50,000') !== -1, 'notice=' + JSON.stringify(out));
  check('the notice never says 没有匹配的消息',
        String(out).indexOf('没有匹配的消息') === -1, 'notice=' + JSON.stringify(out));
})();

[
  { id: 'a page that still has results is not a boundary message', body: TRUNC_NOT_EMPTY },
  { id: 'an untruncated empty page keeps the original wording', body: NOT_TRUNC_EMPTY },
  { id: 'truncated missing (older engine) stays silent', body: { results: [] } },
  { id: 'truncated false even with a huge retained_rows', body: {
      truncated: false, retained_rows: 50000, total: 1018563, results: [] } },
  { id: 'truncated non-boolean (string) stays silent', body: {
      truncated: 'true', retained_rows: 50000, results: [] } },
].forEach(function (c) {
  let out = '(threw)', err = '';
  try { out = truncatedPageNotice(c.body); } catch (e) { err = String(e && e.message || e); }
  check('silent: ' + c.id, err === '' && out === '',
        err ? ('threw: ' + err) : ('notice=' + JSON.stringify(out)));
});

console.log('--- truncatedPageNotice: tolerance ---');
[[null], [undefined], [{}], ['nope'], [42]].forEach(function (args, i) {
  let err = '', out = '';
  try { out = truncatedPageNotice(args[0]); } catch (e) { err = String(e && e.message || e); }
  check('tolerates a non-object body #' + i, err === '' && out === '',
        err ? ('threw: ' + err) : ('notice=' + JSON.stringify(out)));
});

// ---------------------------------------------------------------------------
// formatResult
// ---------------------------------------------------------------------------
console.log('--- formatResult ---');

(function () {
  let err = '', r = null;
  try { r = formatResult({}); } catch (e) { err = String(e && e.message || e); }
  check('formatResult({}) does not throw', err === '' && r !== null && typeof r === 'object',
        err ? ('threw: ' + err) : '');
  check('formatResult({}) returns empty-safe defaults',
        !!r && r.chatName === '' && r.chatNameHtml === '' && r.snippet === ''
        && r.timeText === '' && r.href === ''
        && Array.isArray(r.spans) && r.spans.length === 0 && r.html === '',
        'got=' + JSON.stringify(r));
})();

(function () {
  const raw = {
    chat_id: 'wxid_alpha', chat_display_name: '华为 张', is_group: false,
    local_id: 7, create_time: 1600000000, local_type: 1, type_label: '文本',
    sender_username: 'wxid_alpha', sender_display_name: '华为 张',
    snippet: 'aaa维修bbb', match_spans: [[3, 5]],
  };
  let err = '', r = null;
  try { r = formatResult(raw); } catch (e) { err = String(e && e.message || e); }
  check('full result does not throw', err === '', err);
  if (r) {
    check('maps chat display name', r.chatName === '华为 张', 'chatName=' + JSON.stringify(r.chatName));
    check('exposes escaped chat/sender names for innerHTML',
          r.chatNameHtml === '华为 张' && r.senderNameHtml === '华为 张',
          'chatNameHtml=' + JSON.stringify(r.chatNameHtml));
    check('maps is_group flag', r.isGroup === false);
    check('maps sender display name', r.senderName === '华为 张');
    check('maps type label', r.typeLabel === '文本');
    check('maps snippet', r.snippet === 'aaa维修bbb');
    check('keeps relative spans', JSON.stringify(r.spans) === '[[3,5]]',
          'spans=' + JSON.stringify(r.spans));
    check('produces highlighted html', r.html === 'aaa<mark>维修</mark>bbb',
          'html=' + JSON.stringify(r.html));
    check('produces a non-empty time string', typeof r.timeText === 'string' && r.timeText.length > 0,
          'timeText=' + JSON.stringify(r.timeText));
    check('time string is MM-DD HH:MM:SS shaped', /^\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(r.timeText),
          'timeText=' + JSON.stringify(r.timeText));
    // Task 18（known-issues #29）：结果项同时带 local_id 与 create_time 时，href 必须
    // 带上定位参数 `/chat?open=..&focus=..&ft=..`（点进去要定位到那条消息）。
    check('produces an encoded open-chat href carrying the deep-link focus params',
          r.href === '/chat?open=wxid_alpha&focus=7&ft=1600000000',
          'href=' + JSON.stringify(r.href));
  }
})();

(function () {
  // 群聊 + 缺字段容忍
  const r = formatResult({ chat_id: '111@chatroom', is_group: true, create_time: 1600000300 });
  check('is_group detected from flag', !!r && r.isGroup === true);
  check('missing display name falls back to chat_id', !!r && r.chatName === '111@chatroom',
        'chatName=' + (r && JSON.stringify(r.chatName)));
  check('missing snippet stays empty', !!r && r.snippet === '' && r.html === '');
  check('missing spans becomes empty array', !!r && Array.isArray(r.spans) && r.spans.length === 0);
  // Task 18：定位参数**必须同时在场**才有意义（local_id 跨分片会重号，见 ADR-0012）。
  // 这里 create_time 有、local_id 没有 → 退回旧形态（宁可只开会话，也不带错行的定位）。
  check('href URL-encodes the chat id', !!r && r.href === '/chat?open=111%40chatroom',
        'href=' + (r && JSON.stringify(r.href)));
  const g = formatResult({ chat_id: '111@chatroom', is_group: true, local_id: 9,
                           create_time: 1600000300 });
  check('href carries both focus params for a group chat too',
        !!g && g.href === '/chat?open=111%40chatroom&focus=9&ft=1600000300',
        'href=' + (g && JSON.stringify(g.href)));
  const onlyLocal = formatResult({ chat_id: 'wxid_alpha', local_id: 9 });
  check('a half deep link (local_id without create_time) degrades to the old shape',
        !!onlyLocal && onlyLocal.href === '/chat?open=wxid_alpha',
        'href=' + (onlyLocal && JSON.stringify(onlyLocal.href)));
})();

(function () {
  // spans 脏数据：越界 / 逆序 / 非数字 / null / 重叠 / 非数组
  const r = formatResult({
    snippet: 'aaabbbccc', match_spans: [[5, 3], [2, 2], [0, 9999], [null], ['a', 'b'], 'x',
                                        [6, 8]],
    chat_display_name: '<script>alert(1)</script>',
  });
  check('junk spans normalized to one clamped span',
        JSON.stringify(r.spans) === '[[0,9]]', 'spans=' + JSON.stringify(r.spans));
  check('clamped span highlights the whole snippet',
        r.html === '<mark>aaabbbccc</mark>', 'html=' + JSON.stringify(r.html));
  // 渲染层直接把 `*Html` 塞 innerHTML，所以转义必须在纯函数里做完（否则就是 XSS/双转义）
  check('chatNameHtml is escaped while chatName stays raw',
        r.chatName === '<script>alert(1)</script>'
        && r.chatNameHtml === '&lt;script&gt;alert(1)&lt;/script&gt;',
        'chatNameHtml=' + JSON.stringify(r.chatNameHtml));
})();

(function () {
  // 重叠 span：取先到的、丢弃重叠的（渲染时不能出现交错的 <mark>）
  const r = formatResult({ snippet: 'aaabbbccc', match_spans: [[0, 5], [3, 8]] });
  check('overlapping spans reduced to non-overlapping set',
        JSON.stringify(r.spans) === '[[0,5]]', 'spans=' + JSON.stringify(r.spans));
  check('highlight html has exactly one mark pair',
        (r.html.match(/<mark>/g) || []).length === 1 && r.html === '<mark>aaabb</mark>bccc',
        'html=' + JSON.stringify(r.html));
})();

(function () {
  // 缺 match_spans 但有 snippet：整段不标红
  const r = formatResult({ snippet: '维修服务器', chat_display_name: 'wxid_alpha' });
  check('snippet without spans renders unhighlighted',
        r.html === '维修服务器' && r.spans.length === 0, 'html=' + JSON.stringify(r.html));
})();

console.log('--- escapeHtml / highlightHtml (pure helpers) ---');
check('escapeHtml escapes the 5 characters',
      escapeHtml('<a href="x">&\'</a>') === '&lt;a href=&quot;x&quot;&gt;&amp;&#x27;&lt;/a&gt;',
      'got=' + JSON.stringify(escapeHtml('<a href="x">&\'</a>')));
check('escapeHtml tolerates null/undefined', escapeHtml(null) === '' && escapeHtml(undefined) === '');
check('highlightHtml tolerates null spans',
      highlightHtml('abc', null) === 'abc' && highlightHtml('', [[0, 1]]) === '');
check('highlightHtml escapes the snippet outside marks',
      highlightHtml('<b>x</b>', []) === '&lt;b&gt;x&lt;/b&gt;');

// ---------------------------------------------------------------------------
// 浏览器加载路径（**经典脚本**，页面真实顺序：utils.js → search-render.js）
//
// 这一段是本文件存在的最强理由：`utils.js` 顶层已有 `const escapeHtml`，
// search-render.js 若用顶层 `function escapeHtml` 声明，浏览器实例化阶段直接
//     SyntaxError: Identifier 'escapeHtml' has already been declared
// → 整个文件不执行 → `SearchRender.*` 全 undefined：页面看着正常、功能全死。
// `require()` **测不出来**（CommonJS 把模块包在函数作用域里），只有 vm 里按
// 经典脚本加载才复现。实测过：加这一段之前，上面所有 require 断言全绿而浏览器加载抛错。
// ---------------------------------------------------------------------------
console.log('--- classic script load (browser path) ---');

function loadClassicScripts(files) {
  const sandbox = {};
  sandbox.window = sandbox;                     // 假 window：命名空间挂在它上面
  const ctx = vm.createContext(sandbox);
  files.forEach(function (f) {
    const src = fs.readFileSync(path.join(JS_DIR, f), 'utf8');
    vm.runInContext(src, ctx, { filename: f });
  });
  return ctx;
}

(function () {
  let ctx = null, err = '';
  try { ctx = loadClassicScripts(['utils.js', 'search-render.js']); }
  catch (e) { err = e.name + ': ' + e.message; }
  check('loads as a classic script after utils.js (no load-time throw)', err === '', err);
  if (!ctx) return;

  const ns = ctx.SearchRender;
  check('exposes window.SearchRender namespace', !!ns && typeof ns === 'object');
  if (!ns) return;
  ['statusNotice', 'slowNotice', 'truncatedPageNotice', 'formatResult', 'escapeHtml',
   'highlightHtml'].forEach(function (n) {
      check('window.SearchRender.' + n + ' is a function', typeof ns[n] === 'function');
    });

  // 加载我的文件**不得**动到 utils.js 的全局（命名空间而非裸全局的意义所在）
  let utilsEscape = '(threw)';
  try { utilsEscape = vm.runInContext('escapeHtml("<b>")', ctx); }
  catch (e) { utilsEscape = 'threw: ' + e.message; }
  check('utils.js global escapeHtml still works after loading search-render.js',
        utilsEscape === '&lt;b&gt;', 'got=' + JSON.stringify(utilsEscape));

  // 冒烟：走页面那条路（命名空间 + 真判定）
  check('namespace statusNotice still discriminates',
        ns.statusNotice(HEALTHY) === ''
        && ns.statusNotice(withStatus({ meta_coverage: 0.9 })).length > 0);
})();

// ---------------------------------------------------------------------------
// 跨语言**同源夹具**校验（Task 9 Phase 2 控制方追加）
//
// 判据有两份实现：
//   * JS：`src/web/static/js/search-render.js` 的 `statusNotice(status)`；
//   * 服务端：`src/web/routes/search_api.py` 的 `_index_incompleteness(status)`
//     （+ HTTP 层单独暴露的 `_text_ready`）。
// 两份是实现上的必要代价（判定必须能在 Node 里被测住），代价就是**漂移风险**：
// 有人给服务端加一个新的真信号，JS 那份不会跟着变，UI 又开始漏报或误报，
// 而两边的测试**各自都通过**。
//
// 所以两边读**同一个** JSON 夹具（`tests/fixtures/search_status_cases.json`）：
// 本段读它断言 `statusNotice`，`tests/test_search_page.py` 读它断言服务端那份。
// 任一侧改口径而没跟另一边，就会有一侧红。
//
// 两处判据**不是**同一个谓词，契约是：
//     expect_warn === (expect_incomplete || !status.text_ready)
// `_index_incompleteness` 有意不看 text_ready（那是 HTTP 层单独暴露的字段），
// 所以"meta-only 构建"那一格是 incomplete=False 但**必须**告警。
// ---------------------------------------------------------------------------
console.log('--- cross-language fixture: search_status_cases.json ---');

const FIXTURE_PATH = path.join(__dirname, '..', 'fixtures', 'search_status_cases.json');

(function () {
  let fixture = null;
  let ferr = '';
  try {
    fixture = JSON.parse(fs.readFileSync(FIXTURE_PATH, 'utf8'));
  } catch (e) {
    ferr = String((e && e.message) || e);
  }
  check('shared fixture is readable and parses', ferr === '' && !!fixture, ferr);
  if (!fixture) return;

  const cases = fixture.cases || [];
  check('shared fixture has the required discriminating cases',
        cases.length >= 6, 'cases=' + cases.length);

  // 夹具自身的一致性：所有 case 的 status 必须**同一套键**
  // （两端各写一份"哪些字段在场"的假设，键一漂移判定就会分叉）
  const keySets = {};
  cases.forEach(function (c) {
    keySets[Object.keys(c.status).sort().join(',')] = true;
  });
  check('every case carries the same status key set',
        Object.keys(keySets).length === 1, JSON.stringify(Object.keys(keySets)));

  let warnYes = 0, warnNo = 0;
  cases.forEach(function (c) {
    let out = '(threw)';
    let err = '';
    try { out = statusNotice(c.status); } catch (e) { err = String((e && e.message) || e); }
    const isWarn = err === '' && typeof out === 'string' && out.length > 0;
    if (err === '') { if (isWarn) warnYes++; else warnNo++; }

    check('fixture[' + c.name + '] warns === ' + c.expect_warn,
          err === '' && isWarn === c.expect_warn,
          err ? ('threw: ' + err) : ('notice=' + JSON.stringify(out)));

    c.notice_contains.forEach(function (needle) {
      check('fixture[' + c.name + '] notice mentions ' + JSON.stringify(needle),
            String(out).indexOf(needle) !== -1, 'notice=' + JSON.stringify(out));
    });
    (c.notice_absent || []).forEach(function (needle) {
      check('fixture[' + c.name + '] notice must NOT mention ' + JSON.stringify(needle),
            String(out).indexOf(needle) === -1, 'notice=' + JSON.stringify(out));
    });

    // 夹具编码的跨语言契约（与 pytest 侧同一条）；已知差异的 case 由夹具显式标注
    if (c.known_divergence) {
      check('fixture[' + c.name + '] documents a known divergence with a reason',
            typeof c.known_divergence === 'string' && c.known_divergence.length > 20,
            String(c.known_divergence));
    } else {
      check('fixture[' + c.name + '] satisfies warn === (incomplete || !text_ready)',
            c.expect_warn === (c.expect_incomplete === true
                               || c.status.text_ready === false),
            JSON.stringify({ warn: c.expect_warn, incomplete: c.expect_incomplete,
                             text_ready: c.status.text_ready }));
    }
  });

  // 非空/判别性守卫：不能全是"告警"或全是"不告警"，否则这套夹具什么都测不出来
  check('fixture exercises both directions (guard: not all-warn or all-silent)',
        warnYes > 0 && warnNo > 0, 'warn=' + warnYes + ' silent=' + warnNo);
  check('fixture keeps the legitimate fts_coverage shortfall silent (fts_coverage<1, meta_coverage=1)',
        cases.some(function (c) {
          return c.status.fts_coverage < 1 && c.status.meta_coverage === 1
                 && c.expect_warn === false;
        }));
  check('fixture has the meta-only case (incomplete=false but must warn)',
        cases.some(function (c) {
          return c.status.text_ready === false && c.status.fts_rows === 0
                 && c.expect_incomplete === false && c.expect_warn === true;
        }));
})();

console.log('\nRESULT: ' + pass + ' passed, ' + fail + ' failed');
process.exit(fail ? 1 : 0);
