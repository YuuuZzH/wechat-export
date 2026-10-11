"""P0-1（GitHub issue #16 新评论）：账号 id 不再按 `wxid_` 前缀猜名字。

问题（报告者现场）
------------------
`extract_keys_from_mmkv` / `harvest_v2_keys` / 数据库 MMKV 策略都写着一句
`if not wxid.startswith('wxid_') : 跳过`，而它们拿到的值来自**账号目录名**
（`app.config['WXID']` ← `resolve_account_dir()`）。微信 4.x 的目录名有三种形态：

    wxid_ab12cd34ef56_68f8   ← 裸 id + 4 位十六进制后缀
    wxid_ab12cd34ef56        ← 就是裸 id
    myalias_68f8             ← **自定义微信号**（不带 `wxid_` 前缀）

第三种机器上，这一句会把**唯一**能"微信没在跑也自愈"的通道整段掐死 ——
报告者的日志正是 `[mmkv] Cannot determine wxid — skipping MMKV extraction`。

修法
----
1. `engine.utils.account_id_candidates()`：把"可能是目录名 / 裸 id"的值展开成
   **一组候选**（原值 → `bare_wxid` → 去 `_<4hex>` 后缀，含两段式）；
2. `v2_key_extract._account_id_candidates()`：把"给的值 / `.wxid` / 目录名 /
   hardlink.db 推出的账号目录"全部收进来再展开；
3. **由真文件验证取胜**（`_try_key` 对真实 V2 `.dat` 的 AES 段）——
   候选只是候选，验证不过就不算数（所以多给候选**不会**降低安全性）。

本文件全部**合成**：自造账号目录（名字不带 `wxid_` 前缀）、自造 V2 `.dat`、
自造 hardlink.db、自造 MMKV statistic 文件名；`_FALLBACK_STORAGE_ROOTS` 置空、
`_scan_mmkv_kvcomm_dirs` 缝到合成目录 ⇒ **绝不探测本机真实微信数据**。
"""
import hashlib
import json
import os
import sqlite3
import struct

import pytest

from engine.services import media
from engine.services import v2_key_extract as v2
from engine.utils import account_id_candidates, bare_wxid, strip_account_dir_suffix

# 账号目录名：**自定义微信号** + 4 位十六进制后缀（报告者那种形态）
ALIAS = 'myalias'
ACCOUNT_DIR = 'myalias_68f8'
# 对照：`wxid_` 形态（旧代码唯一认的那种）
WXID_DIR = 'wxid_ab12cd34ef56_68f8'

CODE = 0x13C
DERIVED_XOR = 0x3C          # = CODE & 0xFF
WRONG_XOR = 0xC9            # 老版本写死的默认值
MD5 = 'a' * 32
AES_SIZE = 200


def derive_aes_key(code, account_id):
    """py_wx_key 的派生式，**独立实现**（不调用被测代码，避免自证）。"""
    return hashlib.md5((str(code) + account_id).encode('utf-8')).hexdigest()[:16].encode('ascii')


AES_KEY = derive_aes_key(CODE, ALIAS)


def build_v2_dat(path, plaintext, aes_key, xor_key, aes_size=AES_SIZE):
    """按真实 V2 布局造一个 `.dat`（与其它测试文件的合成器同构，不 import 以免耦合）。"""
    from Crypto.Cipher import AES
    from Crypto.Util import Padding

    assert aes_size % 16 != 0
    xor_size = len(plaintext) - aes_size
    assert xor_size > 0

    cipher = AES.new(aes_key[:16], AES.MODE_ECB)
    aes_seg = cipher.encrypt(Padding.pad(plaintext[:aes_size], 16))
    tail_enc = bytes(b ^ xor_key for b in plaintext[aes_size:])
    header = b'\x07\x08V2\x08\x07' + struct.pack('<II', aes_size, xor_size) + b'\x00'
    with open(path, 'wb') as f:
        f.write(header + aes_seg + tail_enc)
    return path


def fake_jpeg(size=300):
    return b'\xff\xd8\xff\xe0' + bytes((i * 7) % 251 for i in range(size - 6)) + b'\xff\xd9'


def _setup(tmp_path, account_dir=ACCOUNT_DIR, aes_key=AES_KEY, plain=None):
    """造 <tmp>/xwechat_files/<account_dir>/msg/attach/h1/d1/Img/<md5>.dat 与 hardlink.db。"""
    plain = plain if plain is not None else fake_jpeg(300)
    root = tmp_path / 'xwechat_files'
    img = root / account_dir / 'msg' / 'attach' / 'h1' / 'd1' / 'Img'
    img.mkdir(parents=True, exist_ok=True)
    build_v2_dat(str(img / f'{MD5}.dat'), plain, aes_key, DERIVED_XOR)

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
    return str(dec), str(img), plain


def _synthetic_mmkv(monkeypatch, tmp_path, *codes):
    d = tmp_path / 'kvcomm'
    d.mkdir(parents=True, exist_ok=True)
    for code in codes:
        (d / f'key_{code}_deadbeef.statistic').write_bytes(b'MMKV\x00synthetic')
    monkeypatch.setattr(v2, '_scan_mmkv_kvcomm_dirs', lambda: [str(d)])
    return d


def _write_broken_cache(dec, aes_key=AES_KEY, xor=WRONG_XOR):
    payload = {'md5_keys': {MD5: {'aes_key': aes_key.hex(), 'xor_key': '0x%02x' % xor}}}
    with open(os.path.join(dec, '_media_keys.json'), 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2)


def _cache_xor(dec):
    with open(os.path.join(dec, '_media_keys.json'), encoding='utf-8') as f:
        return int(json.load(f)['md5_keys'][MD5]['xor_key'], 16)


def _memory_path_must_not_be_used(*args, **kwargs):
    raise AssertionError('本文件的前提是"微信没在运行"：内存路径绝不该被走到')


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """把一切"本机真实环境"的入口缝掉（与同类测试文件同一套纪律）。"""
    monkeypatch.setattr(v2, '_get_wechat_pids', lambda: [])
    monkeypatch.setattr(v2, 'find_keys_for_files', _memory_path_must_not_be_used)
    monkeypatch.setattr(media, '_FALLBACK_STORAGE_ROOTS', (), raising=False)
    monkeypatch.setattr(media, '_IMAGE_KEY_MAP', {}, raising=False)
    monkeypatch.setattr(media, '_IMAGE_KEY_MAP_DIR', None, raising=False)
    monkeypatch.setattr(v2, '_DERIVED_XOR_BY_DIR', {}, raising=False)


# ---------------------------------------------------------------------------
# A. 候选展开规则（纯函数，先钉住语义）
# ---------------------------------------------------------------------------

class TestAccountIdCandidates:
    def test_two_part_alias_dir_is_expanded(self):
        """两段式 `<自定义微信号>_68f8` 以前两个归一化器都剥不掉。"""
        assert strip_account_dir_suffix(ACCOUNT_DIR) == ALIAS
        assert bare_wxid(ACCOUNT_DIR) == ACCOUNT_DIR, \
            '`bare_wxid` 的"≥3 段"是**比较用**语义，不许在这里被放宽（它被测试锁住）'
        assert account_id_candidates(ACCOUNT_DIR) == [ACCOUNT_DIR, ALIAS], \
            '必须同时给出原值与被剥掉后缀的形态，交给真数据验证去选'

    def test_wxid_shaped_dir_keeps_the_old_result(self):
        assert account_id_candidates(WXID_DIR) == [WXID_DIR, 'wxid_ab12cd34ef56']

    def test_already_bare_id_is_returned_alone(self):
        assert account_id_candidates('wxid_ab12cd34ef56') == ['wxid_ab12cd34ef56']

    def test_empty_and_none_yield_no_candidates(self):
        assert account_id_candidates(None) == []
        assert account_id_candidates('') == []
        assert account_id_candidates('   ') == []

    def test_candidates_are_deduped_and_ordered(self):
        got = account_id_candidates('wxid_ab12cd34ef56_68f8')
        assert got == list(dict.fromkeys(got)), '不许有重复项'
        assert got[0] == 'wxid_ab12cd34ef56_68f8', '原值必须排第一（最可信）'


# ---------------------------------------------------------------------------
# B. 核心 RED：自定义微信号目录名下，离线 MMKV 通道必须仍然能自愈
# ---------------------------------------------------------------------------

class TestOfflineSelfHealWithACustomAliasAccount:
    def test_the_old_guard_would_have_skipped_this_account_name(self):
        """钉子：把"旧守卫"的判据重演一遍，证明这个账号名以前必然被跳过。"""
        assert not ACCOUNT_DIR.startswith('wxid_')
        assert v2._clean_wxid(ACCOUNT_DIR) == ACCOUNT_DIR, \
            '`_clean_wxid` 对不带 `wxid_` 前缀的名字原样返回 ⇒ 旧守卫必然判 False'

    def test_candidates_cover_the_real_account_id(self, tmp_path, monkeypatch):
        dec, _img, _plain = _setup(tmp_path)
        cands, raws = v2._account_id_candidates(dec, ACCOUNT_DIR)
        assert ALIAS in cands, f'候选里必须有真正能派生的那个 id，实际 {cands}'
        assert ACCOUNT_DIR in raws, '原始目录名要单独留着（找文件用）'

    def test_the_bad_cache_is_repaired_offline(self, tmp_path, monkeypatch, capsys):
        """RED：以前 `extract_keys_from_mmkv` 在这里直接 return {}，缓存里的 0xC9 永不纠正。

        注意这个场景是**稳态**（该 md5 已在缓存里）⇒ 返回值仍然是**空集合**
        （发现语义没变），证据在"缓存被就地纠正 + 派生 XOR 被登记"。
        """
        dec, _img, _plain = _setup(tmp_path)
        _write_broken_cache(dec)
        _synthetic_mmkv(monkeypatch, tmp_path, CODE)

        found = v2.extract_keys_from_mmkv(dec, ACCOUNT_DIR)
        out = capsys.readouterr().out

        assert not found, '稳态下返回值仍是"本次新发现的 md5 集合"（= 空），语义没变'
        assert v2.get_derived_xor(dec) == DERIVED_XOR, '必须登记派生真值'
        assert _cache_xor(dec) == DERIVED_XOR, '缓存里写坏的 0xC9 必须被就地纠正'
        assert 'Cannot determine wxid' not in out, '那句"跳过"正是本任务要根除的静默'

    def test_a_fresh_backup_gets_its_key_discovered(self, tmp_path, monkeypatch):
        """有未缓存文件时，发现通道同样要在这种账号名下工作。"""
        dec, _img, _plain = _setup(tmp_path)
        _synthetic_mmkv(monkeypatch, tmp_path, CODE)

        found = v2.extract_keys_from_mmkv(dec, ACCOUNT_DIR)

        assert MD5 in found, '未缓存的 md5 必须被发现'
        assert found[MD5] == AES_KEY
        assert _cache_xor(dec) == DERIVED_XOR

    def test_the_winning_id_form_is_reported(self, tmp_path, monkeypatch, capsys):
        """可观测：日志必须说出"哪个 id 形态赢了"，否则下一次还是没法排查。"""
        dec, _img, _plain = _setup(tmp_path)
        _synthetic_mmkv(monkeypatch, tmp_path, CODE)

        v2.extract_keys_from_mmkv(dec, ACCOUNT_DIR)
        out = capsys.readouterr().out

        assert ALIAS in out, '日志里必须出现真正生效的那个 id 形态（裸 id）'
        assert '账号 id 形态' in out

    def test_a_wrong_id_candidate_never_wins(self, tmp_path, monkeypatch):
        """钉子（安全边界）：候选多给几个**不等于**放宽标准 —— 验证不通过就不写盘。

        这里把 `.dat` 造成用**另一个** id 派生的密钥加密的 ⇒ 所有候选都验证不通过 ⇒
        不许报"发现了密钥"、不许动缓存。
        """
        foreign = derive_aes_key(CODE, 'someone_else')
        dec, _img, _plain = _setup(tmp_path, aes_key=foreign)
        _write_broken_cache(dec, aes_key=foreign)
        _synthetic_mmkv(monkeypatch, tmp_path, CODE)

        before = open(os.path.join(dec, '_media_keys.json'), 'rb').read()
        found = v2.extract_keys_from_mmkv(dec, ACCOUNT_DIR)

        assert not found, '没有候选能通过真文件验证时，不许声称发现了密钥'
        assert v2.get_derived_xor(dec) is None
        assert open(os.path.join(dec, '_media_keys.json'), 'rb').read() == before, \
            '验证不通过 ⇒ 缓存逐字节不许变（候选多是**猜测**，写盘必须有证据）'

    def test_all_candidates_failing_says_so_with_the_count(self, tmp_path, monkeypatch,
                                                           capsys):
        """失败也要说清"试过几个候选" —— 报告者当初只能看到一句"跳过"。"""
        foreign = derive_aes_key(CODE, 'someone_else')
        dec, _img, _plain = _setup(tmp_path, aes_key=foreign)
        _synthetic_mmkv(monkeypatch, tmp_path, CODE)

        v2.extract_keys_from_mmkv(dec, ACCOUNT_DIR)
        out = capsys.readouterr().out

        assert 'No keys matched' in out
        assert '账号 id 形态' in out, '失败日志要报出试过多少形态'


# ---------------------------------------------------------------------------
# C. 端到端：报告者的场景（自定义微信号 + 坏缓存 + 微信没在跑）
# ---------------------------------------------------------------------------

class TestRouteEndToEndWithACustomAliasAccount:
    def _client(self, dec):
        from web.app import create_app
        return create_app(dec, wxid=ACCOUNT_DIR).test_client()

    def test_the_complete_image_is_served(self, tmp_path, monkeypatch):
        dec, _img, plain = _setup(tmp_path)
        _write_broken_cache(dec)
        _synthetic_mmkv(monkeypatch, tmp_path, CODE)

        r = self._client(dec).get(
            '/api/hardlink-media?md5=%s&type=3&path=%s'
            % (MD5, 'msg/attach/h1/d1/Img/' + MD5 + '.dat'))

        assert r.status_code == 200, f'期望 200，实际 {r.status_code}'
        assert r.data == plain, '微信没在跑、账号名还不带 wxid_ 前缀 ⇒ 离线通道必须独自救回整张图'
        assert r.data[-2:] == b'\xff\xd9'


# ---------------------------------------------------------------------------
# D. 其它两个"名字守门"的地方
# ---------------------------------------------------------------------------

class TestHarvestAndFilesystemScan:
    def test_harvest_no_longer_bails_out_on_the_name(self, tmp_path, monkeypatch, capsys):
        """`harvest-keys`（用户手里的修复工具）此前有同一个守卫。"""
        dec, _img, _plain = _setup(tmp_path)
        logs = []
        v2.harvest_v2_keys(dec, wxid=ACCOUNT_DIR, interval=0.01, max_rounds=1,
                           print_fn=lambda *a, **kw: logs.append(' '.join(str(x) for x in a)))
        text = '\n'.join(logs)
        assert 'Cannot determine wxid' not in text
        assert 'Loaded' in text and 'V2 ciphertexts' in text, \
            '必须走到"已加载样本"这一步（说明名字这一关过了）'

    def test_filesystem_scan_finds_files_under_a_non_wxid_directory(self, tmp_path):
        """`media_resolve._scan_filesystem_for_media` 以前只认 `wxid` 开头的目录。

        ⚠️ 只测**目录名筛选**这一件事（本文件改的就是它）：该函数按
        `<search_root>/<hash>/<date>/<file>` 两层目录去 walk，这是它自己的既有形状，
        不在本次改动范围内 —— 所以在合成素材里按它的形状摆文件，
        断言落在"**不带 `wxid_` 前缀的账号目录里也能扫到**"（改动前这里是 None）。
        """
        from engine.services.message.media_resolve import _scan_filesystem_for_media

        dec, _img, _plain = _setup(tmp_path)
        root = tmp_path / 'xwechat_files' / ACCOUNT_DIR
        fdir = root / 'msg' / 'file' / '2026-05' / '2026-05-01'
        fdir.mkdir(parents=True, exist_ok=True)
        (fdir / 'deadbeef_report.docx').write_bytes(b'x')

        got = _scan_filesystem_for_media(dec, 'deadbeef', 6)

        assert got is not None, '账号目录不带 wxid_ 前缀时也必须能扫到（以前返回 None）'
        assert got.endswith('deadbeef_report.docx')
