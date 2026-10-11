"""`/api/keys/*` 的日志粘贴支持：文件上传、GBK 回退、汇总、清单导出。

全部使用**合成**库（复用 test_manual_keys 的 make_page1），不涉及真实密钥。
"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

from engine import config_file
from tests.test_manual_keys import KEY_A, KEY_B, make_page1
from web.app import create_app
from web.routes import keys_api

SALT_A = b'a' * 16
SALT_B = b'b' * 16
MASKED_LINE = '  [Cipher-FOUND] message\\message_1.db salt=10ff********db3c -> 81ee********87ae'


def _log(key_a=KEY_A, salt_a=SALT_A, extra=()):
    lines = [
        '[Cipher] 检测到微信进程: [26228, 10836, 26808]',
        '[Cipher] 只读扫描 WCDB Config.Cipher 对象 (无需管理员权限) ...',
        '  [Cipher-FOUND] message\\message_0.db salt=%s -> %s' % (salt_a.hex(), key_a),
        '  [Cipher-FOUND] contact\\contact.db salt=%s -> %s' % (SALT_B.hex(), KEY_B),
        '[Cipher] PID=26228: 2 个密钥验证通过 (节点 159, 候选 47)',
    ]
    lines.extend(extra)
    return '\n'.join(lines)


@pytest.fixture
def db_dir(tmp_path):
    root = tmp_path / 'db_storage'
    (root / 'message').mkdir(parents=True)
    (root / 'contact').mkdir(parents=True)
    (root / 'message' / 'message_0.db').write_bytes(make_page1(KEY_A, SALT_A))
    (root / 'contact' / 'contact.db').write_bytes(make_page1(KEY_B, SALT_B))
    return str(root)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """配置文件与**导出目录**都重定向到临时目录，绝不碰用户真实配置/仓库 output/。"""
    cfg = str(tmp_path / 'cfg' / '.wechat_exp_config.json')
    os.makedirs(os.path.dirname(cfg), exist_ok=True)
    monkeypatch.setattr(config_file, '_config_path', lambda: cfg)
    # known-issues #41：导出端点默认写 <仓库根>/output，测试必须把它重定向到 tmp_path，
    # 否则每跑一次测试就往仓库真实目录里多一个与真实导出无法分辨的合成文件。
    monkeypatch.setattr(keys_api, '_export_dir', lambda: str(tmp_path / 'export'))
    d = tmp_path / 'decrypted'
    d.mkdir()
    app = create_app(str(d), wxid='wxid_example12345')
    app.config['TESTING'] = True
    return app.test_client()


class TestVerifyWithLogText:
    def test_accepts_full_log_and_reports_summary(self, client, db_dir):
        r = client.post('/api/keys/verify', json={'db_dir': db_dir, 'text': _log()})
        assert r.status_code == 200
        body = r.get_json()
        statuses = [x['status'] for x in body['results']]
        assert statuses == ['noise', 'noise', 'matched', 'matched', 'noise']
        assert body['summary']['noise'] == 3
        assert body['summary']['cipher_log'] == 2
        assert body['summary']['errors'] == 0

    def test_does_not_leak_full_key(self, client, db_dir):
        body = client.post('/api/keys/verify',
                           json={'db_dir': db_dir, 'text': _log()}).get_json()
        raw = str(body)
        assert KEY_A not in raw and KEY_B not in raw

    def test_masked_line_gets_actionable_message(self, client, db_dir):
        body = client.post('/api/keys/verify',
                           json={'db_dir': db_dir,
                                 'text': _log(extra=(MASKED_LINE,))}).get_json()
        masked = [x for x in body['results'] if x['status'] == 'masked']
        assert len(masked) == 1
        assert '打码' in masked[0]['message'] or '脱敏' in masked[0]['message']
        assert body['summary']['masked'] == 1
        assert body['summary']['errors'] == 0

    def test_salt_mismatch_is_reported(self, client, db_dir):
        """日志来自另一台机器（密钥与盐都对不上）→ 诊断要说明是 salt 不符。"""
        log = '  [Cipher-FOUND] message\\message_0.db salt=%s -> %s' % (
            (b'z' * 16).hex(), KEY_B)      # message_0 用的是 KEY_A，故此密钥校验必失败
        body = client.post('/api/keys/verify',
                           json={'db_dir': db_dir, 'text': log}).get_json()
        assert body['results'][0]['status'] == 'no_match'
        assert body['results'][0]['saltMismatch'] is True
        assert 'salt' in body['results'][0]['message'].lower()

    def test_empty_input_is_400(self, client, db_dir):
        r = client.post('/api/keys/verify', json={'db_dir': db_dir, 'text': '   '})
        assert r.status_code == 400


class TestVerifyWithUploadedFile:
    def test_multipart_upload_utf8(self, client, db_dir):
        data = {'db_dir': db_dir,
                'file': (io.BytesIO(_log().encode('utf-8')), 'keyscan.log')}
        r = client.post('/api/keys/verify', data=data,
                        content_type='multipart/form-data')
        assert r.status_code == 200
        body = r.get_json()
        assert [x['status'] for x in body['results']].count('matched') == 2

    def test_multipart_upload_gbk_fallback(self, client, db_dir):
        """Windows 控制台重定向出来的日志常是 GBK，必须能回退解码。"""
        data = {'db_dir': db_dir,
                'file': (io.BytesIO(_log().encode('gbk')), 'keyscan.log')}
        r = client.post('/api/keys/verify', data=data,
                        content_type='multipart/form-data')
        assert r.status_code == 200
        body = r.get_json()
        assert [x['status'] for x in body['results']].count('matched') == 2
        assert body['summary']['noise'] == 3

    def test_upload_takes_precedence_over_text(self, client, db_dir):
        data = {'db_dir': db_dir, 'text': 'garbage-without-hex',
                'file': (io.BytesIO(_log().encode('utf-8')), 'k.log')}
        body = client.post('/api/keys/verify', data=data,
                           content_type='multipart/form-data').get_json()
        assert body['summary']['cipher_log'] == 2


class TestSaveWithLog:
    def test_save_writes_both_databases(self, client, db_dir):
        r = client.post('/api/keys/save', json={'db_dir': db_dir, 'text': _log()})
        assert r.status_code == 200
        body = r.get_json()
        assert body['saved'] == 2
        assert body['status']['verified'] == 2

    def test_save_ignores_noise_lines(self, client, db_dir):
        body = client.post('/api/keys/save',
                           json={'db_dir': db_dir,
                                 'text': _log(extra=(MASKED_LINE,))}).get_json()
        assert body['saved'] == 2

    def test_save_reports_summary(self, client, db_dir):
        body = client.post('/api/keys/save',
                           json={'db_dir': db_dir, 'text': _log()}).get_json()
        assert body['summary']['cipher_log'] == 2


class TestExportEndpoint:
    def test_export_returns_downloadable_list(self, client, db_dir):
        r = client.post('/api/keys/export', json={'db_dir': db_dir, 'text': _log()})
        assert r.status_code == 200
        assert 'attachment' in r.headers['Content-Disposition']
        assert '.txt' in r.headers['Content-Disposition']
        text = r.data.decode('utf-8')
        assert KEY_A in text and KEY_B in text
        assert '勿' in text                       # 密钥警告
        assert 'message\\message_0.db = ' in text
        assert 'contact\\contact.db = ' in text

    def test_export_is_reimportable(self, client, db_dir):
        """导出的清单必须能直接粘回导入（往返一致）。"""
        r = client.post('/api/keys/export', json={'db_dir': db_dir, 'text': _log()})
        text = r.data.decode('utf-8')
        back = client.post('/api/keys/verify',
                           json={'db_dir': db_dir, 'text': text}).get_json()
        statuses = [x['status'] for x in back['results']]
        assert statuses.count('matched') == 2

    def test_export_accepts_upload(self, client, db_dir):
        data = {'db_dir': db_dir,
                'file': (io.BytesIO(_log().encode('gbk')), 'k.log')}
        r = client.post('/api/keys/export', data=data,
                        content_type='multipart/form-data')
        assert r.status_code == 200
        assert KEY_A in r.data.decode('utf-8')

    def test_export_with_nothing_usable_is_400(self, client, db_dir):
        r = client.post('/api/keys/export',
                        json={'db_dir': db_dir, 'text': MASKED_LINE})
        assert r.status_code == 400
        assert 'error' in r.get_json()

    def test_export_filename_has_timestamp(self, client, db_dir):
        r = client.post('/api/keys/export', json={'db_dir': db_dir, 'text': _log()})
        cd = r.headers['Content-Disposition']
        assert 'keys_export_' in cd


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_OUTPUT_DIR = os.path.join(_REPO_ROOT, 'output')


def _repo_output_snapshot():
    """仓库真实 output/ 的目录快照：{文件名: (大小, mtime_ns)}。

    **为什么不能只比文件名集合**：导出文件名的时间戳只有**秒**精度
    （`keys_export_%Y%m%d_%H%M%S.txt`）。同一个 pytest 进程里先跑的导出用例
    若已在「本秒」写过同名文件，守卫用例再被污染也只是**覆盖同一个名字**，
    名字集合照样完全相等 —— 纯名字集合断言会给出**假绿**（实测如此）。
    把大小与 mtime_ns 一起纳入快照，覆盖同样会被判出来，断言才真有判别力。
    """
    if not os.path.isdir(_REPO_OUTPUT_DIR):
        return {}
    snap = {}
    for name in os.listdir(_REPO_OUTPUT_DIR):
        try:
            st = os.stat(os.path.join(_REPO_OUTPUT_DIR, name))
        except OSError:          # 并发删除等短暂状态：跳过即可
            continue
        snap[name] = (st.st_size, st.st_mtime_ns)
    return snap


class TestExportDoesNotTouchRepoOutput:
    """known-issues #41：导出用例每跑一次就往仓库**真实** output/ 里落一个文件。

    合成导出与用户真实导出**同名同格式同目录**，肉眼无法分辨，
    于是 output/ 的审计（从中找出真实导出）不再可能。此处守住这条底线：
    跑导出用例的前后，仓库真实 output/ 的文件集合必须完全一致。
    """

    def test_export_cases_do_not_add_files_to_repo_output(self, client, db_dir):
        before = _repo_output_snapshot()

        r_ok = client.post('/api/keys/export', json={'db_dir': db_dir, 'text': _log()})
        assert r_ok.status_code == 200
        r_empty = client.post('/api/keys/export',
                              json={'db_dir': db_dir, 'text': MASKED_LINE})
        assert r_empty.status_code == 400

        after = _repo_output_snapshot()
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        changed = sorted(n for n in set(before) & set(after) if before[n] != after[n])
        assert after == before, (
            '导出用例污染了仓库真实目录 %s：新增 %d 个 %r，被改写 %d 个 %r，减少 %d 个 %r'
            % (_REPO_OUTPUT_DIR, len(added), added[:5],
               len(changed), changed[:5], len(removed), removed[:5]))


class TestExportDirIsInjectable:
    """导出落盘目录必须是一个**生产路径本身就在用**的可注入缝。

    这里锁死缝的两端，防止「缝被改歪了却没人发现」：
      · 默认（非冻结）= <仓库根>/output  —— 真实用户点导出的落盘位置
      · 冻结（PyInstaller）= <exe 所在目录>/output
      · 端点确实走这条缝（而不是自己另算一份路径）
    """

    def test_default_is_repo_output(self, monkeypatch):
        monkeypatch.delattr(sys, 'frozen', raising=False)
        assert (os.path.normpath(keys_api._export_dir())
                == os.path.normpath(_REPO_OUTPUT_DIR))

    def test_frozen_uses_exe_dir(self, monkeypatch, tmp_path):
        exe_dir = tmp_path / 'app'
        exe_dir.mkdir()
        monkeypatch.setattr(sys, 'frozen', True, raising=False)
        monkeypatch.setattr(sys, 'executable', str(exe_dir / 'wechat_exp.exe'))
        assert (os.path.normpath(keys_api._export_dir())
                == os.path.normpath(os.path.join(str(exe_dir), 'output')))

    def test_endpoint_writes_into_the_seam_dir(self, client, tmp_path,
                                               monkeypatch, db_dir):
        """端点必须真的用这条缝 —— 否则「可注入」是假的。"""
        target = tmp_path / 'redirected_export'
        monkeypatch.setattr(keys_api, '_export_dir', lambda: str(target))
        r = client.post('/api/keys/export', json={'db_dir': db_dir, 'text': _log()})
        assert r.status_code == 200
        written = sorted(os.listdir(target))
        assert len(written) == 1 and written[0].startswith('keys_export_')
        assert KEY_A in (target / written[0]).read_text(encoding='utf-8')
