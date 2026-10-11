// store.js — central state + pub/sub event system
const Store = {
  _listeners: {},

  data: {
    contacts: [],
    activeChat: null,
    messages: [],
    expandedMsg: null,
    pagination: { page: 1, perPage: 50, total: 0, totalPages: 0 },
    filters: { keyword: '', dateStart: '', dateEnd: '', msgTypes: '', sender: '' },
    loading: false,
    contactsLoading: true,
  },

  on(event, fn) {
    if (!this._listeners[event]) this._listeners[event] = [];
    this._listeners[event].push(fn);
  },

  off(event, fn) {
    if (!this._listeners[event]) return;
    this._listeners[event] = this._listeners[event].filter(f => f !== fn);
  },

  emit(event, payload) {
    if (!this._listeners[event]) return;
    this._listeners[event].forEach(fn => fn(payload));
  },

  set(key, value) {
    this.data[key] = value;
  },

  get(key) {
    return this.data[key];
  }
};
