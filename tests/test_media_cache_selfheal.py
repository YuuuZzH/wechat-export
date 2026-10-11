"""issue #46：已经写坏的 V2 图片密钥缓存必须有机会自愈。

机理（与 issue #16 的症状同源）
--------------------------------
* V2 `.dat` 的尾部 XOR 必须来自按账号派生的真值（``code & 0xFF``）；"写死 0xC9"这条
  Task 28 已修。
* **但错值已经写进 `_media_keys.json` 缓存**了：缓存命中即 ``return``，而"成功"的判据只是
  **前 16 字节像图片**（``_sniff_image_mime``）—— 用错 XOR 时那 16 字节来自**正确的 AES 段**
  ⇒ 照样 200 + 一张坏图（"能显示，但只有上面一小部分正常"）
  ⇒ 后续的 MMKV 重新派生**永远不执行** ⇒ 错值永久固化。
* 本文件钉住的新语义：**只有通过完整性校验的候选才立刻返回**；确认缺尾的候选**暂存**并
  让位给后续步骤；链走完仍没有更好的，才**回退返回**它（**绝不 404 / 500 / 415**）。

三态（来自真实数据的硬约束）
----------------------------
控制方实测本机真实 ``decrypted_media`` 缓存 31 个文件（28 JPEG / 3 PNG）里 **2 个 JPEG 缺 EOI**，
很可能是**源图本身就被截断**、但实际能正常显示 ⇒ **绝不允许把"缺尾"当成"这张图不能给用户"**。
所以三态的**处置**必须是：

* ``True``（通过校验）    ⇒ 立刻返回（绝大多数情况，与今天行为一致）；
* ``None``（不可判定：WEBP/BMP/其它/数据太短）⇒ **也立刻返回**（保持现状，不许因此变慢或改变结果）；
* ``False``（可判定类型且确认缺尾）⇒ **暂存 + 继续**；链走完仍无 ``True``/``None`` 才回退返回它。

哪些断言是 RED、哪些只是"机制钉子"
-----------------------------------
改动前（未实现本任务时）：

* RED：``media._image_completeness`` 不存在；"缓存候选被判缺尾后继续走 MMKV"、跳过日志、
  回退日志、``X-WeChat-Image-Completeness`` header —— 这些**改动前都不存在**；
* 机制钉子（改动前**也是绿的**，不算 RED）：`200` 与"回退返回那张候选的字节"这两条 ——
  改动前缓存候选直接返回，同样是 200 + 那张坏图。它们钉的是**不许变坏**（不许 404/500、
  不许把缩略图当成必然更优），以及"假阳性"（源图本身缺尾）不许被当成失败。

本文件**全部合成**：自造 V2 `.dat`、自造 hardlink.db、自造 MMKV 派生结果 / 缩略图，
不读不写任何真实微信数据（``_FALLBACK_STORAGE_ROOTS`` 被置空，绝不探测本机真实根目录）。
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
# 历史版本写死的错值 = 已经被固化进真实用户缓存的那个值
WRONG_XOR = 0xC9

AES_KEY = b'0123456789abcdef'
MD5 = 'a' * 32
AES_SIZE = 200           # 造文件时不放中间段：xor_size = len(plain) - AES_SIZE

PNG_HEAD = b'\x89PNG\r\n\x1a\n'
PNG_IEND = b'\x00\x00\x00\x00IEND\xaeB`\x82'


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
    monkeypatch.setattr(v2, '_DERIVED_XOR_BY_DIR', {}, raising=False)


# ---------------------------------------------------------------------------
# 合成素材
# ---------------------------------------------------------------------------

def build_v2_dat(path, plaintext, aes_key, xor_key, aes_size=AES_SIZE):
    """按真实 V2 布局造一个 `.dat`（与 Task 28 的合成器同构，不 import 它以免耦合）。

        [15B 头: 6B 签名 + <I aes_size + <I xor_size + 1B 填充]
        [AES-128-ECB(PKCS7 填充后的前 aes_size 字节明文)]
        [中间明文（不加密）]
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

    tail_plain = plaintext[aes_size:]
    tail_enc = bytes(b ^ xor_key for b in tail_plain)

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


def fake_png(size=300):
    filler = size - len(PNG_HEAD) - 8 - len(PNG_IEND)
    return PNG_HEAD + b'\x00\x00\x00\rIHDR' + b'\x11' * filler + PNG_IEND


def fake_webp(size=300):
    return b'RIFF' + struct.pack('<I', size - 8) + b'WEBP' + b'VP8 ' + b'\x00' * (size - 20)


def fake_bmp(size=300):
    return b'BM' + struct.pack('<I', size) + b'\x00' * (size - 6)


def _wrong_xor_bytes(plain, aes_size=AES_SIZE, right=DERIVED_XOR, wrong=WRONG_XOR):
    """用**错** XOR 解出来的字节：AES 段与中间段正确，只有尾部被解错。

    这就是真实用户缓存里那条错条目产出的图（"上面一小部分正常"）。
    """
    bad = plain[:aes_size] + bytes(b ^ right ^ wrong for b in plain[aes_size:])
    assert len(bad) == len(plain), 'XOR 不改变长度'
    return bad


def _completeness(x, mime=None):
    """惰性引用三态校验入口，让它"不存在"时的失败理由清清楚楚。"""
    fn = getattr(media, '_image_completeness', None)
    assert fn is not None, ('media._image_completeness 尚未实现 —— 本任务要求新增一个可单测的'
                            '"可判定类型 / 不可判定类型"三态完整性校验入口')
    return fn(x, mime)


# ---------------------------------------------------------------------------
# 合成环境（hardlink.db + 账号目录 + 密钥缓存）
# ---------------------------------------------------------------------------

def _setup(tmp_path, plain, *, aes_size=AES_SIZE, account='wxid_real_1a2b'):
    """造 <tmp>/xwechat_files/<account>/msg/attach/h1/d1/Img/<MD5>.dat 与 hardlink.db。"""
    root = tmp_path / 'xwechat_files'
    img = root / account / 'msg' / 'attach' / 'h1' / 'd1' / 'Img'
    img.mkdir(parents=True, exist_ok=True)
    dat = img / f'{MD5}.dat'
    build_v2_dat(str(dat), plain, AES_KEY, DERIVED_XOR, aes_size=aes_size)

    dec = tmp_path / 'decrypted'
    (dec / 'hardlink').mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(dec / 'hardlink' / 'hardlink.db'))
    conn.execute('CREATE TABLE IF NOT EXISTS db_info (Key TEXT, ValueStdStr TEXT)')
    conn.execute('CREATE TABLE IF NOT EXISTS dir2id (name TEXT, username TEXT)')
    conn.execute('CREATE TABLE IF NOT EXISTS image_hardlink_info_v4 '
                 '(md5 TEXT, file_name TEXT, dir1 INTEGER, dir2 INTEGER)')
    conn.execute('INSERT INTO db_info (Key, ValueStdStr) VALUES (?, ?)',
                 ('uuid', '1_' + 'x' * 20 + '_' + str(root)))
    # `dir2id` 的目录名列在真实库里叫 `username`（`_find_cached_thumbnail` 按这个名字查），
    # 而 `_resolve_hardlink_path` 用 `SELECT *` 取第一列 ⇒ 两个列名都建上、值一致。
    conn.execute('INSERT INTO dir2id (name, username) VALUES (?, ?)', ('h1', 'h1'))
    conn.execute('INSERT INTO dir2id (name, username) VALUES (?, ?)', ('d1', 'd1'))
    conn.execute('INSERT INTO image_hardlink_info_v4 (md5, file_name, dir1, dir2) '
                 'VALUES (?, ?, 1, 2)', (MD5, MD5 + '.dat'))
    conn.commit()
    conn.close()
    return str(dec), str(img)


def _write_cache_keys(dec, aes_key, xor_key):
    """把（可能已经被写坏的）密钥写进 `_media_keys.json` —— issue #46 的起点。"""
    payload = {'md5_keys': {MD5: {'aes_key': aes_key.hex(),
                                  'xor_key': '0x%02x' % xor_key}}}
    with open(os.path.join(dec, '_media_keys.json'), 'w', encoding='utf-8') as f:
        json.dump(payload, f)


def _counting_mmkv(keys, calls):
    """假的 MMKV 派生：只记"有没有被调用"，返回（通常为空的）密钥集。"""
    def _fake(decrypted_dir, wxid=None):
        calls.append(1)
        return dict(keys)
    return _fake


def _correcting_mmkv(dec, aes_key, xor_key, calls):
    """假的 MMKV 派生：像真实实现一样**把派生出来的 XOR 落盘纠正缓存**并返回密钥。"""
    def _fake(decrypted_dir, wxid=None):
        calls.append(1)
        v2._merge_into_cache(decrypted_dir, {MD5: aes_key}, xor_key=xor_key)
        return {MD5: aes_key}
    return _fake


class _FoundWithXor(dict):
    """模拟真实实现返回的 `FoundV2Keys`（`{md5: aes}` + 派生 XOR）。"""

    def __init__(self, *a, derived_xor=None, **kw):
        super().__init__(*a, **kw)
        self.derived_xor = derived_xor


def _client(dec):
    from web.app import create_app
    return create_app(dec, wxid='wxid_real_1a2b').test_client()


def _url_for(md5):
    return '/api/hardlink-media?md5=%s&type=3&path=%s' % (
        md5, 'msg/attach/h1/d1/Img/' + md5 + '.dat')


# ---------------------------------------------------------------------------
# 1. 三态函数的直接单测
# ---------------------------------------------------------------------------

class TestTriStateCompleteness:
    def test_a_jpeg_with_eoi_passes(self):
        assert _completeness(fake_jpeg(120)) is True

    def test_a_jpeg_missing_eoi_is_determinately_incomplete(self):
        assert _completeness(fake_jpeg(120, eoi=False)) is False

    def test_a_png_with_iend_passes(self):
        assert _completeness(fake_png(120)) is True

    def test_a_png_missing_iend_is_determinately_incomplete(self):
        assert _completeness(fake_png(120)[:-12]) is False

    def test_a_png_with_only_part_of_the_iend_is_incomplete(self):
        assert _completeness(fake_png(120)[:-1]) is False

    def test_a_gif_with_trailer_passes(self):
        assert _completeness(b'GIF89a' + b'\x00' * 60 + b'\x3b') is True

    def test_a_gif_missing_trailer_is_determinately_incomplete(self):
        assert _completeness(b'GIF89a' + b'\x00' * 60) is False

    def test_a_webp_is_undecidable(self):
        assert _completeness(fake_webp(120)) is None, \
            'WEBP 的"完整尾部"无法用一个固定 magic 判定 ⇒ 必须返回 None（不可判定），不能是 False'

    def test_a_bmp_is_undecidable(self):
        assert _completeness(fake_bmp(120)) is None

    def test_unknown_bytes_are_undecidable(self):
        assert _completeness(b'\x00' * 120) is None

    def test_no_head_at_all_is_undecidable(self):
        assert _completeness(b'') is None

    def test_too_short_to_judge_is_undecidable(self):
        """3 个字节的"JPEG 头"绝不能判成 False：那只是没写内容的头。"""
        assert _completeness(b'\xff\xd8\xff') is None
        assert _completeness(b'\xff\xd8') is None

    def test_a_path_can_be_judged_without_reading_the_whole_file(self, tmp_path):
        ok = tmp_path / 'ok.jpg'
        ok.write_bytes(fake_jpeg(5000))
        bad = tmp_path / 'bad.jpg'
        bad.write_bytes(fake_jpeg(5000, eoi=False))
        assert _completeness(str(ok)) is True
        assert _completeness(str(bad)) is False
        assert _completeness(str(tmp_path / 'missing.jpg')) is None

    def test_an_explicit_mime_is_honoured(self):
        """调用方已经嗅探过 MIME 时不必再嗅一遍（也证明 mime 参数真的被用了）。"""
        assert _completeness(fake_png(120), 'image/png') is True
        assert _completeness(fake_jpeg(120), 'image/jpeg') is True

    def test_the_undecidable_path_does_not_even_read_the_tail(self, tmp_path, monkeypatch):
        """`None` 这条路上**不许比旧行为多一次 IO**：不可判定类型连尾部都不该去读。"""
        p = tmp_path / 'a.webp'
        p.write_bytes(fake_webp(120))
        verdict = getattr(media, '_tail_verdict', None)
        assert verdict is not None, ('media._tail_verdict 尚未实现 —— 尾部判定要与"要不要读尾部"'
                                     '分开，才能让不可判定类型零额外 IO')
        seen = []
        monkeypatch.setattr(media, '_tail_verdict', lambda mime, tail: seen.append(mime))
        assert _completeness(str(p)) is None
        assert seen == [], 'WEBP 不可判定 ⇒ 连尾部都不该读（否则正常图片无故多一次 IO）'


# ---------------------------------------------------------------------------
# 2. 核心：缓存候选没通过校验 ⇒ 必须让后续步骤（MMKV 重新派生）接管
# ---------------------------------------------------------------------------

class TestACacheCandidateMustPassTheCheckBeforeItWins:
    @pytest.mark.parametrize('make_plain', [fake_jpeg, fake_png], ids=['jpeg', 'png'])
    def test_the_re_derived_key_gets_its_chance_and_the_complete_image_wins(
            self, tmp_path, monkeypatch, make_plain):
        plain = make_plain(300)
        dec, _img = _setup(tmp_path, plain)
        _write_cache_keys(dec, AES_KEY, WRONG_XOR)          # ← 已经被写坏的缓存
        calls = []
        monkeypatch.setattr(v2, 'extract_keys_from_mmkv',
                            _correcting_mmkv(dec, AES_KEY, DERIVED_XOR, calls))
        monkeypatch.setattr(v2, 'is_wechat_running', lambda: False)

        r = _client(dec).get(_url_for(MD5))

        assert calls, ('缓存候选没通过完整性校验时必须**继续走后续步骤**（MMKV 重新派生）—— '
                       '缓存命中即 return 正是 issue #46 把错 XOR 永久固化的原因')
        assert r.status_code == 200, f'期望 200，实际 {r.status_code}'
        assert r.data == plain, (
            '最终返回的必须是**通过校验的那张**（用派生 XOR 解出的完整图）；'
            '实际返回的字节以 %r 结尾 ⇒ 还是那张缺尾的坏图' % r.data[-2:])

    def test_the_skip_is_logged_so_the_chain_is_observable(self, tmp_path, monkeypatch, capsys):
        """这个缺陷活了这么久，正因为"跳过缓存、去重新派生"从外部看不出来。"""
        plain = fake_jpeg(300)
        dec, _img = _setup(tmp_path, plain)
        _write_cache_keys(dec, AES_KEY, WRONG_XOR)
        monkeypatch.setattr(v2, 'extract_keys_from_mmkv',
                            _correcting_mmkv(dec, AES_KEY, DERIVED_XOR, []))
        monkeypatch.setattr(v2, 'is_wechat_running', lambda: False)

        r = _client(dec).get(_url_for(MD5))
        out = capsys.readouterr().out

        assert r.status_code == 200
        assert '不完整' in out, '跳过缓存候选时必须打日志说明"解出的图不完整"'
        assert '缺 EOI' in out, '日志要说清是哪一种缺尾（JPEG 缺 EOI）'
        assert '继续' in out, '日志要说明后续步骤会继续尝试（而不是就此放弃）'

    def test_the_corrected_xor_is_what_makes_the_cache_selfheal(self, tmp_path, monkeypatch):
        """自愈的落点：Task 28 的"就地纠正缓存"分支必须真的被走到。"""
        plain = fake_jpeg(300)
        dec, _img = _setup(tmp_path, plain)
        _write_cache_keys(dec, AES_KEY, WRONG_XOR)
        monkeypatch.setattr(v2, 'extract_keys_from_mmkv',
                            _correcting_mmkv(dec, AES_KEY, DERIVED_XOR, []))
        monkeypatch.setattr(v2, 'is_wechat_running', lambda: False)

        r = _client(dec).get(_url_for(MD5))
        assert r.status_code == 200 and r.data == plain

        with open(os.path.join(dec, '_media_keys.json'), encoding='utf-8') as f:
            cached = json.load(f)['md5_keys'][MD5]['xor_key']
        assert int(cached, 16) == DERIVED_XOR, (
            '走完这一趟之后缓存里的错 XOR 必须被改成派生真值，否则下次请求仍然固化')


class TestTheMemoryPathIsTheOneThatActuallyRepairsTheCachedEntry:
    """⚠️ 复核发现（已写进报告）：**MMKV 那条路修不到"已经缓存但 XOR 是错的"条目**。

    `v2_key_extract.extract_keys_from_mmkv` 里：
        ``pending = {md5: v for md5, v in tasks.items() if md5 not in existing_md5s}``
    ⇒ 已经存在于 `_media_keys.json` 的 md5 **根本不进 pending**，也就不会出现在 `found_all` 里，
    `_merge_into_cache(..., xor_key=verified_xor)` 自然不会纠正它（而且 pending 为空时函数提前
    返回、连派生 XOR 都不记录）。

    真正既提供**正确 XOR**、又**就地纠正缓存**的是内存那条路：
    ``find_keys_for_files`` → ``_merge_into_cache(dec, found, xor_key=derived_xor)``
    —— 它对**被请求的 md5** 生效，与缓存里有没有它无关。本用例把这条真实路径钉住。
    """

    def test_memory_derived_key_wins_and_repairs_the_cache_in_place(self, tmp_path, monkeypatch):
        plain = fake_jpeg(300)
        dec, _img = _setup(tmp_path, plain)
        _write_cache_keys(dec, AES_KEY, WRONG_XOR)          # 已经被写坏的缓存
        monkeypatch.setattr(v2, 'extract_keys_from_mmkv', _counting_mmkv({}, []))
        monkeypatch.setattr(v2, 'is_wechat_running', lambda: True)

        def _fake_find(decrypted_dir, wxid, md5s, print_fn=None, account_xor=None):
            if MD5 not in md5s:
                return _FoundWithXor()
            # 与真实实现一致：拿到键就按派生 XOR 合并进缓存（就地纠正），并带出 derived_xor
            v2._merge_into_cache(decrypted_dir, {MD5: AES_KEY}, xor_key=DERIVED_XOR)
            return _FoundWithXor({MD5: AES_KEY}, derived_xor=DERIVED_XOR)

        monkeypatch.setattr(v2, 'find_keys_for_files', _fake_find)

        r = _client(dec).get(_url_for(MD5))

        assert r.status_code == 200
        assert r.data == plain, ('内存路径给出正确 XOR 时，它解出的**完整**图必须胜出；'
                                 '实际仍是那张缺 EOI 的坏图（bytes 尾部 %r）' % r.data[-2:])
        with open(os.path.join(dec, '_media_keys.json'), encoding='utf-8') as f:
            cached = json.load(f)['md5_keys'][MD5]['xor_key']
        assert int(cached, 16) == DERIVED_XOR, '内存路径必须把缓存里那个错 XOR 就地改成派生真值'



# ---------------------------------------------------------------------------
# 3. 【不许 404 的钉子】所有候选都不通过校验时，仍然返回那张坏候选
# ---------------------------------------------------------------------------

class TestNeverRefusesServiceWhenEverythingIsIncomplete:
    def test_the_incomplete_cached_image_is_still_served_with_200(self, tmp_path, monkeypatch):
        plain = fake_jpeg(300)
        dec, _img = _setup(tmp_path, plain)
        _write_cache_keys(dec, AES_KEY, WRONG_XOR)
        monkeypatch.setattr(v2, 'extract_keys_from_mmkv', _counting_mmkv({}, []))
        monkeypatch.setattr(v2, 'is_wechat_running', lambda: False)

        r = _client(dec).get(_url_for(MD5))

        # —— 机制钉子（改动前也绿）：宁可给一张坏的，也不许不给 ——
        assert r.status_code == 200, ('所有候选都缺尾时也**不许** 404/415/500 —— '
                                      '真实缓存里 2/28 个 JPEG 就是"源图本身缺尾"')
        assert r.data == _wrong_xor_bytes(plain), '必须回退返回那个（唯一）候选，而不是空响应'

        # —— RED（改动前没有这两个可观测面）——
        assert r.headers.get('X-WeChat-Image-Completeness') == 'incomplete', \
            '回退返回未通过校验的图时，响应上必须看得出来（取证用）'

    def test_the_fallback_is_logged_and_the_skip_really_happened(self, tmp_path, monkeypatch,
                                                                 capsys):
        plain = fake_jpeg(300)
        dec, _img = _setup(tmp_path, plain)
        _write_cache_keys(dec, AES_KEY, WRONG_XOR)
        calls = []
        monkeypatch.setattr(v2, 'extract_keys_from_mmkv', _counting_mmkv({}, calls))
        monkeypatch.setattr(v2, 'is_wechat_running', lambda: False)

        r = _client(dec).get(_url_for(MD5))
        out = capsys.readouterr().out

        assert r.status_code == 200
        assert calls, '缓存候选缺尾后必须继续尝试后续步骤（哪怕它们最后都没给出更好的）'
        assert '不完整' in out, '跳过缓存候选时要打日志说明"解出的图不完整"'
        assert '缺 EOI' in out, '日志要说清是哪一种缺尾（JPEG 缺 EOI）'
        assert '回退' in out and '未通过完整性校验' in out, \
            '最终只能回退到未通过校验的候选时，也必须有一条日志说明'


class TestAnIncompleteSourceImageIsNotAFailure:
    """假阳性钉子：**源图本身就被截断**的 JPEG（真实缓存里 2/28=7%）必须照样显示。

    这时即使拿着**正确**的 XOR 解出来也缺 EOI ⇒ 校验判 False。绝不允许把它当成
    "这张图不能给用户"：链走完仍要原样返回它。
    """

    def test_a_genuinely_truncated_jpeg_is_still_served_byte_for_byte(self, tmp_path, monkeypatch,
                                                                     capsys):
        plain = fake_jpeg(300, eoi=False)          # 源图自己就没有 EOI
        dec, _img = _setup(tmp_path, plain)
        _write_cache_keys(dec, AES_KEY, DERIVED_XOR)   # 缓存里的值**是对的**
        calls = []
        monkeypatch.setattr(v2, 'extract_keys_from_mmkv', _counting_mmkv({}, calls))
        monkeypatch.setattr(v2, 'is_wechat_running', lambda: False)

        r = _client(dec).get(_url_for(MD5))
        out = capsys.readouterr().out

        # —— 机制钉子 ——
        assert r.status_code == 200, '源图本身缺尾 ≠ 不能给用户'
        assert r.data == plain, '回退返回的必须是原样解出来的字节（不许改写、不许截断）'

        # —— RED ——
        assert calls, '可判定类型且确认缺尾 ⇒ 要暂存并让后续步骤有机会给出更好的'
        assert '回退' in out and '未通过完整性校验' in out


# ---------------------------------------------------------------------------
# 4. 【未知类型不许被当成失败】WEBP / BMP ⇒ 立刻返回
# ---------------------------------------------------------------------------

class TestUndecidableTypesAreNotTreatedAsFailures:
    @pytest.mark.parametrize('make_plain', [fake_webp, fake_bmp], ids=['webp', 'bmp'])
    def test_an_undecidable_type_is_served_immediately_and_unchanged(self, tmp_path, monkeypatch,
                                                                     capsys, make_plain):
        """`None` = 不可判定 ⇒ 必须**保持现状**：立刻返回，既不跳过也不额外变慢。"""
        plain = make_plain(300)
        dec, _img = _setup(tmp_path, plain)
        _write_cache_keys(dec, AES_KEY, DERIVED_XOR)   # 缓存里的值是对的
        calls = []
        monkeypatch.setattr(v2, 'extract_keys_from_mmkv', _counting_mmkv({}, calls))
        monkeypatch.setattr(v2, 'is_wechat_running', lambda: False)

        r = _client(dec).get(_url_for(MD5))
        out = capsys.readouterr().out

        assert r.status_code == 200
        assert r.data == plain, '不可判定类型的图必须原样返回'
        assert not calls, ('WEBP/BMP 尾部无法用一个 magic 判定 ⇒ 不许当成"缺尾"而多走后续步骤'
                           '（那会让绝大多数正常图片无故变慢）')
        assert '不完整' not in out, '不可判定的类型不许打"不完整"的日志'
        assert 'X-WeChat-Image-Completeness' not in r.headers, \
            '不可判定 ≠ 未通过校验：不该给它挂"incomplete"这个取证标记'

    @pytest.mark.parametrize('make_plain', [fake_webp, fake_bmp], ids=['webp', 'bmp'])
    def test_an_undecidable_candidate_does_not_even_run_the_check(self, tmp_path, monkeypatch,
                                                                  make_plain):
        """更强的一条：不可判定类型连 `_image_completeness` 都不该被调用
        ⇒ 这条路上是**零额外 IO**（不是"少读一点"），与旧行为逐字一致。"""
        plain = make_plain(300)
        dec, _img = _setup(tmp_path, plain)
        _write_cache_keys(dec, AES_KEY, DERIVED_XOR)
        monkeypatch.setattr(v2, 'extract_keys_from_mmkv', _counting_mmkv({}, []))
        monkeypatch.setattr(v2, 'is_wechat_running', lambda: False)

        check = getattr(media, '_image_completeness', None)
        assert check is not None, 'media._image_completeness 尚未实现'
        seen = []
        monkeypatch.setattr(media, '_image_completeness',
                            lambda *a, **k: seen.append(a) or check(*a, **k))

        r = _client(dec).get(_url_for(MD5))

        assert r.status_code == 200 and r.data == plain
        assert seen == [], ('不可判定类型不许被校验（哪怕只是多一次 getsize/打开文件）：'
                            '任务书要求这条路上"保持现状、不许因此变慢"')


# ---------------------------------------------------------------------------
# 5. 多个候选都缺尾时返回哪一个（规则必须被钉住）
# ---------------------------------------------------------------------------

class TestWhichIncompleteCandidateIsReturned:
    """规则：**当前大小最大**的那个；平手时取候选链上先出现的。

    理由：这些都已被判定"缺尾"，用户能看到的内容只可能与**已经解出的像素量**正相关
    ⇒ 原图（大）比缩略图（小）更接近他的预期；反过来（拿小的）会让用户看到的内容
    比改动前**更少**，那是纯粹的退步。

    ⚠️ 诚实说明：在本仓库真实的候选链上「按链顺序取第一个」与「按大小取最大」**恒等价**
    （链本来就是 原图 → 缩略图 递减排列）。下面的单测用**人工构造的乱序候选表**把规则本身
    钉死，使它不依赖候选链的顺序恰好与大小同序。
    """

    def test_the_picker_prefers_the_larger_one_even_when_it_comes_later(self, tmp_path):
        small = tmp_path / 'small.dec'
        small.write_bytes(b'x' * 100)
        big = tmp_path / 'big.dec'
        big.write_bytes(b'y' * 300)
        pick = getattr(media, '_pick_incomplete_candidate', None)
        assert pick is not None, 'media._pick_incomplete_candidate 尚未实现（回退候选的选择规则）'

        cands = [{'path': str(small), 'mime': 'image/jpeg', 'why': 'a', 'source_tag': None},
                 {'path': str(big), 'mime': 'image/jpeg', 'why': 'b', 'source_tag': None}]
        assert pick(cands)['path'] == str(big), \
            '规则是"最大者胜出"（与候选链顺序无关）；取最小 = 让用户看到的内容比改动前更少'
        assert pick(list(reversed(cands)))['path'] == str(big), '反过来也一样'
        assert pick([]) is None, '没有候选时返回 None（调用方据此走原有的 415 诊断路径）'

    def test_the_picker_skips_paths_that_no_longer_exist(self, tmp_path):
        gone = tmp_path / 'gone.dec'
        pick = getattr(media, '_pick_incomplete_candidate', None)
        assert pick is not None
        assert pick([{'path': str(gone), 'mime': 'image/jpeg', 'why': 'a'}]) is None, \
            '暂存的是路径；文件在两次请求之间被清掉时不许抛异常（那会变成 500）'

    def test_the_largest_stashed_candidate_wins(self, tmp_path, monkeypatch, capsys):
        plain = fake_jpeg(300)                      # 原图（大）
        thumb_plain = fake_jpeg(120)                # WeChat 生成的 _t 缩略图（小）
        dec, img = _setup(tmp_path, plain)
        _write_cache_keys(dec, AES_KEY, WRONG_XOR)  # 两张图都会被同一个错 XOR 解坏
        build_v2_dat(os.path.join(img, f'{MD5}_t.dat'), thumb_plain, AES_KEY, DERIVED_XOR,
                     aes_size=100)
        monkeypatch.setattr(v2, 'extract_keys_from_mmkv', _counting_mmkv({}, []))
        monkeypatch.setattr(v2, 'is_wechat_running', lambda: False)

        r = _client(dec).get(_url_for(MD5))
        out = capsys.readouterr().out

        assert r.status_code == 200
        assert '源=thumbnail' in out, ('同目录的 _t 缩略图候选根本没被考虑（没有一个"源=thumbnail"的'
                                       '暂存日志）—— 本用例就钉不住"多个候选都缺尾时选哪个"')
        assert r.data == _wrong_xor_bytes(plain), (
            '所有候选都缺尾时返回的应是**最大**的那个（原图 %d 字节），'
            '而不是缩略图（%d 字节）' % (len(plain), len(thumb_plain)))


# ---------------------------------------------------------------------------
# 6. 【必须写进报告的可见差异】那 7% 的情况下用户看到的东西会变
# ---------------------------------------------------------------------------

class TestTheVisibleDifferenceWhenTheOriginalIsIncomplete:
    """⚠️ 这是本变更**唯一**的、会让用户直接看到的副作用，必须显式钉住：

    缓存候选（原图）被判"缺尾"而被跳过后，后续的**缩略图兜底**可能给出一张
    **通过校验但更小**的图 ⇒ 在"源图本身就被截断"的那 7% 情形里，用户从
    **"大图但只有上面一部分正常"** 变成 **"小图但完整"**（反过来则是"小图 → 清晰的大坏图"）。

    改动前：缓存命中即返回 ⇒ 永远是那张大的坏图。
    """

    def test_a_complete_smaller_thumbnail_now_beats_the_incomplete_original(self, tmp_path,
                                                                           monkeypatch):
        plain = fake_jpeg(300)              # 原图（大）：被错 XOR 解坏 ⇒ 缺 EOI
        thumb_bytes = fake_jpeg(120)        # WeChat 自己的缩略图缓存（小）：完整
        dec, _img = _setup(tmp_path, plain)
        _write_cache_keys(dec, AES_KEY, WRONG_XOR)
        thumb_dir = (tmp_path / 'xwechat_files' / 'wxid_real_1a2b' / 'cache' / '1'
                     / 'Message' / 'h1' / 'Thumb')
        thumb_dir.mkdir(parents=True, exist_ok=True)
        (thumb_dir / '42_1_thumb.jpg').write_bytes(thumb_bytes)
        monkeypatch.setattr(v2, 'extract_keys_from_mmkv', _counting_mmkv({}, []))
        monkeypatch.setattr(v2, 'is_wechat_running', lambda: False)

        r = _client(dec).get(_url_for(MD5) + '&local_id=42')

        assert r.status_code == 200
        assert r.data == thumb_bytes, (
            '完整的缩略图（%d 字节）现在必须胜出，而不是那张缺 EOI 的大图（%d 字节）'
            '—— 这就是本变更在 7%% 情形下的可见差异（图变小了，但完整了）'
            % (len(thumb_bytes), len(plain)))
        assert r.headers.get('X-WeChat-Image-Completeness') is None, \
            '通过校验的候选不该带"未校验"标记'

