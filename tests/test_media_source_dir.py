"""Issue #16（GitHub）：图片/文件定位**不能依赖"猜对一个账号目录名"**。

用户报告（2026-09-22）：
  * `.wechat_exp_config.json` 里 `last_backup_wxid` 缺失、或值为 `"output"` 时，**源文件定位不到** ⇒ 图片/文件读不出来；
  * 手工把它改成真实的"微信数据目录名"后图片能读了；
  * 并且提醒：**数据目录名有时候是微信号，有时候不是**（例如 `wxid_xxx_1a2b`）。

控制方实测（真实数据、只计计数）：把账号目录名传错时命中率 **0/12**，传对时 **5/12**，
即"命中与否完全取决于名字猜得对不对"，而代码里没有任何"按文件是否真的存在来验证"的环节。

本文件的布局**全部在 tmp_path 内合成**，不读本机任何真实微信数据。
"""
import os
import sqlite3

import pytest

from engine.services import media
from engine.services.media import _resolve_hardlink_path


def _resolve_account_dir(decrypted_dir, wxid):
    """惰性引用：`resolve_account_dir` 是本次要新增的公开入口。

    惰性引用是为了让"解析行为"那组用例在新增入口之前就能**各自**红（而不是整模块收集失败），
    RED 才有判别力。
    """
    return media.resolve_account_dir(decrypted_dir, wxid)


def _make_storage(tmp_path, accounts):
    """造 <tmp>/xwechat_files/<account>/msg/attach/h1/d1/Img/<md5>.dat。

    accounts: {account_dir_name: [md5, ...]}；返回 (storage_root, {md5: abs_path})
    """
    root = os.path.join(str(tmp_path), 'xwechat_files')
    paths = {}
    for account, md5s in accounts.items():
        for md5 in md5s:
            d = os.path.join(root, account, 'msg', 'attach', 'h1', 'd1', 'Img')
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, md5 + '.dat')
            with open(p, 'wb') as f:
                f.write(b'\x07\x08V2\x08\x07' + b'\x00' * 64)
            paths[md5] = p
    return root, paths


def _make_decrypted(tmp_path, storage_root, *, md5s=(), write_db_info=True):
    """造 <tmp>/decrypted/hardlink/hardlink.db。

    包含两块真实存在的结构：
      * ``db_info.uuid`` —— 存储根（`_get_base_storage` 靠它）；
      * ``image_hardlink_info_v4`` + ``dir2id`` —— md5 → 相对路径（页面**只**传 md5 时走这条路）。

    真实取值形如 ``'<a>_<b>_D:\\xwechat_files'``，代码按 ``'_'`` 最多切 2 刀取最后一段。
    """
    dec = os.path.join(str(tmp_path), 'decrypted')
    os.makedirs(os.path.join(dec, 'hardlink'), exist_ok=True)
    db = os.path.join(dec, 'hardlink', 'hardlink.db')
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE IF NOT EXISTS db_info (Key TEXT, ValueStdStr TEXT)')
    conn.execute('CREATE TABLE IF NOT EXISTS dir2id (name TEXT)')
    conn.execute('CREATE TABLE IF NOT EXISTS image_hardlink_info_v4 '
                 '(md5 TEXT, file_name TEXT, dir1 INTEGER, dir2 INTEGER)')
    if write_db_info:
        conn.execute('INSERT INTO db_info (Key, ValueStdStr) VALUES (?, ?)',
                     ('uuid', '1_' + 'x' * 20 + '_' + storage_root))
        for name in ('h1', 'd1'):
            conn.execute('INSERT INTO dir2id (name) VALUES (?)', (name,))
        for md5 in md5s:
            conn.execute('INSERT INTO image_hardlink_info_v4 '
                         '(md5, file_name, dir1, dir2) VALUES (?, ?, 1, 2)',
                         (md5, md5 + '.dat'))
        conn.commit()
    conn.close()
    return dec


def _rel_for(md5):
    """页面从消息里解析出来的 `path`（相对账号目录）。"""
    return 'msg/attach/h1/d1/Img/' + md5 + '.dat'


@pytest.fixture(autouse=True)
def _isolate_hardcoded_roots(monkeypatch):
    """不许任何一次解析去探测本机真实的 D:\\xwechat_files 等硬编码根目录。

    修复前该常量不存在（`raising=False`），此时硬编码根仍会被探测——这是测试卫生问题，
    也正是本次要修的一处（把内联字面量提成可注入的模块常量）。
    """
    monkeypatch.setattr(media, '_FALLBACK_STORAGE_ROOTS', [], raising=False)


class TestResolverDoesNotDependOnGuessingTheAccountDirName:
    def test_wrong_account_dir_name_still_resolves(self, tmp_path):
        """配置里是垃圾值（用户实测的 'output'）时，仍必须靠"文件真的存在"找到源文件。"""
        md5 = 'a' * 32
        root, paths = _make_storage(tmp_path, {'wxid_real_1a2b': [md5]})
        dec = _make_decrypted(tmp_path, root, md5s=[md5])
        got = _resolve_hardlink_path(
            dec, {'md5': md5, 'media_type': 3, 'local_path': _rel_for(md5)}, 'output')
        assert got is not None, '传错账号目录名就 0 命中 —— 这正是 issue #16 的症状 1'
        assert os.path.samefile(got, paths[md5])

    def test_wrong_account_dir_name_resolves_via_md5_only(self, tmp_path):
        """页面**只**给出 md5（没有 path）时也要能解析 —— 这是 hardlink.db 那条路。"""
        md5 = '9' * 32
        root, paths = _make_storage(tmp_path, {'wxid_real_1a2b': [md5]})
        dec = _make_decrypted(tmp_path, root, md5s=[md5])
        got = _resolve_hardlink_path(dec, {'md5': md5, 'media_type': 3}, 'output')
        assert got is not None, 'md5-only 路径同样不许依赖账号目录名猜对'
        assert os.path.samefile(got, paths[md5])

    def test_empty_account_dir_name_still_resolves(self, tmp_path):
        """没有账号目录名（None）时也必须能解析 —— 不能回退成 'backup'/'decrypted' 这种必然错的名字。"""
        md5 = 'b' * 32
        root, paths = _make_storage(tmp_path, {'wxid_real_1a2b': [md5]})
        dec = _make_decrypted(tmp_path, root, md5s=[md5])
        got = _resolve_hardlink_path(
            dec, {'md5': md5, 'media_type': 3, 'local_path': _rel_for(md5)}, None)
        assert got is not None
        assert os.path.samefile(got, paths[md5])

    def test_account_dir_name_without_wxid_prefix_still_resolves(self, tmp_path):
        """用户明确提醒：数据目录名**有时候不是**微信号（不带 wxid_ 前缀）。"""
        md5 = 'c' * 32
        root, paths = _make_storage(tmp_path, {'SomeAccountName': [md5]})
        dec = _make_decrypted(tmp_path, root, md5s=[md5])
        got = _resolve_hardlink_path(
            dec, {'md5': md5, 'media_type': 3, 'local_path': _rel_for(md5)}, None)
        assert got is not None
        assert os.path.samefile(got, paths[md5])

    def test_picks_the_account_that_has_the_file_not_the_first_one(self, tmp_path):
        """多账号：文件只属于其中一个 ⇒ 必须按"文件存在"选中它，而不是按目录顺序取第一个。"""
        md5 = 'd' * 32
        root, paths = _make_storage(tmp_path, {'aaa_first_account': [],
                                               'zzz_second_account': [md5]})
        dec = _make_decrypted(tmp_path, root, md5s=[md5])
        got = _resolve_hardlink_path(
            dec, {'md5': md5, 'media_type': 3, 'local_path': _rel_for(md5)},
            'aaa_first_account')
        assert got is not None, '第一个账号里没有该文件，但解析必须继续找真正有它的账号'
        assert os.path.samefile(got, paths[md5])

    def test_probe_is_bounded_when_storage_root_unknown(self, tmp_path):
        """拿不到 storage root 时不许抛异常（宁可 404 也不能 500）。"""
        dec = _make_decrypted(tmp_path, os.path.join(str(tmp_path), 'nope'), write_db_info=False)
        assert _resolve_hardlink_path(dec, {'md5': 'e' * 32, 'media_type': 3}, None) is None

    def test_containment_still_enforced(self, tmp_path):
        """放行"按真实目录试"之后，路径穿越必须仍然被拦住。"""
        root, _ = _make_storage(tmp_path, {'wxid_real_1a2b': ['f' * 32]})
        dec = _make_decrypted(tmp_path, root)
        outside = os.path.join(str(tmp_path), 'secret.txt')
        with open(outside, 'w', encoding='utf-8') as f:
            f.write('secret')
        got = _resolve_hardlink_path(
            dec, {'md5': '', 'media_type': 3,
                  'local_path': os.path.join('..', '..', '..', '..', 'secret.txt')}, None)
        assert got is None, 'local_path 里的 .. 逃出账号目录后绝不能被返回'


class TestResolveAccountDirValidatesInsteadOfTrusting:
    """`app.config['WXID']` 来源是配置里的持久化值 ⇒ 必须**校验**它，而不是 `wxid or detect`。"""

    def test_rejects_a_value_that_is_not_a_real_account_dir(self, tmp_path):
        root, _ = _make_storage(tmp_path, {'wxid_real_1a2b': ['0' * 32]})
        dec = _make_decrypted(tmp_path, root)
        got = _resolve_account_dir(dec, 'output')
        assert got == 'wxid_real_1a2b', '配置里的 "output" 不是账号目录 ⇒ 必须改用检测到的真实目录'

    def test_keeps_a_valid_configured_value(self, tmp_path):
        root, _ = _make_storage(tmp_path, {'wxid_real_1a2b': ['0' * 32]})
        dec = _make_decrypted(tmp_path, root)
        assert _resolve_account_dir(dec, 'wxid_real_1a2b') == 'wxid_real_1a2b'

    def test_falls_back_to_detection_when_unset(self, tmp_path):
        root, _ = _make_storage(tmp_path, {'wxid_real_1a2b': ['0' * 32]})
        dec = _make_decrypted(tmp_path, root)
        assert _resolve_account_dir(dec, None) == 'wxid_real_1a2b'

    def test_returns_none_when_nothing_usable(self, tmp_path):
        dec = _make_decrypted(tmp_path, os.path.join(str(tmp_path), 'nope'), write_db_info=False)
        assert _resolve_account_dir(dec, None) is None


class TestCreateAppUsesValidatedAccountDir:
    """`create_app` 里的 `wxid or _detect_wxid(...)` 是 issue #16 的**直接触发点**。

    用户配置里那个 `"output"` 是 truthy 的 ⇒ 自动检测根本不会跑 ⇒ 之后每次媒体解析都用它拼路径。
    """

    def test_bogus_configured_value_is_replaced_by_a_real_account_dir(self, tmp_path):
        md5 = '1' * 32
        root, _ = _make_storage(tmp_path, {'wxid_real_1a2b': [md5]})
        dec = _make_decrypted(tmp_path, root, md5s=[md5])
        from web.app import create_app
        app = create_app(dec, wxid='output')
        assert app.config['WXID'] == 'wxid_real_1a2b', \
            '配置里的 "output" 不存在 ⇒ 必须换成检测到的真实账号目录（否则图片全读不出来）'

    def test_unverifiable_value_is_not_destroyed(self, tmp_path):
        """证明不了它对，也不该把它抹成 None（否则 own_wxid 从"可能错"变成"必然缺"）。"""
        md5 = '2' * 32
        root, _ = _make_storage(tmp_path, {'wxid_real_1a2b': [md5]})
        dec = _make_decrypted(tmp_path, root, md5s=[md5], write_db_info=False)
        from web.app import create_app
        app = create_app(dec, wxid='wxid_from_config')
        assert app.config['WXID'] == 'wxid_from_config'

    def test_valid_configured_value_is_kept(self, tmp_path):
        md5 = '3' * 32
        root, _ = _make_storage(tmp_path, {'wxid_real_1a2b': [md5]})
        dec = _make_decrypted(tmp_path, root, md5s=[md5])
        from web.app import create_app
        app = create_app(dec, wxid='wxid_real_1a2b')
        assert app.config['WXID'] == 'wxid_real_1a2b'
