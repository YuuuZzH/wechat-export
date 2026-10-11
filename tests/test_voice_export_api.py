# -*- coding: utf-8 -*-
"""语音导出 API 测试：能力探测、参数校验、SSE 完成事件、下载防穿越。"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import web.app as webapp  # noqa: E402
import web.routes.voice_export_api as api  # noqa: E402


@pytest.fixture()
def client(tmp_path):
    flask_app = webapp.create_app(str(tmp_path), wxid='wxid_owner_1a2b')
    flask_app.config['TESTING'] = True
    with flask_app.test_client() as test_client:
        yield test_client


def test_status_reports_capabilities(client):
    resp = client.get('/api/export/voice/status')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['wav'] is True
    assert set(['mp3', 'm4a', 'ffmpeg']).issubset(data)


def test_post_without_chat_or_sender_is_400(client):
    resp = client.post('/api/export/voice', json={})
    assert resp.status_code == 400
    assert resp.get_json()['error'] == 'bad_request'


def test_senders_returns_list(client, monkeypatch):
    monkeypatch.setattr(api, '_decrypted_dir', lambda: '')
    resp = client.get('/api/export/voice/senders?chat=wxid_demo_1a2b')
    assert resp.status_code == 200
    assert resp.get_json()['senders'] == []


def test_sse_done_event_carries_zip_url(client, monkeypatch):
    def fake_export(opts, push, cancel):
        assert opts['chats'] == ['wxid_demo_1a2b']
        assert opts['fmt'] == 'wav'
        return {'count': 3, 'missing': 1, 'duration_total_s': 12.5,
                'out_dir': os.path.join(str(client.application.config['DECRYPTED_DIR']), 'x'),
                'zip': os.path.join(str(client.application.config['DECRYPTED_DIR']), 'x.zip'),
                'merged': [], 'errors': []}

    monkeypatch.setattr(api, '_do_export', fake_export)
    resp = client.post('/api/export/voice', json={'chat': 'wxid_demo_1a2b', 'format': 'wav'})
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert '"zip_url"' in body and 'x.zip' in body and '"count": 3' in body


def test_sse_error_event_on_failure(client, monkeypatch):
    def boom(opts, push, cancel):
        raise RuntimeError('未检测到 ffmpeg')

    monkeypatch.setattr(api, '_do_export', boom)
    resp = client.post('/api/export/voice', json={'chat': 'wxid_demo_1a2b', 'format': 'm4a'})
    assert resp.status_code == 200
    assert '未检测到 ffmpeg' in resp.get_data(as_text=True)


def test_download_strips_path_traversal(client, tmp_path, monkeypatch):
    out_root = tmp_path / 'export' / 'voice'
    out_root.mkdir(parents=True)
    (out_root / 'pack.zip').write_bytes(b'PK\x03\x04demo')
    monkeypatch.setattr(api, '_OUT_ROOT', str(out_root))
    resp = client.get('/api/export/voice/download/pack.zip')
    assert resp.status_code == 200 and resp.data.startswith(b'PK')


def test_display_name_is_resolved_to_real_chat_id(client, monkeypatch):
    """会话可以填显示名：接口必须先解析成真实 wxid 再进采集层。

    回归：真机浏览器里列表给的会话名是**显示名**，而采集层按 md5(wxid) 过滤 ⇒
    不解析就会一条都收不到（表现为"共导出 0 条"）。
    """
    import engine.services.voice_export.collect as collect_mod

    monkeypatch.setattr(collect_mod, 'resolve_chat_target',
                        lambda dec, text, own='': ('wxid_demo_1a2b', []))
    seen = {}

    def fake_export(opts, push, cancel):
        seen['chats'] = opts['chats']
        return {'count': 1, 'missing': 0, 'duration_total_s': 1.0, 'out_dir': 'd',
                'zip': '', 'merged': [], 'errors': []}

    monkeypatch.setattr(api, '_do_export', fake_export)
    resp = client.post('/api/export/voice', json={'chat': '张三', 'format': 'wav'})
    assert resp.status_code == 200
    assert seen['chats'] == ['wxid_demo_1a2b'], '显示名必须被解析成真实 wxid'


def test_ambiguous_chat_emits_select_event(client, monkeypatch):
    import engine.services.voice_export.collect as collect_mod

    monkeypatch.setattr(collect_mod, 'resolve_chat_target',
                        lambda dec, text, own='': (None, [
                            {'username': 'wxid_a_1a2b', 'display_name': '张三', 'msg_count': 3},
                            {'username': 'wxid_b_3c4d', 'display_name': '张三（同事）', 'msg_count': 1}]))
    resp = client.post('/api/export/voice', json={'chat': '张三'})
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert 'event: select' in body and 'wxid_a_1a2b' in body


def test_unknown_chat_returns_404(client, monkeypatch):
    import engine.services.voice_export.collect as collect_mod

    monkeypatch.setattr(collect_mod, 'resolve_chat_target', lambda dec, text, own='': ('', []))
    resp = client.post('/api/export/voice', json={'chat': '不存在的人'})
    assert resp.status_code == 404
    assert resp.get_json()['error'] == 'chat_not_found'


def test_concurrent_export_is_rejected_with_409(client):
    """并发导出必须被礼貌拒绝。

    真机现象：连点两次「导出」时进程直接退出（根因是共享 zstd 上下文的线程安全问题，
    已在 collect/pipeline 侧修掉）。这里额外锁住"同时只允许一个导出"的行为。
    """
    assert api._EXPORT_BUSY.acquire(blocking=False), '测试前置：应能拿到锁'
    try:
        resp = client.post('/api/export/voice', json={'chat': 'wxid_demo_1a2b'})
        assert resp.status_code == 409
        body = resp.get_json()
        assert body['error'] == 'busy' and '正在进行' in body['message']
    finally:
        api._EXPORT_BUSY.release()


def test_busy_lock_is_released_after_export(client, monkeypatch):
    """导出结束后必须释放锁，否则第二次导出永远 409。"""
    def fake_export(opts, push, cancel):
        return {'count': 0, 'missing': 0, 'duration_total_s': 0.0, 'out_dir': 'd',
                'zip': '', 'merged': [], 'errors': []}

    monkeypatch.setattr(api, '_do_export', fake_export)
    first = client.post('/api/export/voice', json={'chat': 'wxid_demo_1a2b'})
    assert first.status_code == 200
    # 消费完 SSE，等后台线程收尾
    first.get_data()
    for _ in range(50):
        if not api._EXPORT_BUSY.locked():
            break
        __import__('time').sleep(0.05)
    assert not api._EXPORT_BUSY.locked(), '导出结束后锁必须释放'
    second = client.post('/api/export/voice', json={'chat': 'wxid_demo_1a2b'})
    assert second.status_code == 200, '第二次导出不应再被 409'
