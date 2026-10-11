"""手动输入密钥：日志格式自动识别 / 噪声分离 / 打码识别 / 盐不符诊断 / 清单导出。

背景（用户实测反馈）：把密钥扫描的 stdout 整段粘进 `/keys` 页面时**每一行都失败**，
且报错毫无帮助：
  - `[Cipher-FOUND] message\\message_0.db salt=<32hex> -> <64hex>` 的密钥能提取，
    但**盐值丢失**（salt_hint=None）、**路径被污染成整行垃圾残留串**；
  - 日志头尾行（`[Cipher] 检测到微信进程…` / `[Cipher] PID=…`）被当成**错误**；
  - 粘贴**打码**过的日志时只报「未识别到 64 位十六进制密钥」，不告诉用户原因是打码。

本文件全部使用**合成**数据（复用 test_manual_keys 的 make_page1），不涉及真实密钥。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

from engine import config_file
from engine import manual_keys as mk
from tests.test_manual_keys import KEY_A, KEY_B, KEY_C, make_page1


SALT_A = b'a' * 16
SALT_B = b'b' * 16
SALT_C = b'c' * 16


@pytest.fixture
def clean_config(tmp_path, monkeypatch):
    """把配置文件重定向到临时目录，绝不碰用户真实配置（与 test_manual_keys 同款）。"""
    cfg_path = str(tmp_path / 'cfg' / '.wechat_exp_config.json')
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    monkeypatch.setattr(config_file, '_config_path', lambda: cfg_path)
    return cfg_path


def _found_line(rel, key_hex, salt_bytes, indent='  '):
    return '%s[Cipher-FOUND] %s salt=%s -> %s' % (
        indent, rel, salt_bytes.hex(), key_hex)


@pytest.fixture
def db_dir(tmp_path):
    """三个库，盐分别为 SALT_A/B/C，密钥分别为 KEY_A/B/C。"""
    root = tmp_path / 'db_storage'
    (root / 'message').mkdir(parents=True)
    (root / 'contact').mkdir(parents=True)
    (root / 'message' / 'message_0.db').write_bytes(make_page1(KEY_A, SALT_A))
    (root / 'contact' / 'contact.db').write_bytes(make_page1(KEY_B, SALT_B))
    (root / 'session').mkdir(parents=True)
    (root / 'session' / 'session.db').write_bytes(make_page1(KEY_C, SALT_C))
    return str(root)


# --------------------------------------------------------------------------
# A. Cipher 日志格式识别
# --------------------------------------------------------------------------

class TestCipherLogFormat:
    def test_single_line_extracts_all_three_fields(self):
        e = mk.parse_entries(_found_line(r'message\message_0.db', KEY_A, SALT_A))[0]
        assert e['key'] == KEY_A
        assert e['salt_hint'] == SALT_A.hex()
        assert e['db_hint'] == r'message\message_0.db'
        assert e['format'] == 'config_cipher_log'
        assert e['kind'] == 'key'
        assert e['error'] is None

    def test_no_garbage_residue_in_db_hint(self):
        """旧行为把整行残留串当 db_hint（`[Cipher-FOUND] … salt=… ->`）。"""
        e = mk.parse_entries(_found_line(r'message\message_0.db', KEY_A, SALT_A))[0]
        assert '[Cipher-FOUND]' not in e['db_hint']
        assert 'salt=' not in e['db_hint']
        assert '->' not in e['db_hint']

    def test_forward_slash_path(self):
        e = mk.parse_entries(_found_line('message/message_0.db', KEY_A, SALT_A))[0]
        assert e['db_hint'] == 'message/message_0.db'

    def test_deep_relative_path(self):
        e = mk.parse_entries(_found_line(r'message\sub\deep.db', KEY_A, SALT_A))[0]
        assert e['db_hint'] == r'message\sub\deep.db'

    def test_no_indent(self):
        e = mk.parse_entries(_found_line('contact/contact.db', KEY_B, SALT_B,
                                         indent=''))[0]
        assert e['key'] == KEY_B and e['db_hint'] == 'contact/contact.db'

    def test_crlf_input(self):
        text = _found_line('a.db', KEY_A, SALT_A) + '\r\n'
        e = mk.parse_entries(text)[0]
        assert e['key'] == KEY_A and e['db_hint'] == 'a.db'

    def test_uppercase_hex_accepted(self):
        salt_hex = ('dead' * 8).upper()          # 32 位、含字母、大写
        e = mk.parse_entries('  [Cipher-FOUND] a.db salt=%s -> %s'
                            % (salt_hex, KEY_C.upper()))[0]
        assert e['kind'] == 'key'
        assert e['key'] == KEY_C                 # 统一小写存储
        assert e['salt_hint'] == 'dead' * 8

    def test_full_user_shaped_log(self, db_dir):
        """用户贴的整段日志：头 + N 行 FOUND + 尾，必须零错误。"""
        lines = [
            '[Cipher] 检测到微信进程: [26228, 10836, 26808, 27220, 17892]',
            '[Cipher] 只读扫描 WCDB Config.Cipher 对象 (无需管理员权限) ...',
            _found_line(r'message\message_0.db', KEY_A, SALT_A),
            _found_line(r'contact\contact.db', KEY_B, SALT_B),
            _found_line(r'session\session.db', KEY_C, SALT_C),
            '[Cipher] PID=26228: 32 个密钥验证通过 (节点 159, 候选 47)',
        ]
        entries = mk.parse_entries('\n'.join(lines))
        assert len(entries) == len(lines)          # 行号与输入行一一对应
        keys = [e for e in entries if e['kind'] == 'key']
        noise = [e for e in entries if e['kind'] == 'noise']
        assert len(keys) == 3
        assert len(noise) == 3
        assert not [e for e in entries if e['error']]
        assert [e['db_hint'] for e in keys] == [
            r'message\message_0.db', r'contact\contact.db', r'session\session.db']


# --------------------------------------------------------------------------
# B. 噪声与错误分离
# --------------------------------------------------------------------------

class TestNoiseVsError:
    """日志行不该被当成错误，但**非日志形态**的无密钥行仍是错误（保持既有契约）。"""

    @pytest.mark.parametrize('line', [
        '[Cipher] 检测到微信进程: [26228, 10836, 26808, 27220, 17892]',
        '[Cipher] 只读扫描 WCDB Config.Cipher 对象 (无需管理员权限) ...',
        '[Cipher] PID=26228: 32 个密钥验证通过 (节点 159, 候选 47)',
        '  [Cipher] 扫描完成',
        '[SomeTool] 没有找到任何东西',
    ])
    def test_bracketed_log_line_is_noise(self, line):
        e = mk.parse_entries(line)[0]
        assert e['kind'] == 'noise'
        assert e['error'] is None

    def test_plain_garbage_is_still_an_error(self):
        """既有契约：`这不是密钥` 仍须报错（不能被降级成噪声）。"""
        e = mk.parse_entries('这不是密钥')[0]
        assert e['kind'] != 'noise'
        assert e['error']

    def test_wrong_length_hex_is_error_not_noise(self):
        """含 hex 样 token 但长度不对 → 是错误，不是噪声。"""
        e = mk.parse_entries('some-key = ' + 'ab' * 31)[0]   # 62 位
        assert e['kind'] != 'noise'
        assert e['error']

    def test_empty_and_comment_lines_skipped(self):
        entries = mk.parse_entries('\n'.join(['', '   ', '# 注释', '// 注释', KEY_A]))
        assert len(entries) == 1
        assert entries[0]['key'] == KEY_A


# --------------------------------------------------------------------------
# C. 打码识别
# --------------------------------------------------------------------------

class TestMaskedDetection:
    def test_user_reported_masked_shape(self):
        """用户贴的形态：前 4 位 + 8 个星号 + 后 4 位。"""
        e = mk.parse_entries(
            '  [Cipher-FOUND] message\\message_0.db '
            'salt=10ff********db3c -> 81ee********87ae')[0]
        assert e['kind'] == 'masked'
        assert e['key'] is None
        assert set(e['masked_fields']) == {'salt', 'key'}
        assert '打码' in e['note'] or '脱敏' in e['note']

    def test_masked_message_tells_user_what_to_do(self):
        e = mk.parse_entries(
            '  [Cipher-FOUND] a.db salt=10ff********db3c -> 81ee********87ae')[0]
        assert '未打码' in e['note'] or '完整' in e['note']

    def test_project_mask_key_form_is_detected(self):
        """项目自身 mask_key() 的形态：前 6 + '...' + 后 4（从界面复制会得到它）。"""
        masked = mk.mask_key(KEY_A)
        assert '...' in masked
        e = mk.parse_entries('  [Cipher-FOUND] a.db salt=%s -> %s'
                            % (SALT_A.hex(), masked))[0]
        assert e['kind'] == 'masked'
        assert 'key' in e['masked_fields']

    def test_unicode_ellipsis_is_detected(self):
        e = mk.parse_entries('  [Cipher-FOUND] a.db salt=%s -> %s\u2026%s'
                            % (SALT_A.hex(), KEY_A[:8], KEY_A[-4:]))[0]
        assert e['kind'] == 'masked'

    def test_only_salt_masked(self):
        e = mk.parse_entries('  [Cipher-FOUND] a.db salt=10ff****db3c -> %s'
                            % KEY_A)[0]
        assert e['kind'] == 'masked'
        assert e['masked_fields'] == ['salt']

    def test_masked_is_not_counted_as_error(self):
        text = '\n'.join([
            '  [Cipher-FOUND] a.db salt=10ff********db3c -> 81ee********87ae',
            '  [Cipher-FOUND] b.db salt=20ff********ec3d -> 92ee********98af',
        ])
        entries = mk.parse_entries(text)
        assert all(e['kind'] == 'masked' for e in entries)
        assert not [e for e in entries if e['error']]


# --------------------------------------------------------------------------
# D. 既有格式向后兼容（不能被本次改动破坏）
# --------------------------------------------------------------------------

class TestExistingFormatsStillWork:
    def test_plain_hex(self):
        e = mk.parse_entries(KEY_A)[0]
        assert e['key'] == KEY_A and e['format'] == 'generic'
        assert e['db_hint'] is None and e['salt_hint'] is None and e['error'] is None

    def test_db_equals_key(self):
        e = mk.parse_entries('message_0.db = ' + KEY_A)[0]
        assert e['key'] == KEY_A and e['db_hint'] == 'message_0.db'

    def test_path_colon_key(self):
        e = mk.parse_entries('message/message_1.db: ' + KEY_C)[0]
        assert e['db_hint'] == 'message/message_1.db'

    def test_salt_equals_key(self):
        e = mk.parse_entries(SALT_A.hex() + ' = ' + KEY_A)[0]
        assert e['key'] == KEY_A and e['salt_hint'] == SALT_A.hex()

    def test_wechat_blob_key_plus_salt(self):
        e = mk.parse_entries("x'" + KEY_A + SALT_A.hex() + "'")[0]
        assert e['key'] == KEY_A and e['salt_hint'] == SALT_A.hex()


# --------------------------------------------------------------------------
# E. 匹配：用上日志给的路径与盐 + HMAC 仍为最终裁决
# --------------------------------------------------------------------------

class TestMatchingWithLogHints:
    def test_log_line_matches_by_path_and_verifies(self, db_dir, clean_config):
        entries = mk.parse_entries(
            _found_line(r'message\message_0.db', KEY_A, SALT_A))
        res = mk.match_entries(db_dir, entries)
        assert res[0]['status'] == 'matched'
        assert [m['rel'].replace('/', '\\') for m in res[0]['matched']] == \
            [r'message\message_0.db']

    def test_wrong_key_for_that_path_does_not_match(self, db_dir, clean_config):
        """路径提示不能让错密钥过关 —— HMAC 仍是最终裁决。"""
        entries = mk.parse_entries(
            _found_line(r'message\message_0.db', KEY_C, SALT_A))
        res = mk.match_entries(db_dir, entries)
        assert res[0]['status'] == 'no_match'

    def test_salt_mismatch_is_diagnosed(self, db_dir, clean_config):
        """日志来自另一台机器：密钥与盐都对不上本地库 → 明确诊断，而不是含糊的「不匹配」。"""
        entries = mk.parse_entries(
            _found_line(r'message\message_0.db', KEY_C, SALT_C))   # 另一台机器的盐+密钥
        res = mk.match_entries(db_dir, entries)
        assert res[0]['status'] == 'no_match'
        assert res[0].get('saltMismatch') is True
        assert 'salt' in res[0]['message'].lower()
        assert '另一台机器' in res[0]['message']

    def test_salt_mismatch_but_key_valid_warns_but_passes(self, db_dir, clean_config):
        """盐字段与本地不符但密钥确实有效（旧日志的盐陈旧）→ 放行并提示。

        判定以 HMAC 为准（密钥正确就该用），但必须提示盐的异常，便于用户察觉日志陈旧。
        """
        entries = mk.parse_entries(
            _found_line(r'message\message_0.db', KEY_A, SALT_C))
        res = mk.match_entries(db_dir, entries)
        assert res[0]['status'] == 'matched'
        assert res[0].get('saltMismatch') is True
        assert 'salt' in res[0]['message'].lower()

    def test_noise_entries_are_reported_as_noise_not_error(self, db_dir, clean_config):
        text = '\n'.join([
            '[Cipher] PID=1: 3 个密钥验证通过',
            _found_line(r'contact\contact.db', KEY_B, SALT_B),
        ])
        res = mk.match_entries(db_dir, mk.parse_entries(text))
        assert res[0]['status'] == 'noise'
        assert res[1]['status'] == 'matched'

    def test_masked_entries_are_reported_as_masked(self, db_dir, clean_config):
        res = mk.match_entries(db_dir, mk.parse_entries(
            '  [Cipher-FOUND] a.db salt=10ff********db3c -> 81ee********87ae'))
        assert res[0]['status'] == 'masked'
        assert '打码' in res[0]['message'] or '脱敏' in res[0]['message']

    def test_full_log_end_to_end(self, db_dir, clean_config):
        """整段日志 → 三个库全部 matched，噪声行不影响。"""
        text = '\n'.join([
            '[Cipher] 检测到微信进程: [26228, 10836]',
            _found_line(r'message\message_0.db', KEY_A, SALT_A),
            _found_line(r'contact\contact.db', KEY_B, SALT_B),
            _found_line(r'session\session.db', KEY_C, SALT_C),
            '[Cipher] PID=26228: 32 个密钥验证通过 (节点 159, 候选 47)',
        ])
        res = mk.match_entries(db_dir, mk.parse_entries(text))
        statuses = [r['status'] for r in res]
        assert statuses == ['noise', 'matched', 'matched', 'matched', 'noise']

    def test_apply_entries_saves_all_three(self, db_dir, clean_config):
        text = '\n'.join([
            _found_line(r'message\message_0.db', KEY_A, SALT_A),
            _found_line(r'contact\contact.db', KEY_B, SALT_B),
            _found_line(r'session\session.db', KEY_C, SALT_C),
        ])
        out = mk.apply_entries(db_dir, mk.parse_entries(text))
        assert out['saved'] == 3
        assert out['status']['verified'] == 3


# --------------------------------------------------------------------------
# F. 汇总
# --------------------------------------------------------------------------

class TestSummary:
    def test_summarize_counts_each_kind(self):
        text = '\n'.join([
            '[Cipher] 检测到微信进程: [1, 2]',
            _found_line(r'a.db', KEY_A, SALT_A),
            _found_line(r'b.db', KEY_B, SALT_B),
            '  [Cipher-FOUND] c.db salt=10ff********db3c -> 81ee********87ae',
            'garbage-without-hex',
        ])
        s = mk.summarize(mk.parse_entries(text))
        assert s['total'] == 5
        assert s['cipher_log'] == 2
        assert s['masked'] == 1
        assert s['noise'] == 1
        assert s['errors'] == 1

    def test_summary_keys_present(self):
        s = mk.summarize([])
        for k in ('total', 'cipher_log', 'generic', 'noise', 'masked', 'errors'):
            assert k in s


# --------------------------------------------------------------------------
# G. 导出对应关系清单（可回读 = 往返一致）
# --------------------------------------------------------------------------

class TestExportKeyList:
    def test_writes_reimportable_lines(self, tmp_path, db_dir, clean_config):
        entries = mk.parse_entries('\n'.join([
            _found_line(r'message\message_0.db', KEY_A, SALT_A),
            _found_line(r'contact\contact.db', KEY_B, SALT_B),
        ]))
        out_path = str(tmp_path / 'keys_export.txt')
        res = mk.export_key_list(db_dir, entries, out_path)
        assert res['count'] == 2
        assert os.path.isfile(out_path)

        # 往返：导出文件必须能被 parse_entries 读回同样的 (路径, 密钥)
        reread = mk.parse_entries(open(out_path, encoding='utf-8').read())
        pairs = {e['db_hint'].replace('/', '\\'): e['key']
                 for e in reread if e['key']}
        assert pairs == {r'message\message_0.db': KEY_A, r'contact\contact.db': KEY_B}

    def test_header_warns_about_secrets(self, tmp_path, db_dir, clean_config):
        entries = mk.parse_entries(_found_line(r'a.db', KEY_A, SALT_A))
        out_path = str(tmp_path / 'k.txt')
        mk.export_key_list(db_dir, entries, out_path)
        head = open(out_path, encoding='utf-8').read()
        assert '完整密钥' in head
        assert '勿' in head          # 明确警告不要外传/入库

    def test_unverified_pairs_exported_with_status_comment(self, tmp_path,
                                                           db_dir, clean_config):
        """校不过的也要导出（用户可能要在另一台机器上用），但标注状态。"""
        entries = mk.parse_entries(
            _found_line(r'message\message_0.db', KEY_C, SALT_A))   # 错密钥
        out_path = str(tmp_path / 'k.txt')
        mk.export_key_list(db_dir, entries, out_path)
        content = open(out_path, encoding='utf-8').read()
        assert KEY_C in content
        assert '未通过' in content

    def test_masked_entries_not_exported(self, tmp_path, db_dir, clean_config):
        entries = mk.parse_entries(
            '  [Cipher-FOUND] a.db salt=10ff********db3c -> 81ee********87ae')
        out_path = str(tmp_path / 'k.txt')
        res = mk.export_key_list(db_dir, entries, out_path)
        assert res['count'] == 0


# --------------------------------------------------------------------------
# H. 文本解码（日志文件常是 GBK —— Windows 控制台重定向的默认编码）
# --------------------------------------------------------------------------

class TestTextDecoding:
    def test_utf8_bytes(self):
        assert mk.decode_text_bytes('维修'.encode('utf-8')) == '维修'

    def test_utf8_bom_stripped(self):
        assert mk.decode_text_bytes(b'\xef\xbb\xbf' + 'abc'.encode()) == 'abc'

    def test_gbk_bytes(self):
        """GBK 中文必须能正确解出，而不是报 UnicodeDecodeError。"""
        assert mk.decode_text_bytes('检测到微信进程'.encode('gbk')) == '检测到微信进程'

    def test_undecodable_falls_back_without_raising(self):
        out = mk.decode_text_bytes(b'\xff\xfe\x00garbage')
        assert isinstance(out, str)

    def test_empty_input(self):
        assert mk.decode_text_bytes(b'') == ''
        assert mk.decode_text_bytes(None) == ''

    def test_read_text_file_utf8(self, tmp_path):
        p = tmp_path / 'k.txt'
        p.write_text(KEY_A + '\n', encoding='utf-8')
        assert KEY_A in mk.read_text_file(str(p))

    def test_read_text_file_gbk(self, tmp_path):
        p = tmp_path / 'k_gbk.txt'
        p.write_bytes(('[Cipher] 检测到微信进程\n' + KEY_A + '\n').encode('gbk'))
        text = mk.read_text_file(str(p))
        assert '检测到微信进程' in text
        assert KEY_A in text

    def test_read_missing_file_returns_empty(self, tmp_path):
        assert mk.read_text_file(str(tmp_path / 'nope.txt')) == ''
        assert mk.read_text_file(None) == ''

    def test_gbk_log_file_parses_and_matches(self, tmp_path, db_dir, clean_config):
        """端到端：GBK 编码的日志文件 → 正常识别并匹配。"""
        p = tmp_path / 'keyscan_gbk.log'
        p.write_bytes('\n'.join([
            '[Cipher] 检测到微信进程: [26228, 10836]',
            _found_line(r'message\message_0.db', KEY_A, SALT_A),
            '[Cipher] PID=26228: 1 个密钥验证通过',
        ]).encode('gbk'))
        entries = mk.parse_entries(mk.read_text_file(str(p)))
        assert mk.summarize(entries)['cipher_log'] == 1
        res = mk.match_entries(db_dir, entries)
        assert [r['status'] for r in res] == ['noise', 'matched', 'noise']
