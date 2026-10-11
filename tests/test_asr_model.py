"""Tests for ASR model locating / downloading (engine/services/asr_model.py).

使用临时目录与本地 HTTP 服务器模拟模型仓库，不下载真实模型。
"""
import os
import threading

import pytest
from http.server import HTTPServer, SimpleHTTPRequestHandler

from engine.services import asr_model


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """把用户模型目录/内置目录都指到临时目录。"""
    user = tmp_path / "user_models"
    bundled = tmp_path / "bundled_models"
    user.mkdir()
    bundled.mkdir()
    monkeypatch.setattr(asr_model, "user_model_dir", lambda: str(user))
    monkeypatch.setattr(asr_model, "bundled_model_dir", lambda: str(bundled))
    return {"user": user, "bundled": bundled}


@pytest.fixture
def fake_model(monkeypatch):
    spec = {"Fake/whisper-test": {"label": "测试模型", "files": ["config.json",
                                  "onnx/model_quantized.onnx"], "sizeMb": 0.01,
                                  "recommended": True}}
    monkeypatch.setattr(asr_model, "ASR_MODELS", spec)
    return "Fake/whisper-test"


class TestStatus:
    def test_missing_when_empty(self, sandbox, fake_model):
        st = asr_model.model_status(fake_model)
        assert st["complete"] is False
        assert sorted(st["missing"]) == ["config.json", "onnx/model_quantized.onnx"]
        assert st["sizeMb"] == 0.01

    def test_complete_when_files_present(self, sandbox, fake_model):
        d = sandbox["user"] / fake_model / "onnx"
        d.mkdir(parents=True)
        (sandbox["user"] / fake_model / "config.json").write_text("{}")
        (d / "model_quantized.onnx").write_bytes(b"x" * 10)
        st = asr_model.model_status(fake_model)
        assert st["complete"] is True and st["missing"] == []

    def test_unknown_model(self):
        st = asr_model.model_status("Nope/none")
        assert st["complete"] is False and st["error"] == "unknown_model"

    def test_available_lists_models(self, fake_model):
        st = asr_model.model_status(fake_model)
        assert st["available"] and st["available"][0]["model"] == fake_model


class TestResolve:
    def test_user_dir_wins(self, sandbox, fake_model):
        for root in (sandbox["user"], sandbox["bundled"]):
            p = root / fake_model / "config.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(root.name)
        got = asr_model.resolve_model_file(fake_model, "config.json")
        assert got is not None
        assert os.path.normcase(got).startswith(os.path.normcase(str(sandbox["user"])))

    def test_falls_back_to_bundled(self, sandbox, fake_model):
        p = sandbox["bundled"] / fake_model / "config.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("bundled")
        got = asr_model.resolve_model_file(fake_model, "config.json")
        assert got is not None and "bundled" in got

    def test_missing_returns_none(self, sandbox, fake_model):
        assert asr_model.resolve_model_file(fake_model, "nope.json") is None


class TestDownload:
    @pytest.fixture
    def server(self, tmp_path, fake_model):
        root = tmp_path / "repo"
        for rel, body in (("config.json", b"{}"), ("onnx/model_quantized.onnx", b"O" * 5000)):
            p = root / fake_model / "resolve" / "main" / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(body)
        handler = lambda *a, **k: SimpleHTTPRequestHandler(*a, directory=str(root), **k)
        srv = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        yield "http://127.0.0.1:%d" % srv.server_address[1]
        srv.shutdown()

    def test_downloads_missing_files(self, sandbox, fake_model, server, tmp_path):
        msgs = []
        out = asr_model.download_model(fake_model, base_url=server,
                                       progress_fn=lambda m, p=0: msgs.append(m),
                                       dest_root=str(tmp_path / "dl"))
        assert out["downloaded"] == 2 and out["skipped"] == 0
        target = tmp_path / "dl" / fake_model / "onnx" / "model_quantized.onnx"
        assert target.is_file() and target.stat().st_size == 5000
        assert any("config.json" in m for m in msgs)

    def test_second_run_skips_existing(self, sandbox, fake_model, server, tmp_path):
        dest = str(tmp_path / "dl")
        asr_model.download_model(fake_model, base_url=server, dest_root=dest)
        out = asr_model.download_model(fake_model, base_url=server, dest_root=dest)
        assert out["skipped"] == 2 and out["downloaded"] == 0

    def test_unknown_model_raises(self, sandbox):
        with pytest.raises(ValueError):
            asr_model.download_model("Nope/none", dest_root=".")
