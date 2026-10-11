# -*- coding: utf-8 -*-
"""语音导出页面测试：模板要素齐全、前端接线正确、入口可达、零外部依赖。"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import web.app as webapp  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


def test_template_has_required_controls():
    text = _read('src/web/templates/voice_export.html')
    for element in ('id="cfg-chat"', 'id="cfg-sender"', 'id="cfg-format"', 'id="cfg-from"',
                    'id="cfg-to"', 'id="cfg-merge-by"', 'id="cfg-split"', 'id="cfg-gap"',
                    'id="cfg-layouts"', 'id="btn-run"', 'id="btn-cancel"',
                    'id="progress-container"', 'id="result-container"'):
        assert element in text, element
    assert not re.search(r'https?://', text), '页面不得引用外部资源'
    assert 'sse_progress.js' in text and 'voice-export.js' in text


def test_template_offers_three_delivery_layouts_and_formats():
    text = _read('src/web/templates/voice_export.html')
    for value in ('html-folder', 'html-inline', 'files', 'merged'):
        assert 'value="%s"' % value in text, value
    for value in ('mp3', 'wav', 'm4a'):
        assert 'value="%s"' % value in text, value


def test_js_wires_status_senders_and_sse():
    text = _read('src/web/static/js/voice-export.js')
    assert '/api/export/voice/status' in text
    assert '/api/export/voice/senders' in text
    assert "'/api/export/voice'" in text
    assert 'SseProgress' in text                          # 复用项目的 SSE 助手
    assert 'm4a' in text and 'zip_url' in text


def test_js_handles_select_and_ignores_empty_done():
    """回归（真机浏览器验收发现的两件事）：

    ① 会话名有多个候选时要能点选（`onSelect`）；
    ② `done` 事件的业务结果包在 `result` 里（项目惯例 `data.result || data`）——
       少了解包就会把**真结果也忽略掉**，页面永远不显示结果（真机表现为"一直转圈"）。
    """
    text = _read('src/web/static/js/voice-export.js')
    assert 'onSelect' in text and 'data-username' in text
    assert 'payload.result' in text, 'done 载荷必须解包 result（见 web/sse.py 与 wizard.js）'


def test_entry_points_link_to_voice_export():
    assert '/voice-export' in _read('src/web/templates/dashboard.html')
    assert '/voice-export' in _read('src/web/templates/export.html')


def test_page_route_renders(tmp_path):
    app = webapp.create_app(str(tmp_path), wxid='wxid_owner_1a2b')
    app.config['TESTING'] = True
    with app.test_client() as client:
        resp = client.get('/voice-export')
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert '按人批量导出语音' in html and 'cfg-chat' in html
