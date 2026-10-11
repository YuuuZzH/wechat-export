"""Tests for ASR settings + commercial (Baidu) ASR client.

不联网：requests 全部被 monkeypatch；WAV 用标准库合成。
"""
import json
import os
import struct
import wave

import pytest

from engine import config_file
from engine.services import asr_commercial as ac


def _make_wav(path, seconds=1.0, rate=24000, channels=1, freq=440):
    import math
    n = int(rate * seconds)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(n):
            v = int(12000 * math.sin(2 * math.pi * freq * i / rate))
            for _c in range(channels):
                frames += struct.pack("<h", v)
        w.writeframes(bytes(frames))
    return str(path)


class TestSimplify:
    def test_traditional_to_simplified(self):
        assert ac.to_simplified("這個東西是簡體的嗎") == "这个东西是简体的吗"

    def test_empty(self):
        assert ac.to_simplified("") == ""


class TestAudio:
    def test_read_wav(self, tmp_path):
        p = _make_wav(tmp_path / "a.wav", seconds=0.5, rate=16000, channels=1)
        rate, channels, bits, data = ac._read_wav(p)
        assert (rate, channels, bits) == (16000, 1, 16)
        assert len(data) == int(16000 * 0.5) * 2

    def test_read_wav_rejects_non_wav(self, tmp_path):
        p = tmp_path / "x.bin"
        p.write_bytes(b"not a wav")
        with pytest.raises(ac.AsrError):
            ac._read_wav(str(p))

    def test_resample_to_16k_mono(self):
        import array
        pcm = array.array("h", [100] * 24000).tobytes()          # 1 秒 24k 单声道
        out = ac._resample_mono16(pcm, 24000, 1, 16000)
        assert abs(len(out) - 16000 * 2) < 64

    def test_resample_downmixes_stereo(self):
        import array
        left = array.array("h", [1000] * 16000)
        right = array.array("h", [3000] * 16000)
        inter = array.array("h")
        for i in range(16000):
            inter.append(left[i])
            inter.append(right[i])
        out = ac._resample_mono16(inter.tobytes(), 16000, 2, 16000)
        vals = array.array("h")
        vals.frombytes(out)
        assert abs(vals[100] - 2000) <= 1


class TestRecognize:
    @pytest.fixture
    def fake_http(self, monkeypatch):
        calls = {"token": 0, "asr": 0, "last_payload": None}

        class Resp:
            def __init__(self, payload):
                self._payload = payload
            def json(self):
                return self._payload

        def fake_post(url, **kwargs):
            if "oauth" in url:
                calls["token"] += 1
                return Resp({"access_token": "T" * 24, "expires_in": 2592000})
            calls["asr"] += 1
            calls["last_payload"] = json.loads(kwargs.get("data", b"{}").decode("utf-8"))
            # cuid/token 必须放在 JSON body：放到 URL 查询参数时百度会报
            # "3311 param rate invalid."（真实踩坑，这里固定住该行为）
            assert not kwargs.get("params"), "ASR 请求不应带 URL 查询参数"
            return Resp({"err_no": 0, "result": ["識別結果一。", "識別結果二。"], "sn": "1"})

        monkeypatch.setattr(ac.requests, "post", fake_post)
        ac._TOKEN_CACHE.update({"token": "", "key": "", "expire_at": 0})
        return calls

    def test_recognize_short_audio(self, tmp_path, fake_http):
        p = _make_wav(tmp_path / "s.wav", seconds=1.0)
        out = ac.recognize_wav(p, "key", "secret")
        assert out["chunks"] == 1
        assert out["text"] == "识别结果一。识别结果二。"      # 自动转简体
        assert fake_http["asr"] == 1
        payload = fake_http["last_payload"]
        assert payload["rate"] == 16000 and payload["channel"] == 1 and payload["format"] == "pcm"
        import base64 as _b64
        assert payload["dev_pid"] == 1537
        assert payload["len"] == len(_b64.b64decode(payload["speech"])) == 16000 * 1 * 2

    def test_long_audio_is_chunked(self, tmp_path, fake_http):
        p = _make_wav(tmp_path / "long.wav", seconds=120.0)
        out = ac.recognize_wav(p, "key", "secret")
        assert out["chunks"] == 3          # 55s + 55s + 10s
        assert fake_http["asr"] == 3

    def test_missing_keys_raise(self, tmp_path):
        p = _make_wav(tmp_path / "s.wav", seconds=0.5)
        with pytest.raises(ac.AsrError):
            ac.recognize_wav(p, "", "")

    def test_baidu_error_is_reported(self, tmp_path, monkeypatch):
        class Resp:
            def json(self):
                return {"err_no": 3301, "err_msg": "audio quality error"}

        def fake_post(url, **kwargs):
            if "oauth" in url:
                return type("R", (), {"json": lambda self: {"access_token": "T" * 24}})()
            return Resp()

        monkeypatch.setattr(ac.requests, "post", fake_post)
        ac._TOKEN_CACHE.update({"token": "", "key": "", "expire_at": 0})
        p = _make_wav(tmp_path / "s.wav", seconds=0.5)
        with pytest.raises(ac.AsrError) as ei:
            ac.recognize_wav(p, "key", "secret")
        assert "3301" in str(ei.value)
        assert "音频质量" in str(ei.value)          # 错误码已翻译成中文提示

    def test_error_hints(self):
        assert "采样率" in ac.baidu_error_message(3311, "param rate invalid.")
        assert "鉴权" in ac.baidu_error_message(3302, "auth")
        assert ac.baidu_error_message(9999, "unknown").endswith("unknown")


class TestSettings:
    def test_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_file, "_config_path", lambda: str(tmp_path / "c.json"))
        st = config_file.get_asr_settings()
        assert st["engine"] == "local"
        assert st["language"] == "zh" and st["simplify"] is True
        assert st["baidu_dev_pid"] == 1537

    def test_save_and_reload(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_file, "_config_path", lambda: str(tmp_path / "c.json"))
        config_file.set_asr_settings({"engine": "baidu", "baidu_api_key": "AK", "baidu_secret_key": "SK"})
        st = config_file.get_asr_settings()
        assert st["engine"] == "baidu" and st["baidu_api_key"] == "AK"

    def test_unknown_fields_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config_file, "_config_path", lambda: str(tmp_path / "c.json"))
        config_file.set_asr_settings({"engine": "local", "hack": "x"})
        assert "hack" not in config_file.get_asr_settings()
