// cleanup-list.js — chat list with space bars and checkboxes
class CleanupList extends Component {
  template(data) {
    if (data.loading) {
      return '<div class="loading"><div class="loading-icon">&#x23f3;</div>分析聊天占用空间...</div>';
    }
    if (data.error) {
      return '<div class="error-msg">&#x26a0; ' + escapeHtml(data.error) +
             '<br><button class="btn btn-secondary" style="margin-top:12px" onclick="CleanupStore.emit(\'refresh\')">重试</button></div>';
    }
    if (!data.chats || !data.chats.length) {
      return '<div class="empty-msg">未找到聊天数据<div class="hint">请先执行备份以建立索引</div></div>';
    }
    // Filter by search query
    var filtered = data.chats;
    var q = (data.searchQuery || '').trim().toLowerCase();
    if (q) {
      filtered = data.chats.filter(function(c) {
        return (c.display_name || '').toLowerCase().indexOf(q) !== -1
            || (c.chat_id || '').toLowerCase().indexOf(q) !== -1;
      });
    }
    if (!filtered.length) {
      return '<div class="empty-msg">无匹配结果<div class="hint">尝试其他关键词</div></div>';
    }
    var html = '';
    filtered.forEach(function(c) {
      var isSelected = !!data.selected[c.chat_id];
      var barHtml = '';
      if (c.total_bytes > 0) {
        var imgPct = c.total_bytes > 0 ? Math.round((c.image_bytes / c.total_bytes) * 100) : 0;
        var vidPct = c.total_bytes > 0 ? Math.round((c.video_bytes / c.total_bytes) * 100) : 0;
        var filePct = 100 - imgPct - vidPct;
        if (filePct < 0) filePct = 0;
        barHtml = '<div class="cleanup-bar-track">' +
          (imgPct > 0 ? '<div class="cleanup-bar-seg cleanup-bar-img" style="width:' + imgPct + '%"></div>' : '') +
          (vidPct > 0 ? '<div class="cleanup-bar-seg cleanup-bar-vid" style="width:' + vidPct + '%"></div>' : '') +
          (filePct > 0 ? '<div class="cleanup-bar-seg cleanup-bar-file" style="width:' + filePct + '%"></div>' : '') +
          '</div>';
      }
      var typeBadge = c.is_group
        ? '<span class="type-badge group">群</span>'
        : '<span class="type-badge user">C</span>';
      var checkbox = '<input type="checkbox" class="cleanup-chat-cb" ' +
        (isSelected ? 'checked' : '') + ' data-chat-id="' + escapeAttr(c.chat_id) + '">';
      html += '<div class="cleanup-chat-item' + (isSelected ? ' selected' : '') + '" data-chat-id="' + escapeAttr(c.chat_id) + '">' +
        '<div class="cleanup-chat-top">' +
        checkbox +
        '<div class="cleanup-chat-info">' +
        '<div class="cleanup-chat-name">' + escapeHtml(c.display_name || c.chat_id) + ' ' + typeBadge + '</div>' +
        '<div class="cleanup-chat-meta">' +
        (c.message_count || 0).toLocaleString() + '条消息 · ' +
        (c.media_count || 0) + '个文件' +
        '</div>' +
        '</div>' +
        '<div class="cleanup-chat-size">' + formatSize(c.total_bytes) + '</div>' +
        '</div>' +
        barHtml +
        '</div>';
    });
    return html;
  }

  mount() {
    var self = this;
    this.el.addEventListener('click', function(e) {
      var item = e.target.closest('.cleanup-chat-item');
      if (!item) return;
      if (e.target.classList.contains('cleanup-chat-cb')) {
        CleanupStore.toggleSelect(item.dataset.chatId);
        self._renderState();
        return;
      }
      var chat = CleanupStore.data.chats.find(function(c) { return c.chat_id === item.dataset.chatId; });
      if (chat) {
        CleanupStore.data.activeChat = chat;
        CleanupStore.emit('detail-changed', chat);
      }
    });
    var selectAllEl = document.getElementById('cleanup-select-all');
    if (selectAllEl) {
      selectAllEl.addEventListener('change', function() {
        CleanupStore.selectAll(this.checked);
        self._renderState();
      });
    }
    CleanupStore.on('contacts-loaded', function() { self._renderState(); });
    CleanupStore.on('selection-changed', function() { self._renderState(); });
    CleanupStore.on('refresh', function() { self._renderState(); });
  }

  _renderState() {
    this.render({
      loading: CleanupStore.data.loading,
      error: CleanupStore.data.error,
      chats: CleanupStore.data.chats,
      selected: CleanupStore.data.selected,
      searchQuery: CleanupStore.data.searchQuery,
    });
    var countEl = document.getElementById('cleanup-chat-count');
    if (countEl) {
      countEl.textContent = CleanupStore.data.chats.length + ' 个聊天';
    }
  }
}
