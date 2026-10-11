// pagination.js — Pagination bar component
// Provides a Pagination class extending Component with template + mount,
// plus a backward-compatible renderPagination global for app.js.

class Pagination extends Component {
  template(data) {
    if (!data || !data.total_pages) {
      return `
        <div class="pagination-controls">
          <button class="pg-btn" disabled title="首页">&#171;</button>
          <button class="pg-btn" disabled title="上页">&#8249;</button>
          <span class="page-numbers"></span>
          <button class="pg-btn" disabled title="下页">&#8250;</button>
          <button class="pg-btn" disabled title="末页">&#187;</button>
        </div>
        <div class="pagination-jump">
          <span>&#x1f4c5;</span>
          <input type="date" class="jump-date" title="跳转到日期">
          <button class="pg-btn btn-jump-date">跳转</button>
        </div>
        <div class="pagination-info">暂无消息</div>`;
    }
    const total = data.total_pages, cur = data.page;
    let start = Math.max(1, cur - 2), end = Math.min(total, cur + 2);
    if (end - start < 4) {
      if (start === 1) end = Math.min(total, start + 4);
      else start = Math.max(1, end - 4);
    }
    let pagesHtml = '';
    for (let p = start; p <= end; p++) {
      pagesHtml += `<button class="pg-btn${p === cur ? ' active' : ''}" data-page="${p}">${p}</button>`;
    }
    if (start > 1) {
      pagesHtml = '<button class="pg-btn" data-page="1">1</button><span style="color:#484f58;padding:0 2px">...</span>' + pagesHtml;
    }
    if (end < total) {
      pagesHtml += '<span style="color:#484f58;padding:0 2px">...</span><button class="pg-btn" data-page="' + total + '">' + total + '</button>';
    }
    return `
      <div class="pagination-controls">
        <button class="pg-btn" data-action="first" ${cur <= 1 ? 'disabled' : ''} title="首页">&#171;</button>
        <button class="pg-btn" data-action="prev" ${cur <= 1 ? 'disabled' : ''} title="上页">&#8249;</button>
        <span class="page-numbers">${pagesHtml}</span>
        <button class="pg-btn" data-action="next" ${cur >= total ? 'disabled' : ''} title="下页">&#8250;</button>
        <button class="pg-btn" data-action="last" ${cur >= total ? 'disabled' : ''} title="末页">&#187;</button>
      </div>
      <div class="pagination-jump">
        <span>&#x1f4c5;</span>
        <input type="date" class="jump-date" title="跳转到日期">
        <button class="pg-btn btn-jump-date">跳转</button>
      </div>
      <div class="pagination-info">第 ${data.page}/${data.total_pages} 页 · 共 ${data.total.toLocaleString()} 条</div>`;
  }

  mount() {
    this.el.addEventListener('click', (e) => {
      const actionBtn = e.target.closest('.pg-btn[data-action]');
      if (actionBtn) {
        Store.emit('pagination-action', actionBtn.dataset.action);
        return;
      }
      const pageBtn = e.target.closest('.pg-btn[data-page]');
      if (pageBtn) {
        Store.emit('pagination-go', parseInt(pageBtn.dataset.page));
        return;
      }
      if (e.target.closest('.btn-jump-date')) {
        const jumpInput = this.el.querySelector('.jump-date');
        if (jumpInput && jumpInput.value) {
          Store.emit('pagination-jump-date', jumpInput.value);
        }
      }
    });
  }
}

// Backward-compatible global for app.js (updates fixed DOM elements directly)
function renderPagination(pg) {
  if (!document.getElementById('pagination-info') || !document.getElementById('btn-first') || !document.getElementById('page-numbers')) return;
  if (!pg.total_pages) {
    document.getElementById('pagination-info').textContent = '暂无消息';
    ['btn-first','btn-prev','btn-next','btn-last'].forEach(id => document.getElementById(id).disabled = true);
    document.getElementById('page-numbers').innerHTML = '';
    return;
  }
  document.getElementById('pagination-info').textContent = `第 ${pg.page}/${pg.total_pages} 页 · 共 ${pg.total.toLocaleString()} 条`;
  ['btn-first','btn-prev'].forEach(id => document.getElementById(id).disabled = pg.page <= 1);
  ['btn-next','btn-last'].forEach(id => document.getElementById(id).disabled = pg.page >= pg.total_pages);

  const pagesEl = document.getElementById('page-numbers');
  let html = '';
  const total = pg.total_pages, cur = pg.page;
  let start = Math.max(1, cur - 2), end = Math.min(total, cur + 2);
  if (end - start < 4) {
    if (start === 1) end = Math.min(total, start + 4);
    else start = Math.max(1, end - 4);
  }
  for (let p = start; p <= end; p++) {
    html += `<button class="pg-btn${p===cur?' active':''}" onclick="goToPage(${p})">${p}</button>`;
  }
  if (start > 1) html = `<button class="pg-btn" onclick="goToPage(1)">1</button><span style="color:#484f58;padding:0 2px">...</span>` + html;
  if (end < total) html += `<span style="color:#484f58;padding:0 2px">...</span><button class="pg-btn" onclick="goToPage(${total})">${total}</button>`;
  pagesEl.innerHTML = html;
}
