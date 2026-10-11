// cleanup-app.js — bootstrap for the /cleanup page
(function() {
  var listEl = document.getElementById('cleanup-list');
  var detailEl = document.getElementById('cleanup-detail');
  var modalEl = document.getElementById('cleanup-modal-content');

  var listComponent = new CleanupList(listEl);
  var confirmComponent = new CleanupConfirm(modalEl);

  listComponent.mount();
  confirmComponent.mount();
  listComponent.render({ loading: true, error: null, chats: [], selected: {} });

  var modalStage = null;

  CleanupStore.on('modal-cancel', function() {
    modalStage = null;
    CleanupStore.emit('modal-stage-changed', null);
  });

  CleanupStore.on('modal-verify', function() {
    modalStage = 'verify';
    CleanupStore.emit('modal-stage-changed', 'verify');
  });

  CleanupStore.on('modal-execute', function() {
    modalStage = 'executing';
    CleanupStore.emit('modal-stage-changed', 'executing');
    executeDeletion();
  });

  CleanupStore.on('modal-done', function() {
    modalStage = null;
    CleanupStore.emit('modal-stage-changed', null);
    loadAnalysis();
  });

  function updateBottombar() {
    var bar = document.getElementById('cleanup-bottombar');
    var text = document.getElementById('cleanup-bottombar-text');
    var btn = document.getElementById('cleanup-preview-btn');
    if (!bar || !text || !btn) return;
    var count = CleanupStore.selectedCount();
    var bytes = CleanupStore.selectedBytes();
    if (count > 0) {
      bar.style.display = 'flex';
      text.textContent = '已选 ' + count + ' 个聊天 · 预计释放 ' + formatSize(bytes);
      btn.disabled = false;
    } else {
      bar.style.display = 'none';
    }
  }

  CleanupStore.on('selection-changed', updateBottombar);

  var previewBtn = document.getElementById('cleanup-preview-btn');
  if (previewBtn) {
    previewBtn.addEventListener('click', function() {
      var selected = CleanupStore.selectedChats();
      if (!selected.length) return;

      // Prevent double-clicks and race conditions during the request
      previewBtn.disabled = true;
      previewBtn.textContent = '分析中...';

      var chatIds = selected.map(function(c) { return c.chat_id; });
      var filters = CleanupStore.getFilterParams();
      api.cleanupPreview({
        chat_ids: chatIds,
        start_date: filters.start_date,
        end_date: filters.end_date,
        msg_types: filters.msg_types,
      }).then(function(data) {
        CleanupStore.data.previewData = data;
        CleanupStore.data.confirmToken = data.confirm_token;
        modalStage = 'preview';
        CleanupStore.emit('modal-stage-changed', 'preview');
      }).catch(function(e) {
        if (e.name === 'AbortError') return; // cancelled by another request, ignore
        alert('预览失败: ' + (e.message || '未知错误'));
      }).finally(function() {
        previewBtn.disabled = false;
        previewBtn.textContent = '预览删除';
      });
    });
  }

  function executeDeletion() {
    var selected = CleanupStore.selectedChats();
    var chatIds = selected.map(function(c) { return c.chat_id; });
    var filters = CleanupStore.getFilterParams();
    api.cleanupExecute({
      confirm_token: CleanupStore.data.confirmToken,
      chat_ids: chatIds,
      start_date: filters.start_date,
      end_date: filters.end_date,
      msg_types: filters.msg_types,
    }).then(function(data) {
      CleanupStore.data.executeResult = data;
      CleanupStore.emit('modal-stage-changed', 'result');
    }).catch(function(e) {
      CleanupStore.data.executeResult = { error: e.message, deleted_files: 0, freed_bytes: 0, errors: [e.message] };
      CleanupStore.emit('modal-stage-changed', 'result');
    });
  }

  // Filter bindings
  var datePreset = document.getElementById('cleanup-date-preset');
  var dateStart = document.getElementById('cleanup-date-start');
  var dateEnd = document.getElementById('cleanup-date-end');
  var dateSep = document.getElementById('cleanup-date-sep');
  if (datePreset) {
    datePreset.addEventListener('change', function() {
      var val = this.value;
      CleanupStore.set('datePreset', val);
      if (val === 'custom') {
        dateStart.style.display = '';
        dateEnd.style.display = '';
        dateSep.style.display = '';
      } else {
        dateStart.style.display = 'none';
        dateEnd.style.display = 'none';
        dateSep.style.display = 'none';
      }
    });
  }
  if (dateStart) {
    dateStart.addEventListener('change', function() { CleanupStore.set('dateStart', this.value); });
  }
  if (dateEnd) {
    dateEnd.addEventListener('change', function() { CleanupStore.set('dateEnd', this.value); });
  }

  var typeCbs = document.querySelectorAll('.cleanup-type-cb');
  typeCbs.forEach(function(cb) {
    cb.addEventListener('change', function() {
      var types = [];
      document.querySelectorAll('.cleanup-type-cb:checked').forEach(function(c) {
        types.push(parseInt(c.value, 10));
      });
      CleanupStore.set('msgTypes', types);
    });
  });

  CleanupStore.on('detail-changed', function(chat) {
    if (!chat) {
      detailEl.innerHTML = '<div class="detail-placeholder"><div class="placeholder-icon">&#x1F5D1;</div><p>选择左侧聊天查看空间占用详情</p></div>';
      return;
    }
    var isGroup = chat.is_group ? '群聊' : '联系人';
    var html = '<div class="cleanup-detail-header">' +
      '<h3>' + escapeHtml(chat.display_name || chat.chat_id) + '</h3>' +
      '<span class="type-badge ' + (chat.is_group ? 'group' : 'user') + '">' + isGroup + '</span>' +
      '</div>' +
      '<div class="cleanup-detail-stats">' +
      '<div class="cleanup-stat-row"><span class="cleanup-stat-label">消息总数</span><span>' + (chat.message_count || 0).toLocaleString() + '</span></div>' +
      '<div class="cleanup-stat-row"><span class="cleanup-stat-label">媒体文件</span><span>' + (chat.media_count || 0) + ' 个</span></div>' +
      '<div class="cleanup-stat-row"><span class="cleanup-stat-label">图片</span><span>' + formatSize(chat.image_bytes) + '</span></div>' +
      '<div class="cleanup-stat-row"><span class="cleanup-stat-label">视频</span><span>' + formatSize(chat.video_bytes) + '</span></div>' +
      '<div class="cleanup-stat-row"><span class="cleanup-stat-label">文件</span><span>' + formatSize(chat.file_bytes) + '</span></div>' +
      '<div class="cleanup-stat-row"><span class="cleanup-stat-label">总占用</span><span style="color:#f85149;font-weight:700">' + formatSize(chat.total_bytes) + '</span></div>' +
      '</div>' +
      '<div class="cleanup-chat-id"><code>' + escapeHtml(chat.chat_id) + '</code></div>';
    detailEl.innerHTML = html;
  });

  function loadAnalysis() {
    CleanupStore.data.loading = true;
    CleanupStore.data.error = null;
    CleanupStore.data.selected = {};
    CleanupStore.data.activeChat = null;
    listComponent.render({ loading: true, error: null, chats: [], selected: {} });
    detailEl.innerHTML = '<div class="detail-placeholder"><div class="placeholder-icon">&#x1F5D1;</div><p>选择左侧聊天查看空间占用详情</p></div>';
    updateBottombar();
    document.getElementById('cleanup-total-size').textContent = '分析中...';
    document.getElementById('cleanup-selected-summary').style.display = 'none';

    api.cleanupAnalyze().then(function(data) {
      CleanupStore.data.chats = data.chats || [];
      CleanupStore.data.totalBytes = data.total_bytes || 0;
      CleanupStore.data.sampling = data.sampling || {};
      CleanupStore.data.loading = false;
      document.getElementById('cleanup-total-size').textContent = '预估总占用: ' + formatSize(data.totalBytes);
      var samplingEl = document.getElementById('cleanup-sampling-info');
      if (samplingEl && data.sampling && data.sampling.sampled > 0) {
        var pct = Math.round(data.sampling.hit_rate * 100);
        samplingEl.textContent = '(采样' + data.sampling.sampled + '个文件，磁盘存活率' + pct + '%)';
        samplingEl.title = '已根据实际文件存活率调整估算';
      } else if (samplingEl) {
        samplingEl.textContent = '';
      }
      listComponent._renderState();
    }).catch(function(e) {
      if (e.name !== 'AbortError') {
        CleanupStore.data.error = '分析失败: ' + (e.message || '未知错误');
        CleanupStore.data.loading = false;
        listComponent._renderState();
        document.getElementById('cleanup-total-size').textContent = '分析失败';
      }
    });
  }

  CleanupStore.on('refresh', loadAnalysis);

  // Search input
  var searchInput = document.getElementById('cleanup-search');
  if (searchInput) {
    searchInput.addEventListener('input', function() {
      CleanupStore.set('searchQuery', this.value);
      listComponent._renderState();
    });
  }

  loadAnalysis();
})();