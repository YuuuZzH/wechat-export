# -*- coding: utf-8 -*-
"""CLI 子命令 voice-export 测试：参数解析、日期解析、参数校验、调用管线。"""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as cli  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Args:
    """模拟 argparse 的 Namespace。"""

    def __init__(self, **kw):
        self.chat = ''
        self.sender = []
        self.include_other_chats = False
        self.from_date = None
        self.to_date = None
        self.format = 'mp3'
        self.layout = 'html-folder,files'
        self.merge_by = 'person'
        self.split = 'single'
        self.gap = 1.0
        self.out = None
        self.keep_silk = False
        self.workers = 2
        self.zip_output = True
        self.decrypted_dir = None
        for key, value in kw.items():
            setattr(self, key, value)


def test_parse_date_arg_boundaries():
    assert cli._parse_date_arg(None) is None
    start = cli._parse_date_arg('2021-03-05')
    end = cli._parse_date_arg('2021-03-05', end_of_day=True)
    assert end - start == 86399
    from datetime import datetime
    from engine.constants import TZ
    assert datetime.fromtimestamp(start, tz=TZ).strftime('%Y-%m-%d %H:%M:%S') == '2021-03-05 00:00:00'


def test_cmd_requires_chat_or_sender(tmp_path, capsys):
    code = cli.cmd_voice_export(_Args(decrypted_dir=str(tmp_path)))
    assert code == 2
    assert '至少要给 --chat 或 --sender' in capsys.readouterr().out


def test_cmd_missing_dir_reports(tmp_path, capsys):
    code = cli.cmd_voice_export(_Args(decrypted_dir=str(tmp_path / 'nope'), chat='wxid_x'))
    assert code == 1


def test_cmd_calls_pipeline_with_mapped_options(tmp_path, monkeypatch, capsys):
    calls = {}

    def fake_export(decrypted, out_root, **kwargs):
        calls['decrypted'] = decrypted
        calls['out_root'] = out_root
        calls.update(kwargs)
        return {'count': 3, 'missing': 1, 'duration_total_s': 90.0,
                'out_dir': str(tmp_path / 'out'), 'zip': str(tmp_path / 'out.zip'),
                'merged': [], 'errors': []}

    import engine.services.voice_export as ve
    monkeypatch.setattr(ve, 'export_voices', fake_export)

    code = cli.cmd_voice_export(_Args(decrypted_dir=str(tmp_path), chat='张三',
                                      sender=['爷爷'], from_date='2021-01-01',
                                      to_date='2021-12-31', format='wav',
                                      layout='files,merged', merge_by='chat',
                                      split='per-month', gap=0.5, keep_silk=True,
                                      workers=8, zip_output=False))
    assert code == 0
    assert calls['chats'] == ['张三'] and calls['senders'] == ['爷爷']
    assert calls['fmt'] == 'wav' and calls['layouts'] == ('files', 'merged')
    assert calls['merge_by'] == 'chat' and calls['split'] == 'per-month'
    assert calls['gap_s'] == 0.5 and calls['keep_silk'] is True and calls['workers'] == 8
    assert calls['end_ts'] - calls['start_ts'] == 365 * 86400 - 1
    out = capsys.readouterr().out
    assert '完成：3 条，缺失 1 条' in out and '缺失明细见 missing.csv' in out


def test_cmd_reports_runtime_error_like_missing_ffmpeg(tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError('未检测到 ffmpeg：M4A 需要 ffmpeg')

    import engine.services.voice_export as ve
    monkeypatch.setattr(ve, 'export_voices', boom)
    code = cli.cmd_voice_export(_Args(decrypted_dir=str(tmp_path), chat='x', format='m4a'))
    assert code == 3 and '未检测到 ffmpeg' in capsys.readouterr().out


def test_cli_help_lists_voice_export_flags():
    """真实子进程跑 --help，确认命令与关键参数真的注册上了。"""
    proc = subprocess.run([sys.executable, os.path.join(ROOT, 'src', 'main.py'),
                           'voice-export', '--help'],
                          capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert proc.returncode == 0
    for flag in ('--chat', '--sender', '--format', '--layout', '--merge-by', '--split',
                 '--gap', '--keep-silk', '--workers'):
        assert flag in proc.stdout, flag


def test_cli_actually_dispatches_voice_export(tmp_path):
    """真机路径回归：`main()` 用 if/elif 显式派发子命令（不是 `args.func`），
    **漏接就会"解析成功但什么都不做"**（曾经真的漏接过，冒烟才发现）。
    这里真的起子进程跑一遍，断言它执行到了导出逻辑并打印了完成行。
    """
    dec = tmp_path / 'decrypted'
    (dec / 'message').mkdir(parents=True)
    proc = subprocess.run([sys.executable, os.path.join(ROOT, 'src', 'main.py'),
                           'voice-export', '--chat', 'wxid_demo_1a2b',
                           '--decrypted-dir', str(dec),
                           '--out', str(tmp_path / 'out'), '--no-zip',
                           '--layout', 'files'],
                          capture_output=True, text=True, timeout=180, cwd=ROOT)
    assert proc.returncode == 0, proc.stderr
    assert '完成：0 条' in proc.stdout, proc.stdout
    assert '输出目录' in proc.stdout
