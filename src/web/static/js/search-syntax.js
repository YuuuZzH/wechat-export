// search-syntax.js — 表单 ⇄ 语法框 的**纯函数**（T9-A5）
//
// 为什么单独一个文件：pytest 不执行 JS，本项目的"页面级断言"只能靠
// **纯函数 + Node 断言**（T9-A7）。所以这里：
//   * 定义期**不访问 DOM、不访问任何宿主全局**（没有 `document` / `window` /
//     `localStorage`）：`require()` 进来就能跑，Node 直接断言；函数体只用
//     ECMAScript 内建（`String` / `Array` / `RegExp`）；
//   * 元素对象一律**显式当参数传**（`formToSyntax(els)` / `syntaxToForm(els, q)`），
//     测试传假 `els`（`{input:{value}, ..., type:{options:[{value,selected}]}}`）。
//
// ---------- 浏览器怎么拿到（Phase 2 必读）----------
// 整个文件包在 IIFE 里，**只**通过命名空间暴露：
//     window.SearchSyntax.formToSyntax(els)
//     window.SearchSyntax.syntaxToForm(els, q)
//     window.SearchSyntax.tokenizeQuery(q)
//     window.SearchSyntax.quoteIfNeeded(v)
//     window.SearchSyntax.unquote(v)
// 为什么不用裸全局函数：脚本是**经典脚本**按顺序加载的，顶层 `function` 声明会占
// 全局词法名。`utils.js` 顶层已经有 `const escapeHtml` —— 本文件（或它的兄弟文件）
// 若再声明同名顶层函数，浏览器会在**实例化阶段**抛
// `SyntaxError: Identifier 'escapeHtml' has already been declared`，整个文件不执行，
// 于是 `SearchSyntax.*` 全是 undefined：页面看着正常、功能全死（与计划缺陷 #15
// `TYPES` 未定义同一类，而且 `require()` 测不出来 —— CommonJS 把模块包在函数作用域里）。
// IIFE + 命名空间从根上消除这类撞名。
// Node 侧仍是 `module.exports = {...}`（同样的名字）。
//
// ---------- 规范语法串的顺序（本 UI 的约定）----------
//   关键词  会话:  发送者:  类型:  日期:from..to  标签:      （单个空格连接）
//
// ---------- 为什么要引号，以及"含逗号也要引号" ----------
// 后端已改为：`字段:"…"` = **一个**值，且**只在引号外**按逗号拆分（Task 1 修复）。
// 于是：
//   * 值里有空白 → 必须引号，否则 `会话:华为 张` 会被拆成 `会话:华为` + 关键词 `张`；
//   * 值里有逗号 → **也**要引号，否则后端 `_apply_field` 会把 `会话:张三,李四`
//     当成**两个**会话（UI 显示 1 个、后端筛 2 个，静默不一致）。注意这一条是
//     **后端语义**要求，UI 往返测试对"含逗号"本来就是 PASS 的，判别性断言在
//     `tests/test_search_query.py` 一侧（`parse_query('会话:"张三,李四"')['chats']
//     == ['张三,李四']`）——本文件不冒充那份覆盖。
//
// ---------- 已知限制（后端也一致，不要依赖）----------
//   * 引号内**不支持转义**：值本身含 `"` 无法表达。本文件**不发明**转义语法 ——
//     后端不认，发明了就是"UI 发得出、后端解析不了"的静默不一致；
//   * `-会话:"x"`（排除 + 字段）是未定义语法，后端报错，本文件也不处理；
//   * 引号内含换行：后端会退化成关键词，本文件的扫描器同样不跨行处理
//     （遇到闭合引号就停，行为与"整段当一个值"一致）。
//
// 分词器是**手写扫描**而不是正则 `/"[^"]*"|(\S+)/g`：后者只有当 token **以引号开头**
// 时才走引号分支，`会话:"华为 张"` 会先被 `\S+` 吃掉半截（T9-A5；真机数据里
// 24,148 个联系人有 1,441 个显示名含空格，不是边角情况）。
(function () {
  'use strict';

  var FIELD_PREFIX_RE = /^(会话|发送者|类型|日期|标签)[:：]/;
  var FIELD_VALUE_RE = /^(会话|发送者|类型|日期|标签)[:：]([\s\S]*)$/;

  function _str(v) {
    return (v === undefined || v === null) ? '' : String(v);
  }

  function _isSpace(ch) {
    return ch === ' ' || ch === '\t' || ch === '\n' || ch === '\r'
        || ch === '\f' || ch === '\v';
  }

  /** 值含空白**或逗号**时加引号。空值原样返回（调用方本就不该传空值）。 */
  function quoteIfNeeded(v) {
    var s = _str(v);
    if (s === '') return s;
    return (/[\s,]/.test(s)) ? ('"' + s + '"') : s;
  }

  /** 剥掉一层外层引号（仅在成对出现时）。 */
  function unquote(v) {
    var s = _str(v);
    if (s.length >= 2 && s.charAt(0) === '"' && s.charAt(s.length - 1) === '"') {
      return s.slice(1, -1);
    }
    return s;
  }

  /** 分词。`字段:"…"` 与裸短语 `"…"` 都是**一个** token；其余按空白切。 */
  function tokenizeQuery(q) {
    var s = _str(q);
    var tokens = [];
    var i = 0;
    while (i < s.length) {
      while (i < s.length && _isSpace(s.charAt(i))) i++;
      if (i >= s.length) break;
      var start = i;

      // 引号可能出现在开头（裸短语），也可能跟在 `字段:` 之后 —— 后者正是需要
      // 特殊识别的形态（正则分词器就是在这里把 token 切坏的）。
      var quoteAt = -1;
      var m = FIELD_PREFIX_RE.exec(s.slice(i, i + 4));
      if (m) quoteAt = i + m[0].length;
      else if (s.charAt(i) === '"') quoteAt = i;

      if (quoteAt !== -1 && s.charAt(quoteAt) === '"') {
        var close = s.indexOf('"', quoteAt + 1);
        if (close === -1) {                // 未闭合：整段交给后端报错，UI 不猜
          tokens.push(s.slice(start));
          break;
        }
        i = close + 1;
        tokens.push(s.slice(start, i));
        continue;
      }

      while (i < s.length && !_isSpace(s.charAt(i))) i++;
      tokens.push(s.slice(start, i));
    }
    return tokens;
  }

  function _options(els) {
    var t = els && els.type;
    var opts = t && t.options;
    if (!opts) return [];
    return Array.prototype.slice.call(opts);
  }

  function _get(el) {
    return (el && el.value !== undefined && el.value !== null) ? String(el.value) : '';
  }

  function _set(el, v) {
    if (el) el.value = v;
  }

  /** 表单 → 规范语法串。顺序：关键词、会话、发送者、类型(逗号)、日期、标签。 */
  function formToSyntax(els) {
    var parts = [];
    var kw = _get(els && els.input).trim();
    if (kw) parts.push(kw);

    var chat = _get(els && els.chat).trim();
    if (chat) parts.push('会话:' + quoteIfNeeded(chat));

    var sender = _get(els && els.sender).trim();
    if (sender) parts.push('发送者:' + quoteIfNeeded(sender));

    var types = _options(els).filter(function (o) { return o && o.selected; })
      .map(function (o) { return _str(o.value); });
    if (types.length) parts.push('类型:' + types.join(','));

    var from = _get(els && els.from);
    var to = _get(els && els.to);
    if (from || to) parts.push('日期:' + from + '..' + to);

    var label = _get(els && els.label);
    if (label) parts.push('标签:' + quoteIfNeeded(label));

    return parts.join(' ');
  }

  /** 语法串 → 表单（就地写回；返回 els 便于链式调用与测试）。 */
  function syntaxToForm(els, q) {
    if (!els) return els;

    // 先清空：语法框是**唯一事实源**，解析不出来的字段必须回到空，不能残留旧值
    _set(els.input, '');
    _set(els.chat, '');
    _set(els.sender, '');
    _set(els.from, '');
    _set(els.to, '');
    _set(els.label, '');
    _options(els).forEach(function (o) { if (o) o.selected = false; });

    var free = [];
    tokenizeQuery(q).forEach(function (tok) {
      var m = FIELD_VALUE_RE.exec(tok);
      if (!m) { free.push(tok); return; }

      var field = m[1];
      var raw = m[2];
      var quoted = (raw.length >= 2 && raw.charAt(0) === '"'
                    && raw.charAt(raw.length - 1) === '"');
      var value = unquote(raw);

      if (field === '会话') {
        _set(els.chat, value);
      } else if (field === '发送者') {
        _set(els.sender, value);
      } else if (field === '标签') {
        _set(els.label, value);
      } else if (field === '类型') {
        // 后端：逗号**只在引号外**才是多值分隔符；引号内是"一个值"。
        // 类型是受控词表（服务端渲染的 12 个规范名，都不含逗号），所以引号形态
        // 只可能来自手输；此时按后端口径当成一个值（匹配不上就一个都不选）。
        var names = quoted ? [value] : value.split(',');
        names.forEach(function (name) {
          var v = _str(name).trim();
          if (!v) return;
          _options(els).forEach(function (o) {
            if (o && _str(o.value) === v) o.selected = true;
          });
        });
      } else if (field === '日期') {
        var r = value.split('..');
        _set(els.from, _str(r[0]).trim());
        _set(els.to, _str(r[1]).trim());
      }
    });

    _set(els.input, free.join(' '));
    return els;
  }

  var API = {
    formToSyntax: formToSyntax,
    syntaxToForm: syntaxToForm,
    tokenizeQuery: tokenizeQuery,
    quoteIfNeeded: quoteIfNeeded,
    unquote: unquote,
  };

  if (typeof module !== 'undefined' && module.exports) module.exports = API;
  if (typeof window !== 'undefined') window.SearchSyntax = API;
})();
