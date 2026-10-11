// contacts-app.js — bootstrap for the /contacts page
(function() {
  var listEl = document.getElementById('contacts-list');
  var detailEl = document.getElementById('contacts-detail');

  var listComponent = new AddressBookList(listEl);
  var detailComponent = new ContactDetail(detailEl);

  listComponent.mount();
  detailComponent.mount();

  // Initial render: loading state
  listComponent.render({ loading: true, error: null, searchQuery: '', letterGroups: null, activeContact: null, activeTab: 'contacts' });

  var _groupsLoaded = false;
  var _groupsLoading = false;

  // Load contacts from API with pagination
  async function loadContacts() {
    AddressBookStore.data.loading = true;
    AddressBookStore.data.error = null;
    listComponent._renderState();
    try {
      var params = {
        q: AddressBookStore.data.searchQuery,
        label: AddressBookStore.data.labelFilter,
        page: AddressBookStore.data.page,
        per_page: AddressBookStore.data.perPage,
      };
      var contactsData = await api.addressBook(params);
      AddressBookStore.data.contacts = contactsData.contacts;
      AddressBookStore.data.total = contactsData.total;
      AddressBookStore.data.page = contactsData.page;
      AddressBookStore.data.totalPages = contactsData.total_pages;
    } catch (e) {
      if (e.name !== 'AbortError') {
        AddressBookStore.data.error = '加载通讯录失败: ' + (e.message || '未知错误');
      }
    }
    AddressBookStore.data.loading = false;
    AddressBookStore.emit('contacts-loaded');
  }

  // Lazy-load groups only when switching to groups tab
  async function loadGroups() {
    if (_groupsLoaded || _groupsLoading) return;
    _groupsLoading = true;
    try {
      var groupsData = await api.addressBookGroups();
      AddressBookStore.data.groups = groupsData.groups || [];
      _groupsLoaded = true;
    } catch (e) {
      if (e.name !== 'AbortError') {
        console.error('Failed to load groups:', e);
      }
    }
    _groupsLoading = false;
    // Re-render to show groups
    AddressBookStore.emit('contacts-loaded');
  }

  // Listen for tab changes to lazy-load groups
  AddressBookStore.on('tab-changed', function(tab) {
    if (tab === 'groups' && !_groupsLoaded) {
      AddressBookStore.data.loading = true;
      listComponent._renderState();
      loadGroups().then(function() {
        AddressBookStore.data.loading = false;
        AddressBookStore.emit('contacts-loaded');
      });
    }
  });

  // Go to specific page
  function goToPage(page) {
    if (page < 1 || page > AddressBookStore.data.totalPages) return;
    AddressBookStore.data.page = page;
    loadContacts();
  }

  // Debounced search: reload from server with query
  var searchInput = document.getElementById('contacts-search');
  if (searchInput) {
    var searchTimer = null;
    searchInput.addEventListener('input', function() {
      clearTimeout(searchTimer);
      var self = this;
      searchTimer = setTimeout(function() {
        AddressBookStore.data.searchQuery = self.value.trim();
        AddressBookStore.data.page = 1;
        loadContacts();
      }, 300);
    });
  }

  // 标签筛选：联系人页服务端筛选，群聊页客户端筛选（群聊列表一次加载）
  var labelSelect = document.getElementById('contacts-label-filter');
  if (labelSelect) {
    api.addressBookLabels().then(function (d) {
      (d.labels || []).forEach(function (name) {
        var opt = document.createElement('option');
        opt.value = name;
        opt.textContent = name;
        labelSelect.appendChild(opt);
      });
    }).catch(function (e) {
      console.error('加载标签列表失败:', e);
    });

    labelSelect.addEventListener('change', function () {
      AddressBookStore.data.labelFilter = this.value;
      AddressBookStore.data.page = 1;
      if (AddressBookStore.data.activeTab === 'groups') {
        AddressBookStore.emit('contacts-loaded');
      } else {
        loadContacts();
      }
    });
  }

  // Expose goToPage for onclick handlers in template
  window._contactsGoToPage = goToPage;

  // 导出当前列表（xlsx / csv / html）
  // 带上当前标签页、标签筛选与搜索词，导出的就是用户此刻看到的内容。
  var exportBtn = document.getElementById('contacts-export-btn');
  var exportFormatEl = document.getElementById('contacts-export-format');
  if (exportBtn && exportFormatEl) {
    exportBtn.addEventListener('click', function() {
      var url = api.addressBookExportUrl({
        format: exportFormatEl.value,
        q: AddressBookStore.data.searchQuery,
        label: AddressBookStore.data.labelFilter,
        // 联系人页排除群聊、群聊页只导群聊，与列表展示保持一致
        kind: AddressBookStore.data.activeTab === 'groups' ? 'groups' : 'contacts',
      });

      var original = exportBtn.textContent;
      exportBtn.disabled = true;
      exportBtn.textContent = '导出中...';

      var restore = function() {
        exportBtn.disabled = false;
        exportBtn.textContent = original;
      };

      fetch(url).then(function(resp) {
        if (!resp.ok) {
          return resp.json().then(function(b) {
            throw new Error((b && b.error) || ('HTTP ' + resp.status));
          }, function() {
            throw new Error('HTTP ' + resp.status);
          });
        }
        var cd = resp.headers.get('Content-Disposition') || '';
        var m = /filename=([^;]+)/.exec(cd);
        var filename = m ? m[1].trim().replace(/^"|"$/g, '') : 'contacts';
        return resp.blob().then(function(blob) {
          var objectUrl = URL.createObjectURL(blob);
          var a = document.createElement('a');
          a.href = objectUrl;
          a.download = filename;
          document.body.appendChild(a);
          a.click();
          document.body.removeChild(a);
          setTimeout(function() { URL.revokeObjectURL(objectUrl); }, 5000);
        });
      }).catch(function(e) {
        alert('导出失败: ' + (e.message || '未知错误'));
      }).then(restore);
    });
  }

  loadContacts();
})();
