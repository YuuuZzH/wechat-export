"""`/settings`（设置）页面的**静态契约**：新增的「微信数据目录 (db_storage)」一节。

本文件测什么、不测什么（说清楚，否则就是"只查静态 HTML 的假测试"）：

  * **能测**：`/settings` 的 HTTP 契约与渲染结果、新一节的 5 个控件 id、逐字标签、
    控件形状与 `backup.html` 逐字一致、`dbdir.js` 先于页面内联脚本加载、
    以及**没有**弄坏设置页原有的 ASR 控件与脚本。
  * **不能测**：DOM 渲染后的行为（pytest 不执行 JS）。那部分交给
    `tests/js/dbdir_picker_guard.js` 的 `scenarioS`：它在一个 vm realm 里按文档顺序
    加载 `/settings` 的脚本，断言选择器被实例化、下拉被填充并默认选中推荐项、
    且设置页原有的 `/api/settings/asr` 请求照旧发出。

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

PICKER_IDS = ('cfg-db-dir', 'cfg-db-dir-list', 'btn-deep', 'btn-use-dir', 'dir-hint')
HINT_LABEL = '检测到的数据目录（选择即填入上面的输入框）'
DIR_LABEL = '微信数据目录 (db_storage)'
# 设置页原有的 ASR 控件（加新一节不许把它们弄丢）
ASR_IDS = ('eng-local', 'eng-baidu', 'cfg-model', 'cfg-language', 'cfg-simplify',
           'model-status', 'btn-model-download', 'model-progress', 'model-bar-wrap',
           'model-bar', 'cfg-baidu-key', 'cfg-baidu-secret', 'cfg-baidu-pid',
           'btn-test-baidu', 'baidu-hint', 'btn-save', 'save-msg')

SCRIPT_RE = re.compile(r'<script([^>]*)>([\s\S]*?)</script>')
SRC_RE = re.compile(r"/static/js/([A-Za-z0-9_\-\.]+)")


def _read(name):
    with open(os.path.join(TPL_DIR, name), encoding='utf-8') as fh:
        return fh.read()


def _tag(html, pattern):
    m = re.search(pattern, html)
    return m.group(0) if m else None


@pytest.fixture(autouse=True)
def no_wxid_detection(monkeypatch):
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
    r = client.get('/settings')
    assert r.status_code == 200
    return r.data.decode('utf-8')


# ==========================================================================
# 1. 新一节：微信数据目录
# ==========================================================================
def test_settings_page_renders(html):
    assert '设置' in html


@pytest.mark.parametrize('cid', PICKER_IDS)
def test_picker_controls_present(html, cid):
    assert 'id="%s"' % cid in html, '设置页缺少选择器控件 #%s' % cid


@pytest.mark.parametrize('cid', PICKER_IDS)
def test_picker_control_ids_are_unique(html, cid):
    assert html.count('id="%s"' % cid) == 1, \
        '#%s 出现了 %d 次（重复 id 会让 getElementById 拿到另一个元素）' % (
            cid, html.count('id="%s"' % cid))


def test_labels_are_verbatim(html):
    assert DIR_LABEL in html
    assert HINT_LABEL in html


def test_section_has_a_heading(html):
    """新一节的标题（用户说的"在设置里面，增加上述对应选择"）。"""
    assert re.search(r'<h3[^>]*>\s*微信数据目录\s*</h3>', html), '缺少「微信数据目录」小节标题'


def test_picker_shape_matches_backup_page():
    """控件标签串（含 style）与 backup.html 逐字一致 —— 一处定义，多处照抄。"""
    backup = _read('backup.html')
    settings = _read('settings.html')
    for pattern in (r'<input id="cfg-db-dir"[^>]*>',
                    r'<select id="cfg-db-dir-list"[^>]*>',
                    r'<button id="btn-deep"[^>]*>[^<]*</button>',
                    r'<button id="btn-use-dir"[^>]*>[^<]*</button>'):
        b = _tag(backup, pattern)
        s = _tag(settings, pattern)
        assert b is not None, 'backup.html 里找不到 %s' % pattern
        assert s == b, 'settings.html 的 %s 与 backup.html 不一致:\n  backup  : %s\n  settings: %s' % (pattern, b, s)


# ==========================================================================
# 2. 脚本装配：dbdir.js 必须先于页面内联脚本
# ==========================================================================
def test_dbdir_js_is_loaded_before_the_inline_page_script(html):
    entries = []
    for attrs, code in SCRIPT_RE.findall(html):
        m = SRC_RE.search(attrs)
        entries.append(m.group(1) if m else '(inline)')
    assert 'dbdir.js' in entries, '设置页没有加载 dbdir.js（顺序=%s）' % entries
    assert '(inline)' in entries, entries
    assert entries.index('dbdir.js') < entries.index('(inline)'), \
        'dbdir.js 必须在页面内联脚本之前加载（否则 new DbDirPicker 直接 ReferenceError）: %s' % entries


def test_page_instantiates_the_picker_with_the_shared_id(html):
    assert re.search(r"new\s+DbDirPicker\(\s*\{\s*inputId:\s*'cfg-db-dir'\s*\}\s*\)", html), \
        '@settings 没有用共享 id 实例化 DbDirPicker'


def test_shared_ids_are_not_overridden():
    """设置页没有与共享 id 冲突的控件 ⇒ 不需要改 `dbdir.js` 的默认 id。

    这条断言把"没有冲突"这个结论钉在数据上：`#cfg-db-dir` 只在**本页**出现一次，
    且 `dbdir.js` 的默认 id 常量没被任务 21 改过。
    """
    settings = _read('settings.html')
    dbdir = _read_js('dbdir.js')
    assert settings.count('id="cfg-db-dir"') == 1
    for default in ('\"cfg-db-dir\"', '\"cfg-db-dir-list\"', '\"btn-deep\"',
                    '\"btn-use-dir\"', '\"dir-hint\"'):
        assert default in dbdir, 'dbdir.js 的默认 id %s 被改动了' % default


def _read_js(name):
    with open(os.path.join(ROOT, 'src', 'web', 'static', 'js', name),
              encoding='utf-8') as fh:
        return fh.read()


# ==========================================================================
# 3. 不许弄坏设置页原有的东西
# ==========================================================================
@pytest.mark.parametrize('cid', ASR_IDS)
def test_existing_asr_controls_survive(html, cid):
    assert 'id="%s"' % cid in html, '原有控件 #%s 消失了' % cid


def test_existing_settings_script_still_runs():
    settings = _read('settings.html')
    inline = '\n'.join(code for attrs, code in SCRIPT_RE.findall(settings)
                       if not SRC_RE.search(attrs))
    for needle in ('loadSettings()', '/api/settings/asr', 'btn-save', 'btn-model-download'):
        assert needle in inline, '设置页原有的脚本片段 %r 不见了' % needle


def test_settings_does_not_persist_db_dir_through_the_asr_endpoint():
    """目录的落盘走 `dbdir.js` 的「使用该目录」(`POST /api/keys/dbdir`)，
    **不**塞进 ASR 设置体（后端 ASR 端点的契约不该被本任务悄悄扩大）。
    """
    settings = _read('settings.html')
    save_body = re.search(r'function collect\(\)\s*\{([\s\S]*?)\n\}', settings)
    assert save_body, '找不到 collect()'
    assert 'db_dir' not in save_body.group(1), \
        'ASR 保存体里出现了 db_dir —— 那会把 db_storage 落盘到 ASR 配置里'
