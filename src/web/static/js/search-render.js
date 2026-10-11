// search-render.js — 结果渲染的**判定类纯函数**（T9-A7）
//
// 为什么是纯函数：pytest 不执行 JS，本项目也没有 JS 测试框架。要真的测住这些判定，
// 只能把它们做成「输入普通值 → 输出字符串/普通对象」的纯函数，由 Node 直接断言
// （`tests/js/search_render_notice.js`）。DOM 层（`search-app.js`）只负责把返回值
// 塞进 `innerHTML` / `textContent`。
//
// 定义期**不访问 DOM、不访问任何宿主全局**（没有 `document` / `window` /
// `localStorage`，也没有 `utils.js` 的 `MSG_TYPE_LABELS`）：函数体只用 ECMAScript
// 内建（`String` / `Array` / `Date` / `Math` / `JSON`），所以 `require()` 进来就能跑。
//
// ---------- 浏览器怎么拿到（Phase 2 必读）----------
// 整个文件包在 IIFE 里，**只**通过命名空间暴露：
//     window.SearchRender.statusNotice(status)          -> string（'' = 正常）
//     window.SearchRender.slowNotice(body)              -> string（'' = 快路径）
//     window.SearchRender.truncatedPageNotice(body)     -> string（'' = 不是"超出保留范围"）
//     window.SearchRender.formatResult(r)               -> object（渲染用结构）
//     window.SearchRender.escapeHtml(s)
//     window.SearchRender.highlightHtml(snippet, spans)
//     window.SearchRender.normalizeSpans(raw, maxLen)
//     window.SearchRender.formatTimeText(ts)
// 为什么不用裸全局函数：脚本是**经典脚本**按顺序加载的，顶层 `function` 声明会占全局
// 词法名。本项目实测：`utils.js` 顶层已有 `const escapeHtml`，本文件若再声明顶层
// `function escapeHtml`，浏览器在**实例化阶段**就抛
//     SyntaxError: Identifier 'escapeHtml' has already been declared
// → 整个文件不执行 → `SearchRender.*` 全是 undefined：
//    页面看着正常、功能全死（与计划缺陷 #15 `TYPES` 未定义同一类）。
// 而且 `require()` **测不出来**（CommonJS 把模块包进函数作用域），必须用
// 「vm 里按经典脚本顺序加载」才能复现 —— `tests/js/search_render_notice.js`
// 的 `classic script load` 段就是这么守住的。
//
// ---------- statusNotice 的判据（控制方更正后的口径）----------
// 告警（真信号）：
//   schema_ok=false                        索引 schema 不对，需整库重建
//   source_shards===0                      一个消息分片都没发现（源没挂载/路径错）
//   source_rows>0 且 meta_coverage<1       元数据没盖住全部消息（完整应恰为 1.0）
//   fts_skipped_no_chat>0                  文本因会话映射失效被跳过（最危险的丢失）
//   msg_text_rows!==fts_rows（两者都在场） 两表行数不一致 = 索引自身不自洽
//   duplicates_dropped>0                   构建时丢了重复行
//   text_ready===false                     全文索引不可用 → 关键词查询不可信
// **不告警**：
//   fts_coverage<1 —— 真机实测 0.8615，差额合法（图片/语音等没有正文）。
//   把它当告警会让**每次正常安装**永久显示"索引不完整"，反而训练用户忽略提示。
//
// ⚠️ 同一套判据在服务端（`src/web/routes/search_api.py` 的 `_index_incompleteness`）
// 也有一份。这里**故意重复**而不是直接读 `status.incomplete`：这样判定能在 Node 里
// 被真正测住，也不依赖一次网络调用。改动任一处都要同步另一处。
//
// ---------- slowNotice 的判据（T9-A4）----------
//   scan_mode: 'fts'（快）/ 'filter'（全扫筛选）/ 'fallback'（降级直扫）
//   slow_query_hint / elapsed_ms / candidate_rows 用来把"慢在哪、慢多少"说清楚
// 'fts' 且引擎没给 hint → 空串（什么都不提示）。
// 'fts' 但引擎**给了** `slow_query_hint`（退化正则：没用上预筛、确认阶段逐行回原文，
// 真机实测约 3.3s）→ 仍然提示：引擎自己说的"慢"不该被 UI 吞掉。
// 这一条是对 brief「`scan_mode='fts'` → 无慢提示」的**有意补充**：被冻结的口径是
// "mode='fts' 且无 hint → 空串"（用例 `scan_mode=fts (fast path)`）。
//
// ---------- truncatedPageNotice 的判据（T16）----------
//   `truncated === true` 且当页 `results` 为空 ⇒ "当前页超出保留范围"。
//   引擎为了控内存只保留前 `retained_rows` 条幸存行（`total` 仍是**精确**命中数），
//   因此"超出保留范围"的空页与"真的没有匹配的消息"必须分开说 —— 前者是**我们**
//   少给了结果，后者是库里没有。判据只看结构化字段（不解析 warning 文案）：
//   字段缺失 / `truncated` 不为 true / 当页有结果 → 返回空串（防误报）。
(function () {
  'use strict';

  function _str(v) {
    return (v === undefined || v === null) ? '' : String(v);
  }

  /** 宽松取数：接受 `number` 与纯数字字符串，其余（含 null / 'x'）→ fallback。 */
  function _num(v, fallback) {
    var n = (typeof v === 'number') ? v : NaN;
    if (typeof v === 'string' && /^\s*-?\d+(\.\d+)?\s*$/.test(v)) n = Number(v);
    if (typeof n !== 'number' || isNaN(n) || !isFinite(n)) {
      return (fallback === undefined) ? 0 : fallback;
    }
    return n;
  }

  /** 严格取数：非数字 → null（用于高亮区间，宁可丢一个 span 也不要标错位置）。 */
  function _numOrNull(v) {
    var n = _num(v, null);
    return (n === null) ? null : n;
  }

  function _has(o, k) {
    return Object.prototype.hasOwnProperty.call(o, k)
        && o[k] !== undefined && o[k] !== null;
  }

  /** 1234567 → '1,234,567'（手写，不用 toLocaleString：后者结果随环境影响）。 */
  function _thousands(n) {
    return String(Math.round(_num(n))).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  /** 毫秒 → 秒（0.1 位、去掉多余的 .0）：12034 → '12'，3300 → '3.3'。 */
  function _secs(ms) {
    return String(Math.round(_num(ms) / 100) / 10);
  }

  // -------------------------------------------------------------------------
  // 索引状态 → 告警文案
  // -------------------------------------------------------------------------

  /**
   * status → 告警文案（**空串** = 一切正常，页面不应渲染任何告警条）。
   * @param {Object} status `GET /api/search/status` 的响应
   * @returns {string}
   */
  function statusNotice(status) {
    if (!status || typeof status !== 'object') return '';

    var reasons = [];
    var textMissing = (status.text_ready === false)
      || (status.text_ready === undefined && _has(status, 'fts_rows')
          && _num(status.fts_rows) === 0);

    if (status.schema_ok === false) {
      reasons.push('索引 schema 校验未通过（旧版本或结构不符，需整库重建）');
    }
    if (status.source_shards === 0) {
      reasons.push('未发现任何消息分片（源目录未挂载或路径不对）');
    }
    if (_has(status, 'source_rows') && _num(status.source_rows) > 0
        && _num(status.meta_coverage) < 1) {
      reasons.push('元数据只覆盖 ' + (Math.round(_num(status.meta_coverage) * 10000) / 100)
                   + '% 的消息（完整时应为 100%），可能有消息搜不到');
    }
    if (_num(status.fts_skipped_no_chat) > 0) {
      reasons.push(_thousands(status.fts_skipped_no_chat)
                   + ' 条文本因会话映射失效被跳过（最危险的一类丢失）');
    }
    // 只在两个数都在场时比较，免得把"字段缺失"当成"不一致"
    if (_has(status, 'msg_text_rows') && _has(status, 'fts_rows')
        && _num(status.msg_text_rows) !== _num(status.fts_rows)) {
      reasons.push('msg_text(' + _thousands(status.msg_text_rows) + ') 与 message_fts('
                   + _thousands(status.fts_rows) + ') 行数不一致，索引自身不自洽');
    }
    if (_num(status.duplicates_dropped) > 0) {
      reasons.push('构建时丢弃了 ' + _thousands(status.duplicates_dropped) + ' 条重复行');
    }

    var parts = [];
    if (textMissing) {
      // T9-A1：必须点明"关键词搜索不可信"，否则 0 条会被当成"没有这条消息"
      parts.push('⚠ 全文索引未构建或不可用（fts_rows=0），关键词搜索不可信，'
                 + '请先构建索引再搜索关键词。');
    }
    if (reasons.length) {
      parts.push('⚠ 索引不完整（可能有消息搜不到）：' + reasons.join('；')
                 + '。建议重新构建索引。');
    }
    return parts.join(' ');
  }

  // -------------------------------------------------------------------------
  // 搜索响应 → 慢查询提示
  // -------------------------------------------------------------------------

  /**
   * body → 慢查询提示（**空串** = 快路径，不提示）。
   * @param {Object} body `GET /api/search` 的响应
   * @returns {string}
   */
  function slowNotice(body) {
    if (!body || typeof body !== 'object') return '';

    var mode = _str(body.scan_mode);
    var hint = body.slow_query_hint ? _str(body.slow_query_hint) : '';
    var isSlowMode = (mode === 'filter' || mode === 'fallback');
    if (!isSlowMode && !hint) return '';

    var parts = [];
    if (mode === 'fallback') {
      parts.push('索引不可用，本次已降级为直接扫描原始消息表（实测约 10 秒级）');
    } else if (mode === 'filter') {
      parts.push('本次查询用不上索引预筛，已全量扫描并逐条确认'
                 + '（实测这类查询约 10–12 秒）');
    } else {
      parts.push('本次查询的正则确认阶段较慢（实测大命中关键词约 3.3 秒）');
    }
    if (hint) parts.push(hint);

    var rows = _num(body.candidate_rows);
    if (rows > 0) parts.push('已扫描 ' + _thousands(rows) + ' 条消息');
    var ms = _num(body.elapsed_ms);
    if (ms > 0) parts.push('本次耗时 ' + _secs(ms) + ' 秒');

    parts.push('请耐心等待，不要重复提交');
    return '⚠ ' + parts.join('；') + '。';
  }

  // -------------------------------------------------------------------------
  // 搜索响应 → 空页的分级文案（T16：结果被截断时的"超出保留范围"）
  // -------------------------------------------------------------------------

  /**
   * body → 空页的"超出保留范围"文案（**空串** = 不是这种情况，交给原分支）。
   *
   * 判据是**结构化字段**，不解析 warning 文案：
   *   `truncated === true` 且当页 `results` 为空 ⇒ 这一页落在**保留窗口之外**
   *   （引擎只保留了前 `retained_rows` 条用于分页，而 `total_pages` 仍按**精确
   *   total** 算，所以后面这些页确实存在、但一定是空的）。
   *
   * 为什么必须单独一个分支（T16 契约 #5）：这种空页**不是**"没有匹配的消息"。
   * 沿用原文案就是把"我们只保留了前 N 条"谎报成"库里没有这条消息"——
   * 一次静默少给结果，正是本轮明令禁止的形态。
   * `truncated` 不为真 / 当页有结果 / 字段缺失（旧引擎）⇒ 一律返回空串，
   * 页面照旧走原来的分支（**防误报**）。
   *
   * @param {Object} body `GET /api/search` 的响应
   * @returns {string}
   */
  function truncatedPageNotice(body) {
    if (!body || typeof body !== 'object') return '';
    if (body.truncated !== true) return '';
    var results = (body.results && typeof body.results.length === 'number')
      ? body.results : [];
    if (results.length > 0) return '';
    return '当前页超出保留范围（只保留了前 ' + _thousands(body.retained_rows)
      + ' 条），请缩小查询范围';
  }

  // -------------------------------------------------------------------------
  // 结果项 → 渲染用结构
  // -------------------------------------------------------------------------

  function escapeHtml(s) {
    return _str(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#x27;');
  }

  /** 归一化高亮区间：切片、排序、丢弃与已保留区间重叠者（不合并，免得夸大命中范围）。 */
  function normalizeSpans(rawSpans, max) {
    var limit = Math.max(0, Math.floor(_num(max)));
    if (!rawSpans || typeof rawSpans.length !== 'number' || rawSpans.length === 0) return [];
    var valid = [];
    for (var i = 0; i < rawSpans.length; i++) {
      var sp = rawSpans[i];
      if (!sp || typeof sp.length !== 'number' || sp.length < 2) continue;
      var a = _numOrNull(sp[0]);
      var b = _numOrNull(sp[1]);
      if (a === null || b === null) continue;
      a = Math.max(0, Math.floor(a));
      b = Math.min(Math.floor(b), limit);
      if (a >= b) continue;
      valid.push([a, b]);
    }
    valid.sort(function (x, y) { return (x[0] - y[0]) || (x[1] - y[1]); });
    var kept = [];
    for (var j = 0; j < valid.length; j++) {
      var prev = kept[kept.length - 1];
      if (prev && valid[j][0] < prev[1]) continue;
      kept.push(valid[j]);
    }
    return kept;
  }

  /**
   * 摘要 → 带 `<mark>` 的 HTML（传入区间必须是**已归一化**的，见 normalizeSpans）。
   * 文本一律转义，调用方可以直接塞 `innerHTML`。
   */
  function highlightHtml(snippet, spans) {
    var s = _str(snippet);
    if (!s) return '';
    var list = (spans && typeof spans.length === 'number') ? spans : [];
    var out = '';
    var pos = 0;
    for (var i = 0; i < list.length; i++) {
      var a = list[i][0], b = list[i][1];
      if (a < pos || b > s.length || b <= a) continue;
      out += escapeHtml(s.slice(pos, a)) + '<mark>' + escapeHtml(s.slice(a, b)) + '</mark>';
      pos = b;
    }
    return out + escapeHtml(s.slice(pos));
  }

  /** 秒级时间戳 → 'MM-DD HH:MM:SS'（与 `utils.js` 的 `formatTime` 同形状）。 */
  function formatTimeText(ts) {
    var n = _num(ts);
    if (n <= 0) return '';
    var d = new Date(n * 1000);
    if (isNaN(d.getTime())) return '';
    var p = function (x) { return (x < 10 ? '0' : '') + x; };
    return p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':'
         + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  /**
   * 结果项 → `/chat` 深链。**带定位参数**（Task 18 / `known-issues.md` #29）：
   *
   *     /chat?open=<chat_id>&focus=<local_id>&ft=<create_time>
   *
   * 两个定位参数**必须同时在场**才有意义（`local_id` 跨分片会重号，见 ADR-0012），
   * 缺任意一个就退回旧形态 `/chat?open=<chat_id>` —— 宁可只打开会话，也不要发一个
   * 会把用户带错行的半截定位参数。字段缺失/非数字**不抛异常**（引擎契约变化时
   * 结果列表不能整块崩）。
   */
  function deepLinkHref(src) {
    var chatId = _str(src.chat_id);
    if (!chatId) return '';
    var href = '/chat?open=' + encodeURIComponent(chatId);
    var localId = _numOrNull(src.local_id);
    var createTime = _numOrNull(src.create_time);
    if (localId !== null && createTime !== null) {
      href += '&focus=' + encodeURIComponent(Math.floor(localId))
            + '&ft=' + encodeURIComponent(Math.floor(createTime));
    }
    return href;
  }

  /**
   * 结果项 → 渲染用结构。**缺字段不抛异常**（引擎契约变化时页面不能整块崩）。
   *
   * ⚠️ `match_spans` 是**相对 snippet** 的偏移（引擎 `build_snippet` 返回的 `rel`），
   * 不是相对原文。传绝对偏移会被切片逻辑丢掉（宁可少标一处，也不要标错位置）。
   *
   * @param {Object} r 引擎 results 里的一项
   * @returns {{chatId:string, chatName:string, chatNameHtml:string, isGroup:boolean,
   *            senderName:string, senderNameHtml:string, ts:number, timeText:string,
   *            typeLabel:string, snippet:string, spans:Array<Array<number>>,
   *            html:string, href:string}}
   */
  function formatResult(r) {
    var src = (r && typeof r === 'object') ? r : {};
    var chatId = _str(src.chat_id);
    var chatName = _str(src.chat_display_name) || chatId;
    var senderName = _str(src.sender_display_name) || _str(src.sender_username);
    var snippet = _str(src.snippet);
    var spans = normalizeSpans(src.match_spans, snippet.length);
    return {
      chatId: chatId,
      chatName: chatName,
      chatNameHtml: escapeHtml(chatName),
      isGroup: !!(src.is_group || /@chatroom$/.test(chatId)),
      senderName: senderName,
      senderNameHtml: escapeHtml(senderName),
      ts: _num(src.create_time),
      timeText: formatTimeText(src.create_time),
      typeLabel: _str(src.type_label),
      snippet: snippet,
      spans: spans,
      html: highlightHtml(snippet, spans),
      href: deepLinkHref(src),
    };
  }

  var API = {
    statusNotice: statusNotice,
    slowNotice: slowNotice,
    truncatedPageNotice: truncatedPageNotice,
    formatResult: formatResult,
    deepLinkHref: deepLinkHref,
    escapeHtml: escapeHtml,
    highlightHtml: highlightHtml,
    normalizeSpans: normalizeSpans,
    formatTimeText: formatTimeText,
  };

  if (typeof module !== 'undefined' && module.exports) module.exports = API;
  if (typeof window !== 'undefined') window.SearchRender = API;
})();
