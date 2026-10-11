// search-app.js — 全局搜索页的页面逻辑（Task 9 Phase 2）
//
// 这个文件的三条硬约束都来自本项目已经踩过的坑，改动前请先读完：
//
// ① **整个文件是一个 IIFE，顶层不留任何标识符。**
//    页面引入的是**经典脚本**，五个文件共用同一个全局名字空间。顶层写一个
//    `function escapeHtml`，就会和 `utils.js` 顶层的 `const escapeHtml` 撞名，
//    浏览器在**实例化阶段**抛
//        SyntaxError: Identifier 'escapeHtml' has already been declared
//    → 整个文件**一行都不执行** → `SearchSyntax.*` / 页面逻辑全 undefined：
//      页面看着正常、功能全死。而 `require()` 测不出来（CommonJS 把模块包进函数
//      作用域）。`tests/test_search_js.py` 的 classic-script load guard 就是守这个的。
//
// ② **只通过命名空间调用 phase-1 的纯函数**（它们是 IIFE 成员，不是裸全局）：
//        SearchSyntax.formToSyntax(els)      表单 → 语法串
//        SearchSyntax.syntaxToForm(els, q)   语法串 → 表单
//        SearchRender.statusNotice(status)   索引状态 → 告警文案（'' = 正常）
//        SearchRender.slowNotice(body)       搜索响应 → 慢路径提示（'' = 快路径）
//        SearchRender.formatResult(r)        结果项 → 渲染结构（含转义与 <mark>）
//    写成裸 `formToSyntax(els)` 就是 `ReferenceError: formToSyntax is not defined`。
//    表单⇄语法同步、提示判定、结果格式化**一律复用**这两个文件 —— 页面里绝不重写
//    一遍（重写的那份正是 phase-1 的 Node 断言覆盖不到的地方）。
//
// ③ **不维护第二份类型列表**（T9-A6）。类型选项由服务端从 `TYPE_ALIASES` 渲染进
//    `<select id="search-type" multiple>`，页面只读写 `els.type.options`。
//    计划模板里那份前端 `TYPES` 在项目里根本不存在，一句 `TYPES.forEach` 就让整页
//    初始化崩掉；而"只查静态 HTML"的断言照样通过。
//
// 依赖缺失（脚本没加载成功、或加载期抛错）时**大声失败**并置
// `window.SearchApp.initialized = false`：这个标记是加载守卫用来区分
// "页面真的初始化完了" 与 "页面只剩静态 HTML" 的观测点。
//
// 结果区里**任何**来自服务端的文本都必须经 `SearchRender` 的转义后再进
// `innerHTML`（正文/显示名可能含 `<script>`）；本文件不自己写第二份转义。
(function () {
  'use strict';

  var SS = window.SearchSyntax;
  var SR = window.SearchRender;

  // `api.js` 的 `api` 是顶层 `const`（全局**词法**绑定，不是 `window.api` 属性），
  // 所以只能用裸标识符引用，且必须用 `typeof` 探测存在性。
  var apiClient = (typeof api !== 'undefined' && api) ? api : null;

  var PER_PAGE = 50;

  var $ = function (id) {
    return document.getElementById(id);
  };

  var els = {
    input: $('search-input'), chat: $('search-chat'), sender: $('search-sender'),
    type: $('search-type'), from: $('search-date-from'), to: $('search-date-to'),
    label: $('search-label'), run: $('search-run'),
    syntax: $('search-syntax'),
    help: $('search-help'), helpToggle: $('search-help-toggle'),
    bar: $('search-index-bar'), results: $('search-results'),
  };

  // 初始化失败一律**大声记录**（见文件头）：`initialized` 只在全部接线跑完后为 true。
  function bail(reason, message) {
    window.SearchApp = { initialized: false, error: reason };
    if (els.results) {
      els.results.innerHTML = '<div class="error-msg">⚠ ' + message + '</div>';
    }
  }

  var REQUIRED = ['input', 'chat', 'sender', 'type', 'from', 'to', 'label', 'run',
                  'syntax', 'results', 'bar'];
  var missingEls = REQUIRED.filter(function (k) { return !els[k]; });
  if (missingEls.length) {
    bail('missing-dom', '页面控件缺失（' + missingEls.join(', ')
         + '）：搜索页的 HTML 与脚本版本不一致，请强制刷新页面。');
    return;
  }
  if (!apiClient || typeof apiClient.search !== 'function'
      || typeof apiClient.searchStatus !== 'function'
      || typeof apiClient.searchBuild !== 'function') {
    bail('missing-api',
         '页面脚本依赖缺失：api.js 未加载成功，或版本过旧（缺少搜索接口）。');
    return;
  }
  if (!SS || typeof SS.formToSyntax !== 'function'
      || typeof SS.syntaxToForm !== 'function'
      || !SR || typeof SR.statusNotice !== 'function'
      || typeof SR.slowNotice !== 'function'
      || typeof SR.formatResult !== 'function') {
    bail('missing-namespace',
         '页面脚本依赖缺失：search-syntax.js / search-render.js 未加载成功，'
         + '表单同步与结果渲染不可用。');
    return;
  }

  // =========================================================================
  // 小工具（IIFE 内部，绝不外泄成全局）
  // =========================================================================

  function thousands(n) {
    var v = Number(n);
    if (!isFinite(v)) return '0';
    return String(Math.round(v)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  function seconds(ms) {
    var v = Number(ms);
    if (!isFinite(v)) return '';
    return String(Math.round(v / 100) / 10);
  }

  function messageOf(e) {
    if (!e) return '未知错误';
    return (e.message ? String(e.message) : String(e));
  }

  function html(id) {
    return document.getElementById(id);
  }

  // =========================================================================
  // 表单 ⇄ 语法框 双向同步
  //
  // 同步逻辑**全部**在 `search-syntax.js`（phase-1，已被 88 条 Node 断言覆盖：
  // 含空格的会话名、含逗号的会话名、日期区间、短语、排除词、正则、幂等…）。
  // 页面只做两件事：把当前表单/语法框交给它，以及用一个**重入闸门**防止两个写者
  // 互相触发（语法框被写 → input 事件 → 反写表单 → change 事件 → 再写语法框…）。
  // =========================================================================

  var syncing = false;

  function onFormEdited() {
    if (syncing) return;
    syncing = true;
    try {
      els.syntax.value = SS.formToSyntax(els);
    } finally {
      syncing = false;
    }
  }

  function onSyntaxEdited() {
    if (syncing) return;
    syncing = true;
    try {
      SS.syntaxToForm(els, els.syntax.value);
    } finally {
      syncing = false;
    }
  }

  [els.input, els.chat, els.sender, els.from, els.to, els.label, els.type]
    .forEach(function (el) {
      if (!el || typeof el.addEventListener !== 'function') return;
      el.addEventListener('input', onFormEdited);
      el.addEventListener('change', onFormEdited);
    });
  els.syntax.addEventListener('input', onSyntaxEdited);
  els.syntax.addEventListener('change', onSyntaxEdited);

  // =========================================================================
  // 语法速查
  // =========================================================================

  var HELP_TEXT = [
    '维修                        关键词（任意长度中文子串）',
    '维修 电话                   空格 = AND',
    '"服务器 宕机"                引号 = 短语',
    '维修 OR 报修                 布尔 OR（也可用 |）',
    '维修 -退货                   - 前缀 = 排除',
    '/报修|维修/                  斜杠包裹 = 正则',
    '会话:"华为 张"               会话（可填备注名或 wxid；含空格要加引号）',
    '发送者:我                    发送者；我 = 本人',
    '类型:图片,语音                消息类型多选（下拉里选也一样）',
    '日期:2026-01-01..2026-03-31  日期范围（也支持 2026-03 或 2026）',
    '标签:only_work               按通讯录标签圈定会话',
  ].join('\n');

  if (els.helpToggle && els.help
      && typeof els.helpToggle.addEventListener === 'function') {
    els.helpToggle.addEventListener('click', function () {
      var show = (els.help.style.display !== 'block');
      els.help.style.display = show ? 'block' : 'none';
      if (show) els.help.textContent = HELP_TEXT;
    });
  }

  // =========================================================================
  // 标签下拉（从通讯录拉取；**失败不得中断页面** —— 标签只是可选筛选）
  // =========================================================================

  function loadLabels() {
    return apiClient.addressBookLabels().then(function (d) {
      var names = (d && d.labels) || [];
      if (!els.label || !names.length) return;
      names.forEach(function (name) {
        var opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        els.label.appendChild(opt);
      });
    }).catch(function () {
      // 没有通讯录 / 请求失败：静默忽略。标签是可选筛选项，不是页面能否用的前提。
    });
  }

  // =========================================================================
  // 索引状态条
  //
  // T9-A2：这里**只是展示**。状态查询失败或挂住都不影响输入与搜索（失败就藏起来），
  // 搜索请求也**不等待**状态请求（`doSearch` 里一次都没碰 status）。
  //
  // T9-A1：三个必须区分的状态 ——
  //   * 索引不完整（元数据没盖住全部消息、文本因会话映射失效被跳过…）→ 告警，且
  //     **不得**同时显示"索引就绪"（那正是"看起来正常的绿色状态"）；
  //   * `text_ready === false`（meta-only 构建后 `ready` 仍为 True，但关键词查询
  //     必然 0 条）→ 明确告知"全文索引未构建"，判据用 `text_ready`，**绝不用** `ready`；
  //   * 构建失败（SSE error）→ 失败原因 + 可重试，且失败态是**粘性**的：在开始新的
  //     构建之前，任何一次状态刷新都不许把"索引就绪"放回来。
  // =========================================================================

  var lastStatus = null;
  var build = { state: 'idle', error: '' };   // idle | running | failed | ok

  function progressBlock(visible) {
    return '<div id="search-build-progress" style="display:'
      + (visible ? 'block' : 'none') + '">'
      + '<div class="progress-bar-outer"><div id="search-build-bar"'
      + ' class="progress-bar-inner" style="width:0%"></div></div>'
      + '<div class="progress-detail" id="search-build-detail">等待开始…</div></div>';
  }

  function buildButton(label) {
    return '<button class="btn btn-primary" id="search-build">' + label + '</button>';
  }

  function coverageText(st) {
    var c = Number(st.fts_coverage);
    if (!isFinite(c) || c <= 0 || c >= 1) return '';
    // `fts_coverage < 1` 是**合法**差额（图片/语音等没有正文），只作说明，
    // **不是**告警：把它当告警会让每次正常安装都永久显示"索引不完整"（T9-A1）。
    return '（全文覆盖 ' + (Math.round(c * 10000) / 100) + '%，差额为图片/语音等无正文消息）';
  }

  function counterText(st) {
    return '全文 ' + thousands(st.fts_rows) + ' 条 / 元数据 ' + thousands(st.meta_rows)
      + ' 条' + coverageText(st);
  }

  function barHtml() {
    var st = lastStatus;

    if (build.state === 'running') {
      return '<div class="progress-detail">正在构建索引…构建期间可以继续搜索'
        + '（会走降级直扫，较慢）。</div>' + progressBlock(true);
    }

    if (build.state === 'failed') {
      // T9-A1：失败**不得**显示"索引就绪"，也不得残留看起来正常的绿色状态。
      // 失败态是粘性的（只在 buildStarted / buildCompleted 时清除）。
      return '<div class="search-bar-failed">'
        + '<div class="progress-detail">❌ 索引构建失败：' + SR.escapeHtml(build.error) + '</div>'
        + '<div class="progress-detail">'
        + (st ? '当前索引：' + counterText(st) : '当前索引状态未知（状态查询也失败了）')
        + '</div>'
        + buildButton('重试构建（约 1 分钟，视机器与负载）')
        + progressBlock(false)
        + '</div>';
    }

    if (!st) {
      return '<div class="progress-detail">索引状态未知（状态查询失败或尚未返回）。'
        + buildButton('构建索引（约 1 分钟，视机器与负载）') + progressBlock(false) + '</div>';
    }

    var notice = SR.statusNotice(st);
    var out = [];
    if (notice) {
      out.push('<div class="search-bar-alert">' + SR.escapeHtml(notice) + '</div>');
    }

    if (st.text_ready === false) {
      // T9-A1 的核心：这是唯一能拦住"关键词查询静默返回 0 条"的地方。
      // 判据必须是 `text_ready`（= schema_ok and fts_rows>0），不是 `ready`。
      out.push('<div class="progress-detail">全文索引未构建或不可用（fts_rows=0）：'
        + '关键词搜索会静默返回 0 条，请先构建索引再搜关键词。'
        + buildButton('一键构建（约 1 分钟，视机器与负载）') + '</div>');
      out.push(progressBlock(false));
      return out.join('');
    }

    if (st.ready === true && st.stale === true) {
      out.push('<div class="progress-detail">索引已过期（源数据有变化），建议增量刷新：'
        + counterText(st)
        + '<button class="btn btn-secondary" id="search-refresh">刷新索引</button></div>');
      return out.join('');
    }

    if (st.ready === true && !notice) {
      out.push('<div class="progress-detail">索引就绪：' + counterText(st)
        + '　<button class="btn btn-secondary" id="search-refresh">增量刷新</button>'
        + '<button class="btn btn-secondary" id="search-build">重建索引</button></div>');
      return out.join('');
    }

    if (st.ready === true) {
      // 有告警时**不**说"索引就绪"（见上面 T9-A1 的说明）
      out.push('<div class="progress-detail">索引可用，但不完整（见上方告警）：'
        + counterText(st)
        + '　<button class="btn btn-secondary" id="search-refresh">增量刷新</button></div>');
      return out.join('');
    }

    out.push('<div class="progress-detail">索引未构建 —— 当前会降级直扫原始消息表'
      + '（较慢，结果应当一致）。' + buildButton('一键构建（约 1 分钟，视机器与负载）') + '</div>');
    out.push(progressBlock(false));
    return out.join('');
  }

  function renderStatus(st) {
    if (st) lastStatus = st;
    var bar = els.bar;
    if (!bar) return;
    bar.style.display = 'block';
    bar.innerHTML = barHtml();
    var b = html('search-build');
    if (b && typeof b.addEventListener === 'function') {
      b.addEventListener('click', function () { runBuild({ action: 'build' }); });
    }
    var r = html('search-refresh');
    if (r && typeof r.addEventListener === 'function') {
      r.addEventListener('click', function () { runBuild({ action: 'refresh' }); });
    }
  }

  function refreshStatus() {
    return apiClient.searchStatus().then(function (st) {
      renderStatus(st);
    }).catch(function () {
      // T9-A2：状态查询失败/挂住**不得**阻塞输入与搜索 —— 只是把状态条藏起来。
      lastStatus = null;
      if (els.bar) els.bar.style.display = 'none';
    });
  }

  function buildStarted() {
    build.state = 'running';
    build.error = '';
    renderStatus(null);
  }

  function buildCompleted() {
    build.state = 'ok';
    build.error = '';
    if (els.bar) {
      els.bar.style.display = 'block';
      els.bar.innerHTML = '<div class="progress-detail">构建完成，正在刷新索引状态…</div>';
    }
    return refreshStatus();
  }

  function buildFailed(message) {
    build.state = 'failed';
    build.error = String(message || '未知错误');
    renderStatus(null);
  }

  function updateProgress(pct, detail) {
    var p = Number(pct);
    if (!isFinite(p)) p = 0;
    var bar = html('search-build-bar');
    if (bar && bar.style) {
      bar.style.width = Math.max(0, Math.min(100, Math.round(p * 100))) + '%';
    }
    var d = html('search-build-detail');
    if (d && detail) d.textContent = String(detail);
  }

  // =========================================================================
  // 构建 / 刷新索引（SSE）
  //
  // `api.searchBuild` 不带 abort signal：构建是一次写库操作，不能被别处的
  // `cancelPending()` 中途掐断（那会留下半成品索引）。
  // 404/409 之类非 2xx 会先被查出来并**按失败处理**（绝不能"请求失败但界面
  // 看起来在正常构建"）。
  // =========================================================================

  function handleSseChunk(chunk) {
    var ev = /(?:^|\n)event:\s*([A-Za-z]+)/.exec(chunk);
    if (!ev) return;
    var dt = /(?:^|\n)data:\s*(.*)/.exec(chunk);
    var payload = {};
    if (dt) {
      try {
        payload = JSON.parse(dt[1]);
      } catch (e) {
        payload = {};                        // 非 JSON 载荷：只当进度事件处理
      }
    }
    var name = ev[1];
    if (name === 'error') {
      // T9-A1：失败必须**盖掉**"索引就绪"，并留下可重试的入口
      buildFailed(payload.message || payload.error || '构建失败（服务端未给出原因）');
      return;
    }
    if (name === 'done') {
      updateProgress(1, '构建完成');
      buildCompleted();
      return;
    }
    if (name === 'progress') {
      updateProgress(payload.progress, payload.detail || payload.message || '');
      return;
    }
    if (name === 'heartbeat') {
      // 服务端 30 秒无进展时的心跳，**只**用来证明连接还活着。
      // 心跳载荷里没有 `progress`；若把它也交给 `updateProgress`，
      // `Number(undefined) → NaN → 0` 会把进度条静默打回 0%（看起来"倒退了"）。
      if (build.state === 'running') {
        var d = html('search-build-detail');
        if (d && d.textContent === '等待开始…') d.textContent = '构建仍在进行…';
      }
      return;
    }
    // 其它事件（例如别的 SSE 端点用的 `select`）在构建流程里没有意义，忽略。
  }

  function pumpStream(body) {
    var reader = body.getReader();
    var decoder = new TextDecoder();
    var buf = '';
    function pump() {
      return reader.read().then(function (r) {
        if (r.done) {
          // 流结束却没收到 done / error：**不得**假定成功
          if (build.state === 'running') {
            buildFailed('构建流意外结束（没有收到完成事件）');
          }
          return undefined;
        }
        buf += decoder.decode(r.value, { stream: true });
        var chunks = buf.split('\n\n');
        buf = chunks.pop();
        chunks.forEach(handleSseChunk);
        return pump();
      });
    }
    return pump();
  }

  function runBuild(body) {
    buildStarted();
    return apiClient.searchBuild(body || {}).then(function (resp) {
      if (!resp || resp.ok === false) {
        var status = (resp && resp.status) ? resp.status : 0;
        var fallback = '构建请求被拒绝'
          + (status ? ('（HTTP ' + status + '）') : '');
        if (!resp || typeof resp.text !== 'function') {
          buildFailed(fallback);
          return undefined;
        }
        return resp.text().then(function (t) {
          var msg = fallback;
          try {
            var j = JSON.parse(t);
            if (j && j.error) msg = j.error;
          } catch (e) { /* 非 JSON 错误体：用状态码说明 */ }
          buildFailed(msg);
          return undefined;
        });
      }
      if (!resp.body || typeof resp.body.getReader !== 'function') {
        // 浏览器不支持流式读取 → 退回"刷新状态"，不假装有进度
        return refreshStatus();
      }
      return pumpStream(resp.body);
    }).catch(function (e) {
      buildFailed(messageOf(e));
    });
  }

  // =========================================================================
  // 结果区
  // =========================================================================

  function noteBox(notes) {
    if (!notes.length) return '';
    return '<div class="search-notes">' + notes.map(function (n) {
      return '<div class="search-note">⚠ ' + SR.escapeHtml(n) + '</div>';
    }).join('') + '</div>';
  }

  function indexCaveat() {
    // 结果区自己也带一条索引层提示：用户看的是结果列表，不是上面的状态条。
    if (!lastStatus) return '';
    return SR.statusNotice(lastStatus);
  }

  function emptyHtml(data) {
    var st = lastStatus;
    var notice = indexCaveat();
    // T16：结果被截断、而这一页落在保留窗口之外 ⇒ **不是**"没有匹配的消息"。
    // 判据在 `SearchRender.truncatedPageNotice`（纯函数，`truncated` 结构化字段驱动），
    // 放在最前面是因为它是对"本页为空"最精确、也最不该被别的文案盖住的解释。
    var truncated = SR.truncatedPageNotice(data);
    if (truncated) {
      return '<div class="search-empty-warn">⚠ ' + SR.escapeHtml(truncated) + '</div>';
    }
    if (st && st.text_ready === false) {
      // T9-A1：**不能**把"0 条结果"呈现成"没有这条消息"。
      // "静默返回 0 条"是本页**自己** text_ready 判据的专属文案（状态条里同一句话），
      // 好让加载守卫分别钉住这两处：只靠 `SearchRender.statusNotice` 的通用告警兜底
      // 的话，判据被换成 `ready` 不会有任何断言变红（实测过，确实测不出来）。
      return '<div class="search-empty-warn">⚠ 全文索引未构建或不可用：'
        + '关键词搜索会静默返回 0 条，本次无法确认是否真的没有这条消息。'
        + '请先点上方「一键构建」建立索引后再搜关键词。</div>';
    }
    if (notice) {
      return '<div class="search-empty-warn">⚠ 本次没有命中，但索引不完整'
        + '（见上方告警）：可能有消息根本没有进索引，因此不能据此判断'
        + '「没有这条消息」。</div>';
    }
    if (!st) {
      return '<div class="empty-msg">没有匹配的消息'
        + '<div class="search-empty-note">（索引状态未知：未能确认索引是否完整）</div></div>';
    }
    return '<div class="empty-msg">没有匹配的消息</div>';
  }

  function resultCard(f) {
    // `f.chatNameHtml` / `f.senderNameHtml` / `f.html` 都已经由 `SearchRender`
    // 转义过（纯函数层负责），这里直接进 `innerHTML` 是安全的 —— 正文**绝不**原样插入。
    return '<div class="search-result">'
      + '<div class="sr-head">'
      + '<span class="sr-chat">' + f.chatNameHtml + '</span>'
      + (f.isGroup ? '<span class="label-chip sr-group">群聊</span>' : '')
      + '<span class="label-chip">' + SR.escapeHtml(f.typeLabel) + '</span>'
      + '<span class="sr-sender">' + f.senderNameHtml + '</span>'
      + '<span class="sr-time">' + SR.escapeHtml(f.timeText) + '</span>'
      + '</div>'
      + '<div class="sr-snippet">' + f.html + '</div>'
      + (f.href
          ? '<div class="sr-actions"><a class="btn btn-secondary" href="'
            + f.href + '">打开会话</a></div>'
          : '')
      + '</div>';
  }

  function pageButton(page, text, disabled) {
    return '<button class="pagination-btn" data-page="' + page + '"'
      + (disabled ? ' disabled' : '') + '>' + text + '</button>';
  }

  function renderResults(body) {
    var data = (body && typeof body === 'object') ? body : {};
    var results = Array.isArray(data.results) ? data.results : [];
    var parsed = (data.parsed && typeof data.parsed === 'object') ? data.parsed : {};

    var notes = [];
    var caveat = indexCaveat();
    if (caveat) notes.push(caveat);

    // T9-A4：慢路径必须在结果区说清楚"慢在哪、慢多少"（判据在 SearchRender.slowNotice）
    var slow = SR.slowNotice(data);
    if (slow) notes.push(slow);

    if (data.used_fallback) {
      notes.push('索引不可用，本次已降级直扫原始消息表（较慢，结果应当一致）。');
    }
    if (data.sender_filter_unsupported) {
      notes.push('降级路径不支持「发送者」筛选：本次没有按发送者过滤，'
                 + '结果可能比预期多。请先构建索引。');
    }
    if (data.fallback_text_only) {
      // Task 6 追加的静默假阴性信号：降级语料只含"有正文"的消息，
      // 按类型/日期筛选时会少给甚至给空 —— 这不是"没有这类消息"。
      notes.push('降级扫描只看得到有正文的消息：图片 / 语音 / 视频等无正文消息'
                 + '不在扫描范围内（按类型或日期筛选时尤其明显）。');
    }
    if (data.regex_degraded) {
      notes.push('正则无法用于索引预筛，本次已退化为全量扫描。');
    }
    (Array.isArray(parsed.errors) ? parsed.errors : []).forEach(function (e) {
      var tok = (e && e.token) ? String(e.token) : '';
      var msg = (e && e.message) ? String(e.message) : String(e);
      notes.push('查询语法：' + (tok ? tok + ' — ' : '') + msg);
    });
    (Array.isArray(data.warnings) ? data.warnings : []).forEach(function (w) {
      notes.push(String(w));
    });

    var total = (typeof data.total === 'number') ? data.total : results.length;
    var page = (typeof data.page === 'number' && data.page > 0) ? data.page : 1;
    var totalPages = (typeof data.total_pages === 'number' && data.total_pages > 0)
      ? data.total_pages : 1;

    // T19：**可翻页上限**。`total_pages` 是 API 给的**精确值**（事实，一字不改），
    // 但引擎为控内存只保留了前 `retained_rows` 条 ⇒ 它之后那些页点进去**必然是空的**
    // （Task 16 只保证"不静默"，没解决"能点进去"）。所以导航上限压到
    // `ceil(retained_rows / per_page)`，并在分页条旁边把这件事实说清楚。
    // 判据**只看结构化字段**：`truncated === true` 且 `retained_rows` 是个数。
    // `truncated` 为假 / 字段缺失（旧引擎、SQL 分页路径）⇒ `maxPage === totalPages`，
    // 行为与改造前**逐字相同**（防误报：绝不把本来有数据的页禁掉）。
    var perPage = (typeof data.per_page === 'number' && data.per_page > 0)
      ? data.per_page : PER_PAGE;
    var maxPage = totalPages;
    var retained = Number(data.retained_rows);
    if (data.truncated === true && isFinite(retained)) {
      var cap = Math.max(1, Math.ceil(retained / perPage));
      if (cap < maxPage) maxPage = cap;
    }

    var out = noteBox(notes);
    out += '<div class="search-summary-row">'
      + '<div class="search-summary">共 ' + total + ' 条，第 ' + page + '/'
      + totalPages + ' 页'
      + (Number(data.elapsed_ms) > 0 ? ' · 耗时 ' + seconds(data.elapsed_ms) + ' 秒' : '')
      + (Number(data.candidate_rows) > 0
          ? ' · 扫描 ' + thousands(data.candidate_rows) + ' 条' : '')
      + '</div>';
    if (results.length) {
      out += '<div class="search-export-bar" id="search-export-bar">'
        + '<span class="search-export-label">导出命中</span>'
        + '<button type="button" class="btn btn-secondary search-export-btn" data-fmt="xlsx">Excel</button>'
        + '<button type="button" class="btn btn-secondary search-export-btn" data-fmt="html">HTML</button>'
        + '<button type="button" class="btn btn-secondary search-export-btn" data-fmt="csv">CSV</button>'
        + '<button type="button" class="btn btn-secondary search-export-btn" data-fmt="txt">TXT</button>'
        + '<span class="search-export-status" id="search-export-status"></span>'
        + '</div>';
    }
    out += '</div>';

    if (!results.length) {
      out += emptyHtml(data);
    } else {
      out += '<div class="search-result-list">';
      results.forEach(function (r) {
        out += resultCard(SR.formatResult(r));
      });
      out += '</div>';
    }

    // T19：截断时在分页条**旁边**标注"仅前 N 页可翻"（分页条本身照旧显示
    // `page / totalPages` 的精确页码 —— API 语义不变，只是不再让人点进去）。
    if (maxPage < totalPages) {
      out += '<div class="search-notes"><div class="search-note">⚠ 仅前 ' + maxPage
        + ' 页可翻（共 ' + total + ' 条命中，引擎为控内存只保留了前 ' + retained
        + ' 条）—— 请增加筛选条件缩小范围。</div></div>';
    }

    if (totalPages > 1) {
      out += '<div class="contacts-pagination"><div class="pagination-btns">'
        + pageButton(page - 1, '‹', page <= 1)
        + '<span class="pagination-info">' + page + ' / ' + totalPages + '</span>'
        + pageButton(page + 1, '›', page >= maxPage)
        + '</div></div>';
    }

    els.results.innerHTML = out;
    if (typeof els.results.querySelectorAll === 'function') {
      Array.prototype.forEach.call(
        els.results.querySelectorAll('[data-page]'), function (b) {
          b.addEventListener('click', function () {
            doSearch(parseInt(b.dataset && b.dataset.page, 10));
          });
        });
      Array.prototype.forEach.call(
        els.results.querySelectorAll('.search-export-btn'), function (b) {
          b.addEventListener('click', function () {
            exportHits(b.getAttribute('data-fmt') || 'xlsx');
          });
        });
    }
  }

  var _exportSse = null;

  function exportHits(fmt) {
    var q = (els.syntax.value || '').trim();
    if (!q) {
      els.results.innerHTML = '<div class="empty-msg">请输入查询条件后再导出</div>';
      return;
    }
    if (typeof SseProgress === 'undefined') {
      alert('导出组件未加载，请强制刷新页面后重试');
      return;
    }
    var statusEl = $('search-export-status');
    var setStatus = function (t) {
      if (statusEl) statusEl.textContent = t || '';
    };
    if (_exportSse) {
      try { _exportSse.stop(); } catch (e) { /* ignore */ }
    }
    setStatus('导出中…');
    Array.prototype.forEach.call(
      els.results.querySelectorAll('.search-export-btn'), function (b) {
        b.disabled = true;
      });

    _exportSse = new SseProgress('/api/export/search', {
      body: { q: q, format: fmt },
      onProgress: function (p) {
        setStatus((p && (p.detail || p.message))
          ? String(p.detail || p.message) : '导出中…');
      },
      onDone: function (payload) {
        var r = (payload && payload.result) ? payload.result : (payload || {});
        Array.prototype.forEach.call(
          els.results.querySelectorAll('.search-export-btn'), function (b) {
            b.disabled = false;
          });
        if (!r.msg_count) {
          setStatus(r.note || '没有可导出的消息');
          return;
        }
        setStatus('已导出 ' + r.msg_count + ' 条'
          + (r.note ? ' · ' + r.note : ''));
        if (r.download_url) {
          window.location.href = r.download_url;
        }
      },
      onError: function (err) {
        Array.prototype.forEach.call(
          els.results.querySelectorAll('.search-export-btn'), function (b) {
            b.disabled = false;
          });
        setStatus('导出失败：' + (err && err.message ? err.message : String(err)));
      },
    });
    _exportSse.start();
  }

  function doSearch(page) {
    var q = (els.syntax.value || '').trim();
    if (!q) {
      els.results.innerHTML = '<div class="empty-msg">请输入查询条件</div>';
      return undefined;
    }
    var p = parseInt(page, 10);
    if (!isFinite(p) || p < 1) p = 1;

    // T9-A4：慢查询（退化正则 / 仅排除词 / 大命中关键词）实测 3.3–12 秒，
    // 必须**在返回前**就给出进度反馈，不能让用户以为页面卡死。
    els.results.innerHTML = '<div class="loading"><div class="loading-icon">⏳</div>'
      + '搜索中…<div class="search-slow-hint">正则、仅排除词、大命中关键词等查询'
      + '可能需要 10 秒以上，请耐心等待。</div></div>';

    // 注意：这里**不**等状态请求（T9-A2）。
    return apiClient.search(q, { page: p, per_page: PER_PAGE })
      .then(function (body) {
        renderResults(body);
      })
      .catch(function (e) {
        els.results.innerHTML = '<div class="error-msg">⚠ '
          + SR.escapeHtml(messageOf(e)) + '</div>';
      });
  }

  els.run.addEventListener('click', function () { doSearch(1); });
  if (els.input && typeof els.input.addEventListener === 'function') {
    els.input.addEventListener('keydown', function (e) {
      if (e && e.key === 'Enter') {
        onFormEdited();
        doSearch(1);
      }
    });
  }
  els.syntax.addEventListener('keydown', function (e) {
    if (e && e.key === 'Enter') doSearch(1);
  });

  // =========================================================================
  // 初始化
  // =========================================================================

  loadLabels();       // 失败静默（见 loadLabels 的 catch）
  refreshStatus();    // 失败只隐藏状态条；**不阻塞**任何交互
  onFormEdited();     // 让语法框与表单从同一份状态出发

  window.SearchApp = {
    initialized: true,
    els: els,
    renderStatus: renderStatus,
    renderResults: renderResults,
    buildStarted: buildStarted,
    buildCompleted: buildCompleted,
    buildFailed: buildFailed,
    search: doSearch,
    formToSyntax: onFormEdited,
    syntaxToForm: onSyntaxEdited,
    sse: handleSseChunk,
  };
})();
