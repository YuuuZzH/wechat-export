"""Task 18 前端守卫的 pytest 包装：`/chat?open=..&focus=..&ft=..` 深链定位。

`tests/js/chat_focus_guard.js` 在一个 **vm realm** 里按 `templates/index.html` 的
真实脚本顺序加载 `/chat` 页面的全部经典脚本，配一个手写假 DOM + 一个**按
`/api/messages` 契约真的算页号**的假 fetch。

⚠️ **边界（不得含糊）**：本仓库没有 Playwright/Selenium，所以这里**没有**在真浏览器
里验证过任何像素/布局行为。假 DOM 只实现 `app.js` 用到的那部分（`getElementById` /
`innerHTML` / `classList` / `children` / `getAttribute` / `scrollIntoView`），
`innerHTML` 只用正则抽元素、不做真正的 HTML 解析（层级不可信、顺序可信）。
`querySelector` 一律返回 `null` —— 那些调用点在 `app.js` 里本来就有 null 判断。

两件事在这里被钉住：
  ① 正常守卫（0 failed，且真的跑了用例）；
  ② **变异测试**：把 focus 参数从请求里摘掉（只在内存里改源码）→ 指定的断言**必须**
     全部变红。没有变异测试的话，"断言恒真"和"断言有效"在输出上长得一模一样。
     变异**不落盘**：跑完前后对被测文件做 SHA-256 比对，逐字节一致才算通过。
"""
import hashlib
import os
import re
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(ROOT, 'tests', 'js')
GUARD = 'chat_focus_guard.js'

# 变异测试可能碰到的文件（守卫在内存里改 app.js；其余只是被读）
# Task 19 起守卫还会**直接调用** `api.js::messages()`（URL 的归属方）⇒ 也纳入只读哈希清单。
WATCHED = (
    os.path.join(ROOT, 'src', 'web', 'static', 'js', 'app.js'),
    os.path.join(ROOT, 'src', 'web', 'static', 'js', 'api.js'),
    os.path.join(ROOT, 'src', 'web', 'static', 'js', 'search-render.js'),
    os.path.join(ROOT, 'src', 'web', 'static', 'css', 'app.css'),
    os.path.join(ROOT, 'src', 'web', 'templates', 'index.html'),
)

_RESULT_RE = re.compile(r'RESULT:\s*(\d+)\s+passed,\s*(\d+)\s+failed')


def _node_or_skip():
    import shutil
    exe = shutil.which('node')
    if not exe:
        pytest.skip('未找到 node：跳过 JS 守卫（本仓库的 Python 测试不受影响）')
    return exe


def _run_node(args):
    """跑 node，显式按 UTF-8 解码（Windows 控制台是 GBK，中文诊断会乱码）。"""
    proc = subprocess.run(args, cwd=JS_DIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return (proc.returncode,
            proc.stdout.decode('utf-8', 'replace'),
            proc.stderr.decode('utf-8', 'replace'))


def _hashes():
    out = {}
    for p in WATCHED:
        with open(p, 'rb') as fh:
            out[p] = hashlib.sha256(fh.read()).hexdigest()
    return out


def test_focus_guard_script_exists():
    assert os.path.isfile(os.path.join(JS_DIR, GUARD)), GUARD


def test_focus_guard_passes():
    """正常守卫：退出 0、没有 FAIL、RESULT 行显示真的跑了用例。"""
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
    assert failed == 0
    assert passed > 0
    assert _hashes() == before, '守卫**不得**修改仓库文件（只读，变异只在内存里）'


def test_focus_guard_mutation_is_red():
    """变异测试：摘掉 focus 参数 → 指定断言必须全部变红（否则它们不是判别性的）。"""
    exe = _node_or_skip()
    before = _hashes()
    code, out, err = _run_node([exe, GUARD, 'drop-focus-param'])
    combined = out + err
    assert code == 0, ('变异运行退出码 %s（说明变异**没有**让断言变红）\n'
                       '--- stdout ---\n%s\n--- stderr ---\n%s' % (code, out, err))
    assert 'MUTATION_CONFIRMED' in out, out
    assert 'FAIL  dropping the focus params left these assertions green' not in combined, combined
    assert _hashes() == before, '变异**不得**落盘：被测文件必须逐字节不变'


def test_focus_guard_mutation_restores_bytes_on_disk():
    """把"变异只在内存里"这件事独立钉一遍：跑完变异后逐字节哈希仍与跑前相同。"""
    exe = _node_or_skip()
    before = _hashes()
    _run_node([exe, GUARD, 'drop-focus-param'])
    after = _hashes()
    assert set(before) == set(after)
    for p in before:
        assert before[p] == after[p], '%s 在变异运行后被改动（不应落盘）' % p


def test_focus_guard_reports_its_browser_boundary():
    """守卫必须自己声明"没在真浏览器里验证"——防止后人把它当成浏览器验证。"""
    with open(os.path.join(JS_DIR, GUARD), encoding='utf-8') as fh:
        src = fh.read()
    assert '没有' in src and '真浏览器' in src, '守卫缺少"未经真浏览器验证"的边界声明'
