"""商业语音识别（ASR）接入 —— 目前支持百度短语音识别。

为什么需要它：本地 Whisper 模型（tiny/base）中文准确度有限，长语音还容易漏内容。
用户可以在「设置」页填入自己的百度语音识别 Key，把转文字切到商业服务。

百度流程（官方 REST API）：
  1) POST https://aip.baidubce.com/oauth/2.0/token  (client_id/client_secret) → access_token
  2) POST https://vop.baidu.com/server_api  JSON{format,rate,channel,cuid,token,speech(base64),len}
限制：单次音频 ≤ 60 秒、格式 wav/pcm、采样率 8000 或 16000、单声道。
因此本模块会把音频重采样到 16k 并按 55 秒切片后依次识别。
"""
import base64
import json
import os
import struct
import time

import requests

BAIDU_TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
BAIDU_ASR_URL = "https://vop.baidu.com/server_api"

# dev_pid: 1537 普通话(纯中文) / 1737 英语 / 1637 粤语 / 1837 四川话 / 1536 普通话(带标点)
BAIDU_DEV_PIDS = [
    {"value": 1537, "label": "普通话（纯中文，推荐）"},
    {"value": 1536, "label": "普通话（带标点）"},
    {"value": 1737, "label": "英语"},
    {"value": 1637, "label": "粤语"},
    {"value": 1837, "label": "四川话"},
]

_TOKEN_CACHE = {"token": "", "expire_at": 0.0, "key": ""}


class AsrError(RuntimeError):
    """商业 ASR 调用失败（带用户可读信息）。"""


# 百度常见错误码 → 给用户看得懂的提示（附带原始错误信息）
BAIDU_ERROR_HINTS = {
    3300: "输入参数不正确（检查识别模型 dev_pid 与音频格式）",
    3301: "音频质量太差，无法识别",
    3302: "鉴权失败：API Key / Secret Key 不正确，或应用未开通「短语音识别」",
    3303: "百度服务端错误，稍后重试",
    3304: "当天请求量已达上限（免费额度用完或 QPS 超限）",
    3305: "音频过长：单次不能超过 60 秒（程序会自动切片）",
    3307: "音频数据为空或读取失败",
    3308: "音频格式不支持（需 16k/8k、单声道、16bit PCM 或 WAV）",
    3309: "音频采样率与参数不一致",
    3310: "音频格式或采样率不支持",
    3311: "采样率参数不合法（本程序固定 16000；出现请联系开发者）",
    3312: "音频长度超出限制（需 ≤ 60 秒）",
    3313: "音频解码失败",
}


def baidu_error_message(err_no, err_msg) -> str:
    """把百度错误码翻译成用户可读信息。"""
    hint = BAIDU_ERROR_HINTS.get(err_no)
    base = "百度返回错误 %s: %s" % (err_no, err_msg)
    return base + ("（%s）" % hint if hint else "")


def to_simplified(text: str) -> str:
    """繁体 → 简体（Whisper 中文输出常为繁体）。失败时原样返回。"""
    if not text:
        return text
    try:
        import warnings
        with warnings.catch_warnings():
            # zhconv 会触发 pkg_resources 弃用告警，屏蔽掉以免污染日志
            warnings.simplefilter("ignore")
            import zhconv
        return zhconv.convert(text, "zh-cn")
    except Exception:
        return text


def get_access_token(api_key: str, secret_key: str, force: bool = False) -> str:
    """获取百度 access_token（带缓存，有效期约 30 天）。"""
    if not api_key or not secret_key:
        raise AsrError("未配置百度 API Key / Secret Key，请到「设置」页填写")
    cache_key = api_key + ":" + secret_key
    now = time.time()
    if (not force and _TOKEN_CACHE["token"] and _TOKEN_CACHE["key"] == cache_key
            and now < _TOKEN_CACHE["expire_at"]):
        return _TOKEN_CACHE["token"]
    try:
        resp = requests.post(BAIDU_TOKEN_URL, params={
            "grant_type": "client_credentials",
            "client_id": api_key,
            "client_secret": secret_key,
        }, timeout=20)
        data = resp.json()
    except Exception as e:
        raise AsrError("连接百度服务器失败: %s" % e)
    token = data.get("access_token")
    if not token:
        raise AsrError("获取 token 失败: %s" % (data.get("error_description") or data))
    _TOKEN_CACHE.update({"token": token, "key": cache_key,
                         "expire_at": now + int(data.get("expires_in") or 2592000) - 600})
    return token


def test_credentials(api_key: str, secret_key: str) -> dict:
    """校验百度凭据是否可用。"""
    try:
        token = get_access_token(api_key, secret_key, force=True)
        return {"ok": True, "message": "凭据有效（已获取 access_token，长度 %d）" % len(token)}
    except AsrError as e:
        return {"ok": False, "message": str(e)}


def _read_wav(path: str):
    """读取 WAV（返回 采样率, 声道数, 位深, PCM bytes）。"""
    with open(path, "rb") as f:
        raw = f.read()
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise AsrError("不是 WAV 文件: " + os.path.basename(path))
    pos = 12
    fmt = None
    data = None
    while pos + 8 <= len(raw):
        chunk_id = raw[pos:pos + 4]
        size = struct.unpack_from("<I", raw, pos + 4)[0]
        body = raw[pos + 8:pos + 8 + size]
        if chunk_id == b"fmt ":
            fmt = body
        elif chunk_id == b"data":
            data = body
        pos += 8 + size + (size & 1)
    if fmt is None or data is None:
        raise AsrError("WAV 缺少 fmt/data 块")
    channels, rate = struct.unpack_from("<HI", fmt, 2)
    bits = struct.unpack_from("<H", fmt, 14)[0]
    return rate, channels, bits, data


def _resample_mono16(pcm: bytes, rate: int, channels: int, target_rate: int = 16000):
    """转成 16k 单声道 16bit PCM（线性插值，纯 Python，避免 audioop 依赖）。"""
    import array
    samples = array.array("h")
    samples.frombytes(pcm[:len(pcm) - (len(pcm) % 2)])
    if channels > 1:
        mono = array.array("h", [0] * (len(samples) // channels))
        for i in range(len(mono)):
            total = 0
            for c in range(channels):
                total += samples[i * channels + c]
            mono[i] = int(total / channels)
        samples = mono
    if rate == target_rate:
        return samples.tobytes()
    ratio = target_rate / float(rate)
    out_len = int(len(samples) * ratio)
    out = array.array("h", [0] * out_len)
    for i in range(out_len):
        src = i / ratio
        i0 = int(src)
        i1 = min(i0 + 1, len(samples) - 1)
        frac = src - i0
        out[i] = int(samples[i0] * (1 - frac) + samples[i1] * frac)
    return out.tobytes()


def recognize_wav(path: str, api_key: str, secret_key: str, dev_pid: int = 1537,
                  max_chunk_sec: int = 55) -> dict:
    """识别一个 WAV 文件（自动重采样/切片）。Returns {text, chunks, engine}。"""
    token = get_access_token(api_key, secret_key)
    rate, channels, bits, pcm = _read_wav(path)
    if bits != 16:
        raise AsrError("只支持 16bit WAV（当前 %d bit）" % bits)
    pcm16 = _resample_mono16(pcm, rate, channels, 16000)
    bytes_per_sec = 16000 * 2
    chunk_bytes = max_chunk_sec * bytes_per_sec
    texts = []
    chunk_count = 0
    for start in range(0, max(len(pcm16), 1), chunk_bytes):
        piece = pcm16[start:start + chunk_bytes]
        if len(piece) < bytes_per_sec // 4:      # < 0.25s 忽略
            continue
        chunk_count += 1
        payload = {
            "format": "pcm",
            "rate": 16000,
            "channel": 1,
            "cuid": "wechat_exp",
            "token": token,
            "dev_pid": int(dev_pid),
            "speech": base64.b64encode(piece).decode("ascii"),
            "len": len(piece),
        }
        try:
            # 注意：cuid/token 必须放在 JSON body 里。放进 URL 查询参数时百度的
            # 新版接口会按老接口解析，直接报 “3311 param rate invalid.”（实测确认）
            resp = requests.post(BAIDU_ASR_URL,
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"},
                                 timeout=60)
            data = resp.json()
        except Exception as e:
            raise AsrError("调用百度语音识别失败: %s" % e)
        if data.get("err_no") not in (0, None):
            raise AsrError(baidu_error_message(data.get("err_no"), data.get("err_msg")))
        for line in (data.get("result") or []):
            texts.append(line.strip())
    return {"text": to_simplified("".join(texts)), "chunks": chunk_count, "engine": "baidu"}
