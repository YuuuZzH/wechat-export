// cleanup-confirm.js — three-stage confirmation modal for deletion
class CleanupConfirm extends Component {
  template(data) {
    if (!data.stage) return '';
    if (data.stage === 'preview') {
      var p = data.previewData;
      if (!p) return '';
      var fileListHtml = '';
      if (p.files_preview && p.files_preview.length) {
        fileListHtml = '<div class="cleanup-file-list">';
        p.files_preview.slice(0, 50).forEach(function(f) {
          fileListHtml += '<div class="cleanup-file-item"><code>' + escapeHtml(f.md5) + '</code> ' + formatSize(f.size) + '</div>';
        });
        if (p.files_preview.length > 50) {
          fileListHtml += '<div class="cleanup-file-more">...及其他 ' + (p.files_preview.length - 50).toLocaleString() + ' 个文件</div>';
        }
        fileListHtml += '</div>';
      }
      return '<div class="cleanup-modal-box">' +
        '<h3>&#x26a0; 确认删除预览</h3>' +
        '<div class="cleanup-modal-stats">' +
        '<div class="cleanup-stat"><span class="cleanup-stat-num">' + p.total_messages.toLocaleString() + '</span> 条消息</div>' +
        '<div class="cleanup-stat"><span class="cleanup-stat-num">' + p.total_files.toLocaleString() + '</span> 个文件</div>' +
        '<div class="cleanup-stat"><span class="cleanup-stat-num">' + formatSize(p.total_bytes) + '</span> 磁盘空间</div>' +
        '</div>' +
        '<p style="color:#f85149;font-size:13px;margin-top:16px"><strong>此操作直接删除源微信媒体文件，不可撤销！</strong></p>' +
        fileListHtml +
        '<div class="cleanup-modal-btns">' +
        '<button class="btn btn-secondary" id="cleanup-modal-cancel">取消</button>' +
        '<button class="btn btn-danger" id="cleanup-modal-confirm">确认，进入验证</button>' +
        '</div></div>';
    }
    if (data.stage === 'verify') {
      return '<div class="cleanup-modal-box">' +
        '<h3 style="color:#f85149">&#x1f6ab; 最终确认</h3>' +
        '<p style="font-size:14px;margin:16px 0">此操作将<strong>永久删除</strong>源微信存储中的图片/视频/文件。</p>' +
        '<p style="font-size:12px;color:#8b949e;margin:8px 0">聊天记录（文字消息）保留不动，仅删除媒体文件节省空间。</p>' +
        '<p style="font-size:14px;margin:16px 0">请输入 <code>DELETE</code> 以确认：</p>' +
        '<input type="text" id="cleanup-delete-input" class="cleanup-delete-input" placeholder="输入 DELETE" autocomplete="off">' +
        '<div class="cleanup-modal-btns">' +
        '<button class="btn btn-secondary" id="cleanup-modal-cancel">取消</button>' +
        '<button class="btn btn-danger" id="cleanup-modal-verify" disabled>确认删除</button>' +
        '</div></div>';
    }
    if (data.stage === 'executing') {
      return '<div class="cleanup-modal-box">' +
        '<h3>&#x23f3; 正在删除...</h3>' +
        '<div class="loading" style="margin:24px 0">执行中，请勿关闭页面...</div>' +
        '</div>';
    }
    if (data.stage === 'result') {
      var r = data.executeResult;
      if (!r) return '';
      var hasError = r.error || (r.errors && r.errors.length > 0);
      return '<div class="cleanup-modal-box">' +
        '<h3>' + (hasError ? '&#x26a0; 删除完成（部分错误）' : '&#x2705; 删除完成') + '</h3>' +
        '<div class="cleanup-modal-stats">' +
        '<div class="cleanup-stat"><span class="cleanup-stat-num">' + (r.deleted_files || 0).toLocaleString() + '</span> 个媒体文件已删除</div>' +
        '<div class="cleanup-stat"><span class="cleanup-stat-num">' + formatSize(r.freed_bytes || 0) + '</span> 空间已释放</div>' +
        '</div>' +
        (r.errors && r.errors.length > 0 ? '<div class="cleanup-modal-errors">' + r.errors.map(function(e) { return escapeHtml(e); }).join('<br>') + '</div>' : '') +
        '<div class="cleanup-modal-btns">' +
        '<button class="btn btn-primary" id="cleanup-modal-done">完成</button>' +
        '</div></div>';
    }
    return '';
  }

  mount() {
    var self = this;
    this.el.addEventListener('click', function(e) {
      var btn = e.target.closest('button');
      if (!btn) return;
      if (btn.id === 'cleanup-modal-cancel') {
        CleanupStore.emit('modal-cancel');
      } else if (btn.id === 'cleanup-modal-confirm') {
        CleanupStore.emit('modal-verify');
      } else if (btn.id === 'cleanup-modal-verify') {
        CleanupStore.emit('modal-execute');
      } else if (btn.id === 'cleanup-modal-done') {
        CleanupStore.emit('modal-done');
      }
    });
    CleanupStore.on('modal-stage-changed', function(stage) {
      self.render({ stage: stage, previewData: CleanupStore.data.previewData, executeResult: CleanupStore.data.executeResult });
      var backdrop = document.getElementById('cleanup-modal-backdrop');
      var modal = document.getElementById('cleanup-modal');
      if (stage) {
        if (backdrop) backdrop.style.display = 'block';
        if (modal) modal.style.display = 'flex';
      } else {
        if (backdrop) backdrop.style.display = 'none';
        if (modal) modal.style.display = 'none';
      }
      if (stage === 'verify') {
        setTimeout(function() {
          var el = document.getElementById('cleanup-delete-input');
          if (el) {
            el.value = '';
            el.focus();
            el.addEventListener('input', function() {
              var btn = document.getElementById('cleanup-modal-verify');
              if (btn) btn.disabled = (el.value !== 'DELETE');
            });
          }
        }, 100);
      }
    });
  }
}
