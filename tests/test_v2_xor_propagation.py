"""issue #16 症状 2：V2 `.dat` 的**尾部 XOR** 必须来自"派生出来的真值"，不许写死 0xC9。

用户描述（GitHub issue #16）：
    手工把数据目录名改对之后图片能读了，**但"图片只有上面很小一部分能显示，
    剩下部分无法解析"**。

机制：V2 文件布局是
    [15B 头][AES-ECB 段][中间明文][XOR 尾部]
XOR 只作用在**尾部**。用错 XOR ⇒ 头/中间都正常、尾部是纯色或垃圾
⇒ 正是"上面一小部分能显示、剩下无法解析"。所以 XOR 不是可有可无的默认值，
它必须按账号（`code & 0xFF`）派生并一路传到解密调用。

本文件**全部合成**：自造 V2 `.dat`、自造 hardlink.db、自造 MMKV 派生结果，
不读、不写任何真实微信数据（`_FALLBACK_STORAGE_ROOTS` 被置空，绝不探测本机真实根目录）。
"""
import json
import os
import sqlite3
import struct

import pytest

from engine.services import media
from engine.services import v2_key_extract as v2

# 一个"非 0xC9"的账号派生值。写死 0xC9 的实现必然在这里露馅。
DERIVED_XOR = 0x3C
# 让 code & 0xFF == DERIVED_XOR 的 MMKV statistic code
CODE_FOR_DERIVED = 0x13C

AES_KEY = b'0123456789abcdef'
MD5 = 'a' * 32


@pytest.fixture(autouse=True)
def _no_real_wechat_roots(monkeypatch):
    """任何一次解析都不许去探测本机真实的 D:\\xwechat_files 等硬编码根。"""
    monkeypatch.setattr(media, '_FALLBACK_STORAGE_ROOTS', (), raising=False)
    # 内存缓存跨用例会串味，逐用例清空
    monkeypatch.setattr(media, '_IMAGE_KEY_MAP', {}, raising=False)
    monkeypatch.setattr(media, '_IMAGE_KEY_MAP_DIR', None, raising=False)
    monkeypatch.setattr(media, '_ACCOUNT_KEYS_CACHE', {}, raising=False)


@pytest.fixture(autouse=True)
def _no_stale_derived_xor_registry(monkeypatch):
    """派生 XOR 的进程内登记表必须逐用例清空，否则用例之间互相污染。"""
    monkeypatch.setattr(v2, '_DERIVED_XOR_BY_DIR', {}, raising=False)


# ---------------------------------------------------------------------------
# 合成 V2 .dat
# ---------------------------------------------------------------------------

def build_v2_dat(path, plaintext, aes_key, xor_key, aes_size=200, xor_size=40,
                 claimed_xor_size=None):
    """按真实 V2 布局造一个 `.dat`：

        [15B 头: 6B 签名 + <I aes_size + <I xor_size + 1B 填充]
        [AES-128-ECB(PKCS7 填充后的前 aes_size 字节明文)]  长度 = aes_size + (16 - aes_size%16)
        [中间明文（不加密）]
        [XOR 尾部: 最后 xor_size 字节逐字节异或]

    注意 `aes_size % 16 != 0`（200 ⇒ 填充 8 字节），这与 ``_decrypt_dat_v2`` 里
    ``aes_size + 16 - (aes_size % 16)`` 的算法一致。

    * ``xor_size = len(plaintext) - aes_size`` ⇒ **没有**中间段，尾部就是整段剩余
      （真实 V2 的典型形态：AES 段中位只占文件 ~1.5%）。
    * ``claimed_xor_size`` 只改**头里写的**长度，用来造"声称长度 > 实际剩余"。
    """
    from Crypto.Cipher import AES
    from Crypto.Util import Padding

    assert aes_size % 16 != 0, '否则 padded_aes_size 的算法会多算一整块'
    assert len(plaintext) >= aes_size + xor_size

    body = aes_key[:16]      # 16 个 ASCII 字符**直接**当 16 字节密钥用（不是 hex 解码）
    cipher = AES.new(body, AES.MODE_ECB)
    aes_seg = cipher.encrypt(Padding.pad(plaintext[:aes_size], 16))
    assert len(aes_seg) == aes_size + 16 - (aes_size % 16)

    raw_seg = plaintext[aes_size:len(plaintext) - xor_size]
    tail_plain = plaintext[len(plaintext) - xor_size:]
    tail_enc = bytes(b ^ xor_key for b in tail_plain)

    header = (b'\x07\x08V2\x08\x07'
              + struct.pack('<II', aes_size,
                            xor_size if claimed_xor_size is None else claimed_xor_size)
              + b'\x00')
    assert len(header) == 15
    with open(path, 'wb') as f:
        f.write(header + aes_seg + raw_seg + tail_enc)
    return path


def fake_jpeg(size=300):
    """一段"长得像 JPEG"的明文：头 4 字节 + 填充 + EOI 结束符。"""
    return b'\xff\xd8\xff\xe0' + bytes((i * 7) % 251 for i in range(size - 6)) + b'\xff\xd9'


# ---------------------------------------------------------------------------
# A1. `_merge_into_cache` 不许把 XOR 写死
# ---------------------------------------------------------------------------

class TestMergeIntoCacheKeepsTheDerivedXor:
    def _read(self, dec):
        with open(os.path.join(dec, '_media_keys.json'), 'r', encoding='utf-8') as f:
            return json.load(f)

    def test_derived_xor_is_written_instead_of_the_hardcoded_default(self, tmp_path):
        dec = str(tmp_path)
        v2._merge_into_cache(dec, {MD5: AES_KEY}, xor_key=DERIVED_XOR)

        got = self._read(dec)['md5_keys'][MD5]['xor_key']
        assert int(got, 16) == DERIVED_XOR, (
            f'派生出来的 XOR 是 0x{DERIVED_XOR:02X}，缓存里却是 {got} —— '
            '写死 0xC9 会让"非 0xC9 账号"的图片尾部解析成纯色')

    def test_default_is_still_0xc9_when_nothing_was_derived(self, tmp_path):
        """向后兼容：拿不到派生值时行为必须**逐字**和以前一样（本机派生值就是 0xC9）。"""
        dec = str(tmp_path)
        v2._merge_into_cache(dec, {MD5: AES_KEY})
        assert self._read(dec)['md5_keys'][MD5]['xor_key'] == '0xc9'

    def test_a_stale_hardcoded_entry_is_corrected_once_the_truth_is_known(self, tmp_path):
        """旧版本写下的 0xC9 条目会命中 `_load_or_build_image_key_map` 并短路整个推导。

        拿到派生真值之后必须把它改回来，否则"已经缓存过的账号"永远修不好。
        """
        dec = str(tmp_path)
        with open(os.path.join(dec, '_media_keys.json'), 'w', encoding='utf-8') as f:
            json.dump({'md5_keys': {MD5: {'aes_key': AES_KEY.hex(),
                                          'xor_key': '0xc9'}}}, f)

        v2._merge_into_cache(dec, {MD5: AES_KEY}, xor_key=DERIVED_XOR)

        got = self._read(dec)['md5_keys'][MD5]['xor_key']
        assert int(got, 16) == DERIVED_XOR
        assert self._read(dec)['md5_keys'][MD5]['aes_key'] == AES_KEY.hex()


# ---------------------------------------------------------------------------
# A2. `find_keys_for_files` 要把 XOR 带出来（且返回值语义向后兼容）
# ---------------------------------------------------------------------------

class _FakeKernel32:
    def OpenProcess(self, access, inherit, pid):
        return 1

    def CloseHandle(self, handle):
        return True


@pytest.fixture
def fake_memory_scan(tmp_path, monkeypatch):
    """把内存扫描整段换成"必然命中一把钥匙"的假实现（不碰真实进程）。"""
    dat = tmp_path / f'{MD5}.dat'
    with open(dat, 'wb') as f:
        f.write(b'\x07\x08V2\x08\x07' + struct.pack('<II', 200, 40) + b'\x00' + b'\x11' * 16)

    monkeypatch.setattr(v2, '_resolve_hardlink_path', lambda *a, **k: str(dat))
    monkeypatch.setattr(v2, '_get_wechat_pids', lambda: [4242])
    monkeypatch.setattr(v2, 'kernel32', _FakeKernel32())
    monkeypatch.setattr(v2, '_scan_near_v2_headers',
                        lambda h, c, print_fn=None: b'KEYKEYKEYKEYKEYK')
    return str(tmp_path)


class TestFindKeysForFilesCarriesTheXor:
    def test_returned_mapping_still_looks_exactly_like_the_old_dict(self, fake_memory_scan):
        """向后兼容：`found[md5]` 仍是 AES 字节、`in`/`len`/`dict()` 语义不变。"""
        found = v2.find_keys_for_files(fake_memory_scan, 'wxid_demo_1a2b', [MD5])

        assert isinstance(found, dict)
        assert found[MD5] == b'KEYKEYKEYKEYKEYK'
        assert MD5 in found and len(found) == 1
        assert dict(found) == {MD5: b'KEYKEYKEYKEYKEYK'}

    def test_derived_xor_comes_out_along_with_the_aes_key(self, fake_memory_scan):
        found = v2.find_keys_for_files(fake_memory_scan, 'wxid_demo_1a2b', [MD5],
                                       account_xor=DERIVED_XOR)
        assert getattr(found, 'derived_xor', None) == DERIVED_XOR, (
            '调用方拿不到派生 XOR，就只能退回写死的 0xC9 —— 尾部必然错')

    def test_derived_xor_is_also_persisted_for_these_keys(self, fake_memory_scan):
        v2.find_keys_for_files(fake_memory_scan, 'wxid_demo_1a2b', [MD5],
                               account_xor=DERIVED_XOR)
        with open(os.path.join(fake_memory_scan, '_media_keys.json'), encoding='utf-8') as f:
            cached = json.load(f)['md5_keys'][MD5]['xor_key']
        assert int(cached, 16) == DERIVED_XOR

    def test_no_derived_xor_available_yields_none_not_a_guess(self, fake_memory_scan):
        found = v2.find_keys_for_files(fake_memory_scan, 'wxid_demo_1a2b', [MD5])
        assert getattr(found, 'derived_xor', None) is None
        with open(os.path.join(fake_memory_scan, '_media_keys.json'), encoding='utf-8') as f:
            cached = json.load(f)['md5_keys'][MD5]['xor_key']
        assert cached == '0xc9'


# ---------------------------------------------------------------------------
# A2b. MMKV 派生路径（生产主路径）必须把已验证的 XOR 落盘
# ---------------------------------------------------------------------------

class TestMmkvDerivationPersistsTheVerifiedXor:
    def test_verified_code_writes_its_own_xor(self, tmp_path, monkeypatch):
        dec = str(tmp_path)
        monkeypatch.setattr(v2, '_scan_mmkv_kvcomm_dirs', lambda: [str(tmp_path)])
        monkeypatch.setattr(v2, '_parse_mmkv_codes', lambda dirs: [CODE_FOR_DERIVED])
        monkeypatch.setattr(v2, '_load_v2_ciphertexts',
                            lambda d, w: {MD5: ('irrelevant.dat', b'\x11' * 16)})
        monkeypatch.setattr(v2, '_try_key', lambda key, ct: 'jpeg')

        found = v2.extract_keys_from_mmkv(dec, 'wxid_demo_1a2b')
        assert MD5 in found

        with open(os.path.join(dec, '_media_keys.json'), encoding='utf-8') as f:
            cached = json.load(f)['md5_keys'][MD5]['xor_key']
        assert int(cached, 16) == DERIVED_XOR, (
            f'code=0x{CODE_FOR_DERIVED:X} 派生出的 XOR 是 0x{DERIVED_XOR:02X}，'
            f'缓存里却是 {cached}')

    def test_verified_xor_is_remembered_for_later_memory_scans(self, tmp_path, monkeypatch):
        dec = str(tmp_path)
        monkeypatch.setattr(v2, '_scan_mmkv_kvcomm_dirs', lambda: [str(tmp_path)])
        monkeypatch.setattr(v2, '_parse_mmkv_codes', lambda dirs: [CODE_FOR_DERIVED])
        monkeypatch.setattr(v2, '_load_v2_ciphertexts',
                            lambda d, w: {MD5: ('irrelevant.dat', b'\x11' * 16)})
        monkeypatch.setattr(v2, '_try_key', lambda key, ct: 'jpeg')

        v2.extract_keys_from_mmkv(dec, 'wxid_demo_1a2b')
        assert v2.get_derived_xor(dec) == DERIVED_XOR


# ---------------------------------------------------------------------------
# A2c. 收割器（harvest_v2_keys）也是 `_media_keys.json` 的写者，
#      同样不许把派生 XOR 丢掉
# ---------------------------------------------------------------------------

class _FakeWeChatMemory:
    """一块假内存：V2 魔数 + 紧邻的真 AES 密钥（不碰任何真实进程）。"""

    BASE = 0x10000000
    SIZE = 4096
    MAGIC_OFF = 1024
    KEY_OFF = 1008          # 与窗口起点(1024-256=768)的偏移 240 是 4 的倍数

    def __init__(self, key):
        self.data = bytearray(self.SIZE)
        self.data[self.MAGIC_OFF:self.MAGIC_OFF + 6] = b'\x07\x08\x56\x32\x08\x07'
        self.data[self.KEY_OFF:self.KEY_OFF + 16] = key
        self._queries = 0

    def OpenProcess(self, access, inherit, pid):
        return 1

    def CloseHandle(self, handle):
        return True

    def VirtualQueryEx(self, h, addr, mbi_ref, size):
        if self._queries:       # 第二次即宣告"没有更多区域"
            return 0
        self._queries += 1
        mbi = mbi_ref._obj
        mbi.BaseAddress = self.BASE
        mbi.RegionSize = self.SIZE
        mbi.State = v2.MEM_COMMIT
        mbi.Protect = v2.PAGE_READWRITE
        mbi.Type = 0
        return 1

    def ReadProcessMemory(self, h, addr, buf, size, br_ref):
        import ctypes
        base = addr.value if hasattr(addr, 'value') else addr
        off = max(0, min(int(base) - self.BASE, self.SIZE))
        chunk = bytes(self.data[off:off + size])
        ctypes.memmove(buf, chunk, len(chunk))
        br_ref._obj.value = len(chunk)
        return 1


class TestHarvesterPersistsTheDerivedXor:
    def test_harvested_keys_carry_the_account_xor(self, tmp_path, monkeypatch):
        dec = str(tmp_path)
        monkeypatch.setattr(v2, '_load_v2_ciphertexts',
                            lambda d, w: {MD5: ('irrelevant.dat', b'\x11' * 16)})
        monkeypatch.setattr(v2, 'extract_keys_from_mmkv', lambda d, w=None: {})
        monkeypatch.setattr(v2, 'get_derived_xor', lambda d: DERIVED_XOR)
        monkeypatch.setattr(v2, 'is_wechat_running', lambda: True)
        monkeypatch.setattr(v2, '_get_wechat_pids', lambda: [4242])
        monkeypatch.setattr(v2, 'kernel32', _FakeWeChatMemory(AES_KEY))
        monkeypatch.setattr(v2, '_test_key_against_all',
                            lambda key, pending: ((MD5, 'jpeg') if key == AES_KEY
                                                  else (None, None)))

        v2.harvest_v2_keys(dec, 'wxid_demo_1a2b', interval=0.0, max_rounds=1)

        with open(os.path.join(dec, '_media_keys.json'), encoding='utf-8') as f:
            cached = json.load(f)['md5_keys'][MD5]['xor_key']
        assert int(cached, 16) == DERIVED_XOR, (
            f'收割器手里就有派生 XOR 0x{DERIVED_XOR:02X}，却写成了 {cached}')


# ---------------------------------------------------------------------------
# A3. media.py 里"内存找到的密钥"这条路不许再用写死的 XOR
# ---------------------------------------------------------------------------

def _make_storage(tmp_path, account='wxid_real_1a2b'):
    d = tmp_path / 'xwechat_files' / account / 'msg' / 'attach' / 'h1' / 'd1' / 'Img'
    d.mkdir(parents=True, exist_ok=True)
    return tmp_path / 'xwechat_files', d


def _make_decrypted(tmp_path, storage_root, md5, dat_bytes):
    dec = tmp_path / 'decrypted'
    (dec / 'hardlink').mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(dec / 'hardlink' / 'hardlink.db'))
    conn.execute('CREATE TABLE IF NOT EXISTS db_info (Key TEXT, ValueStdStr TEXT)')
    conn.execute('CREATE TABLE IF NOT EXISTS dir2id (name TEXT)')
    conn.execute('CREATE TABLE IF NOT EXISTS image_hardlink_info_v4 '
                 '(md5 TEXT, file_name TEXT, dir1 INTEGER, dir2 INTEGER)')
    conn.execute('INSERT INTO db_info (Key, ValueStdStr) VALUES (?, ?)',
                 ('uuid', '1_' + 'x' * 20 + '_' + str(storage_root)))
    conn.execute('INSERT INTO dir2id (name) VALUES (?)', ('h1',))
    conn.execute('INSERT INTO dir2id (name) VALUES (?)', ('d1',))
    conn.execute('INSERT INTO image_hardlink_info_v4 (md5, file_name, dir1, dir2) '
                 'VALUES (?, ?, 1, 2)', (md5, md5 + '.dat'))
    conn.commit()
    conn.close()
    return dec


def _rel_path(md5):
    return 'msg/attach/h1/d1/Img/' + md5 + '.dat'


class _FoundWithXor(dict):
    """模拟"某个实现返回了带派生 XOR 的密钥映射"（真实实现见 v2.FoundV2Keys）。"""

    def __init__(self, *a, derived_xor=None, **kw):
        super().__init__(*a, **kw)
        self.derived_xor = derived_xor


def _url_for(md5):
    return '/api/hardlink-media?md5=%s&type=3&path=%s' % (md5, _rel_path(md5))


class TestMediaMemoryPathUsesTheDerivedXor:
    def test_derived_xor_is_handed_to_the_decryptor(self, tmp_path, monkeypatch):
        root, img_dir = _make_storage(tmp_path)
        dat = img_dir / f'{MD5}.dat'
        build_v2_dat(str(dat), fake_jpeg(), AES_KEY, DERIVED_XOR)
        dec = _make_decrypted(tmp_path, root, MD5, None)

        monkeypatch.setattr(v2, 'extract_keys_from_mmkv', lambda d, w=None: {})
        monkeypatch.setattr(v2, 'is_wechat_running', lambda: True)
        monkeypatch.setattr(v2, 'find_keys_for_files',
                            lambda d, w, md5s, print_fn=None, account_xor=None:
                            _FoundWithXor({MD5: AES_KEY}, derived_xor=DERIVED_XOR))

        seen = []

        def _record(file_path, aes_key, xor_key=None, output_dir=None):
            seen.append((aes_key, xor_key))
            return None

        monkeypatch.setattr(media, '_decrypt_dat_v2', _record)

        from web.app import create_app
        client = create_app(str(dec), wxid='wxid_real_1a2b').test_client()
        r = client.get(_url_for(MD5))

        assert seen, '内存密钥那条路根本没走到解密（请求 status=%s）' % r.status_code
        assert seen[0][0] == AES_KEY
        assert seen[0][1] == DERIVED_XOR, (
            f'传给解密的 XOR 是 {seen[0][1]!r}，应为派生值 0x{DERIVED_XOR:02X} —— '
            '写死 0xC9 时尾部会解成纯色')

    def test_falls_back_to_the_default_when_no_derived_xor_exists(self, tmp_path, monkeypatch,
                                                                  capsys):
        root, img_dir = _make_storage(tmp_path)
        dat = img_dir / f'{MD5}.dat'
        build_v2_dat(str(dat), fake_jpeg(), AES_KEY, DERIVED_XOR)
        dec = _make_decrypted(tmp_path, root, MD5, None)

        monkeypatch.setattr(v2, 'extract_keys_from_mmkv', lambda d, w=None: {})
        monkeypatch.setattr(v2, 'is_wechat_running', lambda: True)
        # 老签名：只给 {md5: aes}，不带任何 XOR 信息
        monkeypatch.setattr(v2, 'find_keys_for_files',
                            lambda d, w, md5s, print_fn=None: {MD5: AES_KEY})

        seen = []

        def _record(file_path, aes_key, xor_key=None, output_dir=None):
            seen.append((aes_key, xor_key))
            return None

        monkeypatch.setattr(media, '_decrypt_dat_v2', _record)

        from web.app import create_app
        client = create_app(str(dec), wxid='wxid_real_1a2b').test_client()
        client.get(_url_for(MD5))

        assert seen, '内存密钥那条路根本没走到解密'
        assert seen[0][1] == media._DAT_V2_DEFAULT_XOR, (
            '拿不到派生值时必须回退到既有默认值，不许变成 None')
        # 回退这件事必须**看得出来**：否则"派生 0xC9"与"没有派生值只能默认 0xC9"
        # 在日志里完全一样 —— 本缺陷长期潜伏的原因之一。
        out = capsys.readouterr().out
        assert 'NOT a derived value' in out, '回退到默认值时必须打日志说明'

    def test_no_fallback_log_when_the_derived_value_is_really_used(self, tmp_path, monkeypatch,
                                                                   capsys):
        root, img_dir = _make_storage(tmp_path)
        dat = img_dir / f'{MD5}.dat'
        build_v2_dat(str(dat), fake_jpeg(), AES_KEY, DERIVED_XOR)
        dec = _make_decrypted(tmp_path, root, MD5, None)

        monkeypatch.setattr(v2, 'extract_keys_from_mmkv', lambda d, w=None: {})
        monkeypatch.setattr(v2, 'is_wechat_running', lambda: True)
        monkeypatch.setattr(v2, 'find_keys_for_files',
                            lambda d, w, md5s, print_fn=None, account_xor=None:
                            _FoundWithXor({MD5: AES_KEY}, derived_xor=DERIVED_XOR))
        monkeypatch.setattr(media, '_decrypt_dat_v2',
                            lambda fp, aes, xor_key=None, output_dir=None: None)

        from web.app import create_app
        client = create_app(str(dec), wxid='wxid_real_1a2b').test_client()
        client.get(_url_for(MD5))

        assert 'NOT a derived value' not in capsys.readouterr().out, \
            '有派生值的时候不该打"回退默认值"的日志（否则等于天天喊狼来了）'


# ---------------------------------------------------------------------------
# A4. 端到端：真值能完整还原，错值只毁尾部（钉住症状 2 的机制）
# ---------------------------------------------------------------------------

class TestV2TailXorMechanism:
    def test_the_derived_xor_recovers_the_whole_plaintext(self, tmp_path):
        plain = fake_jpeg()
        dat = build_v2_dat(str(tmp_path / 'ok.dat'), plain, AES_KEY, DERIVED_XOR)
        out = media._decrypt_dat_v2(dat, AES_KEY, DERIVED_XOR, str(tmp_path / 'out'))
        assert out and os.path.isfile(out)
        with open(out, 'rb') as f:
            assert f.read() == plain

    def test_a_wrong_xor_corrupts_only_the_tail(self, tmp_path):
        """这就是"上面一小部分能显示、剩下无法解析"的机制。"""
        plain = fake_jpeg()
        dat = build_v2_dat(str(tmp_path / 'bad.dat'), plain, AES_KEY, DERIVED_XOR)
        out = media._decrypt_dat_v2(dat, AES_KEY, 0xC9, str(tmp_path / 'out2'))
        assert out and os.path.isfile(out)
        with open(out, 'rb') as f:
            got = f.read()

        with open(dat, 'rb') as f:
            header = f.read(14)
        xor_size = struct.unpack_from('<I', header, 10)[0]
        cut = len(plain) - xor_size
        assert got[:cut] == plain[:cut], 'AES 段与中间明文与 XOR 无关，必须一模一样'
        assert got[cut:] != plain[cut:], '尾部必须被解错 —— 否则本用例钉不住症状 2'
        assert len(got) == len(plain), 'XOR 不会改变长度'

    def test_the_route_serves_the_whole_image_when_the_xor_is_derived(self, tmp_path, monkeypatch):
        """端到端：合成 V2 图经真实路由返回的字节 == 原始明文（尾部也在内）。"""
        plain = fake_jpeg()
        root, img_dir = _make_storage(tmp_path)
        dat = img_dir / f'{MD5}.dat'
        build_v2_dat(str(dat), plain, AES_KEY, DERIVED_XOR)
        dec = _make_decrypted(tmp_path, root, MD5, None)

        monkeypatch.setattr(v2, 'extract_keys_from_mmkv', lambda d, w=None: {})
        monkeypatch.setattr(v2, 'is_wechat_running', lambda: True)
        monkeypatch.setattr(v2, 'find_keys_for_files',
                            lambda d, w, md5s, print_fn=None, account_xor=None:
                            _FoundWithXor({MD5: AES_KEY}, derived_xor=DERIVED_XOR))

        from web.app import create_app
        client = create_app(str(dec), wxid='wxid_real_1a2b').test_client()
        r = client.get(_url_for(MD5))

        assert r.status_code == 200, f'图片没能解密返回（status={r.status_code}）'
        assert r.data == plain, '尾部 XOR 用错 ⇒ 返回的图尾部是垃圾（症状 2）'


# ---------------------------------------------------------------------------
# A5. 【防御性 / 一致性修复，**不是** issue #16 症状 2 的根因】
#     头部声称的 XOR 长度超过实际剩余时，仍要对剩余字节按可用长度做 XOR ——
#     一个字节都不做等于把**密文**当明文交给解码器（静默隐患）。
# ---------------------------------------------------------------------------

class TestDefensiveXorTailSemantics:
    """⚠️ 定位：**防御性 + 一致性**修复，**本机无任何实测实例**，也**解释不了**症状 2。

    控制方/调查员在全库 **18714 个 V2** 上实测：
      * `plain < file_size` = **0/18714**、头部 `delta<0` = **0/41747**、
        真解密 `len(raw 明文) == file_size` = **1229/1229** ⇒ 本机没有走进这个分支的文件；
      * 而且**截断不会产生绿条**（截断 ⇒ 缺失区被"掩盖"，绿占比 0.0000；
        只有**中段被 XOR 破坏**才会产生绿 0.19–0.90）。
    所以本组用例**不许**被当成"症状 2 的复现"，它只钉两件事：

      1. 该分支**不许**把未解密的尾部原样返回（语义一致性；同仓库 V1 解码器
         ``_decrypt_dat_v1`` 就是 ``xor_data = data[raw_start:]``，根本不看头里的 xor_size）；
      2. 它一旦真的发生，必须**在日志里喊出来**（否则又是"静默降级"）。

    **假设（已写进注释、请控制方知悉）**：这条语义假定"声称长度 > 剩余时，剩余字节都是 XOR 区"。
    若真存在"大段未加密中段 + 头部 xor_size 又被写大"的文件，本行为会把中段也 XOR 一遍
    （旧行为则相反：保留中段、尾部原样返回）。两种都不完美，本机无法判定；消歧可后续用
    "尾部是否是合法文件尾（JPEG ``FF D9`` / PNG ``IEND``）"来做——本任务**没做**。
    """

    def test_claimed_length_far_larger_than_the_file_still_xors_the_whole_tail(self, tmp_path):
        """防御性（本机无实例）：字段被钳在 1MiB 而文件远小于它时，尾部要按可用长度 XOR。"""
        plain = fake_jpeg(300)
        dat = build_v2_dat(str(tmp_path / 'clamped.dat'), plain, AES_KEY, DERIVED_XOR,
                           xor_size=len(plain) - 200, claimed_xor_size=0x100000)
        with open(dat, 'rb') as f:
            raw_tail = f.read()[15 + 208:]

        out = media._decrypt_dat_v2(dat, AES_KEY, DERIVED_XOR, str(tmp_path / 'o_claim'))
        assert out and os.path.isfile(out)
        with open(out, 'rb') as f:
            got = f.read()

        assert got == plain, (
            '头部声称 1MiB 尾部、实际只剩 100 字节时必须按可用长度 XOR；'
            '当前实现把这段密文原样返回了')
        assert got[200:] != raw_tail, '尾部被原样返回 ⇒ 一个字节都没 XOR'
        assert len(got) == len(plain), 'XOR 不改变长度'

    def test_truncated_file_xors_the_bytes_that_do_exist(self, tmp_path):
        """防御性（本机无实例）：文件没下完时，幸存的尾部字节也要做 XOR。"""
        plain = fake_jpeg(300)
        full = build_v2_dat(str(tmp_path / 'full.dat'), plain, AES_KEY, DERIVED_XOR,
                            xor_size=len(plain) - 200)
        with open(full, 'rb') as f:
            data = f.read()
        truncated = tmp_path / 'trunc.dat'
        with open(truncated, 'wb') as f:
            f.write(data[:-16])          # 尾部少 16 字节

        out = media._decrypt_dat_v2(str(truncated), AES_KEY, DERIVED_XOR,
                                    str(tmp_path / 'o_trunc'))
        assert out and os.path.isfile(out)
        with open(out, 'rb') as f:
            got = f.read()

        assert got == plain[:-16], (
            '截断文件里幸存的尾部字节也必须做 XOR（能救回多少算多少），'
            '不许原样返回')
        assert got[200:] != data[-16 - 84:-16], '幸存尾部被原样返回 ⇒ 没做 XOR'

    def test_the_defensive_branch_is_logged(self, tmp_path, capsys):
        """它会真的发生（本机没有，别的机器可能有）时，必须在日志里说明白。"""
        plain = fake_jpeg(300)
        dat = build_v2_dat(str(tmp_path / 'loud.dat'), plain, AES_KEY, DERIVED_XOR,
                           xor_size=len(plain) - 200, claimed_xor_size=0x100000)
        media._decrypt_dat_v2(dat, AES_KEY, DERIVED_XOR, str(tmp_path / 'o_log'))

        out = capsys.readouterr().out
        assert '声称' in out and '实际剩余' in out, \
            '走进"声称长度 > 剩余"这条防御分支时必须打日志（否则又是静默降级）'
        assert '防御' in out, '日志里要标明这是防御性/一致性修复，而不是正常路径'

    def test_a_header_consistent_file_never_touches_the_defensive_branch(self, tmp_path, capsys):
        """反向用例：头部自洽的文件（= 本机全库 18714 个的形态）不许打这条日志。

        否则日志会变成"狼来了"，而且说明正常路径被这条防御分支污染了。
        """
        plain = fake_jpeg(300)
        dat = build_v2_dat(str(tmp_path / 'normal.dat'), plain, AES_KEY, DERIVED_XOR)
        out_path = media._decrypt_dat_v2(dat, AES_KEY, DERIVED_XOR, str(tmp_path / 'o_ok'))
        assert out_path and os.path.isfile(out_path)

        out = capsys.readouterr().out
        assert '实际剩余' not in out, '头部自洽的文件不许走/不许报这条防御分支'

    def test_zero_xor_size_still_means_no_xor(self, tmp_path):
        """头里 xor_size == 0 = "没有 XOR 尾部"，这条既有语义不许被上面的修复带歪。

        构造上把 xor 因子取 0 ⇒ 尾部就是**明文**存进去（与"头里声明没有 XOR 尾部"自洽）。
        """
        plain = fake_jpeg(300)
        dat = build_v2_dat(str(tmp_path / 'noxor.dat'), plain, AES_KEY, 0x00,
                           xor_size=len(plain) - 200, claimed_xor_size=0)
        out = media._decrypt_dat_v2(dat, AES_KEY, DERIVED_XOR, str(tmp_path / 'o_none'))
        assert out and os.path.isfile(out)
        with open(out, 'rb') as f:
            got = f.read()
        assert got == plain, '声明 xor_size=0 时尾部就是明文，不许再 XOR 一遍'


# ---------------------------------------------------------------------------
# A6.（追加缺陷 3）缓存条目缺 `xor_key` 字段时不许退化成 0
# ---------------------------------------------------------------------------

class TestMissingXorFieldFallsBackToTheDefault:
    """``int(v.get('xor_key', '0'), 16)`` 在字段缺失时给出 **0**，而 0 意味着
    "尾部一个字节都不 XOR" ⇒ 症状与用错密钥一模一样，却更隐蔽（连值都不显眼）。
    正确行为：缺失 ⇒ 回退 ``_DAT_V2_DEFAULT_XOR``，并且**打得出来**用的是默认值。
    """

    def _write_cache(self, dec, entry):
        with open(os.path.join(dec, '_media_keys.json'), 'w', encoding='utf-8') as f:
            json.dump({'md5_keys': {MD5: entry}}, f)

    def test_missing_field_becomes_the_default_xor_not_zero(self, tmp_path):
        dec = str(tmp_path)
        self._write_cache(dec, {'aes_key': AES_KEY.hex()})     # 老缓存/手改过的缓存
        entry = media._load_or_build_image_key_map(dec)[MD5]

        assert entry['xor'] == media._DAT_V2_DEFAULT_XOR, (
            '缺字段退化成 0 ⇒ 尾部原样返回，症状与"用错密钥"完全相同却更隐蔽')
        assert entry['xor'] != 0

    def test_fallback_is_visible_in_the_log(self, tmp_path, capsys):
        """这个缺陷之所以长期存在，正是因为"用了默认值"和"用了派生值"从外部看不出区别。"""
        dec = str(tmp_path)
        self._write_cache(dec, {'aes_key': AES_KEY.hex()})
        media._load_or_build_image_key_map(dec)

        out = capsys.readouterr().out
        assert 'missing' in out, '必须报出"有条目缺 xor_key"'
        assert 'default 0xC9' in out, '必须报出用的是哪个默认值'
        assert 'NOT a derived value' in out, '必须明确区分"默认值"与"派生值"'

    def test_a_present_field_is_still_used_verbatim(self, tmp_path):
        dec = str(tmp_path)
        self._write_cache(dec, {'aes_key': AES_KEY.hex(), 'xor_key': '0x3c'})
        assert media._load_or_build_image_key_map(dec)[MD5]['xor'] == DERIVED_XOR

    def test_an_explicit_zero_is_not_treated_as_missing(self, tmp_path):
        """显式写 0 是有意义的取值（虽然罕见），不能被当成"缺字段"。"""
        dec = str(tmp_path)
        self._write_cache(dec, {'aes_key': AES_KEY.hex(), 'xor_key': '0x00'})
        assert media._load_or_build_image_key_map(dec)[MD5]['xor'] == 0


class TestXorSourceIsObservable:
    """写入侧也要能看出"这次用的是派生值还是默认值"（缺陷长期存在的原因之一就是二者不可区分）。"""

    def test_write_site_says_derived_or_default(self, tmp_path, capsys):
        a = tmp_path / 'a'
        a.mkdir()
        v2._merge_into_cache(str(a), {MD5: AES_KEY}, xor_key=DERIVED_XOR)
        assert 'derived 0x3c' in capsys.readouterr().out.lower()

        b = tmp_path / 'b'
        b.mkdir()
        v2._merge_into_cache(str(b), {MD5: AES_KEY})
        out = capsys.readouterr().out.lower()
        assert 'default 0xc9' in out and 'no derived value' in out
