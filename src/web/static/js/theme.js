/**
 * WeChat Export — UI theme switcher
 * Persists selection in localStorage; applies via html[data-theme].
 */
(function () {
  'use strict';

  var STORAGE_KEY = 'wechat-export-theme';
  var DEFAULT_THEME = 'pixel';
  var THEMES = [
    { id: 'pixel', label: '像素风' },
    { id: 'youth', label: '青春风' },
    { id: 'tech', label: '科技风' },
    { id: 'anime', label: '动漫风' },
    { id: 'wechat', label: '微信原始风' },
    { id: 'qq', label: 'QQ 复古风' },
  ];

  var VALID = {};
  for (var i = 0; i < THEMES.length; i++) VALID[THEMES[i].id] = true;

  function readStored() {
    try {
      var v = localStorage.getItem(STORAGE_KEY);
      if (v && VALID[v]) return v;
    } catch (e) { /* ignore */ }
    return DEFAULT_THEME;
  }

  function applyTheme(id) {
    if (!VALID[id]) id = DEFAULT_THEME;
    document.documentElement.setAttribute('data-theme', id);
    try { localStorage.setItem(STORAGE_KEY, id); } catch (e) { /* ignore */ }
    syncMenu(id);
    try {
      document.dispatchEvent(new CustomEvent('wechat-export:theme', { detail: { theme: id } }));
    } catch (e) { /* ignore */ }
  }

  function syncMenu(id) {
    var buttons = document.querySelectorAll('[data-theme-set]');
    for (var i = 0; i < buttons.length; i++) {
      var btn = buttons[i];
      var on = btn.getAttribute('data-theme-set') === id;
      btn.setAttribute('aria-checked', on ? 'true' : 'false');
    }
    var summary = document.querySelector('.theme-picker summary');
    if (summary) {
      var label = '主题';
      for (var j = 0; j < THEMES.length; j++) {
        if (THEMES[j].id === id) { label = '主题 · ' + THEMES[j].label; break; }
      }
      summary.textContent = label;
      summary.setAttribute('title', '切换界面主题（当前：' + label.replace('主题 · ', '') + '）');
    }
  }

  function closePicker() {
    var el = document.querySelector('.theme-picker');
    if (el) el.removeAttribute('open');
  }

  function bind() {
    var root = document.querySelector('.theme-picker');
    if (!root) return;

    root.addEventListener('click', function (ev) {
      var btn = ev.target.closest('[data-theme-set]');
      if (!btn) return;
      ev.preventDefault();
      applyTheme(btn.getAttribute('data-theme-set'));
      closePicker();
    });

    document.addEventListener('click', function (ev) {
      if (!root.hasAttribute('open')) return;
      if (!root.contains(ev.target)) closePicker();
    });

    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') closePicker();
    });

    syncMenu(readStored());
  }

  // Early apply (also duplicated inline in base.html to avoid FOUC)
  applyTheme(readStored());

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }

  window.WeChatExportTheme = {
    list: THEMES.slice(),
    get: readStored,
    set: applyTheme,
  };
})();
