/* keys_dirs_annotate_guard.js — Task 22 前端守卫（`/keys`「手动输入密钥」页）
 *
 * 被测对象（**浏览器真实加载路径**，不是静态 HTML 文本）：
 *   `templates/keys.html`（/keys）里的**内联脚本** —— 在一个 vm realm 里当经典脚本
 *   执行，配手写假 DOM + 按契约返回的假 fetch。
 *
 * 断言四件事（用户决定 S8 = 方案 A「最小改」）：
 *   ① `applyDirs(data)` 把后端给的 `dirs[i].reason` **逐字**当前缀渲染进 option 文本
 *      （`reason + " · " + db_path + "  (wxid)"`），前端**不另造一份文案**
 *      —— `reason` 的唯一事实源是后端（api.md A7）；
 *   ② 默认选中 `data.recommended_path` > `data.current` > 第一条（顺序照后端给的，不重排）；
 *   ③ `data.current`（配置里记住的目录）**始终**留在选项里：不在 `dirs` 里就补一个 option
 *      （改造前就有这段逻辑，必须保留）；
 *   ④ 旧引擎响应（没有 `tier` / `reason` / `recommended_path`）下行为与改造前**逐字相同**，
 *      且任何 option 文本里都不出现 `undefined`。
 *   附带钉住：`loadDirs()` / `deepSearch()` / `currentDir()` 的既有语义不变。
 *
 * 用法:
 *   node keys_dirs_annotate_guard.js                     # 正常守卫（应当 0 failed）
 *   node keys_dirs_annotate_guard.js ignore-recommended  # 变异①：无视 recommended_path
 *   node keys_dirs_annotate_guard.js drop-reason         # 变异②：丢掉 reason 只显示路径
 *   node keys_dirs_annotate_guard.js dump-legacy         # 打印旧格式响应的可观测状态（离线比对用）
 *   node keys_dirs_annotate_guard.js real-payload <file> # 用**真实** /api/keys/dirs 响应跑一遍，
 *                                                        # 只打印布尔/计数与掩码后的文本
 *
 * 环境变量:
 *   KEYS_TPL=<path>   用别的 keys.html 跑（只用于"改造前 vs 改造后"的离线比对；
 *                     默认就是仓库里的 `src/web/templates/keys.html`）。
 *
 * ⚠ **边界（不得含糊）**：本仓库没有 Playwright/Selenium，所以这里**没有**在真浏览器里
 * 验证过任何像素/布局行为。假 DOM 只实现本页用得到的那部分（`getElementById` /
 * `createElement` / `appendChild` / `innerHTML` / `textContent` / `value` / `options` /
 * `selected` / `style` / `addEventListener` / `querySelectorAll`），`innerHTML` 不做真正的
 * HTML 解析（层级不可信、顺序可信）。**唯一**按浏览器语义实现的关键点是 `<select>.value`：
 * setter 把匹配的 option 标为 selected、getter 读回被选中的 option 的值
 * （"默认选中推荐项"这件事因此是可观测的，而不是"看起来设置了"）。
 *
 * 全部夹具都是**合成**的：绝不读真实微信数据、不连真实 `/api`（`real-payload` 模式用的是
 * 调用方**已经取下来**的响应文件，且输出一律掩码）。
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..', '..');
const TPL_DIR = path.join(ROOT, 'src', 'web', 'templates');
const KEYS_TPL = process.env.KEYS_TPL
  ? process.env.KEYS_TPL
  : path.join(TPL_DIR, 'keys.html');

// ---------------------------------------------------------------------------
// 合成夹具
//
// **顺序即后端给出的排序**（A7：in_use > locked > recent > config > idle），而且故意让
// "推荐项既不是第一条、也不是 `current`" —— 否则"无视 recommended_path 永远选 current /
// 第一条"这个变异根本不可能变红（夹具不具判别力，断言就是装饰）。
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
// wechat_running —— 前端必须逐字保持改造前的行为。
// 故意让其中一个目录**没有** wxid（改造前的公式是 `d.wxid ? ... : ""`，缺字段不许冒 undefined）。
const LEGACY_DIRS = [
  { db_path: 'D:\\synthetic_root\\acct_recent\\db_storage',
    wxid: 'wxid_synthetic_recent', mtime: 1599990000, db_count: 7, size_mb: 512.2 },
  { db_path: 'D:\\synthetic_root\\acct_named\\db_storage',
    mtime: 1600000000, db_count: 2, size_mb: 7.5 },
];
const LEGACY_CURRENT = LEGACY_DIRS[1].db_path;

function legacyPayload() {
  return { dirs: LEGACY_DIRS, current: LEGACY_CURRENT, mode: 'auto' };
}

// 旧口径的 option 文本（改造前 `keys.html::applyDirs()` 的公式，逐字照抄）：
//   d.db_path + (d.wxid ? "  (" + d.wxid + ")" : "")
// ⚠ 注意这里是**两个空格** —— 这一页与 `dbdir.js` 的 `" (" + wxid + ")"` 本来就不同，
//   改造不许动旧路径，所以这里按本页的公式冻结。
function legacyOptionText(d) {
  return d.db_path + (d.wxid ? '  (' + d.wxid + ')' : '');
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
    // 真 DOM 里任何 input 的 `.value` 都是字符串（空输入是 `''`，不是 undefined）
    value: '',
    style: {}, dataset: {}, children: [], options: [],
    files: null,
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
// 没有任何 option 匹配时 value 变成 ''（真浏览器在没有显式选中时会回落到第一个
// option，而旧代码依赖"设置 current 之后读回来是 current"，所以这里按显式 selected 实现）。
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

function cachedHint(html) {
  const m = /id="dir-hint"[^>]*>([\s\S]*?)<\/div>/.exec(html);
  return m ? m[1] : null;
}

function makeDoc(html) {
  const known = collectIds(html, {});
  const cache = {};
  const doc = {
    _known: known,
    getElementById: function (id) {
      if (!known[id]) return null;   // 真页面里不存在的 id → null（暴露拼写错误）
      if (!cache[id]) {
        cache[id] = (id === 'cfg-db-dir') ? makeSelect(doc, id) : makeEl(doc, id, 'div');
      }
      return cache[id];
    },
    createElement: function (tag) { return makeEl(doc, '', tag); },
    addEventListener: function () {},
    querySelector: function () { return null; },
    querySelectorAll: function () { return []; },
  };
  // `#dir-hint` 的**页面静态文案**：旧响应下 `loadDirs()` 一个字都不许动它，
  // 所以这里按模板原样种进去，断言才不是拿空气比空气。
  const hint = cachedHint(html);
  if (hint !== null && known['dir-hint']) {
    cache['dir-hint'] = makeEl(doc, 'dir-hint', 'div');
    cache['dir-hint']._html = hint;
    cache['dir-hint']._htmlSet = true;
  }
  return doc;
}

// ---------------------------------------------------------------------------
// 脚本提取（本页只有内联脚本，没有 url_for('static', ...)）
// ---------------------------------------------------------------------------
const SCRIPT_RE = /<script([^>]*)>([\s\S]*?)<\/script>/g;

function scriptEntries(html) {
  const out = [];
  SCRIPT_RE.lastIndex = 0;
  let m;
  while ((m = SCRIPT_RE.exec(html))) {
    const attrs = m[1] || '';
    const src = /\bsrc\s*=/.test(attrs);
    out.push({ kind: src ? 'file' : 'inline', name: src ? '(external)' : '(inline)',
               raw: m[0], code: m[2] });
  }
  return out;
}

// ---------------------------------------------------------------------------
// 变异（只在**内存**里改内联脚本文本，绝不落盘）
//   ① ignore-recommended：无视 recommended_path，永远选 current / 第一条；
//   ② drop-reason       ：把后端给的 reason 丢掉，只显示路径。
// 锚点就是被测实现里那两行 —— 锚点找不到就是**测试自身**失效，必须响亮地失败
// （否则"变异没生效"会伪装成"断言不具判别力"）。
// ---------------------------------------------------------------------------
const MUTATIONS = {
  'ignore-recommended': {
    from: 'const chosen = recommended || ',
    to: 'const chosen = ',
  },
  'drop-reason': {
    from: 'const reason = d.reason ? String(d.reason) : "";',
    to: 'const reason = "";',
  },
};
let mutationApplied = false;

function mutateCode(code, mode) {
  const spec = MUTATIONS[mode];
  if (!spec) return code;
  const idx = code.indexOf(spec.from);
  if (idx === -1) return code;
  mutationApplied = true;
  return code.slice(0, idx) + spec.to + code.slice(idx + spec.from.length);
}

// ---------------------------------------------------------------------------
// 假 fetch
// ---------------------------------------------------------------------------
const STATUS_BODY = {
  total: 2, verified: 1, missing: 1, invalid: 0, plain: 0,
  databases: [
    { rel: 'message/message_0.db', sizeMb: 1.5, verified: true, hasKey: true, keyMasked: 'a1b2…9f' },
    { rel: 'contact/contact.db', sizeMb: 0.4, verified: false, hasKey: false, keyMasked: '' },
  ],
};

function makeFetch(calls, dirsPayload, deepPayload) {
  return function (url, opts) {
    const u = String(url);
    calls.push({ url: u, opts: opts || {} });
    let body = {};
    if (u.indexOf('/api/keys/dirs') === 0) {
      body = (u.indexOf('mode=deep') !== -1) ? deepPayload : dirsPayload;
    } else if (u.indexOf('/api/keys/status') === 0) {
      body = STATUS_BODY;
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
    fetch: makeFetch(calls, dirsPayload,
                     o.deepPayload === undefined ? dirsPayload : o.deepPayload),
    TextDecoder: TextDecoder,
    Promise: Promise,
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    alert: function (msg) { alerts.push(String(msg)); },
    confirm: function () { return false; },
  };
  sandbox.window = sandbox;
  const ctx = vm.createContext(sandbox);
  const errors = [];
  const loaded = [];
  const entries = scriptEntries(html);
  entries.forEach(function (e) {
    try {
      const code = e.kind === 'file' ? '' : mutateCode(e.code, o.mutation);
      vm.runInContext(code, ctx, { filename: path.basename(templatePath) + ':inline' });
      loaded.push(e.name);
    } catch (err) {
      errors.push(e.name + ' -> ' + err.name + ': ' + err.message);
    }
  });
  return {
    html: html, ctx: ctx, doc: doc, calls: calls, alerts: alerts, errors: errors,
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
// 等"启动链"真的跑完（`loadDirs().then(loadStatus)`）—— 离线比对要的是**确定性**状态，
// 不能靠"猜 n 次 tick 够了"（猜少了会拍到半个状态，两张 dump 就不可比）。
async function settleUntil(realm, pred, n) {
  for (let i = 0; i < (n || 60); i++) {
    if (pred()) return true;
    await tick(2);
  }
  await settle(5);
  return pred();
}

function optionsOf(doc) {
  const sel = doc.getElementById('cfg-db-dir');
  return sel ? sel.options : [];
}
function optionTexts(doc) {
  return optionsOf(doc).map(function (o) { return String(o.textContent); });
}
function optionValues(doc) {
  return optionsOf(doc).map(function (o) { return String(o.value); });
}
function selectedValue(doc) {
  const sel = doc.getElementById('cfg-db-dir');
  return sel ? String(sel.value) : '';
}
function hintHtml(doc) {
  const h = doc.getElementById('dir-hint');
  return h ? String(h.innerHTML) : '';
}
function hasUndefined(texts) {
  return texts.some(function (t) { return t.indexOf('undefined') !== -1; });
}

function fire(el, event) {
  if (!el) return;
  (el._listeners[event] || []).forEach(function (f) { f.call(el, { type: event }); });
}

// 取值但**不**因为"全局不存在"把整个守卫炸掉：RED 状态下（改动还没做）
// 我们要看到一条条 FAIL，而不是一个 ReferenceError 让它变成"跑不完"。
function evSafe(realm, expr, fallback) {
  try {
    const v = realm.ev(expr);
    return v === undefined ? fallback : v;
  } catch (e) {
    return fallback;
  }
}

// ===========================================================================
// 场景 A：/keys 页面装配（方案 A：页面结构一个字都不动）
// ===========================================================================
async function scenarioA(mutation) {
  console.log('--- A: /keys 页面装配 + 启动行为 ---');
  const html = fs.readFileSync(KEYS_TPL, 'utf8');
  ['cfg-db-dir', 'btn-deep', 'btn-use-dir', 'btn-refresh', 'dir-hint']
    .forEach(function (id) {
      check('A0 /keys 有 #' + id, html.indexOf('id="' + id + '"') !== -1);
    });
  // 用户决定 S8 = 方案 A：**不许**把 select 换成 input（那是方案 B）
  check('A0b #cfg-db-dir 仍然是 <select>（方案 A：不动页面结构）',
        html.indexOf('<select id="cfg-db-dir">') !== -1
        && html.indexOf('<input id="cfg-db-dir"') === -1);
  check('A0c 这一页仍然不加载 dbdir.js（不迁共享组件）',
        html.indexOf('dbdir.js') === -1);

  const realm = boot(KEYS_TPL, newPayload({}), { mutation: mutation });
  check('A1 内联脚本无加载期抛错', realm.errors.length === 0,
        realm.errors.join('\n        '));
  if (realm.errors.length) return realm;

  check('A2 页面里的函数都在（applyDirs / loadDirs / deepSearch / currentDir）',
        ['applyDirs', 'loadDirs', 'deepSearch', 'currentDir']
          .every(function (n) { return evSafe(realm, 'typeof ' + n, '') === 'function'; }),
        JSON.stringify(['applyDirs', 'loadDirs', 'deepSearch', 'currentDir']
          .map(function (n) { return n + '=' + evSafe(realm, 'typeof ' + n, ''); })));

  await settle(20);
  check('A3 启动时仍请求 GET /api/keys/dirs?mode=auto',
        realm.calls.some(function (c) { return c.url === '/api/keys/dirs?mode=auto'; }),
        JSON.stringify(realm.calls.map(function (c) { return c.url; })));
  check('A3b 启动后仍会拉一次密钥覆盖情况（loadDirs().then(loadStatus) 语义不变）',
        realm.calls.some(function (c) { return c.url.indexOf('/api/keys/status?') === 0; }),
        JSON.stringify(realm.calls.map(function (c) { return c.url; })));
  check('A3c #sum-badge 真的被状态渲染写过（不是假绿）',
        String(realm.doc.getElementById('sum-badge').textContent).indexOf('已就绪 1') !== -1,
        JSON.stringify(String(realm.doc.getElementById('sum-badge').textContent)));
  return realm;
}

// ===========================================================================
// 场景 B：reason 标注 + 默认选中推荐项（新契约响应）
// ===========================================================================
async function scenarioB(mutation) {
  console.log('--- B: reason 逐字标注 + 默认选中 recommended_path ---');
  const realm = boot(KEYS_TPL, newPayload({}), { mutation: mutation });
  check('B0 guard: 脚本无加载期抛错（否则本场景断言无意义）',
        realm.errors.length === 0, realm.errors.join('\n        '));
  if (realm.errors.length) return realm;
  await settle(20);

  check('B0a guard: 夹具具判别力（推荐项不是第一条、也不是 current）',
        RECOMMENDED !== CURRENT && RECOMMENDED !== DIR_RECENT.db_path);

  const texts = optionTexts(realm.doc);
  const values = optionValues(realm.doc);
  check('B1 顺序照后端给的渲染（前端不重排）',
        JSON.stringify(values) === JSON.stringify([DIR_RECENT.db_path, DIR_IN_USE.db_path,
                                                   DIR_IDLE.db_path]),
        JSON.stringify(values));
  check('B1b 三个目录都渲染成 option 且没有多余补项（current 就在 dirs 里）',
        texts.length === 3, JSON.stringify(texts));

  const recentText = texts[0] || '';
  const inuseText = texts[1] || '';
  const idleText = texts[2] || '';

  // 后端 reason **逐字**当前缀，其余部分与改造前（本页旧公式）逐字相同
  check('B2 in_use 的 option 文本 = reason + " · " + path + "  (wxid)"（reason 逐字来自后端）',
        inuseText === DIR_IN_USE.reason + ' · ' + DIR_IN_USE.db_path + '  (' + DIR_IN_USE.wxid + ')',
        JSON.stringify(inuseText));
  check('B2b 文本以 reason 开头 ⇒ 前端没有另造/改写标注文案',
        inuseText.indexOf(DIR_IN_USE.reason) === 0
        && inuseText.indexOf(DIR_IN_USE.db_path) !== -1,
        JSON.stringify(inuseText));
  check('B3 recent 档同样前置 reason 逐字、且**不**加前端自造的 ⭐',
        recentText === DIR_RECENT.reason + ' · ' + DIR_RECENT.db_path + '  (' + DIR_RECENT.wxid + ')'
        && recentText.indexOf('⭐') === -1,
        JSON.stringify(recentText));
  check('B3b idle 档前缀同样是后端 reason（不是"未发现活动迹象"以外的任何自造文案）',
        idleText.indexOf(DIR_IDLE.reason) === 0
        && idleText.indexOf(DIR_IDLE.reason + ' · ' + DIR_IDLE.db_path) === 0,
        JSON.stringify(idleText));

  check('B4 recommended_path 被默认选中（而不是配置里的 current）',
        selectedValue(realm.doc) === RECOMMENDED,
        JSON.stringify({ selected: selectedValue(realm.doc), recommended: RECOMMENDED,
                         current: CURRENT }));
  check('B5 current（不是推荐项）仍然留在选项里可供选择',
        values.indexOf(CURRENT) !== -1, JSON.stringify(values));
  check('B6 currentDir() 读到的是被选中的推荐项（语义不变：仍是 #cfg-db-dir 的值）',
        evSafe(realm, 'currentDir()', null) === RECOMMENDED,
        JSON.stringify(evSafe(realm, 'currentDir()', null)));
  check('B7 任何 option 文本里都不出现 undefined',
        !hasUndefined(texts), JSON.stringify(texts));
  check('B8 applyDirs 的返回值仍是"后端给的 dirs 条数"（loadDirs/深度搜索的 n 语义不变）',
        evSafe(realm, 'applyDirs(' + JSON.stringify(newPayload({})) + ')', null) === 3,
        JSON.stringify(evSafe(realm, 'applyDirs(' + JSON.stringify(newPayload({})) + ')', null)));
  check('B9 改造没有吃掉既有字段（wxid 仍在文本里，size 字段未渲染属改造前既有行为）',
        inuseText.indexOf('(' + DIR_IN_USE.wxid + ')') !== -1
        && inuseText.indexOf('MB') === -1,
        JSON.stringify(inuseText));
  // 半新后端：`tier` 有了、`reason` 还没有 —— 只许显示路径（不许前端替后端编文案）
  const r2 = boot(KEYS_TPL, newPayload({
    dirs: [{ db_path: DIR_IDLE.db_path, wxid: '', size_mb: 1, tier: 'in_use' }],
    current: '', recommended_path: '',
  }), { mutation: mutation });
  await settle(20);          // 假 fetch 也是 Promise：不 settle 就断言会假绿/假红
  const t2 = optionTexts(r2.doc);
  check('B10 半新后端（只有 tier 没有 reason）下只显示路径、不冒 undefined、也不自造文案',
        texts.every(function (t) { return t.indexOf('undefined') === -1; })
        && t2.length === 1 && t2[0] === DIR_IDLE.db_path,
        JSON.stringify({ n: t2.length, first: t2[0], want: DIR_IDLE.db_path,
                         errors: r2.errors }));
  return realm;
}

// ===========================================================================
// 场景 E：`current` **不在** dirs 里 —— 必须补成 option 且不抢走推荐项的选中
// ===========================================================================
async function scenarioE(mutation) {
  console.log('--- E: current 不在 dirs 里（补 option 的旧逻辑必须保留）---');
  const elsewhere = 'D:\\synthetic_root\\acct_configured_only\\db_storage';
  const realm = boot(KEYS_TPL, newPayload({ current: elsewhere }), { mutation: mutation });
  check('E0 guard: 脚本无加载期抛错', realm.errors.length === 0,
        realm.errors.join('\n        '));
  await settle(20);

  const values = optionValues(realm.doc);
  const texts = optionTexts(realm.doc);
  check('E1 current 不在 dirs 里时被补成一个 option（改造前的逻辑保留）',
        values.indexOf(elsewhere) !== -1 && values.length === 4,
        JSON.stringify(values));
  check('E1b 补出来的 option 文本就是裸路径（与改造前逐字相同，不带 reason 前缀）',
        texts[texts.length - 1] === elsewhere, JSON.stringify(texts));
  check('E2 补 current 之后，默认选中的**仍然是** recommended_path',
        selectedValue(realm.doc) === RECOMMENDED, JSON.stringify(selectedValue(realm.doc)));
  check('E3 有补项时也不出现 undefined', !hasUndefined(texts), JSON.stringify(texts));
  return realm;
}

// ===========================================================================
// 场景 F：「刷新状态」「深度搜索」按钮的既有语义不变
// ===========================================================================
async function scenarioF(mutation) {
  console.log('--- F: 深度搜索 / 刷新状态按钮的既有语义 ---');
  const deepPayload = newPayload({ mode: 'deep', wechat_running: true });
  const realm = boot(KEYS_TPL, newPayload({}), { mutation: mutation, deepPayload: deepPayload });
  check('F0 guard: 脚本无加载期抛错', realm.errors.length === 0,
        realm.errors.join('\n        '));
  await settle(20);

  fire(realm.doc.getElementById('btn-deep'), 'click');
  await settle(20);
  check('F1 深度搜索仍然请求 /api/keys/dirs?mode=deep（参数不变）',
        realm.calls.some(function (c) { return c.url === '/api/keys/dirs?mode=deep'; }),
        JSON.stringify(realm.calls.map(function (c) { return c.url; })));
  check('F2 深度搜索的响应同样被渲染 + 默认选中推荐项',
        optionValues(realm.doc).length === 3
        && selectedValue(realm.doc) === RECOMMENDED,
        JSON.stringify({ n: optionValues(realm.doc).length,
                         sel: selectedValue(realm.doc) }));
  check('F3 深度搜索完成后的既有文案不变（"共找到 N 个数据目录"）',
        hintHtml(realm.doc).indexOf('深度搜索完成，共找到 3 个数据目录') !== -1,
        JSON.stringify(hintHtml(realm.doc)));
  check('F3b 深度搜索按钮被重新启用（既有行为不变）',
        realm.doc.getElementById('btn-deep').disabled === false);

  fire(realm.doc.getElementById('btn-refresh'), 'click');
  await settle(20);
  check('F4 「刷新状态」只请求 /api/keys/status（不碰目录接口）',
        realm.calls.filter(function (c) {
          return c.url.indexOf('/api/keys/status?') === 0; }).length >= 2,
        JSON.stringify(realm.calls.map(function (c) { return c.url; })));
  return realm;
}

// ===========================================================================
// 场景 L：**旧格式响应**（无 tier / reason / recommended_path）必须与改造前逐字相同
// ===========================================================================
async function scenarioL(mutation) {
  console.log('--- L: 旧引擎响应（无 tier/reason/recommended_path）---');
  const pageHint = cachedHint(fs.readFileSync(KEYS_TPL, 'utf8'));
  const realm = boot(KEYS_TPL, legacyPayload(), { mutation: mutation });
  check('L0 guard: 脚本无加载期抛错', realm.errors.length === 0,
        realm.errors.join('\n        '));
  await settle(20);

  const texts = optionTexts(realm.doc);
  check('L1 旧口径的 option 文本逐字不变（db_path + "  (wxid)"）',
        JSON.stringify(texts) === JSON.stringify(LEGACY_DIRS.map(legacyOptionText)),
        JSON.stringify(texts));
  check('L1b 旧格式下不出现任何 ⭐ / " · " 标注（没有 reason 就没有前缀）',
        texts.every(function (t) { return t.indexOf('⭐') === -1 && t.indexOf(' · ') === -1; }),
        JSON.stringify(texts));
  check('L1c 旧格式下不出现 undefined（缺 wxid 的目录同样不冒）',
        !hasUndefined(texts), JSON.stringify(texts));
  check('L2 旧口径的选中项仍是 current（不因改造而改变）',
        selectedValue(realm.doc) === LEGACY_CURRENT,
        JSON.stringify({ got: selectedValue(realm.doc), want: LEGACY_CURRENT }));
  check('L2b currentDir() 仍返回 current',
        evSafe(realm, 'currentDir()', null) === LEGACY_CURRENT,
        JSON.stringify(evSafe(realm, 'currentDir()', null)));
  check('L3 旧格式下 #dir-hint 一个字都没被改（等于页面静态文案）',
        hintHtml(realm.doc) === pageHint,
        JSON.stringify({ got: hintHtml(realm.doc), want: pageHint }));
  check('L4 旧格式下 applyDirs 的返回值不变（dirs 条数）',
        evSafe(realm, 'applyDirs(' + JSON.stringify(legacyPayload()) + ')', null) === 2,
        JSON.stringify(evSafe(realm, 'applyDirs(' + JSON.stringify(legacyPayload()) + ')', null)));

  // 旧格式 + current 不在 dirs 里 → 补 option 且选中 current（改造前的行为）
  const stray = 'D:\\synthetic_root\\acct_legacy_only\\db_storage';
  const realm2 = boot(KEYS_TPL, { dirs: LEGACY_DIRS, current: stray, mode: 'auto' },
                      { mutation: mutation });
  await settle(20);
  const values2 = optionValues(realm2.doc);
  check('L5 旧格式 + current 不在 dirs 里：补 option 并选中它（改造前行为不变）',
        values2.indexOf(stray) !== -1 && selectedValue(realm2.doc) === stray,
        JSON.stringify({ values: values2, sel: selectedValue(realm2.doc) }));

  // 旧格式 + 一个目录都没有 → loadDirs 的既有提示文案不变
  const realm3 = boot(KEYS_TPL, { dirs: [], current: '', mode: 'auto' }, { mutation: mutation });
  await settle(20);
  check('L6 一个目录都没有时仍走既有提示（"未自动检测到微信数据目录"）',
        hintHtml(realm3.doc).indexOf('未自动检测到微信数据目录') !== -1,
        JSON.stringify(hintHtml(realm3.doc)));
  return realm;
}

// ===========================================================================
// 运行
// ===========================================================================
async function runAll(mutation) {
  await scenarioA(mutation);
  await scenarioB(mutation);
  await scenarioE(mutation);
  await scenarioF(mutation);
  await scenarioL(mutation);
  check('Z1 页面代码没有抛出未处理的 Promise 拒绝',
        unhandled.length === 0, JSON.stringify(unhandled));
}

// ---------------------------------------------------------------------------
// 离线比对：旧格式响应下的全部可观测状态（供"改造前 vs 改造后"逐字比较）
// ---------------------------------------------------------------------------
function observableState(realm, payload) {
  const sel = realm.doc.getElementById('cfg-db-dir');
  return {
    loaded: realm.loaded,
    errors: realm.errors,
    options: (sel ? sel.options : []).map(function (o) {
      return { value: String(o.value), text: String(o.textContent), selected: !!o.selected };
    }),
    selectValue: selectedValue(realm.doc),
    currentDir: evSafe(realm, 'currentDir()', null),
    dirsReturn: evSafe(realm, 'applyDirs(' + JSON.stringify(payload) + ')', null),
    hintHtml: hintHtml(realm.doc),
    urls: realm.calls.map(function (c) { return c.url; }),
    alerts: realm.alerts,
    sumBadge: String(realm.doc.getElementById('sum-badge').textContent),
  };
}

async function dumpLegacy() {
  const payload = legacyPayload();
  const realm = boot(KEYS_TPL, payload, {});
  await settleUntil(realm, function () {
    return realm.calls.some(function (c) { return c.url.indexOf('/api/keys/status?') === 0; });
  });
  console.log(JSON.stringify(observableState(realm, payload), null, 2));
  process.exit(0);
}

// ---------------------------------------------------------------------------
// 真机核对入口：拿**真实**的 `/api/keys/dirs` 响应跑一遍渲染，只打印布尔/计数，
// 以及**掩码后**的文本（账号目录名 / wxid 一律替换，绝不外泄）。
// ---------------------------------------------------------------------------
function maskPaths(s) {
  return String(s).replace(/wxid_[A-Za-z0-9_]+/g, '<WXID>')
                  .replace(/[A-Za-z]:\\[^\s(（·]*/g, '<PATH>');
}

async function realPayload(file) {
  const raw = fs.readFileSync(file, 'utf8');
  const payload = JSON.parse(raw.replace(/^\uFEFF/, ''));
  const realm = boot(KEYS_TPL, payload, {});
  await settle(20);
  const dirs = payload.dirs || [];
  const texts = optionTexts(realm.doc);
  const rec = payload.recommended_path || '';
  const recIdx = dirs.map(function (d) { return d.db_path; }).indexOf(rec);
  const out = {
    load_errors: realm.errors,
    dirs_count: dirs.length,
    options_count: texts.length,
    order_matches_response: JSON.stringify(optionValues(realm.doc))
      === JSON.stringify(dirs.map(function (d) { return d.db_path; })),
    recommended_index: recIdx,
    recommended_selected: !!rec && selectedValue(realm.doc) === rec,
    current_still_selectable: !payload.current
      || optionValues(realm.doc).indexOf(payload.current) !== -1,
    tier_seq: dirs.map(function (d) { return d.tier; }),
    reason_verbatim_in_every_option: dirs.every(function (d) {
      if (!d.reason) return true;
      return (texts[dirs.indexOf(d)] || '').indexOf(d.reason) !== -1;
    }),
    reason_prefixed_in_every_option: dirs.every(function (d) {
      if (!d.reason) return true;
      return (texts[dirs.indexOf(d)] || '').indexOf(String(d.reason) + ' · ') === 0;
    }),
    no_undefined_in_options: !hasUndefined(texts),
    wechat_running: payload.wechat_running,
    probe_t0_ok: (payload.probe || {}).t0_ok,
    probe_errors_count: ((payload.probe || {}).errors || []).length,
    masked_option_texts: texts.map(maskPaths),
    masked_reasons: dirs.map(function (d) { return maskPaths(d.reason || ''); }),
  };
  console.log(JSON.stringify(out, null, 2));
  process.exit(0);
}

// 变异测试的期望表：变异**只**该打红这些断言，其余点名的不许被连坐。
// （`E2` / `F2` 出现在变异①的红名单里是同一件事的另外两个观测点：
//   它们也在断言"默认选中推荐项"，变异把它们一起打红正说明这条断言是判别性的。）
const MUTATION_EXPECT = {
  'ignore-recommended': {
    red: ['B4', 'B6', 'E2', 'F2'],
    green: ['A0b', 'A1', 'A3', 'B0a', 'B1', 'B1b', 'B2', 'B2b', 'B3', 'B3b',
            'B5', 'B7', 'B8', 'B9', 'B10', 'E1', 'E1b', 'E3', 'F1', 'F3',
            'L1', 'L2', 'L2b', 'L3', 'L5', 'L6'],
  },
  'drop-reason': {
    red: ['B2', 'B2b', 'B3', 'B3b'],
    green: ['A1', 'A3', 'B1', 'B1b', 'B4', 'B5', 'B6', 'B7', 'B8',
            'B9', 'E1', 'E2', 'F1', 'F2', 'F3', 'L1', 'L2', 'L3', 'L5', 'L6'],
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
    console.log('FAIL  mutation "' + mode + '" never applied (anchor not found)');
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
