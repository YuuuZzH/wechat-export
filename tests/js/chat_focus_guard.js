// tests/js/chat_focus_guard.js
//
// Task 18：`/chat` 深链定位（`known-issues.md` #29）的前端守卫。
//
// 用法：
//   node chat_focus_guard.js                        → 正常守卫（必须 0 failed）
//   node chat_focus_guard.js drop-focus-param       → 变异测试（必须**变红**）
//
// 为什么是「vm + 假 DOM」：本仓库没有 Playwright/Selenium，pytest 也不执行 JS。
// 唯一的可行路径是 `tests/test_search_js.py` 那套**经典脚本加载守卫** —— 在一个
// vm realm 里按 `/chat` 页面（`templates/index.html`）的真实顺序加载**全部**脚本，
// 配一个假 DOM。这样测的是"页面真的会怎么跑"，而不是"源码里出现过某个字符串"。
// ⚠️ 边界：**没有**在真浏览器里验证过（本仓库无浏览器驱动）。假 DOM 是手写的，
//    只实现 `app.js` 真正用到的那部分（见下方 makeEl），且 `innerHTML` 只用正则
//    抽出元素，不做真正的 HTML 解析。凡本文件没断言的浏览器行为，都未经证实。
//
// 判别性设计的核心：**假 fetch 不是"看字符串"，而是按 `/api/messages` 的既有契约
// 真的算页号**（与 `tests/test_api_messages_focus.py` 同一套形状：多页、目标在第 3 页、
// 另有一条**同 local_id 不同 create_time** 的诱饵落在第 1 页；这里按页面的默认
// `per_page=50` 放大到 130 行）。于是"前端把 focus 参数发出去"是**行为性**可断言的：
// 参数没发 → 服务端按第 1 页回 → 页号断言、高亮断言、滚动断言同时变红（见变异测试）。
// 夹具自身的判别力也当场断言（A0b/A0c/A0d）：目标不在第 1 页、诱饵在第 1 页、页数 > 1。
//
// 控制台可能是 GBK：PASS/FAIL 标签全用 ASCII，中文只出现在诊断里。
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..', '..');
const JS_DIR = path.join(ROOT, 'src', 'web', 'static', 'js');
const TEMPLATE = path.join(ROOT, 'src', 'web', 'templates', 'index.html');
const CSS_PATH = path.join(ROOT, 'src', 'web', 'static', 'css', 'app.css');
const RENDER_PATH = path.join(JS_DIR, 'search-render.js');

// ---------------------------------------------------------------------------
// 夹具（与 tests/test_api_messages_focus.py 同形状，按 per_page=50 放大）
// ---------------------------------------------------------------------------
const CHAT = 'wxid_focus_probe';
const PER_PAGE = 50;                     // 页面默认值（store.js 的 pagination.perPage）
const TARGET_LOCAL_ID = 404;
const TARGET_CREATE_TIME = 1700002300;   // DESC 序下标 106 → 第 3 页
const TARGET_PAGE = 3;
const DECOY_CREATE_TIME = 1700012900;    // 同 local_id 的另一条（另一分片）→ 第 1 页（诱饵）
const MISSING_LOCAL_ID = 999999;
const MISSING_CREATE_TIME = 1700000000;
const TOTAL_ROWS = 130;                  // 130 / 50 → 3 页
const TOTAL_PAGES = 3;
const SERVER_NOT_FOUND_MESSAGE = 'SYNTHETIC:该消息不在当前会话的查询结果中';

function fixtureRows() {
  const rows = [{ local_id: TARGET_LOCAL_ID, create_time: DECOY_CREATE_TIME }];
  for (let i = 0; i <= 128; i++) {
    rows.push({
      local_id: (i === 23 ? TARGET_LOCAL_ID : 1000 + i),
      create_time: 1700000000 + 100 * i,
    });
  }
  return rows;                            // 1 + 129 = 130 行
}

/** 假 `/api/messages`：按**文档化的契约**算页号/分页（不是查字符串表）。 */
function fakeMessagesApi(qs, opts) {
  opts = opts || {};
  const q = new URLSearchParams(qs || '');
  const perPage = parseInt(q.get('per_page') || '50', 10);
  const focusIdRaw = q.get('focus_local_id');
  const focusTsRaw = q.get('focus_create_time');
  const rows = fixtureRows().slice().sort(function (a, b) {
    return (b.create_time - a.create_time) || (a.local_id - b.local_id);
  });
  const total = rows.length;
  const totalPages = Math.ceil(total / perPage);
  let page = Math.max(1, Math.min(parseInt(q.get('page') || '1', 10), totalPages));
  let focused = null;
  if (focusIdRaw !== null && focusTsRaw !== null) {
    const fid = parseInt(focusIdRaw, 10), fts = parseInt(focusTsRaw, 10);
    let idx = -1;
    for (let i = 0; i < rows.length; i++) {
      // 契约：必须 local_id **与** create_time 同时相等（local_id 跨分片会重号）
      if (rows[i].local_id === fid && rows[i].create_time === fts) { idx = i; break; }
    }
    focused = {
      found: idx >= 0,
      page: idx >= 0 ? (Math.floor(idx / perPage) + 1) : 1,
      local_id: fid,
      create_time: fts,
      reason: idx >= 0 ? null : 'not_in_chat',
      message: idx >= 0 ? null : (opts.omitNotFoundMessage ? null : SERVER_NOT_FOUND_MESSAGE),
    };
    page = focused.page;
  }
  const slice = rows.slice((page - 1) * perPage, (page - 1) * perPage + perPage).reverse();
  const body = {
    messages: slice.map(function (r) {
      return {
        id: r.local_id, create_time: r.create_time, msg_type: 1, is_sender: true,
        sender_name: '我', sender_wxid: '', content: 'synthetic body ' + r.create_time,
        media_info: null, xml_parsed: {},
      };
    }),
    pagination: { page: page, per_page: perPage, total: total, total_pages: totalPages },
  };
  if (focused) body.focused = focused;
  return body;
}

// ---------------------------------------------------------------------------
// 假 DOM（只实现 app.js / 各组件真正用到的那部分）
// ---------------------------------------------------------------------------
const ID_RE = /\bid="([A-Za-z0-9_\-]+)"/g;
const TAG_RE = /<([a-zA-Z][a-zA-Z0-9]*)((?:"[^"]*"|[^<>])*)>/g;
const ATTR_RE = /([a-zA-Z_:][-a-zA-Z0-9_:.]*)\s*=\s*"([^"]*)"/g;

function collectIds(src, into) {
  ID_RE.lastIndex = 0;
  let m;
  while ((m = ID_RE.exec(src))) into[m[1]] = true;
  return into;
}

function splitCls(s) {
  return String(s === undefined || s === null ? '' : s).split(/\s+/).filter(Boolean);
}

function makeEl(doc, tag, attrs, id) {
  const el = {
    tagName: String(tag || 'DIV').toUpperCase(),
    _attrs: attrs || {},
    _cls: [],
    _children: [],
    _html: '',
    _listeners: {},
    _id: id || '',
    style: { display: '' },
    dataset: {},
    textContent: '',
    value: '',
    disabled: false,
    scrollTop: 0,
  };
  el._cls = splitCls(el._attrs.class);
  el.parentNode = null;
  Object.defineProperty(el, 'id', {
    get: function () { return el._id; },
    set: function (v) {
      el._id = String(v === undefined || v === null ? '' : v);
      el._attrs.id = el._id;
      if (el._id) doc._byId[el._id] = el;      // createElement 出来的元素也要能被 getElementById 找到
    },
  });
  Object.defineProperty(el, 'className', {
    get: function () { return el._cls.join(' '); },
    set: function (v) { el._cls = splitCls(v); el._attrs.class = el._cls.join(' '); },
  });
  Object.defineProperty(el, 'innerHTML', {
    get: function () { return el._html; },
    set: function (v) {
      el._html = (v === undefined || v === null) ? '' : String(v);
      // ⚠️ 只做"抽元素"，不做真正的 HTML 解析：顺序可信，层级不可信。
      //    app.js 只按 class + data-msg-id 过滤，顺序对它来说就是全部。
      el._children = parseChildren(el._html, doc);
    },
  });
  Object.defineProperty(el, 'children', { get: function () { return el._children; } });
  el.classList = {
    add: function (c) { if (el._cls.indexOf(c) === -1) el._cls.push(c); el._attrs.class = el._cls.join(' '); },
    remove: function (c) {
      const i = el._cls.indexOf(c);
      if (i !== -1) el._cls.splice(i, 1);
      el._attrs.class = el._cls.join(' ');
    },
    contains: function (c) { return el._cls.indexOf(String(c)) !== -1; },
    toggle: function (c) { if (el.classList.contains(c)) el.classList.remove(c); else el.classList.add(c); },
  };
  el.getAttribute = function (n) {
    if (n === 'class') return el.className;
    if (n === 'id') return el._id;
    return (el._attrs[n] === undefined) ? null : el._attrs[n];
  };
  el.setAttribute = function (n, v) {
    if (n === 'class') { el.className = v; return; }
    if (n === 'id') { el.id = v; return; }
    el._attrs[n] = String(v);
  };
  el.removeAttribute = function (n) { delete el._attrs[n]; };
  el.appendChild = function (c) { el._children.push(c); if (c) c.parentNode = el; return c; };
  el.insertBefore = function (c, ref) {
    const i = el._children.indexOf(ref);
    if (i === -1) el._children.push(c); else el._children.splice(i, 0, c);
    if (c) c.parentNode = el;
    return c;
  };
  el.removeChild = function (c) {
    const i = el._children.indexOf(c);
    if (i !== -1) el._children.splice(i, 1);
    return c;
  };
  el.addEventListener = function (ev, fn) {
    (el._listeners[ev] = el._listeners[ev] || []).push(fn);
    return el;
  };
  el.removeEventListener = function () {};
  el.scrollIntoView = function (o) { doc._scrolls.push({ el: el, opts: o }); };
  el.focus = function () {};
  el.click = function () {};
  el.querySelector = function () { return null; };     // 见文件头"边界"说明
  el.querySelectorAll = function () { return []; };
  return el;
}

function parseChildren(html, doc) {
  const out = [];
  TAG_RE.lastIndex = 0;
  let m;
  while ((m = TAG_RE.exec(html))) {
    const tag = m[1];
    if (tag === 'br' || tag === 'meta' || tag === 'link') continue;
    const attrs = {};
    ATTR_RE.lastIndex = 0;
    let a;
    while ((a = ATTR_RE.exec(m[2] || ''))) attrs[a[1]] = a[2];
    if (!attrs.class && !attrs.id) continue;          // 只登记有 class/id 的元素
    out.push(makeEl(doc, tag, attrs, attrs.id || ''));
  }
  return out;
}

function makeDoc(templateHtml, search) {
  const doc = {
    _known: collectIds(templateHtml, {}),
    _byId: {},
    _created: [],
    _scrolls: [],
    _listeners: {},
    readyState: 'complete',
    location: { search: search || '' },
  };
  doc.getElementById = function (id) {
    if (doc._byId[id]) return doc._byId[id];
    if (!doc._known[id]) return null;                 // 真页面里不存在的 id → null
    const el = makeEl(doc, 'DIV', { id: id }, id);
    el.parentNode = doc._fakeParent;
    doc._byId[id] = el;
    return el;
  };
  doc.createElement = function (tag) {
    const el = makeEl(doc, tag, {}, '');
    doc._created.push(el);
    return el;
  };
  doc.addEventListener = function (ev, fn) { (doc._listeners[ev] = doc._listeners[ev] || []).push(fn); };
  doc.removeEventListener = function () {};
  doc.querySelector = function () { return null; };
  doc.querySelectorAll = function () { return []; };
  doc._fire = function (ev) { (doc._listeners[ev] || []).forEach(function (f) { f(); }); };
  doc._fakeParent = {
    insertBefore: function (c, ref) { doc._created.push(c); if (c) c.parentNode = doc._fakeParent; return c; },
    appendChild: function (c) { doc._created.push(c); return c; },
  };
  doc.body = makeEl(doc, 'BODY', {}, 'body');
  return doc;
}

// ---------------------------------------------------------------------------
// realm：把页面的**经典脚本**按 index.html 的真实顺序加载进同一个上下文
// ---------------------------------------------------------------------------
function pageScriptOrder() {
  const html = fs.readFileSync(TEMPLATE, 'utf8');
  const re = /url_for\('static',\s*filename='js\/([^']+)'\)/g;
  const out = [];
  let m;
  while ((m = re.exec(html))) out.push(m[1]);
  return out;
}

function makeFetch(recorder, opts) {
  return function (url) {
    const u = String(url);
    recorder.push(u);
    let body;
    if (u.indexOf('/api/contacts') === 0) {
      body = { contacts: [{ id: CHAT, name: 'synthetic contact', type: 'direct',
                            avatar_url: '', msg_count: TOTAL_ROWS }], total: 1 };
    } else if (/\/api\/chat\/.+\/stats/.test(u)) {
      body = { total_messages: TOTAL_ROWS, date_range: { start: '2023-11-14', end: '2023-11-15' },
               sender_distribution: {} };
    } else if (u.indexOf('/api/messages') === 0) {
      body = fakeMessagesApi(u.split('?')[1] || '', opts);
    } else {
      body = {};
    }
    return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(body); } });
  };
}

function mutateSource(file, src, mode) {
  if (mode !== 'drop-focus-param') return src;
  if (file !== 'app.js') return src;
  const before = src;
  // 变异：把 focus 参数从请求里摘掉（其余逻辑一字不动）
  src = src.replace(/[ \t]*params\.focus_local_id = [^\n]*\n/, '')
           .replace(/[ \t]*params\.focus_create_time = [^\n]*\n/, '');
  if (src === before) {
    console.log('FAIL  mutation did not apply (params.focus_* assignment not found)');
    process.exit(1);
  }
  return src;
}

function loadRealm(order, search, mutation, fetchOpts) {
  const template = fs.readFileSync(TEMPLATE, 'utf8');
  const doc = makeDoc(template, search);
  const calls = [];
  // Task 19：`/api/messages` 的 URL 必须由 `api.js::messages()` 组装（app.js 里那份
  // **复制的** URL 组装已删除）。这里在页面代码真正开始跑之前（`DOMContentLoaded`
  // 还没触发）给 `api.messages` 挂一个记录器：只要 app.js 绕开它自己拼 URL，
  // 这个记录就是空的 ⇒ 断言 I/A10 变红。
  const spy = { apiMessages: [] };
  const sandbox = {
    console: console,
    document: doc,
    location: { search: search },
    fetch: makeFetch(calls, fetchOpts),
    AbortController: AbortController,
    TextDecoder: TextDecoder,
    URLSearchParams: URLSearchParams,
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    __spy: spy,
    localStorage: (function () {
      const m = {};
      return { getItem: function (k) { return m[k] === undefined ? null : m[k]; },
               setItem: function (k, v) { m[k] = String(v); },
               removeItem: function (k) { delete m[k]; } };
    })(),
  };
  sandbox.window = sandbox;
  const ctx = vm.createContext(sandbox);
  const errors = [];
  order.forEach(function (f) {
    try {
      const src = mutateSource(f, fs.readFileSync(path.join(JS_DIR, f), 'utf8'), mutation);
      vm.runInContext(src, ctx, { filename: f });
    } catch (e) {
      errors.push(f + ' -> ' + e.name + ': ' + e.message);
    }
  });
  try {
    vm.runInContext(
        '(function () { var _orig = api.messages;'
      + ' api.messages = function (p) { __spy.apiMessages.push(p); return _orig.call(api, p); };'
      + ' })()', ctx);
  } catch (e) {
    errors.push('api.messages spy -> ' + e.name + ': ' + e.message);
  }
  return { ctx: ctx, doc: doc, calls: calls, errors: errors, spy: spy };
}

// ---------------------------------------------------------------------------
// harness
// ---------------------------------------------------------------------------
let pass = 0, fail = 0;
const results = {};
const unhandled = [];
process.on('unhandledRejection', function (e) { unhandled.push(String((e && e.message) || e)); });

function check(id, ok, extra) {
  // 断言以"短 id"登记（标签的第一个词），这样变异测试能按 A1/A2/... 精确取用，
  // 不受标签里的动态数字（夹具值）影响。
  results[String(id).split(' ')[0]] = !!ok;
  if (ok) { pass++; console.log('  PASS  ' + id); }
  else { fail++; console.log('  FAIL  ' + id + (extra ? '\n        ' + extra : '')); }
}

function tick(ms) { return new Promise(function (r) { setTimeout(r, ms || 0); }); }
async function settle(n) { for (let i = 0; i < (n || 25); i++) await tick(2); }

async function boot(search, mutation, fetchOpts) {
  const order = pageScriptOrder();
  const realm = loadRealm(order, search, mutation, fetchOpts);
  if (realm.errors.length) {
    console.log('FAIL  classic script load errors:\n        ' + realm.errors.join('\n        '));
    fail++;
  }
  realm.doc._fire('DOMContentLoaded');
  await settle(30);
  return realm;
}

function classesOf(rows, cls) {
  return rows.filter(function (e) { return e.classList.contains(cls); });
}

function rowsOf(realm) {
  const listEl = realm.doc.getElementById('message-list');
  const kids = (listEl && listEl.children) || [];
  return kids.filter(function (e) {
    return e.classList && e.classList.contains('msg-row');
  });
}

function noticeEl(realm) {
  const el = realm.doc.getElementById('msg-focus-notice');
  return el || null;
}

function noticeVisible(realm) {
  const el = noticeEl(realm);
  if (!el) return false;
  const shown = String(el.style.display) === 'block';
  return shown && String(el.textContent || '').length > 0;
}

function hasFocusParams(url) {
  return /[?&]focus_local_id=/.test(url) && /[?&]focus_create_time=/.test(url);
}

function messagesUrl(realm) {
  const hits = realm.calls.filter(function (u) { return u.indexOf('/api/messages') === 0; });
  return hits.length ? hits[hits.length - 1] : '';
}

// ===========================================================================
// A. 深链带 focus：请求那一页 + 高亮 + 滚动
// ===========================================================================
async function scenarioA(mutation) {
  console.log('--- A: deep link with focus (target on page ' + TARGET_PAGE + ') ---');

  // --- 夹具自身的判别力：先证明"第 1 页不是它"，否则后面的断言会退化成恒真 ---
  const p1 = fakeMessagesApi('chat_id=' + CHAT + '&page=1&per_page=' + PER_PAGE);
  check('A0a fixture is non-empty and has more than one page',
        p1.pagination.total === TOTAL_ROWS && p1.pagination.total_pages === TOTAL_PAGES
        && TOTAL_PAGES >= 3,
        JSON.stringify(p1.pagination));
  check('A0b fixture is discriminating: page 1 does NOT contain the target',
        !p1.messages.some(function (m) {
          return m.id === TARGET_LOCAL_ID && m.create_time === TARGET_CREATE_TIME;
        }),
        'page1 ids=' + JSON.stringify(p1.messages.map(function (m) { return [m.id, m.create_time]; })));
  check('A0c fixture is discriminating: the decoy (same local_id, other create_time) IS on page 1',
        p1.messages.some(function (m) {
          return m.id === TARGET_LOCAL_ID && m.create_time === DECOY_CREATE_TIME;
        }));

  const realm = await boot('?open=' + CHAT + '&focus=' + TARGET_LOCAL_ID
                           + '&ft=' + TARGET_CREATE_TIME, mutation);
  if (realm.errors.length) return realm;
  const url = messagesUrl(realm);
  const store = vm.runInContext('Store.data', realm.ctx);
  const rows = rowsOf(realm);
  const hi = classesOf(rows, 'msg-focused');

  check('A0 the chat from ?open= was selected (regression)',
        !!store.activeChat && store.activeChat.id === CHAT,
        'activeChat=' + JSON.stringify(store.activeChat && store.activeChat.id));
  check('A1 focus request carries focus_local_id=' + TARGET_LOCAL_ID,
        /[?&]focus_local_id=404(&|$)/.test(url), url);
  check('A2 focus request carries focus_create_time=' + TARGET_CREATE_TIME,
        new RegExp('[?&]focus_create_time=' + TARGET_CREATE_TIME + '(&|$)').test(url), url);
  check('A3 focus request carries the same per_page the page uses (' + PER_PAGE
        + '), so the server\'s page arithmetic matches',
        new RegExp('[?&]per_page=' + PER_PAGE + '(&|$)').test(url), url);
  check('A4 landed on page ' + TARGET_PAGE + ', not on page 1',
        store.pagination && store.pagination.page === TARGET_PAGE,
        'page=' + JSON.stringify(store.pagination && store.pagination.page));
  check('A5 the returned page really contains the target message',
        (store.messages || []).some(function (m) {
          return m.id === TARGET_LOCAL_ID && m.create_time === TARGET_CREATE_TIME;
        }),
        'ids=' + JSON.stringify((store.messages || []).map(function (m) { return [m.id, m.create_time]; })));
  check('A6 exactly one row is highlighted', hi.length === 1,
        'highlighted=' + hi.length);
  check('A7 the highlighted row is the target row',
        hi.length === 1 && String(hi[0].getAttribute('data-msg-id')) === String(TARGET_LOCAL_ID),
        hi.length ? String(hi[0].getAttribute('data-msg-id')) : 'none');
  const scrolled = realm.doc._scrolls.filter(function (s) { return s.el === hi[0]; });
  check('A8 the target row was scrolled into view', hi.length === 1 && scrolled.length >= 1,
        'scrolls=' + realm.doc._scrolls.length);
  check('A9 no "not found" notice when the message WAS found', !noticeVisible(realm),
        'notice=' + JSON.stringify(noticeEl(realm) && noticeEl(realm).textContent));
  // Task 19：这份 URL 必须是 `api.js::messages()` 的产物（app.js 里不再有第二份组装）
  const focusParams = realm.spy.apiMessages.length
    ? realm.spy.apiMessages[realm.spy.apiMessages.length - 1] : null;
  check('A10 the page fetched through api.messages() (URL assembled by api.js)',
        !!focusParams
        && String(focusParams.focus_local_id) === String(TARGET_LOCAL_ID)
        && String(focusParams.focus_create_time) === String(TARGET_CREATE_TIME),
        JSON.stringify(realm.spy.apiMessages));

  // ---- E. 用户手动切页：高亮与提示都必须清掉（不粘死）----
  console.log('--- E: a manual page switch clears highlight + notice ---');
  const before = realm.calls.length;
  vm.runInContext('Store.emit("pagination-action", "prev")', realm.ctx);
  await settle(30);
  const newUrl = realm.calls.slice(before).filter(function (u) {
    return u.indexOf('/api/messages') === 0;
  })[0] || '';
  const store2 = vm.runInContext('Store.data', realm.ctx);
  const rows2 = rowsOf(realm);
  check('E1 the follow-up request carries NO focus params', newUrl !== '' && !hasFocusParams(newUrl), newUrl);
  check('E2 the manual page switch actually moved to page 2', store2.pagination.page === 2,
        'page=' + store2.pagination.page);
  check('E3 no row stays highlighted after the manual switch',
        classesOf(rows2, 'msg-focused').length === 0, 'rows=' + rows2.length);
  check('E4 the notice is cleared after the manual switch', !noticeVisible(realm),
        String(noticeEl(realm) && noticeEl(realm).textContent));
  check('E5 the consumed focus is not re-applied on later loads', store2.focus === null,
        'focus=' + JSON.stringify(store2.focus));
  return realm;
}

// ===========================================================================
// B. 深链 focus 但消息不在会话里 → 必须**明确提示**，不得静默
// ===========================================================================
async function scenarioB(fetchOpts, suffix) {
  console.log('--- B' + suffix + ': focus not found must be explicit ---');
  const realm = await boot('?open=' + CHAT + '&focus=' + MISSING_LOCAL_ID
                           + '&ft=' + MISSING_CREATE_TIME, '', fetchOpts);
  if (realm.errors.length) return realm;
  const url = messagesUrl(realm);
  const store = vm.runInContext('Store.data', realm.ctx);
  check('B' + suffix + '1 the request still carried the focus params', hasFocusParams(url), url);
  check('B' + suffix + '2 a notice element exists and is visible', noticeVisible(realm),
        'notice=' + JSON.stringify(noticeEl(realm) && noticeEl(realm).textContent)
        + ' display=' + JSON.stringify(noticeEl(realm) && noticeEl(realm).style.display));
  check('B' + suffix + '3 the notice says something concrete (not an empty div)',
        noticeEl(realm) !== null && String(noticeEl(realm).textContent).length > 0,
        JSON.stringify(noticeEl(realm) && noticeEl(realm).textContent));
  check('B' + suffix + '4 nothing is highlighted', classesOf(rowsOf(realm), 'msg-focused').length === 0);
  check('B' + suffix + '5 it fell back to page 1 (a defined page, not a broken state)',
        store.pagination.page === 1, 'page=' + store.pagination.page);
  return realm;
}

// ===========================================================================
// C. 深链参数自己就不合法 → 前端不猜、但必须说明白
// ===========================================================================
async function scenarioC(search, id) {
  console.log('--- C: malformed focus in the URL (' + id + ') ---');
  const realm = await boot(search, '');
  if (realm.errors.length) return realm;
  const url = messagesUrl(realm);
  const store = vm.runInContext('Store.data', realm.ctx);
  check('C' + id + '1 malformed focus is NOT forwarded to the API ("' + search + '")',
        !hasFocusParams(url), url);
  check('C' + id + '2 the page says why it did not locate anything', noticeVisible(realm),
        JSON.stringify(noticeEl(realm) && noticeEl(realm).textContent));
  check('C' + id + '3 it still shows a usable page', store.pagination.page === 1,
        'page=' + store.pagination.page);
  return realm;
}

// ===========================================================================
// D. 不带 focus 的普通链接：行为逐条不变
// ===========================================================================
async function scenarioD() {
  console.log('--- D: plain ?open= link keeps the old behaviour ---');
  const realm = await boot('?open=' + CHAT, '');
  if (realm.errors.length) return realm;
  const url = messagesUrl(realm);
  const store = vm.runInContext('Store.data', realm.ctx);
  check('D1 a plain link sends no focus params', !hasFocusParams(url), url);
  check('D2 a plain link lands on page 1', store.pagination.page === 1, 'page=' + store.pagination.page);
  check('D3 a plain link highlights nothing', classesOf(rowsOf(realm), 'msg-focused').length === 0);
  check('D4 a plain link shows no notice', !noticeVisible(realm),
        JSON.stringify(noticeEl(realm) && noticeEl(realm).textContent));
  return realm;
}

// ===========================================================================
// F. 同页出现重号 local_id：高亮必须落在 (local_id, create_time) 对应的那行
// ===========================================================================
function scenarioF(realm) {
  console.log('--- F: duplicate local_id inside one page ---');
  const fn = vm.runInContext('typeof applyFocusHighlight === "function" ? applyFocusHighlight : null',
                             realm.ctx);
  check('F0 the highlight helper is reachable from the page realm', typeof fn === 'function');
  if (typeof fn !== 'function') return;
  const listEl = realm.doc.getElementById('message-list');
  listEl.innerHTML =
      '<div class="date-divider"><span>d</span></div>'
    + '<div class="msg-row me" data-msg-id="404">first</div>'
    + '<div class="msg-row me" data-msg-id="405">other</div>'
    + '<div class="msg-row me" data-msg-id="404">second</div>';
  const kids = listEl.children.filter(function (e) { return e.classList.contains('msg-row'); });
  const msgs = [
    { id: 405, create_time: 1700001000 },
    { id: TARGET_LOCAL_ID, create_time: 1700000000 },
    { id: TARGET_LOCAL_ID, create_time: TARGET_CREATE_TIME },
  ];
  const row = fn(msgs, { localId: TARGET_LOCAL_ID, createTime: TARGET_CREATE_TIME });
  check('F1 the highlighted row is the one matching (local_id, create_time)', row === kids[2],
        'row===kids[2]? ' + (row === kids[2]));
  check('F2 the earlier row with the SAME local_id is NOT highlighted',
        kids[0].classList.contains('msg-focused') === false);
  check('F3 exactly one of the same-id rows carries the highlight class',
        classesOf(kids, 'msg-focused').length === 1);
}

// ===========================================================================
// I. `/api/messages` 的 URL 归 `api.js` 所有（Task 19：收掉 Task 18 复制的那份组装）
// ===========================================================================
async function scenarioI() {
  console.log('--- I: the /api/messages URL is assembled by api.js, not by app.js ---');

  // ① 源码层：app.js 里**不得**再有第二份 URL 组装（去掉注释后再判，免得被注释骗到）
  const appSrc = fs.readFileSync(path.join(JS_DIR, 'app.js'), 'utf8');
  const code = appSrc.replace(/\/\*[\s\S]*?\*\//g, '')
                     .split('\n').map(function (l) {
                       return l.replace(/\/\/.*$/, '');
                     }).join('\n');
  check('I1 app.js contains no /api/messages URL assembly of its own',
        code.indexOf('/api/messages?') === -1 && code.indexOf("'/api/messages'") === -1,
        code.split('\n').filter(function (l) { return l.indexOf('/api/messages') !== -1; }).join(' | '));
  check('I2 app.js no longer reaches for the fetch/abort internals (only api.js does)',
        code.indexOf('fetchJSON(') === -1,
        code.split('\n').filter(function (l) { return l.indexOf('fetchJSON(') !== -1; }).join(' | '));

  // ② 行为层：直接调 `api.messages()`，看假 fetch 记录的 URL（不经 app.js）
  const realm = loadRealm(pageScriptOrder(), '?open=' + CHAT, '', {});
  if (realm.errors.length) return realm;
  const call = function (expr) { return vm.runInContext(expr, realm.ctx); };

  call('api.messages({ chat_id: "' + CHAT + '", page: 1, per_page: ' + PER_PAGE
       + ', focus_local_id: ' + TARGET_LOCAL_ID
       + ', focus_create_time: ' + TARGET_CREATE_TIME + ' })');
  await settle(10);
  const focusUrl = messagesUrl(realm);
  check('I3 api.messages() carries focus_local_id=' + TARGET_LOCAL_ID,
        /[?&]focus_local_id=404(&|$)/.test(focusUrl), focusUrl);
  check('I4 api.messages() carries focus_create_time=' + TARGET_CREATE_TIME,
        new RegExp('[?&]focus_create_time=' + TARGET_CREATE_TIME + '(&|$)').test(focusUrl), focusUrl);
  check('I5 api.messages() still carries every pre-existing param',
        /[?&]chat_id=wxid_focus_probe(&|$)/.test(focusUrl)
        && /[?&]page=1(&|$)/.test(focusUrl)
        && new RegExp('[?&]per_page=' + PER_PAGE + '(&|$)').test(focusUrl), focusUrl);

  // 向后兼容：不给 focus ⇒ URL 里不得出现 focus 参数
  call('api.messages({ chat_id: "' + CHAT + '", page: 2, per_page: ' + PER_PAGE + ' })');
  await settle(10);
  const plainUrl = messagesUrl(realm);
  check('I6 api.messages() without focus emits no focus params (backward compatible)',
        !hasFocusParams(plainUrl) && /[?&]page=2(&|$)/.test(plainUrl), plainUrl);

  // 边界：0 是**合法**取值（页面的 `_focusIntOrNull` 接受 0）。用真值判断把它丢掉，
  // 后端就只收到一半参数 → 400，用户看到的却是"加载消息失败"（把参数错说成加载失败）。
  call('api.messages({ chat_id: "' + CHAT + '", focus_local_id: 0, focus_create_time: 0 })');
  await settle(10);
  const zeroUrl = messagesUrl(realm);
  check('I7 focus_local_id=0 / focus_create_time=0 are still sent (0 is a legal value)',
        /[?&]focus_local_id=0(&|$)/.test(zeroUrl)
        && /[?&]focus_create_time=0(&|$)/.test(zeroUrl), zeroUrl);
  return realm;
}

// ===========================================================================
// G. 搜索结果链接（search-render.js）
// ===========================================================================
function scenarioG() {
  console.log('--- G: search result href carries the deep-link params ---');
  let render;
  try { render = require(RENDER_PATH); } catch (e) {
    check('G0 search-render.js is requireable', false, String(e && e.message));
    return;
  }
  const hr = render.formatResult({
    chat_id: 'wxid_alpha', local_id: TARGET_LOCAL_ID, create_time: TARGET_CREATE_TIME,
    snippet: 'synthetic', match_spans: [], create_time: TARGET_CREATE_TIME,
  });
  check('G1 href carries open= + focus= + ft=',
        hr.href === '/chat?open=wxid_alpha&focus=404&ft=' + TARGET_CREATE_TIME,
        'href=' + JSON.stringify(hr.href));
  const noLocal = render.formatResult({ chat_id: 'wxid_alpha', create_time: 1600000000, snippet: 'x' });
  check('G2 href falls back to the old shape when local_id is missing',
        noLocal.href === '/chat?open=wxid_alpha', 'href=' + JSON.stringify(noLocal.href));
  const noTime = render.formatResult({ chat_id: 'wxid_alpha', local_id: 7, snippet: 'x' });
  check('G3 href falls back to the old shape when create_time is missing',
        noTime.href === '/chat?open=wxid_alpha', 'href=' + JSON.stringify(noTime.href));
  const group = render.formatResult({
    chat_id: '111@chatroom', local_id: 9, create_time: 1600000000, snippet: 'x' });
  check('G4 href URL-encodes the chat id and still carries the focus params',
        group.href === '/chat?open=111%40chatroom&focus=9&ft=1600000000',
        'href=' + JSON.stringify(group.href));
  const junk = render.formatResult({
    chat_id: 'wxid_alpha', local_id: 'not-a-number', create_time: 1600000000, snippet: 'x' });
  check('G5 a junk local_id degrades to the old shape instead of throwing',
        typeof junk.href === 'string' && junk.href.indexOf('/chat?open=wxid_alpha') === 0,
        'href=' + JSON.stringify(junk.href));
  check('G6 an empty chat id still yields no link', render.formatResult({}).href === '');
}

// ===========================================================================
// H. 样式真的写了（不是只加了个 class 名但没有任何视觉差异）
// ===========================================================================
function scenarioH() {
  console.log('--- H: the highlight + notice styles exist ---');
  const css = fs.readFileSync(CSS_PATH, 'utf8');
  const hlBlock = (css.match(/\.msg-focused[^{]*\{[^}]*\}/g) || []).join('\n');
  const ntBlock = (css.match(/\.msg-focus-notice[^{]*\{[^}]*\}/g) || []).join('\n');
  check('H1 app.css defines a .msg-focused rule', hlBlock.length > 0);
  check('H2 .msg-focused has a visible marker (border/outline/box-shadow/background)',
        /border|outline|box-shadow|background/.test(hlBlock), hlBlock);
  check('H3 app.css defines a .msg-focus-notice rule', ntBlock.length > 0);
  check('H4 the notice is hidden by default (app.js shows it only when needed)',
        /display\s*:\s*none/.test(ntBlock), ntBlock);
}

// ===========================================================================
async function runAll() {
  console.log('--- classic script load: every /chat page script, one realm ---');
  const order = pageScriptOrder();
  check('H0 index.html loads app.js (the page really runs the code under guard)',
        order.indexOf('app.js') !== -1, JSON.stringify(order));
  check('H0b index.html loads api.js before app.js (the URL owner is really on this page)',
        order.indexOf('api.js') !== -1 && order.indexOf('api.js') < order.indexOf('app.js'),
        JSON.stringify(order));

  const realmA = await scenarioA('');
  if (!realmA.errors.length) scenarioF(realmA);
  await scenarioB({}, '');
  await scenarioB({ omitNotFoundMessage: true }, '2');
  await scenarioC('?open=' + CHAT + '&focus=' + TARGET_LOCAL_ID, 'onlyfocus');
  await scenarioC('?open=' + CHAT + '&ft=' + TARGET_CREATE_TIME, 'onlyft');
  await scenarioC('?open=' + CHAT + '&focus=abc&ft=' + TARGET_CREATE_TIME, 'junk');
  await scenarioD();
  await scenarioI();
  scenarioG();
  scenarioH();

  check('Z1 no unhandled promise rejection escaped the page code', unhandled.length === 0,
        JSON.stringify(unhandled));
}

// 变异测试：把 focus 参数从请求里摘掉 → 这些断言**必须**变红
// （A10 也在这里：参数被摘掉时 `api.messages` 收到的 params 里就没有 focus 了，
//   它是"URL 真的由 api.js 组装"这条断言的负向判别面。）
const MUTATION_MUST_BE_RED = ['A1', 'A2', 'A4', 'A5', 'A7', 'A8', 'A9', 'A10'];
const MUTATION_MUST_BE_GREEN = ['A0', 'A3'];

async function runMutation(mode) {
  console.log('=== MUTATION MODE: ' + mode + ' ===');
  await scenarioA(mode);
  const red = [], green = [], missing = [];
  MUTATION_MUST_BE_RED.forEach(function (id) {
    if (!(id in results)) missing.push(id);
    else if (results[id] === false) red.push(id);
    else green.push(id);
  });
  const badGreen = MUTATION_MUST_BE_GREEN.filter(function (id) {
    return (id in results) && results[id] === false;
  });
  console.log('  expected-red   : ' + MUTATION_MUST_BE_RED.join(','));
  console.log('  actually-red   : ' + red.join(','));
  console.log('  still-green    : ' + (green.length ? green.join(',') : '(none)'));
  if (missing.length) {
    console.log('FAIL  mutation run did not produce these assertions: ' + missing.join(','));
    process.exit(1);
  }
  if (green.length) {
    console.log('FAIL  dropping the focus params left these assertions green (they are not discriminating): '
                + green.join(','));
    process.exit(1);
  }
  if (badGreen.length) {
    console.log('FAIL  mutation broke unrelated assertions: ' + badGreen.join(','));
    process.exit(1);
  }
  if (red.length !== MUTATION_MUST_BE_RED.length) {
    console.log('FAIL  mutation did not turn every expected assertion red');
    process.exit(1);
  }
  console.log('MUTATION_CONFIRMED focus-params-dropped => ' + red.length + ' assertions RED');
  process.exit(0);
}

const MODE = process.argv[2] || '';
(async function () {
  try {
    if (MODE) return await runMutation(MODE);
    await runAll();
  } catch (e) {
    // 守卫自己崩掉绝不能算通过（否则"代码里没有这个函数"会伪装成 0 failed）
    console.log('FAIL  guard crashed: ' + ((e && e.stack) || e));
    console.log('\nRESULT: ' + pass + ' passed, ' + fail + ' failed');
    process.exit(1);
  }
  console.log('\nRESULT: ' + pass + ' passed, ' + fail + ' failed');
  process.exit(fail === 0 ? 0 : 1);
})();
