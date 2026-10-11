"""`psutil` 护栏的单元测试（Task 23 / S9-A）。

背景：psutil 是**可选**依赖（打包链自 Task 24 起已包含它，但**代码不许假设它存在**：
开发环境可能没装、旧 exe 或精简安装可能没有），而代码里有多处 `import psutil`
（进程枚举 / hook 兜底）。本文件锁定三条红线：

1. **缺 psutil 必须显式、可诊断**：为什么枚举不到进程这件事要能传出来，且只提示一次（不刷屏）；
2. **不许把「依赖缺失」伪装成「微信没运行」**；
3. **psutil 可用时行为逐字不变**——与「改造前的逐字副本」对拍（含假 psutil 合成进程）。

手法（不依赖「本机真的缺 psutil」）：

* 缺 psutil：`sys.meta_path` 上的**拦截 finder**，它每次被探到都记一笔（`probes`）并抛
  `ImportError` —— 这就是「模拟的 ImportError 真的发生了」的**见证**，否则「两句话都不出现」
  也能假通过；另一个变体是 `sys.modules['psutil'] = None`（brief 里提到的写法）。
* 有 psutil：本机真实 psutil + 合成假 psutil（可注入 NameError/内存/异常）双路。

红线：本文件不读真实微信数据、不读 `backup/`、不写仓库内文件（只用 tmp 目录之外的内存与 pip 缓存）。
"""
import contextlib
import ast
import os
import sys
import time
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

import key_scan as ks                                     # noqa: E402
from engine.services import py_wx_key_v2 as kv2           # noqa: E402
from engine.services import py_wx_key_v3 as kv3           # noqa: E402
from engine.services import wechat_key_extract as wke     # noqa: E402
from engine.services import wx_startup_watcher as wsw     # noqa: E402

_MISSING = object()

MB = 1048576


# ---------------------------------------------------------------------------
#  假 psutil（「psutil 可用」场景的合成替身）
# ---------------------------------------------------------------------------
class _FakeNoSuchProcess(Exception):
    pass


class _FakeAccessDenied(Exception):
    pass


class _FakeMem:
    def __init__(self, rss):
        self.rss = rss


class _FakeProc:
    """最小 `psutil.Process` 替身。

    `info` 做成 property：`raises` 不为 None 时，**读取 .info 的瞬间**抛异常，
    用来复现「枚举到一半进程消失 / 权限不足」的分支。
    """

    def __init__(self, pid, name, rss, raises=None, mem_none=False):
        self._pid, self._name, self._rss = pid, name, rss
        self._raises, self._mem_none = raises, mem_none

    @property
    def info(self):
        if self._raises is not None:
            raise self._raises
        mem = None if self._mem_none else _FakeMem(self._rss)
        return {'pid': self._pid, 'name': self._name, 'memory_info': mem}


class _FakeProcessObj:
    """`psutil.Process(pid)` 的替身（给 _wait_dll_loaded 用）。"""

    def __init__(self, maps=(), raises=None):
        self._maps, self._raises = list(maps), raises

    def memory_maps(self):
        if self._raises is not None:
            raise self._raises
        return [types.SimpleNamespace(path=p) for p in self._maps]


#: 合成进程表（覆盖：大小写、非微信进程、name=None、memory_info=None、枚举中抛异常、旧版 WeChat.exe）
FAKE_PROCS = [
    _FakeProc(101, 'weixin.exe', 3 * MB),
    _FakeProc(102, 'Weixin.EXE', 5 * MB),
    _FakeProc(103, 'explorer.exe', 99 * MB),
    _FakeProc(104, None, 7 * MB),
    _FakeProc(105, 'wechat.exe', 2 * MB),
    _FakeProc(106, 'weixin.exe', 0, raises=_FakeAccessDenied('denied mid-enum')),
    _FakeProc(107, 'weixin.exe', 0, mem_none=True),
]


def make_fake_psutil(procs=(), process_factory=None):
    mod = types.ModuleType('psutil')
    mod.NoSuchProcess = _FakeNoSuchProcess
    mod.AccessDenied = _FakeAccessDenied
    mod.process_iter = lambda attrs=None: iter(list(procs))
    if process_factory is not None:
        mod.Process = process_factory
    return mod


# ---------------------------------------------------------------------------
#  注入手段
# ---------------------------------------------------------------------------
class _PsutilBlockingFinder:
    """meta_path finder：拒绝提供 psutil，并把每次探测记进 `probes`（见证）。"""

    def __init__(self):
        self.probes = []

    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'psutil' or fullname.startswith('psutil.'):
            self.probes.append(fullname)
            raise ImportError(
                'psutil blocked by test finder (probe #%d)' % len(self.probes))
        return None


@contextlib.contextmanager
def psutil_blocked():
    """psutil 不可导入（真 ImportError，且带探针见证）。"""
    finder = _PsutilBlockingFinder()
    saved = sys.modules.pop('psutil', _MISSING)
    sys.meta_path.insert(0, finder)
    try:
        yield finder
    finally:
        sys.meta_path.remove(finder)
        sys.modules.pop('psutil', None)
        if saved is not _MISSING:
            sys.modules['psutil'] = saved


@contextlib.contextmanager
def psutil_halted():
    """`sys.modules['psutil'] = None` 变体（import 会抛 ImportError: halted）。"""
    saved = sys.modules.get('psutil', _MISSING)
    sys.modules['psutil'] = None
    try:
        yield
    finally:
        sys.modules.pop('psutil', None)
        if saved is not _MISSING:
            sys.modules['psutil'] = saved


@contextlib.contextmanager
def psutil_available(procs=(), process_factory=None):
    """psutil 可导入，但内容是合成的。"""
    saved = sys.modules.get('psutil', _MISSING)
    sys.modules['psutil'] = make_fake_psutil(procs, process_factory)
    try:
        yield sys.modules['psutil']
    finally:
        sys.modules.pop('psutil', None)
        if saved is not _MISSING:
            sys.modules['psutil'] = saved


@pytest.fixture
def reset_warning_flag(monkeypatch):
    """把「只提示一次」的全局标记复位（否则别的用例会把提示吃掉）。"""
    monkeypatch.setattr(wke, '_psutil_warning_emitted', False)
    return True


# ---------------------------------------------------------------------------
#  改造前的「逐字副本」——用于「psutil 可用时行为逐字不变」对拍
# ---------------------------------------------------------------------------
def old_key_scan_find_wechat_pids():
    """改造前 `key_scan._find_wechat_pids()` 的逐字副本。"""
    try:
        import psutil
    except ImportError:
        return []
    candidates = []
    for proc in psutil.process_iter(['pid', 'name', 'memory_info']):
        try:
            info = proc.info
            if info['name'] and info['name'].lower() == 'weixin.exe':
                mem = info['memory_info'].rss if info['memory_info'] else 0
                candidates.append((mem, info['pid']))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    candidates.sort(reverse=True)
    return candidates


def old_wke_find_wechat_pids():
    """改造前 `wechat_key_extract._find_wechat_pids()` 的逐字副本。"""
    try:
        import psutil
    except ImportError:
        return []
    candidates = []
    for proc in psutil.process_iter(['pid', 'name', 'memory_info']):
        try:
            info = proc.info
            if info['name'] and info['name'].lower() in wke._KNOWN_EXE_NAMES:
                mem = info['memory_info'].rss if info['memory_info'] else 0
                candidates.append((mem, info['pid']))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    candidates.sort(reverse=True)
    return candidates


def old_wsw_find_wechat_pids():
    """改造前 `wx_startup_watcher.WeChatStartupWatcher._find_wechat_pids()` 的逐字副本。"""
    import psutil
    candidates = []
    for proc in psutil.process_iter(['pid', 'name', 'memory_info']):
        try:
            info = proc.info
            if info['name'] and info['name'].lower() == 'weixin.exe':
                mem = info['memory_info'].rss if info['memory_info'] else 0
                candidates.append((mem, info['pid']))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    candidates.sort(reverse=True)
    return candidates


def old_find_weixin_pid():
    """改造前 `py_wx_key_v2/v3._find_weixin_pid()` 的逐字副本。"""
    import psutil
    candidates = []
    for proc in psutil.process_iter(['pid', 'name', 'memory_info']):
        try:
            info = proc.info
            if info['name'] and info['name'].lower() == 'weixin.exe':
                mem = info['memory_info'].rss if info['memory_info'] else 0
                candidates.append((mem, info['pid']))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    candidates.sort(reverse=True)
    if candidates:
        return candidates[0][1], candidates[0][0] // MB
    return None, 0


def old_wait_dll_loaded(pid, timeout=30):
    """改造前 `wechat_key_extract._wait_dll_loaded()` 的逐字副本。"""
    import psutil
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            proc = psutil.Process(pid)
            for m in proc.memory_maps():
                if 'weixin.dll' in m.path.lower():
                    return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
        time.sleep(0.2)
    return False


# ---------------------------------------------------------------------------
#  ① 判别性：模拟的 ImportError 真的发生了（有见证）
# ---------------------------------------------------------------------------
class TestInjectionWitness:
    def test_blocking_finder_really_blocks_import(self):
        with psutil_blocked() as finder:
            with pytest.raises(ImportError):
                import psutil  # noqa: F401
            assert finder.probes == ['psutil'], finder.probes

    def test_halted_sys_modules_really_blocks_import(self):
        with psutil_halted():
            with pytest.raises(ImportError):
                import psutil  # noqa: F401

    def test_pid_enum_reaches_the_injection_point(self, reset_warning_flag):
        """枚举函数必须**真的去 import psutil** 了（否则下面的用例可能空转）。"""
        with psutil_blocked() as finder:
            ks._find_wechat_pids(errors=[], print_fn=lambda _s: None)
        assert 'psutil' in finder.probes, finder.probes

    def test_psutil_available_when_not_injected(self):
        """对照组：不注入时本机 psutil 可用（说明「缺 psutil」测试不是伪命题）。"""
        mod = wke.import_psutil(print_fn=lambda _s: None)
        assert mod is not None and hasattr(mod, 'process_iter')


# ---------------------------------------------------------------------------
#  ② 「为什么枚举不到」必须能传出来 + 只提示一次
# ---------------------------------------------------------------------------
class TestEnumerationReason:
    def test_key_scan_records_reason_and_returns_empty(self, reset_warning_flag):
        lines, errors = [], []
        with psutil_blocked() as finder:
            out = ks._find_wechat_pids(errors=errors, print_fn=lines.append)
        assert out == []
        assert errors, '空结果必须带原因，否则调用方只能猜「微信没运行」'
        assert 'psutil' in errors[0], errors
        assert 'psutil' in finder.probes, '必须真的 import 过 psutil'

    def test_wechat_key_extract_records_reason_and_returns_empty(self, reset_warning_flag):
        lines, errors = [], []
        with psutil_blocked():
            out = wke._find_wechat_pids(errors=errors, print_fn=lines.append)
        assert out == []
        assert errors and 'psutil' in errors[0], errors

    def test_missing_psutil_warns_once_not_silently(self, reset_warning_flag):
        lines = []
        with psutil_blocked():
            ks._find_wechat_pids(errors=[], print_fn=lines.append)
            wke._find_wechat_pids(errors=[], print_fn=lines.append)
            ks._find_wechat_pids(errors=[], print_fn=lines.append)
            wke._find_wechat_pids(errors=[], print_fn=lines.append)
        warns = [ln for ln in lines if '[WARN]' in ln]
        assert len(warns) == 1, '缺 psutil 必须显式提示且只提示一次，实际: %r' % (lines,)
        assert 'psutil' in warns[0]
        assert 'Config.Cipher' in warns[0], '提示要能告诉用户主路径没受影响: %r' % warns[0]

    def test_halted_variant_behaves_the_same(self, reset_warning_flag):
        lines, errors = [], []
        with psutil_halted():
            out = ks._find_wechat_pids(errors=errors, print_fn=lines.append)
        assert out == [] and errors and 'psutil' in errors[0], (out, errors)

    def test_no_reason_when_psutil_present_and_no_process(self, reset_warning_flag):
        """psutil 可用、只是没有微信进程时：没有原因（那就是「没运行」）。"""
        lines, errors = [], []
        with psutil_available(procs=[]):
            out = ks._find_wechat_pids(errors=errors, print_fn=lines.append)
        assert out == [] and errors == []
        assert lines == [], lines


# ---------------------------------------------------------------------------
#  ③ 无护栏的两处（+ v2/v3 同族）必须受控失败
# ---------------------------------------------------------------------------
class TestControlledFailure:
    def test_wait_dll_loaded_raises_actionable_error(self, reset_warning_flag):
        with psutil_blocked() as finder:
            with pytest.raises(wke.PsutilUnavailableError) as ei:
                wke._wait_dll_loaded(1234, timeout=0.01)
        msg = str(ei.value)
        assert 'psutil' in msg, msg
        assert 'Config.Cipher' in msg, '异常信息要可操作: %r' % msg
        assert 'psutil' in finder.probes

    def test_wx_startup_watcher_find_pids_raises_actionable_error(self, reset_warning_flag):
        with psutil_blocked() as finder:
            with pytest.raises(wke.PsutilUnavailableError) as ei:
                wsw.WeChatStartupWatcher._find_wechat_pids()
        msg = str(ei.value)
        assert 'psutil' in msg, msg
        assert 'psutil' in finder.probes

    @pytest.mark.parametrize('mod', [kv2, kv3], ids=['v2', 'v3'])
    def test_hook_backend_find_pid_raises_actionable_error(self, mod, reset_warning_flag):
        with psutil_blocked():
            with pytest.raises(wke.PsutilUnavailableError) as ei:
                mod._find_weixin_pid()
        assert 'psutil' in str(ei.value), str(ei.value)

    @pytest.mark.parametrize('mod', [kv2, kv3], ids=['v2', 'v3'])
    def test_hook_backend_autodetect_fails_controlled_not_misleading(self, mod, reset_warning_flag):
        """`initialize_hook()` 不带 pid：psutil 缺失要给出**真实**原因。"""
        with psutil_blocked():
            ok = mod.initialize_hook()
        assert ok is False, '不能崩，也不能假装成功'
        err = mod.get_last_error_msg()
        assert 'psutil' in err, err
        assert 'Weixin.exe not running' not in err, '不许把依赖缺失说成「微信没运行」: %r' % err


# ---------------------------------------------------------------------------
#  ④ [Hook] 分支必须说实话（真调用点，不是替身）
# ---------------------------------------------------------------------------
class _FakeWatcher:
    """顶掉 `_ProcessStartWatcher`，避免真的起线程 / 等 300s。"""

    def __init__(self, *a, **kw):
        self.stopped = False

    def pop_new_pids(self):
        return []

    def stop(self):
        self.stopped = True


class _JumpClock:
    """第一次调用返回 0，之后跳跃到 1e6（让等待循环立刻超时退出）。"""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return 0.0 if self.calls == 1 else 1e6


@contextlib.contextmanager
def fake_admin(module):
    """把模块里的 `ctypes` 换成「一定是管理员」的哑元（只影响该模块的名字绑定）。"""
    saved = module.ctypes
    module.ctypes = types.SimpleNamespace(
        windll=types.SimpleNamespace(
            shell32=types.SimpleNamespace(IsUserAnAdmin=lambda: 1)))
    try:
        yield
    finally:
        module.ctypes = saved


NOT_RUNNING = '[Hook] WeChat is not running.'


class TestHookBranchHonesty:
    def _run_wke(self, monkeypatch, print_fn):
        """走 `wechat_key_extract.extract_keys_via_hook` 的真实分支。"""
        monkeypatch.setattr(wke, '_import_hook_backend', lambda: (object(), 'v3'))
        monkeypatch.setattr(wke, '_ProcessStartWatcher', _FakeWatcher)
        monkeypatch.setattr(wke, 'time', types.SimpleNamespace(time=_JumpClock()))
        with fake_admin(wke):
            return wke.extract_keys_via_hook(
                'D:\\fake\\db_storage', [], {'aa' * 16: ['x.db']}, {}, print_fn)

    def _run_key_scan(self, monkeypatch, print_fn):
        """走 `key_scan._extract_keys_via_hook` 的真实分支。

        `key_scan` 在函数内 `import time as _time`，所以只能顶掉全局 `time.time`
        （否则 Phase 2 的 120s 等待会让用例挂住）。
        """
        dummy = types.ModuleType('engine.services.py_wx_key_v3')
        monkeypatch.setitem(sys.modules, 'engine.services.py_wx_key_v3', dummy)
        import engine.services as _pkg
        monkeypatch.setattr(_pkg, 'py_wx_key_v3', dummy, raising=False)
        monkeypatch.setattr(time, 'time', _JumpClock())
        with fake_admin(ks):
            return ks._extract_keys_via_hook(
                'D:\\fake\\db_storage', [], {'aa' * 16: ['x.db']}, {}, print_fn)

    def test_wke_hook_branch_says_psutil_not_not_running(self, monkeypatch, reset_warning_flag):
        out = []
        with psutil_blocked():
            self._run_wke(monkeypatch, out.append)
        text = '\n'.join(out)
        assert NOT_RUNNING not in out, '依赖缺失被说成了「微信没在运行」:\n%s' % text
        assert any('psutil' in ln and '无法枚举' in ln for ln in out), text
        assert not any('Not running as admin' in ln for ln in out), text

    def test_key_scan_hook_branch_says_psutil_not_not_running(self, monkeypatch, reset_warning_flag):
        out = []
        with psutil_blocked():
            self._run_key_scan(monkeypatch, out.append)
        text = '\n'.join(out)
        assert NOT_RUNNING not in out, '依赖缺失被说成了「微信没在运行」:\n%s' % text
        assert any('psutil' in ln and '无法枚举' in ln for ln in out), text
        assert not any('Not running as admin' in ln for ln in out), text
        assert not any('No hook backend available' in ln for ln in out), text

    def test_hook_branch_still_says_not_running_when_psutil_is_fine(self, monkeypatch, reset_warning_flag):
        """对照片：psutil 可用且没有微信进程 ⇒ 仍然打印原来那句（逐字不变）。"""
        out = []
        with psutil_available(procs=[]):
            self._run_wke(monkeypatch, out.append)
        assert NOT_RUNNING in out, out
        assert not any('psutil' in ln for ln in out), out

    def test_key_scan_hook_branch_still_says_not_running_when_psutil_is_fine(self, monkeypatch, reset_warning_flag):
        out = []
        with psutil_available(procs=[]):
            self._run_key_scan(monkeypatch, out.append)
        assert NOT_RUNNING in out, out
        assert not any('psutil' in ln for ln in out), out


# ---------------------------------------------------------------------------
#  ⑤ psutil 可用时：与改造前的逐字副本对拍（含合成进程表）
# ---------------------------------------------------------------------------
class TestUnchangedWhenPsutilAvailable:
    def test_import_psutil_returns_real_module_and_is_quiet(self):
        lines = []
        mod = wke.import_psutil(print_fn=lines.append)
        assert mod is not None
        assert lines == [], lines

    def test_key_scan_enum_equivalent_fake_psutil(self, monkeypatch):
        with psutil_available(procs=FAKE_PROCS):
            monkeypatch.setattr(wke, '_psutil_warning_emitted', False)
            assert ks._find_wechat_pids() == old_key_scan_find_wechat_pids()

    def test_wke_enum_equivalent_fake_psutil(self, monkeypatch):
        with psutil_available(procs=FAKE_PROCS):
            monkeypatch.setattr(wke, '_psutil_warning_emitted', False)
            assert wke._find_wechat_pids() == old_wke_find_wechat_pids()

    def test_wsw_enum_equivalent_fake_psutil(self, monkeypatch):
        with psutil_available(procs=FAKE_PROCS):
            monkeypatch.setattr(wke, '_psutil_warning_emitted', False)
            assert wsw.WeChatStartupWatcher._find_wechat_pids() == old_wsw_find_wechat_pids()

    @pytest.mark.parametrize('mod', [kv2, kv3], ids=['v2', 'v3'])
    def test_hook_find_pid_equivalent_fake_psutil(self, mod, monkeypatch):
        with psutil_available(procs=FAKE_PROCS):
            monkeypatch.setattr(wke, '_psutil_warning_emitted', False)
            assert mod._find_weixin_pid() == old_find_weixin_pid()

    def test_enumerations_equivalent_with_real_psutil(self, monkeypatch):
        """本机真实 psutil：进程集合与「降序」不变量一致。

        注意**不能**逐字比对 (rss, pid)：真实进程的内存每秒都在变，两次调用的 RSS
        本来就会漂移（这不是行为差异）。逐字对拍交给上面注入合成 psutil 的用例。
        """
        monkeypatch.setattr(wke, '_psutil_warning_emitted', False)
        kn, ko = ks._find_wechat_pids(), old_key_scan_find_wechat_pids()
        wn, wo = wke._find_wechat_pids(), old_wke_find_wechat_pids()
        sn, so = wsw.WeChatStartupWatcher._find_wechat_pids(), old_wsw_find_wechat_pids()
        new_pid_sets = [sorted(p for _, p in kn), sorted(p for _, p in wn),
                        sorted(p for _, p in sn)]
        for new, old in ((kn, ko), (wn, wo), (sn, so)):
            assert sorted(p for _, p in new) == sorted(p for _, p in old)
            assert new == sorted(new, reverse=True), '必须仍是按内存降序'
        for mod in (kv2, kv3):
            pid, mem_mb = mod._find_weixin_pid()
            old_pid, _old_mb = old_find_weixin_pid()
            assert (pid is None) == (old_pid is None), (pid, old_pid)
            if pid is None:
                assert all(not s for s in new_pid_sets), new_pid_sets
            else:
                assert pid in set(new_pid_sets[0]), (pid, new_pid_sets)
                assert isinstance(mem_mb, int)

    def test_wait_dll_loaded_equivalent_with_fake_psutil(self, monkeypatch):
        monkeypatch.setattr(wke, '_psutil_warning_emitted', False)
        factory = lambda pid: _FakeProcessObj(maps=[r'C:\Program Files\WeChat\Weixin.dll'])
        with psutil_available(process_factory=factory):
            new, old = wke._wait_dll_loaded(4242, timeout=0.01), old_wait_dll_loaded(4242, timeout=0.01)
        assert new is True and old is True, (new, old)
        factory = lambda pid: _FakeProcessObj(maps=[r'C:\Windows\System32\ntdll.dll'])
        with psutil_available(process_factory=factory):
            new, old = wke._wait_dll_loaded(4242, timeout=0.01), old_wait_dll_loaded(4242, timeout=0.01)
        assert new is False and old is False, (new, old)
        factory = lambda pid: _FakeProcessObj(raises=_FakeNoSuchProcess('gone'))
        with psutil_available(process_factory=factory):
            new, old = wke._wait_dll_loaded(4242, timeout=0.01), old_wait_dll_loaded(4242, timeout=0.01)
        assert new is False and old is False, (new, old)

    def test_wait_dll_loaded_equivalent_with_real_psutil(self, monkeypatch):
        """本机真实 psutil + 不存在的 PID ⇒ 两边都必须返回 False。"""
        monkeypatch.setattr(wke, '_psutil_warning_emitted', False)
        new = wke._wait_dll_loaded(0x7FFFFFFF, timeout=0.01)
        old = old_wait_dll_loaded(0x7FFFFFFF, timeout=0.01)
        assert new is False and old is False

    @pytest.mark.parametrize('mod', [kv2, kv3], ids=['v2', 'v3'])
    def test_hook_backend_execution_unchanged_with_psutil(self, mod, monkeypatch):
        """有 psutil、没有微信进程：`initialize_hook()` 的旧文案逐字不变。"""
        monkeypatch.setattr(wke, '_psutil_warning_emitted', False)
        with psutil_available(procs=[]):
            ok = mod.initialize_hook()
        assert ok is False
        assert mod.get_last_error_msg() == 'Weixin.exe not running'


# ---------------------------------------------------------------------------
#  ⑥ 缺 psutil 时的调用点不得被异常打断（wx_startup_watcher 的 WMI 主路径）
# ---------------------------------------------------------------------------
class TestWatcherDegradesGracefully:
    def test_poll_loop_returns_instead_of_killing_the_thread(self, reset_warning_flag, capsys):
        """psutil 轮询兜底缺依赖时必须退出循环，而不是在子线程里抛异常空转。"""
        w = wsw.WeChatStartupWatcher()
        w._running = True
        with psutil_blocked():
            w._psutil_poll_loop()
        out = capsys.readouterr().out
        assert 'psutil' in out, out
        assert 'psutil polling active' not in out, out

    def test_check_new_pids_is_not_fatal(self, reset_warning_flag, capsys):
        w = wsw.WeChatStartupWatcher()
        with psutil_blocked():
            w._psutil_check_new_pids(set())
            w._psutil_check_new_pids(set())
        out = capsys.readouterr().out
        assert 'psutil' in out, out
        assert out.count('psutil 不可用 —— 跳过') == 1, '同一实例只提示一次: %r' % out


# ---------------------------------------------------------------------------
#  ⑦ 红线：出货面（会进 exe 的源码）不许再出现裸 `import psutil`
#
#  扫描范围 = `src/**` + 仓库根目录的 `.py`（build.bat / wechat_exp.spec 的入口是
#  `src\main.py`，`--add-data "src;src"`，另加 tools\silk_decoder*）。
#  **不在范围内**：`scripts/`（.gitignore:7 忽略）与 `tools/re/`（.gitignore:33 忽略）
#  里的 48 处裸 `import psutil` —— 它们是开发者/逆向辅助脚本，既不进 exe，也不被
#  `src/` 导入（`src` 里唯一涉及 tools 的是 `media.tools_dir`，指 silk_decoder.exe）。
#  详见 task-23-report.md「控制方指令/任务书里有误的地方」。
# ---------------------------------------------------------------------------
_SKIP_DIRS = {'build_venv', 'dist', 'build', 'backup', 'output', 'export',
              'tests', 'docs', 'scripts', 'tools', '.git', '.pytest_cache',
              '__pycache__'}


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _shipped_py_files():
    """出货面源码：`src/**/*.py` + 仓库根目录的 `*.py`。"""
    root = _repo_root()
    files = [os.path.join(root, n) for n in os.listdir(root) if n.endswith('.py')]
    for base, dirs, names in os.walk(os.path.join(root, 'src')):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        files += [os.path.join(base, n) for n in names if n.endswith('.py')]
    return sorted(set(files))


def _psutil_import_nodes(node, guarded=False):
    """递归收集 psutil 导入点 → [(行号, 是否被 try/except ImportError 包住)]。"""
    found = []
    for child in ast.iter_child_nodes(node):
        child_guarded = guarded
        if isinstance(child, ast.Try):
            for handler in child.handlers:
                if handler.type is None:
                    child_guarded = True
                else:
                    names = {n.id for n in ast.walk(handler.type)
                             if isinstance(n, ast.Name)}
                    if names & {'ImportError', 'ModuleNotFoundError',
                                'Exception', 'BaseException'}:
                        child_guarded = True
        if isinstance(child, ast.Import):
            if any(a.name.split('.')[0] == 'psutil' for a in child.names):
                found.append((child.lineno, child_guarded))
        elif (isinstance(child, ast.ImportFrom) and child.module
              and child.module.split('.')[0] == 'psutil'):
            found.append((child.lineno, child_guarded))
        found.extend(_psutil_import_nodes(child, child_guarded))
    return found


class TestRedLines:
    def test_no_unguarded_psutil_import_anywhere(self):
        """裸 `import psutil` 一旦回潮，缺 psutil 的环境（开发机 / 旧 exe / 精简安装）
        就会以 ImportError / 静默空结果崩掉。"""
        root, bad, seen, scanned = _repo_root(), [], 0, 0
        for path in _shipped_py_files():
            try:
                src = open(path, encoding='utf-8').read()
            except (OSError, UnicodeDecodeError):
                continue
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            scanned += 1
            for lineno, guarded in _psutil_import_nodes(tree):
                seen += 1
                if not guarded:
                    bad.append('%s:%d' % (os.path.relpath(path, root), lineno))
        assert scanned > 50, '扫描面不对（只扫了 %d 个文件）' % scanned
        assert seen > 0, '扫描本身没找到任何 import psutil —— 这个测试失去意义了'
        assert bad == [], '存在无护栏的 import psutil: %r' % bad

    def test_config_cipher_main_path_has_no_psutil_at_all(self):
        """主路径 Strategy 3（只读 Config.Cipher 扫描）连 import 都不该有 psutil。"""
        from engine.services import config_cipher_extract as cce
        tree = ast.parse(open(cce.__file__, encoding='utf-8').read())
        assert _psutil_import_nodes(tree) == []

    def test_config_cipher_pid_enumeration_is_pure_ctypes(self, reset_warning_flag):
        """主路径的 PID 枚举走 ctypes（`config_cipher_extract.find_wechat_pids`）。"""
        from engine.services import config_cipher_extract as cce
        with psutil_blocked() as finder:
            pids = cce.find_wechat_pids()
        assert isinstance(pids, list)
        assert finder.probes == [], '纯 ctypes 枚举不该碰 psutil: %r' % finder.probes
