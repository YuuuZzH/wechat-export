"""ISAAC-64 keystream + XOR decrypt for WeChat Moments (SNS) CDN media.

Algorithm details match public community reverse engineering of WeChat 4.x SNS media:
- Seed = uint64 media key from XML
- Keystream emitted as big-endian uint64 words
- Images: XOR entire file
- Videos: XOR only the first 128 KiB
"""
from __future__ import annotations

MASK = (1 << 64) - 1
GOLDEN = 0x9E3779B97F4A7C15
VIDEO_XOR_BYTES = 131072  # 128 KiB


class Isaac64:
    """Minimal ISAAC-64 PRNG used by WeChat SNS media encryption."""

    def __init__(self, seed: int):
        self.mm = [0] * 256
        self.randrsl = [0] * 256
        self.aa = 0
        self.bb = 0
        self.cc = 0
        self.randcnt = 256
        self.randrsl[0] = seed & MASK
        self._init()

    def _mix(self, a, b, c, d, e, f, g, h):
        a = (a - e) & MASK
        f ^= (h >> 9)
        h = (h + a) & MASK
        b = (b - f) & MASK
        g ^= (a << 9) & MASK
        a = (a + b) & MASK
        c = (c - g) & MASK
        h ^= (b >> 23)
        b = (b + c) & MASK
        d = (d - h) & MASK
        a ^= (c << 15) & MASK
        c = (c + d) & MASK
        e = (e - a) & MASK
        b ^= (d >> 14)
        d = (d + e) & MASK
        f = (f - b) & MASK
        c ^= (e << 20) & MASK
        e = (e + f) & MASK
        g = (g - c) & MASK
        d ^= (f >> 17)
        f = (f + g) & MASK
        h = (h - d) & MASK
        e ^= (g << 14) & MASK
        g = (g + h) & MASK
        return a, b, c, d, e, f, g, h

    def _init(self):
        a = b = c = d = e = f = g = h = GOLDEN
        for _ in range(4):
            a, b, c, d, e, f, g, h = self._mix(a, b, c, d, e, f, g, h)

        for pass_no in range(2):
            for i in range(0, 256, 8):
                if pass_no == 0:
                    a = (a + self.randrsl[i]) & MASK
                    b = (b + self.randrsl[i + 1]) & MASK
                    c = (c + self.randrsl[i + 2]) & MASK
                    d = (d + self.randrsl[i + 3]) & MASK
                    e = (e + self.randrsl[i + 4]) & MASK
                    f = (f + self.randrsl[i + 5]) & MASK
                    g = (g + self.randrsl[i + 6]) & MASK
                    h = (h + self.randrsl[i + 7]) & MASK
                else:
                    a = (a + self.mm[i]) & MASK
                    b = (b + self.mm[i + 1]) & MASK
                    c = (c + self.mm[i + 2]) & MASK
                    d = (d + self.mm[i + 3]) & MASK
                    e = (e + self.mm[i + 4]) & MASK
                    f = (f + self.mm[i + 5]) & MASK
                    g = (g + self.mm[i + 6]) & MASK
                    h = (h + self.mm[i + 7]) & MASK
                a, b, c, d, e, f, g, h = self._mix(a, b, c, d, e, f, g, h)
                self.mm[i] = a
                self.mm[i + 1] = b
                self.mm[i + 2] = c
                self.mm[i + 3] = d
                self.mm[i + 4] = e
                self.mm[i + 5] = f
                self.mm[i + 6] = g
                self.mm[i + 7] = h

        self._isaac64()
        self.randcnt = 256

    def _isaac64(self):
        self.cc = (self.cc + 1) & MASK
        self.bb = (self.bb + self.cc) & MASK
        for i in range(256):
            x = self.mm[i]
            aa = self.aa
            sw = i & 3
            if sw == 0:
                aa ^= (~((aa << 21) & MASK)) & MASK
            elif sw == 1:
                aa ^= (aa >> 5)
            elif sw == 2:
                aa ^= (aa << 12) & MASK
            else:
                aa ^= (aa >> 33)
            aa = (self.mm[(i + 128) & 255] + aa) & MASK
            self.aa = aa
            y = (self.mm[int(x >> 3) & 255] + aa + self.bb) & MASK
            self.mm[i] = y
            self.bb = (self.mm[int(y >> 11) & 255] + x) & MASK
            self.randrsl[i] = self.bb

    def next_u64(self) -> int:
        if self.randcnt == 0:
            self._isaac64()
            self.randcnt = 256
        self.randcnt -= 1
        # WeChat consumes randrsl in reverse order
        return self.randrsl[self.randcnt]

    def keystream_be(self, size: int) -> bytes:
        out = bytearray(size)
        full = size // 8
        for i in range(full):
            out[i * 8:(i + 1) * 8] = self.next_u64().to_bytes(8, 'big')
        rem = size % 8
        if rem:
            chunk = self.next_u64().to_bytes(8, 'big')
            out[full * 8:] = chunk[:rem]
        return bytes(out)


def parse_seed(key) -> int | None:
    if key is None:
        return None
    s = str(key).strip()
    if not s:
        return None
    try:
        return int(s) & MASK
    except ValueError:
        try:
            return int(s, 16) & MASK
        except ValueError:
            return None


def xor_decrypt(data: bytes, key, *, video: bool = False) -> bytes:
    """XOR-decrypt SNS media with ISAAC-64 keystream."""
    seed = parse_seed(key)
    if seed is None or not data:
        return data
    n = min(len(data), VIDEO_XOR_BYTES) if video else len(data)
    ks = Isaac64(seed).keystream_be(n)
    buf = bytearray(data)
    for i in range(n):
        buf[i] ^= ks[i]
    return bytes(buf)


def looks_like_image(data: bytes) -> bool:
    if len(data) < 4:
        return False
    if data[:3] == b'\xff\xd8\xff':
        return True
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return True
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return True
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return True
    return False


def looks_like_video(data: bytes) -> bool:
    return len(data) >= 8 and data[4:8] == b'ftyp'
