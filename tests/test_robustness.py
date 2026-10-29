"""
LTspice süreci, çalışma alanı diski ve .raw belleği: Gemini incelemesinin (2026-10-06) ölçülerek tutan kısımları.
Ölçümler ve kaynaklar: examples/16_process_and_memory_hardening.py, docs/LESSONS_LEARNED_PERFORMANCE.md §10.

LTspice gerektirmez. Sahte olan yalnızca gerçek sınırdır (``subprocess.Popen``): başlatılınca gerçek LTspice 26.1.1
kayıtlarını netlistin yanına yazar ve kayıtlı çıkış koduyla döner (``ref26_crash_recursive.*``, examples/14 ``crash``;
``ref26_flow_rc.*``, examples/14 ``flow``). Job Object testleri gerçek Windows süreçleri başlatır.
"""

import builtins
import ctypes
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

import ltspice_llm.adapter as adapter_mod
import ltspice_llm.visualizer as visualizer_mod
from ltspice_llm import LTSpiceAdapter
from ltspice_llm.parser import ALL_TRACES, RawParser, open_raw, raw_axis_values, raw_steps, raw_wave
from ltspice_llm.report import read_trace
from ltspice_llm.visualizer import WaveformVisualizer

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(__file__).parent / "data"
CRASH = json.loads((DATA / "ref26_crash_recursive.json").read_text(encoding="utf-8"))
CRASH_NET = (DATA / "ref26_crash_recursive.net").read_text(encoding="utf-8")
RC = (DATA / "ref26_flow_rc.net").read_text(encoding="utf-8")
RC_RUN = {".log": DATA / "ref26_flow_rc.log.txt", ".raw": DATA / "ref26_flow_rc.raw"}
FLOW_RAW = DATA / "ref26_flow_rc.raw"
V_OUT_MAX = 3.58834481239  # .meas tran v_max MAX V(out), ref26_flow_rc.log.txt (LTspice 26.1.1)

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object / NTSTATUS")


class _Replay:
    """Kayıtlı LTspice çıktılarını netlistin yanına yazar ve kayıtlı çıkış kodu / stderr ile döner."""
    outputs: dict = {}
    returncode = 0
    stderr = ""

    def __init__(self, cmd, **kwargs):
        netlist = Path(cmd[-1])
        for suffix, source in type(self).outputs.items():
            netlist.with_suffix(suffix).write_bytes(source.read_bytes())
        self.pid, self.returncode = 999, type(self).returncode

    def communicate(self, timeout=None):
        return "", type(self).stderr


@pytest.fixture
def replay(monkeypatch):
    class Replay(_Replay):
        outputs = dict(RC_RUN)

    monkeypatch.setattr(adapter_mod.subprocess, "Popen", Replay)
    return Replay


def _adapter(tmp_path, **kwargs):
    return LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path / "ws", solver_threads=1, **kwargs)


# --------------------------------------------------------------------------- çökme kodu
def test_ltspice_crash_is_reported_with_its_exit_code(tmp_path, replay):
    """LTspice 26.1.1 kendini çağıran alt devrede 0xC00000FD ile çöküyor: stderr boş, log yalnızca 3 başlık satırı
    (examples/14 ``crash``). Eski hata: çıkış kodu yalnızca stderr doluysa yazılıyordu; sonuç yanıltıcı biçimde
    "Simülasyon dalga formu (.raw) üretilemedi. Devre sözdizimi veya bağlantılarını kontrol edin." diyordu."""
    replay.outputs = {".log": DATA / "ref26_crash_recursive.log.txt"}
    replay.returncode, replay.stderr = CRASH["returncode"], CRASH["stderr"]
    res = _adapter(tmp_path).run_netlist(CRASH_NET, circuit_name="crash", treat_as_text=True)
    assert not res.success
    crash = [e for e in res.errors if "0xC00000FD" in e]
    assert len(crash) == 1
    # Windows ileti tablosu (examples/16): 0xC00000FD -> Win32 1001 "Recursion too deep; the stack overflowed."
    assert "yığın taşması" in crash[0]
    assert ".subckt" in crash[0] and ".func" in crash[0]  # ölçülen neden (examples/16): kendini çağıran tanım
    assert not any("(.raw) üretilemedi" in e for e in res.errors)


def test_crash_of_a_circuit_file_still_leaves_its_folder_clean(tmp_path, replay):
    """Dosya yolu verilen devre kendi klasöründe koşar (run_netlist). LTspice çökse de geçici kopya ve log çalışma
    alanına taşınır: tasarımcının klasöründe yalnız kendi dosyası kalır, çökme logu okunur (test-reviewer, 2026-10-29)."""
    replay.outputs = {".log": DATA / "ref26_crash_recursive.log.txt"}
    replay.returncode, replay.stderr = CRASH["returncode"], CRASH["stderr"]
    src = tmp_path / "proj" / "crash.net"
    src.parent.mkdir()
    src.write_text(CRASH_NET, encoding="utf-8")
    ad = _adapter(tmp_path)
    res = ad.run_netlist(src, circuit_name="crash")
    assert not res.success
    assert sum("0xC00000FD" in e for e in res.errors) == 1
    assert res.log_path == ad.workspace_dir / "crash.log"
    assert sorted(p.name for p in src.parent.iterdir()) == ["crash.net"]


@pytest.mark.parametrize("code, name", [
    (0xC0000005, "erişim ihlali"),        # "The instruction at 0x%p referenced memory at 0x%p." (ntdll)
    (0xC0000094, "tamsayı sıfıra bölme"),  # "{EXCEPTION} Integer division by zero." (ntdll)
    (0xC0000017, "yetersiz bellek"),       # "{Not Enough Quota} Not enough virtual memory..." (ntdll)
    (0xC0001234, "bilinmeyen"),            # tabloda olmayan hata önem dereceli NTSTATUS da çökme sayılır
])
def test_every_error_severity_ntstatus_exit_is_a_crash(tmp_path, replay, code, name):
    replay.outputs = {".log": DATA / "ref26_crash_recursive.log.txt"}
    replay.returncode = code
    res = _adapter(tmp_path).run_netlist(CRASH_NET, circuit_name="crash", treat_as_text=True)
    assert not res.success
    (crash,) = [e for e in res.errors if f"0x{code:08X}" in e]
    assert name in crash


def test_posix_signal_exit_is_a_crash(tmp_path, replay):
    """POSIX'te sinyalle ölen süreç negatif çıkış kodu döner (subprocess belgesi: ``-N`` = N sinyali)."""
    replay.outputs = {".log": DATA / "ref26_crash_recursive.log.txt"}
    replay.returncode = -int(signal.SIGSEGV)
    res = _adapter(tmp_path).run_netlist(CRASH_NET, circuit_name="crash", treat_as_text=True)
    assert any("SIGSEGV" in e for e in res.errors)


def test_ordinary_nonzero_exit_is_not_called_a_crash(tmp_path, replay):
    """LTspice hata mesajlı koşumlarda rc=1 döner ve hata logdadır (ölçüldü: 'Division by zero.' rc=1); çökme değil."""
    replay.outputs = {".log": DATA / "ref26_syntax_bad_line.log.txt"}
    replay.returncode = 1
    res = _adapter(tmp_path).run_netlist(RC, circuit_name="syntax", treat_as_text=True)
    assert not res.success and res.errors
    assert not any("çöktü" in e for e in res.errors)


# --------------------------------------------------------------------------- Job Object (yetim süreç, WER)
def _wait_for_exit(pid, seconds):
    """Windows: süreç ``seconds`` içinde biterse True (OpenProcess(SYNCHRONIZE) + WaitForSingleObject)."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = k32.OpenProcess(0x00100000, 0, pid)  # SYNCHRONIZE
    if not handle:
        return True  # süreç zaten yok
    try:
        return k32.WaitForSingleObject(handle, int(seconds * 1000)) == 0  # WAIT_OBJECT_0
    finally:
        k32.CloseHandle(handle)


_PARENT = (
    "import subprocess, sys, time\n"
    "import ltspice_llm.adapter as a\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "print(child.pid, a._assign_to_job(child), flush=True)\n"
    "time.sleep(60)\n"
)


@windows_only
def test_child_dies_with_the_python_process_that_started_it():
    """Ölçüm (examples/16): ebeveyn Python TerminateProcess ile ölünce LTspice yetim kalıp 10 sn'de 19,7 CPU-sn
    harcadı ve çekirdek bütçesi 12/12 boş göründü (yeni koşum yetimle birlikte çekirdekleri aşırı abone eder).
    İş nesnesinin tanıtıcısı ebeveynle birlikte kapanır, çekirdek çocuğu sonlandırır."""
    parent = subprocess.Popen([sys.executable, "-c", _PARENT], cwd=ROOT, stdout=subprocess.PIPE, text=True)
    child_pid = None
    try:
        pid_text, assigned = parent.stdout.readline().split()
        child_pid = int(pid_text)
        assert assigned == "True"
        assert not _wait_for_exit(child_pid, 0.5)  # ebeveyn yaşarken çocuk çalışıyor
        parent.kill()  # TerminateProcess: yalnızca ebeveyn (çökme / görev yöneticisi gibi)
        parent.wait(timeout=10)
        assert _wait_for_exit(child_pid, 10)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=10)
        parent.stdout.close()
        if child_pid is not None and not _wait_for_exit(child_pid, 0):
            subprocess.run(["taskkill", "/F", "/PID", str(child_pid)], capture_output=True, check=False)


@windows_only
def test_job_suppresses_windows_error_reporting_for_its_processes():
    """JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION (0x400) iş nesnesindeki her sürece SEM_NOGPFAULTERRORBOX uygular.
    Ölçüm (examples/16, varsayılan hata kipi): çöken LTspice WerFault ile 3,5-4,1 sn'de, bu bayrakla 0,28-0,30 sn'de
    döndü. Gemini'nin önerdiği SetErrorMode Python sürecinin kendi kipini de değiştirirdi; bayrak yalnızca LTspice'a
    uygulanır. KILL_ON_JOB_CLOSE (0x2000) yetim süreci önler."""
    flags = adapter_mod._PROCESS_JOB.limit_flags()
    assert flags & 0x400 and flags & 0x2000


def _job_process_count():
    """İşe şimdiye dek bağlanan süreç sayısı: işletim sisteminin kendi sayacı (QueryInformationJobObject,
    JobObjectBasicAccountingInformation = 1, JOBOBJECT_BASIC_ACCOUNTING_INFORMATION.TotalProcesses)."""
    class Accounting(ctypes.Structure):
        _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                    ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                    ("TotalPageFaultCount", ctypes.c_uint32), ("TotalProcesses", ctypes.c_uint32),
                    ("ActiveProcesses", ctypes.c_uint32), ("TotalTerminatedProcesses", ctypes.c_uint32)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.QueryInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
                                              ctypes.c_void_p]
    info = Accounting()
    assert k32.QueryInformationJobObject(adapter_mod._PROCESS_JOB._job(), 1, ctypes.byref(info), ctypes.sizeof(info),
                                         None), ctypes.get_last_error()
    return info.TotalProcesses


@windows_only
def test_execute_puts_the_ltspice_process_in_the_job(tmp_path):
    """Süreç bitmeden işe bağlanmalı: biten süreç bağlanamaz ve sayaç artmaz (test-reviewer ölçtü; eski sürümdeki
    mock testi bunu göremiyordu). Çocuk asıl yorumlayıcıdır: Windows venv'inin ``Scripts\\python.exe``'si bir
    başlatıcıdır ve yorumlayıcıyı ikinci süreç olarak açar, sayaç 2 artar (2026-10-29 ölçümü: 81 -> 83)."""
    interpreter = getattr(sys, "_base_executable", None) or sys.executable
    before = _job_process_count()
    outcome = _adapter(tmp_path)._execute([interpreter, "-c", "import time; time.sleep(0.2)"], 30, (), None)
    assert outcome.errors == []
    assert _job_process_count() == before + 1


def test_fake_process_without_a_handle_is_left_alone():
    """Testlerin sahte Popen'ı gerçek bir süreç değildir (pid 999); iş nesnesine PID ile atanmaya çalışılmaz."""
    assert adapter_mod._assign_to_job(_Replay.__new__(_Replay)) is False


# --------------------------------------------------------------------------- çalışma alanı diski
def _touch(path, size, age_seconds):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))


def _artifact_bytes(workspace):
    return sum(p.stat().st_size for p in workspace.rglob("*") if p.is_file()
               and p.suffix in (".net", ".asc", ".log", ".raw", ".db", ".png"))


def test_cleanup_workspace_also_cleans_batch_directories(tmp_path):
    """Ölçüm (2026-10-06): varsayılan çalışma alanı 9 günde 3,16 GB; 1,82 GB'ı run_batch'in ``_batch_*`` alt
    dizinlerindeydi ve cleanup_workspace alt dizinlere hiç girmiyordu."""
    ad = _adapter(tmp_path)
    ws = ad.workspace_dir
    _touch(ws / "_batch_a_0" / "a.raw", 10, 3600)
    _touch(ws / "_batch_a_0" / "a.log", 10, 3600)
    _touch(ws / "_batch_b_1" / "b.raw", 10, 0)
    _touch(ws / "mine" / "keep.raw", 10, 3600)          # kullanıcının dizini: adaptörün değil
    _touch(ws / "_batch_c_2" / "notes.txt", 10, 3600)    # simülasyon çıktısı değil
    assert ad.cleanup_workspace(60) == 2
    assert not (ws / "_batch_a_0").exists()              # boşalan toplu koşum dizini de silinir
    assert (ws / "_batch_b_1" / "b.raw").exists()
    assert (ws / "mine" / "keep.raw").exists()
    assert (ws / "_batch_c_2" / "notes.txt").exists()


def test_quota_deletes_the_oldest_runs_but_never_a_live_results_files(tmp_path, replay):
    """FIFO: en eski koşum bütün dosyalarıyla silinir. Grafik araçları .raw'ı sonradan circuit_name ile okur
    (tools.py); elinde tutulan bir SimulationResult'ın dosyaları ise hiç silinmez."""
    ad = _adapter(tmp_path)
    ws = ad.workspace_dir
    live = ad.run_netlist(RC, circuit_name="oldest_live", treat_as_text=True)
    ad.run_netlist(RC, circuit_name="older_dropped", treat_as_text=True)  # sonuç tutulmuyor
    newest = ad.run_netlist(RC, circuit_name="newest", treat_as_text=True)
    for stem, age in (("oldest_live", 300), ("older_dropped", 200)):
        for p in ws.glob(f"{stem}.*"):
            os.utime(p, (time.time() - age, time.time() - age))
    dropped = sorted(p.name for p in ws.glob("older_dropped.*"))
    assert dropped == ["older_dropped.log", "older_dropped.net", "older_dropped.raw"]
    one_run = sum((ws / name).stat().st_size for name in dropped)
    quota_mb = (_artifact_bytes(ws) - one_run / 2) / 1e6  # en az bir koşum silinmeli
    assert ad.cleanup_workspace(max_total_mb=quota_mb) == 3
    assert not list(ws.glob("older_dropped.*"))
    assert live.raw_path.exists() and live.log_path.exists()
    assert newest.raw_path.exists()
    assert _artifact_bytes(ws) <= quota_mb * 1e6


def test_quota_deletes_the_older_of_two_unheld_runs(tmp_path, replay):
    """Tutulmayan iki koşumdan birinin silinmesi yetiyorsa silinen eskisidir (FIFO). Yukarıdaki testte silinebilir tek
    koşum vardı; en yeniyi silen (LIFO) sıralama oradan sağ çıkıyordu (test-reviewer)."""
    ad = _adapter(tmp_path)
    ws = ad.workspace_dir
    for name in ("old", "mid"):
        ad.run_netlist(RC, circuit_name=name, treat_as_text=True)  # sonuç tutulmuyor
    for stem, age in (("old", 300), ("mid", 200)):
        for p in ws.glob(f"{stem}.*"):
            os.utime(p, (time.time() - age, time.time() - age))
    one_run = sum(p.stat().st_size for p in ws.glob("old.*"))
    assert ad.cleanup_workspace(max_total_mb=(_artifact_bytes(ws) - one_run / 2) / 1e6) == 3
    assert not list(ws.glob("old.*"))
    assert sorted(p.suffix for p in ws.glob("mid.*")) == [".log", ".net", ".raw"]


def test_quota_keeps_the_bode_plot_of_a_live_result(tmp_path, replay):
    """plot_bode_diagram grafiği ``<ad>_bode.png`` adıyla yazar (visualizer.plot_bode) ve yolunu LLM'e verir. Eski hata
    (Gemini, 2026-10-06): kota dosyaları ilk noktaya kadarki adla grupluyordu; ``filt_bode`` elde tutulan ``filt``
    koşumu sayılmıyor, grafik yetim dosya gibi siliniyordu. Tutulmayan koşumun grafiği ise koşumuyla birlikte gider."""
    ad = _adapter(tmp_path)
    ws = ad.workspace_dir
    live = ad.run_netlist(RC, circuit_name="filt", treat_as_text=True)
    ad.run_netlist(RC, circuit_name="old", treat_as_text=True)  # sonuç tutulmuyor
    for stem in ("filt", "old"):
        (ws / f"{stem}_bode.png").write_bytes(b"\x89PNG" + b"\0" * 1000)
    for p in ws.glob("old*"):
        os.utime(p, (time.time() - 300, time.time() - 300))
    assert ad.cleanup_workspace(max_total_mb=0) == 4  # old.net/.log/.raw ve old_bode.png
    assert sorted(p.name for p in ws.iterdir()) == ["filt.log", "filt.net", "filt.raw", "filt_bode.png"]
    assert live.raw_path.exists()


def test_quota_does_not_give_a_live_circuit_named_x_bode_to_circuit_x(tmp_path, replay):
    """``x_bode.png`` adı iki şey olabilir: ``x``'in Bode grafiği ya da ``x_bode`` adlı devrenin dalga formu grafiği
    (``<ad>.png``). Elde tutulan ``x_bode`` koşumunun grafiği, tutulmayan ``x`` silinirken gitmemeli."""
    ad = _adapter(tmp_path)
    ws = ad.workspace_dir
    ad.run_netlist(RC, circuit_name="x", treat_as_text=True)  # sonuç tutulmuyor
    live = ad.run_netlist(RC, circuit_name="x_bode", treat_as_text=True)
    (ws / "x_bode.png").write_bytes(b"\x89PNG" + b"\0" * 1000)
    for p in ws.glob("x.*"):
        os.utime(p, (time.time() - 300, time.time() - 300))
    assert ad.cleanup_workspace(max_total_mb=0) == 3
    assert sorted(p.name for p in ws.iterdir()) == ["x_bode.log", "x_bode.net", "x_bode.png", "x_bode.raw"]
    assert live.raw_path.exists()


def test_quota_deletes_an_unheld_runs_bode_plot_together_with_the_run(tmp_path, replay):
    """Bode grafiği koşumunun parçası: tutulmayan en eski koşum silinirken grafiği yetim kalmaz. Kota yalnız bir
    koşumluk yer açtırır; grafik ayrı gruplanırsa (eski kod) old.* silinince kota dolar ve old_bode.png kalırdı
    (test-reviewer: kota 0'da her şey silindiği için yukarıdaki test bunu görmüyordu)."""
    ad = _adapter(tmp_path)
    ws = ad.workspace_dir
    for name in ("old", "mid"):
        ad.run_netlist(RC, circuit_name=name, treat_as_text=True)  # sonuç tutulmuyor
    (ws / "old_bode.png").write_bytes(b"\x89PNG" + b"\0" * 1000)
    for pattern, age in (("old.*", 300), ("old_bode.png", 250), ("mid.*", 200)):
        for p in ws.glob(pattern):
            os.utime(p, (time.time() - age, time.time() - age))
    one_run = sum(p.stat().st_size for p in ws.glob("old.*"))
    assert ad.cleanup_workspace(max_total_mb=(_artifact_bytes(ws) - one_run / 2) / 1e6) == 4
    assert sorted(p.name for p in ws.iterdir()) == ["mid.log", "mid.net", "mid.raw"]


def test_quota_smaller_than_one_run_keeps_the_run_just_made(tmp_path, replay):
    """Kota sonuç kaydedildikten sonra uygulanır: yeni koşumun dosyaları kotadan büyük olsa da silinmez."""
    one_run = sum(p.stat().st_size for p in RC_RUN.values()) + len(RC.encode())
    res = _adapter(tmp_path, workspace_quota_mb=0.5 * one_run / 1e6).run_netlist(RC, circuit_name="only",
                                                                                treat_as_text=True)
    assert res.raw_path.exists() and res.log_path.exists()


def test_workspace_quota_is_enforced_after_run_batch(tmp_path, replay):
    """İşçiler kotasız koşar, kota toplu koşumdan sonra bütün alana uygulanır. Bir toplu koşumun sonuçları dönene
    kadar elde tutulur; önceki toplu koşumun (sonuçları atılmış) _batch_* dizinleri silinir."""
    one_run = sum(p.stat().st_size for p in RC_RUN.values()) + len(RC.encode())
    ad = _adapter(tmp_path, workspace_quota_mb=2.5 * one_run / 1e6)

    def jobs(tag):
        return [{"netlist_content_or_path": RC, "circuit_name": f"{tag}{i}", "treat_as_text": True} for i in range(2)]

    ad.run_batch(jobs("a"))  # sonuçlar tutulmuyor
    second = ad.run_batch(jobs("b"))
    ws = ad.workspace_dir
    assert sorted(p.name for p in ws.iterdir() if p.name.startswith("_batch_")) == ["_batch_b0_0", "_batch_b1_1"]
    assert all(r.raw_path.exists() for r in second)
    assert _artifact_bytes(ws) <= 2.5 * one_run


def test_workspace_quota_is_enforced_after_each_run(tmp_path, replay):
    one_run = sum(p.stat().st_size for p in RC_RUN.values()) + len(RC.encode())
    quota_mb = 2.5 * one_run / 1e6  # iki koşum sığar, üçüncüsü sığmaz
    ad = _adapter(tmp_path, workspace_quota_mb=quota_mb)
    for i in range(5):
        last = ad.run_netlist(RC, circuit_name=f"run{i}", treat_as_text=True)
    assert _artifact_bytes(ad.workspace_dir) <= quota_mb * 1e6
    assert last.raw_path.exists() and last.measurements["v_max"] == pytest.approx(V_OUT_MAX)
    assert not list(ad.workspace_dir.glob("run0.*"))


def test_workspace_quota_must_be_positive(tmp_path):
    with pytest.raises(ValueError):
        _adapter(tmp_path, workspace_quota_mb=0)


def test_cleanup_needs_an_age_or_a_quota(tmp_path):
    with pytest.raises(ValueError):
        _adapter(tmp_path).cleanup_workspace()


# --------------------------------------------------------------------------- .raw belleği
@pytest.fixture
def raw_reads(monkeypatch):
    """Gerçek sınır (dosya sistemi): ``FLOW_RAW``'dan okunan bayt sayısı."""
    real_open, count = builtins.open, {"bytes": 0}

    def counting_open(file, mode="r", *args, **kwargs):
        f = real_open(file, mode, *args, **kwargs)
        if "b" in mode and isinstance(file, (str, Path)) and Path(file).resolve() == FLOW_RAW.resolve():
            read = f.read

            def counted(n=-1):
                data = read(n)
                count["bytes"] += len(data)
                return data

            f.read = counted
        return f

    monkeypatch.setattr(builtins, "open", counting_open)
    return count


def test_summary_reads_the_raw_file_once(raw_reads):
    """spicelib 1.6.3 ikili .raw'da (LTspice varsayılanı, 'Normal access') tembel okunan her iz için bütün veri
    bloğunu yeniden okuyup izin önbelleğinde tutuyor. Ölçüm (examples/16): 67 MB .raw, 13 sinyal -> özet +939 MB
    tepe bellek; tek geçişte +132 MB, değerler birebir aynı. Bu fixture'da eski kod 17 722 baytlık dosyadan
    34 784 bayt okuyordu (2 iz, iz başına bir veri bloğu). Tek geçişte başlık ve veri bir kez okunur, artı
    spicelib'in başlıktaki 6 baytlık kodlama yoklaması: 17 728 bayt."""
    summary = RawParser.summarize_file(FLOW_RAW)
    assert FLOW_RAW.stat().st_size <= raw_reads["bytes"] < 1.5 * FLOW_RAW.stat().st_size  # alt sınır: sayaç okumayı gördü
    assert summary["V(out)"].max_val == pytest.approx(V_OUT_MAX, rel=1e-6)


# .meas tran in_avg/in_rms/out_avg/out_rms, adım 1 (ton = 1 µs) ve 2 (ton = 3 µs): ref26_mean_pulse.log.txt
# (LTspice 26.1.1, examples/14 ``mean``)
MEAN_MEAS = {"V(in)": ((0.505000000011, 1.58638861211), (1.50500000002, 2.74164777644)),
             "V(out)": ((0.504999999903, 1.5086738856), (1.50500000491, 2.69743240104))}


@pytest.fixture
def pulse_raw(tmp_path):
    """Steplenmiş .raw'ı spicelib yanındaki .log ile okur (adım sayısı oradan)."""
    for suffix, name in ((".raw", "ref26_mean_pulse.raw"), (".log", "ref26_mean_pulse.log.txt")):
        (tmp_path / f"pulse{suffix}").write_bytes((DATA / name).read_bytes())
    return tmp_path / "pulse.raw"


def test_summary_mean_and_rms_are_time_weighted(pulse_raw):
    """LTspice'ın zaman adımı bu kayıtta düzlükte 48,8 ns, kenarlarda 0,49 ps (oran 1e5). Eski hata (examples/16 T7,
    Gemini 2026-10-06): özet örneklerin aritmetik ortalamasıydı; kenar çevresinde sıklaşan noktalar fazla ağırlık
    alıyordu (V(in) yüksek seviyesi sürenin %10,0'ı, noktaların %11,7'si; ortalama 0,5896 V, %16,8 fazla). Elle (V(in), iki 10 ns kenar, periyot 10 µs): AVG = 5·(ton + 10n)/10u,
    RMS = √(25·(ton + 2·10n/3)/10u). RMS'te y²'nin yamuk integrali kenarlarda %0,15 fazla verir; doğrusal ara değerin
    karesinin integrali Δt·(a² + ab + b²)/3 elle hesaba 1e-6 içinde oturur."""
    summary = RawParser.summarize_file(pulse_raw)
    for step, ton in ((1, 1e-6), (2, 3e-6)):
        v_in = summary[f"V(in) [step {step}]"]
        assert v_in.mean_val == pytest.approx(5 * (ton + 10e-9) / 10e-6, rel=1e-9)
        assert v_in.rms_val == pytest.approx(np.sqrt(25 * (ton + 2 * 10e-9 / 3) / 10e-6), rel=1e-6)
        for trace, measured in MEAN_MEAS.items():
            avg, rms = measured[step - 1]
            assert summary[f"{trace} [step {step}]"].mean_val == pytest.approx(avg, rel=1e-9)
            assert summary[f"{trace} [step {step}]"].rms_val == pytest.approx(rms, rel=2e-5)  # ölçülen fark ≤ 7,5e-6


def test_tran_preview_is_evenly_spaced_in_time():
    """Önizleme 15 değeri zaman bilgisi olmadan verir; okuyan (LLM) onları zamanda eşit aralıklı sanır. Eski hata
    (Gemini 2026-10-06, ölçüm examples/18 T2): değerler indeks aralıklı seçiliyordu; LTspice'ın adımı değişken, bu
    kayıtta önizleme zamanları eşit aralığın 0,69-1,05 katıydı, aşağıdaki elle hesaptan en çok 0,27 V sapıyordu.
    Elle (RC τ = 1 ms, 5 V darbe 0-1 ms ve 2-3 ms, examples/14 ``flow``): V(out) = 5(1 − e^(−t/τ)) [0, 1m];
    V1·e^(−(t − 1m)/τ) [1m, 2m]; 5 − (5 − V2)·e^(−(t − 2m)/τ) [2m, 3m]; V1 = 5(1 − e^(−1)), V2 = V1·e^(−1).
    Tolerans 0,1 mV: LTspice'ın çözümü bu kayıtta bütün noktalarda elle hesaptan en çok 8,0 µV sapıyor; ara değer
    yerine en yakın örneği alan önizleme (en büyük adım 2,93 µs, eğim 5 V/ms) mV mertebesinde sapar (test-reviewer)."""
    tau, v1 = 1e-3, 5 * (1 - np.exp(-1))
    v2 = v1 * np.exp(-1)

    def v_out(t):
        if t <= 1e-3:
            return 5 * (1 - np.exp(-t / tau))
        if t <= 2e-3:
            return v1 * np.exp(-(t - 1e-3) / tau)
        return 5 - (5 - v2) * np.exp(-(t - 2e-3) / tau)

    preview = RawParser.summarize_file(FLOW_RAW)["V(out)"].downsampled_points
    assert preview == pytest.approx([v_out(t) for t in np.linspace(0, 3e-3, 15)], abs=1e-4)


def test_stepped_tran_preview_uses_each_steps_own_time_axis(pulse_raw):
    """Her adımın zaman ekseni ayrı (adım 1: 1082, adım 2: 1087 nokta). Elle: önizleme zamanları k·50µ/14; V(in)
    PULSE(0 5 0 10n 10n ton 10u), periyot içindeki konum 10 ns-ton+10 ns arasındaysa 5 V. Konumlar kenarlardan en az
    0,15 µs uzakta (adım 2'de 2,857 µs, düşen kenar 3,01 µs); k = 0 ve k = 14 (50 µs) yükselen kenarın başında, 0 V."""
    summary = RawParser.summarize_file(pulse_raw)
    for step, ton in ((1, 1e-6), (2, 3e-6)):
        phase = np.linspace(0, 50e-6, 15) % 10e-6
        expected = np.where((phase > 10e-9) & (phase < ton + 10e-9), 5.0, 0.0)
        assert summary[f"V(in) [step {step}]"].downsampled_points == pytest.approx(expected, abs=1e-6)


# ref26_rc_tstart (examples/14 ``tstart``, LTspice 26.1.1): examples/14 RC'si .tran 0 3m 1m ile; .meas değerleri logdan.
# LTspice'ın .meas zamanları mutlak (AVG penceresi logda FROM 0.001 TO 0.003).
TSTART_RAW = DATA / "ref26_rc_tstart.raw"
TSTART_MEAS = {"t_fall": 0.00105215400853, "t_rise": 0.0026516161884, "v_avg": 2.28613173513}


def test_axis_of_a_tran_with_tstart_starts_at_tstart():
    """Eski hata (2026-10-06, examples/18 T3): LTspice Tstart > 0'da .raw eksenini 0'dan yazıp kaymayı başlığın Offset
    alanına koyuyor, spicelib 1.6.3 uygulamıyor: grafik ve read_trace 1 ms kayıktı (0-2 ms). Tanık LTspice'ın kendi
    WHEN zamanları; elle de V(out) 3 V'u 1m + τ·ln(V1/3)'te düşerek, 2m + τ·ln((5 − V2)/2)'de yükselerek geçer.
    Logda 12 hane: geçişler 1e-12 s içinde."""
    # Tembel açılışta iz okunmadan ilk çağrı ekseni ayrı yoldan yükler (spicelib 1.6 RuntimeError) (test-reviewer)
    lazy = raw_axis_values(open_raw(TSTART_RAW))
    assert (lazy[0], lazy[-1]) == pytest.approx((1e-3, 3e-3), abs=1e-12)  # .tran 0 3m 1m
    x, y = read_trace(TSTART_RAW, "V(out)")
    assert (x[0], x[-1]) == pytest.approx((1e-3, 3e-3), abs=1e-12)
    i = np.flatnonzero(np.diff(np.sign(y - 3.0)))
    crossings = x[i] + (3.0 - y[i]) * (x[i + 1] - x[i]) / (y[i + 1] - y[i])
    assert crossings == pytest.approx([TSTART_MEAS["t_fall"], TSTART_MEAS["t_rise"]], abs=1e-12)
    (line,) = WaveformVisualizer._build_trace_figure(TSTART_RAW, ["V(out)"], "t").axes[0].get_lines()
    assert (line.get_xdata()[0], line.get_xdata()[-1]) == pytest.approx((1e-3, 3e-3), abs=1e-12)


def test_every_step_of_a_stepped_tran_with_tstart_starts_at_tstart(tmp_path):
    """ref26_rc_tstart_step (examples/14 ``tstart``): aynı RC, .step param r list 1k 2k, .tran 0 3m 1m. Her adımın
    ekseni 0'dan yazılıyor, Offset tek; yalnız ilk adıma uygulanırsa ikinci adımın grafiği kayık kalır (test-reviewer).
    Tanık her adımın .meas WHEN V(out)=2.5 RISE=1 zamanı (log); elle 2m + τ·ln((5 − V2)/2,5): 2,428473 ms, 2,840967 ms."""
    for suffix, name in ((".raw", "ref26_rc_tstart_step.raw"), (".log", "ref26_rc_tstart_step.log.txt")):
        (tmp_path / f"x{suffix}").write_bytes((DATA / name).read_bytes())
    raw = open_raw(tmp_path / "x.raw", traces=ALL_TRACES)
    assert raw_steps(raw) == [0, 1]
    for step, t_meas in ((0, 0.00242847317622), (1, 0.00284096690465)):
        x, y = raw_axis_values(raw, step), np.asarray(raw_wave(raw, "V(out)", step), dtype=float)
        assert (x[0], x[-1]) == pytest.approx((1e-3, 3e-3), abs=1e-12)
        i = np.flatnonzero((y[:-1] < 2.5) & (y[1:] >= 2.5))[0]
        assert x[i] + (2.5 - y[i]) * (x[i + 1] - x[i]) / (y[i + 1] - y[i]) == pytest.approx(t_meas, abs=1e-12)
    lines = WaveformVisualizer._build_trace_figure(tmp_path / "x.raw", ["V(out)"], "t").axes[0].get_lines()
    assert [line.get_xdata()[0] for line in lines] == pytest.approx([1e-3, 1e-3], abs=1e-12)


def test_summary_of_a_tran_with_tstart_covers_the_saved_window():
    """Özet kaydedilen pencereyi (1-3 ms) kapsar: ortalama LTspice'ın aynı penceredeki .meas AVG'si; önizleme 1-3 ms'de
    eşit aralıklı. Elle (RC τ = 1 ms): V(out) = V1·e^(−(t − 1m)/τ) [1m, 2m], 5 − (5 − V2)·e^(−(t − 2m)/τ) [2m, 3m].
    Izgara 0'dan başlasaydı önizlemenin ilk üçte biri ilk örneğe (V1) sabitlenirdi (test-reviewer)."""
    tau, v1 = 1e-3, 5 * (1 - np.exp(-1))
    v2 = v1 * np.exp(-1)
    t = np.linspace(1e-3, 3e-3, 15)
    hand = np.where(t <= 2e-3, v1 * np.exp(-(t - 1e-3) / tau), 5 - (5 - v2) * np.exp(-(t - 2e-3) / tau))
    summary = RawParser.summarize_file(TSTART_RAW)["V(out)"]
    assert summary.mean_val == pytest.approx(TSTART_MEAS["v_avg"], rel=1e-9)
    assert summary.downsampled_points == pytest.approx(hand, abs=1e-4)  # LTspice çözümü elle hesaba ~8 µV


def _step_dc(tmp_path, fmt, log=None):
    """examples/14 ``stepraw`` kaydı (LTspice 26.1.1, .step r 1k/3k, .dc V1 0 2 1) x.raw olarak; ``log``: x.log'un
    içeriği (``"kayıt"``: aynı koşumun logu, None: yok)."""
    (tmp_path / "x.raw").write_bytes((DATA / f"ref26_step_dc_{fmt}.raw").read_bytes())
    if log == "kayıt":
        log = (DATA / f"ref26_step_dc_{fmt}.log.txt").read_bytes()
    if log is not None:
        (tmp_path / "x.log").write_bytes(log)
    return tmp_path / "x.raw"


@pytest.mark.parametrize("traces", [None, ALL_TRACES])
def test_stepped_binary_raw_reads_its_steps_from_the_log(tmp_path, traces):
    """Elle (bölücü): V(out) = V1·1k/(r + 1k); r = 1k'da 0, 0,5, 1 V; r = 3k'da 0, 0,25, 0,5 V."""
    raw = open_raw(_step_dc(tmp_path, "binary", "kayıt"), traces=traces)
    assert raw_steps(raw) == [0, 1]
    assert raw_wave(raw, "V(out)", 0) == pytest.approx([0.0, 0.5, 1.0])
    assert raw_wave(raw, "V(out)", 1) == pytest.approx([0.0, 0.25, 0.5])


@pytest.mark.parametrize("traces", [None, ALL_TRACES])
def test_stepped_binary_raw_without_its_log_names_the_missing_log(tmp_path, traces):
    """Eski hata (Gemini 2026-10-06, ölçüm examples/18 T1): spicelib 1.6.3 ikili .raw'ın adımlarını yanındaki .log'dan
    okur, .log yoksa iz okurken IndexError verir. Tek geçişte (özet, grafik) open_raw bunu 'steplenmiş ASCII' diye
    bildiriyordu; tembel açılışta (read_trace, Bode) açılış geçiyor, hata ilk izde açıklamasız çıkıyordu."""
    path = _step_dc(tmp_path, "binary")
    with pytest.raises(ValueError, match=r"'x\.log' yok") as info:  # '.step satırı yok'tan ayrılır (test-reviewer)
        open_raw(path, traces=traces)
    assert "ASCII" not in str(info.value)


@pytest.mark.parametrize("log", [(DATA / "ref26_flow_rc.log.txt").read_bytes(), "başka bir dosya\n".encode()],
                         ids=["steplenmemiş koşumun logu", "LTspice logu değil"])
@pytest.mark.parametrize("traces", [None, ALL_TRACES])
def test_stepped_binary_raw_with_a_log_without_step_lines_is_refused(tmp_path, traces, log):
    """Ölçüm (examples/18 T1): yanındaki .log'da .step satırı yoksa spicelib 1.6.3 ya IndexError verir (log
    okunamıyor) ya da iki adımı sessizce tek adımda birleştirir (steplenmemiş LTspice logu: V(out) = 0, 0,5, 1, 0,
    0,25, 0,5); özet adımları karıştırırdı. LTspice tek değerli .step'i reddeder (ref26_step_single), steplenmiş .raw en
    az iki adımdır."""
    path = _step_dc(tmp_path, "binary", log)
    with pytest.raises(ValueError, match=r"'x\.log'.*\.step satırı yok"):
        open_raw(path, traces=traces)


def test_stepped_raw_with_another_stepped_runs_log_says_the_step_counts_differ(tmp_path):
    """Ölçüm (examples/18 T1): 5 adımlı .raw (ref26_mc_divider) yanında 2 .step satırlı log (ref26_step_dc_binary):
    spicelib 1.6.3 iz okurken IndexError verir. Log .step satırı içeriyor; hata 'satır yok' değil uyuşmazlık demeli."""
    (tmp_path / "x.raw").write_bytes((DATA / "ref26_mc_divider.raw").read_bytes())
    (tmp_path / "x.log").write_bytes((DATA / "ref26_step_dc_binary.log.txt").read_bytes())
    with pytest.raises(ValueError, match=r"'x\.log'.*2 \.step satırı.*uyuşmuyor"):
        open_raw(tmp_path / "x.raw", traces=ALL_TRACES)


def test_stepped_ascii_raw_is_reported_as_ascii_even_with_its_log(tmp_path):
    """Ölçüm (examples/18 T1): spicelib 1.6.3 steplenmiş ASCII .raw'ı yanında logu olsa da okuyamıyor (IndexError).
    Hata .log'u değil biçimi göstermeli."""
    path = _step_dc(tmp_path, "ascii", "kayıt")
    # Genel yedek mesaj da ASCII ve binary der; başlığın ASCII diye tanındığını bu ifade gösterir (test-reviewer)
    with pytest.raises(ValueError, match=r"steplenmiş \(\.step\) bir ASCII \.raw") as info:
        open_raw(path, traces=ALL_TRACES)
    assert "x.log" not in str(info.value)


def test_ac_summary_stays_a_plain_mean_over_the_frequency_points():
    """.ac'de zaman ağırlığı yok: özet noktaların aritmetik ortalaması kalır (parser.summarize_file). Kayıt
    ref26_ac_rc.raw (examples/14 ``mean``, .ac dec 10 10 100k). Elle: |H(f)| = 1/√(1 + (f/fc)²),
    fc = 1/(2π·1k·159n), kaydın frekans noktalarında. Frekansa göre ağırlıklansaydı ortalamayı en üst dekad belirlerdi
    (test-reviewer)."""
    from ltspice_llm.parser import open_raw, raw_axis_values

    raw_path = DATA / "ref26_ac_rc.raw"
    f = np.abs(raw_axis_values(open_raw(raw_path)))
    assert len(f) == 41 and f[0] == pytest.approx(10) and f[-1] == pytest.approx(1e5)  # 4 dekad x 10 + 1
    fc = 1 / (2 * np.pi * 1e3 * 159e-9)
    summary = RawParser.summarize_file(raw_path)["V(out)"]
    assert summary.is_complex
    assert summary.mean_val == pytest.approx(np.mean(1 / np.sqrt(1 + (f / fc) ** 2)), rel=1e-6)


def test_plot_of_several_signals_reads_the_raw_file_once(raw_reads, tmp_path):
    """Tembel okumada her sinyal veri bloğunu yeniden okur (yukarıdaki ölçüm); çizim de tek geçişte okur."""
    png = WaveformVisualizer.plot_traces(FLOW_RAW, ["V(in)", "V(out)"], output_png_path=tmp_path / "p.png")
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert FLOW_RAW.stat().st_size <= raw_reads["bytes"] < 1.5 * FLOW_RAW.stat().st_size  # tembel 34 784, tek 17 728


def test_long_trace_is_plotted_with_every_bucket_extreme(monkeypatch):
    """Çizimde kova başına min/max seyreltme (report._decimate). Ölçüm (examples/16): 1,12 M nokta 2,47 sn / 659 MB
    -> 0,78 sn / 212 MB, piksellerin %0,0066'sı farklı. LTTB (Gemini önerisi) kovaların %40'ında tepeyi, %63'ünde
    dibi ve global minimumu kaybetti."""
    monkeypatch.setattr(visualizer_mod, "PLOT_BUCKETS", 20, raising=False)
    fig = WaveformVisualizer._build_trace_figure(FLOW_RAW, ["V(out)"], "t")
    (line,) = fig.axes[0].get_lines()
    x, y = line.get_xdata(), line.get_ydata()
    x_all, y_all = read_trace(FLOW_RAW, "V(out)")
    assert len(x_all) == 1066 and len(y) <= 2 * 20 + 2
    assert max(y) == pytest.approx(V_OUT_MAX, rel=1e-6)
    assert min(y) == pytest.approx(min(y_all)) and (x[0], x[-1]) == pytest.approx((x_all[0], x_all[-1]))
    # Genel tepe son, genel dip ilk örnek: yalnız uçları tutan seyreltme yukarıdakileri geçer (test-reviewer). Her eşit
    # süreli kovanın (report._decimate'in tanımı) kendi tepesi ve dibi de çizgide olmalı (ör. 1 ms'deki 3,16 V tepe).
    edges = np.searchsorted(x_all, np.linspace(x_all[0], x_all[-1], 21))
    for a, b in zip(edges[:-1], edges[1:]):
        if b > a:
            inside = y[(x >= x_all[a]) & (x <= x_all[b - 1])]
            assert np.isclose(inside, y_all[a:b].max()).any() and np.isclose(inside, y_all[a:b].min()).any()
