/* ⚠ 这是 **Task 21 改造前的 `dbdir.js` 逐字节快照**（冻结基线），**不是**任何页面加载的代码。
 *
 * 用途只有一个：`tests/js/dbdir_picker_guard.js dump-legacy` 会把「旧格式响应
 * （没有 tier / reason / recommended_path / probe / wechat_running）」下的**全部可观测
 * 状态**（option 值/文本/选中项、select 的值、输入框的值、#dir-hint 的 HTML、请求 URL）
 * 导成 JSON；`tests/test_dbdir_picker_js.py` 分别用**本基线源**与**当前源**跑一次，
 * 断言两份 JSON 逐字节相同 —— 这就是"旧引擎下行为与改造前逐字相同"的机器证据。
 *
 * 原始文件：`git show HEAD:src/web/static/js/dbdir.js`
 * SHA-256 = 27ffcc41ddeffa3ef70a20d7cc74d6b5eed8ef4d445840df2e260b20f6490101
 * （本文件与该 SHA-256 一致：逐字节快照，含文件尾。）
 *
 * 请不要"顺手"把它同步成当前实现 —— 一旦同步，上面那个等价性断言就退化成恒真。
 */
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

  fill(dirs, current) {
    const sel = this.select();
    const inp = this.input();
    if (!sel) {
      if (inp && !inp.value && current) inp.value = current;
      return;
    }
    sel.innerHTML = "";
    const list = dirs || [];
    list.forEach((d) => {
      const o = document.createElement("option");
      o.value = d.db_path;
      const wxid = d.wxid ? " (" + d.wxid + ")" : "";
      const size = d.size_mb ? "  " + d.size_mb + " MB" : "";
      o.textContent = d.db_path + wxid + size;
      sel.appendChild(o);
    });
    const chosen = (inp && inp.value) || current || (list[0] && list[0].db_path) || "";
    if (chosen) {
      sel.value = chosen;
      if (inp && !inp.value) inp.value = chosen;
    }
  }

  load(mode) {
    return fetch("/api/keys/dirs?mode=" + (mode || "auto"))
      .then((r) => r.json())
      .then((data) => {
        this.fill(data.dirs || [], data.current || "");
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
        this.fill(data.dirs || [], data.current || "");
        this.setHint(n
          ? ("🔍 深度搜索完成，找到 " + n + " 个数据目录（已填入，可直接开始）")
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
