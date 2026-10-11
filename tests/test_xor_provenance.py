"""P0-2（GitHub issue #16 新评论）：把"这把密钥是哪来的"变成看得见的东西。

报告者的日志只能看到他期望之外的现象：

    [media] Loaded 6 verified keys from cache (xor from cache for all 3)
    [V2] 候选未通过完整性校验（解出的图不完整：PNG 缺 IEND 块）… 暂存为兜底并继续
    [mmkv] Cannot determine wxid — skipping MMKV extraction
    （然后 70 秒没有任何输出）
    [V2] 所有候选都未通过完整性校验（暂存 5 个），回退返回其中最大的一个… —— 不 404

三处不可观测：
  1. `(xor from cache for all 3)` —— **老版本写死的 0xC9** 与"派生真值恰好是 0xC9"
     在缓存里长得一模一样，读侧无法区分；
  2. 内存密钥路径（唯一可能跑几十秒的一步）**完全静默**：`find_keys_for_files`
     有日志，但调用点没把 `print_fn` 传进去（函数内默认是个空函数）；
  3. 每张坏图只报"不完整"，不说**用的是哪把 XOR、它从哪来**。

本文件钉住的新语义
------------------
1. `_media_keys.json` 的每条记录都带 `xor_src`（`derived` / `repaired` / `default`）；
   老缓存没有这个字段 ⇒ 读侧记为 `legacy`，**不假设**它是哪一种，并打出来；
2. 读侧日志按来源分类打点（`xor_src: legacy=3` 这种），并对 `legacy` 给出可操作建议；
3. 内存路径必须**有开始/结束与耗时**，并把它自己的日志接出来；
4. "图不完整"的日志要带上**密钥来源**（`cache:legacy` / `memory` / `thumbnail`…）。

全部**合成**：自造 V2 `.dat`、自造 hardlink.db、自造缓存；不碰本机真实数据。
"""
import json
import os
import sqlite3
import struct

import pytest

from engine.services import media
from engine.services import v2_key_extract as v2

MD5 = 'a' * 32
# 本文件的主密钥刻意取成"由 code + 账号 id 派生出来的那一把"，
# 这样"离线修复通道"的归属判定（aes_key 必须与本次验证过的账号密钥一致）才成立。
CODE = 0x13C
AES_KEY = (__import__('hashlib').md5((str(CODE) + 'wxid_demo_1a2b').encode())
           .hexdigest()[:16].encode('ascii'))
XOR = 0xC9
AES_SIZE = 200


def build_v2_dat(path, plaintext, aes_key, xor_key, aes_size=AES_SIZE):
    from Crypto.Cipher import AES
    from Crypto.Util import Padding

    xor_size = len(plaintext) - aes_size
    cipher = AES.new(aes_key[:16], AES.MODE_ECB)
    aes_seg = cipher.encrypt(Padding.pad(plaintext[:aes_size], 16))
    tail_enc = bytes(b ^ xor_key for b in plaintext[aes_size:])
    header = b'\x07\x08V2\x08\x07' + struct.pack('<II', aes_size, xor_size) + b'\x00'
    with open(path, 'wb') as f:
        f.write(header + aes_seg + tail_enc)
    return path


def fake_jpeg(size=300, eoi=True):
    body = b'\xff\xd8\xff\xe0' + bytes((i * 7) % 251 for i in range(size - 6))
    return body + b'\xff\xd9' if eoi else body


def _setup(tmp_path, plain=None):
    plain = plain if plain is not None else fake_jpeg(300)
    root = tmp_path / 'xwechat_files'
    img = root / 'wxid_demo_1a2b' / 'msg' / 'attach' / 'h1' / 'd1' / 'Img'
    img.mkdir(parents=True, exist_ok=True)
    build_v2_dat(str(img / f'{MD5}.dat'), plain, AES_KEY, XOR)

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
    conn.execute('INSERT INTO image_hardlink_info_v4 (md5, file_name, dir1, dir2) '
                 'VALUES (?, ?, 1, 2)', (MD5, MD5 + '.dat'))
    conn.commit()
    conn.close()
    return str(dec), plain


def _write_cache(dec, entry):
    with open(os.path.join(dec, '_media_keys.json'), 'w', encoding='utf-8') as f:
        json.dump({'md5_keys': {MD5: entry}}, f, indent=2)


def _read_entry(dec):
    with open(os.path.join(dec, '_media_keys.json'), encoding='utf-8') as f:
        return json.load(f)['md5_keys'][MD5]


def _url(md5=MD5):
    return ('/api/hardlink-media?md5=%s&type=3&path=%s'
            % (md5, 'msg/attach/h1/d1/Img/' + md5 + '.dat'))


def _client(dec):
    from web.app import create_app
    return create_app(dec, wxid='wxid_demo_1a2b').test_client()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(media, '_FALLBACK_STORAGE_ROOTS', (), raising=False)
    monkeypatch.setattr(media, '_IMAGE_KEY_MAP', {}, raising=False)
    monkeypatch.setattr(media, '_IMAGE_KEY_MAP_DIR', None, raising=False)
    monkeypatch.setattr(v2, '_DERIVED_XOR_BY_DIR', {}, raising=False)
    monkeypatch.setattr(v2, '_get_wechat_pids', lambda: [], raising=False)


# ---------------------------------------------------------------------------
# A. 写入侧：来源必须落盘
# ---------------------------------------------------------------------------

class TestWriteSideRecordsProvenance:
    def test_derived_value_is_marked_derived(self, tmp_path):
        dec = str(tmp_path)
        v2._merge_into_cache(dec, {MD5: AES_KEY}, xor_key=0x3C)
        entry = _read_entry(dec)
        assert entry['xor_key'] == '0x3c'
        assert entry['xor_src'] == 'derived', \
            '派生真值必须标明来源，否则读侧无法与"老版本写死的默认值"区分'

    def test_no_derived_value_is_marked_default(self, tmp_path):
        dec = str(tmp_path)
        v2._merge_into_cache(dec, {MD5: AES_KEY})
        entry = _read_entry(dec)
        assert entry['xor_key'] == '0xc9'
        assert entry['xor_src'] == 'default'

    def test_repair_channel_marks_repaired(self, tmp_path, monkeypatch):
        """离线修复通道写下的值要标成 `repaired`（与"内存发现"区分开）。"""
        dec = str(tmp_path)
        _write_cache(dec, {'aes_key': AES_KEY.hex(), 'xor_key': '0xc9'})
        monkeypatch.setattr(v2, '_scan_mmkv_kvcomm_dirs',
                            lambda: [str(tmp_path / 'kvcomm')])
        (tmp_path / 'kvcomm').mkdir()
        (tmp_path / 'kvcomm' / f'key_{CODE}_deadbeef.statistic').write_bytes(b'x')
        monkeypatch.setattr(v2, '_load_v2_ciphertexts',
                            lambda d, w: {MD5: ('x.dat', b'\x11' * 16)})
        monkeypatch.setattr(v2, '_try_key', lambda key, ct: 'JPEG')

        v2.extract_keys_from_mmkv(dec, 'wxid_demo_1a2b')

        entry = _read_entry(dec)
        assert entry['xor_key'] == '0x3c'
        assert entry['xor_src'] == 'repaired'

    def test_an_old_entry_gets_a_source_once_it_is_rewritten(self, tmp_path):
        """老条目（没有 xor_src）被纠正时，来源必须补上 —— 不能永远"来源不明"。"""
        dec = str(tmp_path)
        _write_cache(dec, {'aes_key': AES_KEY.hex(), 'xor_key': '0xc9'})
        v2._merge_into_cache(dec, {MD5: AES_KEY}, xor_key=0x3C)
        assert _read_entry(dec)['xor_src'] == 'derived'


# ---------------------------------------------------------------------------
# B. 读侧：按来源分类 + 对"来源不明"给出可操作建议
# ---------------------------------------------------------------------------

class TestReadSideClassifiesProvenance:
    def test_legacy_entry_is_reported_as_legacy_not_assumed(self, tmp_path, capsys):
        dec = str(tmp_path)
        _write_cache(dec, {'aes_key': AES_KEY.hex(), 'xor_key': '0xc9'})   # 老缓存
        media._load_or_build_image_key_map(dec)
        out = capsys.readouterr().out

        assert 'xor_src: legacy=1' in out, \
            '老缓存没有 xor_src ⇒ 必须报 "legacy"，不许假设它是派生值'
        assert '来源不明' in out
        assert 'harvest-keys' in out, '要给出可操作的建议（报告者正是不知道该做什么）'

    def test_derived_entry_is_reported_as_derived(self, tmp_path, capsys):
        dec = str(tmp_path)
        _write_cache(dec, {'aes_key': AES_KEY.hex(), 'xor_key': '0xc9',
                           'xor_src': 'derived'})
        media._load_or_build_image_key_map(dec)
        out = capsys.readouterr().out

        assert 'xor_src: derived=1' in out
        assert '来源不明' not in out, '来源清楚的时候不该喊狼来了'

    def test_missing_field_contract_is_kept(self, tmp_path, capsys):
        """既有契约不变：缺字段 ⇒ 默认值 + `missing` + `NOT a derived value`。"""
        dec = str(tmp_path)
        _write_cache(dec, {'aes_key': AES_KEY.hex()})
        entry = media._load_or_build_image_key_map(dec)[MD5]
        out = capsys.readouterr().out

        assert entry['xor'] == media._DAT_V2_DEFAULT_XOR
        assert 'missing' in out and 'NOT a derived value' in out and 'default 0xC9' in out
        assert 'xor_src: default-missing=1' in out


# ---------------------------------------------------------------------------
# C. 内存路径：不许再静默（70 秒盲区）
# ---------------------------------------------------------------------------

class TestMemoryPathIsNoLongerSilent:
    def test_print_fn_is_handed_in_and_start_end_are_logged(self, tmp_path, monkeypatch,
                                                            capsys):
        # 源图尾部缺失 ⇒ 门禁判 False ⇒ 链路继续，才会走到内存路径
        dec, _plain = _setup(tmp_path, plain=fake_jpeg(300, eoi=False))
        _write_cache(dec, {'aes_key': AES_KEY.hex(), 'xor_key': '0xc9',
                           'xor_src': 'legacy'})          # 坏的缓存 ⇒ 闸门会继续往下走
        monkeypatch.setattr(v2, 'extract_keys_from_mmkv', lambda d, w=None: {})
        monkeypatch.setattr(v2, 'is_wechat_running', lambda: True)

        seen = {}

        def _fake_find(d, w, md5s, print_fn=None, account_xor=None):
            seen['print_fn'] = print_fn
            if print_fn:
                print_fn('  [v2_key] (合成) 正在扫描内存……')
            return {}

        monkeypatch.setattr(v2, 'find_keys_for_files', _fake_find)

        r = _client(dec).get(_url(MD5))
        out = capsys.readouterr().out

        assert r.status_code == 200
        assert seen.get('print_fn') is not None, \
            '内存路径的日志函数必须传进去（以前没传 ⇒ 全程静默，用户只看到"异常漫长"）'
        assert '(合成) 正在扫描内存' in out, '它的日志必须真的出现在用户能看到的输出里'
        assert '内存密钥路径：开始扫描' in out and '内存密钥路径：结束' in out, \
            '必须有一头一尾（含耗时），否则"卡在哪一步"永远查不出来'

    def test_wechat_not_running_says_so_instead_of_staying_silent(self, tmp_path, monkeypatch,
                                                                  capsys):
        dec, _plain = _setup(tmp_path, plain=fake_jpeg(300, eoi=False))
        _write_cache(dec, {'aes_key': AES_KEY.hex(), 'xor_key': '0xc9'})
        monkeypatch.setattr(v2, 'extract_keys_from_mmkv', lambda d, w=None: {})
        monkeypatch.setattr(v2, 'is_wechat_running', lambda: False)

        r = _client(dec).get(_url(MD5))
        out = capsys.readouterr().out

        assert r.status_code == 200
        assert '微信未运行' in out, '跳过内存路径也要说一句（静默正是本缺陷的一部分）'


# ---------------------------------------------------------------------------
# D. 核心场景：坏缓存 + 两条修复通道都够不到（= 报告者的现场）
# ---------------------------------------------------------------------------

class TestTheReportersScenarioIsNowSelfExplaining:
    def test_the_incomplete_image_log_names_the_key_source(self, tmp_path, monkeypatch,
                                                           capsys):
        """源图尾部缺失 ⇒ 门禁判 False ⇒ 走完整条链 ⇒ 回退 200 + 不完整。

        用户该看到的三件事：① 用的是哪把密钥、从哪来；② 后续每一步为什么也没救回来；
        ③ 最终还是给图（不 404）。本用例钉住 ① 和 ③。
        """
        dec, plain_without_eoi = _setup(tmp_path, plain=fake_jpeg(300, eoi=False))
        _write_cache(dec, {'aes_key': AES_KEY.hex(), 'xor_key': '0xc9'})   # legacy
        monkeypatch.setattr(v2, 'extract_keys_from_mmkv', lambda d, w=None: {})
        monkeypatch.setattr(v2, 'is_wechat_running', lambda: False)

        r = _client(dec).get(_url(MD5))
        out = capsys.readouterr().out

        assert r.status_code == 200, '绝不 404：给不了好图也要给图'
        assert r.headers.get('X-WeChat-Image-Completeness') == 'incomplete'
        assert '候选未通过完整性校验' in out
        assert '密钥来源=cache:legacy' in out, \
            '必须说出"这张坏图是用**来源不明**的缓存密钥解的" —— 报告者当时只能猜'

    def test_the_fallback_line_also_carries_the_key_source(self, tmp_path, monkeypatch,
                                                           capsys):
        dec, _plain = _setup(tmp_path, plain=fake_jpeg(300, eoi=False))
        _write_cache(dec, {'aes_key': AES_KEY.hex(), 'xor_key': '0xc9',
                           'xor_src': 'repaired'})
        monkeypatch.setattr(v2, 'extract_keys_from_mmkv', lambda d, w=None: {})
        monkeypatch.setattr(v2, 'is_wechat_running', lambda: False)

        _client(dec).get(_url(MD5))
        out = capsys.readouterr().out
        assert '密钥来源=cache:repaired' in out
