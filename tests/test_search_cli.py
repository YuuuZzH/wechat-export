"""CLI：`search` / `build-search-index` 两个子命令（Task 8）。

覆盖目标（含控制方增补 T8-A1 / T8-A2 / T8-A4）
------------------------------------------------
* **T8-A1 退出码**：失败必须反映在进程退出码上，不能只打印。本文件是 `src/main.py`
  里 `sys.exit` 约定的第一份测试：`cli.main()` 在失败路径上必须抛 `SystemExit`
  且 `code != 0`；`_cmd_search` / `_cmd_build_search_index` 本身返回非 0 码。
  同时钉住"**不吞编程错误**"：`_cmd_search` 只捕获 `SearchIndexError` / `ValueError`，
  其他异常必须原样冒出（否则真 bug 会变成"看起来正常的失败提示"）。
* **T8-A2 `--json` 原样透传**：断言打印出来的 JSON 与 stub 返回的 dict **逐键相等**
  （整个响应体，不是挑几个键），并逐个检查新增诊断键的存在与类型。
* **T8-A4 完整契约 stub**：stub 的返回补齐 `scan_mode` / `elapsed_ms` /
  `slow_query_hint` / `candidate_rows` / `warnings` / `fallback_text_only` **非空值**，
  并分成「有提示」「无提示」两种 stub 断言提示**真的打印 / 真的不打印**。
* **T8-A4 `own_wxid` 传递**：monkeypatch `engine.config_file.get_backup_wxid`，
  钉住 CLI→引擎的参数传递（#16/#17 两个 Critical 都在这个参数上）。

数据安全：不使用任何真实微信数据、`backup/` 或密钥；目录是 `tmp_path` 下现造的
空合成目录，名字全部是 `fixture` 前缀的合成值。
"""
import argparse
import copy
import io
import json
import os
import pathlib
import subprocess
import sys

import pytest

import main as cli
from engine.services import search as engine_search

# --------------------------------------------------------------------------
# 合成常量（全部为虚构值）
# --------------------------------------------------------------------------
# 形如真实账号**目录名**（wxid_ + 后缀），用来钉住"原样透传、不自己剥后缀"
OWN_WXID = 'wxid_fixture_0000_abc123'

SLOW_HINT = 'SLOWHINT_该正则较复杂已全量扫描'
WARN_LINE = 'WARNLINE_文本索引不可用已降级'
FALLBACK_ONLY_TEXT = '只覆盖有正文的消息'

INDEX_READY = {
    'exists': True, 'ready': True, 'stale': False, 'schema_ok': True,
    'built_at': 1, 'fts_rows': 4, 'meta_rows': 5, 'source_rows': 5,
}

BUILD_RESULT = {
    'meta_rows': 5, 'fts_rows': 4, 'built_at': 123, 'source_rows': 5,
    'msg_text_rows': 4,
}


def _parsed(query='维修', **over):
    """`search_query.parse_query` 的形状（键名与 `_EMPTY` 一致）。"""
    parsed = {'keywords': [query] if query else [], 'phrases': [], 'or_groups': [],
              'exclude': [], 'regexes': [], 'chats': [], 'senders': [], 'types': [],
              'date_from': None, 'date_to': None, 'labels': [], 'errors': []}
    parsed.update(over)
    return parsed


def _hit(**over):
    hit = {
        'chat_id': 'wxid_fixture_chat',
        'chat_display_name': '会话甲',
        'is_group': False,
        'local_id': 7,
        'create_time': 1735689600,      # 2025-01-01T00:00:00Z → CST 2025-01-01 08:00
        'local_type': 1,
        'type_label': '文本',
        'sender_username': 'wxid_fixture_chat',
        'sender_display_name': '发送者乙',
        'snippet': '摘要片段 维修 内容',
        'match_spans': [[5, 7]],
    }
    hit.update(over)
    return hit


def _search_response(query='维修', hits=None, **over):
    """`search_messages` 的**完整**返回契约（T8-A4：诊断键一律非空）。

    非空值是刻意的：`slow_query_hint` / `warnings` / `fallback_text_only` /
    `scan_mode` 若为 None/空，CLI 里读这些键的显示分支就**永远不会执行**，
    写错了测试也不会失败。
    """
    hits = list(hits or [])
    res = {
        'results': hits,
        'total': len(hits),
        'page': 1,
        'per_page': 50,
        'total_pages': 1,
        'parsed': _parsed(query),
        'index': dict(INDEX_READY),
        'used_fallback': True,
        'regex_degraded': True,
        'scan_mode': 'fallback',
        'elapsed_ms': 1234.5,
        'slow_query_hint': SLOW_HINT,
        'candidate_rows': 999,
        'sender_filter_unsupported': True,
        'fallback_text_only': True,
        'warnings': [WARN_LINE],
    }
    res.update(over)
    return res


def _quiet_response(**over):
    """「无提示」stub：所有诊断键都是空/False，且没有命中。"""
    quiet = {'used_fallback': False, 'regex_degraded': False,
             'sender_filter_unsupported': False, 'scan_mode': 'fts',
             'slow_query_hint': None, 'candidate_rows': 12,
             'fallback_text_only': False, 'warnings': []}
    quiet.update(over)
    return _search_response(**quiet)


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

@pytest.fixture
def ctl(monkeypatch, tmp_path):
    """测试对 stub 的**控制面**（返回什么 / 抛什么 / 目录 / wxid）。"""
    dec = tmp_path / 'dec'
    (dec / 'message').mkdir(parents=True)
    other = tmp_path / 'dec2'
    other.mkdir()
    return {
        'dir': str(dec),
        'other_dir': str(other),
        'missing_dir': str(tmp_path / 'no-such-decrypted-dir'),
        'wxid': OWN_WXID,
        'response': None,          # None → 默认的完整契约（含非空诊断键）
        'search_raise': None,
        'build_raise': None,
        'refresh_raise': None,
        'build_result': dict(BUILD_RESULT),
        'refresh_result': dict(BUILD_RESULT),
        'status': dict(INDEX_READY),
        'progress_message': None,   # 非 None → 假 build 用它回调一次 progress
    }


@pytest.fixture
def stub(monkeypatch, ctl):
    """把搜索/索引下游与配置读取全部换成记录器。"""
    calls = {}

    import engine.config_file as cfg_mod
    import engine.services.search as search_mod
    import engine.services.search_index as index_mod

    monkeypatch.setattr(cli, '_resolve_decrypted_dir', lambda: ctl['dir'])
    # 保留真实实现：有些测试要验证"真的打印出来"的路径（缺目录提示）
    calls['_real_print_missing_dir'] = cli._print_missing_dir
    monkeypatch.setattr(cli, '_print_missing_dir',
                        lambda p: calls.setdefault('missing', p))
    monkeypatch.setattr(cfg_mod, 'get_backup_wxid', lambda: ctl['wxid'])

    def _fake_search(decrypted_dir, q, **kw):
        calls['search'] = {'dir': decrypted_dir, 'q': q, **kw}
        if ctl['search_raise'] is not None:
            raise ctl['search_raise']
        res = ctl['response']
        if res is None:
            res = _search_response(query=q)
        calls['response'] = copy.deepcopy(res)
        return copy.deepcopy(res)

    def _fake_build(decrypted_dir, **kw):
        calls['build'] = {'dir': decrypted_dir, **kw}
        if ctl['build_raise'] is not None:
            raise ctl['build_raise']
        if kw.get('progress'):
            calls['progress'] = kw['progress']
            if ctl['progress_message']:
                # 模拟引擎在构建过程中回调进度（message 由引擎提供，可能含任意字符）
                kw['progress']('meta', ctl['progress_message'], 0.5)
        return dict(ctl['build_result'])

    def _fake_refresh(decrypted_dir, **kw):
        calls['refresh'] = {'dir': decrypted_dir, **kw}
        if ctl['refresh_raise'] is not None:
            raise ctl['refresh_raise']
        if kw.get('progress'):
            calls['progress'] = kw['progress']
        return dict(ctl['refresh_result'])

    def _fake_status(decrypted_dir):
        calls['status'] = decrypted_dir
        return dict(ctl['status'])

    monkeypatch.setattr(search_mod, 'search_messages', _fake_search)
    monkeypatch.setattr(index_mod, 'build_index', _fake_build)
    monkeypatch.setattr(index_mod, 'refresh_index', _fake_refresh)
    monkeypatch.setattr(index_mod, 'index_status', _fake_status)
    return calls


@pytest.fixture
def run(monkeypatch, stub):
    """经真实 argparse 解析 + 真实 dispatch 跑一次 CLI。"""
    def _run(*argv):
        monkeypatch.setattr(sys, 'argv', ['main.py'] + [str(a) for a in argv])
        cli.main()
        return stub
    return _run


def _search_ns(**over):
    """直接调用 `_cmd_search` 用的 Namespace（绕过 argparse）。"""
    ns = argparse.Namespace(command='search', query='维修', json=False, page=1,
                            per_page=50, sort='time_desc', decrypted_dir=None)
    for key, value in over.items():
        setattr(ns, key, value)
    return ns


def _build_ns(**over):
    ns = argparse.Namespace(command='build-search-index', refresh=False,
                            no_text=False, no_meta=False, decrypted_dir=None)
    for key, value in over.items():
        setattr(ns, key, value)
    return ns


# --------------------------------------------------------------------------
# search：参数转发
# --------------------------------------------------------------------------

class TestSearchCommand:
    def test_query_passed_through(self, run):
        calls = run('search', '维修 类型:图片')
        assert calls['search']['q'] == '维修 类型:图片'

    def test_pagination_defaults(self, run):
        calls = run('search', '维修')
        assert calls['search']['page'] == 1
        assert calls['search']['per_page'] == 50
        assert calls['search']['sort'] == 'time_desc'

    def test_explicit_pagination_and_sort(self, run):
        calls = run('search', '维修', '--page', 3, '--per-page', 10,
                    '--sort', 'time_asc')
        assert calls['search']['page'] == 3
        assert calls['search']['per_page'] == 10
        assert calls['search']['sort'] == 'time_asc'

    def test_decrypted_dir_flag_overrides_auto_detect(self, run, ctl):
        calls = run('search', '维修', '--decrypted-dir', ctl['other_dir'])
        assert calls['search']['dir'] == ctl['other_dir']

    def test_auto_detected_dir_used_by_default(self, run, ctl):
        calls = run('search', '维修')
        assert calls['search']['dir'] == ctl['dir']

    def test_requires_query(self, run):
        """argparse 缺位置参数 → SystemExit(2)（用法错误）。"""
        with pytest.raises(SystemExit) as exc:
            run('search')
        assert exc.value.code == 2

    def test_missing_dir_exits_nonzero_and_reports(self, run, stub, ctl):
        """T8-A1：目录不存在也要非零退出（不能只打印）。"""
        ctl['dir'] = ctl['missing_dir']
        with pytest.raises(SystemExit) as exc:
            run('search', '维修')
        assert exc.value.code != 0
        assert stub['missing'] == ctl['missing_dir']


# --------------------------------------------------------------------------
# search --json：原样透传整个响应体（T8-A2）
# --------------------------------------------------------------------------

class TestSearchJsonOutput:
    def test_prints_valid_json(self, run, capsys):
        run('search', '维修', '--json')
        out = capsys.readouterr().out
        assert out.strip().startswith('{')
        assert json.loads(out)['total'] == 0

    def test_passes_whole_body_through_verbatim(self, run, stub, capsys):
        """整个响应体逐键相等 —— 不是"挑几个键"。

        少传任何一个键（或改值/改结构）这条测试就会失败。
        """
        run('search', '维修 类型:图片', '--json')
        body = json.loads(capsys.readouterr().out)
        assert body == stub['response']

    def test_carries_task5_and_task6_diagnostic_keys(self, run, capsys):
        """T8-A4：新增诊断键必须存在、类型正确、值非空。"""
        run('search', '维修', '--json')
        body = json.loads(capsys.readouterr().out)
        assert body['scan_mode'] == 'fallback'
        assert body['elapsed_ms'] == 1234.5
        assert body['slow_query_hint'] == SLOW_HINT
        assert body['candidate_rows'] == 999
        assert body['warnings'] == [WARN_LINE]
        assert body['fallback_text_only'] is True
        assert body['sender_filter_unsupported'] is True
        assert body['used_fallback'] is True
        assert body['regex_degraded'] is True
        assert isinstance(body['index'], dict) and body['index']['ready'] is True
        assert body['parsed']['keywords'] == ['维修']
        assert body['parsed']['errors'] == []

    def test_hits_are_carried_through(self, run, ctl, capsys):
        ctl['response'] = _search_response(query='维修', hits=[_hit()])
        run('search', '维修', '--json')
        body = json.loads(capsys.readouterr().out)
        assert body['total'] == 1
        assert body['results'][0]['snippet'] == '摘要片段 维修 内容'
        assert body['results'][0]['match_spans'] == [[5, 7]]

    def test_syntax_error_json_is_machine_readable_but_exit_nonzero(
            self, run, ctl, capsys):
        """T8-A1：查询有语法错误 → 先输出可读 JSON，再以非零码退出。"""
        ctl['response'] = _search_response(
            query='维修',
            parsed=_parsed('维修', errors=[{'token': '/[a-',
                                            'message': '正则语法错误'}]))
        with pytest.raises(SystemExit) as exc:
            run('search', '维修 /[a-', '--json')
        assert exc.value.code != 0
        body = json.loads(capsys.readouterr().out)
        assert body['parsed']['errors'][0]['token'] == '/[a-'

    def test_json_parse_error_keeps_whole_body(self, run, ctl, capsys):
        """有语法错误时也不能"只打印错误、丢掉响应体"。"""
        ctl['response'] = _search_response(
            query='维修',
            parsed=_parsed('维修', errors=[{'token': '/[a-', 'message': 'X'}]))
        with pytest.raises(SystemExit):
            run('search', '维修', '--json')
        body = json.loads(capsys.readouterr().out)
        assert body['scan_mode'] == 'fallback'
        assert body['index']['fts_rows'] == 4


# --------------------------------------------------------------------------
# search：人类可读输出与提示（T8-A4 问题 1）
# --------------------------------------------------------------------------

class TestSearchHumanOutput:
    def test_mentions_total_and_page(self, run, capsys):
        run('search', '维修')
        out = capsys.readouterr().out
        assert '命中: 0 条（第 1/1 页）' in out

    def test_no_results_message(self, run, capsys):
        run('search', '维修')
        assert '（无结果）' in capsys.readouterr().out

    def test_hints_are_really_printed(self, run, capsys):
        """「有提示」stub：四个提示分支都必须真的执行到。"""
        run('search', '维修')
        out = capsys.readouterr().out
        # used_fallback
        assert 'build-search-index 提速' in out
        # regex_degraded
        assert '正则无法预筛' in out
        # sender_filter_unsupported
        assert '降级路径不支持发送者筛选' in out
        # slow_query_hint（stub 提供的原文）
        assert SLOW_HINT in out
        # fallback_text_only（CLI 自己的文案）
        assert FALLBACK_ONLY_TEXT in out
        # warnings（stub 提供的原文）
        assert WARN_LINE in out
        # scan_mode / candidate_rows / elapsed_ms
        assert 'fallback' in out
        assert '999' in out
        assert '1234' in out

    def test_no_hints_prints_no_hint_lines(self, run, ctl, capsys):
        """「无提示」stub：提示行必须一条都不出现。"""
        ctl['response'] = _quiet_response()
        run('search', '维修')
        out = capsys.readouterr().out
        assert '提示' not in out
        assert '警告' not in out
        assert SLOW_HINT not in out
        assert WARN_LINE not in out
        assert FALLBACK_ONLY_TEXT not in out
        assert '命中: 0 条（第 1/1 页）' in out

    def test_regex_degraded_only_prints_slow_hint(self, run, ctl, capsys):
        """退化但没走降级路径：只有退化提示，不该误报"索引未构建"。"""
        ctl['response'] = _quiet_response(regex_degraded=True,
                                          slow_query_hint=SLOW_HINT)
        run('search', '维修')
        out = capsys.readouterr().out
        assert '正则无法预筛' in out
        assert SLOW_HINT in out
        assert 'build-search-index 提速' not in out
        assert FALLBACK_ONLY_TEXT not in out

    def test_fallback_text_only_prints_its_own_line(self, run, ctl, capsys):
        ctl['response'] = _quiet_response(fallback_text_only=True,
                                          scan_mode='fallback',
                                          used_fallback=True)
        run('search', '类型:图片')
        out = capsys.readouterr().out
        assert FALLBACK_ONLY_TEXT in out
        assert WARN_LINE not in out

    def test_index_not_ready_shows_degraded_header(self, run, ctl, capsys):
        ctl['response'] = _quiet_response(
            used_fallback=True,
            index={'exists': False, 'ready': False, 'stale': False,
                   'schema_ok': False, 'built_at': None, 'fts_rows': 0,
                   'meta_rows': 0, 'source_rows': 0})
        run('search', '维修')
        out = capsys.readouterr().out
        assert '降级直扫' in out

    def test_index_ready_shows_counts(self, run, capsys):
        run('search', '维修')
        out = capsys.readouterr().out
        assert '已就绪' in out
        assert '4' in out          # fts_rows
        assert '5' in out          # meta_rows

    def test_hit_line_is_rendered(self, run, ctl, capsys):
        """命中行渲染（时间/会话/发送者/类型/摘要）此前没有任何覆盖。"""
        ctl['response'] = _search_response(query='维修', hits=[_hit()])
        run('search', '维修')
        out = capsys.readouterr().out
        assert '命中: 1 条（第 1/1 页）' in out
        assert '会话甲' in out
        assert '发送者乙' in out
        assert '文本' in out
        assert '摘要片段 维修 内容' in out
        assert '2025-01-01 08:00' in out

    def test_hit_without_sender_display_name_falls_back_to_chat_name(
            self, run, ctl, capsys):
        ctl['response'] = _search_response(
            query='维修',
            hits=[_hit(sender_display_name=None, snippet=None)])
        run('search', '维修')
        out = capsys.readouterr().out
        assert '会话甲' in out
        assert '命中: 1 条（第 1/1 页）' in out

    def test_syntax_error_is_printed_in_human_mode(self, run, ctl, capsys):
        ctl['response'] = _search_response(
            query='维修',
            parsed=_parsed('维修', errors=[{'token': '/[a-',
                                            'message': '正则语法错误'}]))
        with pytest.raises(SystemExit) as exc:
            run('search', '维修 /[a-')
        assert exc.value.code != 0
        out = capsys.readouterr().out
        assert '/[a-' in out
        assert '正则语法错误' in out


# --------------------------------------------------------------------------
# search：失败必须非零退出（T8-A1）
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# T16：显式截断的 CLI 呈现（结构化字段 + 人话，缺一即"静默截断"）
# --------------------------------------------------------------------------

TRUNCATED_TOTAL = 1018563
TRUNCATED_RETAINED = 50000
# 单一事实源（R44 §8.7）：**不要手抄**引擎的截断文案 —— 手抄的第二份会在改文案时静默漂移。
# 这里用引擎常量的**前缀**构造一条同文 warning（stub 的 warnings 由我们喂给 CLI，故格式自定）。
TRUNC_WARNING = engine_search._TRUNCATED_WARNING % (1234, 500)


class TestTruncationOutput:
    """`--` 是必须的：纯排除词查询以 `-` 开头，argparse 会把它当成选项。"""

    def test_truncated_hint_is_printed(self, run, ctl, capsys):
        """截断必须**在人类可读输出里说出来**（不能只在 JSON 里）。"""
        ctl['response'] = _search_response(query='-退货', hits=[_hit()],
                                           total=TRUNCATED_TOTAL,
                                           truncated=True,
                                           retained_rows=TRUNCATED_RETAINED,
                                           warnings=[TRUNC_WARNING])
        run('search', '--', '-退货')
        out = capsys.readouterr().out
        # CLI **自己的**提示行（结构化字段驱动）——前缀是"提示:"，与 warning 那行走不同分支
        assert '  提示: 结果集过大（共 %d 条）' % TRUNCATED_TOTAL in out
        assert '只保留了前 %d 条' % TRUNCATED_RETAINED in out
        # `total` 仍是精确值：截断不许污染命中数
        assert '命中: %d 条' % TRUNCATED_TOTAL in out

    def test_the_hint_line_is_driven_by_the_field_not_by_the_warning_text(
            self, run, ctl, capsys):
        """**判别性**：warning 里端着同一句话，但只要 `truncated` 为假就**不许**打提示行。

        否则"CLI 真的读了结构化字段"这件事根本没被测到 —— 靠 warnings 兜底照样绿。
        """
        ctl['response'] = _quiet_response(truncated=False, retained_rows=0,
                                          warnings=[TRUNC_WARNING])
        run('search', '维修')
        out = capsys.readouterr().out
        assert TRUNC_WARNING in out              # 引擎的 warning 原样透传
        assert '  提示: 结果集过大' not in out    # 但 CLI 自己的判断必须来自结构化字段

    def test_no_truncated_line_when_not_truncated(self, run, ctl, capsys):
        """**防误报**：未截断的查询里不得出现任何截断文案。"""
        ctl['response'] = _quiet_response()
        run('search', '维修')
        out = capsys.readouterr().out
        assert '结果集过大' not in out
        assert '只保留了前' not in out
        assert '超出保留范围' not in out

    def test_empty_page_beyond_the_window_is_not_reported_as_no_results(
            self, run, ctl, capsys):
        """空页**不得**呈现成"没有匹配的消息"（T16 契约 #5 的 CLI 侧）。"""
        ctl['response'] = _search_response(query='-退货', hits=[],
                                           total=TRUNCATED_TOTAL,
                                           retained_rows=TRUNCATED_RETAINED,
                                           page=500, total_pages=20372,
                                           truncated=True,
                                           warnings=[TRUNC_WARNING])
        run('search', '--', '-退货')
        out = capsys.readouterr().out
        assert '超出保留范围' in out
        assert '（无结果）' not in out

    def test_guard_empty_page_without_truncation_still_says_no_results(
            self, run, ctl, capsys):
        """判别性守卫：没有截断时**照旧**打「（无结果）」（否则上一句是常量串）。"""
        ctl['response'] = _quiet_response()
        run('search', '维修')
        out = capsys.readouterr().out
        assert '（无结果）' in out
        assert '超出保留范围' not in out

    def test_json_carries_the_truncation_fields(self, run, ctl, capsys):
        """`--json` 原样透传：两个结构化字段必须在机器可读输出里。"""
        ctl['response'] = _search_response(query='-退货', hits=[_hit()],
                                           total=TRUNCATED_TOTAL,
                                           truncated=True,
                                           retained_rows=TRUNCATED_RETAINED,
                                           warnings=[TRUNC_WARNING])
        run('search', '--json', '--', '-退货')
        body = json.loads(capsys.readouterr().out)
        assert body['truncated'] is True
        assert body['retained_rows'] == TRUNCATED_RETAINED
        assert body['total'] == TRUNCATED_TOTAL


class TestCliOutputHasNoMarkdownAsterisks:
    """T19（R41 裁决 #5）：CLI 把引擎 warning 与自己的 `提示:` 行**直接 print**。

    `**…**` 在那个出口上是**字面星号**（`escapeHtml` 也救不了 CLI），用户看到的是
    `**只能看到有正文的消息**`。这里断言 stdout 里没有 `**`。

    `warnings` 用的是**引擎的那两条真文案**（不是手抄一份），所以引擎侧的星号
    也在这里被端到端钉住 —— 手抄的话，引擎改了这里照样绿。
    """

    FALLBACK_ONLY_MARK = '只能看到有正文的消息'
    TRUNC_MARK = '只保留了前 %d 条' % TRUNCATED_RETAINED

    def _truncated_stub(self, **over):
        warnings = [engine_search._FALLBACK_TEXT_ONLY_WARNING,
                    engine_search._TRUNCATED_WARNING % (TRUNCATED_TOTAL,
                                                        TRUNCATED_RETAINED)]
        args = dict(query='-退货', hits=[_hit()], total=TRUNCATED_TOTAL,
                    truncated=True, retained_rows=TRUNCATED_RETAINED,
                    fallback_text_only=True, used_fallback=True,
                    scan_mode='fallback', warnings=warnings)
        args.update(over)
        return _search_response(**args)

    def test_no_asterisks_with_results(self, run, ctl, capsys):
        ctl['response'] = self._truncated_stub()
        run('search', '--', '-退货')
        out = capsys.readouterr().out
        # 防空转：这几句**用户可见的**文案必须先真的出现，否则"没有星号"是恒真
        assert self.FALLBACK_ONLY_MARK in out
        assert self.TRUNC_MARK in out
        assert '降级扫描只覆盖有正文的消息' in out      # CLI 自己的提示行
        assert '**' not in out, out

    def test_no_asterisks_on_the_empty_page_beyond_the_window(self, run, ctl, capsys):
        """空页分支（`超出保留范围`）同样在 stdout 上被钉住。"""
        ctl['response'] = self._truncated_stub(hits=[], total_pages=20372, page=1001)
        run('search', '--', '-退货')
        out = capsys.readouterr().out
        assert '超出保留范围' in out                  # 防空转：走的是这个分支
        assert self.TRUNC_MARK in out
        assert '**' not in out, out


class TestSearchFailureExitCodes:
    def test_index_error_exits_nonzero(self, run, ctl, capsys):
        from engine.services.search import SearchIndexError
        ctl['search_raise'] = SearchIndexError(
            '索引查询失败，可能需要重建索引', code='index_query_failed',
            detail='OperationalError: no such table: msg_meta',
            hint='重建索引：POST /api/search/index')
        with pytest.raises(SystemExit) as exc:
            run('search', '维修')
        assert exc.value.code != 0
        out = capsys.readouterr().out
        assert '索引查询失败' in out
        assert 'no such table: msg_meta' in out

    def test_value_error_exits_nonzero(self, run, ctl, capsys):
        ctl['search_raise'] = ValueError('查询为空：请至少给出关键词、正则或一个筛选条件')
        with pytest.raises(SystemExit) as exc:
            run('search', '维修')
        assert exc.value.code != 0
        assert '查询为空' in capsys.readouterr().out

    def test_text_index_unavailable_subclass_also_exits_nonzero(self, run, ctl,
                                                                capsys):
        """`TextIndexUnavailableError` 是 `SearchIndexError` 的子类。

        引擎已不再抛它，但 CLI 不能因为它"是个更具体的类"就漏接 —— 必须同样
        非零退出**且打印错误本身**（否则这里的 SystemExit 可能只是 argparse 的
        用法错误 2，属于假通过）。
        """
        from engine.services.search import TextIndexUnavailableError
        ctl['search_raise'] = TextIndexUnavailableError(
            '文本索引不可用', detail='message_fts 为空')
        with pytest.raises(SystemExit) as exc:
            run('search', '维修')
        assert exc.value.code != 0
        out = capsys.readouterr().out
        assert '文本索引不可用' in out
        assert 'message_fts 为空' in out

    def test_programming_error_is_not_swallowed(self, run, ctl):
        """只捕获已声明的异常类型：其他异常必须原样冒出（T8-A1 明确要求）。"""
        ctl['search_raise'] = KeyError('boom')
        with pytest.raises(KeyError):
            run('search', '维修')

    def test_empty_query_exits_nonzero(self, run, capsys):
        with pytest.raises(SystemExit) as exc:
            run('search', '   ')
        assert exc.value.code != 0
        assert '查询为空' in capsys.readouterr().out

    def test_only_syntax_error_query_exits_nonzero(self, run, capsys):
        """只有语法错误、没有任何有效条件 → 查询没被执行 → 非零退出。"""
        with pytest.raises(SystemExit) as exc:
            run('search', 'OR')
        assert exc.value.code != 0
        out = capsys.readouterr().out
        assert '查询为空或没有有效条件' in out
        assert 'OR' in out          # 逐条打印解析错误（含出错 token）

    def test_success_exit_code_is_zero(self, run):
        """成功路径不抛 SystemExit（退出码 0）。"""
        run('search', '维修')      # 不抛异常即为 0

    def test_handler_returns_nonzero_code_directly(self, stub, ctl):
        """约定本身：处理函数**返回**退出码，而不是只打印。"""
        from engine.services.search import SearchIndexError
        ctl['search_raise'] = SearchIndexError('坏了', detail='X')
        assert cli._cmd_search(_search_ns()) != 0

    def test_handler_returns_zero_code_directly(self, stub):
        assert cli._cmd_search(_search_ns()) == 0

    def test_handler_returns_nonzero_for_missing_dir(self, stub, ctl):
        assert cli._cmd_search(_search_ns(decrypted_dir=ctl['missing_dir'])) != 0


# --------------------------------------------------------------------------
# search：own_wxid 传递（T8-A4 问题 2）
# --------------------------------------------------------------------------

class TestOwnWxidPlumbing:
    """钉住 CLI → 引擎的 `own_wxid` 参数传递。

    #16：`own_wxid` 被无条件当成筛选条件 → 所有查询归零；
    #17：它的值形式与索引里存的不一致 → `发送者:我` 永远匹配不上。
    两条都在引擎侧修好了，但 CLI 如果传错（或干脆不传），退化会**静默**复现。
    """

    def test_own_wxid_from_config_is_passed(self, run, stub, ctl):
        run('search', '维修')
        assert stub['search']['own_wxid'] == OWN_WXID
        assert stub['search']['own_wxid'] is not None

    def test_own_wxid_passed_verbatim_not_stripped(self, run, stub, ctl):
        """值必须**原样**透传：不做剥后缀/规范化（引擎自己会归一化）。"""
        ctl['wxid'] = OWN_WXID
        run('search', '维修')
        assert stub['search']['own_wxid'] == OWN_WXID
        assert stub['search']['own_wxid'].endswith('_abc123')

    def test_own_wxid_is_none_when_unconfigured(self, run, stub, ctl):
        """配置里没有 wxid → 传 None，并且**不报错**、退出码 0。"""
        ctl['wxid'] = None
        run('search', '维修')
        assert stub['search']['own_wxid'] is None

    def test_none_own_wxid_does_not_change_other_params(self, run, stub, ctl):
        ctl['wxid'] = None
        run('search', '发送者:我', '--page', 2, '--sort', 'chat')
        assert stub['search']['own_wxid'] is None
        assert stub['search']['q'] == '发送者:我'
        assert stub['search']['page'] == 2
        assert stub['search']['sort'] == 'chat'

    def test_own_wxid_is_read_per_invocation(self, run, stub, ctl):
        """每次调用都重新读配置（不能把第一次的值缓存住）。"""
        run('search', '维修')
        assert stub['search']['own_wxid'] == OWN_WXID
        ctl['wxid'] = None
        run('search', '维修')
        assert stub['search']['own_wxid'] is None

    def test_own_wxid_is_not_used_as_a_filter_by_cli(self, run, stub, ctl):
        """CLI 只传 `own_wxid=`；绝不能自己把它拼进查询串或另加筛选参数。"""
        run('search', '维修')
        assert set(stub['search']) == {'dir', 'q', 'page', 'per_page', 'sort',
                                       'own_wxid'}
        assert stub['search']['q'] == '维修'


# --------------------------------------------------------------------------
# build-search-index
# --------------------------------------------------------------------------

class TestBuildIndexCommand:
    def test_full_build_defaults(self, run):
        calls = run('build-search-index')
        assert calls['build']['text'] is True
        assert calls['build']['meta'] is True
        assert calls['build']['force'] is True

    def test_uses_auto_detected_dir(self, run, ctl):
        calls = run('build-search-index')
        assert calls['build']['dir'] == ctl['dir']

    def test_decrypted_dir_flag(self, run, ctl):
        calls = run('build-search-index', '--decrypted-dir', ctl['other_dir'])
        assert calls['build']['dir'] == ctl['other_dir']

    def test_refresh_action(self, run):
        calls = run('build-search-index', '--refresh')
        assert 'refresh' in calls
        assert 'build' not in calls

    def test_refresh_reports_skipped(self, run, ctl, capsys):
        ctl['refresh_result'] = {'skipped': True}
        run('build-search-index', '--refresh')
        assert '无需刷新' in capsys.readouterr().out

    def test_no_text_flag(self, run):
        calls = run('build-search-index', '--no-text')
        assert calls['build']['text'] is False
        assert calls['build']['meta'] is True

    def test_no_meta_flag(self, run):
        calls = run('build-search-index', '--no-meta')
        assert calls['build']['meta'] is False
        assert calls['build']['text'] is True

    def test_progress_callback_is_wired(self, run, stub, capsys):
        """`progress` 必须真的传下去，且回调会打印进度。"""
        run('build-search-index')
        assert callable(stub['progress'])
        stub['progress']('meta', '发现源数据...', 0.5)
        assert '50%' in capsys.readouterr().out

    def test_reports_counts_and_status(self, run, capsys):
        run('build-search-index')
        out = capsys.readouterr().out
        assert '元数据 5 行' in out
        assert '全文 4 行' in out
        assert 'chat_search_index.db' in out
        assert '状态: 就绪' in out

    def test_reports_not_ready_status(self, run, ctl, capsys):
        ctl['status'] = {'exists': True, 'ready': False, 'stale': True,
                         'schema_ok': False, 'built_at': None, 'fts_rows': 0,
                         'meta_rows': 0, 'source_rows': 0}
        run('build-search-index')
        assert '状态: 未就绪' in capsys.readouterr().out

    def test_success_exit_code_is_zero(self, run):
        run('build-search-index')       # 不抛异常即为 0

    def test_missing_dir_exits_nonzero(self, run, stub, ctl):
        ctl['dir'] = ctl['missing_dir']
        with pytest.raises(SystemExit) as exc:
            run('build-search-index')
        assert exc.value.code != 0
        assert stub['missing'] == ctl['missing_dir']

    def test_build_failure_exits_nonzero(self, run, ctl, capsys):
        ctl['build_raise'] = RuntimeError('disk I/O error')
        with pytest.raises(SystemExit) as exc:
            run('build-search-index')
        assert exc.value.code != 0
        assert 'disk I/O error' in capsys.readouterr().out

    def test_refresh_failure_exits_nonzero(self, run, ctl, capsys):
        ctl['refresh_raise'] = RuntimeError('database is locked')
        with pytest.raises(SystemExit) as exc:
            run('build-search-index', '--refresh')
        assert exc.value.code != 0
        assert 'database is locked' in capsys.readouterr().out

    def test_unexpected_error_type_still_exits_nonzero(self, run, ctl, capsys):
        """构建失败对异常类型不做要求（T8-A1）—— 但退出码必须非零。

        与 `_cmd_search` 不同：构建是长事务，崩在中途只会留下一个半成品索引，
        这里按键名兜底并给出可操作提示，比抛 traceback 对用户更有利。
        """
        ctl['build_raise'] = KeyError('unexpected_shape')
        with pytest.raises(SystemExit) as exc:
            run('build-search-index')
        assert exc.value.code != 0
        assert 'unexpected_shape' in capsys.readouterr().out

    def test_handler_returns_nonzero_code_directly(self, stub, ctl):
        ctl['build_raise'] = RuntimeError('boom')
        assert cli._cmd_build_search_index(_build_ns()) != 0

    def test_handler_returns_zero_code_directly(self, stub):
        assert cli._cmd_build_search_index(_build_ns()) == 0

    def test_both_no_text_and_no_meta_is_rejected(self, run, stub, capsys):
        """`--no-text --no-meta` 会建出一个空索引（ready 判定仍可能为真）。

        这是"静默退化"的同族缺陷：宁可拒绝并给出可读提示 + 非零退出码。
        """
        with pytest.raises(SystemExit) as exc:
            run('build-search-index', '--no-text', '--no-meta')
        assert exc.value.code != 0
        assert 'build' not in stub
        assert '至少' in capsys.readouterr().out


# --------------------------------------------------------------------------
# GBK 控制台：真实消息内容里的"不可编码字符"不能让命令崩溃（Critical）
# --------------------------------------------------------------------------
# 本机控制台是 GBK（cp936），而**真实消息正文与显示名里什么都可能有**：
# U+2005 四点空格、U+2776 ❶、emoji、罕见汉字……在 101 万条真实消息上，直接
# `print(snippet)` 几乎必然撞上 `UnicodeEncodeError: 'gbk' codec can't encode
# character '\u2005'` —— 搜索本身成功了，用户却拿不到任何结果。
#
# 与进度条（`_make_progress_display` 按 `sys.stdout.encoding` 挑安全字符）的区别：
# 进度条是我们自己画的固定字符，可以预筛；**结果内容不能预筛**，只能"编码安全输出"。
#
# 这里用的三个字符都是**构造值**，不代表任何真实消息内容。
U2005 = '\u2005'            # 四点空格（真实数据里常见）
U2776 = '\u2776'            # ❶
EMOJI = '\U0001f600'        # 😀
HOSTILE = U2005 + U2776 + EMOJI


class GbkConsole:
    """GBK + `errors='strict'` 的输出流 —— 等价于本机控制台的真实行为。"""

    def __init__(self):
        self.raw = io.BytesIO()
        self.stream = io.TextIOWrapper(self.raw, encoding='gbk', errors='strict',
                                       newline='')

    def text(self):
        self.stream.flush()
        return self.raw.getvalue().decode('gbk', 'replace')


def _attach_gbk_console(monkeypatch):
    """把 stdout/stderr 换成 GBK 流，返回可读取内容的句柄。

    ⚠️ **必须在测试体内调用，不能做成夹具**（实测陷阱，pytest 9.0.3 + fd 捕获）：
    pytest 的全局捕获会在 setup→call 阶段之间重新执行 `sys.stdout = EncodedFile(...)`，
    于是**在夹具里打的补丁会被悄悄覆盖** —— 输出全部落进 pytest 的 UTF-8 捕获流
    （不抛异常）、控制台读到空字符串，测试表现为"断言失败但看不到 UnicodeEncodeError"，
    完全掩盖了真实缺陷。诊断证据（D:\\dsh_tmp\\probe_cli.py）：
        DIAG stdout-is-gbk=False type=<class '_pytest.capture.EncodedFile'>
    在测试体内打补丁则不会被覆盖（同一探针：`MAIN raised UnicodeEncodeError`）。
    """
    out, err = GbkConsole(), GbkConsole()
    monkeypatch.setattr(sys, 'stdout', out.stream)
    monkeypatch.setattr(sys, 'stderr', err.stream)
    return {'out': out, 'err': err}


def _hostile_response(**over):
    """正文 / 显示名 / 类型标签 / 警告里各放一组 GBK 编不出的字符。"""
    res = _search_response(
        query='维修',
        hits=[_hit(chat_display_name='会话甲' + HOSTILE,
                   sender_display_name='发送者乙' + HOSTILE,
                   snippet='摘要' + HOSTILE + '结尾',
                   type_label='文本' + HOSTILE)],
        warnings=['WARN' + HOSTILE])
    res.update(over)
    return res


class TestGbkConsoleOutput:
    def test_console_patch_is_active(self, run, monkeypatch):
        """前置条件自检：补丁在**测试体内**打才不会被打架（见 `_attach_gbk_console`）。

        没有这条，未来 pytest 行为再变时整个测试类会"静默不覆盖"。
        """
        console = _attach_gbk_console(monkeypatch)
        assert sys.stdout is console['out'].stream
        run('search', '维修')
        assert sys.stdout is console['out'].stream   # 跑完仍是我们的流
        assert console['out'].text()                 # 确实有内容落到我们的流里

    def test_human_output_degrades_instead_of_crashing(self, run, ctl, monkeypatch):
        """不可编码字符 → 降级显示为 `?`，而不是抛 UnicodeEncodeError。"""
        console = _attach_gbk_console(monkeypatch)
        ctl['response'] = _hostile_response()
        run('search', '维修')                        # 不抛异常才算通过
        text = console['out'].text()
        assert '会话甲' in text                       # 可显示字符**保留**
        assert '发送者乙' in text
        assert '摘要' in text
        assert '???' in text                         # 3 个不可编码字符 → 3 个 ?
        assert '命中: 1 条（第 1/1 页）' in text

    def test_query_echo_survives_hostile_chars(self, run, monkeypatch):
        """回显的查询串也必须被覆盖（它在所有提示之前打印）。"""
        console = _attach_gbk_console(monkeypatch)
        run('search', '维修' + HOSTILE)
        assert '查询: 维修???' in console['out'].text()

    def test_warning_and_type_label_survive_hostile_chars(self, run, ctl,
                                                          monkeypatch):
        """警告文案与类型标签也在覆盖范围内。"""
        console = _attach_gbk_console(monkeypatch)
        ctl['response'] = _hostile_response()
        run('search', '维修')
        text = console['out'].text()
        assert 'WARN???' in text
        assert '文本???' in text

    def test_json_is_pure_ascii_and_lossless_on_gbk(self, run, ctl, monkeypatch):
        """`--json`：输出纯 ASCII（任何编码都不会崩）且**零数据损失**。"""
        console = _attach_gbk_console(monkeypatch)
        ctl['response'] = _hostile_response()
        run('search', '维修', '--json')
        text = console['out'].text()
        assert text.isascii()                      # 不会再撞上 GBK
        body = json.loads(text)                    # 仍是合法 JSON
        assert body == ctl['response']             # 逐键相等：转义是精确的
        # 关键字段与 stub 一致（控制方要求的判别性断言）
        assert body['total'] == ctl['response']['total'] == 1
        assert len(body['results']) == len(ctl['response']['results']) == 1
        # 不可编码字符**原样保留**在解析后的数据里（没有被换成 ?）
        assert HOSTILE in body['results'][0]['snippet']
        assert HOSTILE in body['results'][0]['chat_display_name']

    def test_json_hostile_query_is_ascii_too(self, run, monkeypatch):
        console = _attach_gbk_console(monkeypatch)
        run('search', '维修' + HOSTILE, '--json')
        text = console['out'].text()
        assert text.isascii()
        assert json.loads(text)['parsed']['keywords'] == ['维修' + HOSTILE]

    def test_build_output_survives_hostile_dir_and_progress(self, run, ctl,
                                                            monkeypatch, tmp_path):
        """构建命令也打印路径与引擎提供的进度文案，同样必须不崩。"""
        console = _attach_gbk_console(monkeypatch)
        hostile_dir = tmp_path / ('dec' + HOSTILE)
        hostile_dir.mkdir()
        ctl['dir'] = str(hostile_dir)
        ctl['progress_message'] = '发现源数据' + HOSTILE
        run('build-search-index')
        text = console['out'].text()
        assert 'dec???' in text                    # 路径被降级打印，没有崩
        assert '发现源数据???' in text
        assert '完成: 元数据 5 行' in text

    def test_missing_dir_message_with_hostile_path_survives(self, run, stub, ctl,
                                                            monkeypatch, tmp_path):
        """缺目录提示里也会回显路径 —— 它同样必须不崩（走**真实**的打印函数）。"""
        console = _attach_gbk_console(monkeypatch)
        monkeypatch.setattr(cli, '_print_missing_dir',
                            stub['_real_print_missing_dir'])
        ctl['dir'] = str(tmp_path / 'missing') + HOSTILE
        with pytest.raises(SystemExit) as exc:
            run('search', '维修')
        assert exc.value.code != 0
        text = console['out'].text()
        assert '解密目录不存在' in text
        assert 'missing???' in text

    def test_stdout_without_reconfigure_does_not_break(self, run, ctl, monkeypatch):
        """没有 `reconfigure` 的输出流（老式包装器）不能让命令失败。"""
        class NoReconfigure:
            def __init__(self):
                self.chunks = []

            def write(self, s):
                self.chunks.append(s)

            def flush(self):
                pass

        stream = NoReconfigure()
        monkeypatch.setattr(sys, 'stdout', stream)
        monkeypatch.setattr(sys, 'stderr', NoReconfigure())
        ctl['response'] = _hostile_response()
        run('search', '维修')
        assert '命中: 1 条' in ''.join(stream.chunks)


# --------------------------------------------------------------------------
# 真实进程级验收：GBK 控制台 + 不可编码字符（与用户实际的崩溃路径一致）
# --------------------------------------------------------------------------
# 前面的测试用 stub + 进程内替换 stdout；这里再补一层**真进程**验证：
# `PYTHONIOENCODING=gbk` 让子进程的 stdout 就是 GBK（strict），查询串里带不可编码
# 字符 —— 修复前用户看到的就是
# `UnicodeEncodeError: 'gbk' codec can't encode character '\u2005'` + traceback + exit 1。
# 不依赖任何数据：`--decrypted-dir` 指向 tmp_path 下的**空目录**，唯一的"内容"就是
# 我们自己传进去的合成查询串；stdout/stderr 用**文件句柄**重定向（沙箱下管道会失败）。
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_MAIN_PY = _REPO_ROOT / 'src' / 'main.py'


def _run_real_cli(tmp_path, *argv):
    """→ (exit_code, stdout_text, stderr_text)，子进程 stdout/stderr 强制 GBK。"""
    if not _MAIN_PY.is_file():
        pytest.skip('src/main.py 不存在: %s' % _MAIN_PY)
    out_path = tmp_path / 'stdout.bin'
    err_path = tmp_path / 'stderr.bin'
    env = dict(os.environ,
               PYTHONIOENCODING='gbk',      # strict GBK，等价于本机控制台
               PYTHONUTF8='0')
    with open(out_path, 'wb') as out, open(err_path, 'wb') as err:
        code = subprocess.call([sys.executable, str(_MAIN_PY), *argv],
                               cwd=str(_REPO_ROOT), env=env,
                               stdout=out, stderr=err)
    return (code,
            out_path.read_bytes().decode('gbk', 'replace'),
            err_path.read_bytes().decode('gbk', 'replace'))


class TestRealProcessGbkConsole:
    def test_human_mode_does_not_crash_on_real_process(self, tmp_path):
        empty = tmp_path / 'empty_dec'
        empty.mkdir()
        code, out, err = _run_real_cli(tmp_path, 'search', '维修' + HOSTILE,
                                       '--decrypted-dir', str(empty))
        assert code == 0, err
        assert 'Traceback' not in err
        assert 'UnicodeEncodeError' not in err
        assert '查询: 维修???' in out               # 降级显示，没有崩
        assert '命中: 0 条' in out

    def test_json_mode_does_not_crash_on_real_process(self, tmp_path):
        """真进程 + GBK：`--json` 必须 exit 0、纯 ASCII、可解析、**零损失**。"""
        empty = tmp_path / 'empty_dec'
        empty.mkdir()
        # 注意用**不含空白类字符**的查询串：U+2005 会被查询分词器当成空白分隔符
        # （真引擎把它拆成两个关键词），那不是编码问题 —— 见下一条测试。
        query = '维修' + U2776 + EMOJI
        code, out, err = _run_real_cli(tmp_path, 'search', query,
                                       '--json', '--decrypted-dir', str(empty))
        assert code == 0, err
        assert 'UnicodeEncodeError' not in err
        assert out.isascii()
        body = json.loads(out)                     # 真进程输出也可解析
        assert body['parsed']['keywords'] == [query]   # 逐字保留：零损失
        assert body['total'] == 0

    def test_json_mode_survives_whitespace_like_hostile_char(self, tmp_path):
        """U+2005 是"类空白"字符：分词器会把它当分隔符 —— 但不能因此崩溃。"""
        empty = tmp_path / 'empty_dec'
        empty.mkdir()
        code, out, err = _run_real_cli(tmp_path, 'search', '维修' + HOSTILE,
                                       '--json', '--decrypted-dir', str(empty))
        assert code == 0, err
        assert 'UnicodeEncodeError' not in err
        body = json.loads(out)
        joined = ''.join(body['parsed']['keywords'])
        assert U2776 in joined and EMOJI in joined    # 非空白字符一个不少
        assert body['total'] == 0

    def test_build_mode_does_not_crash_on_real_process(self, tmp_path):
        hostile_dir = tmp_path / ('dec' + HOSTILE)
        hostile_dir.mkdir()
        code, out, err = _run_real_cli(tmp_path, 'build-search-index',
                                       '--decrypted-dir', str(hostile_dir))
        assert code == 0, err
        assert 'UnicodeEncodeError' not in err
        assert 'dec???' in out


# --------------------------------------------------------------------------
# 子命令注册本身
# --------------------------------------------------------------------------
class TestSubcommandRegistration:
    def test_help_lists_both_subcommands(self, run, capsys):
        with pytest.raises(SystemExit) as exc:
            run('--help')
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert 'search' in out
        assert 'build-search-index' in out

    def test_search_help_shows_flags(self, run, capsys):
        with pytest.raises(SystemExit) as exc:
            run('search', '--help')
        assert exc.value.code == 0
        out = capsys.readouterr().out
        for flag in ('--json', '--page', '--per-page', '--sort',
                     '--decrypted-dir'):
            assert flag in out

    def test_build_help_shows_flags(self, run, capsys):
        with pytest.raises(SystemExit) as exc:
            run('build-search-index', '--help')
        assert exc.value.code == 0
        out = capsys.readouterr().out
        for flag in ('--refresh', '--no-text', '--no-meta', '--decrypted-dir'):
            assert flag in out

    def test_success_path_does_not_raise(self, run, stub):
        """成功路径必须正常返回（不抛 SystemExit），否则脚本里的 `main()`
        调用方会被无谓打断；退出码由返回值 0 表达。"""
        assert run('search', '维修') is stub
