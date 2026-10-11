"""Tests for WeChat data-directory detection (engine/utils.py).

全部使用临时目录与伪造的 ini/注册表，不依赖本机真实微信数据。
"""
import os

import pytest

from engine import utils


def _make_db_storage(root, name="wxid_testuser_1a2b"):
    """在 root 下造一个 xwechat_files/<wxid>/db_storage 结构。"""
    storage = os.path.join(root, "xwechat_files", name, "db_storage")
    os.makedirs(os.path.join(storage, "message"), exist_ok=True)
    os.makedirs(os.path.join(storage, "contact"), exist_ok=True)
    with open(os.path.join(storage, "message", "message_0.db"), "wb") as f:
        f.write(b"\x00" * 4096)
    return storage


class TestCandidatesUnder:
    def test_standard_layout(self, tmp_path):
        root = str(tmp_path)
        storage = _make_db_storage(root)
        got = utils._candidates_under(root)
        assert len(got) == 1
        assert os.path.normcase(got[0]) == os.path.normcase(storage)

    def test_root_is_xwechat_files(self, tmp_path):
        storage = _make_db_storage(str(tmp_path))
        xwf = os.path.join(str(tmp_path), "xwechat_files")
        got = utils._candidates_under(xwf)
        assert [os.path.normcase(g) for g in got] == [os.path.normcase(storage)]

    def test_root_is_account_dir(self, tmp_path):
        storage = _make_db_storage(str(tmp_path))
        acc = os.path.dirname(storage)
        got = utils._candidates_under(acc)
        assert [os.path.normcase(g) for g in got] == [os.path.normcase(storage)]

    def test_root_is_db_storage(self, tmp_path):
        storage = _make_db_storage(str(tmp_path))
        got = utils._candidates_under(storage)
        assert got == [storage]

    def test_empty_root(self, tmp_path):
        assert utils._candidates_under(str(tmp_path)) == []


class TestIniAndRegistryParsing:
    def test_plain_path_ini(self, tmp_path, monkeypatch):
        appdata = tmp_path / "Roaming"
        cfg = appdata / "Tencent" / "xwechat" / "config"
        cfg.mkdir(parents=True)
        (cfg / "abc.ini").write_bytes(b"D:\\")
        monkeypatch.setenv("APPDATA", str(appdata))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        assert utils._wechat_config_paths() == [os.path.normpath("D:\\")]

    def test_key_value_and_quotes(self, tmp_path, monkeypatch):
        appdata = tmp_path / "Roaming"
        cfg = appdata / "Tencent" / "xwechat" / "config"
        cfg.mkdir(parents=True)
        (cfg / "a.ini").write_text("FileSavePath=\"E:\\WeChatData\"\n", encoding="utf-8")
        monkeypatch.setenv("APPDATA", str(appdata))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        paths = utils._wechat_config_paths()
        assert os.path.normpath("E:\\WeChatData") in paths

    def test_mydocument_token(self, tmp_path, monkeypatch):
        appdata = tmp_path / "Roaming"
        cfg = appdata / "Tencent" / "xwechat" / "config"
        cfg.mkdir(parents=True)
        (cfg / "b.ini").write_text("SavePath=MyDocument:", encoding="utf-8")
        monkeypatch.setenv("APPDATA", str(appdata))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "Home"))
        paths = utils._wechat_config_paths()
        assert os.path.join(str(tmp_path / "Home"), "Documents") in paths

    def test_gbk_encoded_ini(self, tmp_path, monkeypatch):
        appdata = tmp_path / "Roaming"
        cfg = appdata / "Tencent" / "xwechat" / "config"
        cfg.mkdir(parents=True)
        (cfg / "c.ini").write_bytes("路径=F:\\微信文件".encode("gbk"))
        monkeypatch.setenv("APPDATA", str(appdata))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        paths = utils._wechat_config_paths()
        assert os.path.normpath("F:\\微信文件") in paths

    def test_binary_dat_ignored(self, tmp_path, monkeypatch):
        appdata = tmp_path / "Roaming"
        cfg = appdata / "Tencent" / "xwechat" / "config"
        cfg.mkdir(parents=True)
        (cfg / "hardlink.dat").write_bytes(bytes(range(32)))
        monkeypatch.setenv("APPDATA", str(appdata))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        assert utils._wechat_config_paths() == []

    def test_normalize_save_path(self, monkeypatch, tmp_path):
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        assert utils._normalize_save_path("MyDocument:") == os.path.join(str(tmp_path), "Documents")
        assert utils._normalize_save_path("D:\\keep") == "D:\\keep"


class TestHint:
    def test_long_hint_mentions_steps(self):
        text = utils.data_dir_hint()
        assert "db_storage" in text
        assert "--db-dir" in text

    def test_short_hint_mentions_ui(self):
        text = utils.data_dir_hint(short=True)
        assert "深度搜索" in text
        assert "使用该目录" in text


class TestFastRoots:
    def test_ini_path_becomes_root(self, tmp_path, monkeypatch):
        data_root = tmp_path / "DataRoot"
        _make_db_storage(str(data_root))
        appdata = tmp_path / "Roaming"
        cfg = appdata / "Tencent" / "xwechat" / "config"
        cfg.mkdir(parents=True)
        (cfg / "x.ini").write_text(str(data_root), encoding="utf-8")
        monkeypatch.setenv("APPDATA", str(appdata))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        monkeypatch.setattr(utils, "_registry_paths", lambda: [])
        monkeypatch.setattr(utils, "_logical_drives", lambda: [])
        roots = utils._get_fast_data_roots()
        assert any(os.path.normcase(r) == os.path.normcase(str(data_root)) for r in roots)
        found = utils._scan_roots_for_wechat(roots)
        assert len(found) == 1

    def test_nonexistent_ini_path_uses_parent(self, tmp_path, monkeypatch):
        appdata = tmp_path / "Roaming"
        cfg = appdata / "Tencent" / "xwechat" / "config"
        cfg.mkdir(parents=True)
        parent = tmp_path / "Moved"
        parent.mkdir()
        (cfg / "y.ini").write_text(os.path.join(str(parent), "gone"), encoding="utf-8")
        monkeypatch.setenv("APPDATA", str(appdata))
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
        monkeypatch.setattr(utils, "_registry_paths", lambda: [])
        monkeypatch.setattr(utils, "_logical_drives", lambda: [])
        roots = utils._get_fast_data_roots()
        assert any(os.path.normcase(r) == os.path.normcase(str(parent)) for r in roots)


class TestDeepSearch:
    def test_deep_search_finds_nested_dir(self, tmp_path, monkeypatch):
        drive = tmp_path / "Drive"
        nested = drive / "some" / "deep" / "place"
        nested.mkdir(parents=True)
        storage = _make_db_storage(str(nested))
        monkeypatch.setattr(utils, "_logical_drives", lambda: [str(drive) + os.sep])
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "NoHome"))
        found = utils._deep_find_db_storage(budget_s=20.0, max_depth=5)
        assert [os.path.normcase(p) for p in found] == [os.path.normcase(storage)]

    def test_deep_search_skips_junk_dirs(self, tmp_path, monkeypatch):
        drive = tmp_path / "Drive"
        junk = drive / "Windows" / "xwechat_files" / "fake" / "db_storage"
        junk.mkdir(parents=True)
        monkeypatch.setattr(utils, "_logical_drives", lambda: [str(drive) + os.sep])
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "NoHome"))
        assert utils._deep_find_db_storage(budget_s=20.0, max_depth=5) == []

    def test_fallback_used_when_fast_pass_empty(self, tmp_path, monkeypatch):
        drive = tmp_path / "Drive"
        nested = drive / "apps" / "tencent_files"
        nested.mkdir(parents=True)
        storage = _make_db_storage(str(nested))
        monkeypatch.setattr(utils, "_get_fast_data_roots", lambda: [])
        monkeypatch.setattr(utils, "_logical_drives", lambda: [str(drive) + os.sep])
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "NoHome"))
        found = utils._detect_db_storage(deep=True, use_cache=False, budget_s=20.0, max_depth=5)
        assert [os.path.normcase(p) for p in found] == [os.path.normcase(storage)]

    def test_no_deep_means_no_fallback(self, tmp_path, monkeypatch):
        drive = tmp_path / "Drive"
        nested = drive / "apps" / "tencent_files"
        nested.mkdir(parents=True)
        _make_db_storage(str(nested))
        monkeypatch.setattr(utils, "_get_fast_data_roots", lambda: [])
        monkeypatch.setattr(utils, "_logical_drives", lambda: [str(drive) + os.sep])
        assert utils._detect_db_storage(deep=False, use_cache=False) == []
