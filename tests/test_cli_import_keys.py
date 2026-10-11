"""CLI `import-keys`：日志文件（含 GBK）读取与 `--export` 清单导出。

全部使用**合成**库与伪造密钥，不涉及真实数据。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

import main as cli
from engine import config_file
from tests.test_manual_keys import KEY_A, KEY_B, make_page1

SALT_A = b'a' * 16
SALT_B = b'b' * 16


def _log():
    return '\n'.join([
        '[Cipher] 检测到微信进程: [26228, 10836]',
        '  [Cipher-FOUND] message\\message_0.db salt=%s -> %s' % (SALT_A.hex(), KEY_A),
        '  [Cipher-FOUND] contact\\contact.db salt=%s -> %s' % (SALT_B.hex(), KEY_B),
        '  [Cipher-FOUND] message\\message_1.db salt=10ff********db3c -> 81ee********87ae',
        '[Cipher] PID=26228: 2 个密钥验证通过',
    ])


@pytest.fixture
def db_dir(tmp_path):
    root = tmp_path / 'db_storage'
    (root / 'message').mkdir(parents=True)
    (root / 'contact').mkdir(parents=True)
    (root / 'message' / 'message_0.db').write_bytes(make_page1(KEY_A, SALT_A))
    (root / 'contact' / 'contact.db').write_bytes(make_page1(KEY_B, SALT_B))
    return str(root)


@pytest.fixture
def clean_config(tmp_path, monkeypatch):
    cfg = str(tmp_path / 'cfg' / '.wechat_exp_config.json')
    os.makedirs(os.path.dirname(cfg), exist_ok=True)
    monkeypatch.setattr(config_file, '_config_path', lambda: cfg)
    return cfg


@pytest.fixture
def run(monkeypatch):
    def _run(*argv):
        monkeypatch.setattr(sys, 'argv', ['main.py'] + [str(a) for a in argv])
        cli.main()
    return _run


class TestLogFileInput:
    def test_utf8_log_file(self, tmp_path, db_dir, clean_config, run, capsys):
        p = tmp_path / 'log.txt'
        p.write_text(_log(), encoding='utf-8')
        run('import-keys', '--file', p, '--db-dir', db_dir, '--dry-run')
        out = capsys.readouterr().out
        assert '匹配' in out or 'matched' in out

    def test_gbk_log_file_does_not_crash(self, tmp_path, db_dir, clean_config,
                                         run, capsys):
        """GBK 日志文件此前会直接 UnicodeDecodeError 崩掉。"""
        p = tmp_path / 'log_gbk.txt'
        p.write_bytes(_log().encode('gbk'))
        run('import-keys', '--file', p, '--db-dir', db_dir, '--dry-run')
        out = capsys.readouterr().out
        assert 'UnicodeDecodeError' not in out
        assert '检测到微信进程' in out or '匹配' in out or 'matched' in out

    def test_summary_is_printed(self, tmp_path, db_dir, clean_config, run, capsys):
        p = tmp_path / 'log.txt'
        p.write_text(_log(), encoding='utf-8')
        run('import-keys', '--file', p, '--db-dir', db_dir, '--dry-run')
        out = capsys.readouterr().out
        assert '噪声' in out or '打码' in out

    def test_noise_lines_not_reported_as_errors(self, tmp_path, db_dir, clean_config,
                                                run, capsys):
        p = tmp_path / 'log.txt'
        p.write_text(_log(), encoding='utf-8')
        run('import-keys', '--file', p, '--db-dir', db_dir, '--dry-run')
        out = capsys.readouterr().out
        assert '未识别到 64 位十六进制密钥' not in out


class TestExport:
    def test_export_writes_reimportable_list(self, tmp_path, db_dir, clean_config,
                                             run, capsys):
        p = tmp_path / 'log.txt'
        p.write_text(_log(), encoding='utf-8')
        out_file = tmp_path / 'exported.txt'
        run('import-keys', '--file', p, '--db-dir', db_dir, '--dry-run',
            '--export', out_file)
        assert out_file.is_file()
        content = out_file.read_text(encoding='utf-8')
        assert KEY_A in content and KEY_B in content
        assert 'message\\message_0.db = ' in content
        assert '勿' in content

        # 往返：导出文件能被再次解析出同样的两条
        from engine import manual_keys as mk
        entries = mk.parse_entries(content)
        assert len([e for e in entries if e['key']]) == 2

    def test_export_saved_run_also_works(self, tmp_path, db_dir, clean_config,
                                         run, capsys):
        p = tmp_path / 'log.txt'
        p.write_text(_log(), encoding='utf-8')
        out_file = tmp_path / 'exported.txt'
        run('import-keys', '--file', p, '--db-dir', db_dir, '--export', out_file)
        assert out_file.is_file()
        assert '已保存' in capsys.readouterr().out

    def test_export_reports_count(self, tmp_path, db_dir, clean_config, run, capsys):
        p = tmp_path / 'log.txt'
        p.write_text(_log(), encoding='utf-8')
        out_file = tmp_path / 'exported.txt'
        run('import-keys', '--file', p, '--db-dir', db_dir, '--dry-run',
            '--export', out_file)
        assert '2' in capsys.readouterr().out
