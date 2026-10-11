"""CLI `export` 子命令各模式的分发与参数传递。

背景：`export -m chat / list / employee` 三个模式此前**全部是坏的** ——
`cmd_export` 调用下游函数时漏传必填参数，一跑就 TypeError：

  - chat     : ``export_all_contacts()``      缺 decrypted_dir / out_dir
  - list     : ``list_chats()``               缺 decrypted_dir
  - employee : ``run_employee_export(excel)`` 把 Excel 路径当成了 decrypted_dir

这些模式此前没有任何测试覆盖，所以坏了很久没人发现。本文件锁住修复结果：
每个模式都必须把解析好的参数正确传下去，且错误输入要有可读提示而不是崩栈。
"""
import sys
import types

import pytest

import main as cli


@pytest.fixture
def data_dir(tmp_path):
    d = tmp_path / 'decrypted'
    d.mkdir()
    return str(d)


@pytest.fixture
def stub(monkeypatch, data_dir):
    """把四个导出下游全部替换成记录器，并固定目录解析结果。"""
    calls = {}

    def _recorder(name):
        def _fn(*args, **kwargs):
            calls[name] = {'args': args, 'kwargs': kwargs}
            if name == 'chat':
                return []
            if name == 'contacts':
                return {'path': '', 'count': 0, 'format': '', 'bytes': 0}
            return None
        return _fn

    import chat_export
    import chat_list
    import employee_match
    import contacts_export

    monkeypatch.setattr(cli, '_resolve_decrypted_dir', lambda: data_dir)
    monkeypatch.setattr(cli, '_resolve_db_dir', lambda: data_dir)
    monkeypatch.setattr(chat_export, 'export_all_contacts', _recorder('chat'))
    monkeypatch.setattr(chat_list, 'list_chats', _recorder('list'))
    monkeypatch.setattr(employee_match, 'run_employee_export', _recorder('employee'))
    monkeypatch.setattr(contacts_export, 'export_contacts', _recorder('contacts'))
    return calls


@pytest.fixture
def run(monkeypatch, stub):
    """经真实 argparse 解析 + 真实 dispatch 跑一次 CLI。"""
    def _run(*argv):
        monkeypatch.setattr(sys, 'argv', ['main.py'] + [str(a) for a in argv])
        cli.main()
        return stub
    return _run


# --------------------------------------------------------------------------
# chat 模式（原 TypeError #1）
# --------------------------------------------------------------------------

class TestChatMode:
    def test_no_longer_raises_type_error(self, run, data_dir):
        calls = run('export', '-m', 'chat')
        assert 'chat' in calls
        args = calls['chat']['args']
        assert args[0] == data_dir              # decrypted_dir
        assert args[1]                           # out_dir
        assert calls['chat']['kwargs']['print_fn'] is print

    def test_passes_name_filter(self, run):
        calls = run('export', '-m', 'chat', '--chat', '张三')
        assert calls['chat']['kwargs']['name_filter'] == '张三'

    def test_default_format_is_txt(self, run):
        calls = run('export', '-m', 'chat')
        assert calls['chat']['kwargs']['fmt'] == 'txt'

    def test_honours_html_format(self, run):
        calls = run('export', '-m', 'chat', '--format', 'html')
        assert calls['chat']['kwargs']['fmt'] == 'html'

    def test_honours_xlsx_format(self, run):
        calls = run('export', '-m', 'chat', '--format', 'xlsx')
        assert calls['chat']['kwargs']['fmt'] == 'xlsx'

    def test_passes_keyword(self, run):
        calls = run('export', '-m', 'chat', '--keyword', '会议')
        assert calls['chat']['kwargs']['keyword'] == '会议'

    def test_unknown_format_falls_back_to_txt(self, run, capsys):
        # argparse 只允许白名单；用 monkeypatch 模拟异常值走回退分支
        import argparse
        ns = argparse.Namespace(
            mode='chat', format='jsonl', chat=None, keyword=None,
            output=None, decrypted_dir=None, include_asr=False)
        import chat_export
        calls = {}
        def _fn(*a, **kw):
            calls['kw'] = kw
            return []
        import main as cli
        from unittest.mock import patch
        with patch.object(chat_export, 'export_all_contacts', _fn):
            with patch.object(cli, '_resolve_decrypted_dir', return_value='.'):
                with patch('os.path.isdir', return_value=True):
                    cli.cmd_export(ns)
        assert calls['kw']['fmt'] == 'txt'
        assert '回退' in capsys.readouterr().out

    def test_output_dir_is_used(self, run, tmp_path):
        out = tmp_path / 'myexport'
        calls = run('export', '-m', 'chat', '-o', out)
        assert calls['chat']['args'][1] == str(out)


# --------------------------------------------------------------------------
# list 模式（原 TypeError #2）
# --------------------------------------------------------------------------

class TestListMode:
    def test_no_longer_raises_type_error(self, run, data_dir):
        calls = run('export', '-m', 'list')
        assert calls['list']['args'][0] == data_dir

    def test_passes_name_filter(self, run):
        calls = run('export', '-m', 'list', '--chat', '李')
        assert calls['list']['kwargs']['name_filter'] == '李'


# --------------------------------------------------------------------------
# employee 模式（原 TypeError #3）
# --------------------------------------------------------------------------

class TestEmployeeMode:
    def test_passes_three_positional_args(self, run, data_dir, tmp_path):
        xlsx = tmp_path / 'emp.xlsx'
        xlsx.write_bytes(b'stub')
        calls = run('export', '-m', 'employee', '--excel', xlsx)
        args = calls['employee']['args']
        assert args[0] == data_dir        # decrypted_dir（此前被 excel 抢占）
        assert args[1] == str(xlsx)       # excel_path
        assert args[2]                    # out_dir

    def test_missing_excel_is_reported_not_crashed(self, run, capsys):
        calls = run('export', '-m', 'employee')
        assert 'employee' not in calls
        assert '--excel' in capsys.readouterr().out


# --------------------------------------------------------------------------
# contacts 模式（本次新增）
# --------------------------------------------------------------------------

class TestContactsMode:
    def test_default_is_xlsx_all(self, run):
        calls = run('export', '-m', 'contacts')
        assert calls['contacts']['kwargs']['fmt'] == 'xlsx'
        assert calls['contacts']['kwargs']['kind'] == 'all'

    @pytest.mark.parametrize('fmt', ['xlsx', 'csv', 'html'])
    def test_all_three_formats_accepted(self, run, fmt):
        calls = run('export', '-m', 'contacts', '--format', fmt)
        assert calls['contacts']['kwargs']['fmt'] == fmt

    def test_passes_filters_through(self, run):
        calls = run('export', '-m', 'contacts', '--format', 'csv',
                    '--kind', 'groups', '--has-chat', '--letter', 'L',
                    '--chat', '张', '--sort', 'msg_count')
        kw = calls['contacts']['kwargs']
        assert kw['kind'] == 'groups'
        assert kw['has_chat'] == '1'
        assert kw['letter'] == 'L'
        assert kw['q'] == '张'
        assert kw['sort'] == 'msg_count'

    def test_label_filter_passed_through(self, run):
        calls = run('export', '-m', 'contacts', '--label', 'only_work')
        assert calls['contacts']['kwargs']['label'] == 'only_work'

    def test_label_defaults_to_none(self, run):
        calls = run('export', '-m', 'contacts')
        assert calls['contacts']['kwargs']['label'] is None

    def test_default_output_path_has_extension(self, run):
        calls = run('export', '-m', 'contacts', '--format', 'html')
        out_path = calls['contacts']['args'][1]
        assert out_path.endswith('.html')
        assert 'contacts_' in out_path

    def test_groups_only_default_filename(self, run):
        calls = run('export', '-m', 'contacts', '--kind', 'groups')
        assert 'groups_' in calls['contacts']['args'][1]

    def test_output_dir_gets_default_filename(self, run, tmp_path):
        out_dir = tmp_path / 'outdir'
        out_dir.mkdir()
        calls = run('export', '-m', 'contacts', '--format', 'csv', '-o', out_dir)
        out_path = calls['contacts']['args'][1]
        assert out_path.startswith(str(out_dir))
        assert out_path.endswith('.csv')

    def test_explicit_file_path_is_kept(self, run, tmp_path):
        target = tmp_path / 'mine.csv'
        calls = run('export', '-m', 'contacts', '--format', 'csv', '-o', target)
        assert calls['contacts']['args'][1] == str(target)

    def test_not_yet_existing_dir_gets_default_filename(self, run, tmp_path):
        """-o 指向还不存在的目录时也要补文件名，不能生成无后缀的文件。"""
        target = tmp_path / 'notyet'
        calls = run('export', '-m', 'contacts', '--format', 'csv', '-o', target)
        out_path = calls['contacts']['args'][1]
        assert out_path.startswith(str(target))
        assert out_path.endswith('.csv')

    def test_chatlab_format_is_rejected(self, run, capsys):
        """jsonl/json 是 chatlab 专用，用到通讯录上必须给提示而不是静默出坏文件。"""
        calls = run('export', '-m', 'contacts', '--format', 'jsonl')
        assert 'contacts' not in calls
        out = capsys.readouterr().out
        assert 'jsonl' in out
        assert 'chatlab' in out

    def test_invalid_kind_is_rejected_by_parser(self, run):
        """非法 --kind 由 argparse choices 拦下（打印 usage 并退出码 2）。"""
        with pytest.raises(SystemExit):
            run('export', '-m', 'contacts', '--kind', 'aliens')

    def test_internal_kind_guard(self, data_dir, capsys):
        """choices 之外的值若由程序化调用传入，也要给提示而不是崩栈。"""
        args = types.SimpleNamespace(
            decrypted_dir=data_dir, output=None, format='csv', kind='aliens',
            chat=None, has_chat=False, letter=None, sort='name')
        cli._export_contacts_cmd(args)
        assert 'aliens' in capsys.readouterr().out


# --------------------------------------------------------------------------
# 通用错误处理
# --------------------------------------------------------------------------

class TestCommonErrors:
    @pytest.mark.parametrize('mode', ['chat', 'list', 'contacts'])
    def test_missing_decrypted_dir_reported(self, monkeypatch, run, tmp_path, capsys, mode):
        monkeypatch.setattr(cli, '_resolve_decrypted_dir',
                            lambda: str(tmp_path / 'nope'))
        calls = run('export', '-m', mode)
        assert mode not in calls
        assert '解密目录不存在' in capsys.readouterr().out
