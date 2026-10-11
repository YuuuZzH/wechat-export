// tests/js/search_syntax_roundtrip.js
//
// T9-A5：表单 ⇄ 语法（`formToSyntax` / `syntaxToForm`）的往返 + 幂等测试。
//
// 纯 Node、零依赖：`node tests/js/search_syntax_roundtrip.js`，失败时退出码非 0。
//
// 三条纪律（本项目的教训）：
//   1. 断言的是**仓库里那一份** `src/web/static/js/search-syntax.js`（require 进来），
//      绝不在测试里复制一份逻辑 —— 复制一份正是 `to_fts_expr` 类缺陷能连过 5 轮评审的原因；
//   2. **非空守卫**：先证明夹具真的把值写进去了、`formToSyntax` 真的产出了非空语法串，
//      否则「两边都是空」也能让比较通过；
//   3. 判别力自检：证明比较函数**真的能**发现不一致（`same()` 不是恒真的）。
//
// 控制台可能是 GBK：PASS/FAIL 标签全用 ASCII，中文只出现在 JSON 诊断里。
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const JS_DIR = path.join(__dirname, '..', '..', 'src', 'web', 'static', 'js');
const MODULE_PATH = path.join(JS_DIR, 'search-syntax.js');

// 类型下拉的规范名（服务端按 TYPE_ALIASES 渲染，T9-A6）。这里只是**假 els** 的 options。
const TYPE_NAMES = ['文本', '图片', '文件', '语音', '名片', '视频', '表情',
  '位置', '链接', '网络电话', '系统', '撤回'];

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

// ---------- 假 els（纯对象，无 DOM） ----------
function mkEls() {
  return {
    input: { value: '' },
    chat: { value: '' },
    sender: { value: '' },
    from: { value: '' },
    to: { value: '' },
    label: { value: '' },
    type: { options: TYPE_NAMES.map(function (n) {
      return { value: n, textContent: n, selected: false };
    }) },
  };
}

function writeForm(els, form) {
  Object.keys(form).forEach(function (k) {
    if (k === 'types') {
      els.type.options.forEach(function (o) {
        o.selected = (form.types || []).indexOf(o.value) !== -1;
      });
    } else {
      els[k].value = form[k];
    }
  });
}

function readForm(els) {
  return {
    input: els.input.value,
    chat: els.chat.value,
    sender: els.sender.value,
    from: els.from.value,
    to: els.to.value,
    label: els.label.value,
    types: els.type.options.filter(function (o) { return o.selected; })
      .map(function (o) { return o.value; }).sort(),
  };
}

function expectForm(form) {
  return {
    input: form.input || '',
    chat: form.chat || '',
    sender: form.sender || '',
    from: form.from || '',
    to: form.to || '',
    label: form.label || '',
    types: (form.types || []).slice().sort(),
  };
}

function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

// ---------- 加载被测模块 ----------
let mod;
try {
  mod = require(MODULE_PATH);
} catch (e) {
  console.log('FAIL  cannot require ' + MODULE_PATH + ': ' + e.message);
  process.exit(1);
}

const formToSyntax = mod.formToSyntax;
const syntaxToForm = mod.syntaxToForm;
const tokenizeQuery = mod.tokenizeQuery;
const quoteIfNeeded = mod.quoteIfNeeded;

// ---------- 自检（判别力）----------
console.log('=== harness selftest ===');
check('formToSyntax is a function', typeof formToSyntax === 'function');
check('syntaxToForm is a function', typeof syntaxToForm === 'function');
check('tokenizeQuery is a function', typeof tokenizeQuery === 'function');
check('quoteIfNeeded is a function', typeof quoteIfNeeded === 'function');
check('same() detects a differing value', !same({ a: 'x' }, { a: 'y' }));
check('same() detects a missing key', !same({ a: 'x' }, {}));
check('same() detects a differing key count', !same({ a: 'x' }, { a: 'x', b: '' }));
check('same() accepts identical objects', same({ a: 'x' }, { a: 'x' }));

// ---------- 用例（T9-A5 的 8 组 + 2 组加强）----------
// expectSyntax / expectTokens 是**格式契约**断言：光比较往返会把「两边一起错」放过。
const CASES = [
  { id: '1 plain keyword', form: { input: '维修' },
    expectSyntax: '维修', expectTokens: ['维修'] },
  { id: '2 multi type', form: { types: ['图片', '视频'] },
    expectSyntax: '类型:图片,视频', expectTokens: ['类型:图片,视频'] },
  { id: '3 chat name with space', form: { chat: '华为 张' },
    expectSyntax: '会话:"华为 张"', expectTokens: ['会话:"华为 张"'] },
  { id: '4 chat name with comma', form: { chat: '张三,李四' },
    expectSyntax: '会话:"张三,李四"', expectTokens: ['会话:"张三,李四"'] },
  { id: '5 date range', form: { from: '2026-01', to: '2026-03' },
    expectSyntax: '日期:2026-01..2026-03', expectTokens: ['日期:2026-01..2026-03'] },
  { id: '6 phrase keyword', form: { input: '"报修 电话"' },
    expectSyntax: '"报修 电话"', expectTokens: ['"报修 电话"'] },
  { id: '7 exclude + regex', form: { input: '-退货 /报修|维修/' },
    expectSyntax: '-退货 /报修|维修/', expectTokens: ['-退货', '/报修|维修/'] },
  { id: '8 label', form: { label: 'only_work' },
    expectSyntax: '标签:only_work', expectTokens: ['标签:only_work'] },
  { id: '9 sender with space', form: { sender: '华为 张' },
    expectSyntax: '发送者:"华为 张"', expectTokens: ['发送者:"华为 张"'] },
  { id: '10 combined all fields',
    form: { input: '维修 -退货', chat: '华为 张', sender: 'wxid_alpha',
            types: ['图片', '视频'], from: '2026-01-01', to: '2026-03-31',
            label: 'only_work' },
    expectSyntax: '维修 -退货 会话:"华为 张" 发送者:wxid_alpha 类型:图片,视频 '
      + '日期:2026-01-01..2026-03-31 标签:only_work' },
];

// 非空守卫①：夹具用例本身不得是「全空表单」，否则什么都能过
let nonEmptyCases = 0;
CASES.forEach(function (c) {
  const e = expectForm(c.form);
  if (e.input || e.chat || e.sender || e.from || e.to || e.label || e.types.length) {
    nonEmptyCases++;
  }
});
check('guard: every case has a non-empty form (non-vacuous fixtures)',
      nonEmptyCases === CASES.length, nonEmptyCases + '/' + CASES.length);

CASES.forEach(function (c) {
  console.log('--- ' + c.id + ' ---');
  const els = mkEls();
  writeForm(els, c.form);

  // 非空守卫②：夹具真的写进去了
  const wrote = readForm(els);
  const guardOk = same(wrote, expectForm(c.form));
  check('guard: fixture written into fake els', guardOk,
        'expect=' + JSON.stringify(expectForm(c.form)) + ' got=' + JSON.stringify(wrote));

  if (typeof formToSyntax !== 'function' || typeof syntaxToForm !== 'function') {
    return;                              // 前面已报错，避免二次崩溃
  }

  // 1) 表单 → 语法
  const s1 = formToSyntax(els);

  // 非空守卫③：产出的语法串不得为空（否则下面的往返是在比较两个空值）
  check('guard: formToSyntax produced a non-empty query', s1.length > 0,
        'syntax=' + JSON.stringify(s1));

  if (c.expectSyntax !== undefined) {
    check('canonical syntax string', s1 === c.expectSyntax,
          'expect=' + JSON.stringify(c.expectSyntax) + ' got=' + JSON.stringify(s1));
  }
  if (c.expectTokens !== undefined && typeof tokenizeQuery === 'function') {
    const toks = tokenizeQuery(s1);
    check('tokenizeQuery splits as expected', same(toks, c.expectTokens),
          'expect=' + JSON.stringify(c.expectTokens) + ' got=' + JSON.stringify(toks));
  }

  // 2) 语法 → 表单
  syntaxToForm(els, s1);
  const back = readForm(els);
  const want = expectForm(c.form);
  check('round-trip fidelity', same(back, want),
        'syntax=' + JSON.stringify(s1)
        + '\n        expect=' + JSON.stringify(want)
        + '\n        got   =' + JSON.stringify(back));

  // 3) 幂等：f2s(s2f(f2s(form))) === f2s(form)（用户下一次改任意字段时走的就是这条）
  const s2 = formToSyntax(els);
  check('idempotence (2nd sync) s2 === s1', s2 === s1,
        's1=' + JSON.stringify(s1) + ' s2=' + JSON.stringify(s2));

  syntaxToForm(els, s2);
  const s3 = formToSyntax(els);
  check('idempotence (3rd sync) s3 === s2', s3 === s2,
        's2=' + JSON.stringify(s2) + ' s3=' + JSON.stringify(s3));
});

// ---------- 附加：引号不劫持普通短语 / 未闭合引号不崩 ----------
console.log('--- extra: quoting must not hijack a plain phrase ---');
(function () {
  const els = mkEls();
  els.input.value = '"报修 电话"';
  const s = formToSyntax(els);
  check('phrase stays a phrase (no 会话: prefix invented)',
        s === '"报修 电话"' && s.indexOf('会话:') === -1, 'syntax=' + JSON.stringify(s));
  syntaxToForm(els, s);
  check('phrase returns to the keyword box untouched',
        els.input.value === '"报修 电话"' && els.chat.value === '',
        'input=' + JSON.stringify(els.input.value) + ' chat=' + JSON.stringify(els.chat.value));
})();

console.log('--- extra: unclosed quote must not throw ---');
(function () {
  const els = mkEls();
  let err = '';
  try {
    syntaxToForm(els, '会话:"没闭合 的引号');
  } catch (e) { err = String(e && e.message || e); }
  check('unclosed quote tolerated (no throw)', err === '', 'error=' + err);
})();

// ---------------------------------------------------------------------------
// 浏览器加载路径：**经典脚本**按页面顺序 eval（`utils.js` 在先 —— 它顶层有
// `const escapeHtml`，正是能撞死整个文件的形态）。
//
// 为什么必须单独测：`require()` 走 CommonJS，模块体被包进函数作用域，**顶层
// 标识符撞名根本不会发生** —— 只有"经典脚本 + 全局作用域"才复现浏览器行为。
// 本项目刚被 `TYPES` 未定义（加载期 ReferenceError、整页初始化崩溃、静态页面
// 测试照样通过）咬过一次，这里防的是它的孪生形态（`SyntaxError: Identifier
// 'escapeHtml' has already been declared`）。
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
  try { ctx = loadClassicScripts(['utils.js', 'search-syntax.js']); }
  catch (e) { err = e.name + ': ' + e.message; }
  check('loads as a classic script after utils.js (no load-time throw)', err === '', err);
  if (!ctx) return;

  const ns = ctx.SearchSyntax;
  check('exposes window.SearchSyntax namespace', !!ns && typeof ns === 'object');
  if (!ns) return;
  ['formToSyntax', 'syntaxToForm', 'tokenizeQuery', 'quoteIfNeeded'].forEach(function (n) {
    check('window.SearchSyntax.' + n + ' is a function', typeof ns[n] === 'function');
  });

  // 冒烟：走**页面那条路**（命名空间 + 假 els）真的跑一次往返
  const els = mkEls();
  els.chat.value = '华为 张';
  const q = ns.formToSyntax(els);
  ns.syntaxToForm(els, q);
  check('namespace round-trip works in a browser-like realm',
        q === '会话:"华为 张"' && els.chat.value === '华为 张' && els.input.value === '',
        'syntax=' + JSON.stringify(q) + ' chat=' + JSON.stringify(els.chat.value)
        + ' input=' + JSON.stringify(els.input.value));
})();

console.log('\nRESULT: ' + pass + ' passed, ' + fail + ' failed');
process.exit(fail ? 1 : 0);
