"""回归测试：/api/asr/commercial 的参数合并。

背景（用户反馈）：一条从未播放过的语音，直接点「转文字」会提示
「找不到这条语音的音频文件」；先点播放听一次，再点转文字就正常。

根因：前端固定传 create_time=0 / local_id=0，路由把这些**无效值**也当作覆盖值，
把 voice_url 里的真实 create_time/local_id 冲成 0（随后被当成 None），
于是未生成缓存的语音无法从 VoiceInfo 里提取出来。播放一次会生成 WAV 缓存，
所以「先播放」就又能转写了。
"""
import pytest

from engine.services import media
from web.app import create_app

HEX_PATH = ("7f0c00080220d46d1929d754046c7bfda44878048102de24ee6acef8a7a8882ab65"
            "6bf2aed7c0301090f0001809101000401050300000006cfb8b80107b799a7c0")
VOICE_URL = ("/api/voice?path=" + HEX_PATH +
             "&create_time=1789531971&local_id=1585&chat=wxid_example12345")


@pytest.fixture
def client(tmp_path):
    d = tmp_path / "decrypted"
    (d / "message").mkdir(parents=True)
    app = create_app(str(d), wxid="wxid_example12345")
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def captured(monkeypatch):
    """拦下 WAV 定位，只记录路由传下来的参数。"""
    seen = {}

    def fake_get_voice_wav_path(decrypted_dir, voice_path=None, create_time=None,
                                local_id=None, db_dir=None, chat=None):
        seen.update(voice_path=voice_path, create_time=create_time,
                    local_id=local_id, chat=chat)
        return None

    monkeypatch.setattr(media, "get_voice_wav_path", fake_get_voice_wav_path)
    return seen


class TestCommercialAsrParams:
    def test_zero_ids_do_not_override_url(self, client, captured):
        """前端旧写法（body 里带 0）不能覆盖 URL 里的真实值。"""
        client.post("/api/asr/commercial",
                    json={"voice_url": VOICE_URL, "create_time": 0, "local_id": 0})
        assert captured["create_time"] == 1789531971
        assert captured["local_id"] == 1585
        assert captured["chat"] == "wxid_example12345"
        assert captured["voice_path"] == HEX_PATH

    def test_missing_ids_use_url_values(self, client, captured):
        client.post("/api/asr/commercial", json={"voice_url": VOICE_URL})
        assert captured["create_time"] == 1789531971
        assert captured["local_id"] == 1585

    def test_explicit_body_ids_still_win(self, client, captured):
        """body 里给了有效值仍然优先（保持原语义）。"""
        client.post("/api/asr/commercial",
                    json={"voice_url": VOICE_URL, "create_time": 1700000000, "local_id": 42})
        assert captured["create_time"] == 1700000000
        assert captured["local_id"] == 42

    def test_empty_string_ids_do_not_override(self, client, captured):
        client.post("/api/asr/commercial",
                    json={"voice_url": VOICE_URL, "create_time": "", "local_id": ""})
        assert captured["create_time"] == 1789531971
        assert captured["local_id"] == 1585

    def test_not_found_reports_404(self, client, captured):
        r = client.post("/api/asr/commercial", json={"voice_url": VOICE_URL})
        assert r.status_code == 404
        assert r.get_json()["error"] == "voice_not_found"
