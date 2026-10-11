"""known-issues **#46 的残留**：让"**微信没在运行**"时也能自愈已写坏的图片密钥缓存。

背景（前序任务已实测，本文件按同一机制复核）
--------------------------------------------
* 根因（issue #16 症状 2）：V2 `.dat` 的**尾部 XOR** 是按账号派生的真值 `code & 0xFF`，
  老版本把它写死成 `0xC9` ⇒ 图"能显示但只有上面一小部分正常"（XOR 只作用文件尾部）。
  `known-issues` #45 修了代码、#46 加了"读缓存得到的图必须通过完整性校验"的闸门。
* **残留缺口（本文件要钉住的）**：真正能"既给出正确 XOR、又就地纠正缓存"的是**内存路径**
  （`find_keys_for_files`），它**需要微信在运行**。而**离线**的 MMKV 路径
  （`extract_keys_from_mmkv`，只读 `%APPDATA%\\Tencent\\xwechat\\**\\kvcomm\\key_*.statistic`）
  此前是：``pending = {md5: … if md5 not in existing_md5s}`` ⇒ 已缓存的 md5 根本不进 pending
  ⇒ ``if not pending: return {}``（**连"派生出哪个 XOR"都不记录**）
  ⇒ 那条已经写坏的缓存**永远**不被纠正 ⇒ 微信不跑就自愈不了。

本文件钉住的新语义
------------------
1. MMKV 用**真文件**验证通过（= 它已经能确认"这个 XOR 是对的"）时，对**已缓存**且 `xor_key`
   与真值不等的条目**就地纠正**；
2. **绝不在拿不到已验证真值时动缓存**（不猜、绝不写默认值）；
3. 修复与否**必须打日志**（"用了派生值还是默认值"必须仍然可观测）；
4. **幂等**：值相等就跳过，不反复重写；
5. MMKV 路径的**发现语义与产出集合逐字不变**（返回的仍然只是"本次新发现的 md5 集合"）。

哪些用例是 RED、哪些只是"机制钉子"
-----------------------------------
改动前（未实现本任务时）就绿的用例一律**显式标成"机制钉子"**（例如"拿不到真值时缓存逐字节
未变""`aes_key` 不许被动""返回集合仍是 pending 那一套"），它们钉的是**不许变坏**；
它们"确实咬得住"由实现报告里的变异 M2 / M3 证明（M2 = 没验证就修、M3 = 连别的账号的条目也修）。

本文件**全部合成**：自造 MMKV statistic 文件名、自造 V2 `.dat`、自造 hardlink.db、
自造一份**已被写坏**的 `_media_keys.json`。不读不写任何真实微信数据
（`_FALLBACK_STORAGE_ROOTS` 置空、`_scan_mmkv_kvcomm_dirs` 被缝到合成目录，
绝不探测本机真实的 `%APPDATA%` 与存储根）。
"""
import hashlib
import json
import os
import sqlite3
import struct

import pytest

from engine.services import media
from engine.services import v2_key_extract as v2

# 账号级派生真值（"非 0xC9"，写死默认值的实现必然在这里露馅）
DERIVED_XOR = 0x3C
# 历史版本写死的错值 = 已经被固化进真实用户缓存的那个值
WRONG_XOR = 0xC9
# 让 `code & 0xFF == DERIVED_XOR` 的 MMKV statistic code
CODE = 0x13C
# `_clean_wxid` 的**恒等**情形（两段式，不带 `_xxxx` 后缀）⇒ 派生式的入参没有歧义
ACCOUNT = 'wxid_demo'
MD5 = 'a' * 32
MD5B = 'b' * 32

AES_SIZE = 200           # 造文件时不放中间段：xor_size = len(plain) - AES_SIZE


def derive_aes_key(code, wxid):
    """py_wx_key 的派生式，**独立实现**（刻意不调用被测代码，避免自证）。"""
    return hashlib.md5((str(code) + wxid).encode('utf-8')).hexdigest()[:16].encode('ascii')


AES_KEY = derive_aes_key(CODE, ACCOUNT)


# ---------------------------------------------------------------------------
# 合成素材
# ---------------------------------------------------------------------------

def build_v2_dat(path, plaintext, aes_key, xor_key, aes_size=AES_SIZE):
    """按真实 V2 布局造一个 `.dat`（与其它测试的合成器同构，不 import 以免耦合）。

        [15B 头: 6B 签名 + <I aes_size + <I xor_size + 1B 填充]
        [AES-128-ECB(PKCS7 填充后的前 aes_size 字节明文)]
        [XOR 尾部: 最后 xor_size 字节逐字节异或]
    """
    from Crypto.Cipher import AES
    from Crypto.Util import Padding

    assert aes_size % 16 != 0, '否则 padded_aes_size 的算法会多算一整块'
    xor_size = len(plaintext) - aes_size
    assert xor_size > 0

    cipher = AES.new(aes_key[:16], AES.MODE_ECB)
    aes_seg = cipher.encrypt(Padding.pad(plaintext[:aes_size], 16))
    assert len(aes_seg) == aes_size + 16 - (aes_size % 16)

    tail_enc = bytes(b ^ xor_key for b in plaintext[aes_size:])
    header = (b'\x07\x08V2\x08\x07'
              + struct.pack('<II', aes_size, xor_size)
              + b'\x00')
    assert len(header) == 15
    with open(path, 'wb') as f:
        f.write(header + aes_seg + tail_enc)
    return path


def fake_jpeg(size=300, eoi=True):
    """一段"长得像 JPEG"的明文：4 字节头 + 填充 + （可选）EOI 结束符。"""
    body = b'\xff\xd8\xff\xe0' + bytes((i * 7) % 251 for i in range(size - 6))
    return body + b'\xff\xd9' if eoi else body


# ---------------------------------------------------------------------------
# 合成环境
# ---------------------------------------------------------------------------

def _setup(tmp_path, files, *, account=ACCOUNT):
    """造 <tmp>/xwechat_files/<account>/msg/attach/h1/d1/Img/<md5>.dat 与 hardlink.db。

    ``files`` = ``[(md5, plaintext), ...]``（明文按**派生真值** XOR 落盘 ⇒ 只有真值能解全）。
    """
    root = tmp_path / 'xwechat_files'
    img = root / account / 'msg' / 'attach' / 'h1' / 'd1' / 'Img'
    img.mkdir(parents=True, exist_ok=True)
    for md5, plain in files:
        build_v2_dat(str(img / f'{md5}.dat'), plain, AES_KEY, DERIVED_XOR)

    dec = tmp_path / 'decrypted'
    (dec / 'hardlink').mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(dec / 'hardlink' / 'hardlink.db'))
    conn.execute('CREATE TABLE IF NOT EXISTS db_info (Key TEXT, ValueStdStr TEXT)')
    conn.execute('CREATE TABLE IF NOT EXISTS dir2id (name TEXT, username TEXT)')
    conn.execute('CREATE TABLE IF NOT EXISTS image_hardlink_info_v4 '
                 '(md5 TEXT, file_name TEXT, dir1 INTEGER, dir2 INTEGER)')
    conn.execute('INSERT INTO db_info (Key, ValueStdStr) VALUES (?, ?)',
                 ('uuid', '1_' + 'x' * 20 + '_' + str(root)))
    conn.execute('INSERT INTO dir2id (name, username) VALUES (?, ?)', ('h1', 'h1'))
    conn.execute('INSERT INTO dir2id (name, username) VALUES (?, ?)', ('d1', 'd1'))
    for md5, _plain in files:
        conn.execute('INSERT INTO image_hardlink_info_v4 (md5, file_name, dir1, dir2) '
                     'VALUES (?, ?, 1, 2)', (md5, md5 + '.dat'))
    conn.commit()
    conn.close()
    return str(dec), str(img)


def _synthetic_mmkv(monkeypatch, tmp_path, *codes):
    """把"本机 MMKV 环境在哪儿"这一缝缝到合成目录 —— **派生与验证逻辑全走真实实现**。

    ⚠️ 只替换 `_scan_mmkv_kvcomm_dirs`（它的职责是"去 `%APPDATA%` 找目录"），
    这样本机真实的 `%APPDATA%\\Tencent\\xwechat` **绝不会**被读到。
    ``*codes`` 里的每个 code 造一个真实的 `key_<code>_<salt>.statistic` **文件名**
    —— 真实实现只解析**文件名**（内容不参与），这里写几个标记字节并在报告里如实说明
    "并不声称它是真的 MMKV 序列化格式"。
    """
    d = tmp_path / 'kvcomm'
    d.mkdir(parents=True, exist_ok=True)
    for code in codes:
        (d / f'key_{code}_deadbeef.statistic').write_bytes(b'MMKV\x00synthetic')
    monkeypatch.setattr(v2, '_scan_mmkv_kvcomm_dirs', lambda: [str(d)])
    return d


def _write_cache(dec, entries):
    """把（可能已经被写坏的）密钥写进 `_media_keys.json` —— issue #46 的起点。

    ``entries`` = ``{md5: (aes_key_bytes, xor_key_int)}``。
    """
    payload = {'md5_keys': {m: {'aes_key': a.hex(), 'xor_key': '0x%02x' % x}
                            for m, (a, x) in entries.items()}}
    with open(os.path.join(dec, '_media_keys.json'), 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2)


def _cache_path(dec):
    return os.path.join(dec, '_media_keys.json')


def _cache_bytes(dec):
    with open(_cache_path(dec), 'rb') as f:
        return f.read()


def _cache_entry(dec, md5):
    with open(_cache_path(dec), encoding='utf-8') as f:
        return json.load(f)['md5_keys'][md5]


def _cache_xor(dec, md5):
    return int(_cache_entry(dec, md5)['xor_key'], 16)


def _url_for(md5):
    return '/api/hardlink-media?md5=%s&type=3&path=%s' % (
        md5, 'msg/attach/h1/d1/Img/' + md5 + '.dat')


def _client(dec):
    from web.app import create_app
    return create_app(dec, wxid=ACCOUNT).test_client()


# ---------------------------------------------------------------------------
# 前提：微信**没在运行**（本文件所有用例的地基）
# ---------------------------------------------------------------------------

def _memory_path_must_not_be_used(*args, **kwargs):
    raise AssertionError(
        '本文件所有用例的前提是"**微信没在运行**"：内存路径（find_keys_for_files）'
        '绝不该被走到 —— 它需要微信进程在跑，正是本任务要摆脱的依赖')


@pytest.fixture(autouse=True)
def _wechat_is_not_running(monkeypatch):
    """把"进程枚举"这一缝缝成"一个进程都没有"，**真实的** `is_wechat_running()`
    因此返回 False；内存路径整个换成"一调用就炸"。"""
    monkeypatch.setattr(v2, '_get_wechat_pids', lambda: [])
    monkeypatch.setattr(v2, 'find_keys_for_files', _memory_path_must_not_be_used)


@pytest.fixture(autouse=True)
def _no_real_wechat_roots(monkeypatch):
    """任何一次解析都不许去探测本机真实的 D:\\xwechat_files 等硬编码根。"""
    monkeypatch.setattr(media, '_FALLBACK_STORAGE_ROOTS', (), raising=False)
    monkeypatch.setattr(media, '_IMAGE_KEY_MAP', {}, raising=False)
    monkeypatch.setattr(media, '_IMAGE_KEY_MAP_DIR', None, raising=False)
    monkeypatch.setattr(media, '_ACCOUNT_KEYS_CACHE', {}, raising=False)


@pytest.fixture(autouse=True)
def _no_stale_derived_xor_registry(monkeypatch):
    """派生 XOR 的进程内登记表必须逐用例清空，否则用例之间互相污染。"""
    monkeypatch.setattr(v2, '_DERIVED_XOR_BY_DIR', {}, raising=False)


class TestThePremiseIsReallyEnforced:
    """钉子（改动前也绿）：证明"微信不在运行"不是嘴上说说，而是**结构上**成立的。"""

    def test_the_real_check_says_wechat_is_not_running(self):
        assert v2.is_wechat_running() is False, \
            '本文件的前提是微信没在运行；`is_wechat_running()` 走的是真实实现（只缝了进程枚举）'

    def test_the_memory_path_is_genuinely_unusable_here(self):
        with pytest.raises(AssertionError):
            v2.find_keys_for_files('/nonexistent', ACCOUNT, [MD5])


# ---------------------------------------------------------------------------
# 夹具自检（钉子：改动前也绿）—— 保证下面那些用例红/绿都不是因为素材不对
# ---------------------------------------------------------------------------

class TestTheSyntheticEnvironmentIsSound:
    def test_the_fixture_derives_and_verifies_with_the_real_code(self, tmp_path, monkeypatch):
        plain = fake_jpeg(300)
        dec, _img = _setup(tmp_path, [(MD5, plain)])
        _synthetic_mmkv(monkeypatch, tmp_path, CODE)

        # 1) 派生式：独立实现与真实实现必须一致
        assert v2._derive_key_from_mmkv(CODE, ACCOUNT) == (DERIVED_XOR, AES_KEY), \
            '合成 code 与派生式的口径不一致 ⇒ 后面的用例会红得莫名其妙'
        # 2) 真文件确实能被"这把 AES + 真值"解开（真实实现的验证入口）
        tasks = v2._load_v2_ciphertexts(dec, ACCOUNT)
        assert MD5 in tasks, '合成 .dat 没被 `_load_v2_ciphertexts` 发现 ⇒ 夹具坏了'
        assert v2._try_key(AES_KEY, tasks[MD5][1]) == 'JPEG', \
            '合成的 .dat 用派生密钥解不出 JPEG 头 ⇒ 夹具坏了'
        # 3) code 的文件名能被真实的解析器认出来
        codes = v2._parse_mmkv_codes([str(tmp_path / 'kvcomm')])
        assert codes == [CODE], f'statistic 文件名没被真实解析器识别: {codes}'


# ---------------------------------------------------------------------------
# A. 拿到"已验证真值"时必须登记（此前 pending 为空就连这个都不做）
# ---------------------------------------------------------------------------

class TestTheVerifiedOfflineTruthIsRecorded:
    def test_the_derived_value_is_recorded_even_when_everything_is_already_cached(
            self, tmp_path, monkeypatch):
        """稳态（所有 V2 文件都已缓存）= 真实用户的普遍状态：此前函数**提前 return**，
        连"派生出哪个 XOR"都不记录 ⇒ 后续读者只能回退默认值 0xC9。"""
        plain = fake_jpeg(300)
        dec, _img = _setup(tmp_path, [(MD5, plain)])
        _write_cache(dec, {MD5: (AES_KEY, WRONG_XOR)})     # 已缓存（且写坏了）
        _synthetic_mmkv(monkeypatch, tmp_path, CODE)

        assert v2.get_derived_xor(dec) is None, '起点：本进程还不知道这个账号的派生值'

        v2.extract_keys_from_mmkv(dec, ACCOUNT)

        assert v2.get_derived_xor(dec) == DERIVED_XOR, (
            'MMKV 明明用**真文件**验证出了真值 0x%02X，却没登记 ⇒ 后续读者（内存扫描、'
            '缓存修复）只能回退默认值 0x%02X —— 这正是缺陷长期潜伏的原因' % (DERIVED_XOR, WRONG_XOR))


# ---------------------------------------------------------------------------
# B. 核心：已缓存的坏条目必须被**就地纠正**（不需要微信在运行）
# ---------------------------------------------------------------------------

class TestAnAlreadyCachedEntryIsRepairedOffline:
    def test_the_wrong_cached_xor_is_corrected_in_place(self, tmp_path, monkeypatch):
        plain = fake_jpeg(300)
        dec, _img = _setup(tmp_path, [(MD5, plain)])
        _write_cache(dec, {MD5: (AES_KEY, WRONG_XOR)})      # ← 已经被写坏的缓存
        _synthetic_mmkv(monkeypatch, tmp_path, CODE)
        assert v2.is_wechat_running() is False, '前提：微信没在运行'

        before = _cache_bytes(dec)
        found = v2.extract_keys_from_mmkv(dec, ACCOUNT)
        after = _cache_bytes(dec)

        # —— RED：那条已缓存的错值被就地纠正 ——
        assert _cache_xor(dec, MD5) == DERIVED_XOR, (
            'MMKV 已经用真文件确认了真值 0x%02X，却没把缓存里那条写坏的 0x%02X 改回来 ⇒ '
            '微信没在运行时这张图永远修不好（缓存命中会短路掉后续所有推导）'
            % (DERIVED_XOR, WRONG_XOR))
        assert after != before, '修复必须真的落到盘上（字节级差异）'

        # —— 机制钉子：产出集合语义逐字不变（改动前也是空集合）——
        assert isinstance(found, dict) and not found and len(found) == 0
        assert dict(found) == {}, '返回值仍然只表示"本次新发现的 md5 集合"（这里是空）'

    def test_the_cached_aes_key_is_not_a_casualty_of_the_repair(self, tmp_path, monkeypatch):
        """钉子（改动前也绿）：这条通道只该动 `xor_key`，不许顺手重写 `aes_key`。"""
        plain = fake_jpeg(300)
        dec, _img = _setup(tmp_path, [(MD5, plain)])
        _write_cache(dec, {MD5: (AES_KEY, WRONG_XOR)})
        _synthetic_mmkv(monkeypatch, tmp_path, CODE)

        v2.extract_keys_from_mmkv(dec, ACCOUNT)

        assert _cache_entry(dec, MD5)['aes_key'] == AES_KEY.hex(), \
            'aes_key 是另一个维度的数据（本次并没有重新"发现"密钥），不许被动'

    def test_another_md5s_entry_with_a_wrong_xor_is_also_corrected(self, tmp_path, monkeypatch):
        """修复通道不能只认"被验证用的那一个 md5"：同账号的**所有**坏条目都该改回来。"""
        plain = fake_jpeg(300)
        dec, _img = _setup(tmp_path, [(MD5, plain)])
        # MD5B 只有缓存条目、没有对应的 .dat（模拟"文件已删/已清理"）
        _write_cache(dec, {MD5: (AES_KEY, WRONG_XOR), MD5B: (AES_KEY, WRONG_XOR)})
        _synthetic_mmkv(monkeypatch, tmp_path, CODE)

        v2.extract_keys_from_mmkv(dec, ACCOUNT)

        assert _cache_xor(dec, MD5) == DERIVED_XOR
        assert _cache_xor(dec, MD5B) == DERIVED_XOR, \
            '同一个账号下另一条写坏的缓存条目也必须一并纠正（只修一条 = 用户下次换张图又坏）'

    def test_an_entry_whose_aes_key_is_not_this_accounts_key_is_left_alone(
            self, tmp_path, monkeypatch):
        """钉子（改动前也绿；M3 变异会让它变红）。

        保守边界：`xor_key` 是**账号级**属性 ⇒ 只有当条目的 `aes_key` 与本次用真文件验证过的
        账号密钥一致时，才能断定"这条属于本账号、它的 xor 就该是真值"。
        `aes_key` 不同的条目属于**归属不明**的条目（可能是另一个账号/另一次实验留下的），
        动手可能把本来能用的图改坏 ⇒ **不动**，并且要在日志里说出来。
        """
        plain = fake_jpeg(300)
        dec, _img = _setup(tmp_path, [(MD5, plain)])
        foreign = b'FEDCBA9876543210'
        _write_cache(dec, {MD5: (AES_KEY, WRONG_XOR), MD5B: (foreign, WRONG_XOR)})
        _synthetic_mmkv(monkeypatch, tmp_path, CODE)

        v2.extract_keys_from_mmkv(dec, ACCOUNT)

        assert _cache_xor(dec, MD5) == DERIVED_XOR, '属于本账号的那条必须被修'
        assert _cache_xor(dec, MD5B) == WRONG_XOR, \
            '归属不明的条目不许动（归错账号会把本来能显示的图改坏）'


# ---------------------------------------------------------------------------
# C. 产出集合语义不变（把这条钉死）
# ---------------------------------------------------------------------------

class TestTheDiscoverySemanticsAreUnchanged:
    def test_the_returned_set_is_still_exactly_the_pending_md5s(self, tmp_path, monkeypatch):
        """有未缓存文件时：返回的**恰好**是 pending 那一套（改动前也是），
        同时已缓存的那条坏条目顺带被修好。"""
        plain = fake_jpeg(300)
        plain_b = fake_jpeg(320)
        dec, _img = _setup(tmp_path, [(MD5, plain), (MD5B, plain_b)])
        _write_cache(dec, {MD5: (AES_KEY, WRONG_XOR)})       # MD5 已缓存（且坏），MD5B 未缓存
        _synthetic_mmkv(monkeypatch, tmp_path, CODE)

        found = v2.extract_keys_from_mmkv(dec, ACCOUNT)

        # —— 机制钉子：发现语义不变 ——
        assert set(found) == {MD5B}, (
            '返回的必须是"本次**新发现**的 md5"（= pending），多一条少一条都算改了语义；'
            '实际 %r' % (set(found),))
        assert found[MD5B] == AES_KEY
        assert MD5 not in found, '已缓存的 md5 不许因为被"修复"而混进返回值里'

        # —— RED：修复通道与发现通道互不干扰，两条都到位 ——
        assert _cache_xor(dec, MD5) == DERIVED_XOR, '已缓存的那条也要在这一次调用里被修好'
        assert _cache_xor(dec, MD5B) == DERIVED_XOR

    def test_the_result_is_still_a_plain_dict_like_before(self, tmp_path, monkeypatch):
        """钉子：`{md5: aes}` 的既有用法（`in` / `len` / `dict()`）一律不受影响。"""
        plain = fake_jpeg(300)
        dec, _img = _setup(tmp_path, [(MD5, plain)])
        _synthetic_mmkv(monkeypatch, tmp_path, CODE)

        found = v2.extract_keys_from_mmkv(dec, ACCOUNT)

        assert isinstance(found, dict)
        assert found[MD5] == AES_KEY and MD5 in found and len(found) == 1
        assert dict(found) == {MD5: AES_KEY}


# ---------------------------------------------------------------------------
# D. 拿不到"已验证真值" ⇒ 一律不动缓存（绝不猜、绝不写默认值）
# ---------------------------------------------------------------------------

class TestNoVerifiedTruthMeansNoTouch:
    def test_failed_verification_leaves_the_cache_byte_for_byte(self, tmp_path, monkeypatch, capsys):
        """派生得出来、但**真文件验证不通过** ⇒ 那个值不算真值 ⇒ 缓存逐字节不许变。"""
        plain = fake_jpeg(300)
        dec, _img = _setup(tmp_path, [(MD5, plain)])
        _write_cache(dec, {MD5: (AES_KEY, WRONG_XOR)})
        _synthetic_mmkv(monkeypatch, tmp_path, CODE)
        monkeypatch.setattr(v2, '_try_key', lambda key, ct: None)    # 验证一律不通过

        before = _cache_bytes(dec)
        found = v2.extract_keys_from_mmkv(dec, ACCOUNT)
        out = capsys.readouterr().out

        assert not found, '验证不通过时不许报"发现了密钥"'
        assert _cache_bytes(dec) == before, \
            '拿不到**已验证**的真值时，缓存必须逐字节不变（不猜、不写默认值）'
        assert v2.get_derived_xor(dec) is None, '验证没通过的值不是真值，不许登记'
        assert '不动' in out and '绝不猜' in out, \
            ('"这次没动缓存"必须在日志里说出来，否则"修了"与"没修"从外部看一样'
             '（本缺陷长期潜伏的原因之一）')

    def test_no_codes_means_the_cache_is_not_touched(self, tmp_path, monkeypatch):
        """钉子（改动前也绿）：连 code 都没有时更不该动缓存。"""
        plain = fake_jpeg(300)
        dec, _img = _setup(tmp_path, [(MD5, plain)])
        _write_cache(dec, {MD5: (AES_KEY, WRONG_XOR)})
        _synthetic_mmkv(monkeypatch, tmp_path)          # 造出目录但**不放** statistic 文件

        before = _cache_bytes(dec)
        found = v2.extract_keys_from_mmkv(dec, ACCOUNT)

        assert not found
        assert _cache_bytes(dec) == before
        assert v2.get_derived_xor(dec) is None

    def test_no_v2_files_means_the_cache_is_not_touched(self, tmp_path, monkeypatch):
        """钉子：没有任何 V2 文件可供验证 ⇒ 同样不许动。"""
        plain = fake_jpeg(300)
        dec, _img = _setup(tmp_path, [(MD5, plain)])
        _write_cache(dec, {MD5: (AES_KEY, WRONG_XOR)})
        _synthetic_mmkv(monkeypatch, tmp_path, CODE)
        monkeypatch.setattr(v2, '_load_v2_ciphertexts', lambda d, w: {})

        before = _cache_bytes(dec)
        found = v2.extract_keys_from_mmkv(dec, ACCOUNT)

        assert not found
        assert _cache_bytes(dec) == before


# ---------------------------------------------------------------------------
# E. 可观测：修了 / 没修，日志上必须看得出来
# ---------------------------------------------------------------------------

class TestTheRepairIsObservable:
    def test_the_repair_says_how_many_entries_it_fixed(self, tmp_path, monkeypatch, capsys):
        plain = fake_jpeg(300)
        dec, _img = _setup(tmp_path, [(MD5, plain)])
        _write_cache(dec, {MD5: (AES_KEY, WRONG_XOR)})
        _synthetic_mmkv(monkeypatch, tmp_path, CODE)

        v2.extract_keys_from_mmkv(dec, ACCOUNT)
        out = capsys.readouterr().out

        assert '修复' in out, '修复了缓存就必须有"修复"的日志（否则用户/我们无从知道发生什么）'
        assert '0x3c' in out.lower(), '日志要写出纠正成了哪个值（派生真值），不许含糊'
        assert '不动' not in out, '真的修了的时候不该同时说"不动"（狼来了）'

    def test_when_nothing_needed_repair_it_says_so_and_does_not_write(
            self, tmp_path, monkeypatch, capsys):
        plain = fake_jpeg(300)
        dec, _img = _setup(tmp_path, [(MD5, plain)])
        _write_cache(dec, {MD5: (AES_KEY, DERIVED_XOR)})     # 缓存里的值本来就是对的
        _synthetic_mmkv(monkeypatch, tmp_path, CODE)
        before_mtime = os.stat(_cache_path(dec)).st_mtime_ns

        found = v2.extract_keys_from_mmkv(dec, ACCOUNT)
        out = capsys.readouterr().out

        assert not found
        assert '无需修复' in out, \
            '什么都没修的时候也必须有一条明确的日志（"这次没动"和"这次修了"必须可区分）'
        assert os.stat(_cache_path(dec)).st_mtime_ns == before_mtime, \
            '值已经相等 ⇒ 不许重写文件（幂等）'


# ---------------------------------------------------------------------------
# F. 幂等：同一账号重复调用不反复重写
# ---------------------------------------------------------------------------

class TestTheRepairIsIdempotent:
    def test_the_second_call_does_not_rewrite_the_cache(self, tmp_path, monkeypatch):
        plain = fake_jpeg(300)
        dec, _img = _setup(tmp_path, [(MD5, plain)])
        _write_cache(dec, {MD5: (AES_KEY, WRONG_XOR)})
        _synthetic_mmkv(monkeypatch, tmp_path, CODE)

        writes = []
        real_merge = v2._merge_into_cache

        def _counting_merge(*args, **kwargs):
            writes.append(1)
            return real_merge(*args, **kwargs)

        monkeypatch.setattr(v2, '_merge_into_cache', _counting_merge)

        v2.extract_keys_from_mmkv(dec, ACCOUNT)
        assert _cache_xor(dec, MD5) == DERIVED_XOR, '第一次调用必须完成纠正（否则本用例没意义）'
        assert len(writes) == 1, '第一次调用写一次就够了（不给 pending 文件写，因为一个都没有）'

        first_bytes = _cache_bytes(dec)
        first_mtime = os.stat(_cache_path(dec)).st_mtime_ns

        v2.extract_keys_from_mmkv(dec, ACCOUNT)          # ← 第二次

        assert len(writes) == 1, \
            '第二次调用不该再走一次写盘通道（值已经相等 ⇒ 跳过，幂等）'
        assert _cache_bytes(dec) == first_bytes
        assert os.stat(_cache_path(dec)).st_mtime_ns == first_mtime, '文件不该被重写（mtime 为证）'


# ---------------------------------------------------------------------------
# G. 端到端：坏缓存 + 离线 MMKV 真值 ⇒ 路由给出的图**通过完整性校验**
# ---------------------------------------------------------------------------

class TestEndToEndOfflineSelfHeal:
    def test_the_bad_cache_now_yields_a_complete_image_without_wechat(
            self, tmp_path, monkeypatch, capsys):
        plain = fake_jpeg(300)
        dec, img = _setup(tmp_path, [(MD5, plain)])
        _write_cache(dec, {MD5: (AES_KEY, WRONG_XOR)})
        _synthetic_mmkv(monkeypatch, tmp_path, CODE)

        # —— 对照：**修复前**用户看到的那张图（用错 XOR 解出来）缺 EOI ——
        bad_path = media._decrypt_dat_v2(
            os.path.join(img, f'{MD5}.dat'), AES_KEY, WRONG_XOR,
            str(tmp_path / 'compare_before'))
        assert bad_path and os.path.isfile(bad_path)
        with open(bad_path, 'rb') as f:
            bad = f.read()
        assert not bad.endswith(b'\xff\xd9'), \
            '对照前提不成立：用错 XOR 解出来的图居然带 EOI ⇒ 本用例钉不住"修复前是坏的"'
        assert bad[:AES_SIZE] == plain[:AES_SIZE], '错值只毁尾部（AES 段与 XOR 无关）'

        r = _client(dec).get(_url_for(MD5))
        out = capsys.readouterr().out

        assert r.status_code == 200, f'期望 200，实际 {r.status_code}'
        assert r.data == plain, (
            '微信没在运行时，MMKV 路径必须独自把图救回来（现在结尾是 %r，'
            '完整图应结尾 %r）' % (r.data[-2:], b'\xff\xd9'))
        assert r.data[-2:] == b'\xff\xd9', '修复后出来的图必须通过完整性校验（JPEG 有 FF D9）'
        assert _cache_xor(dec, MD5) == DERIVED_XOR, '而且缓存必须被就地纠正（下次才不会又走一遍）'
        assert '修复' in out, '这条离线自愈必须在日志里留下痕迹（否则"修没修"没人知道）'

    def test_the_second_request_also_serves_the_complete_image(self, tmp_path, monkeypatch):
        """内存里的 key_map 是**缓存**：修好盘上的文件后若不重读，这一整个进程都会继续用旧值
        ⇒ 第一次请求能看到好图、第二次又变坏。本用例把"修完要重读"钉住。"""
        plain = fake_jpeg(300)
        dec, _img = _setup(tmp_path, [(MD5, plain)])
        _write_cache(dec, {MD5: (AES_KEY, WRONG_XOR)})
        _synthetic_mmkv(monkeypatch, tmp_path, CODE)

        client = _client(dec)
        first = client.get(_url_for(MD5))
        second = client.get(_url_for(MD5))

        assert first.status_code == 200 and second.status_code == 200
        assert first.data == plain, '第一次请求就该给出完整的图'
        assert second.data == plain, \
            '第二次请求也必须给出完整的图（内存缓存必须被重读，否则本进程一直用那条坏值）'

    def test_the_memory_path_was_never_consulted(self, tmp_path, monkeypatch):
        """`calls == []` 是**机制钉子**（改动前也绿）；`r.data == plain` 是本任务的 RED
        —— 改动前那张图是坏的，而"微信没在运行"这条前提又恰恰说明**只有** MMKV 路径能救它。"""
        plain = fake_jpeg(300)
        dec, _img = _setup(tmp_path, [(MD5, plain)])
        _write_cache(dec, {MD5: (AES_KEY, WRONG_XOR)})
        _synthetic_mmkv(monkeypatch, tmp_path, CODE)

        calls = []
        monkeypatch.setattr(v2, 'find_keys_for_files',
                            lambda *a, **k: calls.append(1) or {})
        assert v2.is_wechat_running() is False

        r = _client(dec).get(_url_for(MD5))

        assert calls == [], '微信没在运行时不许走内存路径（本任务就是要摆脱这个依赖）'
        assert r.status_code == 200 and r.data == plain
