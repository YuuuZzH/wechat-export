"""Task 21 前端守卫的 pytest 包装：`DbDirPicker` 的推荐排序/标注渲染
+ `/keyscan`（提取密钥）与 `/settings`（设置）两页的选择器装配。

被测的 JS 在 `tests/js/dbdir_picker_guard.js`（一个 vm realm = 一个页面，
按**文档顺序**把页面脚本当经典脚本加载、配手写假 DOM + 按契约返回的假 fetch）。
pytest 不执行 JS，本仓库也没有 JS 测试框架，所以"页面级行为"只能这样落地。

这里顺带把**变异测试**也包成 pytest 用例（与本项目 `test_chat_focus_js.py` 同一写法）：
守卫支持两个变异模式，变异**只在内存里改源码**，跑完必须逐字节还原（哈希比对）——
没有变异测试的话，"断言恒真"和"断言有效"在输出上长得一模一样：

  * `ignore-recommended` —— 无视 `recommended_path`，永远选 current / 第一条；
  * `drop-reason`        —— 把 `reason` 丢掉，只显示路径（⭐ 标注随之消失）。

`node` 不存在时一律 `skip`（缺 Node 不是本仓库的缺陷）。
"""
import hashlib
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(ROOT, 'tests', 'js')
FIXTURES = os.path.join(JS_DIR, 'fixtures')
GUARD = 'dbdir_picker_guard.js'
# Task 21 改造前的 dbdir.js 逐字节快照（只给"旧格式响应等价性"这一条断言用）
BASELINE = os.path.join(FIXTURES, 'dbdir_prechange_baseline.js')
BASELINE_SHA256 = '27ffcc41ddeffa3ef70a20d7cc74d6b5eed8ef4d445840df2e260b20f6490101'

# 变异**可能**碰到的文件：守卫只在内存里改 dbdir.js，其余只读。
# 全部纳入哈希清单 ⇒ "变异落盘" 会在这里立刻变红。
WATCHED = (
    os.path.join(ROOT, 'src', 'web', 'static', 'js', 'dbdir.js'),
    os.path.join(ROOT, 'src', 'web', 'templates', 'keyscan.html'),
    os.path.join(ROOT, 'src', 'web', 'templates', 'settings.html'),
    os.path.join(ROOT, 'src', 'web', 'templates', 'backup.html'),
    os.path.join(ROOT, 'src', 'web', 'templates', 'decrypt.html'),
    os.path.join(ROOT, 'src', 'web', 'templates', 'keys.html'),
)

MUTATIONS = (
    ('ignore-recommended', '无视 recommended_path，永远选 current / 第一条'),
    ('drop-reason', '把 reason 丢掉，只显示路径'),
)

_RESULT_RE = re.compile(r'RESULT:\s*(\d+)\s+passed,\s*(\d+)\s+failed')


def _node_or_skip():
    exe = shutil.which('node')
    if not exe:
        pytest.skip('未找到 node：跳过 JS 守卫（本仓库的 Python 测试不受影响）')
    return exe


def _run_node(args):
    """跑 node 并**显式按 UTF-8 解码**（Windows 控制台是 GBK，中文诊断会乱码）。"""
    proc = subprocess.run(args, cwd=JS_DIR, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE)
    return (proc.returncode,
            proc.stdout.decode('utf-8', 'replace'),
            proc.stderr.decode('utf-8', 'replace'))


def _run_node_env(args, env_over):
    """带额外环境变量跑 node（`dump-legacy` 的离线比对用）。"""
    env = dict(os.environ)
    env.update(env_over)
    proc = subprocess.run(args, cwd=JS_DIR, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=env)
    return (proc.returncode,
            proc.stdout.decode('utf-8', 'replace'),
            proc.stderr.decode('utf-8', 'replace'))


def _legacy_dump(env_over):
    """跑 `dump-legacy` 模式，返回它打印的"旧格式响应可观测状态"原始文本。"""
    exe = _node_or_skip()
    code, out, err = _run_node_env([exe, GUARD, 'dump-legacy'], env_over)
    assert code == 0, ('dump-legacy 退出码 %s:\n%s\n%s' % (code, out, err))
    assert out.strip().startswith('{'), out
    return out


def _hashes():
    out = {}
    for p in WATCHED:
        with open(p, 'rb') as fh:
            out[p] = hashlib.sha256(fh.read()).hexdigest()
    return out


def test_dbdir_guard_script_exists():
    assert os.path.isfile(os.path.join(JS_DIR, GUARD)), GUARD


def test_dbdir_guard_passes():
    """正常守卫：退出 0、没有 FAIL、RESULT 行显示真的跑了用例、且没改仓库文件。"""
    exe = _node_or_skip()
    before = _hashes()
    code, out, err = _run_node([exe, GUARD])
    combined = out + err
    assert code == 0, ('node %s 退出码 %s\n--- stdout ---\n%s\n--- stderr ---\n%s'
                       % (GUARD, code, out, err))
    assert 'FAIL' not in combined, 'node %s 输出里有 FAIL:\n%s' % (GUARD, combined)
    m = _RESULT_RE.search(out)
    assert m, 'node %s 没有输出 RESULT 行，可能根本没跑用例:\n%s' % (GUARD, out)
    passed, failed = int(m.group(1)), int(m.group(2))
    assert failed == 0, 'node %s 报告 %d 条失败:\n%s' % (GUARD, failed, combined)
    assert passed > 0, 'node %s 一条用例都没跑:\n%s' % (GUARD, out)
    assert _hashes() == before, '守卫**不得**修改仓库文件（只读，变异只在内存里）'


def test_dbdir_guard_is_not_vacuous_on_the_new_fields():
    """守卫必须真的断言了新契约的字段（否则"全绿"可能是因为什么都没测）。

    纯文本层面确认四件必备的事：`recommended_path` 的默认选中、`reason` 的标注、
    `probe.errors` 的"部分探测失败"、以及旧格式响应的向后兼容场景。
    """
    with open(os.path.join(JS_DIR, GUARD), encoding='utf-8') as fh:
        src = fh.read()
    for needle in ('recommended_path', '部分探测失败', '旧格式响应',
                   'recommended_path 被默认选中', 'dump-legacy'):
        assert needle in src, '守卫缺少对 %r 的断言（或缺少离线比对入口）' % needle


@pytest.mark.parametrize('mode,desc', MUTATIONS)
def test_dbdir_guard_mutation_is_red(mode, desc):
    """变异测试：每条变异必须让**指定**断言全部变红（否则它们不是判别性的）。"""
    exe = _node_or_skip()
    before = _hashes()
    code, out, err = _run_node([exe, GUARD, mode])
    combined = out + err
    assert code == 0, ('变异 %s（%s）运行退出码 %s —— 说明变异**没有**让断言变红\n'
                       '--- stdout ---\n%s\n--- stderr ---\n%s'
                       % (mode, desc, code, out, err))
    assert 'MUTATION_CONFIRMED' in out, out
    assert 'are not discriminating' not in combined, combined
    assert 'broke unrelated assertions' not in combined, combined
    assert _hashes() == before, '变异**不得**落盘：被测文件必须逐字节不变'


@pytest.mark.parametrize('mode,desc', MUTATIONS)
def test_dbdir_guard_mutation_restores_bytes_on_disk(mode, desc):
    """把"变异只在内存里"独立钉一遍：跑完变异后逐字节哈希仍与跑前相同。"""
    exe = _node_or_skip()
    before = _hashes()
    _run_node([exe, GUARD, mode])
    after = _hashes()
    assert set(before) == set(after)
    for p in before:
        assert before[p] == after[p], '%s 在变异（%s）运行后被改动（不应落盘）' % (p, mode)


def test_dbdir_guard_reports_its_browser_boundary():
    """守卫必须自己声明"没在真浏览器里验证"—— 防止后人把它当成浏览器验证。"""
    with open(os.path.join(JS_DIR, GUARD), encoding='utf-8') as fh:
        src = fh.read()
    assert '没有' in src and '真浏览器' in src, '守卫缺少"未经真浏览器验证"的边界声明'


def test_baseline_snapshot_is_the_prechange_implementation():
    """冻结基线必须是改造前的逐字节快照（否则等价性断言会自己骗自己）。

    头部那一段注释是本任务加的（说明用途）；**注释之后的正文**必须与
    `git show HEAD:src/web/static/js/dbdir.js` 逐字节一致。
    """
    assert os.path.isfile(BASELINE), BASELINE
    with open(BASELINE, 'rb') as fh:
        raw = fh.read()
    idx = raw.find(b'/* dbdir.js')
    assert idx > 0, '基线快照缺少"头部注释 + 正文"结构'
    body = raw[idx:]
    assert hashlib.sha256(body).hexdigest() == BASELINE_SHA256, \
        '基线正文与改造前的 dbdir.js 不逐字节一致（SHA-256 不匹配）'
    text = body.decode('utf-8')
    assert 'class DbDirPicker' in text
    # 基线里**没有**新契约的东西：它不该认识 recommended_path / tier / reason
    for needle in ('recommended_path', 'tier', 'reason', 'summarize'):
        assert needle not in text, '基线快照里出现了 %r —— 它不再是改造前的实现' % needle


def test_legacy_response_behaviour_is_identical_to_the_prechange_baseline():
    """**旧格式响应的行为必须与改造前逐字相同**（机器证据，不是我说了算）。

    做法：同一份页面模板、同一份旧的 `/api/keys/dirs` 响应体（没有
    tier/reason/recommended_path/probe/wechat_running），分别用
    **改造前的 dbdir.js 快照**与**当前实现**渲染，把全部可观测状态导成 JSON 比对。
    任何"顺手改了旧路径"的改动都会在这里逐字节暴露。
    """
    before = _hashes()
    baseline = _legacy_dump({'DBDIR_SRC': BASELINE})
    current = _legacy_dump({})
    assert baseline == current, (
        '旧格式响应下的行为与改造前不一致：\n--- 改造前 ---\n%s\n--- 现在 ---\n%s'
        % (baseline, current))

    # 防空转：这份 dump 必须是"有内容"的（否则两边都是空对象也能过）
    state = json.loads(current)
    assert state['errors'] == []
    assert len(state['options']) == 2, state
    assert state['selectValue'] and state['inputValue']
    assert 'db_storage' in state['hintHtml']        # 页面静态提示文案原样保留
    assert state['urls'] == ['/api/keys/dirs?mode=auto']
    # 旧口径的文本里不许冒出 undefined（brief 验收 3④）
    assert 'undefined' not in current, current
    assert _hashes() == before, '离线比对**不得**修改仓库文件'
