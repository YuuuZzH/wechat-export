/* dbdir_picker_guard.js — Task 21 前端守卫
 *
 * 被测对象（**浏览器真实加载路径**，不是静态 HTML 文本）：
 *   ① `templates/keyscan.html`（/keyscan，「提取密钥」）与 `templates/settings.html`
 *      （/settings，「设置」）两页的选择器装配 —— 脚本按**文档顺序**当经典脚本
 *      加载进同一个 vm realm，缺 `#cfg-db-dir` 之类的 id 会立刻炸出来；
 *   ② `dbdir.js::DbDirPicker` 的**推荐排序 / ⭐ 标注 / 默认选中推荐项**渲染；
 *   ③ `keyscan.html` 两种提取（内存扫描 + Hook）的请求 body 里**真的带上了**
 *      `db_dir`（空输入则不带这个键，保持后端"自动探测第一个"的回落行为）。
 *
 * 用法:
 *   node dbdir_picker_guard.js                  # 正常守卫（应当 0 failed）
 *   node dbdir_picker_guard.js ignore-recommended   # 变异①：无视 recommended_path
 *   node dbdir_picker_guard.js drop-reason          # 变异②：把 reason 丢掉只显示路径
 *   node dbdir_picker_guard.js dump-legacy          # 打印旧格式响应的可观测状态（离线比对用）
 *   node dbdir_picker_guard.js real-payload <file>  # 用**真实** /api/keys/dirs 响应跑一遍，
 *                                                   # 只打印布尔/计数与掩码后的文本（供真机核对）
 *
 * 环境变量:
 *   DBDIR_SRC=<path>   用别的 dbdir.js 源文件跑（只用于"改造前 vs 改造后"的离线比对；
 *                      默认就是仓库里的 `src/web/static/js/dbdir.js`）。
 *
 * 环境变量（只用于"改造前 vs 改造后"的离线比对与 RED 复现，正常回归不需要）:
 *   DBDIR_SRC=<path>       用别的 dbdir.js 源文件跑
 *   KEYSCAN_TPL=<path>     用别的 keyscan.html 跑
 *   SETTINGS_TPL=<path>    用别的 settings.html 跑
 *
 * ⚠ **边界（不得含糊）**：本仓库没有 Playwright/Selenium，所以这里**没有**在真浏览器里
 * 验证过任何像素/布局行为。假 DOM 只实现两页用得到的那部分（`getElementById` /
 * `createElement` / `appendChild` / `innerHTML` / `textContent` / `value` / `options` /
 * `selected` / `classList` / `style` / `addEventListener`），`innerHTML` 不做真正的
 * HTML 解析（层级不可信、顺序可信）。**唯一**按浏览器语义实现的关键点是
 * `<select>.value`：setter 会把匹配的 option 标为 selected、getter 读回被选中的
 * option 的值（`fill()` 选中推荐项这件事因此是可观测的，而不是"看起来设置了"）。
 * `querySelectorAll` 只支持本守卫显式提供的两组选择器（`.hook-step` / `.step-item`），
 * 其余一律返回空数组；`querySelector` 一律返回 null。
 *
 * 全部夹具都是**合成**的：绝不读真实微信数据、不连真实 `/api`。
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..', '..');
const JS_DIR = path.join(ROOT, 'src', 'web', 'static', 'js');
const TPL_DIR = path.join(ROOT, 'src', 'web', 'templates');
const DBDIR_SRC = process.env.DBDIR_SRC
  ? process.env.DBDIR_SRC
  : path.join(JS_DIR, 'dbdir.js');

const KEYSCAN_TPL = process.env.KEYSCAN_TPL
  ? process.env.KEYSCAN_TPL
  : path.join(TPL_DIR, 'keyscan.html');
const SETTINGS_TPL = process.env.SETTINGS_TPL
  ? process.env.SETTINGS_TPL
  : path.join(TPL_DIR, 'settings.html');
const BACKUP_TPL = path.join(TPL_DIR, 'backup.html');

// 与 `backup.html` 同形的 5 个控件（id 由 `DbDirPicker` 的默认值给定）
const PICKER_IDS = ['cfg-db-dir', 'cfg-db-dir-list', 'btn-deep', 'btn-use-dir',
                    'dir-hint'];
// 用户原话里那句逐字标签
const HINT_LABEL = '检测到的数据目录（选择即填入上面的输入框）';
// keyscan 两种提取的 SSE 端点
const KEYSCAN_URL = '/api/backup/keyscan';
const HOOK_URL = '/api/backup/hook-keyscan';

// ---------------------------------------------------------------------------
// 合成夹具
//
// **顺序即后端给出的排序**（in_use > locked > recent > config > idle），而且故意让
// "推荐项不是第一条、也不是 current" —— 否则"无视 recommended_path 永远选第一条"
// 这个变异根本不可能变红（夹具不具判别力，断言就是装饰）。
// ---------------------------------------------------------------------------
const DIR_RECENT = {
  db_path: 'D:\\synthetic_root\\acct_recent\\db_storage',
  wxid: 'wxid_synthetic_recent',
  mtime: 1599990000, db_count: 7, size_mb: 512.2,
  tier: 'recent', reason: '最近活跃（12 分钟前有写入）',
  recommended: false, pids: [], active: false, last_write_min: 12,
};
const DIR_IN_USE = {
  db_path: 'D:\\synthetic_root\\acct_inuse\\db_storage',
  wxid: 'wxid_synthetic_inuse',
  mtime: 1600000000, db_count: 9, size_mb: 1945.4,
  tier: 'in_use', reason: '⭐ 微信进程正在使用（PID 1234）',
  recommended: true, pids: [1234], active: true, last_write_min: 28.8,
};
const DIR_IDLE = {
  db_path: 'D:\\synthetic_root\\acct_idle\\db_storage',
  wxid: 'wxid_synthetic_idle',
  mtime: 1500000000, db_count: 3, size_mb: 88.5,
  tier: 'idle', reason: '未发现活动迹象',
  recommended: false, pids: [], active: false, last_write_min: null,
};

const RECOMMENDED = DIR_IN_USE.db_path;
const CURRENT = DIR_RECENT.db_path;      // 配置指向的**不是**推荐项

function newPayload(over) {
  const p = {
    dirs: [DIR_RECENT, DIR_IN_USE, DIR_IDLE],
    current: CURRENT,
    recommended_path: RECOMMENDED,
    wechat_running: true,
    probe: { t0_ok: true, t0_elapsed_ms: 400, t1_probed: 3, errors: [] },
    mode: 'auto',
  };
  Object.keys(over || {}).forEach(function (k) { p[k] = over[k]; });
  return p;
}

// 旧引擎（改造前）的响应：**没有** recommended_path / tier / reason / probe /
// wechat_running —— 前端必须逐字保持改造前的行为
const LEGACY_DIRS = [
  { db_path: 'D:\\synthetic_root\\acct_recent\\db_storage',
    wxid: 'wxid_synthetic_recent', mtime: 1599990000, db_count: 7, size_mb: 512.2 },
  { db_path: 'D:\\synthetic_root\\acct_inuse\\db_storage',
    wxid: 'wxid_synthetic_inuse', mtime: 1600000000, db_count: 9, size_mb: 1945.4 },
];
const LEGACY_CURRENT = LEGACY_DIRS[1].db_path;   // 旧口径：current 优先于第一条

function legacyPayload() {
  return { dirs: LEGACY_DIRS, current: LEGACY_CURRENT, mode: 'auto' };
}

// 旧口径的 option 文本（改造前 fill() 的公式，逐字照抄）：
//   d.db_path + (d.wxid ? " (" + d.wxid + ")" : "") + (d.size_mb ? "  " + d.size_mb + " MB" : "")
function legacyOptionText(d) {
  const wxid = d.wxid ? ' (' + d.wxid + ')' : '';
  const size = d.size_mb ? '  ' + d.size_mb + ' MB' : '';
  return d.db_path + wxid + size;
}

// ---------------------------------------------------------------------------
// 假 DOM
// ---------------------------------------------------------------------------
const ID_RE = /\bid="([A-Za-z0-9_\-]+)"/g;

function collectIds(html, into) {
  ID_RE.lastIndex = 0;
  let m;
  while ((m = ID_RE.exec(html))) into[m[1]] = true;
  return into;
}

function escapeText(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                  .replace(/>/g, '&gt;');
}

function makeEl(doc, id, tag) {
  const el = {
    id: id || '', tagName: String(tag || 'div').toUpperCase(),
    disabled: false, checked: false, selected: false,
    // 真 DOM 里任何 input 的 `.value` 都是字符串（空输入是 `''`，不是 undefined）——
    // 少了这一行，页面里的 `input.value.trim()` 会在假 DOM 上假炸。
    value: '',
    style: {}, dataset: {}, children: [], options: [],
    _text: '', _html: '', _htmlSet: false, _listeners: {},
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
    setAttribute: function () {},
    removeAttribute: function () {},
    focus: function () {},
    click: function () {},
    querySelector: function () { return null; },
    querySelectorAll: function () { return []; },
    scrollIntoView: function () {},
    classList: {
      add: function () {}, remove: function () {}, toggle: function () {},
      contains: function () { return false; },
    },
  };
  Object.defineProperty(el, 'textContent', {
    get: function () { return el._text; },
    set: function (v) {
      el._text = (v === undefined || v === null) ? '' : String(v);
      el._htmlSet = false;
    },
  });
  Object.defineProperty(el, 'innerHTML', {
    get: function () { return el._htmlSet ? el._html : escapeText(el._text); },
    set: function (v) {
      el._html = (v === undefined || v === null) ? '' : String(v);
      el._htmlSet = true;
      // `sel.innerHTML = ""` 在真浏览器里会清空子节点 —— 不清的话 `options` 会累积，
      // "顺序/条数"断言就全是假的。
      el.children = [];
      el.options = [];
      if (doc && doc._known) collectIds(el._html, doc._known);
    },
  });
  return el;
}

// `<select>.value` —— **按浏览器语义**实现（本文件最重要的一个假件）：
// setter 把匹配的 option 标记为 selected，getter 读回被选中的 option 的值；
// 没有任何 option 匹配时 value 变成 ''（真浏览器就是这样，旧代码依赖这个行为）。
function makeSelect(doc, id) {
  const el = makeEl(doc, id, 'select');
  Object.defineProperty(el, 'value', {
    get: function () {
      const hit = el.options.filter(function (o) { return o.selected; })[0];
      return hit ? String(hit.value) : '';
    },
    set: function (v) {
      const want = (v === undefined || v === null) ? '' : String(v);
      let hit = false;
      el.options.forEach(function (o) {
        o.selected = String(o.value) === want;
        if (o.selected) hit = true;
      });
      el._lastValue = hit ? want : '';
    },
  });
  return el;
}

function makeDoc(html) {
  const known = collectIds(html, {});
  const cache = {};
  const doc = {
    _known: known,
    getElementById: function (id) {
      if (!known[id]) return null;   // 真页面里不存在的 id → null（暴露拼写错误）
      if (!cache[id]) cache[id] = makeEl(doc, id, 'div');
      return cache[id];
    },
    createElement: function (tag) { return makeEl(doc, '', tag); },
    addEventListener: function () {},
    querySelector: function () { return null; },
    querySelectorAll: function () { return []; },
  };
  // dir-hint 的**页面静态文案**：旧响应下 `load()` 一个字都不许动它，
  // 所以这里按模板原样种进去，断言才不是拿空气比空气。
  const hint = cachedHint(html);
  if (hint !== null && known['dir-hint']) {
    cache['dir-hint'] = makeEl(doc, 'dir-hint', 'div');
    cache['dir-hint']._html = hint;
    cache['dir-hint']._htmlSet = true;
  }
  // 选择器控件按 SELECT 语义造
  if (known['cfg-db-dir-list']) {
    cache['cfg-db-dir-list'] = makeSelect(doc, 'cfg-db-dir-list');
  }
  // keyscan 的 Hook 步骤条：`document.querySelectorAll('.hook-step')`，每个节点
  // 再 `querySelector('.step-dot')`。步名从模板的 data-step 属性里取，不另抄一份。
  const stepNames = [];
  const stepRe = /data-step="([a-z_]+)"/g;
  let sm;
  while ((sm = stepRe.exec(html))) stepNames.push(sm[1]);
  doc.querySelectorAll = function (sel) {
    if (sel === '.hook-step') {
      return stepNames.map(function (name) {
        const node = makeEl(doc, '', 'div');
        node.dataset.step = name;
        node.querySelector = function (s) {
          return s === '.step-dot' ? makeEl(doc, '', 'span') : null;
        };
        return node;
      });
    }
    return [];
  };
  return doc;
}

function cachedHint(html) {
  const m = /id="dir-hint"[^>]*>([\s\S]*?)<\/div>/.exec(html);
  return m ? m[1] : null;
}

// ---------------------------------------------------------------------------
// 脚本顺序（**文档顺序**，外部脚本与内联脚本混在一起按出现次序）
// ---------------------------------------------------------------------------
const SCRIPT_RE = /<script([^>]*)>([\s\S]*?)<\/script>/g;
const SRC_RE = /url_for\('static',\s*filename='js\/([^']+)'\)/;

function scriptEntries(html) {
  const out = [];
  SCRIPT_RE.lastIndex = 0;
  let m;
  while ((m = SCRIPT_RE.exec(html))) {
    const attrs = m[1] || '';
    const src = SRC_RE.exec(attrs);
    if (src) out.push({ kind: 'file', name: src[1], raw: m[0] });
    else out.push({ kind: 'inline', name: '(inline)', raw: m[0], code: m[2] });
  }
  return out;
}

// ---------------------------------------------------------------------------
// 变异（只在内存里改源码，绝不落盘）
// ---------------------------------------------------------------------------
const MUTATIONS = ['ignore-recommended', 'drop-reason'];
let mutationApplied = false;

function mutateSource(file, src, mode) {
  if (!mode || file !== 'dbdir.js') return src;
  const before = src;
  if (mode === 'ignore-recommended') {
    // 变异①：无视 recommended_path，永远选 current / 第一条（别的逻辑一字不动）
    src = src.replace(
      /const chosen = \(inp && inp\.value\) \|\| recommended \|\| current \|\| first;/,
      'const chosen = (inp && inp.value) || current || first;');
  } else if (mode === 'drop-reason') {
    // 变异②：把 reason 丢掉，只显示路径（⭐ 标注也随之消失）
    src = src.replace(/const reason = d && d\.reason \? String\(d\.reason\) : "";/,
                      'const reason = "";');
  } else {
    return src;
  }
  if (src === before) {
    console.log('FAIL  mutation "' + mode + '" did not apply to ' + file
                + ' (anchor line not found)');
    process.exit(1);
  }
  mutationApplied = true;
  return src;
}

function readJs(file, mode) {
  const src = file === 'dbdir.js'
    ? fs.readFileSync(DBDIR_SRC, 'utf8')
    : fs.readFileSync(path.join(JS_DIR, file), 'utf8');
  return mutateSource(file, src, mode);
}

// ---------------------------------------------------------------------------
// 假 fetch
// ---------------------------------------------------------------------------
function makeFetch(recorder, dirsPayload) {
  return function (url, opts) {
    const u = String(url);
    recorder.push({ url: u, opts: opts || {} });
    if (u.indexOf(KEYSCAN_URL) === 0 || u.indexOf(HOOK_URL) === 0) {
      return new Promise(function () {});    // SSE：永不落定，只记录请求体
    }
    let body = {};
    if (u.indexOf('/api/keys/dirs') === 0) body = dirsPayload;
    else if (u.indexOf('/api/settings/asr') === 0) {
      body = { engine: 'local', language: 'zh', simplify: false, devPids: [],
               local_model: 'synthetic-model', localModelStatus: { available: [],
               complete: true, missing: [] } };
    } else if (u.indexOf('/api/keys/dbdir') === 0) {
      body = { dbDir: 'D:\\synthetic_root\\acct_inuse\\db_storage' };
    }
    return Promise.resolve({
      ok: true, status: 200,
      json: function () { return Promise.resolve(body); },
    });
  };
}

// ---------------------------------------------------------------------------
// realm
// ---------------------------------------------------------------------------
function boot(templatePath, dirsPayload, opts) {
  const o = opts || {};
  const html = fs.readFileSync(templatePath, 'utf8');
  const doc = makeDoc(html);
  const calls = [];
  const alerts = [];
  const sandbox = {
    console: console,
    document: doc,
    fetch: makeFetch(calls, dirsPayload),
    AbortController: AbortController,
    TextDecoder: TextDecoder,
    Promise: Promise,
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    alert: function (msg) { alerts.push(String(msg)); },
  };
  sandbox.window = sandbox;
  const ctx = vm.createContext(sandbox);
  const errors = [];
  const loaded = [];
  const entries = scriptEntries(html);
  entries.forEach(function (e) {
    try {
      const code = e.kind === 'file' ? readJs(e.name, o.mutation)
                                     : e.code;
      vm.runInContext(code, ctx, {
        filename: e.kind === 'file' ? e.name : path.basename(templatePath) + ':inline',
      });
      loaded.push(e.kind === 'file' ? e.name : '(inline)');
    } catch (err) {
      errors.push((e.kind === 'file' ? e.name : '(inline)') + ' -> '
                  + err.name + ': ' + err.message);
    }
  });
  return {
    ctx: ctx, doc: doc, calls: calls, alerts: alerts, errors: errors,
    loaded: loaded, entries: entries,
    ev: function (expr) { return vm.runInContext(expr, ctx); },
  };
}

// ---------------------------------------------------------------------------
// harness
// ---------------------------------------------------------------------------
let pass = 0, fail = 0;
const results = {};
const unhandled = [];
process.on('unhandledRejection', function (e) {
  unhandled.push(String((e && e.message) || e));
});

function check(id, ok, extra) {
  results[String(id).split(' ')[0]] = !!ok;
  if (ok) { pass++; console.log('  PASS  ' + id); }
  else {
    fail++;
    console.log('  FAIL  ' + id + (extra ? '\n        ' + extra : ''));
  }
}

function tick(ms) { return new Promise(function (r) { setTimeout(r, ms || 0); }); }
async function settle(n) { for (let i = 0; i < (n || 20); i++) await tick(2); }

function optionTexts(doc) {
  const sel = doc.getElementById('cfg-db-dir-list');
  return sel ? sel.options.map(function (o) { return String(o.textContent); }) : [];
}

function optionValues(doc) {
  const sel = doc.getElementById('cfg-db-dir-list');
  return sel ? sel.options.map(function (o) { return String(o.value); }) : [];
}

function optionCount(doc) {
  return optionValues(doc).length;
}

function selectedValue(doc) {
  const sel = doc.getElementById('cfg-db-dir-list');
  return sel ? String(sel.value) : '';
}

function inputValue(doc) {
  const inp = doc.getElementById('cfg-db-dir');
  return inp ? String(inp.value) : '';
}

function hintHtml(doc) {
  const h = doc.getElementById('dir-hint');
  return h ? String(h.innerHTML) : '';
}

function bodyOf(calls, url) {
  const hit = calls.filter(function (c) { return c.url.indexOf(url) === 0; }).pop();
  if (!hit) return null;
  const raw = hit.opts && hit.opts.body;
  if (!raw) return null;
  try { return JSON.parse(raw); } catch (e) { return { __unparsable: String(raw) }; }
}

function fire(el, event) {
  if (!el) return;
  (el._listeners[event] || []).forEach(function (f) { f.call(el, { type: event }); });
}

// 取值但**不**因为"全局不存在"把整个守卫炸掉：RED 状态下（选择器还没实现）
// 我们要看到一条条 FAIL，而不是一个 ReferenceError 让它变成"跑不完"。
function evSafe(realm, expr, fallback) {
  try {
    const v = realm.ev(expr);
    return v === undefined ? fallback : v;
  } catch (e) {
    return fallback;
  }
}

function getBodyOf(realm) {
  const body = evSafe(realm, 'memoryPage.getBody()', null);
  if (!body || typeof body !== 'object') return null;
  try { return JSON.parse(JSON.stringify(body)); } catch (e) { return null; }
}

// 控件不存在（RED 状态）时静默跳过：断言应当变红，而不是让守卫抛 TypeError
function setValue(doc, id, v) {
  const el = doc.getElementById(id);
  if (el) el.value = v;
  return !!el;
}

// ===========================================================================
// 场景 A：/keyscan 页面的选择器装配 + 请求体里的 db_dir
// ===========================================================================
async function scenarioA(mutation) {
  console.log('--- A: /keyscan 页面装配（经典脚本，文档顺序，一个 realm）---');
  const realm = boot(KEYSCAN_TPL, newPayload({}), { mutation: mutation });

  PICKER_IDS.forEach(function (id) {
    check('A0 /keyscan 有 ' + id, realm.doc.getElementById(id) !== null);
  });
  check('A0c /keyscan 的"检测到的数据目录"逐字标签',
        fs.readFileSync(KEYSCAN_TPL, 'utf8').indexOf(HINT_LABEL) !== -1);

  check('A1 所有页面脚本按文档顺序加载且没有加载期抛错',
        realm.errors.length === 0, realm.errors.join('\n        '));
  check('A1b 每个脚本都真的执行了',
        realm.loaded.filter(function (n) { return n === '(inline)'; }).length === 1
        && realm.loaded.indexOf('dbdir.js') !== -1,
        'loaded=' + JSON.stringify(realm.loaded));
  check('A1c dbdir.js 在页面脚本里被加载',
        realm.loaded.indexOf('dbdir.js') !== -1, JSON.stringify(realm.loaded));
  if (realm.errors.length) return realm;      // 加载都失败了，后面的断言没有意义

  check('A2 页面真的实例化了 DbDirPicker({inputId:"cfg-db-dir"})',
        realm.ev('typeof dirPicker === "object" && dirPicker !== null')
        && evSafe(realm, 'dirPicker.inputId', null) === 'cfg-db-dir',
        'typeof dirPicker=' + realm.ev('typeof dirPicker'));
  check('A2b DbDirPicker 类的绑定 id 就是页面的那 5 个默认 id',
        evSafe(realm, 'dirPicker.selectId', null) === 'cfg-db-dir-list'
        && evSafe(realm, 'dirPicker.searchBtnId', null) === 'btn-deep'
        && evSafe(realm, 'dirPicker.useBtnId', null) === 'btn-use-dir'
        && evSafe(realm, 'dirPicker.hintId', null) === 'dir-hint');

  // 空输入 → body 里**不得**出现 db_dir 这个键（后端才会走"自动探测第一个"的回落）
  setValue(realm.doc, 'cfg-db-dir', '');
  const emptyBody = getBodyOf(realm);
  check('A3 输入框为空时 getBody() 不带 db_dir 键（后端回落行为不变）',
        !!emptyBody && emptyBody.force === false && !('db_dir' in emptyBody),
        JSON.stringify(emptyBody));

  const typed = '  D:\\synthetic_root\\acct_idle\\db_storage  ';
  setValue(realm.doc, 'cfg-db-dir', typed);
  const filledBody = getBodyOf(realm);
  check('A4 填了目录时 getBody() 带上（去空白的）db_dir',
        !!filledBody && filledBody.db_dir === typed.trim() && filledBody.force === false,
        JSON.stringify(filledBody));

  // 内存扫描：点「开始提取密钥」→ 真的 POST 出去
  fire(realm.doc.getElementById('btn-run-memory'), 'click');
  await settle(5);
  const memBody = bodyOf(realm.calls, KEYSCAN_URL);
  check('A5 内存扫描请求 body 里带上了 db_dir',
        !!memBody && memBody.db_dir === typed.trim(),
        JSON.stringify({ body: memBody,
                         urls: realm.calls.map(function (c) { return c.url; }) }));
  check('A5b 内存扫描仍然是 POST /api/backup/keyscan',
        realm.calls.some(function (c) {
          return c.url.indexOf(KEYSCAN_URL) === 0 && c.opts.method === 'POST';
        }));

  // Hook 提取：同样要带上
  fire(realm.doc.getElementById('btn-run-hook'), 'click');
  await settle(5);
  const hookBody = bodyOf(realm.calls, HOOK_URL);
  check('A6 Hook 提取请求 body 里带上了 db_dir',
        !!hookBody && hookBody.db_dir === typed.trim(), JSON.stringify(hookBody));

  // 清空输入 → Hook 请求体不再带 db_dir（向后兼容）
  setValue(realm.doc, 'cfg-db-dir', '');
  fire(realm.doc.getElementById('btn-run-hook'), 'click');
  await settle(5);
  const hookEmpty = bodyOf(realm.calls, HOOK_URL);
  check('A7 输入框清空后 Hook 请求体不带 db_dir 键（回落行为保留）',
        !!hookEmpty && !('db_dir' in hookEmpty), JSON.stringify(hookEmpty));
  return realm;
}

// ===========================================================================
// 场景 B：DbDirPicker 的推荐排序 / ⭐ 标注 / 默认选中推荐项 / 提示行
// ===========================================================================
async function scenarioB(mutation) {
  console.log('--- B: 推荐排序 + 标注 + 默认选中推荐项（新契约响应）---');
  const realm = boot(KEYSCAN_TPL, newPayload({}), { mutation: mutation });
  check('B0 guard: /keyscan 脚本无加载期抛错（否则本场景断言无意义）',
        realm.errors.length === 0, realm.errors.join('\n        '));
  if (realm.errors.length) return realm;
  await settle(20);                       // 等 load('auto') 落定

  check('B0a guard: 夹具具判别力（推荐项既不是第一条、也不是 current）',
        RECOMMENDED !== CURRENT && RECOMMENDED !== DIR_RECENT.db_path
        && [DIR_RECENT.db_path, DIR_IN_USE.db_path, DIR_IDLE.db_path][0] !== RECOMMENDED);

  const texts = optionTexts(realm.doc);
  const values = optionValues(realm.doc);
  check('B1 按后端给的顺序渲染（前端不重排）',
        JSON.stringify(values) === JSON.stringify([DIR_RECENT.db_path, DIR_IN_USE.db_path,
                                                   DIR_IDLE.db_path]),
        JSON.stringify(values));
  check('B1b 三个目录都渲染成了一个 option', texts.length === 3, JSON.stringify(texts));

  const inuseText = texts[1] || '';
  const recentText = texts[0] || '';
  const idleText = texts[2] || '';
  check('B2 in_use 的 option 带 ⭐ 且 reason 逐字（后端文案，前端不另写）',
        inuseText.indexOf('⭐') === 0
        && inuseText.indexOf(DIR_IN_USE.reason) !== -1
        && inuseText.indexOf(DIR_IN_USE.db_path) !== -1,
        JSON.stringify(inuseText));
  check('B2b in_use 的 option 形如「⭐ reason · path (wxid) size MB」',
        inuseText === DIR_IN_USE.reason + ' · ' + DIR_IN_USE.db_path
          + ' (' + DIR_IN_USE.wxid + ')  ' + DIR_IN_USE.size_mb + ' MB',
        JSON.stringify(inuseText));
  check('B3 非 ⭐ 档（recent）前置 reason 且**不**加 ⭐',
        recentText.indexOf('⭐') === -1
        && recentText === DIR_RECENT.reason + ' · ' + DIR_RECENT.db_path
          + ' (' + DIR_RECENT.wxid + ')  ' + DIR_RECENT.size_mb + ' MB',
        JSON.stringify(recentText));
  check('B3b idle 档同样只前置 reason（不加 ⭐）',
        idleText.indexOf('⭐') === -1 && idleText.indexOf(DIR_IDLE.reason) === 0,
        JSON.stringify(idleText));

  check('B4 recommended_path 被默认选中',
        selectedValue(realm.doc) === RECOMMENDED, JSON.stringify(selectedValue(realm.doc)));
  check('B4b 被选中的确实是那个带 ⭐ 的推荐 option',
        inuseText.indexOf('⭐') === 0 && selectedValue(realm.doc) === DIR_IN_USE.db_path);
  check('B5 recommended_path 被填进输入框',
        inputValue(realm.doc) === RECOMMENDED, JSON.stringify(inputValue(realm.doc)));

  check('B8 任何 option 文本里都不出现 undefined',
        texts.every(function (t) { return t.indexOf('undefined') === -1; }),
        JSON.stringify(texts));

  const hint = hintHtml(realm.doc);
  check('B6 提示行汇总了"检测到 N 个数据目录"',
        hint.indexOf('检测到 3 个数据目录') !== -1, JSON.stringify(hint));
  check('B7 提示行说明微信正在运行 + 推荐第几个（含后端 reason 逐字）',
        hint.indexOf('微信正在运行') !== -1
        && hint.indexOf('推荐第 2 个') !== -1
        && hint.indexOf(DIR_IN_USE.reason) !== -1, JSON.stringify(hint));
  check('B7b probe.errors 为空时**不**报"部分探测失败"（不是恒真文案）',
        hint.indexOf('部分探测失败') === -1, JSON.stringify(hint));

  // 直接调用 fill()（不经 load）也必须是同一套推荐/选中逻辑
  setValue(realm.doc, 'cfg-db-dir', '');
  evSafe(realm, 'dirPicker.fill(' + JSON.stringify([DIR_RECENT, DIR_IN_USE, DIR_IDLE])
         + ', ' + JSON.stringify(CURRENT) + ', ' + JSON.stringify(RECOMMENDED) + ')', null);
  check('B9 直接调用 fill(dirs, current, recommended_path) 也选中推荐项并填入输入框',
        selectedValue(realm.doc) === RECOMMENDED
        && inputValue(realm.doc) === RECOMMENDED,
        JSON.stringify({ sel: selectedValue(realm.doc), inp: inputValue(realm.doc) }));
  return realm;
}

// ===========================================================================
// 场景 C：probe 失败 / 微信未运行 —— 提示行必须如实反映（不许静默）
// ===========================================================================
async function scenarioC(mutation) {
  console.log('--- C: probe.errors / wechat_running 的提示 ---');
  const bad = boot(KEYSCAN_TPL, newPayload({
    probe: { t0_ok: true, t0_elapsed_ms: 900, t1_probed: 1,
             errors: ['synthetic probe failure'] },
  }), { mutation: mutation });
  check('C0 guard: 夹具脚本无加载期抛错', bad.errors.length === 0, bad.errors.join('\n        '));
  await settle(20);
  const badHint = hintHtml(bad.doc);
  // 裁决 R47：t0_ok 仍为 true ⇒ 推荐的最强证据没丢 ⇒ **不**喊"部分探测失败"，
  // 但必须**不静默**地中性说明"有 N 项探测不可用"。
  check('C1 t0_ok=true 时 errors 只给中性说明，不报"部分探测失败"（不喊狼来了）',
        badHint.indexOf('部分探测失败') === -1
        && badHint.indexOf('探测不可用') !== -1, JSON.stringify(badHint));

  // R47 的另一半：**最强证据丢了**时必须升级为告警
  const fatal = boot(KEYSCAN_TPL, newPayload({
    probe: { t0_ok: false, t0_elapsed_ms: 900, t1_probed: 1,
             errors: ['synthetic handle enumeration failure'] },
  }), { mutation: mutation });
  check('C1b guard: 夹具脚本无加载期抛错', fatal.errors.length === 0, fatal.errors.join('\n        '));
  await settle(20);
  const fatalHint = hintHtml(fatal.doc);
  check('C1b t0_ok=false 时必须升级为告警（推荐可能不准确）',
        fatalHint.indexOf('⚠') !== -1 && fatalHint.indexOf('推荐可能不准确') !== -1,
        JSON.stringify(fatalHint));

  const stopped = boot(KEYSCAN_TPL, newPayload({ wechat_running: false }),
                       { mutation: mutation });
  check('C2 guard: 夹具脚本无加载期抛错',
        stopped.errors.length === 0, stopped.errors.join('\n        '));
  await settle(20);
  const stoppedHint = hintHtml(stopped.doc);
  check('C3 wechat_running=false 时提示"微信未在运行"',
        stoppedHint.indexOf('微信未在运行') !== -1, JSON.stringify(stoppedHint));
  check('C3b 微信未运行时也仍然标注推荐项（推荐不依赖进程存活）',
        stoppedHint.indexOf('推荐第 2 个') !== -1, JSON.stringify(stoppedHint));
}

// ===========================================================================
// 场景 L：**旧格式响应**（无新字段）行为必须与改造前逐字相同
// ===========================================================================
async function scenarioL(mutation) {
  console.log('--- L: 旧引擎响应（无 recommended_path/tier/reason/probe）---');
  const pageHint = cachedHint(fs.readFileSync(KEYSCAN_TPL, 'utf8'));
  const realm = boot(KEYSCAN_TPL, legacyPayload(), { mutation: mutation });
  check('L0 guard: /keyscan 脚本无加载期抛错', realm.errors.length === 0,
        realm.errors.join('\n        '));
  await settle(20);

  const texts = optionTexts(realm.doc);
  check('L1 旧口径的 option 文本逐字不变（path (wxid)  size MB）',
        JSON.stringify(texts) === JSON.stringify(LEGACY_DIRS.map(legacyOptionText)),
        JSON.stringify(texts));
  check('L1b 旧格式下不出现任何 ⭐ 标注（没有 tier 就没有标注）',
        texts.every(function (t) { return t.indexOf('⭐') === -1; }), JSON.stringify(texts));
  check('L1c 旧格式下不出现 undefined',
        texts.every(function (t) { return t.indexOf('undefined') === -1; }),
        JSON.stringify(texts));
  check('L2 旧口径的选中项仍是 current（不因改造而改变）',
        selectedValue(realm.doc) === LEGACY_CURRENT,
        JSON.stringify(selectedValue(realm.doc)));
  check('L2b 旧口径下 current 被填进输入框（与改造前同一行为）',
        inputValue(realm.doc) === LEGACY_CURRENT, JSON.stringify(inputValue(realm.doc)));
  check('L3 旧格式下 #dir-hint 一个字都没被改（等于页面静态文案）',
        hintHtml(realm.doc) === pageHint,
        JSON.stringify({ got: hintHtml(realm.doc), want: pageHint }));

  // 输入框已被用户填过 → 不得被覆盖（改造前就是这个行为）
  const typed = 'D:\\typed\\by\\user\\db_storage';
  const realm2 = boot(KEYSCAN_TPL, legacyPayload(), { mutation: mutation });
  setValue(realm2.doc, 'cfg-db-dir', typed);
  evSafe(realm2, 'dirPicker.load("auto")', null);
  await settle(20);
  check('L4 旧格式下输入框里用户已填的值不被覆盖',
        inputValue(realm2.doc) === typed, JSON.stringify(inputValue(realm2.doc)));

  // 新响应下同样不得覆盖用户已填的值（推荐项只填"空输入框"）
  const realm3 = boot(KEYSCAN_TPL, newPayload({}), { mutation: mutation });
  setValue(realm3.doc, 'cfg-db-dir', typed);
  evSafe(realm3, 'dirPicker.load("auto")', null);
  await settle(20);
  check('L4b 新响应下同样不覆盖用户已填的值',
        inputValue(realm3.doc) === typed, JSON.stringify(inputValue(realm3.doc)));
}

// ===========================================================================
// 场景 S：/settings 页面的选择器装配（且没弄坏设置页原有的脚本）
// ===========================================================================
async function scenarioS(mutation) {
  console.log('--- S: /settings 页面装配 ---');
  const html = fs.readFileSync(SETTINGS_TPL, 'utf8');
  PICKER_IDS.forEach(function (id) {
    check('S0 /settings 有 ' + id, html.indexOf('id="' + id + '"') !== -1);
  });
  check('S0b /settings 的"检测到的数据目录"逐字标签',
        html.indexOf(HINT_LABEL) !== -1);

  const realm = boot(SETTINGS_TPL, newPayload({}), { mutation: mutation });
  check('S1 所有设置页脚本按文档顺序加载且没有加载期抛错',
        realm.errors.length === 0, realm.errors.join('\n        '));
  const entries = realm.entries;
  const jsAt = entries.findIndex(function (e) {
    return e.kind === 'file' && e.name === 'dbdir.js'; });
  const inlineAt = entries.findIndex(function (e) { return e.kind === 'inline'; });
  check('S1b settings.html 先加载 dbdir.js、再跑页面内联脚本',
        jsAt !== -1 && inlineAt !== -1 && jsAt < inlineAt,
        JSON.stringify(entries.map(function (e) { return e.name; })));
  if (realm.errors.length) return realm;

  check('S2 设置页真的实例化了选择器（inputId 指向 #cfg-db-dir）',
        realm.ev('typeof dirPicker === "object" && dirPicker !== null')
        && evSafe(realm, 'dirPicker.inputId', null) === 'cfg-db-dir',
        'typeof dirPicker=' + realm.ev('typeof dirPicker'));
  await settle(20);
  check('S3 设置页的下拉真的被填充 + 默认选中推荐项（证明装配有效）',
        optionCount(realm.doc) === 3 && selectedValue(realm.doc) === RECOMMENDED,
        JSON.stringify({ n: optionCount(realm.doc), sel: selectedValue(realm.doc) }));
  check('S4 设置页原有的脚本仍然跑完了（/api/settings/asr 被请求）',
        realm.calls.some(function (c) { return c.url.indexOf('/api/settings/asr') === 0; }),
        JSON.stringify(realm.calls.map(function (c) { return c.url; })));
  return realm;
}

// ===========================================================================
// 运行
// ===========================================================================
async function runAll(mutation) {
  await scenarioA(mutation);
  await scenarioB(mutation);
  await scenarioC(mutation);
  await scenarioL(mutation);
  await scenarioS(mutation);
  check('Z1 页面代码没有抛出未处理的 Promise 拒绝',
        unhandled.length === 0, JSON.stringify(unhandled));
}

async function dumpLegacy() {
  const realm = boot(KEYSCAN_TPL, legacyPayload(), {});
  await settle(20);
  const sel = realm.doc.getElementById('cfg-db-dir-list');
  const state = {
    loaded: realm.loaded,
    errors: realm.errors,
    options: (sel ? sel.options : []).map(function (o) {
      return { value: String(o.value), text: String(o.textContent),
               selected: !!o.selected };
    }),
    selectValue: selectedValue(realm.doc),
    inputValue: inputValue(realm.doc),
    hintHtml: hintHtml(realm.doc),
    urls: realm.calls.map(function (c) { return c.url; }),
    alerts: realm.alerts,
  };
  console.log(JSON.stringify(state, null, 2));
  process.exit(0);
}

// ---------------------------------------------------------------------------
// 真机核对入口：拿**真实**的 `/api/keys/dirs` 响应跑一遍渲染，只打印布尔/计数，
// 以及**掩码后**的文本（账号目录名一律替换成 <PATH>，绝不外泄）。
// ---------------------------------------------------------------------------
function maskPaths(s) {
  // 目录名与 wxid 一律掩码 —— 这个模式的输出可能被直接粘进报告
  return String(s).replace(/wxid_[A-Za-z0-9_]+/g, '<WXID>')
                  .replace(/[A-Za-z]:\\[^\s(（]*/g, '<PATH>');
}

async function realPayload(file) {
  const raw = fs.readFileSync(file, 'utf8');
  const payload = JSON.parse(raw.replace(/^\uFEFF/, ''));
  const realm = boot(KEYSCAN_TPL, payload, {});
  await settle(20);
  const dirs = payload.dirs || [];
  const texts = optionTexts(realm.doc);
  const rec = payload.recommended_path || '';
  const recIdx = dirs.map(function (d) { return d.db_path; }).indexOf(rec);
  const recDir = recIdx >= 0 ? dirs[recIdx] : null;
  const out = {
    load_errors: realm.errors,
    dirs_count: dirs.length,
    options_count: texts.length,
    order_matches_response: JSON.stringify(optionValues(realm.doc))
      === JSON.stringify(dirs.map(function (d) { return d.db_path; })),
    recommended_index: recIdx,
    recommended_selected: selectedValue(realm.doc) === rec && !!rec,
    recommended_filled_input: inputValue(realm.doc) === rec && !!rec,
    tier_seq: dirs.map(function (d) { return d.tier; }),
    star_on_star_tier: dirs.every(function (d) {
      if (d.tier !== 'in_use' && d.tier !== 'locked') return true;
      const t = texts[dirs.indexOf(d)] || '';
      return t.indexOf('⭐') === 0;
    }),
    no_star_on_other_tiers: dirs.every(function (d) {
      if (d.tier === 'in_use' || d.tier === 'locked') return true;
      return (texts[dirs.indexOf(d)] || '').indexOf('⭐') === -1;
    }),
    reason_verbatim_in_every_option: dirs.every(function (d) {
      if (!d.reason) return true;
      return (texts[dirs.indexOf(d)] || '').indexOf(d.reason) !== -1;
    }),
    no_undefined_in_options: texts.every(function (t) {
      return t.indexOf('undefined') === -1; }),
    // 提示行里只有计数 / 状态 / 后端 reason（PID 之类），**不含**任何路径
    hint_html: maskPaths(hintHtml(realm.doc)),
    hint_mentions_probe_failure: hintHtml(realm.doc).indexOf('部分探测失败') !== -1,
    probe_errors_count: ((payload.probe || {}).errors || []).length,
    wechat_running: payload.wechat_running,
    masked_option_texts: texts.map(maskPaths),
    masked_reasons: dirs.map(function (d) { return maskPaths(d.reason || ''); }),
  };
  console.log(JSON.stringify(out, null, 2));
  process.exit(0);
}

// 变异测试的期望表：变异**只**该打红这些断言，其余点名的不许被连坐。
// （`S3` 出现在变异①的红名单里是**意外收获**：它断言"设置页的下拉默认选中推荐项"，
//   与 keyscan 页的 B4/B5 是同一件事的第二个观测点 —— 变异把两页一起打红了，
//   正说明这条断言是判别性的，而不是装饰。）
const MUTATION_EXPECT = {
  'ignore-recommended': {
    red: ['B4', 'B4b', 'B5', 'B9', 'S3'],
    green: ['A1', 'A2', 'A3', 'A4', 'B0a', 'B1', 'B2', 'B2b', 'B3', 'B6', 'B7',
            'L1', 'L2', 'L3', 'L4', 'S2'],
  },
  'drop-reason': {
    red: ['B2', 'B2b', 'B3', 'B7'],
    green: ['A1', 'A2', 'A3', 'A4', 'B0a', 'B1', 'B4', 'B5', 'B9', 'B6',
            'L1', 'L2', 'L3', 'L4', 'S3'],
  },
};

async function runMutation(mode) {
  const expect = MUTATION_EXPECT[mode];
  if (!expect) {
    console.log('FAIL  unknown mutation mode: ' + mode);
    process.exit(1);
  }
  console.log('=== MUTATION MODE: ' + mode + ' ===');
  await runAll(mode);
  if (!mutationApplied) {
    console.log('FAIL  mutation "' + mode + '" never applied (anchor未命中)');
    process.exit(1);
  }
  const missing = [], stillGreen = [], confirmedRed = [], broke = [];
  expect.red.forEach(function (id) {
    if (!(id in results)) missing.push(id);
    else if (results[id] === false) confirmedRed.push(id);
    else stillGreen.push(id);
  });
  expect.green.forEach(function (id) {
    if (!(id in results)) missing.push(id);
    else if (results[id] === false) broke.push(id);
  });
  console.log('  expected-red   : ' + expect.red.join(','));
  console.log('  actually-red   : ' + confirmedRed.join(','));
  console.log('  still-green    : ' + (stillGreen.length ? stillGreen.join(',') : '(none)'));
  console.log('  unrelated-broken: ' + (broke.length ? broke.join(',') : '(none)'));
  if (missing.length) {
    console.log('FAIL  mutation run did not produce these assertions: ' + missing.join(','));
    process.exit(1);
  }
  if (stillGreen.length) {
    console.log('FAIL  mutation "' + mode + '" left these assertions green '
                + '(they are not discriminating): ' + stillGreen.join(','));
    process.exit(1);
  }
  if (broke.length) {
    console.log('FAIL  mutation "' + mode + '" broke unrelated assertions: ' + broke.join(','));
    process.exit(1);
  }
  console.log('MUTATION_CONFIRMED ' + mode + ' => ' + confirmedRed.length
              + ' assertions RED (' + confirmedRed.join(',') + ')');
  process.exit(0);
}

const MODE = process.argv[2] || '';
(async function () {
  try {
    if (MODE === 'dump-legacy') return await dumpLegacy();
    if (MODE === 'real-payload') return await realPayload(process.argv[3]);
    if (MODE) return await runMutation(MODE);
    await runAll('');
  } catch (e) {
    console.log('FAIL  guard crashed: ' + ((e && e.stack) || e));
    console.log('\nRESULT: ' + pass + ' passed, ' + (fail + 1) + ' failed');
    process.exit(1);
  }
  console.log('\nRESULT: ' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail === 0 ? 0 : 1);
})();
