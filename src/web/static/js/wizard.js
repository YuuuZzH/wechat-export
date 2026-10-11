/* wizard.js — Shared wizard page logic (config → SSE → result) */
class WizardPage {
  constructor(options) {
    this.formId = options.formId || 'wizard-form';
    this.runBtnId = options.runBtnId || 'btn-run';
    this.progressContainerId = options.progressContainerId || 'progress-container';
    this.resultContainerId = options.resultContainerId || 'result-container';
    this.logContainerId = options.logContainerId || 'log-console';
    this.progressBarId = options.progressBarId || 'progress-bar';
    this.progressDetailId = options.progressDetailId || 'progress-detail';
    this.steps = options.steps || [];
    this.apiUrl = options.apiUrl || '';
    this.method = options.method || 'POST';
    this.getBody = options.getBody || (() => ({}));
    this.chatFieldId = options.chatFieldId || 'cfg-chat';

    this.form = document.getElementById(this.formId);
    this.runBtn = document.getElementById(this.runBtnId);
    this.progressContainer = document.getElementById(this.progressContainerId);
    this.resultContainer = document.getElementById(this.resultContainerId);
    this.logConsole = document.getElementById(this.logContainerId);
    this.progressBar = document.getElementById(this.progressBarId);
    this.progressDetail = document.getElementById(this.progressDetailId);

    this.sse = null;
    this.currentStage = '';
    this._selectedDisplayName = '';
    this.bindEvents();
  }

  bindEvents() {
    if (this.runBtn) {
      this.runBtn.addEventListener('click', () => this.start());
    }
  }

  start() {
    this.runBtn.disabled = true;
    this.progressContainer.style.display = 'block';
    this.resultContainer.style.display = 'none';
    const selectContainer = document.getElementById('select-container');
    if (selectContainer) selectContainer.style.display = 'none';
    this.logConsole.innerHTML = '';
    this.updateProgress(0, '正在准备...');

    const body = this.getBody();
    if (this._selectedDisplayName) {
      body.display_name = this._selectedDisplayName;
      this._selectedDisplayName = '';
    }

    this.sse = new SseProgress(this.apiUrl, {
      method: this.method,
      body: body,
      onProgress: (data) => this.handleProgress(data),
      onDone: (data) => this.handleDone(data),
      onError: (err) => this.handleError(err),
      onSelect: (data) => this.handleSelect(data),
    });
    this.sse.start();
  }

  handleProgress(data) {
    this.updateProgress(data.progress || 0, data.detail || '');
    if (data.stage && data.stage !== this.currentStage) {
      this.currentStage = data.stage;
      this.setActiveStep(data.stage);
    }
    const cls = data.stage === 'error' ? 'error' : 'ok';
    this.appendLog(data.detail || '', cls);
  }

  handleDone(data) {
    this.updateProgress(1.0, '完成');
    const result = data.result || data;
    const hasErrors = result && result.errors && result.errors.length > 0;
    if (hasErrors) {
      this.appendLog('✗ 操作失败: ' + result.errors.join('; '), 'error');
    } else {
      this.appendLog('✓ 操作完成', 'ok');
    }
    this.showResult(result);
    this.runBtn.disabled = false;
    this.markAllStepsDone();
  }

  handleError(err) {
    this.appendLog('✗ ' + err.message, 'error');
    this.runBtn.disabled = false;
  }

  handleSelect(data) {
    this.runBtn.disabled = false;
    this.progressContainer.style.display = 'none';
    const matches = data.matches || [];
    this.showSelectDialog(matches);
  }

  showSelectDialog(matches) {
    const container = document.getElementById('select-container');
    if (!container) return;
    let html = '<h3 style="margin:0 0 12px 0;color:#e94560">请选择聊天对象</h3>';
    html += '<div class="select-list">';
    for (const m of matches) {
      const name = this.escapeHtml(m.display_name || m.username);
      const count = (m.msg_count || 0).toLocaleString();
      html += '<div class="select-item" data-username="' + this.escapeHtml(m.username) + '" data-display-name="' + this.escapeHtml(m.display_name || m.username) + '">';
      html += '<span class="select-name">' + name + '</span>';
      html += '<span class="select-count">' + count + ' 条消息</span>';
      html += '</div>';
    }
    html += '</div>';
    container.innerHTML = html;
    container.style.display = 'block';

    for (const item of container.querySelectorAll('.select-item')) {
      item.addEventListener('click', () => {
        const username = item.dataset.username;
        this._selectedDisplayName = item.dataset.displayName || '';
        container.style.display = 'none';
        const chatField = document.getElementById(this.chatFieldId);
        if (chatField) chatField.value = username;
        this.start();
      });
    }
  }

  escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  updateProgress(pct, detail) {
    this.progressBar.style.width = (pct * 100).toFixed(0) + '%';
    this.progressDetail.textContent = detail;
  }

  appendLog(msg, cls) {
    const div = document.createElement('div');
    div.className = 'log-line ' + (cls || '');
    div.textContent = msg;
    this.logConsole.appendChild(div);
    this.logConsole.scrollTop = this.logConsole.scrollHeight;
  }

  setActiveStep(stage) {
    for (const item of document.querySelectorAll('.step-item')) {
      item.classList.remove('active');
      if (item.dataset.stage === stage) {
        item.classList.add('active');
      }
    }
  }

  markAllStepsDone() {
    for (const item of document.querySelectorAll('.step-item')) {
      item.classList.add('done');
    }
  }

  showResult(result) {
    this.resultContainer.style.display = 'block';
    const container = document.getElementById('result-content');
    if (!container) return;
    container.innerHTML = '';

    if (!result) return;

    const linkStyle = 'color:#58a6ff;font-size:15px;text-decoration:none;' +
      'padding:10px 20px;background:#1a1a2e;border:1px solid #30363d;' +
      'border-radius:8px;display:inline-block;margin-top:4px;cursor:pointer';

    if (result.view_url) {
      container.innerHTML = '<p style="color:#8b949e;margin:0 0 8px 0">共分析 ' +
        (result.msg_count || 0).toLocaleString() + ' 条消息</p>';
      const link = document.createElement('a');
      link.href = result.view_url;
      link.target = '_blank';
      link.textContent = '📊 在新标签页打开词云报告';
      link.style.cssText = linkStyle;
      container.appendChild(link);
    } else if (result.download_url) {
      container.innerHTML = '<p style="color:#8b949e;margin:0 0 8px 0">共导出 ' +
        (result.msg_count || 0).toLocaleString() + ' 条消息</p>';
      const link = document.createElement('a');
      link.href = result.download_url;
      link.textContent = '📥 下载导出文件 (' + (result.filename || '') + ')';
      link.style.cssText = linkStyle;
      container.appendChild(link);
    } else if (result.file) {
      container.innerHTML = '<p style="color:#8b949e;margin:0 0 8px 0">文件路径: ' +
        this.escapeHtml(result.file) + '</p>';
    }

    if (result.note) {
      const note = document.createElement('p');
      note.style.cssText = 'color:#8b949e;font-size:12px;margin:10px 0 0 0;' +
        'max-width:640px;line-height:1.7';
      note.textContent = result.note;
      container.appendChild(note);
    }
  }
}
