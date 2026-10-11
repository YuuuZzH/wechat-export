"""`engine.services.active_dir` 的单元测试。

原则：**不依赖「微信真的在运行」**。
- 排序 / 分级 / 文案：全部用**合成信号**（注入 `Probe`）驱动 `rank_dirs(..., signals=...)`；
- T0 的降级路径：注入**合成的句柄快照 buffer** + monkeypatch `_open_process` 等底层原语；
- T1 / T2 的真实机制：只用临时目录里的**自有文件**验证（`is_locked` 用自己持有的独占句柄）。

本文件没有任何对微信 db 的写操作，也不写任何文件（只有 pytest tmp_path）。
"""
import ctypes
import os
import struct
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

from engine.services import active_dir as ad

# --- 脱敏的合成路径（不是本机真实账号目录）---
A = r'D:\xwechat_files\acct_a\db_storage'
B = r'D:\xwechat_files\acct_b\db_storage'
C = r'D:\xwechat_files\acct_c\db_storage'
E = r'D:\xwechat_files\acct_e\db_storage'

BASE_KEYS = {'db_path', 'wxid', 'mtime', 'db_count', 'size_mb'}
RANK_KEYS = {'tier', 'reason', 'recommended', 'pids', 'active', 'last_write_min'}


def mkdir_entry(path, *, size_mb=10.0, wxid='acct', mtime=0.0, db_count=1):
    """模拟 find_all_wechat_data_dirs() 的既有返回项（既有 5 个键）。"""
    return {'db_path': path, 'wxid': wxid, 'mtime': mtime,
            'db_count': db_count, 'size_mb': size_mb}


def mkprobe(**kw):
    """合成一份 T0/T1/T2 信号（默认：什么都没发现）。"""
    d = dict(pids=[], wechat_running=False, in_use={}, locked={},
             last_write_min={}, t0_ok=True, t0_elapsed_ms=1.0, t1_probed=0,
             t2_probed=0, errors=[])
    d.update(kw)
    return ad.Probe(**d)


def mk_snapshot(entries, header_pad=0, entry_size=None):
    """按 NtQuerySystemInformation 的布局造一个最小句柄快照 buffer。

    entries: [(pid, handle_value, object_type_index), ...]
    头部 = 两个 ULONG_PTR（NumberOfHandles + Reserved）——少跳一个就一个句柄都找不到。
    entry_size: 条目步长（默认用被测模块的默认值；用于验证自校准）。
    """
    stride = int(entry_size or ad._ENTRY_SIZE)
    n = len(entries)
    size = ad._HEADER_BYTES + n * stride + header_pad
    buf = (ctypes.c_char * size)()
    struct.pack_into('<QQ', buf, 0, n, 0)
    for i, (pid, handle, type_index) in enumerate(entries):
        off = ad._HEADER_BYTES + i * stride
        struct.pack_into(ad._ENTRY_FMT, buf, off, 0, pid, handle, type_index, 0)
    return buf


# ---------------------------------------------------------------------------
#  文案：前后端必须逐字一致（Task 21 直接用这些常量/文案）
# ---------------------------------------------------------------------------
class TestReasonText:
    def test_in_use_text_is_exact(self):
        assert ad.REASON_IN_USE.format(pids='111,222') == '⭐ 微信进程正在使用（PID 111,222）'

    def test_locked_text_is_exact(self):
        assert ad.REASON_LOCKED == '⭐ 数据库正被占用（微信正在运行）'

    def test_recent_text_is_exact(self):
        assert ad.REASON_RECENT.format(n='28.8') == '最近活跃（28.8 分钟前有写入）'

    def test_config_text_is_exact(self):
        assert ad.REASON_CONFIG == '微信配置指向此目录'

    def test_idle_text_is_exact(self):
        assert ad.REASON_IDLE == '未发现活动迹象'

    def test_tier_order_matches_brief(self):
        assert ad.TIER_ORDER == {'in_use': 0, 'locked': 1, 'recent': 2,
                                 'config': 3, 'idle': 4}

    def test_recent_threshold_is_15_minutes(self):
        assert ad.RECENT_MINUTES == 15.0


# ---------------------------------------------------------------------------
#  五档 tier
# ---------------------------------------------------------------------------
class TestTiers:
    def test_in_use_first_with_pid_reason_and_fields(self):
        dirs = [mkdir_entry(B, size_mb=99.0), mkdir_entry(A, size_mb=1.0)]
        sig = mkprobe(pids=[111, 222], wechat_running=True, in_use={A: [111, 222]})
        ranked, rec = ad.rank_dirs(dirs, signals=sig)

        assert [x['db_path'] for x in ranked] == [A, B]
        assert rec == A
        top = ranked[0]
        assert top['tier'] == 'in_use'
        assert top['reason'] == '⭐ 微信进程正在使用（PID 111,222）'
        assert top['pids'] == [111, 222]
        assert top['active'] is True
        assert top['recommended'] is True
        assert ranked[1]['tier'] == 'idle'
        assert ranked[1]['recommended'] is False
        assert ranked[1]['active'] is False
        assert ranked[1]['pids'] == []

    def test_locked_when_wechat_running(self):
        sig = mkprobe(pids=[333], wechat_running=True, locked={A: True, B: False})
        ranked, rec = ad.rank_dirs([mkdir_entry(B, size_mb=99.0), mkdir_entry(A)], signals=sig)

        assert [x['db_path'] for x in ranked] == [A, B]
        assert rec == A
        assert ranked[0]['tier'] == 'locked'
        assert ranked[0]['reason'] == '⭐ 数据库正被占用（微信正在运行）'
        assert ranked[0]['active'] is True
        assert ranked[1]['tier'] == 'idle'

    def test_locked_but_wechat_not_running_is_not_locked_tier(self):
        sig = mkprobe(pids=[], wechat_running=False, locked={A: True})
        ranked, _ = ad.rank_dirs([mkdir_entry(A)], signals=sig)
        assert ranked[0]['tier'] == 'idle'
        assert ranked[0]['reason'] == '未发现活动迹象'

    def test_locked_none_means_unknown_and_does_not_rank(self):
        sig = mkprobe(pids=[333], wechat_running=True, locked={A: None})
        ranked, _ = ad.rank_dirs([mkdir_entry(A)], signals=sig)
        assert ranked[0]['tier'] == 'idle'

    def test_recent_within_15_minutes_uses_one_decimal(self):
        sig = mkprobe(pids=[1], wechat_running=True,
                      last_write_min={A: 12.34, B: 15.0, C: 15.1})
        ranked, rec = ad.rank_dirs([mkdir_entry(C), mkdir_entry(B), mkdir_entry(A)], signals=sig)
        by_path = {x['db_path']: x for x in ranked}

        assert by_path[A]['tier'] == 'recent'
        assert by_path[A]['reason'] == '最近活跃（12.3 分钟前有写入）'
        assert by_path[B]['tier'] == 'recent'          # 恰好 15.0 ⇒ 仍算 recent
        assert by_path[B]['reason'] == '最近活跃（15.0 分钟前有写入）'
        assert by_path[C]['tier'] == 'idle'            # 15.1 ⇒ 不算
        assert rec == A
        assert by_path[A]['active'] is False
        assert by_path[A]['last_write_min'] == 12.34

    def test_recent_zero_minutes_is_recent(self):
        sig = mkprobe(pids=[1], wechat_running=True, last_write_min={A: 0.0})
        ranked, _ = ad.rank_dirs([mkdir_entry(A)], signals=sig)
        assert ranked[0]['tier'] == 'recent'
        assert ranked[0]['reason'] == '最近活跃（0.0 分钟前有写入）'

    def test_config_tier_when_dir_equals_configured_db_dir(self):
        sig = mkprobe(wechat_running=False)
        ranked, rec = ad.rank_dirs([mkdir_entry(A), mkdir_entry(B)],
                                   config_dir=B, signals=sig)
        assert [x['db_path'] for x in ranked] == [B, A]
        assert rec == B
        assert ranked[0]['tier'] == 'config'
        assert ranked[0]['reason'] == '微信配置指向此目录'
        assert ranked[1]['tier'] == 'idle'

    def test_config_tier_is_case_and_separator_insensitive(self):
        sig = mkprobe(wechat_running=False)
        ranked, _ = ad.rank_dirs([mkdir_entry(A)], config_dir=A.lower(), signals=sig)
        assert ranked[0]['tier'] == 'config'

    def test_empty_or_missing_config_dir_never_matches(self):
        for cfg in ('', None):
            sig = mkprobe(wechat_running=False)
            ranked, _ = ad.rank_dirs([mkdir_entry(A)], config_dir=cfg, signals=sig)
            assert ranked[0]['tier'] == 'idle'

    def test_full_priority_stack_in_use_locked_recent_config_idle(self):
        d_in = mkdir_entry(A); d_lk = mkdir_entry(B)
        d_rc = mkdir_entry(C); d_cf = mkdir_entry(E)
        d_id = mkdir_entry(r'D:\xwechat_files\acct_z\db_storage')
        sig = mkprobe(
            pids=[7], wechat_running=True,
            in_use={A: [7]},
            locked={B: True, A: True},
            last_write_min={A: 1.0, B: 2.0, C: 3.0},
        )
        # 输入故意打乱顺序
        ranked, rec = ad.rank_dirs([d_id, d_cf, d_rc, d_lk, d_in],
                                   config_dir=E, signals=sig)
        assert [x['tier'] for x in ranked] == ['in_use', 'locked', 'recent',
                                              'config', 'idle']
        assert [x['db_path'] for x in ranked] == [A, B, C, E, d_id['db_path']]
        assert rec == A


# ---------------------------------------------------------------------------
#  排序确定性
# ---------------------------------------------------------------------------
class TestOrdering:
    def test_same_tier_ties_by_last_write_then_size_then_path(self):
        sig = mkprobe(pids=[7], wechat_running=True,
                      locked={A: True, B: True, C: True},
                      last_write_min={A: None, B: 5.0, C: 5.0})
        ranked, _ = ad.rank_dirs([mkdir_entry(A, size_mb=100.0),
                                  mkdir_entry(B, size_mb=1.0),
                                  mkdir_entry(C, size_mb=50.0)], signals=sig)
        # last_write_min: C/B(5.0) 排 A(None) 之前；同 5.0 按 size_mb 降序 ⇒ C(50) 先
        assert [x['db_path'] for x in ranked] == [C, B, A]

    def test_equal_signals_fall_back_to_path_ascending(self):
        sig = mkprobe(pids=[7], wechat_running=True,
                      locked={A: True, B: True}, last_write_min={A: 5.0, B: 5.0})
        ranked, _ = ad.rank_dirs([mkdir_entry(B, size_mb=10.0),
                                  mkdir_entry(A, size_mb=10.0)], signals=sig)
        assert [x['db_path'] for x in ranked] == [A, B]

    def test_order_is_deterministic_regardless_of_input_order(self):
        dirs = [mkdir_entry(A, size_mb=10.0), mkdir_entry(B, size_mb=10.0),
                mkdir_entry(C, size_mb=10.0)]
        sig = mkprobe(pids=[7], wechat_running=True, locked={A: True, B: True, C: True})
        first, _ = ad.rank_dirs(list(dirs), signals=sig)
        second, _ = ad.rank_dirs(list(reversed(dirs)), signals=sig)
        third, _ = ad.rank_dirs(list(dirs), signals=sig)
        assert [x['db_path'] for x in first] == [A, B, C]
        assert [x['db_path'] for x in first] == [x['db_path'] for x in second]
        assert [x['db_path'] for x in first] == [x['db_path'] for x in third]

    def test_all_signals_none_everything_is_idle(self):
        sig = mkprobe(pids=[], wechat_running=False,
                      in_use={}, locked={}, last_write_min={A: None, B: None, C: None})
        ranked, rec = ad.rank_dirs([mkdir_entry(C, size_mb=1.0),
                                    mkdir_entry(B, size_mb=5.0),
                                    mkdir_entry(A, size_mb=5.0)], signals=sig)
        assert [x['tier'] for x in ranked] == ['idle', 'idle', 'idle']
        assert [x['reason'] for x in ranked] == ['未发现活动迹象'] * 3
        # size_mb 降序 ⇒ B/A(5.0) 先于 C(1.0)；同 size 按路径升序 ⇒ A 先于 B
        assert [x['db_path'] for x in ranked] == [A, B, C]
        assert rec == A
        assert all(x['last_write_min'] is None for x in ranked)

    def test_empty_dir_list(self):
        ranked, rec = ad.rank_dirs([], signals=mkprobe())
        assert ranked == []
        assert rec == ''

    def test_none_dir_list_is_tolerated(self):
        ranked, rec = ad.rank_dirs(None, signals=mkprobe())
        assert ranked == []
        assert rec == ''

    def test_missing_db_path_key_does_not_crash(self):
        ranked, rec = ad.rank_dirs([{'wxid': 'x', 'size_mb': 1}], signals=mkprobe())
        assert isinstance(rec, str)


# ---------------------------------------------------------------------------
#  字段契约
# ---------------------------------------------------------------------------
class TestFields:
    def test_every_dir_has_base_keys_plus_rank_keys_and_exact_types(self):
        dirs = [mkdir_entry(A, size_mb=5.0), mkdir_entry(B, size_mb=1.0)]
        sig = mkprobe(pids=[1], wechat_running=True, in_use={A: [1]},
                      last_write_min={B: 3.0})
        ranked, _ = ad.rank_dirs(dirs, signals=sig)
        for x in ranked:
            assert set(x) == BASE_KEYS | RANK_KEYS
            assert isinstance(x['tier'], str)
            assert isinstance(x['reason'], str) and x['reason']
            assert isinstance(x['recommended'], bool)
            assert isinstance(x['active'], bool)
            assert isinstance(x['pids'], list)
            assert all(isinstance(p, int) for p in x['pids'])
            assert x['last_write_min'] is None or isinstance(x['last_write_min'], float)

    def test_exactly_one_recommended_and_it_is_the_first(self):
        sig = mkprobe(pids=[1], wechat_running=True, last_write_min={B: 1.0})
        ranked, rec = ad.rank_dirs([mkdir_entry(A), mkdir_entry(B)], signals=sig)
        assert sum(1 for x in ranked if x['recommended']) == 1
        assert ranked[0]['recommended'] is True
        assert rec == ranked[0]['db_path'] == B

    def test_active_only_for_in_use_and_locked(self):
        sig = mkprobe(pids=[1], wechat_running=True, in_use={A: [1]},
                      locked={B: True}, last_write_min={C: 1.0})
        ranked, _ = ad.rank_dirs([mkdir_entry(A), mkdir_entry(B),
                                  mkdir_entry(C), mkdir_entry(E)],
                                 config_dir=E, signals=sig)
        active = {x['db_path'] for x in ranked if x['active']}
        assert active == {A, B}

    def test_input_dicts_are_reused_not_copied_lost(self):
        d = mkdir_entry(A)
        ranked, _ = ad.rank_dirs([d], signals=mkprobe())
        assert ranked[0] is d


# ---------------------------------------------------------------------------
#  T1: is_locked（只用临时文件验证真实机制）
# ---------------------------------------------------------------------------
@pytest.mark.skipif(os.name != 'nt', reason='Windows only')
class TestIsLocked:
    def test_free_file_is_false(self, tmp_path):
        f = tmp_path / 'message_0.db'
        f.write_bytes(b'x')
        assert ad.is_locked(str(f)) is False

    def test_exclusively_held_file_is_true(self, tmp_path):
        f = tmp_path / 'message_0.db'
        f.write_bytes(b'x')
        holder = ad._open_read_only(str(f), ad.FILE_SHARE_NONE)
        assert holder is not None
        try:
            assert ad.is_locked(str(f)) is True
        finally:
            ad._close_handle(holder)
        assert ad.is_locked(str(f)) is False

    def test_missing_file_is_none(self, tmp_path):
        assert ad.is_locked(str(tmp_path / 'nope.db')) is None

    def test_garbage_path_never_raises(self):
        assert ad.is_locked('') is None
        assert ad.is_locked('\x00bad') is None


# ---------------------------------------------------------------------------
#  T2: last_write_minutes
# ---------------------------------------------------------------------------
class TestLastWriteMinutes:
    def _tree(self, tmp_path):
        root = tmp_path / 'db_storage'
        (root / 'message').mkdir(parents=True)
        (root / 'contact').mkdir(parents=True)
        return root

    def test_picks_newest_shm_or_wal(self, tmp_path):
        root = self._tree(tmp_path)
        shm = root / 'message' / 'message_0.db-shm'
        wal = root / 'message' / 'message_0.db-wal'
        shm.write_bytes(b'x')
        wal.write_bytes(b'x')
        now = time.time()
        os.utime(shm, (now - 600, now - 600))
        os.utime(wal, (now - 300, now - 300))
        m = ad.last_write_minutes(str(root))
        assert m is not None
        assert 4.8 <= m <= 5.2

    def test_none_when_no_shm_or_wal(self, tmp_path):
        root = self._tree(tmp_path)
        (root / 'message' / 'message_0.db').write_bytes(b'x')
        assert ad.last_write_minutes(str(root)) is None

    def test_none_for_missing_dir_and_never_raises(self, tmp_path):
        assert ad.last_write_minutes(str(tmp_path / 'nope')) is None
        assert ad.last_write_minutes('') is None
        assert ad.last_write_minutes(None) is None


# ---------------------------------------------------------------------------
#  T0 底层：布局 / 纯函数
# ---------------------------------------------------------------------------
class TestNtLayout:
    def test_header_is_two_ulong_ptr(self):
        assert ad._HEADER_BYTES == 2 * struct.calcsize('P')
        if struct.calcsize('P') == 8:
            assert ad._HEADER_BYTES == 16   # 少跳一个 ULONG_PTR ⇒ 一个句柄都找不到

    def test_entry_stride_is_40_on_x64(self):
        """步长 40 是本机实测结论（16 + 40*句柄数 == 需要的缓冲区大小）。

        步长写错**不会崩**，只会静默采样（56 时只有 1/5 条目落在真实边界），
        所以 40 只当默认值，实际用 `_pick_entry_size` 自校准。
        """
        if struct.calcsize('P') != 8:
            pytest.skip('只验证 x64 布局')
        assert ad._ENTRY_SIZE == 40
        assert ad._HEADER_BYTES == 16
        assert struct.calcsize(ad._ENTRY_FMT) <= ad._ENTRY_SIZE

    def test_pick_entry_size_detects_40(self):
        entries = [(1000 + i * 4, 4 + i * 4, 3 + i) for i in range(8)]
        assert ad._pick_entry_size(mk_snapshot(entries, entry_size=40)) == 40

    def test_pick_entry_size_detects_56(self):
        entries = [(1000 + i * 4, 4 + i * 4, 3 + i) for i in range(8)]
        assert ad._pick_entry_size(mk_snapshot(entries, entry_size=56)) == 56

    def test_pick_entry_size_garbage_falls_back_to_default(self):
        buf = (ctypes.c_char * 64)()
        assert ad._pick_entry_size(buf) == ad._ENTRY_SIZE

    def test_snapshot_parse_reads_pid_and_type_index(self):
        buf = mk_snapshot([(4244, 0x44, 3), (9996, 0x48, 7)])
        assert list(ad._iter_handle_entries(buf)) == [(4244, 0x44, 3), (9996, 0x48, 7)]

    def test_snapshot_parse_clamps_to_buffer_capacity(self):
        buf = mk_snapshot([(4244, 0x44, 3)])
        struct.pack_into('<Q', buf, 0, 999)      # 谎报句柄数
        assert len(list(ad._iter_handle_entries(buf))) == 1

    def test_file_type_key_found_via_own_handle(self, tmp_path):
        """File 对象类型值必须靠「自己打开的句柄 + 快照里的本进程条目」反查。"""
        f = tmp_path / 'x.bin'
        f.write_bytes(b'x')
        h = ad._open_read_only(str(f), ad.FILE_SHARE_READ)
        assert h is not None
        type_value = 0x002A000000120089
        try:
            buf = mk_snapshot([(os.getpid(), int(h), type_value)])
            mask, target = ad._file_type_key(buf, [h])
            assert mask == 0xFFFFFFFFFFFFFFFF
            assert target == type_value
        finally:
            ad._close_handle(h)

    def test_file_type_key_masks_unstable_bits(self):
        """两个文件的类型值若在某些 bit 上不一致，那些 bit 必须被掩掉。"""
        p, q = 0x002A000000120089, 0x002A000700120089
        buf = mk_snapshot([(os.getpid(), 0x44, p), (os.getpid(), 0x48, q),
                           (4244, 0x4C, q), (4244, 0x50, 3)])
        mask, target = ad._file_type_key(buf, [0x44, 0x48])
        assert mask != 0xFFFFFFFFFFFFFFFF
        # 两个样本都命中，且另一种类型值不命中
        assert (p & mask) == target and (q & mask) == target
        assert (3 & mask) != target

    def test_file_type_key_none_without_own_handles(self):
        assert ad._file_type_key(mk_snapshot([(1, 2, 3)]), []) == (None, None)

    def test_file_type_key_none_when_handle_not_in_snapshot(self):
        assert ad._file_type_key(mk_snapshot([(1000, 4, 3)]), [0x44]) == (None, None)

    def test_probe_handle_is_opened_before_snapshot(self, monkeypatch):
        """回归：句柄必须先于快照打开，否则句柄不在快照里 ⇒ 拿不到 File 类型值
        （本机实测踩到过：T0 退化成扫描全部句柄）。"""
        order = []
        real_open = ad._open_read_only

        def spy_open(path, share=ad.FILE_SHARE_NONE):
            order.append('open')
            return real_open(path, share)

        def spy_snapshot(**k):
            order.append('snapshot')
            return mk_snapshot([])

        monkeypatch.setattr(ad, '_open_read_only', spy_open)
        monkeypatch.setattr(ad, '_snapshot_handles', spy_snapshot)
        monkeypatch.setattr(ad, 'wechat_pids', lambda *a, **k: [4244])
        ad.collect_signals([], budget_s=0.5)
        assert 'open' in order and 'snapshot' in order
        assert order.index('open') < order.index('snapshot')

    def test_t0_takes_exactly_one_snapshot_even_with_retry(self, monkeypatch):
        """T0 全程**只拍一次**句柄快照（含兜底重试也不重拍）—— 控制方提示的优化。

        两次快照的写法本机约 1.6 s，一次约 0.3~0.45 s。
        """
        calls = []
        monkeypatch.setattr(ad, 'wechat_pids', lambda *a, **k: [4244])
        monkeypatch.setattr(ad, '_open_probe_handles', lambda: [0x44])
        monkeypatch.setattr(ad, '_open_process', lambda pid, **k: 0x1000)
        monkeypatch.setattr(ad, '_resolve_handle_names',
                            lambda pairs, **k: ({}, False))   # 解析不出名字 ⇒ 触发重试

        def spy_snapshot(**k):
            calls.append(1)
            return mk_snapshot([(os.getpid(), 0x44, 7), (4244, 0x48, 7)])

        monkeypatch.setattr(ad, '_snapshot_handles', spy_snapshot)
        ad._scan_in_use([4244], errors=[], timeout_s=1.0)
        assert len(calls) == 1

    def test_type_filter_miss_retries_unfiltered_scan(self, monkeypatch):
        """File 类型值**过紧**时（过滤后 0 个候选/0 命中）必须用全句柄再试一次，
        绝不静默漏掉"微信正在用哪个目录"——本功能的核心结论。"""
        good = 0x002A000000120089          # 本机 File 类型值（探测得到，不写死）
        monkeypatch.setattr(ad, 'wechat_pids', lambda *a, **k: [4244])
        monkeypatch.setattr(ad, '_open_probe_handles', lambda: [0x44])
        monkeypatch.setattr(ad, '_open_process', lambda pid, **k: 0x1000)
        monkeypatch.setattr(ad, '_dos_drive_map',
                            lambda: {r'\device\harddiskvolume9': 'Z:'})
        monkeypatch.setattr(ad, '_snapshot_handles', lambda **k: mk_snapshot([
            (os.getpid(), 0x44, good),                 # 我自己的文件句柄 ⇒ 推出类型值
            (4244, 0x48, good ^ 0x01),                 # 微信的 db 句柄：差 1 bit ⇒ 被过滤掉
        ]))
        monkeypatch.setattr(ad, '_resolve_handle_names', lambda pairs, **k: (
            {(4244, 0x48): r'\Device\HarddiskVolume9\xwechat_files\acct_a'
                           r'\db_storage\message\message_0.db'}, False))

        errors = []
        out, timed_out = ad._scan_in_use([4244], errors=errors, timeout_s=1.0)
        assert out == {r'Z:\xwechat_files\acct_a\db_storage': [4244]}
        assert timed_out is False
        assert any('过滤' in e for e in errors), errors

    def test_type_filter_miss_with_tight_budget_records_reason(self, monkeypatch):
        """预算不足时不做重试，但必须留下原因（不静默）。"""
        good = 0x002A000000120089
        monkeypatch.setattr(ad, 'wechat_pids', lambda *a, **k: [4244])
        monkeypatch.setattr(ad, '_open_probe_handles', lambda: [0x44])
        monkeypatch.setattr(ad, '_open_process', lambda pid, **k: 0x1000)
        monkeypatch.setattr(ad, '_resolve_handle_names', lambda pairs, **k: ({}, False))

        def slow_map(*a, **k):
            time.sleep(0.3)
            return {}, False

        monkeypatch.setattr(ad, '_map_handles_to_roots', slow_map)
        monkeypatch.setattr(ad, '_snapshot_handles', lambda **k: mk_snapshot([
            (os.getpid(), 0x44, good), (4244, 0x48, good ^ 0x01)]))

        errors = []
        ad._scan_in_use([4244], errors=errors, timeout_s=0.2)
        assert any('预算' in e for e in errors), errors


class TestPathMapping:
    def test_db_storage_root_extraction(self):
        assert ad._db_storage_root(
            r'D:\xwechat_files\acct_a\db_storage\message\message_0.db'
        ) == r'D:\xwechat_files\acct_a\db_storage'
        assert ad._db_storage_root(r'D:\xwechat_files\acct_a\db_storage') == \
            r'D:\xwechat_files\acct_a\db_storage'
        assert ad._db_storage_root(r'D:\xwechat_files\acct_a\DB_STORAGE') == \
            r'D:\xwechat_files\acct_a\DB_STORAGE'

    def test_not_db_storage_returns_none(self):
        assert ad._db_storage_root(r'D:\xwechat_files\acct_a\something\x.db') is None
        assert ad._db_storage_root('') is None
        assert ad._db_storage_root(None) is None

    def test_device_path_to_dos_path(self, monkeypatch):
        monkeypatch.setattr(ad, '_dos_drive_map',
                            lambda: {r'\device\harddiskvolume9': 'Z:'})
        assert ad._device_to_dos_path(
            r'\Device\HarddiskVolume9\xwechat_files\acct_a\db_storage\message\m.db'
        ) == r'Z:\xwechat_files\acct_a\db_storage\message\m.db'

    def test_unknown_device_returns_none(self, monkeypatch):
        monkeypatch.setattr(ad, '_dos_drive_map', lambda: {})
        assert ad._device_to_dos_path(r'\Device\HarddiskVolume9\x\y.db') is None
        assert ad._device_to_dos_path(r'') is None
        assert ad._device_to_dos_path(r'\Device\Mup\server\share\x.db') is None

    def test_normkey_is_case_and_separator_insensitive(self):
        assert ad.normkey(r'D:\Xwechat_Files\A\db_storage') == \
            ad.normkey('D:/xwechat_files/a/DB_STORAGE/')

    def test_t0_root_spelling_differences_still_align(self):
        """T0 的根路径与 find_all_wechat_data_dirs() 的 db_path **写法不同**（大小写/斜杠）
        也必须对上 —— 否则会静默不匹配、丢掉最强的 in_use 档。"""
        sig = mkprobe(pids=[9], wechat_running=True,
                      in_use={'D:/XWECHAT_FILES/ACCT_A/DB_STORAGE': [9]})
        ranked, rec = ad.rank_dirs([mkdir_entry(A, size_mb=1.0)], signals=sig)
        assert ranked[0]['tier'] == 'in_use'
        assert rec == A

    def test_unmatchable_t0_root_falls_back_to_t1_not_silent(self):
        """对不上的写法（这里用 \\\\?\\ 前缀）不会错标 in_use，而是靠 T1 兜底成 locked ——
        即"对不上"也只是降一档，不会整条信息消失。"""
        sig = mkprobe(pids=[9], wechat_running=True, locked={A: True},
                      in_use={r'\\?\D:\xwechat_files\acct_a\db_storage': [9]})
        ranked, _ = ad.rank_dirs([mkdir_entry(A)], signals=sig)
        assert ranked[0]['tier'] == 'locked'
        assert ranked[0]['reason'] == '⭐ 数据库正被占用（微信正在运行）'


# ---------------------------------------------------------------------------
#  依赖与只读红线
# ---------------------------------------------------------------------------
class TestRedLines:
    def test_module_does_not_import_psutil(self):
        """硬约束 1：只用标准库 ctypes，不引入 psutil 等新依赖。"""
        import ast
        src = open(ad.__file__, encoding='utf-8').read()
        imported = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                imported.update(a.name.split('.')[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split('.')[0])
        assert 'psutil' not in imported, imported
        assert imported <= {'__future__', 'ctypes', 'os', 'struct', 'sys',
                            'threading', 'time', 'dataclasses', 'engine'}, imported

    def test_every_handle_returning_win32_call_declares_restype(self):
        """踩坑点 5：默认 restype=c_int 会截断伪句柄 ⇒ 必须显式声明。"""
        protos = ad._win32_prototypes()
        assert protos, '必须列出所有返回句柄的 Win32 调用'
        names = [n for n, _ in protos]
        assert 'CreateFileW' in names and 'OpenProcess' in names \
            and 'CreateToolhelp32Snapshot' in names
        for name, fn in protos:
            assert fn.argtypes is not None, name + ' 必须显式声明 argtypes'
            assert fn.restype is ctypes.c_void_p, \
                name + ' 必须显式声明 restype=ctypes.c_void_p'

    def test_wechat_pids_returns_list_of_int_without_raising(self):
        pids = ad.wechat_pids()
        assert isinstance(pids, list)
        assert all(isinstance(p, int) and p > 0 for p in pids)

    def test_dirs_in_use_never_raises_and_returns_dict(self):
        errors = []
        out = ad.dirs_in_use_by_wechat(errors=errors, timeout_s=1.0)
        assert isinstance(out, dict)
        for k, v in out.items():
            assert isinstance(k, str) and k
            assert isinstance(v, list) and all(isinstance(p, int) for p in v)
        assert isinstance(errors, list)

    def test_probing_a_dir_tree_writes_nothing(self, tmp_path, monkeypatch):
        root = tmp_path / 'db_storage'
        (root / 'message').mkdir(parents=True)
        (root / 'contact').mkdir(parents=True)
        (root / 'message' / 'message_0.db').write_bytes(b'x')
        (root / 'message' / 'message_0.db-shm').write_bytes(b'x')
        (root / 'contact' / 'contact.db').write_bytes(b'x')

        def snap():
            out = {}
            for dirpath, dirnames, filenames in os.walk(root):
                for n in dirnames + filenames:
                    p = os.path.join(dirpath, n)
                    st = os.stat(p)
                    out[p] = (st.st_size, st.st_mtime_ns, st.st_ctime_ns)
            return out

        before = snap()
        monkeypatch.setattr(ad, 'wechat_pids', lambda *a, **k: [4244])
        monkeypatch.setattr(ad, '_scan_in_use', lambda *a, **k: ({}, False))
        probe = ad.collect_signals([mkdir_entry(str(root))], budget_s=1.0)
        assert probe.wechat_running is True
        assert probe.t1_probed > 0            # 真的走了 T1
        assert probe.t2_probed > 0            # 真的走了 T2
        assert snap() == before


# ---------------------------------------------------------------------------
#  降级路径（T0 失败 ⇒ 仍返回可解释的排序列表）
# ---------------------------------------------------------------------------
class TestDegradation:
    @pytest.fixture
    def real_dirs(self, tmp_path):
        """真实存在的合成 db_storage 树（T1 需要能列出候选文件）。"""
        out = []
        for name in ('acct_a', 'acct_b'):
            root = tmp_path / 'xwechat_files' / name / 'db_storage'
            (root / 'message').mkdir(parents=True)
            (root / 'contact').mkdir(parents=True)
            (root / 'message' / 'message_0.db').write_bytes(b'x')
            (root / 'contact' / 'contact.db').write_bytes(b'x')
            out.append(str(root))
        return out

    def test_snapshot_failure_is_recorded_and_degrades_to_t1_t2(self, monkeypatch,
                                                                real_dirs):
        a, b = real_dirs

        def boom():
            raise OSError('NtQuerySystemInformation failed: 0xC0000004')

        monkeypatch.setattr(ad, 'wechat_pids', lambda *a, **k: [4244])
        monkeypatch.setattr(ad, '_snapshot_handles', boom)
        # 只有 acct_a 的文件被占用（模拟 T1 判定）
        monkeypatch.setattr(ad, 'is_locked', lambda p: str(p).startswith(a))
        monkeypatch.setattr(ad, 'last_write_minutes', lambda p: 3.0)

        probe = ad.collect_signals([mkdir_entry(a), mkdir_entry(b)], budget_s=1.0)
        assert probe.t0_ok is False
        assert any('t0' in e for e in probe.errors), probe.errors
        assert probe.in_use == {}
        assert probe.t1_probed > 0
        assert probe.locked.get(ad.normkey(a)) is True

        ranked, rec = ad.rank_dirs([mkdir_entry(a), mkdir_entry(b)], signals=probe)
        assert ranked[0]['tier'] == 'locked'
        assert ranked[0]['reason'] == '⭐ 数据库正被占用（微信正在运行）'
        assert rec == a
        # acct_b 没被占用，但 T2 兜底给出「最近活跃」—— 排序列表依然可解释
        assert ranked[1]['tier'] == 'recent'
        assert ranked[1]['reason'] == '最近活跃（3.0 分钟前有写入）'

    def test_open_process_denied_is_recorded_and_degrades(self, monkeypatch,
                                                          real_dirs):
        a = real_dirs[0]
        monkeypatch.setattr(ad, 'wechat_pids', lambda *a, **k: [4244])
        monkeypatch.setattr(ad, '_snapshot_handles',
                            lambda **k: mk_snapshot([(4244, 0x44, 3)]))
        monkeypatch.setattr(ad, '_open_process', lambda pid, **k: None)
        monkeypatch.setattr(ad, 'is_locked', lambda p: True)

        errors = []
        out = ad.dirs_in_use_by_wechat(errors=errors, timeout_s=1.0)
        assert out == {}
        assert any('OpenProcess' in e or '4244' in e for e in errors), errors

        probe = ad.collect_signals([mkdir_entry(a)], budget_s=1.0)
        assert probe.t0_ok is False
        assert probe.errors
        ranked, _ = ad.rank_dirs([mkdir_entry(a)], signals=probe)
        assert ranked[0]['tier'] == 'locked'

    def test_handle_query_timeout_is_recorded_and_degrades(self, monkeypatch):
        monkeypatch.setattr(ad, 'wechat_pids', lambda *a, **k: [4244])
        monkeypatch.setattr(ad, '_snapshot_handles',
                            lambda **k: mk_snapshot([(4244, 0x44, 3)]))
        monkeypatch.setattr(ad, '_open_process', lambda pid, **k: 0x1000)
        monkeypatch.setattr(ad, '_resolve_handle_names',
                            lambda pairs, **k: ({}, True))
        monkeypatch.setattr(ad, 'last_write_minutes', lambda p: 2.0)

        probe = ad.collect_signals([mkdir_entry(A)], budget_s=1.0)
        assert probe.t0_ok is False
        assert any('超时' in e or 'timeout' in e for e in probe.errors), probe.errors
        ranked, _ = ad.rank_dirs([mkdir_entry(A)], signals=probe)
        assert ranked[0]['tier'] == 'recent'          # T1/T2 兜底，不是 500 不是空列表
        assert ranked[0]['reason'] == '最近活跃（2.0 分钟前有写入）'

    def test_timeout_with_partial_hits_keeps_them_but_flags_t0(self, monkeypatch):
        monkeypatch.setattr(ad, 'wechat_pids', lambda *a, **k: [4244])
        monkeypatch.setattr(ad, '_snapshot_handles',
                            lambda **k: mk_snapshot([(4244, 0x44, 3)]))
        monkeypatch.setattr(ad, '_open_process', lambda pid, **k: 0x1000)
        monkeypatch.setattr(ad, '_dos_drive_map',
                            lambda: {r'\device\harddiskvolume9': 'Z:'})
        partial = {(4244, 0x44): r'\Device\HarddiskVolume9\xwechat_files\acct_a'
                                  r'\db_storage\message\message_0.db'}
        monkeypatch.setattr(ad, '_resolve_handle_names',
                            lambda pairs, **k: (dict(partial), True))

        z_dir = r'Z:\xwechat_files\acct_a\db_storage'
        probe = ad.collect_signals([mkdir_entry(z_dir)], budget_s=1.0)
        assert probe.t0_ok is False
        assert probe.in_use, '部分命中不该被丢弃'
        ranked, rec = ad.rank_dirs([mkdir_entry(z_dir)], signals=probe)
        assert ranked[0]['tier'] == 'in_use'
        assert rec == z_dir

    def test_wechat_not_running_still_ranks_by_recent_and_config(self, monkeypatch):
        monkeypatch.setattr(ad, 'wechat_pids', lambda *a, **k: [])
        monkeypatch.setattr(ad, '_scan_in_use', lambda *a, **k: ({}, False))
        monkeypatch.setattr(ad, 'last_write_minutes', lambda p: 4.0 if p == A else None)

        probe = ad.collect_signals([mkdir_entry(A), mkdir_entry(B)], config_dir=B,
                                   budget_s=1.0)
        assert probe.wechat_running is False
        assert probe.t1_probed == 0            # 微信没跑 ⇒ 不做 T1
        ranked, rec = ad.rank_dirs([mkdir_entry(A), mkdir_entry(B)],
                                   config_dir=B, signals=probe)
        assert [x['tier'] for x in ranked] == ['recent', 'config']
        assert rec == A

    def test_budget_exhausted_skips_t1_and_records_reason(self, monkeypatch):
        monkeypatch.setattr(ad, 'wechat_pids', lambda *a, **k: [4244])

        def slow(*a, **k):
            time.sleep(0.25)
            return {}, False

        monkeypatch.setattr(ad, '_scan_in_use', slow)
        monkeypatch.setattr(ad, 'is_locked', lambda p: True)

        probe = ad.collect_signals([mkdir_entry(A)], budget_s=0.05)
        assert probe.t1_probed == 0
        assert any('预算' in e for e in probe.errors), probe.errors

    def test_probe_as_dict_has_contract_keys(self):
        probe = ad.collect_signals([mkdir_entry(A)], budget_s=0.05)
        d = probe.as_dict()
        for k in ('t0_ok', 't0_elapsed_ms', 't1_probed', 'errors'):
            assert k in d, k
        assert isinstance(d['t0_ok'], bool)
        assert isinstance(d['t0_elapsed_ms'], (int, float))
        assert isinstance(d['t1_probed'], int)
        assert isinstance(d['errors'], list)
        for e in d['errors']:
            assert isinstance(e, str) and e
