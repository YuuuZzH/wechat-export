// address-book-list.js — contacts page list component
class AddressBookList extends Component {
  template(data) {
    if (data.loading) {
      return '<div class="loading"><div class="loading-icon">&#x23f3;</div>加载通讯录...</div>';
    }
    if (data.error) {
      return '<div class="error-msg">&#x26a0; ' + escapeHtml(data.error) +
             '<br><button class="btn btn-secondary" style="margin-top:12px" onclick="AddressBookStore.emit(\'retry\')">重试</button></div>';
    }
    if (!data.letterGroups || !data.letterGroups.length) {
      if (data.searchQuery) {
        return '<div class="empty-msg">未找到匹配 \'' + escapeHtml(data.searchQuery) + '\' 的联系人</div>';
      }
      return '<div class="empty-msg">contact.db 中未找到联系人<div class="hint">请先执行备份以提取通讯录数据</div></div>';
    }
    var html = '';
    data.letterGroups.forEach(function(group) {
      html += '<div class="addr-section-header">' + escapeHtml(group.letter) + '</div>';
      group.contacts.forEach(function(c) {
        var isActive = data.activeContact && data.activeContact.wxid === c.wxid;
        var subtitle = c.remark || c.nick_name || c.alias || c.wxid;
        var msgBadge = '';
        if (c.msg_count > 0) {
          msgBadge = '<span class="addr-msg-badge">' + c.msg_count.toLocaleString() + '条</span>';
        }
        var labelBadges = '';
        if (c.labels && c.labels.length) {
          labelBadges = '<div class="addr-item-labels">' + c.labels.map(function(name) {
            return '<span class="label-chip">' + escapeHtml(name) + '</span>';
          }).join('') + '</div>';
        }
        var avatarFallbackStyle = 'width:40px;height:40px;border-radius:4px;display:inline-flex;align-items:center;justify-content:center;font-size:18px;font-weight:700;color:white;flex-shrink:0;background:' + _avatarColor(c.wxid || '');
        var avatarChar = _avatarChar(c.display_name || c.wxid);
        var avatarHtml = '<img class="addr-avatar" src="' + escapeAttr(c.avatar_url) + '" loading="lazy" alt=""' +
          ' onerror="this.onerror=null;this.outerHTML=\'<div style=\\\'' + avatarFallbackStyle + '\\\'>' + avatarChar + '</div>\'" />';
        html += '<div class="addr-item' + (isActive ? ' active' : '') + '" data-wxid="' + escapeAttr(c.wxid) + '">' +
          avatarHtml +
          '<div class="addr-item-info">' +
          '<div class="addr-item-name">' + escapeHtml(c.display_name || c.wxid) + '</div>' +
          '<div class="addr-item-sub">' + escapeHtml(subtitle) + '</div>' +
          labelBadges +
          '</div>' +
          msgBadge +
          '</div>';
      });
    });

    // Pagination controls
    if (data.totalPages > 1) {
      var page = data.page || 1;
      var totalPages = data.totalPages || 1;
      html += '<div class="contacts-pagination">';
      html += '<span class="pagination-info">' + data.total.toLocaleString() + ' 人 · 第 ' + page + '/' + totalPages + ' 页</span>';
      html += '<div class="pagination-btns">';
      html += '<button class="pagination-btn" onclick="_contactsGoToPage(1)" ' + (page <= 1 ? 'disabled' : '') + '>&#x23EE;</button>';
      html += '<button class="pagination-btn" onclick="_contactsGoToPage(' + (page - 1) + ')" ' + (page <= 1 ? 'disabled' : '') + '>&#x2039;</button>';
      html += '<button class="pagination-btn" onclick="_contactsGoToPage(' + (page + 1) + ')" ' + (page >= totalPages ? 'disabled' : '') + '>&#x203A;</button>';
      html += '<button class="pagination-btn" onclick="_contactsGoToPage(' + totalPages + ')" ' + (page >= totalPages ? 'disabled' : '') + '>&#x23ED;</button>';
      html += '</div></div>';
    }

    return html;
  }

  mount() {
    var self = this;
    // Tab clicks
    var tabsEl = document.getElementById('contacts-tabs');
    if (tabsEl) {
      tabsEl.addEventListener('click', function(e) {
        var tab = e.target.closest('.contacts-tab');
        if (!tab) return;
        tabsEl.querySelectorAll('.contacts-tab').forEach(function(t) { t.classList.remove('active'); });
        tab.classList.add('active');
        AddressBookStore.set('activeTab', tab.dataset.tab);
      });
    }
    // List item clicks
    this.el.addEventListener('click', function(e) {
      var item = e.target.closest('.addr-item');
      if (!item) return;
      AddressBookStore.selectContact(item.dataset.wxid);
    });
    // Listen for store events
    AddressBookStore.on('contacts-loaded', function() { self._renderState(); });
    AddressBookStore.on('tab-changed', function() { self._renderState(); });
    AddressBookStore.on('contact-selected', function() { self._renderState(); });
    AddressBookStore.on('retry', function() { self._renderState(); });
  }

  _renderState() {
    var state = {
      loading: AddressBookStore.data.loading,
      error: AddressBookStore.data.error,
      searchQuery: AddressBookStore.data.searchQuery,
      letterGroups: AddressBookStore.letterGroups(),
      activeContact: AddressBookStore.data.activeContact,
      activeTab: AddressBookStore.data.activeTab,
      page: AddressBookStore.data.page,
      perPage: AddressBookStore.data.perPage,
      totalPages: AddressBookStore.data.totalPages,
      total: AddressBookStore.data.total,
    };
    this.render(state);
  }
}
