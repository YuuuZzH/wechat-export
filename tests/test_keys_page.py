"""Task 22 前端守卫的 pytest 包装：`/keys`（手动输入密钥）页的
**推荐排序 + `reason` 逐字标注**（用户决定 S8 = 方案 A「最小改」）。

被测的 JS 在 `tests/js/keys_dirs_annotate_guard.js`（一个 vm realm = 一个页面，
把 `keys.html` 的内联脚本当经典脚本执行，配手写假 DOM + 按契约返回的假 fetch）。
pytest 不执行 JS，本仓库也没有 JS 测试框架，所以"页面级行为"只能这样落地。

这里同时钉住三件**不是靠"我说了算"**的事：

  * **旧引擎响应（没有 tier / reason / recommended_path）下的行为必须与改造前逐字相同** ——
    用 `tests/js/fixtures/keys_prechange_baseline.html`（改造前 keys.html 的逐字节快照）
    与当前模板跑同一份旧响应，把全部可观测状态导成 JSON 逐字节比对；
  * **变异测试 ≥2 条**（与本项目 `test_dbdir_picker_js.py` / `test_chat_focus_js.py` 同一写法）：
    守卫支持两个变异模式，变异**只在内存里改源码**，跑完必须逐字节还原（哈希比对）——
    没有变异测试的话，"断言恒真"和"断言有效"在输出上长得一模一样：
      * `ignore-recommended` —— 无视 `recommended_path`，永远选 current / 第一条；
      * `drop-reason`        —— 把 `reason` 丢掉，只显示路径。
  * **前端不许再造一份 `reason` 文案**（api.md A7：一份事实源）—— 直接在模板文本层面
    确认 A7 表里那五句后端文案**一个都**不出现在 `keys.html` 里。

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
TPL_DIR = os.path.join(ROOT, 'src', 'web', 'templates')
GUARD = 'keys_dirs_annotate_guard.js'
KEYS_TPL = os.path.join(TPL_DIR, 'keys.html')
# Task 22 改造前的 keys.html 逐字节快照（只给"旧格式响应等价性"这一条断言用）。
# ⚠ 这份快照**不是** `git show HEAD:` —— 改造前工作区里的 keys.html 已经领先 HEAD
#   （别的任务先改过这一页），所以正文哈希是改造前**工作区**文件的 SHA-256。
BASELINE = os.path.join(FIXTURES, 'keys_prechange_baseline.html')
BASELINE_SHA256 = 'b25ea9ac3e8d7b33a789c0fcde8604f70cf15b213d29cb1ffe8cb71e3d85829a'

# 守卫**可能**碰到的文件：守卫只在内存里改内联源码，其余只读。
# 全部纳入哈希清单 ⇒ "变异落盘"会在这里立刻变红。
WATCHED = (
    KEYS_TPL,
    os.path.join(TPL_DIR, 'backup.html'),
    os.path.join(TPL_DIR, 'decrypt.html'),
    os.path.join(TPL_DIR, 'keyscan.html'),
    os.path.join(TPL_DIR, 'settings.html'),
    os.path.join(ROOT, 'src', 'web', 'static', 'js', 'dbdir.js'),
    BASELINE,
)

MUTATIONS = (
    ('ignore-recommended', '无视 recommended_path，永远选 current / 第一条'),
    ('drop-reason', '把 reason 丢掉，只显示路径'),
)

# api.md A7「五档判据与逐字文案」里的五句 reason —— 前端**只许显示**，不许在 JS 里再造。
A7_REASON_LITERALS = (
    '微信进程正在使用',
    '数据库正被占用',
    '最近活跃（',
    '微信配置指向此目录',
    '未发现活动迹象',
)

_RESULT_RE = re.compile(r'RESULT:\s*(\d+)\s+passed,\s*(\d+)\s+failed')


def _node_or_skip():
    exe = shutil.which('node')
    if not exe:
        pytest.skip('未找到 node：跳过 JS 守卫（本仓库的 Python 测试不受影响）')
    return exe


def _run_node(args, env_over=None):
    """跑 node 并**显式按 UTF-8 解码**（Windows 控制台是 GBK，中文诊断会乱码）。"""
    env = dict(os.environ)
    env.update(env_over or {})
    proc = subprocess.run(args, cwd=JS_DIR, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=env)
    return (proc.returncode,
            proc.stdout.decode('utf-8', 'replace'),
            proc.stderr.decode('utf-8', 'replace'))


def _legacy_dump(env_over):
    """跑 `dump-legacy` 模式，返回它打印的"旧格式响应可观测状态"原始文本。"""
    exe = _node_or_skip()
    code, out, err = _run_node([exe, GUARD, 'dump-legacy'], env_over)
    assert code == 0, ('dump-legacy 退出码 %s:\n%s\n%s' % (code, out, err))
    assert out.strip().startswith('{'), out
    return out


def _hashes():
    out = {}
    for p in WATCHED:
        with open(p, 'rb') as fh:
            out[p] = hashlib.sha256(fh.read()).hexdigest()
    return out


def _read(name):
    with open(os.path.join(TPL_DIR, name), encoding='utf-8') as fh:
        return fh.read()


def test_keys_guard_script_exists():
    assert os.path.isfile(os.path.join(JS_DIR, GUARD)), GUARD


def test_keys_guard_passes():
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
    assert passed >= 40, 'node %s 只跑了 %d 条用例（少于预期的 40 条）:\n%s' % (GUARD, passed, out)
    assert _hashes() == before, '守卫**不得**修改仓库文件（只读，变异只在内存里）'


def test_keys_guard_is_not_vacuous_on_the_new_fields():
    """守卫必须真的断言了新契约的字段（否则"全绿"可能是因为什么都没测）。"""
    with open(os.path.join(JS_DIR, GUARD), encoding='utf-8') as fh:
        src = fh.read()
    for needle in ('recommended_path', 'reason', '旧格式响应',
                   'recommended_path 被默认选中', 'current 不在 dirs 里',
                   'dump-legacy', 'real-payload'):
        assert needle in src, '守卫缺少对 %r 的断言（或缺少离线比对入口）' % needle


def test_keys_guard_reports_its_browser_boundary():
    """守卫必须自己声明"没在真浏览器里验证"—— 防止后人把它当成浏览器验证。"""
    with open(os.path.join(JS_DIR, GUARD), encoding='utf-8') as fh:
        src = fh.read()
    assert '没有' in src and '真浏览器' in src, '守卫缺少"未经真浏览器验证"的边界声明'


def test_page_structure_kept_by_this_task():
    """硬约束：只改 `applyDirs()`。页面结构、控件类型、既有函数语义都不许动。

    * `#cfg-db-dir` **仍是 `<select>`**（方案 A；改成 `<input>` 是方案 B，用户没选）；
    * 这一页**仍然不加载** `dbdir.js`（不迁共享组件）；
    * `loadDirs` / `loadStatus` / `deepSearch` / `currentDir` 都还在，接口没换。
    """
    keys = _read('keys.html')
    assert '<select id="cfg-db-dir">' in keys
    assert '<input id="cfg-db-dir"' not in keys
    assert 'dbdir.js' not in keys
    for needle in ('function applyDirs(', 'function loadDirs(', 'function loadStatus(',
                   'function deepSearch(', 'function currentDir(',
                   '/api/keys/dirs?mode=', '/api/keys/dbdir', '/api/keys/status?db_dir='):
        assert needle in keys, 'keys.html 缺少 %r（既有接口/函数被改动了？）' % needle


def test_js_does_not_reinvent_the_backend_reason_copy():
    """硬约束：`reason` 只由后端给一份，前端只显示、**不许**在 JS 里再造文案。

    做法：把 api.md A7 表里的五句逐字文案拿去搜 `keys.html` —— 一句都不许出现。
    """
    keys = _read('keys.html')
    for literal in A7_REASON_LITERALS:
        assert literal not in keys, (
            'keys.html 里出现了后端 reason 文案 %r —— 前端不许再造一份文案' % literal)


def test_baseline_snapshot_is_the_prechange_implementation():
    """冻结基线必须是改造前的逐字节快照（否则等价性断言会自己骗自己）。

    头部那一段 HTML 注释是本任务加的（说明用途）；**注释之后的正文**必须与改造前的
    `src/web/templates/keys.html` 逐字节一致 —— 注意改造前的工作区文件已经领先
    `git show HEAD:`（别的任务先改过这一页），所以这里比的是改造前**工作区**的 SHA-256。
    """
    assert os.path.isfile(BASELINE), BASELINE
    with open(BASELINE, 'rb') as fh:
        raw = fh.read()
    assert raw[:3] != b'\xef\xbb\xbf', '基线快照带了 BOM'
    idx = raw.find(b'{% extends', raw.find(b'-->'))
    assert idx > 0, '基线快照缺少"头部注释 + 正文"结构'
    body = raw[idx:]
    assert hashlib.sha256(body).hexdigest() == BASELINE_SHA256, \
        '基线正文与改造前的 keys.html 不逐字节一致（SHA-256 不匹配）'
    text = body.decode('utf-8')
    assert 'function applyDirs(' in text
    # 基线里**没有**新契约的东西：它不该认识 recommended_path / tier / reason
    for needle in ('recommended_path', 'tier', 'reason', 'chosen'):
        assert needle not in text, '基线快照里出现了 %r —— 它不再是改造前的实现' % needle


def test_legacy_response_behaviour_is_identical_to_the_prechange_baseline():
    """**旧格式响应的行为必须与改造前逐字相同**（机器证据，不是我说了算）。

    做法：同一份旧的 `/api/keys/dirs` 响应体（没有 tier/reason/recommended_path），
    分别用**改造前的 keys.html 快照**与**当前实现**渲染，把全部可观测状态
    （option 的 value/text/selected、`select.value`、`currentDir()`、`applyDirs()` 返回值、
    `#dir-hint` 的 HTML、请求过的 URL、#sum-badge 文本）导成 JSON 比对。
    任何"顺手改了旧路径"的改动都会在这里逐字节暴露。
    """
    before = _hashes()
    baseline = _legacy_dump({'KEYS_TPL': BASELINE})
    current = _legacy_dump({})
    assert baseline == current, (
        '旧格式响应下的行为与改造前不一致：\n--- 改造前 ---\n%s\n--- 现在 ---\n%s'
        % (baseline, current))

    # 防空转：这份 dump 必须是"有内容"的（否则两边都是空对象也能过）
    state = json.loads(current)
    assert state['errors'] == []
    assert len(state['options']) == 2, state
    assert state['dirsReturn'] == 2, state
    assert state['selectValue'] and state['currentDir'] == state['selectValue']
    assert 'db_storage' in state['hintHtml']         # 页面静态提示文案原样保留
    # 启动链不变：先 /api/keys/dirs?mode=auto，再 loadDirs().then(loadStatus) 拉一次状态
    assert state['urls'][0] == '/api/keys/dirs?mode=auto', state['urls']
    assert state['urls'][1].startswith('/api/keys/status?db_dir='), state['urls']
    assert len(state['urls']) == 2, state['urls']
    # 旧口径的文本里不许冒出 undefined（brief 验收 3③）
    assert 'undefined' not in current, current
    assert _hashes() == before, '离线比对**不得**修改仓库文件'


@pytest.mark.parametrize('mode,desc', MUTATIONS)
def test_keys_guard_mutation_is_red(mode, desc):
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
def test_keys_guard_mutation_restores_bytes_on_disk(mode, desc):
    """把"变异只在内存里"独立钉一遍：跑完变异后逐字节哈希仍与跑前相同。"""
    exe = _node_or_skip()
    before = _hashes()
    _run_node([exe, GUARD, mode])
    after = _hashes()
    assert set(before) == set(after)
    for p in before:
        assert before[p] == after[p], '%s 在变异（%s）运行后被改动（不应落盘）' % (p, mode)
