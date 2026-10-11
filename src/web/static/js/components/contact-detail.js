// contact-detail.js — contact detail panel component
class ContactDetail extends Component {
  template(data) {
    // data = contact object or null
    if (!data) {
      return '<div class="detail-placeholder">' +
        '<div class="placeholder-icon">&#x1F465;</div>' +
        '<p>选择左侧联系人查看详情</p>' +
        '</div>';
    }
    var avatarFallbackStyle = 'width:64px;height:64px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:28px;font-weight:700;color:white;background:' + _avatarColor(data.wxid || '');
    var avatarChar = _avatarChar(data.display_name);
    var avatarHtml = data.avatar_url
      ? '<img class="detail-avatar" src="' + escapeAttr(data.avatar_url) + '" alt=""' +
        ' onerror="this.onerror=null;this.outerHTML=\'<div style=\\\'' + avatarFallbackStyle + '\\\'>' + avatarChar + '</div>\'" />'
      : '<div style="' + avatarFallbackStyle + '">' + avatarChar + '</div>';

    var fields = [];
    if (data.remark) fields.push('<div class="detail-field"><span class="detail-label">备注</span><span>' + escapeHtml(data.remark) + '</span></div>');
    if (data.nick_name && data.nick_name !== data.display_name) fields.push('<div class="detail-field"><span class="detail-label">昵称</span><span>' + escapeHtml(data.nick_name) + '</span></div>');
    if (data.alias) fields.push('<div class="detail-field"><span class="detail-label">微信号</span><span style="font-family:monospace;font-size:0.85em">' + escapeHtml(data.alias) + '</span></div>');

    // 微信标签（来自 contact.extra_buffer field 30 → contact_label）
    if (data.labels && data.labels.length) {
      var labelChips = data.labels.map(function(name) {
        return '<span class="label-chip">' + escapeHtml(name) + '</span>';
      }).join('');
      fields.push('<div class="detail-field"><span class="detail-label">标签</span><span>' + labelChips + '</span></div>');
    }

    // Phone: either stored phone field or wxid that looks like a phone number
    if (data.phone) fields.push('<div class="detail-field"><span class="detail-label">手机号</span><span style="font-family:monospace">' + escapeHtml(data.phone) + '</span></div>');

    // Gender
    if (data.sex !== undefined && data.sex !== null) {
      var genderMap = {'0': '未设置', '1': '男', '2': '女'};
      var genderText = genderMap[String(data.sex)] || ('未知(' + data.sex + ')');
      fields.push('<div class="detail-field"><span class="detail-label">性别</span><span>' + genderText + '</span></div>');
    }

    // Region: combine country/province/city
    var region = [data.country, data.province, data.city].filter(Boolean).join(' ');
    if (region) fields.push('<div class="detail-field"><span class="detail-label">地区</span><span>' + escapeHtml(region) + '</span></div>');

    // Signature / status
    if (data.signature) fields.push('<div class="detail-field"><span class="detail-label">签名</span><span>' + escapeHtml(data.signature) + '</span></div>');

    // Description (user notes)
    if (data.description) fields.push('<div class="detail-field"><span class="detail-label">描述</span><span>' + escapeHtml(data.description) + '</span></div>');

    fields.push('<div class="detail-field"><span class="detail-label">wxid</span><span style="font-family:monospace;font-size:0.85em">' + escapeHtml(data.wxid) + '</span></div>');

    var statsHtml = '';
    if (data.msg_count > 0) {
      var lastTime = data.last_msg_time ? formatTime(data.last_msg_time) : '—';
      statsHtml = '<hr class="detail-divider">' +
        '<div class="detail-field"><span class="detail-label">聊天记录</span><span style="color:#58a6ff">' + data.msg_count.toLocaleString() + ' 条</span></div>' +
        '<div class="detail-field"><span class="detail-label">最近消息</span><span>' + lastTime + '</span></div>' +
        '<button class="btn btn-primary" style="width:100%;margin-top:12px" onclick="window.location=\'/chat?open=' + encodeURIComponent(data.wxid) + '\'">查看聊天记录</button>';
    } else if (!data.is_group) {
      statsHtml = '<hr class="detail-divider"><p style="color:#484f58;font-size:0.85em;text-align:center;padding:12px 0">无聊天记录</p>';
    }

    return '<div class="detail-content">' +
      '<div style="display:flex;flex-direction:column;align-items:center;margin-bottom:16px">' +
      avatarHtml +
      '<h3 class="detail-name">' + escapeHtml(data.display_name || data.wxid) + '</h3>' +
      (data.is_group ? '<span class="type-badge group" style="font-size:11px">群聊</span>' : '<span class="type-badge user" style="font-size:11px">联系人</span>') +
      '</div>' +
      fields.join('') +
      statsHtml +
      '</div>';
  }

  mount() {
    var self = this;
    AddressBookStore.on('contact-selected', function(contact) {
      self.render(contact);
    });
  }
}
