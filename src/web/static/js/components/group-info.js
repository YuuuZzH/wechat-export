// group-info.js — Group information panel component
// Renders group details (avatar composite, owner, notice, member list).
// Provides a GroupInfo class extending Component plus backward-compatible renderGroupInfo global.

class GroupInfo extends Component {
  template(data) {
    if (!data || !data.members) return '<div style="color:#8b949e;padding:12px">无群信息</div>';
    let html = '';

    // Group avatar (composite of first 4 members)
    const initials = data.members.slice(0, 4).map(m => _avatarChar(m.display_name));
    const colors = _AVATAR_COLORS;
    html += '<div style="text-align:center;padding:12px 0">';
    html += '<div style="display:inline-grid;grid-template-columns:30px 30px;gap:2px">';
    for (let i = 0; i < 4; i++) {
      const ch = initials[i] || '';
      const bg = ch ? colors[i % colors.length] : '#30363d';
      html += `<div style="width:30px;height:30px;border-radius:4px;background:${bg};display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;color:white">${ch}</div>`;
    }
    html += '</div>';
    html += `<div style="color:#c9d1d9;font-size:15px;font-weight:700;margin-top:6px">${escapeHtml(data.display_name || data.chat_id)}</div>`;
    html += `<div style="color:#8b949e;font-size:11px">${data.member_count} 名成员</div>`;
    html += '</div>';

    // Owner
    if (data.owner) {
      html += `<div style="border-top:1px solid #21262d;padding:10px 0"><span style="color:#8b949e;font-size:11px">群主</span><span style="color:#c9d1d9;font-size:13px;margin-left:8px;font-weight:600">${escapeHtml(data.owner)}</span></div>`;
    }

    // Notice
    if (data.notice) {
      html += `<div style="border-top:1px solid #21262d;padding:10px 0"><span style="color:#8b949e;font-size:11px">公告</span><div style="color:#c9d1d9;font-size:12px;background:#0d1117;padding:8px;border-radius:4px;margin-top:4px">${escapeHtml(data.notice)}</div></div>`;
    }

    // Members
    html += '<div style="border-top:1px solid #21262d;padding:10px 0"><span style="color:#8b949e;font-size:11px">成员</span></div>';
    for (const m of data.members) {
      html += '<div style="display:flex;align-items:center;gap:8px;padding:6px 8px;background:#161b22;border-radius:4px;margin-bottom:3px">';
      html += `<div style="width:28px;height:28px;border-radius:50%;background:${_avatarColor(m.wxid)};display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;color:white;flex-shrink:0">${_avatarChar(m.display_name)}</div>`;
      html += `<span style="color:#c9d1d9;font-size:12px;flex:1">${escapeHtml(m.display_name)}</span>`;
      if (m.is_owner) html += '<span style="color:#56d364;font-size:9px">群主</span>';
      html += '</div>';
    }

    return html;
  }

  mount() {
    // Close button
    const closeBtn = this.el.querySelector('.gi-close');
    if (closeBtn) {
      closeBtn.addEventListener('click', () => {
        this.hide();
      });
    }
  }
}

// Backward-compatible global for app.js (appends to fixed #group-info-content element)
function renderGroupInfo(info) {
  const el = document.getElementById('group-info-content');
  if (!el) return;
  let html = '';

  const initials = info.members.slice(0, 4).map(m => _avatarChar(m.display_name));
  const colors = _AVATAR_COLORS;
  html += '<div style="text-align:center;padding:12px 0">';
  html += '<div style="display:inline-grid;grid-template-columns:30px 30px;gap:2px">';
  for (let i = 0; i < 4; i++) {
    const ch = initials[i] || '';
    const bg = ch ? colors[i % colors.length] : '#30363d';
    html += `<div style="width:30px;height:30px;border-radius:4px;background:${bg};display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;color:white">${ch}</div>`;
  }
  html += '</div>';
  html += `<div style="color:#c9d1d9;font-size:15px;font-weight:700;margin-top:6px">${escapeHtml(info.display_name || info.chat_id)}</div>`;
  html += `<div style="color:#8b949e;font-size:11px">${info.member_count} 名成员</div>`;
  html += '</div>';

  if (info.owner) {
    html += `<div style="border-top:1px solid #21262d;padding:10px 0"><span style="color:#8b949e;font-size:11px">群主</span><span style="color:#c9d1d9;font-size:13px;margin-left:8px;font-weight:600">${escapeHtml(info.owner)}</span></div>`;
  }

  if (info.notice) {
    html += `<div style="border-top:1px solid #21262d;padding:10px 0"><span style="color:#8b949e;font-size:11px">公告</span><div style="color:#c9d1d9;font-size:12px;background:#0d1117;padding:8px;border-radius:4px;margin-top:4px">${escapeHtml(info.notice)}</div></div>`;
  }

  html += '<div style="border-top:1px solid #21262d;padding:10px 0"><span style="color:#8b949e;font-size:11px">成员</span></div>';
  for (const m of info.members) {
    html += '<div style="display:flex;align-items:center;gap:8px;padding:6px 8px;background:#161b22;border-radius:4px;margin-bottom:3px">';
    html += `<div style="width:28px;height:28px;border-radius:50%;background:${_avatarColor(m.wxid)};display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;color:white;flex-shrink:0">${_avatarChar(m.display_name)}</div>`;
    html += `<span style="color:#c9d1d9;font-size:12px;flex:1">${escapeHtml(m.display_name)}</span>`;
    if (m.is_owner) html += '<span style="color:#56d364;font-size:9px">群主</span>';
    html += '</div>';
  }

  el.innerHTML = html;
}
