"""`/keyscan`（提取密钥）页面的**静态契约**：数据目录选择器与请求体里的 `db_dir`。

本文件测什么、不测什么（说清楚，否则就是"只查静态 HTML 的假测试"）：

  * **能测**：`/keyscan` 的 HTTP 契约与渲染结果、选择器那 5 个控件的 id、逐字标签、
    `dbdir.js` 的加载（含与其它脚本的相对顺序）、`#cfg-db-dir` 的唯一性，
    以及"与 `backup.html` 同形"这件事（控件的标签/样式串逐字一致）。
  * **不能测**：DOM 渲染后的行为（pytest 不执行 JS）。那部分交给
    `tests/js/dbdir_picker_guard.js`：它在一个 vm realm 里按**文档顺序**加载
    `/keyscan` 的全部脚本，断言选择器真的被实例化、下拉真的被填充、
    `recommended_path` 真的被选中，并且**两种提取（内存扫描 / Hook）的请求
    body 里真的带上了 `db_dir`**。

全部使用**合成**夹具（tmp 目录 + 显式 wxid），绝不读取真实微信数据目录。
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

from web.app import create_app
from engine.services import media as media_mod

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL_DIR = os.path.join(ROOT, 'src', 'web', 'templates')
JS_DIR = os.path.join(ROOT, 'src', 'web', 'static', 'js')

# 与 `backup.html` 同形的 5 个控件（id 就是 `DbDirPicker` 的默认值）
PICKER_IDS = ('cfg-db-dir', 'cfg-db-dir-list', 'btn-deep', 'btn-use-dir', 'dir-hint')
# 用户原话里的那句逐字标签
HINT_LABEL = '检测到的数据目录（选择即填入上面的输入框）'
DIR_LABEL = '微信数据目录 (db_storage)'
# 参考形态的脚本顺序（backup.html / decrypt.html 一致）
SCRIPT_ORDER = ('sse_progress.js', 'wizard.js', 'dbdir.js')

SRC_RE = re.compile(r"/static/js/([A-Za-z0-9_\-\.]+)")
INLINE_RE = re.compile(r'<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>')


def _read(name):
    with open(os.path.join(TPL_DIR, name), encoding='utf-8') as fh:
        return fh.read()


def _tag(html, pattern):
    m = re.search(pattern, html)
    return m.group(0) if m else None


def html_has_ids(html, ids):
    return all('id="%s"' % i in html for i in ids)


@pytest.fixture(autouse=True)
def no_wxid_detection(monkeypatch):
    """屏蔽 `_detect_wxid`：绝不因为某台机器上真实存在 `D:\\xwechat_files` 而读真数据。"""
    monkeypatch.setattr(media_mod, '_detect_wxid', lambda decrypted_dir: None)


@pytest.fixture
def client(tmp_path):
    dec = tmp_path / 'dec'
    (dec / 'message').mkdir(parents=True)
    app = create_app(str(dec), wxid='wxid_owner')
    app.config['TESTING'] = True
    return app.test_client()


@pytest.fixture
def html(client):
    r = client.get('/keyscan')
    assert r.status_code == 200
    return r.data.decode('utf-8')


# ==========================================================================
# 1. 页面本身
# ==========================================================================
def test_keyscan_page_renders(html):
    assert '提取密钥' in html


@pytest.mark.parametrize('cid', PICKER_IDS)
def test_picker_controls_present(html, cid):
    assert 'id="%s"' % cid in html, '缺少选择器控件 #%s' % cid


@pytest.mark.parametrize('cid', PICKER_IDS)
def test_picker_control_ids_are_unique(html, cid):
    """id 必须唯一 —— 重复 id 会让 `getElementById` 拿到"另一个"元素（真浏览器里静默错）。"""
    assert html.count('id="%s"' % cid) == 1, '#%s 出现了 %d 次' % (cid, html.count('id="%s"' % cid))


def test_hint_label_is_verbatim(html):
    assert HINT_LABEL in html
    assert DIR_LABEL in html


def test_dbdir_js_is_loaded_in_page_order(html):
    """`dbdir.js` 必须与参考形态同序加载：sse_progress → wizard → dbdir。"""
    order = SRC_RE.findall(html)
    for name in SCRIPT_ORDER:
        assert name in order, '页面没有加载 %s（顺序=%s）' % (name, order)
    assert [n for n in order if n in SCRIPT_ORDER] == list(SCRIPT_ORDER), order


def test_page_instantiates_the_picker_with_the_shared_id(html):
    assert re.search(r"new\s+DbDirPicker\(\s*\{\s*inputId:\s*'cfg-db-dir'\s*\}\s*\)", html), \
        '@keyscan 没有用共享 id 实例化 DbDirPicker'


def test_both_extraction_paths_route_through_the_dir_helper(html):
    """两种提取的 body 都必须经过**同一个**取 `db_dir` 的助手。

    真正的行为断言在 `tests/js/dbdir_picker_guard.js`（点按钮、抓请求体）；这里只钉住
    "没有哪一条路径绕过它自己拼 body"（源码级、廉价、防漂移）。
    """
    assert html.count('dbDirBody(') >= 3, html.count('dbDirBody(')   # 定义 + 两处使用
    assert re.search(r"getBody:\s*\(\)\s*=>\s*dbDirBody\(", html), '内存扫描没有走助手'
    assert re.search(r"body:\s*dbDirBody\(", html), 'Hook 提取没有走助手'
    assert html.count('db_dir') >= 1


def test_keyscan_did_not_lose_its_existing_controls(html):
    """加选择器不许把原有控件弄丢。"""
    for cid in ('tab-memory', 'tab-hook', 'btn-run-memory', 'btn-run-hook',
                'panel-memory', 'panel-hook', 'hook-steps', 'result-container'):
        assert 'id="%s"' % cid in html, '原有控件 #%s 消失了' % cid


# ==========================================================================
# 2. 与参考形态（backup.html / decrypt.html）同形
# ==========================================================================
def test_picker_shape_matches_backup_page():
    """标签文案与控件标签**逐字**取自 backup.html —— 一份事实源，不各写一份。"""
    backup = _read('backup.html')
    keyscan = _read('keyscan.html')
    # 5 个控件的开标签串（含 style）逐字一致
    for pattern in (r'<input id="cfg-db-dir"[^>]*>',
                    r'<select id="cfg-db-dir-list"[^>]*>',
                    r'<button id="btn-deep"[^>]*>[^<]*</button>',
                    r'<button id="btn-use-dir"[^>]*>[^<]*</button>',
                    r'<div id="dir-hint"[^>]*>'):
        b = _tag(backup, pattern)
        k = _tag(keyscan, pattern)
        assert b is not None, 'backup.html 里找不到 %s' % pattern
        assert k == b, 'keyscan.html 的 %s 与 backup.html 不一致:\n  backup : %s\n  keyscan: %s' % (pattern, b, k)
    assert HINT_LABEL in keyscan and HINT_LABEL in backup


def test_settings_picker_shape_matches_backup_page():
    backup = _read('backup.html')
    settings = _read('settings.html')
    for pattern in (r'<input id="cfg-db-dir"[^>]*>',
                    r'<select id="cfg-db-dir-list"[^>]*>',
                    r'<button id="btn-deep"[^>]*>[^<]*</button>',
                    r'<button id="btn-use-dir"[^>]*>[^<]*</button>'):
        b = _tag(backup, pattern)
        s = _tag(settings, pattern)
        assert b is not None and s == b, '%s 在 settings.html 与 backup.html 不一致' % pattern


def test_backup_and_decrypt_templates_untouched_by_this_task():
    """Task 21 的硬约束：不许改 backup.html / decrypt.html / keys.html。

    这三页也在用 `dbdir.js`。这里断言它们**仍然**是各自的样子（只读检查，不做哈希
    硬编码 —— 别的任务可能合法地改它们，那不该在这里假红）。
    """
    backup = _read('backup.html')
    decrypt = _read('decrypt.html')
    keys = _read('keys.html')
    assert "new DbDirPicker({ inputId: 'cfg-db-dir' })" in backup
    assert "new DbDirPicker({ inputId: 'cfg-db-dir' })" in decrypt
    assert 'id="cfg-db-dir-list"' in backup and 'id="cfg-db-dir-list"' in decrypt
    assert html_has_ids(backup, PICKER_IDS)
    assert html_has_ids(decrypt, PICKER_IDS)
    # keys.html 有自己的 select#cfg-db-dir + 自写 JS，**不**加载 dbdir.js（本次不改它）
    assert '<select id="cfg-db-dir">' in keys
    assert 'dbdir.js' not in keys


def test_js_dir_has_the_shared_picker():
    assert os.path.isfile(os.path.join(JS_DIR, 'dbdir.js'))
