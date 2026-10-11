// contact-list.js — contact sidebar rendering
class ContactList extends Component {
  template(data) {
    // data = { contacts, activeId, loading, searchQuery }
    if (data.loading) {
      return '<div class="loading"><div class="loading-icon">⏳</div>正在加载联系人...</div>';
    }
    if (!data.contacts || !data.contacts.length) {
      if (data.searchQuery) {
        return '<div class="empty-msg">未找到匹配的联系人</div>';
      }
      return '<div class="empty-msg">暂无聊天记录可用<div class="hint">请先在主菜单执行 <b>[3] 解密数据库</b><br>确保微信已登录并完成解密后再试</div></div>';
    }
    return data.contacts.map(c => `
      <div class="contact-item${c.id === data.activeId ? ' active' : ''}" data-id="${escapeAttr(c.id)}">
        <img class="contact-avatar" src="${c.avatar_url || ''}"
             onerror="this.onerror=null;this.outerHTML='<div class=\\'contact-avatar-fallback\\' style=\\'width:40px;height:40px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;font-size:18px;font-weight:700;color:white;flex-shrink:0;background:'+_avatarColor('${c.id.replace(/'/g,"\\'")}')+'\\'>'+_avatarChar('${(c.name||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/'/g,"\\'")}')+'</div>'"
             loading="lazy" alt="" />
        <div class="contact-info">
          <div class="name">${escapeHtml(c.name)}</div>
          <div class="meta">
            <span>${c.msg_count.toLocaleString()} 条</span>
            <span class="type-badge ${c.type}">${c.type === 'group' ? '群' : '人'}</span>
          </div>
        </div>
      </div>`).join('');
  }

  mount() {
    // Click delegation on contact items — emit contact-selected event
    this.el.addEventListener('click', (e) => {
      const item = e.target.closest('.contact-item');
      if (item) {
        Store.emit('contact-selected', item.dataset.id);
      }
    });

    // Search input handler — emit contact-search event with debounce
    const searchInput = document.getElementById('contact-search');
    if (searchInput) {
      searchInput.addEventListener('input', () => {
        clearTimeout(searchInput._t);
        searchInput._t = setTimeout(() => {
          Store.emit('contact-search', searchInput.value.trim());
        }, 300);
      });
    }
  }
}
