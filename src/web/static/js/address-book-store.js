// address-book-store.js — state management for the contacts page
const AddressBookStore = {
  _listeners: {},

  data: {
    contacts: [],
    groups: [],
    activeTab: 'contacts',     // 'contacts' | 'groups'
    activeContact: null,       // currently selected contact object or null
    searchQuery: '',
    labelFilter: '',           // 微信标签名，'' = 不筛选
    availableLabels: [],       // 供下拉框使用
    loading: true,
    error: null,
    page: 1,
    perPage: 100,
    totalPages: 1,
    total: 0,
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
    if (key === 'activeTab') {
      this.data.activeContact = null;
      this.emit('tab-changed', value);
      this.emit('contact-selected', null);
    }
  },

  // Return contacts filtered by current search query
  // Contacts tab: server already filters (q / label), skip client-side re-filtering
  // Groups tab: client-side filtering (server doesn't filter groups)
  filteredContacts() {
    const source = this.data.activeTab === 'groups'
      ? this.data.groups
      : this.data.contacts.filter(c => !c.is_group);
    if (this.data.activeTab === 'contacts') return source;
    let out = source;
    const label = (this.data.labelFilter || '').toLowerCase();
    if (label) {
      out = out.filter(c => (c.labels || []).some(n => (n || '').toLowerCase() === label));
    }
    const q = this.data.searchQuery.toLowerCase();
    if (!q) return out;
    return out.filter(c =>
      (c.display_name || '').toLowerCase().includes(q) ||
      (c.remark || '').toLowerCase().includes(q) ||
      (c.nick_name || '').toLowerCase().includes(q) ||
      (c.alias || '').toLowerCase().includes(q) ||
      (c.wxid || '').toLowerCase().includes(q)
    );
  },

  // Group contacts by first letter of display_name
  letterGroups() {
    const filtered = this.filteredContacts();
    const groups = {};
    filtered.forEach(c => {
      const name = c.display_name || c.wxid || '?';
      let ch = '?';
      for (let i = 0; i < name.length; i++) {
        const code = name.charCodeAt(i);
        if ((code >= 0x4E00 && code <= 0x9FFF) ||
            (code >= 0x3400 && code <= 0x4DBF) ||
            (code >= 65 && code <= 90) ||
            (code >= 97 && code <= 122) ||
            (code >= 48 && code <= 57)) {
          ch = name[i].toUpperCase();
          break;
        }
      }
      if (!groups[ch]) groups[ch] = [];
      groups[ch].push(c);
    });
    const keys = Object.keys(groups).sort((a, b) => {
      const aIsNum = a >= '0' && a <= '9';
      const bIsNum = b >= '0' && b <= '9';
      const aIsAlpha = a >= 'A' && a <= 'Z';
      const bIsAlpha = b >= 'A' && b <= 'Z';
      if (aIsNum && !bIsNum) return -1;
      if (!aIsNum && bIsNum) return 1;
      if (aIsAlpha && !bIsAlpha) return -1;
      if (!aIsAlpha && bIsAlpha) return 1;
      return a.localeCompare(b);
    });
    return keys.map(k => ({ letter: k, contacts: groups[k] }));
  },

  selectContact(wxid) {
    const source = this.data.activeTab === 'groups'
      ? this.data.groups
      : this.data.contacts;
    this.data.activeContact = source.find(c => c.wxid === wxid) || null;
    this.emit('contact-selected', this.data.activeContact);
  }
};
