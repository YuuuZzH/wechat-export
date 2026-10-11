// cleanup-store.js — state management for the cleanup page
const CleanupStore = {
  _listeners: {},

  data: {
    chats: [],
    totalBytes: 0,
    selected: {},
    activeChat: null,
    loading: true,
    analyzing: false,
    error: null,
    searchQuery: '',
    datePreset: '90',
    dateStart: '',
    dateEnd: '',
    msgTypes: [3, 43, 6],
    confirmToken: null,
    previewData: null,
    executing: false,
    executeResult: null,
  },

  on(event, fn) {
    if (!this._listeners[event]) this._listeners[event] = [];
    this._listeners[event].push(fn);
  },

  emit(event, payload) {
    if (!this._listeners[event]) return;
    this._listeners[event].forEach(function(fn) { fn(payload); });
  },

  set(key, value) {
    this.data[key] = value;
  },

  toggleSelect(chatId) {
    var sel = this.data.selected;
    if (sel[chatId]) {
      delete sel[chatId];
    } else {
      sel[chatId] = true;
    }
    this.emit('selection-changed');
  },

  selectAll(select) {
    var sel = this.data.selected;
    this.data.chats.forEach(function(c) {
      if (select) {
        sel[c.chat_id] = true;
      } else {
        delete sel[c.chat_id];
      }
    });
    this.emit('selection-changed');
  },

  selectedChats() {
    var self = this;
    return this.data.chats.filter(function(c) { return self.data.selected[c.chat_id]; });
  },

  selectedBytes() {
    return this.selectedChats().reduce(function(sum, c) { return sum + c.total_bytes; }, 0);
  },

  selectedCount() {
    return Object.keys(this.data.selected).length;
  },

  getDateRange() {
    if (this.data.datePreset === 'custom') {
      return { start: this.data.dateStart, end: this.data.dateEnd };
    }
    var days = parseInt(this.data.datePreset, 10);
    var d = new Date();
    d.setDate(d.getDate() - days);
    var endStr = d.toISOString().split('T')[0];
    return { start: '', end: endStr };
  },

  getFilterParams() {
    var range = this.getDateRange();
    return {
      start_date: range.start,
      end_date: range.end,
      msg_types: this.data.msgTypes,
    };
  },
};
