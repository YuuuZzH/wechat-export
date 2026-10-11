# -*- coding: utf-8 -*-
"""语音导出的音频层：编码（WAV / MP3 / M4A）、重采样、拼接、能力探测。

依赖策略（见设计文档 §2/§10）：

* **WAV** —— 纯 Python，零依赖，无损；
* **MP3** —— `lameenc`（自带 LAME 的 wheel，约 157 KB），**不需要 ffmpeg**；
* **M4A** —— 只能靠 ffmpeg，缺失时**明确报错**，绝不静默换格式。

本模块不依赖 Flask，也不改动微信原始库。
"""

import os
import subprocess
import tempfile


def _import_lameenc():
    """返回 lameenc 模块；不可用时返回 None（便于测试 monkeypatch）。"""
    try:
        import lameenc
        return lameenc
    except ImportError:
        return None


def _find_ffmpeg():
    """复用 media 的探测逻辑（PATH → tools → 程序目录 → %LOCALAPPDATA%）。"""
    from engine.services.media import _find_ffmpeg as _f
    return _f()


def pcm_duration_s(pcm: bytes, sample_rate: int) -> float:
    """时长一律以 PCM 长度为准（16-bit 单声道）——XML 的 voicelength 只作兜底。"""
    if not pcm or not sample_rate:
        return 0.0
    return len(pcm) / 2.0 / float(sample_rate)


def encode_wav(pcm: bytes, out_path: str, sample_rate: int) -> str:
    """写 16-bit 单声道 WAV（复用 media._write_wav，保证与播放路径一致）。"""
    from engine.services.media import _write_wav
    _write_wav(out_path, pcm, sample_rate)
    return out_path


def encode_mp3(pcm: bytes, out_path: str, sample_rate: int, bitrate: int = 64,
               quality: int = 5):
    """MP3（lameenc）。lameenc 不可用时返回 None，由调用方决定降级。

    ``quality``：LAME 的 0（最好/最慢）~ 9（最快）。语音场景默认 5；
    想更快可以给 7~9（体积与听感差异很小，见 CLI 的 ``--mp3-quality``）。
    """
    lameenc = _import_lameenc()
    if lameenc is None or not pcm:
        return None
    enc = lameenc.Encoder()
    enc.set_bit_rate(bitrate)
    enc.set_in_sample_rate(sample_rate)
    enc.set_channels(1)
    enc.set_quality(int(quality))
    data = enc.encode(pcm) + enc.flush()
    with open(out_path, 'wb') as f:
        f.write(data)
    return out_path


def encode_m4a(pcm: bytes, out_path: str, sample_rate: int, bitrate: str = '64k'):
    """M4A/AAC（需要 ffmpeg）。没有 ffmpeg 时抛 RuntimeError。

    用临时 PCM 文件而不是管道喂 ffmpeg：受限沙箱下管道 stdio 不可用，
    而且临时文件更便于排错。
    """
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError('未检测到 ffmpeg：M4A 需要 ffmpeg（可点「一键安装 ffmpeg」，'
                           '或手动把 ffmpeg.exe 放进 tools/ 后重试；也可改用 mp3 / wav）')
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.pcm', delete=False) as tf:
            tf.write(pcm)
            tmp = tf.name
        cmd = [ffmpeg, '-y', '-v', 'error', '-f', 's16le', '-ar', str(sample_rate),
               '-ac', '1', '-i', tmp, '-c:a', 'aac', '-b:a', bitrate, out_path]
        result = subprocess.run(cmd, capture_output=True, timeout=300)
        if result.returncode != 0 or not os.path.isfile(out_path):
            raise RuntimeError('ffmpeg 转换失败: %s'
                               % result.stderr.decode('utf-8', 'replace')[:200])
        return out_path
    finally:
        if tmp and os.path.isfile(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def capabilities() -> dict:
    """界面据此决定 M4A 是否可选、是否提示安装 ffmpeg / 降级 WAV。"""
    ffmpeg = _find_ffmpeg() or ''
    return {'wav': True,
            'mp3': _import_lameenc() is not None,
            'm4a': bool(ffmpeg),
            'ffmpeg': ffmpeg}


def silence_pcm(seconds: float, sample_rate: int) -> bytes:
    """生成静音 PCM（16-bit 单声道）。"""
    return b'\x00\x00' * int(round(seconds * sample_rate))


def resample_pcm(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """线性插值重采样（16-bit 单声道）。

    * 采样率相同 ⇒ **原样返回**（绝大多数微信语音都是 24k，这条快路径保证批量导出不会变慢）；
    * 只有混合采样率时才走插值（语音场景精度足够，且避免引入 ffmpeg 依赖）。
    """
    if not pcm or src_rate == dst_rate:
        return pcm
    import array
    src = array.array('h')
    src.frombytes(pcm[:len(pcm) // 2 * 2])
    n_src = len(src)
    if n_src == 0:
        return b''
    n_dst = int(n_src * dst_rate / float(src_rate))
    if n_dst <= 0:
        return b''
    out = array.array('h', [0]) * n_dst
    ratio = (n_src - 1) / float(n_dst - 1) if n_dst > 1 else 0.0
    for i in range(n_dst):
        pos = i * ratio
        i0 = int(pos)
        i1 = i0 + 1 if i0 + 1 < n_src else n_src - 1
        frac = pos - i0
        out[i] = int(src[i0] + (src[i1] - src[i0]) * frac)
    return out.tobytes()


def concat_pcm(segments, gap_s: float, dst_rate: int) -> bytes:
    """把 ``[(pcm, rate), ...]`` 依次拼接：段间插 ``gap_s`` 秒静音，统一到 ``dst_rate``。

    注意：这是**内存内**拼接，适合单个分组（一个人/一天）；超大分组由 layout 层
    改为流式写出（见 ``layout.write_merged`` 的 ``max_bytes`` 保护）。
    """
    parts = []
    for idx, (pcm, rate) in enumerate(segments):
        if idx:
            parts.append(silence_pcm(gap_s, dst_rate))
        parts.append(resample_pcm(pcm, rate, dst_rate))
    return b''.join(parts)
