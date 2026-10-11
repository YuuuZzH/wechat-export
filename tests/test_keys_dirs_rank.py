"""`GET /api/keys/dirs` 的排序/标注扩展与**向后兼容**测试。

不依赖「微信真的在运行」：T0/T1/T2 的原始信号在 `engine.services.active_dir` 边界上
用**合成信号**注入（monkeypatch `active_dir.collect_signals`），目录列表用合成列表
（monkeypatch `keys_api._detect_dirs`）。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

from engine import config_file
from engine.services import active_dir as ad
from tests.test_active_dir import BASE_KEYS, RANK_KEYS, mkdir_entry, mkprobe
from web.app import create_app
from web.routes import keys_api


@pytest.fixture
def client(tmp_path, monkeypatch):
    """隔离配置文件；**不**替换目录探测（探测契约的那两个测试要用真函数）。"""
    cfg = tmp_path / 'cfg' / '.wechat_exp_config.json'
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({}), encoding='utf-8')
    monkeypatch.setattr(config_file, '_config_path', lambda: cfg)
    d = tmp_path / 'decrypted'
    d.mkdir(exist_ok=True)
    app = create_app(str(d), wxid='wxid_example12345')
    app.config['TESTING'] = True
    return app.test_client()


@pytest.fixture
def ctx(tmp_path, monkeypatch, client):
    """两个真实存在的合成 db_storage 目录 + 合成目录探测 + 配置里记住 acct_b。"""
    root = tmp_path / 'xwechat_files'
    a = root / 'acct_a' / 'db_storage'
    b = root / 'acct_b' / 'db_storage'
    for d in (a, b):
        (d / 'message').mkdir(parents=True)
        (d / 'contact').mkdir(parents=True)
        (d / 'message' / 'message_0.db').write_bytes(b'x')
        (d / 'contact' / 'contact.db').write_bytes(b'x')
    a, b = str(a), str(b)

    cfg = tmp_path / 'cfg' / '.wechat_exp_config.json'
    cfg.write_text(json.dumps({'_db_dir': b}), encoding='utf-8')

    monkeypatch.setattr(keys_api, '_detect_dirs',
                        lambda mode='auto': [mkdir_entry(b, size_mb=90.0),
                                             mkdir_entry(a, size_mb=10.0)])
    return {'a': a, 'b': b, 'client': client}


def _patch_signals(monkeypatch, probe):
    monkeypatch.setattr(ad, 'collect_signals',
                        lambda dirs, **kw: probe)


def test_dirs_keep_original_keys_and_add_rank_fields(ctx, monkeypatch):
    a, b = ctx['a'], ctx['b']
    _patch_signals(monkeypatch, mkprobe(pids=[111], wechat_running=True,
                                        in_use={a: [111]}))
    data = ctx['client'].get('/api/keys/dirs').get_json()

    assert set(data) >= {'dirs', 'current', 'mode', 'recommended_path',
                         'wechat_running', 'probe'}
    assert len(data['dirs']) == 2
    for d in data['dirs']:
        assert set(d) == BASE_KEYS | RANK_KEYS
    top = data['dirs'][0]
    assert top['db_path'] == a
    assert top['tier'] == 'in_use'
    assert top['reason'] == '⭐ 微信进程正在使用（PID 111）'
    assert top['pids'] == [111]
    assert top['active'] is True
    assert top['recommended'] is True
    assert data['recommended_path'] == a
    assert data['wechat_running'] is True
    # acct_b 没在用，但正是配置里记住的目录 ⇒ config 档
    assert data['dirs'][1]['tier'] == 'config'
    assert data['dirs'][1]['reason'] == '微信配置指向此目录'


def test_probe_contract_fields_are_exposed(ctx, monkeypatch):
    _patch_signals(monkeypatch, mkprobe(t0_ok=False, t0_elapsed_ms=12.5,
                                        t1_probed=3, errors=['t0: 注入的失败']))
    data = ctx['client'].get('/api/keys/dirs').get_json()
    probe = data['probe']
    for k in ('t0_ok', 't0_elapsed_ms', 't1_probed', 'errors'):
        assert k in probe, k
    assert probe['t0_ok'] is False
    assert probe['t0_elapsed_ms'] == 12.5
    assert probe['t1_probed'] == 3
    assert probe['errors'] == ['t0: 注入的失败']
    assert data['wechat_running'] is False


def test_current_semantics_unchanged_even_when_recommendation_differs(ctx, monkeypatch):
    """current 仍按「入参→配置→应用配置→自动检测」解析；推荐是**另一个**字段。"""
    a, b = ctx['a'], ctx['b']
    _patch_signals(monkeypatch, mkprobe(pids=[7], wechat_running=True,
                                        in_use={a: [7]}))
    data = ctx['client'].get('/api/keys/dirs').get_json()
    assert data['current'] == b            # = 配置里的 _db_dir，语义没变
    assert data['recommended_path'] == a   # 微信正在用的是 A


def test_config_tier_marks_the_configured_dir(ctx, monkeypatch):
    a, b = ctx['a'], ctx['b']
    _patch_signals(monkeypatch, mkprobe(wechat_running=False))
    data = ctx['client'].get('/api/keys/dirs').get_json()
    by_path = {d['db_path']: d for d in data['dirs']}
    assert by_path[b]['tier'] == 'config'
    assert by_path[b]['reason'] == '微信配置指向此目录'
    assert [d['db_path'] for d in data['dirs']][0] == b
    assert data['recommended_path'] == b
    assert data['current'] == b


def test_empty_detection_returns_empty_list_and_blank_recommendation(ctx, monkeypatch):
    monkeypatch.setattr(keys_api, '_detect_dirs', lambda mode='auto': [])
    _patch_signals(monkeypatch, mkprobe())
    r = ctx['client'].get('/api/keys/dirs')
    assert r.status_code == 200
    data = r.get_json()
    assert data['dirs'] == []
    assert data['recommended_path'] == ''
    assert data['wechat_running'] is False


def test_ranking_failure_still_returns_200_with_idle_fields(ctx, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError('注入的排序失败')

    monkeypatch.setattr(ad, 'rank_and_probe', boom)
    r = ctx['client'].get('/api/keys/dirs')
    assert r.status_code == 200
    data = r.get_json()
    assert len(data['dirs']) == 2
    for d in data['dirs']:
        assert set(d) >= BASE_KEYS | RANK_KEYS
        assert d['tier'] == 'idle'
        assert d['reason'] == '未发现活动迹象'
        assert d['recommended'] is False
        assert d['pids'] == []
        assert d['active'] is False
        assert d['last_write_min'] is None
    assert data['recommended_path'] == ''
    assert data['probe']['errors'], '降级必须留下原因，不能静默'


def test_probe_failure_still_returns_200(ctx, monkeypatch):
    def boom(*a, **k):
        raise OSError('注入的探针失败')

    monkeypatch.setattr(ad, 'collect_signals', boom)
    r = ctx['client'].get('/api/keys/dirs')
    assert r.status_code == 200
    data = r.get_json()
    assert len(data['dirs']) == 2
    assert data['probe']['errors']
    assert data['probe']['t0_ok'] is False


def test_legacy_consumer_keys_are_preserved(ctx, monkeypatch):
    """dbdir.js / keys.html 只用 dirs[].db_path|wxid|size_mb 与 current。"""
    _patch_signals(monkeypatch, mkprobe())
    data = ctx['client'].get('/api/keys/dirs').get_json()
    assert data['mode'] == 'auto'
    for d in data['dirs']:
        assert isinstance(d['db_path'], str) and d['db_path']
        assert 'wxid' in d and 'size_mb' in d and 'db_count' in d and 'mtime' in d


def test_mode_fast_passes_deep_false(client, monkeypatch):
    """fast 走既有路径：find_all_wechat_data_dirs(deep=False)，不做深度搜索。"""
    calls = []

    def fake_find(deep=True, budget_s=15.0, max_depth=5):
        calls.append({'deep': deep, 'budget_s': budget_s, 'max_depth': max_depth})
        return []

    import engine.utils as utils
    monkeypatch.setattr(utils, 'find_all_wechat_data_dirs', fake_find)
    _patch_signals(monkeypatch, mkprobe())
    data = client.get('/api/keys/dirs?mode=fast').get_json()
    assert calls[0] == {'deep': False, 'budget_s': 15.0, 'max_depth': 5}
    assert data['mode'] == 'fast'


def test_mode_deep_contract_unchanged(client, monkeypatch):
    """deep 仍是 45s / 7 层（既有行为不变）。"""
    calls = []

    def fake_find(deep=True, budget_s=15.0, max_depth=5):
        calls.append({'deep': deep, 'budget_s': budget_s, 'max_depth': max_depth})
        return []

    import engine.utils as utils
    monkeypatch.setattr(utils, 'find_all_wechat_data_dirs', fake_find)
    _patch_signals(monkeypatch, mkprobe())
    data = client.get('/api/keys/dirs?mode=deep').get_json()
    assert calls[0] == {'deep': True, 'budget_s': 45.0, 'max_depth': 7}
    assert data['mode'] == 'deep'


def test_invalid_mode_falls_back_to_auto(client, monkeypatch):
    calls = []

    def fake_find(deep=True, budget_s=15.0, max_depth=5):
        calls.append({'deep': deep, 'budget_s': budget_s, 'max_depth': max_depth})
        return []

    import engine.utils as utils
    monkeypatch.setattr(utils, 'find_all_wechat_data_dirs', fake_find)
    _patch_signals(monkeypatch, mkprobe())
    data = client.get('/api/keys/dirs?mode=bogus').get_json()
    assert calls[0] == {'deep': True, 'budget_s': 15.0, 'max_depth': 5}
    assert data['mode'] == 'auto'


def test_recommended_path_is_first_dir_path(ctx, monkeypatch):
    a, b = ctx['a'], ctx['b']
    _patch_signals(monkeypatch, mkprobe(pids=[1, 2], wechat_running=True,
                                        last_write_min={b: 2.0, a: 1.0},
                                        locked={a: True}))
    data = ctx['client'].get('/api/keys/dirs').get_json()
    assert data['dirs'][0]['db_path'] == a
    assert data['recommended_path'] == data['dirs'][0]['db_path'] == a
    assert data['dirs'][0]['active'] is True
