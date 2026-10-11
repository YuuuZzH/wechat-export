/* dbdir.js — 微信数据目录选择器：自动检测 / 深度搜索 / 记住目录
 *
 * 用法：
 *   new DbDirPicker({ inputId: "cfg-db-dir" });
 * 需要的元素（都可选，缺省用默认 id）：
 *   #cfg-db-dir        路径输入框（表单提交时读它）
 *   #cfg-db-dir-list   检测结果下拉（选中即填入输入框）
 *   #btn-deep          「深度搜索」按钮
 *   #btn-use-dir       「使用该目录」按钮（写入配置，备份/解密/密钥共用）
 *   #dir-hint          提示行
 *
 * 后端契约（`GET /api/keys/dirs?mode=auto|deep`）：
 *   { dirs: [ { db_path, wxid, size_mb, ..., tier, reason, recommended, pids,
 *               active, last_write_min } ],
 *     current, recommended_path, wechat_running,
 *     probe: { t0_ok, t0_elapsed_ms, t1_probed, errors } }
 *
 *   · `dirs` **已由后端排好序**（in_use > locked > recent > config > idle）——
 *     前端按它给的顺序原样渲染，**不重排**；本文件只做"读"与"显示"。
 *   · 标注文案（`reason`）由**后端**给出（例如「⭐ 微信进程正在使用（PID 1234）」），
 *     前端只负责显示，**不在 JS 里再造一份文案**（一份事实源）。
 *     前端唯一的自主判断：`⭐` 只给 `in_use` / `locked` 两档（其余档直接前置 reason）。
 *   · 旧引擎（响应里没有 tier / reason / recommended_path / probe / wechat_running）下，
 *     本文件的行为与改造前**逐字相同**：#dir-hint 一个字都不动，option 文本仍是
 *     `db_path (wxid) size MB`。
 */
class DbDirPicker {
  constructor(options) {
    const opts = options || {};
    this.inputId = opts.inputId || "cfg-db-dir";
    this.selectId = opts.selectId || "cfg-db-dir-list";
    this.searchBtnId = opts.searchBtnId || "btn-deep";
    this.useBtnId = opts.useBtnId || "btn-use-dir";
    this.hintId = opts.hintId || "dir-hint";
    this.autoload = opts.autoload !== false;
    this.onChange = opts.onChange || null;
    this.bind();
    if (this.autoload) this.load("auto");
  }

  el(id) { return document.getElementById(id); }
  input() { return this.el(this.inputId); }
  select() { return this.el(this.selectId); }

  setHint(html) {
    const h = this.el(this.hintId);
    if (h) h.innerHTML = html;
  }

  bind() {
    const sel = this.select();
    if (sel) {
      sel.addEventListener("change", () => {
        if (!sel.value) return;
        const inp = this.input();
        if (inp) inp.value = sel.value;
        if (this.onChange) this.onChange(sel.value);
      });
    }
    const searchBtn = this.el(this.searchBtnId);
    if (searchBtn) searchBtn.addEventListener("click", () => this.deepSearch());
    const useBtn = this.el(this.useBtnId);
    if (useBtn) useBtn.addEventListener("click", () => this.useDir());
    const inp = this.input();
    if (inp) {
      inp.addEventListener("change", () => {
        if (this.onChange) this.onChange(inp.value);
      });
    }
  }

  fill(dirs, current, recommendedPath) {
    const sel = this.select();
    const inp = this.input();
    const list = dirs || [];
    const recommended = (recommendedPath || "").trim();
    if (!sel) {
      // 没有下拉时至少把推荐项 / 当前值填进输入框（只在输入框为空时填，与改造前一致）
      if (inp && !inp.value) {
        const only = recommended || current;
        if (only) inp.value = only;
      }
      return;
    }
    sel.innerHTML = "";
    // 顺序**照后端给的**（后端已按 in_use > locked > recent > config > idle 排好），
    // 这里不重排 —— 前端只负责把标注渲染出来。
    list.forEach((d) => {
      const o = document.createElement("option");
      o.value = d.db_path;
      o.textContent = this.optionText(d);
      sel.appendChild(o);
    });
    const first = (list[0] && list[0].db_path) || "";
    // 默认选中：推荐项（recommended_path）> 当前配置 > 第一条（只选，不重排）
    const chosen = (inp && inp.value) || recommended || current || first;
    if (chosen) {
      sel.value = chosen;
      if (inp && !inp.value) inp.value = chosen;
    }
  }

  /** 标注：`⭐` 只给 in_use / locked 两档，其余档直接显示后端给的 reason。
   *
   * reason 文本**原样来自后端**（一份事实源）；本函数只做两件事：给这两档补 `⭐`、
   * 以及在"只有 tier 没有 reason"（半新后端）时退化成只显示 `⭐`。
   */
  mark(d) {
    const tier = d && d.tier ? String(d.tier) : "";
    const reason = d && d.reason ? String(d.reason) : "";
    const star = (tier === "in_use" || tier === "locked") ? "⭐" : "";
    let label = reason;
    if (star && label && label.indexOf("⭐") !== 0) label = star + " " + label;
    if (!label) label = star;
    return label;
  }

  /** option 文本：`[标注 · ]db_path (wxid)  N MB`（无标注时与改造前逐字相同）。 */
  optionText(d) {
    const mark = this.mark(d);
    const wxid = d.wxid ? " (" + d.wxid + ")" : "";
    const size = d.size_mb ? "  " + d.size_mb + " MB" : "";
    return (mark ? mark + " · " : "") + d.db_path + wxid + size;
  }

  /** 推荐项路径：优先顶层 `recommended_path`；没有再取 dirs 里第一个 `recommended`。 */
  recommendedPath(data) {
    if (!data) return "";
    const top = (data.recommended_path || "").trim();
    if (top) return top;
    const hit = (data.dirs || []).filter((d) => d && d.recommended)[0];
    return (hit && hit.db_path) || "";
  }

  samePath(a, b) {
    if (!a || !b) return false;
    return String(a).toLowerCase() === String(b).toLowerCase();
  }

  /** 把 `probe` / `wechat_running` / `recommended_path` 汇总成一句提示。
   *
   * **只有**后端给了这些新字段才返回字符串 ⇒ 旧引擎下 `#dir-hint` 一个字都不动
   * （页面静态文案原样保留）。
   *
   * `probe.errors` **不等于故障**（裁决 R47）：真机正常状态下也会有"某条探测线不可用"，
   * 一律报警会训练用户忽略告警。所以只在 `t0_ok === false`（最强证据丢了）或
   * 一个目录都没找到时才升级为「⚠ 进程占用分析不可用…」，其余只给中性的
   * 「（另有 N 项探测不可用，不影响推荐）」——**仍然不静默**，但不喊狼来了。
   */
  summarize(data) {
    if (!data) return "";
    const hasNew = data.probe !== undefined || data.wechat_running !== undefined
      || (data.recommended_path !== undefined && data.recommended_path !== null);
    if (!hasNew) return "";
    const dirs = data.dirs || [];
    const recPath = this.recommendedPath(data);
    let idx = -1;
    if (recPath) {
      dirs.forEach((d, i) => {
        if (idx === -1 && d && this.samePath(d.db_path, recPath)) idx = i;
      });
    }
    const rec = idx >= 0 ? dirs[idx] : null;
    const parts = [];
    parts.push(dirs.length
      ? "✅ 检测到 " + dirs.length + " 个数据目录"
      : "⚠ 未检测到数据目录");
    let line = data.wechat_running ? "微信正在运行" : "微信未在运行";
    if (rec) {
      const mark = this.mark(rec);
      line += "，推荐第 " + (idx + 1) + " 个" + (mark ? "（" + mark + "）" : "");
    }
    parts.push(line);
    const probe = data.probe || {};
    const errs = (probe.errors || []).length;
    // 控制方裁决 R47（2026-09-22）：`errors` 非空**不等于**故障。
    // 真机正常状态下也会出现"某条探测线不可用"（本机实测 errors=1 而推荐完全正确）⇒
    // 一律报警会把用户训练成忽略告警（与 known-issues #27 的 `fts_coverage` 同理）。
    // 因此只在**推荐据以成立的最强证据丢了**或**一个目录都没找到**时才升级为告警：
    const lostT0 = probe.t0_ok === false;
    if (lostT0 || dirs.length === 0) {
      parts.push("⚠ 进程占用分析不可用"
        + (errs ? "（" + errs + " 项探测失败）" : "")
        + "：推荐可能不准确");
    } else if (errs) {
      parts.push("（另有 " + errs + " 项探测不可用，不影响推荐）");
    }
    return parts.join("；");
  }

  load(mode) {
    return fetch("/api/keys/dirs?mode=" + (mode || "auto"))
      .then((r) => r.json())
      .then((data) => {
        this.fill(data.dirs || [], data.current || "", this.recommendedPath(data));
        const summary = this.summarize(data);
        if (summary) this.setHint(summary);
        return data;
      })
      .catch(() => null);
  }

  deepSearch() {
    const btn = this.el(this.searchBtnId);
    if (btn) btn.disabled = true;
    this.setHint("🔍 正在深度搜索磁盘（最多 45 秒，请稍候）...");
    fetch("/api/keys/dirs?mode=deep")
      .then((r) => r.json())
      .then((data) => {
        if (btn) btn.disabled = false;
        const n = (data.dirs || []).length;
        this.fill(data.dirs || [], data.current || "", this.recommendedPath(data));
        // 新后端才追加推荐汇总（旧后端下与改造前逐字相同，见 summarize()）
        const summary = n ? this.summarize(data) : "";
        this.setHint(n
          ? ("🔍 深度搜索完成，找到 " + n + " 个数据目录（已填入，可直接开始）"
             + (summary ? "<br>" + summary : ""))
          : "🔍 仍未找到。请在微信「设置 → 文件管理 → 打开文件夹」里找到 xwechat_files\\&lt;账号&gt;\\db_storage 路径，粘贴到上面的输入框。");
      })
      .catch((e) => {
        if (btn) btn.disabled = false;
        this.setHint("深度搜索失败: " + e);
      });
  }

  useDir() {
    const inp = this.input();
    const path = inp ? inp.value.trim() : "";
    if (!path) { alert("请先填写或选择一个微信数据目录"); return; }
    fetch("/api/keys/dbdir", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: path }),
    })
      .then((r) => r.json())
      .then((data) => {
        if (data.error) { alert("失败: " + (data.message || data.error)); return; }
        if (inp) inp.value = data.dbDir;
        this.setHint("✅ 已记住数据目录：<code>" + data.dbDir + "</code>（备份 / 解密 / 密钥都会使用它）");
      })
      .catch((e) => alert("请求失败: " + e));
  }
}
