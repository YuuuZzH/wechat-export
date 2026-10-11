/* voice-export.js — 按人批量导出语音：能力探测 → 发送者选择 → SSE 进度 → 结果下载 */
(function () {
  var el = function (id) { return document.getElementById(id); };
  var sse = null;

  function loadStatus() {
    fetch('/api/export/voice/status')
      .then(function (r) { return r.json(); })
      .then(function (c) {
        var parts = [];
        parts.push(c.mp3 ? 'MP3 可用' : 'MP3 不可用（将自动降级为 WAV）');
        parts.push(c.m4a ? ('M4A 可用（ffmpeg: ' + (c.ffmpeg_name || '已检测到') + '）')
                         : 'M4A 需要 ffmpeg（未检测到，可点导出页的「一键安装 ffmpeg」）');
        if (el('caps-hint')) el('caps-hint').textContent = parts.join(' · ');
        if (el('opt-m4a')) el('opt-m4a').disabled = !c.m4a;
      })
      .catch(function () {});
  }

  function loadSenders() {
    var chat = el('cfg-chat') ? el('cfg-chat').value.trim() : '';
    var sel = el('cfg-sender');
    if (!sel) return;
    if (!chat) { sel.innerHTML = ''; return; }
    fetch('/api/export/voice/senders?chat=' + encodeURIComponent(chat))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        sel.innerHTML = '';
        (d.senders || []).forEach(function (s) {
          var o = document.createElement('option');
          o.value = s.key;
          o.textContent = s.name + '（' + s.count + ' 条）';
          sel.appendChild(o);
        });
      })
      .catch(function () {});
  }

  function selectedLayouts() {
    var out = [];
    document.querySelectorAll('#cfg-layouts input[type=checkbox]').forEach(function (c) {
      if (c.checked && c.value) out.push(c.value);
    });
    return out;
  }

  function log(message, cls) {
    var box = el('log-console');
    if (!box) return;
    var line = document.createElement('div');
    if (cls) line.className = cls;
    line.textContent = message;
    box.appendChild(line);
    box.scrollTop = box.scrollHeight;
  }

  function setProgress(pct, detail) {
    if (el('progress-bar')) el('progress-bar').style.width = Math.round((pct || 0) * 100) + '%';
    if (el('progress-detail') && detail) el('progress-detail').textContent = detail;
  }

  function start() {
    var chat = el('cfg-chat') ? el('cfg-chat').value.trim() : '';
    var sel = el('cfg-sender');
    var senders = sel ? Array.prototype.slice.call(sel.selectedOptions).map(function (o) { return o.value; }) : [];
    if (!chat && senders.length === 0) { alert('请至少填写会话，或在会话加载后选择发送者'); return; }
    var body = {
      chat: chat,
      senders: senders,
      format: el('cfg-format') ? el('cfg-format').value : 'mp3',
      layouts: selectedLayouts(),
      merge_by: el('cfg-merge-by') ? el('cfg-merge-by').value : 'person',
      split: el('cfg-split') ? el('cfg-split').value : 'single',
      gap_s: parseFloat(el('cfg-gap') ? el('cfg-gap').value : '1') || 0,
      mp3_quality: parseInt(el('cfg-quality') ? el('cfg-quality').value : '7', 10) || 7,
      include_asr: !!(el('cfg-asr') && el('cfg-asr').checked),
      include_other_chats: el('cfg-other-chats') ? el('cfg-other-chats').checked : false,
      keep_silk: el('cfg-keep-silk') ? el('cfg-keep-silk').checked : false
    };
    if (el('cfg-from') && el('cfg-from').value) {
      body.start_ts = Math.floor(new Date(el('cfg-from').value + 'T00:00:00').getTime() / 1000);
    }
    if (el('cfg-to') && el('cfg-to').value) {
      body.end_ts = Math.floor(new Date(el('cfg-to').value + 'T23:59:59').getTime() / 1000);
    }
    if (el('progress-container')) el('progress-container').style.display = '';
    if (el('result-container')) el('result-container').style.display = 'none';
    if (el('btn-cancel')) el('btn-cancel').style.display = '';
    if (el('btn-run')) { el('btn-run').disabled = true; }   // 防连点：并发导出会白干活
    setProgress(0.02, '正在准备...');

    sse = new SseProgress('/api/export/voice', {
      body: body,
      onProgress: function (d) { setProgress(d.progress, d.detail); log(d.detail || ''); },
      onDone: function (payload) {
        // SSE 的 done 事件把业务结果包在 result 里（见 web/sse.py；项目惯例是
        // `data.result || data`）。若某层把普通进度行也用 stage='done' 推出来，
        // 那种载荷既没有 result 也没有 count ⇒ 直接忽略，避免渲染"导出 0 条"的假结果。
        var d = (payload && payload.result) || payload || {};
        if ((!payload || payload.result === undefined) && d.count === undefined) {
          log(d.detail || '');
          return;
        }
        setProgress(1, '完成');
        if (el('btn-cancel')) el('btn-cancel').style.display = 'none';
        if (el('btn-run')) el('btn-run').disabled = false;
        var html = '<p>共导出 <b>' + (d.count || 0) + '</b> 条，总时长约 '
          + Math.round((d.duration_total_s || 0) / 60) + ' 分钟；缺失 <b>' + (d.missing || 0)
          + '</b> 条（见 missing.csv，可重跑一次「一键备份」补齐）。</p>';
        if (d.zip_url) {
          html += '<p><a class="btn btn-primary" href="' + d.zip_url + '">⬇ 下载 ZIP（解压后双击 index.html）</a></p>';
        }
        if ((d.merged || []).length) {
          html += '<p style="color:#8b949e;font-size:12px">合并文件：'
            + d.merged.map(function (m) { return m.stem + '（' + Math.round(m.duration_s) + 's）'; }).join('、')
            + '</p>';
        }
        (d.errors || []).forEach(function (e) { html += '<p style="color:#f0883e">' + e + '</p>'; });
        if (el('result-content')) el('result-content').innerHTML = html;
        if (el('result-container')) el('result-container').style.display = '';
      },
      onError: function (err) {
        setProgress(1, '失败');
        if (el('btn-cancel')) el('btn-cancel').style.display = 'none';
        if (el('btn-run')) el('btn-run').disabled = false;
        log('✗ ' + err.message, 'error');
      },
      onSelect: function (data) {
        // 会话名匹配到多个：让用户点选（不猜）
        if (el('btn-cancel')) el('btn-cancel').style.display = 'none';
        if (el('btn-run')) el('btn-run').disabled = false;
        var list = (data && data.matches) || [];
        var html = '<p>「' + (el('cfg-chat') ? el('cfg-chat').value : '')
          + '」匹配到多个会话，请选择具体哪一个：</p>';
        list.forEach(function (m) {
          html += '<p><a href="#" class="btn" data-username="' + m.username + '">'
            + m.display_name + '（' + (m.msg_count || 0) + ' 条）</a></p>';
        });
        if (el('result-content')) el('result-content').innerHTML = html;
        if (el('result-container')) el('result-container').style.display = '';
        document.querySelectorAll('#result-content a[data-username]').forEach(function (a) {
          a.addEventListener('click', function (ev) {
            ev.preventDefault();
            el('cfg-chat').value = a.getAttribute('data-username');
            if (el('result-container')) el('result-container').style.display = 'none';
            start();
          });
        });
      }
    });
    sse.start();
  }

  function stop() {
    if (sse) { sse.stop(); log('已请求停止：已写出的文件与清单仍然保留'); }
    if (el('btn-cancel')) el('btn-cancel').style.display = 'none';
  }

  window.addEventListener('DOMContentLoaded', function () {
    loadStatus();
    if (el('cfg-chat')) el('cfg-chat').addEventListener('change', loadSenders);
    if (el('btn-run')) el('btn-run').addEventListener('click', start);
    if (el('btn-cancel')) el('btn-cancel').addEventListener('click', stop);
  });
})();
