"""
Koşum olayları (`ltspice_llm.events`) ve canlı monitör (`ltspice_llm.monitor`).

Beklenen olay dizileri ``ltspice_llm/events.py`` belge dizgisindeki "Bir koşumun olay sırası" paragrafından yazıldı,
koddan değil. Sahte olan yalnızca gerçek sınırdır (``subprocess.Popen``, süreç sonlandırma): başarılı koşumun sahte
süreci gerçek LTspice 26.1.1 kaydını (``tests/data/ref26_flow_rc.*``, examples/14) netlistin yanına yazar.
"""

import contextlib
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

import ltspice_llm.adapter as adapter_mod
import ltspice_llm.cpu_guard as guard
from ltspice_llm import LTSpiceAdapter, events, monitor
from ltspice_llm.adapter import SimulationResult
from ltspice_llm.cpu_guard import CpuSlotPool
from ltspice_llm.parser import LogParser
from ltspice_llm.schematic import write_asc_file

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(__file__).parent / "data"
RC = (DATA / "ref26_flow_rc.net").read_text(encoding="utf-8")
RC_OUTPUTS = {".log": DATA / "ref26_flow_rc.log.txt", ".raw": DATA / "ref26_flow_rc.raw"}
#: tests/data/ref26_flow_rc.log.txt (LTspice 26.1.1): "Maximum thread count: 1", "method = trap",
#: "Total elapsed time: 0.017 seconds."
RC_LOG_FACTS = {"threads": 1, "method": "trap", "solver_seconds": 0.017}
#: LTspice 26.1.1 kendini çağıran alt devrede çöker (examples/14 ``crash``): çıkış kodu, boş stderr, 3 satırlık log
CRASH = json.loads((DATA / "ref26_crash_recursive.json").read_text(encoding="utf-8"))


class _Ltspice:
    """Sahte LTspice: her başlatmada sıradaki davranışı oynar. 'replay' gerçek kaydı yazıp rc=0 ile biter, 'crash'
    gerçek çökme kaydını yazıp çökme koduyla biter, 'stuck' öldürülene kadar ilerlemez, 'fail_start' başlatılamaz
    (Popen OSError)."""
    script = []
    launches = 0
    _lock = threading.Lock()

    def __init__(self, cmd, **kwargs):
        cls = type(self)
        with cls._lock:
            index = cls.launches
            cls.launches += 1
        self.behaviour = cls.script[index]
        if self.behaviour == "fail_start":
            raise OSError("başlatılamadı")
        self.pid, self.returncode, self.killed = 5000 + index, None, False
        if self.behaviour == "replay":
            for suffix, source in RC_OUTPUTS.items():
                Path(cmd[-1]).with_suffix(suffix).write_bytes(source.read_bytes())
        elif self.behaviour == "crash":
            Path(cmd[-1]).with_suffix(".log").write_bytes((DATA / "ref26_crash_recursive.log.txt").read_bytes())

    def communicate(self, timeout=None):
        if self.behaviour == "replay":
            self.returncode = 0
            return "", ""
        if self.behaviour == "crash":
            self.returncode = CRASH["returncode"]
            return "", CRASH["stderr"]
        if self.killed:
            return "", ""
        time.sleep(min(timeout or 0, 0.01))
        raise subprocess.TimeoutExpired("ltspice", timeout)


@pytest.fixture
def ltspice(monkeypatch):
    monkeypatch.setattr(adapter_mod.subprocess, "Popen", _Ltspice)
    monkeypatch.setattr(LTSpiceAdapter, "_WATCH_INTERVAL", 0.01)

    def fake_kill(process):
        process.killed, process.returncode = True, 1

    monkeypatch.setattr(LTSpiceAdapter, "_kill_process_tree", staticmethod(fake_kill))
    _Ltspice.script, _Ltspice.launches = ["replay"], 0
    return _Ltspice


@pytest.fixture
def event_files(tmp_path, monkeypatch):
    directory = tmp_path / "events"
    monkeypatch.setenv(events.ENV_EVENTS, "1")
    monkeypatch.setenv(events.ENV_EVENT_DIR, str(directory))
    return directory


@pytest.fixture
def four_cpus(monkeypatch):
    monkeypatch.setattr(adapter_mod, "available_cpus", lambda: 4)
    monkeypatch.setattr(guard.os, "cpu_count", lambda: 4)


def _adapter(tmp_path, **kw):
    return LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path / "ws", **kw)


def _pool(tmp_path, size):
    return CpuSlotPool(size=size, directory=tmp_path / "slots")


def _read(directory):
    """Bu sürecin tek olay dosyasındaki olaylar; her satır tek başına geçerli JSON olmalı."""
    (path,) = directory.glob("*.jsonl")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _kinds(evts):
    return [e["kind"] for e in evts]


# --------------------------------------------------------------------------- olay yazımı
@pytest.mark.parametrize("value,on", [
    ("1", True), ("true", True), ("YES", True), (" on ", True),
    ("", False), ("0", False), ("false", False), ("2", False),
])
def test_switch_accepts_only_the_documented_values(monkeypatch, value, on):
    """events belge dizgisi: 1, true, yes ya da on açar; büyük/küçük harf ve baştaki/sondaki boşluk önemsiz."""
    monkeypatch.setenv(events.ENV_EVENTS, value)
    assert events.enabled() is on


def test_default_event_dir_is_in_the_system_temp_dir(monkeypatch):
    """events belge dizgisi ve docs/GUIDE.md: varsayılan ``%TEMP%/ltspice_llm_events``."""
    monkeypatch.delenv(events.ENV_EVENT_DIR)
    assert events.event_dir() == Path(tempfile.gettempdir()) / "ltspice_llm_events"


def test_nothing_is_written_while_events_are_off(tmp_path, ltspice, monkeypatch):
    directory = tmp_path / "events"
    monkeypatch.setenv(events.ENV_EVENT_DIR, str(directory))  # conftest anahtarı siler: varsayılan kapalı
    res = _adapter(tmp_path, cpu_guard=False).run_netlist(RC, circuit_name="rc", treat_as_text=True)
    assert res.success
    assert not directory.exists()


def test_guarded_run_writes_the_documented_sequence(tmp_path, ltspice, event_files, four_cpus, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["C:/proje/devre.py", "--api-key=gizli-anahtar", "a1"])
    adapter = _adapter(tmp_path, cpu_guard=_pool(tmp_path, 4))
    res = adapter.run_netlist(RC, circuit_name="rc", treat_as_text=True)
    assert res.success
    evts = _read(event_files)
    assert _kinds(evts) == ["process", "run_start", "cpu_request", "cpu_lease", "ltspice_start", "ltspice_exit",
                            "cpu_release", "run_end"]
    header, start, request, lease, launch, exit_, release, end = evts
    # yalnız ilk öğe, betiğin yolu: argümanlar gizli bilgi taşıyabilir, yazılmaz (events belge dizgisi). Yol kısa:
    # birleştirip kesen bir yazar da anahtarı 299. karakterin ötesine itip gizleyemesin (test-reviewer 2026-11-20)
    assert "run" not in header and header["argv"] == ["C:/proje/devre.py"]
    (written,) = event_files.glob("*.jsonl")
    assert written.read_text(encoding="utf-8").count("gizli-anahtar") == 0
    assert len({e["run"] for e in evts[1:]}) == 1
    assert {e["pid"] for e in evts} == {os.getpid()}
    assert (start["circuit"], start["workspace"], start["guard"]) == ("rc", str(adapter.workspace_dir), True)
    assert start["source"] is None  # metin olarak verilen netlist
    assert end["elapsed"] == round(res.elapsed_seconds, 3)
    assert (start["timeout"], start["stall"]) == (adapter.default_timeout, adapter.stall_timeout)
    assert request["want"] == 4
    assert (lease["threads"], lease["budget"], release["threads"]) == (4, 4, 4)
    assert (launch["ltspice_pid"], launch["netlist"]) == (5000, str(res.netlist_path))
    assert (exit_["outcome"], exit_["returncode"], exit_["crash"]) == ("exited", 0, None)
    assert end["success"] is True and end["n_errors"] == 0 and end["errors"] == []
    assert end["threads"] == res.solver_threads == 4
    assert end["n_meas"] == 2  # v_max ve vr_max (tests/data/ref26_flow_rc.log.txt)
    assert (end["log"], end["raw"]) == (str(res.log_path), str(res.raw_path))
    times = [e["t"] for e in evts]
    assert times == sorted(times)


def test_long_script_path_is_cut_like_other_texts(event_files, monkeypatch):
    """events belge dizgisi: metin alanları (``argv`` öğesi de) 300 karakterde kesilir: ilk 299 karakter ve ``…``."""
    script = "C:/proje/" + "x" * 400 + ".py"
    monkeypatch.setattr(sys, "argv", [script])
    events.emit("note")
    assert _read(event_files)[0]["argv"] == [script[:299] + "…"]


def test_stalled_solver_retry_repeats_request_lease_and_launch(tmp_path, ltspice, event_files, four_cpus):
    ltspice.script = ["stuck", "replay"]
    res = _adapter(tmp_path, cpu_guard=_pool(tmp_path, 4), stall_timeout=0.1).run_netlist(
        RC, circuit_name="rc", treat_as_text=True)
    evts = _read(event_files)
    assert _kinds(evts) == ["process", "run_start",
                            "cpu_request", "cpu_lease", "ltspice_start", "ltspice_exit", "cpu_release",
                            "cpu_request", "cpu_lease", "ltspice_start", "ltspice_exit", "cpu_release",
                            "run_end"]
    assert [e["outcome"] for e in evts if e["kind"] == "ltspice_exit"] == ["stalled", "exited"]
    assert [e["threads"] for e in evts if e["kind"] == "cpu_lease"] == [4, 1]
    assert [e["ltspice_pid"] for e in evts if e["kind"] == "ltspice_start"] == [5000, 5001]
    end = evts[-1]
    assert end["success"] is res.success is True
    assert end["threads"] == 1
    assert end["n_warnings"] == len(res.warnings) >= 1
    assert end["warnings"] == res.warnings[:3]  # uyarılar 300 karakterden kısa: kesilmeden


def test_timeout_without_guard_has_no_cpu_events(tmp_path, ltspice, event_files):
    ltspice.script = ["stuck"]
    res = _adapter(tmp_path, cpu_guard=False, stall_timeout=None, default_timeout=0.3).run_netlist(
        RC, circuit_name="t", treat_as_text=True)
    evts = _read(event_files)
    assert _kinds(evts) == ["process", "run_start", "ltspice_start", "ltspice_exit", "run_end"]
    assert (evts[1]["guard"], evts[1]["stall"], evts[1]["timeout"]) == (False, None, 0.3)
    assert evts[3]["outcome"] == "timeout"
    assert 0.3 <= evts[3]["seconds"] <= evts[4]["elapsed"]  # LTspice'ın süresi koşumunkini aşamaz
    assert evts[4]["success"] is res.success is False
    assert sum("zaman aşımına" in e for e in evts[4]["errors"]) == 1


def test_lease_smaller_than_the_request_is_written_as_leased(tmp_path, ltspice, event_files, four_cpus):
    """Bütçenin 6 yuvasından 3'ü başka bir süreçte: 4 istenir, boş 3'ü alınır (``CpuSlotPool.acquire`` ilk denemede
    boşta olanla yetinir; kullanıcı threads vermediği için en az 1)."""
    pool = _pool(tmp_path, 6)
    other = CpuSlotPool(size=6, directory=pool.directory).acquire(3)
    try:
        res = _adapter(tmp_path, cpu_guard=pool).run_netlist(RC, circuit_name="p", treat_as_text=True)
    finally:
        other.release()
    by_kind = {e["kind"]: e for e in _read(event_files)}
    assert by_kind["cpu_request"]["want"] == 4
    assert (by_kind["cpu_lease"]["threads"], by_kind["cpu_lease"]["budget"], by_kind["cpu_release"]["threads"]) == (
        3, 6, 3)
    assert by_kind["run_end"]["threads"] == res.solver_threads == 3


def test_crash_is_written_with_its_exit_code(tmp_path, ltspice, event_files):
    ltspice.script = ["crash"]
    res = _adapter(tmp_path, cpu_guard=False).run_netlist(RC, circuit_name="c", treat_as_text=True)
    (exit_,) = [e for e in _read(event_files) if e["kind"] == "ltspice_exit"]
    assert (exit_["outcome"], exit_["returncode"]) == ("exited", CRASH["returncode"])
    assert exit_["crash"].count("0xC00000FD") == 1
    assert res.success is False


def test_run_end_carries_the_first_three_messages_and_their_count(tmp_path, ltspice, event_files):
    """Netlistte olmayan her parametre adı bir uyarıdır (``apply_parameters``): dört ad, dört uyarı."""
    res = _adapter(tmp_path, cpu_guard=False).run_netlist(
        RC, circuit_name="w", treat_as_text=True, parameters={f"yok{i}": 1 for i in range(4)})
    end = _read(event_files)[-1]
    assert len(res.warnings) >= 4
    assert (end["n_warnings"], end["warnings"]) == (len(res.warnings), res.warnings[:3])


def test_run_end_keeps_the_first_three_errors_and_cuts_long_messages():
    """events belge dizgisi: ``errors``/``warnings`` ilk 3; metin alanları 300 karakterde (ilk 299 + "…")."""
    result = SimulationResult(success=False, netlist_path=Path("x.net"), errors=[f"hata {i}" for i in range(5)],
                              warnings=["u" * 400, "kısa"])
    fields = events.result_fields(result)
    assert (fields["n_errors"], fields["errors"]) == (5, ["hata 0", "hata 1", "hata 2"])
    assert (fields["n_warnings"], fields["warnings"]) == (2, ["u" * 299 + "…", "kısa"])


def test_measurement_count_is_of_distinct_names_in_a_stepped_run():
    """events belge dizgisi: ``n_meas`` ayrı ölçüm adı sayısı. Gerçek steplenmiş LTspice 26.1.1 kaydı
    (tests/data/ref26_cmp_step_c.*, examples/14 ``compare``): netlist iki ölçüm tanımlar (``.meas tran v_max``,
    ``.meas tran vr_max``) ve ikisi hem adım tablosunda hem tek değerlerde geçer; sayı 2'dir."""
    parsed = LogParser.parse_file(DATA / "ref26_cmp_step_c.log.txt")
    assert set(parsed.measurements) == set(parsed.stepped_measurements) == {"v_max", "vr_max"}  # önkoşul: kayıt
    result = SimulationResult(success=True, netlist_path=Path("x.net"), measurements=parsed.measurements,
                              stepped_measurements=parsed.stepped_measurements)
    assert events.result_fields(result)["n_meas"] == 2


def test_exception_text_is_cut_like_other_messages(event_files):
    with pytest.raises(ValueError):
        with events.RunScope(circuit="c"):
            raise ValueError("x" * 400)
    end = _read(event_files)[-1]
    assert end["exception"] == ("ValueError: " + "x" * 400)[:299] + "…"


def test_time_spent_waiting_for_cores_is_written(tmp_path, ltspice, event_files, four_cpus):
    """Bütçenin bütün yuvaları başka bir süreçte; 0,3 sn sonra bırakılır: kira en az o kadar beklemiş olmalı."""
    pool = _pool(tmp_path, 4)
    other = CpuSlotPool(size=4, directory=pool.directory).acquire(4)
    releaser = threading.Timer(0.3, other.release)
    releaser.start()
    try:
        _adapter(tmp_path, cpu_guard=pool).run_netlist(RC, circuit_name="w", treat_as_text=True)
    finally:
        releaser.join()
    evts = _read(event_files)
    (lease,) = [e for e in evts if e["kind"] == "cpu_lease"]
    # saniye cinsinden: koşumun süresi (kiradan önce başlar) bekleyişi kapsar
    assert 0.25 <= lease["waited"] <= evts[-1]["elapsed"]


def test_positional_name_and_explicit_timeout_are_written(tmp_path, ltspice, event_files):
    _adapter(tmp_path, cpu_guard=False).run_netlist(RC, "pos", None, 7, treat_as_text=True)
    start = _read(event_files)[1]
    assert (start["circuit"], start["timeout"]) == ("pos", 7)


def test_schematic_file_run_writes_its_source_path(tmp_path, ltspice, event_files, monkeypatch):
    """Dosyadan koşulan şemanın tam yolu ``source``'ta (göreli verilse de): monitörün şema sayfası onu listeler."""
    (tmp_path / "proje").mkdir()
    asc = write_asc_file(tmp_path / "proje" / "devre.asc", "Version 4\nSHEET 1 880 680\nTEXT 0 0 Left 2 !.op\n")
    monkeypatch.chdir(tmp_path)
    _adapter(tmp_path, cpu_guard=False).run_netlist(Path("proje") / "devre.asc", circuit_name="sema")
    start = _read(event_files)[1]
    assert start["source"] == str(asc.resolve())


def test_full_budget_ends_the_run_without_a_launch(tmp_path, ltspice, event_files, monkeypatch):
    monkeypatch.setattr(adapter_mod, "available_cpus", lambda: 2)
    monkeypatch.setattr(guard.os, "cpu_count", lambda: 2)
    pool = _pool(tmp_path, 2)
    other = CpuSlotPool(size=2, directory=pool.directory).acquire(2)  # başka bir süreç gibi: aynı dizinin kilitleri
    try:
        res = _adapter(tmp_path, cpu_guard=pool, slot_wait_timeout=0.2).run_netlist(
            RC, circuit_name="b", treat_as_text=True)
    finally:
        other.release()
    evts = _read(event_files)
    assert _kinds(evts) == ["process", "run_start", "cpu_request", "run_end"]
    assert ltspice.launches == 0
    assert evts[-1]["success"] is res.success is False
    assert "bütçesi dolu" in evts[-1]["errors"][0]


def test_launch_failure_releases_the_lease_without_launch_events(tmp_path, ltspice, event_files, four_cpus):
    ltspice.script = ["fail_start"]
    res = _adapter(tmp_path, cpu_guard=_pool(tmp_path, 4)).run_netlist(RC, circuit_name="f", treat_as_text=True)
    evts = _read(event_files)
    assert _kinds(evts) == ["process", "run_start", "cpu_request", "cpu_lease", "cpu_release", "run_end"]
    assert evts[-1]["success"] is res.success is False


def test_exception_in_run_netlist_still_ends_the_run(tmp_path, ltspice, event_files):
    with pytest.raises(ValueError, match="Geçersiz parametre"):
        _adapter(tmp_path, cpu_guard=False).run_netlist(
            RC, circuit_name="x", treat_as_text=True, parameters={"R1": "1k\n.tran 1"})
    evts = _read(event_files)
    assert _kinds(evts) == ["process", "run_start", "run_end"]
    assert evts[2]["run"] == evts[1]["run"]
    assert evts[2]["success"] is False and evts[2]["exception"].startswith("ValueError: Geçersiz parametre")
    assert ltspice.launches == 0


def test_unwritable_event_dir_leaves_the_result_unchanged(tmp_path, ltspice, monkeypatch):
    baseline = _adapter(tmp_path / "a", cpu_guard=False).run_netlist(RC, circuit_name="rc", treat_as_text=True)
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv(events.ENV_EVENTS, "1")
    monkeypatch.setenv(events.ENV_EVENT_DIR, str(blocker / "events"))  # bir dosyanın altı: dizin kurulamaz
    ltspice.script, ltspice.launches = ["replay"], 0
    res = _adapter(tmp_path / "b", cpu_guard=False).run_netlist(RC, circuit_name="rc", treat_as_text=True)
    assert res.success
    assert (res.errors, res.warnings, res.measurements) == (baseline.errors, baseline.warnings, baseline.measurements)


def test_batch_runs_keep_their_own_ids_and_whole_lines(tmp_path, ltspice, event_files, four_cpus):
    ltspice.script = ["replay"] * 4
    jobs = [{"netlist_content_or_path": RC, "circuit_name": f"rc{i}", "treat_as_text": True} for i in range(4)]
    results = _adapter(tmp_path, cpu_guard=False).run_batch(jobs, max_workers=4)
    assert [r.success for r in results] == [True] * 4
    evts = _read(event_files)  # dört iş parçacığı aynı dosyaya yazdı; her satır tek başına çözülür
    by_run = {}
    for e in evts[1:]:
        by_run.setdefault(e["run"], []).append(e["kind"])
    assert list(by_run.values()) and len(by_run) == 4
    assert all(kinds == ["run_start", "ltspice_start", "ltspice_exit", "run_end"] for kinds in by_run.values())
    assert sorted(e["circuit"] for e in evts if e["kind"] == "run_start") == ["rc0", "rc1", "rc2", "rc3"]


def test_new_event_file_prunes_files_past_retention(event_files):
    """Yalnız saklama süresini geçen olay dosyaları silinir: ``LTSPICE_LLM_EVENT_DIR`` kullanıcının seçtiği bir dizin
    olabilir (``.jsonl`` olmayan dosya; ``.jsonl`` adlıları `test_retention_spares_jsonl_files_that_are_not_event_files`)."""
    event_files.mkdir(parents=True)
    old, fresh, other = event_files / "1-1.jsonl", event_files / "2-2.jsonl", event_files / "notlar.txt"
    for path in (old, fresh, other):
        path.write_text("{}\n", encoding="utf-8")
    retention = events.RETENTION_DAYS * 86400
    os.utime(old, (time.time() - retention - 60,) * 2)
    os.utime(other, (time.time() - retention - 60,) * 2)
    os.utime(fresh, (time.time() - retention + 3600,) * 2)
    events.emit("note")
    assert not old.exists() and fresh.exists() and other.exists()


def test_retention_spares_jsonl_files_that_are_not_event_files(event_files):
    """Olay dosyasının adı ``<pid>-<başlangıç ms>.jsonl`` (events belge dizgisi). ``LTSPICE_LLM_EVENT_DIR`` kullanıcının
    kendi verisinin durduğu bir dizin olabilir: saklama süresi yalnız bu adlara uygulanır; kullanıcının ``.jsonl``
    dosyası (tarihle adlandırılmış günlük, ad-sayı yedeği) ne kadar eski olursa olsun kalır."""
    event_files.mkdir(parents=True)
    expired = time.time() - events.RETENTION_DAYS * 86400 - 60
    old_event = event_files / "4242-1794810500000.jsonl"
    own = [event_files / name for name in ("veri.jsonl", "2026-10-01.jsonl", "12-34-yedek.jsonl")]
    for path in (old_event, *own):
        path.write_text("{}\n", encoding="utf-8")
        os.utime(path, (expired, expired))
    events.emit("note")
    assert not old_event.exists()
    assert [path.name for path in own if path.exists()] == ["veri.jsonl", "2026-10-01.jsonl", "12-34-yedek.jsonl"]


# --------------------------------------------------------------------------- okuma ve log
def test_reader_keeps_a_partial_line_for_the_next_read(tmp_path):
    path = tmp_path / "7-1.jsonl"
    path.write_bytes(b'{"kind": "a", "t": 1}\n{"kind": "b", "t"')
    reader = events.EventReader(tmp_path)
    assert [e["kind"] for e in reader.read_new()] == ["a"]
    assert reader.read_new() == []
    with open(path, "ab") as handle:
        handle.write(b': 2}\n')
    assert [(e["kind"], e["t"], e["src"]) for e in reader.read_new()] == [("b", 2, "7-1")]


def test_reader_reads_a_long_file_in_pieces(tmp_path):
    """Bir okumada dosya başına en çok MAX_CHUNK bayt: kalan, satır bölünmeden sonraki okumalarda gelir."""
    (tmp_path / "1-1.jsonl").write_bytes(b"".join(b'{"kind": "k%d"}\n' % i for i in range(10)))
    reader = events.EventReader(tmp_path)
    reader.MAX_CHUNK = 40  # her satır 14-15 bayt: bir okumada en çok iki tam satır
    kinds = []
    for _ in range(10):
        kinds += [e["kind"] for e in reader.read_new()]
    assert kinds == [f"k{i}" for i in range(10)]


def test_reader_skips_lines_that_are_not_events(tmp_path):
    (tmp_path / "1-1.jsonl").write_bytes(b'not json\n[1, 2]\n{"t": 1}\n{"kind": 3}\n{"kind": "ok"}\n')
    assert [e["kind"] for e in events.EventReader(tmp_path).read_new()] == ["ok"]


def test_reader_skips_a_stale_file_until_it_changes(tmp_path):
    path = tmp_path / "1-1.jsonl"
    path.write_bytes(b'{"kind": "old"}\n')
    os.utime(path, (time.time() - 7200,) * 2)
    reader = events.EventReader(tmp_path, max_age=3600)
    assert reader.read_new() == []
    with open(path, "ab") as handle:
        handle.write(b'{"kind": "new"}\n')
    assert [e["kind"] for e in reader.read_new()] == ["old", "new"]


@pytest.mark.parametrize("read_while_missing", [False, True], ids=["iki-okuma-arasinda", "okunurken-yoktu"])
def test_reader_rereads_a_file_that_was_recreated_shorter(tmp_path, read_while_missing):
    """Saklama süresini geçen dosyayı başka bir süreç siler; dosyanın süreci yeni olayda onu baştan yazar."""
    path = tmp_path / "1-1.jsonl"
    path.write_bytes(b'{"kind": "first", "pad": "xxxxxxxxxxxxxxxx"}\n')
    reader = events.EventReader(tmp_path)
    assert [e["kind"] for e in reader.read_new()] == ["first"]
    path.unlink()
    if read_while_missing:
        assert reader.read_new() == []
    path.write_bytes(b'{"kind": "second"}\n')
    assert [e["kind"] for e in reader.read_new()] == ["second"]


# --------------------------------------------------------------------------- bozuk olay satırları (dış girdi)
@pytest.mark.parametrize("line", [b'{"kind": "x", "timeout": NaN}', b'{"kind": "x", "timeout": Infinity}',
                                  b'{"kind": "x", "timeout": -Infinity}', b'{"kind": "x", "timeout": 1e400}'],
                         ids=["NaN", "Infinity", "eksi-Infinity", "1e400"])
def test_reader_skips_a_line_with_a_number_json_cannot_carry(tmp_path, line):
    """RFC 8259 §6: "Numeric values that cannot be represented in the grammar below (such as Infinity and NaN) are not
    permitted." Python'un json'u bunları yine okur (``1e400`` sonsuz olur) ve geri yazar; sayfa yanıtı tarayıcının
    JSON.parse'ıyla çözer ve reddeder: tek satır sayfayı her istekte bozardı. Satır atlanır, sonraki okunur."""
    (tmp_path / "1-1.jsonl").write_bytes(line + b'\n{"kind": "ok"}\n')
    assert [e["kind"] for e in events.EventReader(tmp_path).read_new()] == ["ok"]


def _no_constant(name):
    raise ValueError(f"JSON sayısı değil: {name}")


@contextlib.contextmanager
def _serving(mon):
    """Verilen monitörü 127.0.0.1'de boş bir portta sunar: sayfanın gördüğü yanıt sınanır, durum sözlüğü değil."""
    server = monitor.make_server(mon, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(10)


def _strict_json(server, path):
    """Sayfanın ``response.json()`` ile çözdüğü yanıt: 200, UTF-8 ve katı JSON (``NaN``/``Infinity`` yok, RFC 8259 §6).
    Sunucu yanıtı kuramazsa (ör. UTF-8'e kodlanamayan metin) bağlantı kapanır ve ``_get`` hata verir."""
    status, _, body = _get(server, path)
    assert status == 200, body[:300]
    return json.loads(body.decode("utf-8"), parse_constant=_no_constant)


@pytest.mark.parametrize("bad", [
    [{"kind": "run_start", "run": "b-1", "t": "x"}],
    [{"kind": "run_start", "run": "b-1", "t": "nan"}],
    [{"kind": "run_start", "run": "b-1", "t": [1]}],
    [{"kind": "run_start", "run": "b-1", "t": 10 ** 400}],
    [{"kind": "process", "t": 1.0, "argv": 5}],
    [{"kind": "run_start", "run": "b-1", "t": -1e308}, {"kind": "run_end", "run": "b-1", "t": 1e308, "success": True}],
], ids=["t-metin", "t-nan", "t-liste", "t-tasan-tamsayi", "argv-sayi", "uc-zamanlar"])
def test_a_malformed_event_drops_only_itself(tmp_path, bad):
    """Olay dosyası dış girdidir: başka bir yazarın türü bozuk satırı yalnız kendisi düşer. Aynı okumadaki sonraki olaylar
    okunur ve sayfanın aldığı yanıt katı JSON kalır (RFC 8259 §6). Önceden ilk istek istisnayla düşüyor, aynı okumadaki
    sonraki olaylar kayboluyordu (ölçüm 2026-11-19). Uç zamanların her biri sonludur ama farkları (süre) taşar:
    ``1e308 - (-1e308)`` sonsuzdur ve sayfaya ``Infinity`` giderdi (test-reviewer 2026-11-20)."""
    now = time.time()
    lines = [*bad, {"kind": "process", "t": now, "pid": os.getpid(), "argv": ["saglam.py"]},
             {"kind": "run_start", "t": now, "pid": os.getpid(), "run": "s-1", "circuit": "saglam"}]
    (tmp_path / "1-1.jsonl").write_text("".join(json.dumps(e) + "\n" for e in lines), encoding="utf-8")
    with _serving(monitor.Monitor(tmp_path)) as server:
        state = _strict_json(server, "/api/state")
    assert [(r["circuit"], r["process"]) for r in state["active"]] == [("saglam", "saglam.py")]
    assert state["finished"] == []


def test_a_non_list_argv_counts_as_an_unknown_process(tmp_path):
    """`MonitorState`: liste olmayan ``argv`` boş sayılır, süreç adı "python" olur ve koşum görünür. Bozuk satır kendi
    dosyasında: aynı dosyadaki sağlam bir ``process`` satırı etiketi ezip "olay atlandı"yla farkı gizlemesin ("?")."""
    now = time.time()
    lines = [{"kind": "process", "t": now, "pid": os.getpid(), "argv": 5},
             {"kind": "run_start", "t": now, "pid": os.getpid(), "run": "a-1", "circuit": "a"}]
    (tmp_path / "1-1.jsonl").write_text("".join(json.dumps(e) + "\n" for e in lines), encoding="utf-8")
    with _serving(monitor.Monitor(tmp_path)) as server:
        state = _strict_json(server, "/api/state")
    assert [(r["circuit"], r["process"]) for r in state["active"]] == [("a", "python")]


def test_unpaired_surrogates_reach_the_page_as_valid_json(tmp_path):
    """Eşleşmemiş vekil karakter UTF-8'e kodlanamaz; sunucu yanıtı ``ensure_ascii=False`` ile UTF-8'e çevirirken her istek
    düşüyordu (test-reviewer 2026-11-20). İki kaynak: başka bir yazarın olay satırı (JSON kaçışı) ve izlenen klasördeki
    UTF-8 olmayan dosya adı (Linux'ta ``os.listdir`` baytı ``surrogateescape`` ile ``\\udcff`` yapar; Python
    ``os.fsdecode`` belgesi). Yanıt ASCII kaçışlı JSON'dur; tarayıcı kaçışı çözer ve metni ``textContent`` ile yazar.
    Böyle bir yol ise sayfaya gönderilmez: sayfa onu ``encodeURIComponent`` ile bağlantıya ya da isteğe koyar, o da
    eşleşmemiş vekilde URIError atar (ECMA-262 "Encode") ve koşumlar sayfası güncellenmeyi bırakırdı; şema hiç açılamaz."""
    now = time.time()
    root, directory = tmp_path / "kok", tmp_path / "olaylar"
    root.mkdir()
    directory.mkdir()
    write_asc_file(root / "s.asc", "Version 4\n")
    odd = write_asc_file(root / "\udcff.asc", "Version 4\n")  # Linux'ta b"\xff.asc", Windows'ta eşleşmemiş vekilli ad
    lines = [{"kind": "process", "t": now, "pid": os.getpid(), "argv": ["x.py"]},
             {"kind": "run_start", "t": now, "pid": os.getpid(), "run": "v-1", "circuit": "a\ud800b",
              "source": str(odd)}]
    (directory / "1-1.jsonl").write_text("".join(json.dumps(e) + "\n" for e in lines), encoding="utf-8")
    with _serving(monitor.Monitor(directory, asc_roots=[root])) as server:
        state, listing = _strict_json(server, "/api/state"), _strict_json(server, "/api/schematics")
    assert [(r["circuit"], r["source"]) for r in state["active"]] == [("a\ud800b", None)]
    assert [f["name"] for f in listing["files"]] == ["s.asc"]


def test_impossible_lease_sizes_do_not_break_the_budget(tmp_path):
    """Kira bir çekirdek sayısıdır (LTspice iş parçacığını mantıksal çekirdek sayısına kırpar, `ltspice_thread_limit`).
    json 4300 basamağa kadar tamsayı okur; 4300 dokuzlu iki kiranın toplamı 4301 basamak olur ve Python 3.11+ onu yazamaz
    ("Exceeds the limit (4300 digits)"): yanıt her istekte düşerdi (test-reviewer 2026-11-20). Makul aralık dışındaki
    kira bütçeye sayılmaz."""
    now, huge = time.time(), int("9" * 4300)
    lines = [{"kind": "process", "t": now, "pid": os.getpid(), "argv": ["x.py"]}]
    for i in (1, 2):
        lines += [{"kind": "run_start", "t": now, "pid": os.getpid(), "run": f"k-{i}", "circuit": f"k{i}"},
                  {"kind": "cpu_lease", "t": now, "pid": os.getpid(), "run": f"k-{i}", "threads": huge, "budget": 8}]
    (tmp_path / "1-1.jsonl").write_text("".join(json.dumps(e) + "\n" for e in lines), encoding="utf-8")
    with _serving(monitor.Monitor(tmp_path)) as server:
        state = _strict_json(server, "/api/state")
    assert state["budget"] == {"in_use": 0, "size": 8}
    assert [r["circuit"] for r in state["active"]] == ["k1", "k2"]


def test_own_writer_keeps_a_run_with_an_infinite_setting_and_an_undecodable_name(tmp_path, event_files):
    """``LTSpiceAdapter(stall_timeout=float("inf"))`` geçerli bir ayardır (bekçi hiç tetiklenmez). Yazar onu JSON'a
    ``Infinity`` diye yazınca okuyucu satırı atıyor ve koşum monitörde hiç görünmüyordu (test-reviewer 2026-11-20);
    UTF-8'e kodlanamayan ad (Linux'ta ``surrogateescape``'li betik ya da devre adı) satırı hiç yazdırmazdı. Yazar
    sonlu olmayan sayıyı null, metni ASCII kaçışlı yazar."""
    with events.RunScope(circuit="a\udcffb", stall=float("inf"), timeout=30):
        with _serving(monitor.Monitor(event_files)) as server:
            state = _strict_json(server, "/api/state")
    assert [(r["circuit"], r["stall"], r["timeout"]) for r in state["active"]] == [("a\udcffb", None, 30)]


@pytest.mark.parametrize("field,value", [
    ("source", "C:/x/a\x00b.asc"), ("source", "C:/" + "a" * 40000 + ".asc"), ("source", 5),
    ("netlist", "C:/x/a\x00b.net"), ("netlist", "C:/" + "a" * 40000 + ".net"), ("netlist", "/"),
    ("log", "C:/x/a\x00b.log"), ("log", "C:/" + "a" * 40000 + ".log"),
    ("netlist", "C:/x/" + "\U0001F600" * 17000 + ".net"), ("netlist", "C:/x/\ud800.net"), ("log", "C:/x/\ud800.log"),
], ids=["source-nul", "source-uzun", "source-sayi", "netlist-nul", "netlist-uzun", "netlist-adsiz", "log-nul",
        "log-uzun", "netlist-utf16-uzun", "netlist-vekil", "log-vekil"])
def test_unusable_paths_in_events_do_not_break_the_pages(tmp_path, field, value):
    """İşletim sistemi böyle yolları ``ValueError`` ile reddeder: gömülü NUL; Windows'ta 32767 UTF-16 birimini aşan yol
    (CPython "path too long for Windows"; 17009 karakterlik emoji yolu UTF-16'da 34009 birim); adı boş yol
    (``PurePath.with_suffix``); Linux'ta UTF-8'e kodlanamayan vekil (``os.stat`` UnicodeEncodeError). Değer olayda
    kaldıkça sayfa her istekte düşüyordu (ölçüm 2026-11-19). Metin olmayan ``source`` sayfanın betiğini durdururdu
    (``schemaLink``: ``toLowerCase``). Alan yok sayılır, koşum ve izlenen şema görünmeye devam eder; iki istek kalıcı
    bozulmayı da sınar."""
    now = time.time()
    root, directory = tmp_path / "kok", tmp_path / "olaylar"
    root.mkdir()
    directory.mkdir()
    write_asc_file(root / "s.asc", "Version 4\n")
    start = {"kind": "run_start", "t": now, "pid": os.getpid(), "run": "r-1", "circuit": "c"}
    lines = [{"kind": "process", "t": now, "pid": os.getpid(), "argv": ["x.py"]}, start]
    if field == "source":
        start["source"] = value
    elif field == "netlist":
        lines.append({"kind": "ltspice_start", "t": now, "pid": os.getpid(), "run": "r-1", "ltspice_pid": 9,
                      "netlist": value})
    else:
        lines.append({"kind": "run_end", "t": now, "pid": os.getpid(), "run": "r-1", "success": True, "log": value})
    (directory / "1-1.jsonl").write_text("".join(json.dumps(e) + "\n" for e in lines), encoding="utf-8")
    with _serving(monitor.Monitor(directory, asc_roots=[root])) as server:
        for _ in range(2):
            state, listing = _strict_json(server, "/api/state"), _strict_json(server, "/api/schematics")
    runs = state["active"] + state["finished"]
    assert [(r["circuit"], r["source"]) for r in runs] == [("c", None)]
    assert [f["name"] for f in listing["files"]] == ["s.asc"]


def test_log_facts_of_a_real_ltspice_log():
    assert events.log_facts((DATA / "ref26_flow_rc.log.txt").read_text(encoding="utf-8")) == RC_LOG_FACTS


def test_log_facts_match_whole_lines():
    """``"Maximum thread count: 1" in log`` 12 çekirdeğin satırında da doğrudur (GEMINI.md, 2026-10-11). Gerçek LTspice
    26.1.1 kayıtları: ref26_ac_bode.log.txt satır 5 "Maximum thread count: 12", ref26_flow_rc_gear.log.txt satır 9
    "method = Gear"."""
    assert events.log_facts(LogParser.read_text(DATA / "ref26_ac_bode.log.txt"))["threads"] == 12
    assert events.log_facts(LogParser.read_text(DATA / "ref26_flow_rc_gear.log.txt"))["method"] == "Gear"
    assert events.log_facts("x Maximum thread count: 3\nmethod = \nTotal elapsed time: 2 s\n") == {}


@pytest.mark.parametrize("argv,label", [
    ([r"C:\Py\Lib\site-packages\pytest\__main__.py", "tests"], "pytest"),  # python -m pytest
    ([r"C:\Py\Scripts\pytest.exe", "-q"], "pytest"),
    (["/usr/bin/py.test"], "pytest"),
    (["examples/15_hotplug_damping_validation.py"], "15_hotplug_damping_validation.py"),
    ([r"C:\tmp\pytest-of-u\pytest-1\run.py"], "run.py"),  # yolda pytest geçmesi süreci pytest yapmaz
    ([r"C:\Py\Lib\site-packages\pip\__main__.py"], "python -m pip"),
    (["-c"], "python -c"),
    ([""], "python"),
    ([], "python"),
])
def test_process_label(argv, label):
    assert events.process_label(argv) == label


# --------------------------------------------------------------------------- durum modeli
def _ev(kind, t, run="p-1-1", **fields):
    return {"kind": kind, "t": t, "pid": 77, "run": run, "src": "p-1", **fields}


def _state(*evts, **kw):
    state = events.MonitorState(**kw)
    state.ingest({"kind": "process", "t": 0.5, "pid": 77, "src": "p-1", "argv": ["examples/15_x.py"]})
    for event in evts:
        state.ingest(event)
    return state


def _alive(pid, first):
    return True


def _no_files(path):
    return None


def test_state_follows_the_documented_sequence_with_a_stall_retry():
    state = _state(_ev("run_start", 1.0, circuit="rc", guard=True, timeout=30, stall=10))

    def stage():
        return state.snapshot(2.0, _alive, _no_files)["active"][0]["stage"]

    assert stage() == "preparing"
    state.ingest(_ev("cpu_request", 1.1, want=4))
    assert stage() == "waiting_cpu"
    state.ingest(_ev("cpu_lease", 1.2, threads=4, waited=0.1, budget=12))
    assert state.snapshot(2.0, _alive, _no_files)["budget"] == {"in_use": 4, "size": 12}
    state.ingest(_ev("ltspice_start", 1.3, ltspice_pid=9, netlist="C:/ws/rc.net"))
    assert stage() == "running"
    state.ingest(_ev("ltspice_exit", 1.8, outcome="stalled", returncode=1, seconds=0.5, crash=None))
    assert stage() == "stalled"
    state.ingest(_ev("cpu_release", 1.81, threads=4))
    assert state.snapshot(2.0, _alive, _no_files)["budget"]["in_use"] == 0
    state.ingest(_ev("cpu_request", 1.82, want=1))
    state.ingest(_ev("cpu_lease", 1.83, threads=1, waited=0.0, budget=12))
    state.ingest(_ev("ltspice_start", 1.9, ltspice_pid=10, netlist="C:/ws/rc.net"))
    assert stage() == "running"
    assert state.snapshot(2.0, _alive, _no_files)["active"][0]["attempts"] == 2
    state.ingest(_ev("ltspice_exit", 2.5, outcome="exited", returncode=0, seconds=0.6, crash=None))
    assert stage() == "collecting"
    state.ingest(_ev("cpu_release", 2.51, threads=1))
    state.ingest(_ev("run_end", 2.6, success=True, elapsed=1.6, threads=1, n_errors=0, errors=[], n_warnings=1,
                     warnings=["w"], n_meas=2, log=None, raw=None))
    snap = state.snapshot(3.0, _alive, _no_files)
    assert snap["active"] == []
    assert snap["counts"] == {"ok": 1, "failed": 0, "abandoned": 0}
    (done,) = snap["finished"]
    assert (done["status"], done["outcomes"], done["threads"], done["elapsed"]) == ("ok", ["stalled", "exited"], 1, 1.6)
    assert (done["process"], done["n_warnings"], done["log"]) == ("15_x.py", 1, False)


def test_run_end_frees_a_lease_whose_release_was_lost():
    state = _state(_ev("run_start", 1.0, guard=True), _ev("cpu_lease", 1.1, threads=3, budget=8))
    assert state.snapshot(1.5, _alive, _no_files)["budget"] == {"in_use": 3, "size": 8}
    state.ingest(_ev("run_end", 2.0, success=False, n_errors=1, errors=["e"]))
    assert state.snapshot(2.5, _alive, _no_files)["budget"] == {"in_use": 0, "size": 8}
    assert state.snapshot(2.5, _alive, _no_files)["counts"] == {"ok": 0, "failed": 1, "abandoned": 0}


def test_run_of_a_dead_process_is_abandoned_until_its_result_arrives():
    asked = []

    def dead(pid, first):
        asked.append((pid, first))
        return False

    state = _state(_ev("run_start", 5.0, circuit="rc", guard=True), _ev("cpu_lease", 5.1, threads=2, budget=4))
    snap = state.snapshot(9.0, dead, _no_files)
    assert snap["active"] == [] and snap["budget"]["in_use"] == 0
    assert (snap["finished"][0]["status"], snap["finished"][0]["elapsed"]) == ("abandoned", 4.0)
    assert asked == [(77, 0.5)]  # PID yeniden kullanımı için sürecin ilk olay zamanı (process satırı) verilir
    state.ingest(_ev("run_end", 8.0, success=True, elapsed=3.0))  # geç okunan sonuç kazanır
    assert state.snapshot(10.0, dead, _no_files)["finished"][0]["status"] == "ok"


def test_running_run_shows_the_growth_of_the_files_ltspice_writes():
    state = _state(_ev("run_start", 1.0), _ev("ltspice_start", 1.1, ltspice_pid=9, netlist=str(Path("C:/ws/rc.net"))))
    files = {str(Path("C:/ws/rc.raw")): (2048, 4.0), str(Path("C:/ws/rc.log")): (100, 3.0)}
    view = state.snapshot(5.5, _alive, files.get)["active"][0]
    assert (view["raw_bytes"], view["log_bytes"], view["since_change"]) == (2048, 100, 1.5)


def test_only_the_newest_finished_runs_are_kept():
    state = events.MonitorState(keep=2)
    for i in range(4):
        state.ingest(_ev("run_start", float(i), run=f"r{i}", circuit=f"c{i}"))
        state.ingest(_ev("run_end", i + 0.5, run=f"r{i}", success=True))
    assert [r["circuit"] for r in state.snapshot(9.0, _alive, _no_files)["finished"]] == ["c3", "c2"]


@pytest.mark.parametrize("keep,expected", [(1, ["c0"]), (5, ["c0", "c1"])])
def test_finished_runs_are_ordered_and_kept_by_their_end(keep, expected):
    """r0 önce başlar ama sonra biter: en yeni biten r0'dır; liste bitişe göre yeniden eskiye, keep=1'de kalan r0."""
    state = events.MonitorState(keep=keep)
    state.ingest(_ev("run_start", 1.0, run="r0", circuit="c0"))
    state.ingest(_ev("run_start", 2.0, run="r1", circuit="c1"))
    state.ingest(_ev("run_end", 3.0, run="r1", success=True))
    state.ingest(_ev("run_end", 4.0, run="r0", success=True))
    assert [r["circuit"] for r in state.snapshot(5.0, _alive, _no_files)["finished"]] == expected


def test_runs_that_end_in_the_same_clock_tick_are_listed_newest_first():
    """Windows'ta Python 3.13'ten önce ``time.time()`` 15,6 ms adımla ilerler (Python 3.13 "What's New": artık
    GetSystemTimePreciseAsFileTime): art arda biten iki koşumun ``t``'si eşit olabilir (CI Windows 3.10/3.12, haftalık iş
    Windows 3.12). Liste yine en yeni biten önce olmalı: eşitlikte bitiş olaylarının okuma sırası (``end_seq``) belirler;
    başlangıcınkinden ayrıldığı vaka `test_tied_ends_are_ordered_by_their_end_not_their_start`. test-reviewer 2026-11-19:
    1/64 sn'lik saatle sıra 40 denemenin 5'inde tersti."""
    state = events.MonitorState()
    for run in ("r1", "r2"):
        state.ingest(_ev("run_start", 1.0, run=run, circuit=run))
        state.ingest(_ev("run_end", 1.0, run=run, success=True))
    assert [r["circuit"] for r in state.snapshot(2.0, _alive, _no_files)["finished"]] == ["r2", "r1"]


@pytest.mark.parametrize("keep,expected", [(200, ["r1", "r2"]), (1, ["r1"])])
def test_tied_ends_are_ordered_by_their_end_not_their_start(keep, expected):
    """r1 önce başlar, r2 sonra; r2 önce biter, r1 sonra, ikisi aynı saat adımında (t=2.0). En yeni biten r1'dir: eşitlikte
    bitiş olaylarının okuma sırası belirler, başlangıcınki değil (`test_finished_runs_are_ordered_and_kept_by_their_end`'in
    "bitişe göre" sözleşmesi). keep=1'de kalan r1 (test-reviewer 2026-11-19, ikinci tur)."""
    state = events.MonitorState(keep=keep)
    state.ingest(_ev("run_start", 1.0, run="r1", circuit="r1"))
    state.ingest(_ev("run_start", 1.2, run="r2", circuit="r2"))
    state.ingest(_ev("run_end", 2.0, run="r2", success=True))
    state.ingest(_ev("run_end", 2.0, run="r1", success=True))
    assert [r["circuit"] for r in state.snapshot(3.0, _alive, _no_files)["finished"]] == expected


def test_events_of_a_run_that_started_before_the_window_are_ignored():
    state = _state(_ev("ltspice_start", 1.0, ltspice_pid=1, netlist="x.net"), _ev("run_end", 2.0, success=True))
    snap = state.snapshot(3.0, _alive, _no_files)
    assert (snap["active"], snap["finished"], snap["events_seen"]) == ([], [], 3)


def test_log_facts_are_read_once_when_the_run_ends():
    seen = []

    def read_log(path, ended):
        seen.append((path, ended))
        return {"threads": 2}

    state = _state(_ev("run_start", 1.0), _ev("run_end", 2.0, success=True, log="C:/ws/rc.log"), read_log=read_log)
    state.snapshot(3.0, _alive, _no_files)
    assert seen == [("C:/ws/rc.log", 2.0)]
    assert state.snapshot(3.0, _alive, _no_files)["finished"][0]["log_facts"] == {"threads": 2}


# --------------------------------------------------------------------------- sunucu
@pytest.fixture
def served(tmp_path):
    server = monitor.make_server(monitor.Monitor(tmp_path / "served_events"), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(10)


def _get(server, path, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        conn.request("GET", path, headers={"Host": host or f"127.0.0.1:{server.port}"})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


@pytest.fixture
def served_events(served, monkeypatch):
    monkeypatch.setenv(events.ENV_EVENTS, "1")
    monkeypatch.setenv(events.ENV_EVENT_DIR, str(served.monitor.reader.directory))
    return served


def test_page_is_served_with_a_policy_against_foreign_scripts(served):
    status, headers, body = _get(served, "/")
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    policy = dict(part.strip().split(" ", 1) for part in headers["Content-Security-Policy"].split(";") if part.strip())
    # yönerge yönerge: betik yalnız /app.js; veri yalnız aynı sunucudan (fetch -> connect-src); stil sayfanın <style>
    # bloğu; görsel yüklenmez; <base> ve form ele geçirilemez (ikisi default-src'ye düşmez)
    assert policy == {"default-src": "'none'", "script-src": "'self'", "connect-src": "'self'",
                      "style-src": "'unsafe-inline'", "img-src": "'self' data:", "base-uri": "'none'",
                      "form-action": "'none'"}
    assert body.count(b'<script src="/app.js"></script>') == 1
    status, headers, script = _get(served, "/app.js")
    assert status == 200 and headers["Content-Type"].startswith("text/javascript")
    assert b"innerHTML" not in script  # veri yalnızca textContent ile yazılır (monitor belge dizgisi)


@pytest.mark.parametrize("path", ["/", "/app.js", "/api/state", "/sema", "/sema.js", "/api/schematics"])
@pytest.mark.parametrize("host", ["evil.example", "evil.example:{port}", "127.0.0.1:1", "",
                                  "localhost.evil.example:{port}", "127.0.0.1.evil.example:{port}"])
def test_request_for_a_foreign_host_is_refused(served, host, path):
    """DNS rebinding: 127.0.0.1'e çözülen yabancı bir ad sayfayı ve logları okuyamamalı (monitor belge dizgisi). Ad bütün
    olarak karşılaştırılır (RFC 9110 §7.2, Host = uri-host [":" port]): ``localhost.evil.example`` yerel değildir."""
    assert _get(served, path, host=host.format(port=served.port) or " ")[0] == 403


def _cors_headers(response):
    return [name for name, _ in response.getheaders() if name.lower().startswith("access-control-")]


@pytest.mark.parametrize("path", ["/", "/app.js", "/api/state", "/api/log?run=yok", "/sema", "/sema.js",
                                  "/api/schematics", "/api/schematic?path=x.asc"])
def test_no_response_lets_another_site_read_it(served, path):
    """SECURITY.md: ziyaret edilen bir sayfa monitörün gösterdiğini okuyamamalı. Host denetimi yalnız DNS rebinding'i keser:
    başka bir sitenin ``fetch("http://127.0.0.1:8765/api/state")`` isteği geçerli Host'la gelir ve tarayıcı yanıtı yalnız
    ``Access-Control-Allow-Origin`` olmadığı için sayfaya vermez (Fetch Standard, "CORS check")."""
    conn = http.client.HTTPConnection("127.0.0.1", served.port, timeout=10)
    try:
        conn.request("GET", path, headers={"Host": f"127.0.0.1:{served.port}", "Origin": "http://evil.example"})
        response = conn.getresponse()
        response.read()
        assert _cors_headers(response) == []
    finally:
        conn.close()


def test_preflight_for_the_folder_request_is_not_granted(served):
    """``monitor.POST_HEADER``: başka bir sitenin betiği bu başlığı koyarsa tarayıcı önce izin sorar (CORS ön isteği,
    Fetch Standard "CORS-preflight fetch") ve sunucu izin vermez: ön istek 2xx almaz, ``Access-Control-Allow-*`` görmez."""
    conn = http.client.HTTPConnection("127.0.0.1", served.port, timeout=10)
    try:
        conn.request("OPTIONS", "/api/roots/add", headers={
            "Host": f"127.0.0.1:{served.port}", "Origin": "http://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": f"content-type,{monitor.POST_HEADER.lower()}"})
        response = conn.getresponse()
        response.read()
        assert not 200 <= response.status < 300 and _cors_headers(response) == []
    finally:
        conn.close()


def test_log_of_a_real_run_is_refused_to_a_foreign_host(tmp_path, ltspice, served_events):
    _adapter(tmp_path, cpu_guard=False).run_netlist(RC, circuit_name="rc", treat_as_text=True)
    (run,) = json.loads(_get(served_events, "/api/state")[2])["finished"]
    path = "/api/log?run=" + run["run"]
    assert _get(served_events, path)[0] == 200  # gerçek koşum: 403'ün tek nedeni Host
    assert _get(served_events, path, host="evil.example")[0] == 403


@pytest.mark.parametrize("host", ["127.0.0.1:{port}", "localhost:{port}", "LOCALHOST:{port}"])
def test_request_for_a_local_name_is_served(served, host):
    assert _get(served, "/api/state", host=host.format(port=served.port))[0] == 200


def test_unknown_paths_and_runs_are_not_found(served):
    assert _get(served, "/api/log?run=nope")[0] == 404
    assert _get(served, "/../events")[0] == 404


def test_state_shows_a_finished_run_with_facts_from_its_ltspice_log(tmp_path, ltspice, served_events):
    res = _adapter(tmp_path, cpu_guard=False).run_netlist(RC, circuit_name="rc", treat_as_text=True)
    status, headers, body = _get(served_events, "/api/state")
    state = json.loads(body)
    assert status == 200 and headers["Content-Type"].startswith("application/json")
    assert state["active"] == [] and state["event_dir"] == str(served_events.monitor.reader.directory)
    (run,) = state["finished"]
    assert (run["circuit"], run["status"], run["n_meas"]) == ("rc", "ok", 2)
    assert run["log_facts"] == RC_LOG_FACTS
    status, _, body = _get(served_events, "/api/log?run=" + run["run"])
    log = json.loads(body)
    assert status == 200 and log["path"] == str(res.log_path)
    assert log["text"] == LogParser.read_text(res.log_path)


def test_long_log_is_shown_from_its_end(tmp_path, served_events):
    """monitor.MAX_LOG_CHARS: log sayfada sonuyla gösterilir (hata ve özet sondadır)."""
    long_log = tmp_path / "uzun.log"
    long_log.write_text("a" * 1000 + "b" * monitor.MAX_LOG_CHARS, encoding="utf-8")
    events.emit("run_start", run="u-1", circuit="u")
    events.emit("run_end", run="u-1", success=True, log=str(long_log))
    _get(served_events, "/api/state")
    status, _, body = _get(served_events, "/api/log?run=u-1")
    assert status == 200 and json.loads(body)["text"] == "b" * monitor.MAX_LOG_CHARS


def test_only_files_ending_in_log_are_read(tmp_path, served_events):
    """monitor belge dizgisi: yalnızca olaylarda geçen ``.log`` dosyaları okunur; olay dizinine yazabilen biri ``.log``
    olmayan bir dosyayı okutamaz. Yalnız sonek denetlenir: ``.log`` adlı bir bağlantı hedefini okutur."""
    secret = tmp_path / "gizli.txt"
    secret.write_text("gizli içerik", encoding="utf-8")
    events.emit("run_start", run="g-1", circuit="g")
    events.emit("run_end", run="g-1", success=True, log=str(secret))
    (run,) = json.loads(_get(served_events, "/api/state")[2])["finished"]
    assert run["log_facts"] == {"stale": True}
    status, _, body = _get(served_events, "/api/log?run=g-1")
    # gövde çözülerek aranır: sunucu ASCII kaçışlı JSON yazar, ham baytta Türkçe metin aranırsa sızıntı görünmezdi
    assert status == 409 and json.dumps(json.loads(body), ensure_ascii=False).count("gizli içerik") == 0


def test_log_is_hidden_only_by_a_later_run_with_the_same_name_and_workspace(tmp_path, ltspice, served_events):
    """Aynı dosyalara yalnız aynı ``circuit_name`` ve aynı çalışma dizini yazar (LTSpiceAdapter belgesi)."""
    ltspice.script = ["replay"] * 3
    first_ws, other_ws = _adapter(tmp_path / "a", cpu_guard=False), _adapter(tmp_path / "b", cpu_guard=False)
    first_ws.run_netlist(RC, circuit_name="rc", treat_as_text=True)
    time.sleep(0.01)  # olay zamanları milisaniyelik: sıra kesin olsun
    first_ws.run_netlist(RC, circuit_name="baska", treat_as_text=True)  # aynı dizin, başka ad
    time.sleep(0.01)
    other_ws.run_netlist(RC, circuit_name="rc", treat_as_text=True)  # aynı ad, başka dizin
    first = min(json.loads(_get(served_events, "/api/state")[2])["finished"], key=lambda r: r["started"])
    assert first["circuit"] == "rc"
    assert _get(served_events, "/api/log?run=" + first["run"])[0] == 200


def test_log_deleted_after_its_run_is_reported_missing(tmp_path, ltspice, served_events):
    res = _adapter(tmp_path, cpu_guard=False).run_netlist(RC, circuit_name="rc", treat_as_text=True)
    (run,) = json.loads(_get(served_events, "/api/state")[2])["finished"]
    res.log_path.unlink()
    status, _, body = _get(served_events, "/api/log?run=" + run["run"])
    assert status == 409 and "artık yok" in json.loads(body)["error"]


def test_log_overwritten_by_a_later_run_of_the_same_name_is_not_shown(tmp_path, ltspice, served_events):
    ltspice.script = ["replay", "replay"]
    adapter = _adapter(tmp_path, cpu_guard=False)
    adapter.run_netlist(RC, circuit_name="rc", treat_as_text=True)
    adapter.run_netlist(RC, circuit_name="rc", treat_as_text=True)
    finished = json.loads(_get(served_events, "/api/state")[2])["finished"]
    newest, oldest = finished  # en yenisi önce
    assert newest["started"] >= oldest["started"]
    status, _, body = _get(served_events, "/api/log?run=" + oldest["run"])
    assert status == 409 and "üzerine yazdı" in json.loads(body)["error"]
    assert _get(served_events, "/api/log?run=" + newest["run"])[0] == 200


def test_log_changed_after_its_run_ended_is_not_shown(tmp_path, ltspice, served_events):
    res = _adapter(tmp_path, cpu_guard=False).run_netlist(RC, circuit_name="rc", treat_as_text=True)
    later = time.time() + 60
    os.utime(res.log_path, (later, later))
    (run,) = json.loads(_get(served_events, "/api/state")[2])["finished"]
    assert run["log_facts"] == {"stale": True}
    status, _, body = _get(served_events, "/api/log?run=" + run["run"])
    assert status == 409 and "değişti" in json.loads(body)["error"]


def test_busy_port_is_reported_instead_of_shared(served, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata_yok"))  # geliştiricinin LTspice.ini'si ve monitor.json'ı okunmaz
    assert monitor.main(["--port", str(served.port), "--dir", str(tmp_path)]) == 1
    assert "dinlenemedi" in capsys.readouterr().err


@contextlib.contextmanager
def _cli(args, cwd, appdata=None):
    """``python -m ltspice_llm.monitor --port 0 ...`` başlatır, ilk satırındaki portu verir, sonunda öldürür. Depo kökü
    PYTHONPATH'te: başka bir kopyaya kurulu paket koşmasın (GEMINI.md "Proje"). APPDATA'da LTspice.ini yoksa (varsayılan)
    sonuç bu makinedeki LTspice ayarına bağlı olmaz."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "APPDATA": str(appdata or Path(cwd) / "appdata_yok"),
           "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT), os.environ.get("PYTHONPATH")]))}
    cmd = [sys.executable, "-m", "ltspice_llm.monitor", "--port", "0", *args]
    with subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          encoding="utf-8") as proc:
        try:
            line = proc.stdout.readline()  # "LTspice monitörü: http://127.0.0.1:<port>/  şemalar: ..."
            match = re.search(r"http://127\.0\.0\.1:(\d+)/", line)
            assert match, line + proc.stderr.read()
            yield int(match.group(1))
        finally:
            proc.kill()
            proc.communicate(timeout=10)


def _json_get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()


def test_command_line_serves_the_state_until_stopped(tmp_path):
    """``--asc`` verilmezse şemalar çalışma klasöründen listelenir (monitor main yardımı)."""
    directory, work = tmp_path / "cli_events", tmp_path / "calisma"
    work.mkdir()
    write_asc_file(work / "devre.asc", "Version 4\n")
    with _cli(["--dir", str(directory)], cwd=work) as port:
        status, state = _json_get(port, "/api/state")
        assert status == 200
        assert (state["events_seen"], state["event_dir"]) == (0, str(directory))
        listing = _json_get(port, "/api/schematics")[1]
    assert listing["roots"] == [str(work.resolve())]
    assert [f["name"] for f in listing["files"]] == ["devre.asc"]


def test_command_line_also_watches_ltspice_folder_and_recent_schematics(tmp_path):
    """``--asc`` yoksa LTspice'ın çalışma klasörü de izlenir; LTspice'ta son açılan şema her zaman listelenir (ayar
    dosyası LTspice 26.1.1 gibi BOM'lu UTF-16LE)."""
    work, ltspice_work, elsewhere = tmp_path / "calisma", tmp_path / "ltspice", tmp_path / "baska"
    for folder in (work, ltspice_work, elsewhere):
        folder.mkdir()
    opened = write_asc_file(elsewhere / "acilan.asc", "Version 4\n")
    appdata = tmp_path / "appdata"
    appdata.mkdir()
    ini = f"[Options]\r\nWorkingDirectory={ltspice_work}\r\n[Recent File List]\r\nFile1={opened}\r\n"
    (appdata / "LTspice.ini").write_bytes(b"\xff\xfe" + ini.encode("utf-16-le"))
    added = tmp_path / "sayfadan_eklenen"  # önceki bir oturumda sayfadan eklenmiş (monitor.config_path)
    added.mkdir()
    (appdata / "ltspice_llm").mkdir()
    (appdata / "ltspice_llm" / "monitor.json").write_text(json.dumps({"roots": [str(added)]}), encoding="utf-8")
    with _cli(["--dir", str(tmp_path / "e")], cwd=work, appdata=appdata) as port:
        listing = _json_get(port, "/api/schematics")[1]
    assert listing["roots"] == [str(work.resolve()), str(ltspice_work.resolve()), str(added.resolve())]
    assert [f["name"] for f in listing["files"]] == ["acilan.asc"]


def test_command_line_watches_each_asc_option(tmp_path):
    folder, single = tmp_path / "klasor", tmp_path / "tek.asc"
    folder.mkdir()
    write_asc_file(single, "Version 4\n")
    with _cli(["--dir", str(tmp_path / "e"), "--asc", str(folder), "--asc", str(single)], cwd=tmp_path) as port:
        listing = _json_get(port, "/api/schematics")[1]
    assert listing["roots"] == [str(folder.resolve()), str(single.resolve())]
    assert [f["name"] for f in listing["files"]] == ["tek.asc"]


def test_pid_alive_tells_a_running_process_from_an_exited_one():
    assert monitor.pid_alive(os.getpid(), time.time())
    with subprocess.Popen([sys.executable, "-c", "pass"]) as proc:
        proc.wait()
        assert not monitor.pid_alive(proc.pid, time.time())


def test_pid_alive_is_false_once_the_process_handle_is_released():
    """Tutamaç kapanınca Windows'ta OpenProcess başarısız olur (ölçüm, test-reviewer 2026-11-19: tutamaç 0, hata 87);
    bu dal da ölü demeli, yoksa yazan Python ölünce koşum hiç "yarım kaldı" olmaz."""
    with subprocess.Popen([sys.executable, "-c", "pass"]) as proc:
        proc.wait()
    pid = proc.pid
    del proc
    assert not monitor.pid_alive(pid, time.time())


@pytest.mark.parametrize("pid", [0, -1, 2 ** 32, 2 ** 63, 2 ** 32 + os.getpid()],
                         ids=["sifir", "eksi-bir", "2-uzeri-32", "2-uzeri-63", "2-uzeri-32-arti-kendi-pid"])
def test_impossible_pids_are_not_alive(pid):
    # açık kimlik: değer süreçten sürece değişir; isolation.py düğümü yeni süreçte aynı kimlikle bulabilmeli
    """POSIX kill(2): 0 ve negatif PID süreç grubunu gösterir (``kill(0, 0)`` "yaşıyor" derdi); ``pid_t`` 32 bittir ve
    CPython 2**31'den büyük PID'de OverflowError verir. Windows'ta PID bir DWORD'dür ve ctypes fazlasını maskeler:
    ``2**32 + kendi PID'i`` bu sürecin kendisi sayılırdı (test-reviewer 2026-11-20). Olay dosyasındaki böyle bir PID'in
    koşumu yarım kalmış sayılmalı, sayfa düşmemeli."""
    assert monitor.pid_alive(pid, time.time()) is False


@pytest.mark.skipif(sys.platform != "win32", reason="PID yeniden kullanımı GetProcessTimes ile, yalnız Windows'ta")
def test_process_created_after_the_first_event_has_reused_the_pid():
    assert not monitor.pid_alive(os.getpid(), time.time() - 365 * 86400)
