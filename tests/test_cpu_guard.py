"""
Kilitlenme koruması testleri: makine geneli çekirdek bütçesi (`CpuSlotPool`), makineye göre çekirdek sayısı
(`available_cpus`) ve takılma bekçisi (stall watchdog). Gerçek LTspice çalıştırılmaz.
"""

import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import ltspice_llm.adapter as adapter_mod
import ltspice_llm.cpu_guard as guard
from ltspice_llm import LTSpiceAdapter
from ltspice_llm.cpu_guard import CpuSlotPool, CpuSlotTimeout

BUCK = "* buck\nV1 vin 0 12\nR1 vin vout 1k\nC1 vout 0 1u\n.tran 0 1m\n.meas tran vpp PP V(vout)\n.end\n"


def _pool(tmp_path, size):
    return CpuSlotPool(size=size, directory=tmp_path / "slots")


def _threads_in(netlist: Path):
    """LTspice'ın gördüğü değer: ilk ``.end``'den sonrası okunmaz (``read_text`` CR'leri gizlediği için bayt okunur)."""
    text = netlist.read_bytes().decode("utf-8")
    text = re.split(r"^[ \t]*\.end\b", text, maxsplit=1, flags=re.IGNORECASE | re.MULTILINE)[0]
    m = re.search(r"threads\s*=\s*(\d+)", text, re.IGNORECASE)
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------- makineye göre çekirdek
def test_available_cpus_follows_process_affinity(monkeypatch):
    monkeypatch.setattr(guard, "_affinity_count", lambda: 3)
    monkeypatch.setattr(guard.os, "cpu_count", lambda: 12)
    assert guard.available_cpus() == 3


def test_cpu_budget_env_caps_machine_and_process(monkeypatch):
    monkeypatch.setattr(guard, "_affinity_count", lambda: 12)
    monkeypatch.setattr(guard.os, "cpu_count", lambda: 12)
    monkeypatch.setenv(guard.ENV_CPU_BUDGET, "4")
    assert guard.machine_cpus() == 4
    assert guard.available_cpus() == 4


@pytest.mark.parametrize("bad", ["abc", "0", "-3"])
def test_invalid_cpu_budget_env_is_ignored(monkeypatch, bad):
    monkeypatch.setattr(guard.os, "cpu_count", lambda: 8)
    monkeypatch.setenv(guard.ENV_CPU_BUDGET, bad)
    assert guard.machine_cpus() == 8


def test_available_cpus_never_below_one(monkeypatch):
    monkeypatch.setattr(guard, "_affinity_count", lambda: None)
    monkeypatch.setattr(guard.os, "cpu_count", lambda: None)
    assert guard.available_cpus() == 1


def test_default_pool_uses_env_directory(tmp_path, monkeypatch):
    monkeypatch.setenv(guard.ENV_SLOT_DIR, str(tmp_path / "custom"))
    assert guard.default_pool().directory == tmp_path / "custom"


# --------------------------------------------------------------------------- bütçe semantiği
def test_pool_grants_partially_then_makes_new_runs_wait(tmp_path):
    pool = _pool(tmp_path, 4)
    first = pool.acquire(3)
    second = pool.acquire(3)          # yalnızca 1 yuva boş: aşırı abone etmek yerine azıyla yetinir
    assert (first.threads, second.threads) == (3, 1)
    with pytest.raises(CpuSlotTimeout):
        pool.acquire(1, wait_timeout=0.2)   # bütçe dolu: yeni koşum bekler
    first.release()
    third = pool.acquire(2, wait_timeout=1)
    assert third.threads == 2
    second.release(); third.release()
    assert pool.free_slots() == 4


def test_contended_request_asks_for_a_fair_share_instead_of_the_whole_machine(tmp_path):
    pool = _pool(tmp_path, 12)
    lone = pool.acquire(12, contended_want=3)
    assert lone.threads == 12                               # tek başına: tüm makine
    got = []
    waiter = threading.Thread(target=lambda: got.append(pool.acquire(12, contended_want=3, wait_timeout=5)))
    waiter.start()
    time.sleep(0.1)
    lone.release()
    waiter.join()
    assert got[0].threads == 3                              # bekledi: makineyi kapmak yerine pay aldı
    got[0].release()


def test_release_is_atomic_for_other_takers(tmp_path, monkeypatch):
    """
    Kira yuvaları tek tek bırakılıyordu: başka bir alıcı bu sırada yoklarsa yarım kalmış bırakmayı görüp payından
    az yuvayla dönüyordu (tam pakette yük altında: 3 yerine 2). Bırakma bitmeden kimse yuva almamalı.
    """
    pool = _pool(tmp_path, 12)
    lone = pool.acquire(12)
    lone_fds, real_unlock, probe = set(lone._fds), guard._unlock, []

    def slow_unlock(fd):
        real_unlock(fd)
        if fd in lone_fds and not probe:                    # ilk yuva boşaldı, 11'i hâlâ tutuluyor
            probe.append(threading.Thread(target=lambda: probe.append(pool._try_take(12))))
            probe[0].start()
            time.sleep(0.1)                                 # alıcıya yarım durumu görme fırsatı ver

    monkeypatch.setattr(guard, "_unlock", slow_unlock)
    lone.release()
    probe[0].join()
    taken = probe[1]
    for fd in taken:
        real_unlock(fd)
    assert len(taken) == 12


def test_exact_minimum_waits_for_enough_slots(tmp_path):
    pool = _pool(tmp_path, 4)
    held = pool.acquire(3)
    with pytest.raises(CpuSlotTimeout):
        pool.acquire(2, minimum=2, wait_timeout=0.2)
    held.release()
    assert pool.acquire(2, minimum=2, wait_timeout=1).threads == 2


def test_minimum_above_want_is_capped_to_want(tmp_path):
    # acquire sözleşmesi: "En az minimum, en çok want yuva alır"; minimum > want çelişkisi want'a indirilir
    pool = _pool(tmp_path, 3)
    assert pool.acquire(2, minimum=5, wait_timeout=0.5).threads == 2


def test_lease_is_released_by_context_manager_and_idempotent(tmp_path):
    pool = _pool(tmp_path, 2)
    with pool.acquire(2) as lease:
        assert pool.free_slots() == 0
    assert lease.released and pool.free_slots() == 2
    lease.release()  # ikinci kez bırakmak sorun çıkarmamalı
    assert pool.free_slots() == 2


def test_pool_never_oversubscribes_under_thread_contention(tmp_path):
    pool = _pool(tmp_path, 4)
    in_use, peak, lock = [0], [0], threading.Lock()

    def worker():
        with pool.acquire(2, wait_timeout=10) as lease:
            with lock:
                in_use[0] += lease.threads
                peak[0] = max(peak[0], in_use[0])
            time.sleep(0.02)
            with lock:
                in_use[0] -= lease.threads

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert 1 <= peak[0] <= 4


def test_slots_held_by_a_killed_process_are_recovered(tmp_path):
    """Çöken/öldürülen bir süreç bütçeyi kalıcı olarak kilitlememeli (işletim sistemi kilidi bırakır)."""
    directory = tmp_path / "slots"
    code = (
        "import sys, time; sys.path.insert(0, %r)\n"
        "from ltspice_llm.cpu_guard import CpuSlotPool\n"
        "lease = CpuSlotPool(size=3, directory=%r).acquire(3)\n"
        "print(lease.threads, flush=True); time.sleep(60)\n"
    ) % (str(Path(__file__).resolve().parent.parent), str(directory))
    with subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True) as child:
        try:
            assert child.stdout.readline().strip() == "3"
            pool = CpuSlotPool(size=3, directory=directory)
            with pytest.raises(CpuSlotTimeout):
                pool.acquire(1, wait_timeout=0.3)   # başka süreç tüm bütçeyi tutuyor
        finally:
            child.kill()
    assert pool.acquire(3, minimum=3, wait_timeout=5).threads == 3


# --------------------------------------------------------------------------- adaptör + bütçe
class _QuickPopen:
    """Anında biten sahte LTspice; başlatıldığı andaki threads değerini ve eşzamanlı toplamı kaydeder."""
    launched = []
    running = [0]
    peak = [0]
    lock = threading.Lock()
    raise_on_start = False

    def __init__(self, cmd, **kwargs):
        if type(self).raise_on_start:
            raise OSError("başlatılamadı")
        self.threads = _threads_in(Path(cmd[-1])) or 0
        self.pid, self.returncode = 999, None
        with self.lock:
            type(self).launched.append(self.threads)
            self.running[0] += self.threads
            self.peak[0] = max(self.peak[0], self.running[0])

    def communicate(self, timeout=None):
        time.sleep(0.02)
        with self.lock:
            self.running[0] -= self.threads
        self.returncode = 0
        return "", ""


@pytest.fixture
def quick_popen(monkeypatch):
    monkeypatch.setattr(adapter_mod.subprocess, "Popen", _QuickPopen)
    _QuickPopen.launched, _QuickPopen.running, _QuickPopen.peak = [], [0], [0]
    _QuickPopen.raise_on_start = False
    return _QuickPopen


def _adapter(tmp_path, **kw):
    return LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path / "ws", **kw)


def test_uncoordinated_concurrent_runs_stay_within_the_budget(tmp_path, quick_popen, monkeypatch):
    """Asıl tehlikeli senaryo: run_batch kullanmadan, iş parçacıklarından ayrı adaptörlerle aynı anda koşum."""
    monkeypatch.setattr(adapter_mod, "available_cpus", lambda: 6)
    pool = _pool(tmp_path, 6)

    def run(i):
        _adapter(tmp_path / str(i), cpu_guard=pool).run_netlist(BUCK, circuit_name=f"c{i}", treat_as_text=True)

    workers = [threading.Thread(target=run, args=(i,)) for i in range(12)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    assert len(quick_popen.launched) == 12
    assert all(n >= 1 for n in quick_popen.launched)       # her koşuma açık bir threads değeri verildi
    assert quick_popen.peak[0] <= 6                          # toplam hiçbir an bütçeyi aşmadı
    assert pool.free_slots() == 6                            # bütün kiralar bırakıldı


def test_full_budget_fails_cleanly_instead_of_oversubscribing(tmp_path, quick_popen):
    pool = _pool(tmp_path, 2)
    blocker = pool.acquire(2)
    try:
        res = _adapter(tmp_path, cpu_guard=pool, slot_wait_timeout=0.2).run_netlist(
            BUCK, circuit_name="c", treat_as_text=True)
    finally:
        blocker.release()
    assert not res.success
    assert "çekirdek bütçesi dolu" in res.errors[0]
    assert quick_popen.launched == []                        # LTspice hiç başlatılmadı


def test_lease_is_released_even_if_ltspice_cannot_start(tmp_path, quick_popen):
    quick_popen.raise_on_start = True
    pool = _pool(tmp_path, 3)
    res = _adapter(tmp_path, cpu_guard=pool).run_netlist(BUCK, circuit_name="c", treat_as_text=True)
    assert not res.success and "yürütme hatası" in res.errors[0]
    assert pool.free_slots() == 3
    assert res.solver_threads is None  # LTspice hiç başlamadı: kira değil None (SimulationResult.solver_threads)


def test_thread_limit_lands_before_first_end(tmp_path, quick_popen, monkeypatch):
    """İki ``.end``'li netlistte sınır ikincinin önüne yazılıyordu; LTspice okumadı (ölçüm: solver_threads=2 iken
    logda ``Maximum thread count: 12``, examples/12)."""
    monkeypatch.setattr(adapter_mod, "available_cpus", lambda: 4)  # tek çekirdekli makinede 2 -> 1 kırpılmasın
    net = BUCK + "* .end sonrası\n.end\n"
    _adapter(tmp_path, cpu_guard=_pool(tmp_path, 4), solver_threads=2).run_netlist(
        net, circuit_name="two_end", treat_as_text=True)
    assert quick_popen.launched == [2]


def test_threads_setting_after_end_is_not_taken_as_users(tmp_path, quick_popen, monkeypatch):
    """``.end`` sonrasındaki ``threads=3``'ü LTspice okumaz; kullanıcı ayarı sayılırsa hiç sınır konmazdı."""
    monkeypatch.setattr(adapter_mod, "available_cpus", lambda: 4)
    monkeypatch.setattr(guard.os, "cpu_count", lambda: 4)  # tutarlı makine: LTspice'ın sınırı da 4
    net = BUCK + ".options threads=3\n.end\n"
    res = _adapter(tmp_path, cpu_guard=_pool(tmp_path, 4), solver_threads=2).run_netlist(
        net, circuit_name="late_threads", treat_as_text=True)
    assert quick_popen.launched == [2]
    assert res.solver_threads == 2


def test_user_threads_setting_is_honoured_and_reserved(tmp_path, quick_popen, monkeypatch):
    monkeypatch.setattr(adapter_mod, "available_cpus", lambda: 8)
    monkeypatch.setattr(guard.os, "cpu_count", lambda: 8)  # CI'ın 2 çekirdeğinde LTspice 3'ü 2'ye kırpar (2026-10-11)
    pool = _pool(tmp_path, 8)
    net = BUCK.replace(".tran", ".options threads=3\n.tran")
    res = _adapter(tmp_path, cpu_guard=pool).run_netlist(net, circuit_name="u", treat_as_text=True)
    assert quick_popen.launched == [3]
    assert res.solver_threads == 3


def test_user_threads_above_available_cores_is_warned(tmp_path, quick_popen, monkeypatch):
    monkeypatch.setattr(adapter_mod, "available_cpus", lambda: 4)
    net = BUCK.replace(".tran", ".options threads=16\n.tran")
    res = _adapter(tmp_path, cpu_guard=_pool(tmp_path, 4)).run_netlist(net, circuit_name="u", treat_as_text=True)
    assert any("threads=16" in w and "kilitlenebilir" in w for w in res.warnings)


@pytest.mark.parametrize("logical_cpus,expected", [(12, 6), (4, 4)])
def test_result_reports_the_user_threads_that_ltspice_receives(tmp_path, quick_popen, monkeypatch, logical_cpus,
                                                               expected):
    """``SimulationResult.solver_threads``: LTspice çözücüsünün kullandığı iş parçacığı sayısı. Netlist ``threads``
    değerini kendisi veriyorsa ona dokunulmaz (``LTSpiceAdapter`` ``solver_threads`` parametresi): LTspice 6 alır ve
    onu makinenin mantıksal çekirdek sayısına kırpar; affinity'yi ve bütçeyi bilmez (examples/12 "LTspice threads'i
    mantıksal çekirdek sayısına kırpar", LTspice 26.1.1: 4 mantıksal çekirdekte threads=6 -> 4; affinity 1'de de
    varsayılan 4). Süreç 2 çekirdeğe bağlı ve bütçe 2: kira 2, sonuç kirayı bildiriyordu (test-reviewer,
    2026-10-11). Sahteler işletim sistemi sınırında: affinity, mantıksal çekirdek, ortam değişkeni."""
    monkeypatch.setattr(guard, "_affinity_count", lambda: 2)
    monkeypatch.setattr(guard.os, "cpu_count", lambda: logical_cpus)
    monkeypatch.setenv(guard.ENV_CPU_BUDGET, "2")
    pool = _pool(tmp_path, 12)
    net = BUCK.replace(".tran", ".options threads=6\n.tran")
    res = _adapter(tmp_path, cpu_guard=pool).run_netlist(net, circuit_name="u", treat_as_text=True)
    assert quick_popen.launched == [6]
    assert res.solver_threads == expected
    assert pool.free_slots() == 12  # kira bırakıldı


def test_result_does_not_guess_a_threads_expression(tmp_path, quick_popen, monkeypatch):
    """``.options threads={N}``'i LTspice kendisi çözer, adaptör çözemez (``_user_threads``: (True, None)). Sonuç
    kirayı (4) değil "bilinmiyor"u (None) bildirir (test-reviewer, 2026-10-11)."""
    monkeypatch.setattr(adapter_mod, "available_cpus", lambda: 4)
    pool = _pool(tmp_path, 4)
    net = BUCK.replace(".tran", ".param N=2\n.options threads={N}\n.tran")
    res = _adapter(tmp_path, cpu_guard=pool).run_netlist(net, circuit_name="u", treat_as_text=True)
    assert "{N}" in (tmp_path / "ws" / "u.net").read_text(encoding="utf-8")  # kullanıcının ifadesine dokunulmadı
    assert res.solver_threads is None
    assert pool.free_slots() == 4


def test_result_caps_the_adapters_own_limit_like_ltspice(tmp_path, quick_popen, monkeypatch):
    """Koruma kapalıyken adaptör ``solver_threads``'i olduğu gibi yazar (16). LTspice onu mantıksal çekirdek sayısına
    kırpar (examples/12 "LTspice threads'i mantıksal çekirdek sayısına kırpar"); sonuç 16 diyordu (test-reviewer,
    2026-10-11)."""
    monkeypatch.setattr(guard.os, "cpu_count", lambda: 4)
    res = _adapter(tmp_path, cpu_guard=False, solver_threads=16).run_netlist(BUCK, circuit_name="s", treat_as_text=True)
    assert quick_popen.launched == [16]
    assert res.solver_threads == 4


def test_full_budget_with_user_threads_reports_no_threads(tmp_path, quick_popen):
    """Kira alınamazsa LTspice başlamaz; netlistin kendi threads'i de bildirilmez (test-reviewer, 2026-10-11)."""
    pool = _pool(tmp_path, 2)
    blocker = pool.acquire(2)
    try:
        res = _adapter(tmp_path, cpu_guard=pool, slot_wait_timeout=0.2).run_netlist(
            BUCK.replace(".tran", ".options threads=2\n.tran"), circuit_name="c", treat_as_text=True)
    finally:
        blocker.release()
    assert "çekirdek bütçesi dolu" in res.errors[0]
    assert quick_popen.launched == []
    assert res.solver_threads is None


# --------------------------------------------------------------------------- takılma bekçisi
class _ScriptedPopen:
    """Her başlatmada sıradaki davranışı oynar: 'stuck' (hiç ilerlemez), 'grow' (log büyür), 'ok', 'fail_start'
    (süreç başlatılamaz, Popen OSError verir)."""
    script = []
    instances = []

    def __init__(self, cmd, **kwargs):
        self.netlist = Path(cmd[-1])
        self.threads = _threads_in(self.netlist)
        self.behaviour = type(self).script[len(type(self).instances)]
        if self.behaviour == "fail_start":
            raise OSError("başlatılamadı")
        self.pid, self.returncode, self.killed, self.polls = 999, None, False, 0
        type(self).instances.append(self)

    def communicate(self, timeout=None):
        if self.killed:
            return "", ""
        self.polls += 1
        if self.behaviour == "ok" or (self.behaviour == "grow" and self.polls > 40):
            self.returncode = 0
            return "", ""
        if self.behaviour == "grow":
            with open(self.netlist.with_suffix(".log"), "a", encoding="utf-8") as f:
                f.write("adim\n")
        time.sleep(timeout or 0)
        raise subprocess.TimeoutExpired("ltspice", timeout)


@pytest.fixture
def scripted(monkeypatch):
    monkeypatch.setattr(adapter_mod.subprocess, "Popen", _ScriptedPopen)
    monkeypatch.setattr(LTSpiceAdapter, "_WATCH_INTERVAL", 0.01)

    def fake_kill(process):
        process.killed, process.returncode = True, -9

    monkeypatch.setattr(LTSpiceAdapter, "_kill_process_tree", staticmethod(fake_kill))
    _ScriptedPopen.instances = []
    return _ScriptedPopen


def test_stalled_solver_is_killed_and_retried_single_threaded(tmp_path, scripted, monkeypatch):
    monkeypatch.setattr(adapter_mod, "available_cpus", lambda: 4)
    scripted.script = ["stuck", "ok"]
    pool = _pool(tmp_path, 4)
    res = _adapter(tmp_path, cpu_guard=pool, stall_timeout=0.1).run_netlist(BUCK, circuit_name="s", treat_as_text=True)
    first, second = scripted.instances
    assert first.killed and first.threads == 4
    assert second.threads == 1                                   # kurtarma: tek iş parçacığı
    # "threads=1 ile yeniden" başlamayan denemenin notunda da geçer (test-reviewer, 2026-10-11)
    assert any("yeniden çalıştırıldı" in w for w in res.warnings)
    assert not any("başlatılamadı" in w for w in res.warnings)
    assert not any("ilerlemedi (.log" in e for e in res.errors)  # ilk denemenin hatası sonuca taşınmaz
    assert res.solver_threads == 1
    assert pool.free_slots() == 4


class _TakenOnRetryPool(CpuSlotPool):
    """Takılan koşumun kirası bırakılınca başka bir süreç bütçenin hepsini kapar. Gerçek sınır: aynı dizindeki ikinci
    bir havuzun (başka bir sürecin) dosya kilitleri."""

    def __init__(self, size, directory):
        super().__init__(size=size, directory=directory)
        self.calls, self.other = 0, None

    def acquire(self, want, *args, **kwargs):
        self.calls += 1
        if self.calls == 2:
            self.other = CpuSlotPool(size=self.size, directory=self.directory).acquire(self.size)
        return super().acquire(want, *args, **kwargs)


def test_stall_retry_without_a_free_core_is_not_reported_as_rerun(tmp_path, scripted, monkeypatch):
    """Takılan koşum durdurulur; tek iş parçacıklı yeniden deneme için bütçe doluysa LTspice yeniden başlamaz. Uyarı
    "threads=1 ile yeniden çalıştırıldı" diyordu, hata ise "Yeniden deneme başlatılamadı" (test-reviewer, 2026-10-11).
    Sonuç son başlatılan (takılan) koşumun sayısıdır: kira 4 (``_run_ltspice``)."""
    monkeypatch.setattr(adapter_mod, "available_cpus", lambda: 4)
    monkeypatch.setattr(guard.os, "cpu_count", lambda: 4)
    scripted.script = ["stuck", "ok"]
    pool = _TakenOnRetryPool(4, tmp_path / "slots")
    res = _adapter(tmp_path, cpu_guard=pool, stall_timeout=0.1, slot_wait_timeout=0.2).run_netlist(
        BUCK, circuit_name="s", treat_as_text=True)
    pool.other.release()
    assert len(scripted.instances) == 1 and scripted.instances[0].killed and scripted.instances[0].threads == 4
    assert any("Yeniden deneme başlatılamadı" in e for e in res.errors)
    assert not any("yeniden çalıştırıldı" in w for w in res.warnings)
    assert sum("ilerlemedi" in w and "bütçesi dolu" in w for w in res.warnings) == 1  # neden de yazılır
    assert res.solver_threads == 4
    assert pool.free_slots() == 4


@pytest.mark.parametrize("solver_threads,expected", [(None, None), (3, 3)], ids=["varsayılan", "solver_threads=3"])
def test_stall_retry_that_cannot_start_ltspice_is_not_reported_as_rerun(tmp_path, scripted, monkeypatch,
                                                                        solver_threads, expected):
    """Takılan koşum durdurulur; tek iş parçacıklı yeniden denemede LTspice başlatılamazsa uyarı "yeniden
    çalıştırıldı" demez ve sonuç hiç başlamayan denemenin threads=1'ini bildirmez (test-reviewer, 2026-10-11). Sonuç
    son başlatılan (takılan) koşumun sayısıdır: LTspice varsayılanı (None) ya da min(3, 4 mantıksal çekirdek) = 3."""
    monkeypatch.setattr(guard.os, "cpu_count", lambda: 4)
    scripted.script = ["stuck", "fail_start"]
    res = _adapter(tmp_path, cpu_guard=False, stall_timeout=0.1, solver_threads=solver_threads).run_netlist(
        BUCK, circuit_name="s", treat_as_text=True)
    assert len(scripted.instances) == 1 and scripted.instances[0].killed
    assert any("yürütme hatası" in e for e in res.errors)
    assert not any("yeniden çalıştırıldı" in w for w in res.warnings)
    assert any("ilerlemedi" in w and "başlatılamadı" in w for w in res.warnings)
    assert not any("bütçesi dolu" in w for w in res.warnings)  # koruma kapalı: neden bütçe değil
    assert res.solver_threads == expected


def test_growing_output_is_never_mistaken_for_a_stall(tmp_path, scripted):
    scripted.script = ["grow"]
    _adapter(tmp_path, cpu_guard=_pool(tmp_path, 2), stall_timeout=0.05).run_netlist(
        BUCK, circuit_name="g", treat_as_text=True)
    (only,) = scripted.instances
    assert not only.killed and only.polls > 40                   # stall süresinin çok üstünde çalıştı


def test_stall_with_user_threads_is_reported_not_overridden(tmp_path, scripted, monkeypatch):
    monkeypatch.setattr(guard.os, "cpu_count", lambda: 4)
    scripted.script = ["stuck"]
    net = BUCK.replace(".tran", ".options threads=2\n.tran")
    res = _adapter(tmp_path, cpu_guard=_pool(tmp_path, 4), stall_timeout=0.1).run_netlist(
        net, circuit_name="u", treat_as_text=True)
    assert len(scripted.instances) == 1                         # kullanıcının ayarı ezilmez, yeniden denenmez
    assert any("livelock" in e for e in res.errors)
    assert res.solver_threads == 2                              # takılan koşum netlistin 2'siyle koştu


def test_stall_watchdog_can_be_disabled_and_timeout_still_applies(tmp_path, scripted):
    scripted.script = ["stuck"]
    res = _adapter(tmp_path, cpu_guard=False, stall_timeout=None, default_timeout=0.3).run_netlist(
        BUCK, circuit_name="t", treat_as_text=True)
    assert scripted.instances[0].killed
    assert any("zaman aşımına" in e for e in res.errors)


def test_watchdog_recovers_even_without_guard(tmp_path, scripted):
    """Koruma kapalı (ya da yük dışarıdan geliyor) olsa bile bekçi takılan koşumu kurtarır."""
    scripted.script = ["stuck", "ok"]
    res = _adapter(tmp_path, cpu_guard=False, stall_timeout=0.1).run_netlist(BUCK, circuit_name="s", treat_as_text=True)
    first, second = scripted.instances
    assert first.threads is None and second.threads == 1
    assert any("yeniden çalıştırıldı" in w for w in res.warnings)
    assert not any("başlatılamadı" in w for w in res.warnings)


@pytest.mark.parametrize("bad", [0, -1.5])
def test_invalid_stall_timeout_is_rejected(tmp_path, bad):
    with pytest.raises(ValueError, match="stall_timeout"):
        _adapter(tmp_path, stall_timeout=bad)
