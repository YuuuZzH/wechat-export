"""GitHub issue #15：一键提取密钥时**扫错账号目录** ⇒ 每个进程都"验证 0 个密钥"、最终失败且无解释。

控制方在本机完整复现（真实数据、只计计数）：

    内存里的 47 个候选密钥（各带自己的 salt）
      与「配置里那个 db_storage」的 28 个 salt 相交 = 0   ⇒ 验证通过 0
      与「微信正在使用的那个 db_storage」的 32 个 salt 相交 = 32 ⇒ 验证通过 32

也就是解密钥的链路是**好的**（对正确目录 32/32 全通过），坏的是**目标目录选错了**：
`_db_dir` / 界面选择器里的目录是**上一次用的那个账号**，而内存里的密钥属于**当前正在运行的账号**。
用户因此看到"每个进程验证 0 个密钥"+ 一句没有原因的失败。

本文件全部使用合成目录与合成 SQLCipher 首页（含**真实可校验的 HMAC**），不读任何真实微信数据。
"""
import hashlib
import hmac as hmac_mod
import os
import sqlite3
import struct

import pytest

from engine.services import config_cipher_extract as cce

SALT_SZ = 16
PAGE_SZ = 4096
KEY_SZ = 32


def make_page1(key: bytes, salt: bytes) -> bytes:
    """造一张**真能通过 `verify_enc_key`** 的 SQLCipher 首页（纯合成）。"""
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = hashlib.pbkdf2_hmac('sha512', key, mac_salt, 2, dklen=KEY_SZ)
    page = bytearray(PAGE_SZ)
    page[:SALT_SZ] = salt
    hm = hmac_mod.new(mac_key, bytes(page[SALT_SZ:PAGE_SZ - 80 + 16]), hashlib.sha512)
    hm.update(struct.pack('<I', 1))
    page[PAGE_SZ - 64:] = hm.digest()
    return bytes(page)


def make_db_dir(tmp_path, name, key, salt, n_dbs=1):
    """造一个 db_storage 目录：n_dbs 个 .db，每个都有该 key+salt 的有效首页。"""
    d = os.path.join(str(tmp_path), name, 'db_storage')
    os.makedirs(os.path.join(d, 'message'), exist_ok=True)
    for i in range(n_dbs):
        with open(os.path.join(d, 'message', 'message_%d.db' % i), 'wb') as f:
            f.write(make_page1(key, salt))
    return d


def _scan_stub(candidates, *, salt_of_target_attr):
    """返回一个 `scan_pid_for_config_cipher` 替身。

    它模拟"内存里有一批候选密钥（带各自的 salt）"，并**真的**用 `verify_enc_key`
    去校验当前目标目录的库 —— 于是"换对目录后就真的能验证通过"是真实行为，不是断言。
    """
    def _stub(pid, db_files, salt_to_dbs, key_map, print_fn, remaining_salts):
        stats = {'needles': 1, 'nodes': 9, 'candidates': len(candidates),
                 'verified': 0, 'opened': True, 'open_error': 0, 'regions': 100,
                 'cand_salts': set(s for _k, s in candidates)}
        for key_hex, salt_hex in candidates:
            for rel, _p, _sz, s, page1 in db_files:
                if s == salt_hex and salt_hex in remaining_salts:
                    try:
                        key = bytes.fromhex(key_hex)
                    except ValueError:
                        continue
                    from engine.services.wechat_key_extract import verify_enc_key
                    if verify_enc_key(key, page1):
                        key_map[salt_hex] = key_hex
                        remaining_salts.discard(salt_hex)
                        stats['verified'] += 1
                        print_fn('  [Cipher-FOUND] %s' % rel)
        return stats
    return _stub


@pytest.fixture
def two_accounts(tmp_path):
    """两个账号：wrong（配置里指的）与 right（内存里密钥真正属于的那个）。"""
    key_right = bytes(range(1, 33))
    salt_right = bytes(range(0x10, 0x20))
    key_wrong = bytes(range(2, 34))
    salt_wrong = bytes(range(0x40, 0x50))
    wrong = make_db_dir(tmp_path, 'account_wrong', key_wrong, salt_wrong, n_dbs=2)
    right = make_db_dir(tmp_path, 'account_right', key_right, salt_right, n_dbs=3)
    return {
        'wrong': wrong, 'right': right,
        'wrong_key': key_wrong.hex(), 'wrong_salt': salt_wrong.hex(),
        'right_key': key_right.hex(), 'right_salt': salt_right.hex(),
    }


def _run(tmp_path, accounts, *, candidates, monkeypatch, connected_dirs=None):
    from engine.services.wechat_key_extract import collect_db_files
    monkeypatch.setattr(cce, 'find_wechat_pids', lambda: [(0, 4242)])
    monkeypatch.setattr(cce, 'scan_pid_for_config_cipher',
                        _scan_stub(candidates, salt_of_target_attr=None))
    monkeypatch.setattr(cce, '_detect_db_dirs',
                        lambda: list(connected_dirs if connected_dirs is not None
                                     else [accounts['wrong'], accounts['right']]))
    db_files, salt_to_dbs = collect_db_files(accounts['wrong'])
    key_map = {}
    resolution = {}
    out = []
    found = cce.extract_keys_via_config_cipher(
        accounts['wrong'], db_files, salt_to_dbs, key_map,
        out.append, None, resolution=resolution)
    return found, key_map, resolution, '\n'.join(out)


class TestRetargetToTheAccountTheMemoryKeysBelongTo:
    def test_switches_to_the_dir_whose_salts_match(self, tmp_path, two_accounts,
                                                   monkeypatch):
        a = two_accounts
        found, key_map, resolution, log = _run(
            tmp_path, a,
            candidates=[(a['right_key'], a['right_salt'])],
            monkeypatch=monkeypatch)
        assert found == 1, '必须自动换到"内存里密钥真正属于"的那个账号目录并验证通过'
        assert key_map.get(a['right_salt']) == a['right_key']
        assert resolution.get('db_dir') == a['right'], '调用方要能拿到换过之后的目录'
        assert resolution.get('salt_to_dbs'), '调用方要能拿到换过之后的 salt 表'
        assert '账号' in log or '目录' in log, '换目录这件事必须说出来，不能默默换'

    def test_does_not_switch_when_salts_already_match(self, two_accounts, tmp_path,
                                                      monkeypatch):
        """正常情况（目录本来就对）不许改变行为：既不换目录，也不换 db_files。"""
        a = two_accounts
        from engine.services.wechat_key_extract import collect_db_files
        calls = []
        real = cce.scan_pid_for_config_cipher
        monkeypatch.setattr(cce, 'find_wechat_pids', lambda: [(0, 4242)])
        monkeypatch.setattr(cce, '_detect_db_dirs', lambda: [a['wrong'], a['right']])

        def _counting_stub(pid, db_files, salt_to_dbs, key_map, print_fn, remaining):
            calls.append(list(db_files))
            return _scan_stub([(a['wrong_key'], a['wrong_salt'])],
                              salt_of_target_attr=None)(pid, db_files, salt_to_dbs,
                                                        key_map, print_fn, remaining)

        monkeypatch.setattr(cce, 'scan_pid_for_config_cipher', _counting_stub)
        db_files, salt_to_dbs = collect_db_files(a['wrong'])
        key_map, resolution = {}, {}
        found = cce.extract_keys_via_config_cipher(
            a['wrong'], db_files, salt_to_dbs, key_map, lambda *_: None, None,
            resolution=resolution)
        assert found == 1
        assert len(calls) == 1, '目录本来就对时只扫一次（不许白白重扫）'
        assert not resolution, '没有换目录就不该写 resolution'

    def test_backward_compatible_without_resolution_kwarg(self, tmp_path, two_accounts,
                                                          monkeypatch):
        """老调用方（不传 resolution）不许崩，返回值语义不变。"""
        a = two_accounts
        from engine.services.wechat_key_extract import collect_db_files
        monkeypatch.setattr(cce, 'find_wechat_pids', lambda: [(0, 4242)])
        monkeypatch.setattr(cce, 'scan_pid_for_config_cipher',
                            _scan_stub([(a['wrong_key'], a['wrong_salt'])],
                                       salt_of_target_attr=None))
        monkeypatch.setattr(cce, '_detect_db_dirs', lambda: [a['wrong'], a['right']])
        db_files, salt_to_dbs = collect_db_files(a['wrong'])
        key_map = {}
        found = cce.extract_keys_via_config_cipher(
            a['wrong'], db_files, salt_to_dbs, key_map, lambda *_: None, None)
        assert found == 1


class TestFailureExplainsItself:
    """issue #15 用户的原话里明确要求：**"或提供更明确的失败原因和排查建议"**。"""

    def test_mismatch_with_no_other_account_is_explained(self, tmp_path, two_accounts,
                                                         monkeypatch):
        """候选 salt 与被扫目录不相交、又没有别的账号目录可换 ⇒ 必须解释"为什么" + 给建议。"""
        a = two_accounts
        from engine.services.wechat_key_extract import collect_db_files
        monkeypatch.setattr(cce, 'find_wechat_pids', lambda: [(0, 4242)])
        monkeypatch.setattr(cce, 'scan_pid_for_config_cipher',
                            _scan_stub([(a['right_key'], a['right_salt'])],
                                       salt_of_target_attr=None))
        monkeypatch.setattr(cce, '_detect_db_dirs', lambda: [a['wrong']])   # 没有别的目录
        db_files, salt_to_dbs = collect_db_files(a['wrong'])
        log = []
        found = cce.extract_keys_via_config_cipher(
            a['wrong'], db_files, salt_to_dbs, {}, log.append, None, resolution={})
        text = '\n'.join(log)
        assert found == 0
        assert '候选' in text and ('salt' in text.lower() or '不属于' in text), \
            '必须说明"取到了候选但与所扫目录的 salt 不匹配"'
        assert '账号' in text, '必须点出最可能的原因：目录与当前登录的账号不是同一个'
        assert any(w in text for w in ('建议', '请')), '必须给出可操作的下一步'

    def test_open_process_denied_is_reported_with_advice(self, tmp_path, two_accounts,
                                                         monkeypatch):
        """OpenProcess 失败 → 与"打开了但没找到"必须能区分开，并提示管理员权限。"""
        a = two_accounts
        from engine.services.wechat_key_extract import collect_db_files
        monkeypatch.setattr(cce, 'find_wechat_pids', lambda: [(0, 4242)])
        monkeypatch.setattr(cce, 'scan_pid_for_config_cipher', lambda *args, **kw: {
            'needles': 0, 'nodes': 0, 'candidates': 0, 'verified': 0,
            'opened': False, 'open_error': 5, 'regions': 0, 'cand_salts': set()})
        monkeypatch.setattr(cce, '_detect_db_dirs', lambda: [a['wrong']])
        db_files, salt_to_dbs = collect_db_files(a['wrong'])
        log = []
        cce.extract_keys_via_config_cipher(a['wrong'], db_files, salt_to_dbs, {},
                                           log.append, None, resolution={})
        text = '\n'.join(log)
        # ⚠️ 不能只断言 "管理员"：流程里本来就有"无需管理员权限"这句横幅，会**假绿**。
        # 必须断言只有**新诊断**才会产生的字样。
        assert '打不开' in text and 'GetLastError' in text, \
            '必须把"打不开进程（OpenProcess 失败）"单独指出来'
        assert 'ERROR_ACCESS_DENIED' in text or '权限不足' in text
        assert '以管理员身份重新运行' in text, '必须给出可操作的建议'

    def test_no_pids_is_explained(self, tmp_path, two_accounts, monkeypatch):
        a = two_accounts
        from engine.services.wechat_key_extract import collect_db_files
        monkeypatch.setattr(cce, 'find_wechat_pids', lambda: [])
        db_files, salt_to_dbs = collect_db_files(a['wrong'])
        log = []
        assert cce.extract_keys_via_config_cipher(a['wrong'], db_files, salt_to_dbs, {},
                                                  log.append, None, resolution={}) == 0
        assert '未检测到微信进程' in '\n'.join(log)


class TestFindDbDirMatchingSalts:
    def test_picks_the_dir_with_most_hits(self, tmp_path, two_accounts):
        from engine.services.wechat_key_extract import collect_db_files
        a = two_accounts
        _f1, s_wrong = collect_db_files(a['wrong'])
        _f2, s_right = collect_db_files(a['right'])
        got_dir, hits = cce.find_db_dir_matching_salts(
            set(s_right), exclude=a['wrong'], db_dirs=[a['wrong'], a['right']])
        assert got_dir == a['right'] and hits == len(s_right)

    def test_returns_none_when_nothing_matches(self, tmp_path, two_accounts):
        from engine.services.wechat_key_extract import collect_db_files
        a = two_accounts
        got_dir, hits = cce.find_db_dir_matching_salts(
            {'0' * 32}, exclude=a['wrong'], db_dirs=[a['wrong'], a['right']])
        assert got_dir is None and hits == 0

    def test_never_raises_on_bad_dirs(self, two_accounts):
        got_dir, hits = cce.find_db_dir_matching_salts(
            {'0' * 32}, exclude=None, db_dirs=[r'Z:\definitely\missing', ''])
        assert got_dir is None and hits == 0


class TestKeyscanEndpointHonoursRetarget:
    """端点级接线测试：`/api/backup/keyscan` 必须把**换过之后的目录**用于保存。

    以前这个 SSE 端点**完全没有测试覆盖**（本项目踩过"静态测试全绿、整段端点不执行"的坑）。
    这里如果接线漏了，密钥会被存到**错的账号名下** —— 比"提取失败"更难发现。
    """

    def _make_app(self, tmp_path, monkeypatch, *, retarget):
        import engine.services.config_cipher_extract as cce_mod
        import engine.services.wechat_key_extract as wke_mod
        import engine.config_file as cfg_mod

        wrong = tmp_path / 'account_wrong' / 'db_storage'
        right = tmp_path / 'account_right' / 'db_storage'
        wrong.mkdir(parents=True)
        right.mkdir(parents=True)

        monkeypatch.setattr(wke_mod, 'collect_db_files',
                            lambda d: ([('a.db', 'p', 4096, 'aa' * 16, b'\x00' * 4096)], {'aa' * 16: ['a.db']}))
        monkeypatch.setattr(wke_mod, 'load_from_config',
                            lambda *a, **k: 0)          # 配置里没有现成密钥 → 走扫描
        saved = []
        monkeypatch.setattr(wke_mod, 'save_key_results',
                            lambda files, s2d, km, db_dir, pf: saved.append(db_dir))
        monkeypatch.setattr(cfg_mod, 'get_db_keys', lambda: {'a.db': 'bb' * 32})
        messages = []

        def _fake_extract(db_dir, db_files, salt_to_dbs, key_map, print_fn, progress_fn,
                          *, resolution=None):
            print_fn('[Cipher] fake scan')
            if retarget:
                assert resolution is not None, '端点必须把 resolution 出参传进来'
                resolution.update({'db_dir': str(right), 'db_files': [('b', 'p', 1, 'cc' * 16, b'')],
                                   'salt_to_dbs': {'cc' * 16: ['b']}, 'retargeted': True,
                                   'reason': 'account_mismatch'})
            messages.append(str(db_dir))
            key_map['cc' * 16] = 'dd' * 32
            return 1

        monkeypatch.setattr(cce_mod, 'extract_keys_via_config_cipher', _fake_extract)

        from web.app import create_app
        app = create_app(str(tmp_path / 'decrypted'), wxid='wxid_owner')
        return app, str(wrong), str(right), saved, messages

    def test_uses_the_retargeted_dir_for_saving(self, tmp_path, monkeypatch):
        app, wrong, right, saved, _msgs = self._make_app(tmp_path, monkeypatch, retarget=True)
        r = app.test_client().post('/api/backup/keyscan', json={'db_dir': wrong})
        body = r.data.decode('utf-8', 'replace')
        assert r.status_code == 200
        assert '已自动改用微信正在使用的账号目录' in body, \
            '换目录这件事必须在流里说出来（用户要能看到），实际流：%r' % body[:400]
        assert saved and os.path.normcase(os.path.normpath(saved[-1])) == \
            os.path.normcase(os.path.normpath(right)), \
            '密钥必须存到换过之后的目录，否则会被记在错的账号名下（实际存到 %r）' % (saved,)

    def test_without_retarget_still_saves_to_the_given_dir(self, tmp_path, monkeypatch):
        app, wrong, _right, saved, _m = self._make_app(tmp_path, monkeypatch, retarget=False)
        r = app.test_client().post('/api/backup/keyscan', json={'db_dir': wrong})
        assert r.status_code == 200
        assert saved and os.path.normcase(os.path.normpath(saved[-1])) == \
            os.path.normcase(os.path.normpath(wrong)), '没换目录时不许改变保存目标'
        assert '已自动改用' not in r.data.decode('utf-8', 'replace')
