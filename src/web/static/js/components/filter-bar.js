// filter-bar.js — Message filter bar component
// Provides filter UI: keyword search, date range, message type, sender.
// Updates Store.data.filters and emits 'filter-changed' on input.
// Includes FilterBar class extending Component.

class FilterBar extends Component {
  template(data) {
    const filters = (data && data.filters) || {};
    return `
      <input type="text" class="filter-keyword" placeholder="搜索消息内容..." autocomplete="off" value="${escapeAttr(filters.keyword || '')}">
      <input type="date" class="filter-date-start" title="开始日期" value="${escapeAttr(filters.dateStart || '')}">
      <span class="date-sep">&mdash;</span>
      <input type="date" class="filter-date-end" title="结束日期" value="${escapeAttr(filters.dateEnd || '')}">
      <select class="filter-type">
        <option value="">全部类型</option>
        <option value="1">文本</option>
        <option value="3">图片</option>
        <option value="34">语音</option>
        <option value="43">视频</option>
        <option value="48">位置</option>
        <option value="49">链接/转发</option>
        <option value="42">名片</option>
        <option value="47">表情</option>
        <option value="6">文件</option>
        <option value="50">网络电话</option>
        <option value="10000,10002">系统消息</option>
      </select>
      <select class="filter-sender"><option value="">全部发送者</option></select>
      <button type="button" class="btn-filter-go" title="按当前条件检索">检索</button>
      <span class="filter-result-count"></span>`;
  }

  mount() {
    this._timer = null;

    // 日期/类型/发送者变更立即筛选；关键词输入防抖，也可点「检索」
    this.el.addEventListener('input', (e) => {
      const input = e.target.closest('input[type="text"]');
      if (input) this._scheduleFilter();
    });
    this.el.addEventListener('change', (e) => {
      if (e.target.closest('select, input[type="date"]')) this._applyFilters();
    });
    this.el.addEventListener('click', (e) => {
      if (e.target.closest('.btn-filter-go')) {
        clearTimeout(this._timer);
        this._applyFilters();
      }
    });
    this.el.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && e.target.closest('.filter-keyword')) {
        e.preventDefault();
        clearTimeout(this._timer);
        this._applyFilters();
      }
    });
  }

  _scheduleFilter() {
    clearTimeout(this._timer);
    this._timer = setTimeout(() => {
      this._applyFilters();
    }, 300);
  }

  _applyFilters() {
    const keyword = (this.el.querySelector('.filter-keyword') || {}).value || '';
    const dateStart = (this.el.querySelector('.filter-date-start') || {}).value || '';
    const dateEnd = (this.el.querySelector('.filter-date-end') || {}).value || '';
    const msgTypes = (this.el.querySelector('.filter-type') || {}).value || '';
    const sender = (this.el.querySelector('.filter-sender') || {}).value || '';

    Store.data.filters = { keyword, dateStart, dateEnd, msgTypes, sender };
    Store.data.pagination.page = 1;
    Store.emit('filter-changed');
  }

  resetFilters() {
    const kw = this.el.querySelector('.filter-keyword');
    const ds = this.el.querySelector('.filter-date-start');
    const de = this.el.querySelector('.filter-date-end');
    const ft = this.el.querySelector('.filter-type');
    const fs = this.el.querySelector('.filter-sender');
    const rc = this.el.querySelector('.filter-result-count');
    if (kw) kw.value = '';
    if (ds) ds.value = '';
    if (de) de.value = '';
    if (ft) ft.value = '';
    if (fs) fs.value = '';
    if (rc) rc.textContent = '';
    Store.data.filters = { keyword: '', dateStart: '', dateEnd: '', msgTypes: '', sender: '' };
    // Don't emit filter-changed here — caller triggers reload
  }

  populateSenderFilter(dist) {
    const el = this.el.querySelector('.filter-sender');
    if (!el) return;
    el.innerHTML = '<option value="">全部发送者</option>';
    for (const [sender, info] of Object.entries(dist || {})) {
      const displayName = (info && info.name) || sender;
      const count = (info && info.count) || 0;
      const val = String(sender).replace(/"/g, '&quot;');
      el.innerHTML += `<option value="${val}">${escapeHtml(displayName)} (${count})</option>`;
    }
  }
}
