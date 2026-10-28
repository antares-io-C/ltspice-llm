"""
`.save` sinyal budaması, `run_sweep` (yerel .step taraması) ve `run_batch` (sınırlı eşzamanlılık)
davranış testleri. Gerçek LTspice çalıştırılmaz; süreç başlatma sahte bir Popen ile izlenir.
"""

import re
import sys

import pytest

import ltspice_llm.adapter as adapter_mod
import ltspice_llm.cpu_guard as guard
from ltspice_llm import LTSpiceAdapter
from ltspice_llm.adapter import AUTO_THREADS_MARK
from ltspice_llm.cpu_guard import CpuSlotPool
from ltspice_llm.netlist import measured_signals, validated_save_signal

BUCK = (
    "* buck\n"
    "V1 vin 0 12\n"
    "L1 vsw vout 22u\n"
    "C1 vout 0 100u\n"
    "R1 vout 0 2.5\n"
    ".tran 0 1m\n"
    ".meas tran vpp PP V(vout) FROM 0.5m TO 1m\n"
    ".meas tran il MAX I(L1)\n"
    ".end\n"
)


class _FakePopen:
    calls = []

    def __init__(self, cmd, **kwargs):
        type(self).calls.append(cmd)
        self.returncode = 0
        self.pid = 999

    def communicate(self, timeout=None):
        return "", ""


@pytest.fixture
def fake_popen(monkeypatch):
    monkeypatch.setattr(adapter_mod.subprocess, "Popen", _FakePopen)
    _FakePopen.calls = []
    return _FakePopen


def _adapter(tmp_path, **kw):
    return LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path / "ws", **kw)


def _written(tmp_path, name):
    return (tmp_path / "ws" / (name + ".net")).read_text(encoding="utf-8")


def _save_lines(text):
    return [l for l in text.splitlines() if l.lower().startswith(".save")]


# --------------------------------------------------------------------------- .save yardımcıları
def test_measured_signals_collects_meas_references():
    assert measured_signals(BUCK) == ["V(vout)", "I(L1)"]


def test_measured_signals_ignores_comments_and_non_meas_lines():
    net = "* V(ghost)\n.meas tran a MAX V(out) ; V(comment)\nR1 V(x) 0 1\n"
    assert measured_signals(net) == ["V(out)"]


@pytest.mark.parametrize("bad", [
    "V(out)\n.tran 0 1 0",
    "V(out) ; yorum",
    "V(out) R9 0 0 1",
    "",
    "   ",
])
def test_validated_save_signal_rejects_injection(bad):
    with pytest.raises(ValueError, match="save"):
        validated_save_signal(bad)


@pytest.mark.parametrize("good,expected", [
    (" V( out ) ", "V(out)"),
    ("I(L1)", "I(L1)"),
    ("V(a,b)", "V(a,b)"),
    ("Ix(U1:D)", "Ix(U1:D)"),
    ("vout", "vout"),
])
def test_validated_save_signal_normalises(good, expected):
    assert validated_save_signal(good) == expected


# --------------------------------------------------------------------------- save_signals
def test_save_signals_always_keeps_measured_signals(tmp_path, fake_popen):
    """.meas'te geçen sinyaller düşürülürse ölçüm üretilemez; otomatik eklenmeli."""
    _adapter(tmp_path).run_netlist(BUCK, circuit_name="s", treat_as_text=True, save_signals=["V(vsw)"])
    lines = _save_lines(_written(tmp_path, "s"))
    assert len(lines) == 1
    assert lines[0].split()[1:] == ["V(vsw)", "V(vout)", "I(L1)"]


def test_save_signals_omitted_leaves_netlist_untouched(tmp_path, fake_popen):
    _adapter(tmp_path).run_netlist(BUCK, circuit_name="s", treat_as_text=True)
    assert ".save" not in _written(tmp_path, "s")


def test_save_after_end_does_not_block_signal_pruning(tmp_path, fake_popen):
    # LTspice ilk .end'den sonrasını okumaz: oradaki .save yokmuş gibi davranılmalı
    net = BUCK.replace(".end", ".end\n.save V(vin)\n.end")
    _adapter(tmp_path).run_netlist(net, circuit_name="s", treat_as_text=True, save_signals=["V(vout)"])
    visible = _written(tmp_path, "s").split(".end")[0]
    assert ".save V(vout)" in _save_lines(visible)[0]


def test_existing_save_directive_is_not_duplicated(tmp_path, fake_popen):
    net = BUCK.replace(".tran", ".save V(vin)\n.tran")
    _adapter(tmp_path).run_netlist(net, circuit_name="s", treat_as_text=True, save_signals=["V(vout)"])
    assert _save_lines(_written(tmp_path, "s")) == [".save V(vin)"]


def test_save_signals_are_deduplicated_case_insensitively(tmp_path, fake_popen):
    _adapter(tmp_path).run_netlist(BUCK, circuit_name="s", treat_as_text=True,
                                   save_signals=["v(vout)", "V(VOUT)", "I(L1)"])
    assert _save_lines(_written(tmp_path, "s")) == [".save v(vout) I(L1)"]


def test_save_signal_injection_is_rejected(tmp_path, fake_popen):
    with pytest.raises(ValueError, match="save"):
        _adapter(tmp_path).run_netlist(BUCK, circuit_name="s", treat_as_text=True,
                                       save_signals=["V(out)\n.tran 0 1 0"])


# --------------------------------------------------------------------------- run_sweep
def test_run_sweep_emits_single_step_directive_in_one_process(tmp_path, fake_popen):
    net = BUCK.replace("L1 vsw vout 22u", "L1 vsw vout {Lval}").replace(".tran", ".param Lval=22u\n.tran")
    _adapter(tmp_path).run_sweep(net, parameter="Lval", values=["10u", "22u", "47u"],
                                 circuit_name="sw", treat_as_text=True)
    text = _written(tmp_path, "sw")
    assert ".step param Lval list 10u 22u 47u" in text
    assert text.count(".step") == 1
    assert len(fake_popen.calls) == 1  # taramanın tamamı tek LTspice koşumunda


def test_run_sweep_parameterises_a_plain_element(tmp_path, fake_popen):
    """'L1' netlistte {L1} olarak kullanılmıyor; eleman otomatik parametrelenmeli."""
    _adapter(tmp_path).run_sweep(BUCK, parameter="L1", values=["10u", "47u"],
                                 circuit_name="sw", treat_as_text=True)
    text = _written(tmp_path, "sw")
    assert "L1 vsw vout {L1}" in text
    assert ".step param L1 list 10u 47u" in text


def test_run_sweep_accepts_numeric_values(tmp_path, fake_popen):
    _adapter(tmp_path).run_sweep(BUCK, parameter="R1", values=[1, 2.5], circuit_name="sw", treat_as_text=True)
    assert ".step param R1 list 1 2.5" in _written(tmp_path, "sw")


def test_run_sweep_rejects_unknown_parameter(tmp_path, fake_popen):
    with pytest.raises(ValueError, match="bulunamadı"):
        _adapter(tmp_path).run_sweep(BUCK, parameter="Qz", values=["1", "2"], treat_as_text=True)


@pytest.mark.parametrize("values", [["10u"], ["10u", "10u"], ["1k", "1000"]])
def test_run_sweep_rejects_single_distinct_value_before_running(tmp_path, fake_popen, values):
    """LTspice 26.1.1 tek adımlık .step'i "This results in only one step." diye koşmadan reddeder (ölçüldü)."""
    with pytest.raises(ValueError, match="en az iki farklı değer"):
        _adapter(tmp_path).run_sweep(BUCK, parameter="L1", values=values, treat_as_text=True)
    assert fake_popen.calls == []


def test_run_sweep_drops_numerically_duplicate_values(tmp_path, fake_popen):
    _adapter(tmp_path).run_sweep(BUCK, parameter="L1", values=["10u", "1e-5", "22u"], circuit_name="sw",
                                 treat_as_text=True)
    assert ".step param L1 list 10u 22u" in _written(tmp_path, "sw")


def test_run_sweep_rejects_empty_values_and_bad_names(tmp_path, fake_popen):
    ad = _adapter(tmp_path)
    with pytest.raises(ValueError, match="values"):
        ad.run_sweep(BUCK, parameter="L1", values=[], treat_as_text=True)
    with pytest.raises(ValueError, match="parametre adı"):
        ad.run_sweep(BUCK, parameter="bad name", values=["1"], treat_as_text=True)


def test_run_sweep_rejects_injected_value(tmp_path, fake_popen):
    with pytest.raises(ValueError, match="değeri"):
        _adapter(tmp_path).run_sweep(BUCK, parameter="L1", values=["10u\n.tran 0 9"], treat_as_text=True)


def test_run_sweep_rejects_asc_schematic(tmp_path, fake_popen):
    asc = "Version 4\nSHEET 1 880 680\nSYMBOL res 0 0 R0\n"
    with pytest.raises(ValueError, match="run_sweep"):
        _adapter(tmp_path).run_sweep(asc, parameter="R1", values=["1k"], treat_as_text=True)


def test_run_sweep_uses_binary_raw(tmp_path, fake_popen):
    """spicelib steplenmiş ASCII .raw okuyamaz: tarama her zaman ikili biçimde koşmalı."""
    _adapter(tmp_path).run_sweep(BUCK, parameter="L1", values=["10u", "22u"], circuit_name="sw", treat_as_text=True)
    assert "-ascii" not in fake_popen.calls[0]


def test_run_sweep_can_prune_signals(tmp_path, fake_popen):
    _adapter(tmp_path).run_sweep(BUCK, parameter="L1", values=["10u", "22u"], circuit_name="sw",
                                 treat_as_text=True, save_signals=["V(vsw)"])
    assert _save_lines(_written(tmp_path, "sw")) == [".save V(vsw) V(vout) I(L1)"]


# --------------------------------------------------------------------------- run_batch
def test_run_batch_returns_results_in_input_order(tmp_path, fake_popen):
    jobs = [dict(netlist_content_or_path=BUCK, circuit_name="c" + str(i), treat_as_text=True) for i in range(5)]
    results = _adapter(tmp_path).run_batch(jobs)
    assert [r.netlist_path.stem for r in results] == ["c" + str(i) for i in range(5)]
    assert len(fake_popen.calls) == 5


def test_run_batch_gives_each_job_its_own_workspace(tmp_path, fake_popen):
    """Aynı ada sahip işler birbirinin dosyalarını ezmemeli."""
    jobs = [dict(netlist_content_or_path=BUCK, circuit_name="same", treat_as_text=True) for _ in range(3)]
    results = _adapter(tmp_path).run_batch(jobs)
    assert len({r.netlist_path.parent for r in results}) == 3


def _twelve_cpus(monkeypatch, tmp_path):
    """Test makinesinin çekirdek sayısından bağımsız: 12 çekirdekli bir makine ve ona ait bütçe."""
    monkeypatch.setattr(adapter_mod, "available_cpus", lambda: 12)
    monkeypatch.setattr(guard.os, "cpu_count", lambda: 12)  # LTspice'ın sınırı da 12 (cpu_guard.ltspice_thread_limit)
    return CpuSlotPool(size=12, directory=tmp_path / "slots")


def _auto(n):
    return f".options threads={n} ; {AUTO_THREADS_MARK}"


def test_run_batch_clamps_workers_to_cpu_count(tmp_path, fake_popen, caplog, monkeypatch):
    ad = _adapter(tmp_path, cpu_guard=_twelve_cpus(monkeypatch, tmp_path))
    assert ad.default_workers() == ad.DEFAULT_BATCH_WORKERS
    jobs = [dict(netlist_content_or_path=BUCK, circuit_name="c" + str(i), treat_as_text=True) for i in range(20)]
    with caplog.at_level("WARNING"):
        ad.run_batch(jobs, max_workers=32)
    assert "mantıksal çekirdek sayısının (12) üstünde; 12'e kırpıldı" in caplog.text


def _threads_lines(text):
    return [l for l in text.splitlines() if re.search(r"threads\s*=", l, re.IGNORECASE)]


def test_run_batch_limits_solver_threads_to_avoid_oversubscription(tmp_path, fake_popen, monkeypatch):
    """Kök neden: LTspice çözücüsü tüm çekirdekler kadar iş parçacığı açar; örnek × thread ≤ çekirdek olmalı."""
    pool = _twelve_cpus(monkeypatch, tmp_path)
    jobs = [dict(netlist_content_or_path=BUCK, circuit_name="c" + str(i), treat_as_text=True) for i in range(8)]
    results = _adapter(tmp_path, cpu_guard=pool).run_batch(jobs, max_workers=4)
    for r in results:
        assert _threads_lines(r.netlist_path.read_text(encoding="utf-8")) == [_auto(3)]
        assert r.solver_threads == 3
    results = _adapter(tmp_path, cpu_guard=pool).run_batch(jobs, max_workers=8)
    assert _threads_lines(results[0].netlist_path.read_text(encoding="utf-8")) == [_auto(1)]


def test_run_batch_respects_user_threads_option(tmp_path, fake_popen):
    net = BUCK.replace(".tran", ".options threads=2\n.tran")
    results = _adapter(tmp_path).run_batch([dict(netlist_content_or_path=net, circuit_name="u", treat_as_text=True)])
    assert _threads_lines(results[0].netlist_path.read_text(encoding="utf-8")) == [".options threads=2"]


def test_run_batch_warns_when_explicit_solver_threads_oversubscribe(tmp_path, fake_popen, caplog, monkeypatch):
    ad = _adapter(tmp_path, solver_threads=6, cpu_guard=False)
    monkeypatch.setattr(adapter_mod, "available_cpus", lambda: 12)
    jobs = [dict(netlist_content_or_path=BUCK, circuit_name="c" + str(i), treat_as_text=True) for i in range(4)]
    with caplog.at_level("WARNING"):
        ad.run_batch(jobs, max_workers=4)
    assert "4 örnek × solver_threads=6 > 12 çekirdek" in caplog.text
    assert "aşırı abone" in caplog.text


# --------------------------------------------------------------------------- solver_threads
def test_without_guard_netlist_is_left_to_ltspice_default(tmp_path, fake_popen):
    _adapter(tmp_path, cpu_guard=False).run_netlist(BUCK, circuit_name="s", treat_as_text=True)
    assert _threads_lines(_written(tmp_path, "s")) == []


def test_guard_gives_a_lone_run_the_whole_machine(tmp_path, fake_popen, monkeypatch):
    """Tek başına koşum LTspice varsayılanı kadar hızlı kalmalı: tüm çekirdekler ona verilir."""
    res = _adapter(tmp_path, cpu_guard=_twelve_cpus(monkeypatch, tmp_path)).run_netlist(
        BUCK, circuit_name="s", treat_as_text=True)
    assert _threads_lines(_written(tmp_path, "s")) == [_auto(12)]
    assert res.solver_threads == 12


def test_solver_threads_are_capped_to_the_process_affinity(tmp_path, fake_popen, monkeypatch):
    """KILAVUZ "Kilitlenme koruması" 2: bütçe sürecin gerçekten kullanabildiği çekirdek sayısıdır (affinity maskesi
    okunur). CI'daki karşı örnek (2026-10-11): 2 çekirdekte solver_threads=3 -> threads=2. Makine 12 çekirdekli, yani
    havuz 3'e yeter; sınır affinity'den gelmeli."""
    monkeypatch.setattr(guard, "_affinity_count", lambda: 2)
    monkeypatch.setattr(guard.os, "cpu_count", lambda: 12)
    res = _adapter(tmp_path, solver_threads=3).run_netlist(BUCK, circuit_name="s", treat_as_text=True)
    assert _threads_lines(_written(tmp_path, "s")) == [_auto(2)]
    assert res.solver_threads == 2


def test_solver_threads_is_injected_once_and_refreshed_on_rerun(tmp_path, fake_popen, monkeypatch):
    """Araçlar çalışma alanındaki dosyayı hem kaynak hem hedef yapar: otomatik satır birikmemeli, güncellenmeli."""
    pool = _twelve_cpus(monkeypatch, tmp_path)
    _adapter(tmp_path, solver_threads=2, cpu_guard=pool).run_netlist(BUCK, circuit_name="s", treat_as_text=True)
    stored = tmp_path / "ws" / "s.net"
    assert _threads_lines(stored.read_text(encoding="utf-8")) == [_auto(2)]
    _adapter(tmp_path, solver_threads=5, cpu_guard=pool).run_netlist(stored, circuit_name="s")
    text = stored.read_text(encoding="utf-8")
    assert _threads_lines(text) == [_auto(5)]
    assert text.rstrip().endswith(".end")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_solver_threads_stays_before_end_across_rewrites(tmp_path, fake_popen, monkeypatch, newline):
    """
    Netlist metin kipinde yazılır (Windows'ta LF -> CRLF) ama bayt olarak okunuyordu: her okuma-yazma turunda bir CR
    birikiyor, ``.end\\r`` tanınmıyor ve threads satırı LTspice'ın okumadığı ``.end`` sonrasına düşüyordu. Ölçüm
    (22 işlik bir varyant çalışması, 4 eşzamanlı koşum): logların hepsinde ``Maximum thread count: 12``, takma anı
    koşumları livelock.
    """
    src = tmp_path / "src.net"
    src.write_bytes(BUCK.replace("\n", newline).encode("utf-8"))
    ad = _adapter(tmp_path, solver_threads=2, cpu_guard=_twelve_cpus(monkeypatch, tmp_path))
    ad.run_netlist(src, circuit_name="s")
    stored = tmp_path / "ws" / "s.net"
    ad.run_netlist(stored, circuit_name="s")      # araçlar çalışma alanındaki dosyayı yeniden kaynak yapar
    ad._write_solver_threads(stored, 1)           # takılma bekçisinin threads=1 ile yeniden denemesi
    data = stored.read_bytes()
    assert b"\r\r" not in data
    lines = data.decode("utf-8").splitlines()
    assert [l for l in lines if l.strip().lower() == ".end"] == [".end"]
    assert lines.index(_auto(1)) < lines.index(".end")


def test_crlf_netlist_text_gets_directives_before_end(tmp_path, fake_popen, monkeypatch):
    _adapter(tmp_path, solver_threads=2, cpu_guard=_twelve_cpus(monkeypatch, tmp_path)).run_netlist(
        BUCK.replace("\n", "\r\n"), circuit_name="s", treat_as_text=True, save_signals=["V(vout)"])
    lines = (tmp_path / "ws" / "s.net").read_bytes().decode("utf-8").splitlines()
    assert lines[-3:] == [".save V(vout) I(L1)", _auto(2), ".end"]


@pytest.mark.parametrize("existing", [".options threads=4", ".OPTIONS gmin=1e-12 Threads = 4", ".option threads=4"])
def test_solver_threads_does_not_override_user_setting(tmp_path, fake_popen, existing):
    net = BUCK.replace(".tran", existing + "\n.tran")
    _adapter(tmp_path, solver_threads=1).run_netlist(net, circuit_name="s", treat_as_text=True)
    assert _threads_lines(_written(tmp_path, "s")) == [existing]


def test_solver_threads_in_asc_schematic_uses_text_directive(tmp_path, fake_popen, monkeypatch):
    """CI (2 çekirdek) koruma 3'ü 2'ye kırpıyordu: çekirdek sayısı sabitlenir (2026-10-11)."""
    asc = "Version 4\nSHEET 1 880 680\nTEXT 0 0 Left 2 !.tran 1m\n"
    _adapter(tmp_path, solver_threads=3, cpu_guard=_twelve_cpus(monkeypatch, tmp_path)).run_netlist(
        asc, circuit_name="a", treat_as_text=True)
    text = (tmp_path / "ws" / "a.asc").read_text(encoding="utf-8")
    assert "TEXT 0 0 Left 2 !" + _auto(3) in text
    assert ".end" not in text  # .asc şemaya netlist sonu eklenmemeli


def test_asc_existing_save_is_detected(tmp_path, fake_popen):
    asc = "Version 4\nSHEET 1 880 680\nTEXT 0 0 Left 2 !.save V(out)\n"
    _adapter(tmp_path).run_netlist(asc, circuit_name="a", treat_as_text=True, save_signals=["V(x)"])
    text = (tmp_path / "ws" / "a.asc").read_text(encoding="utf-8")
    assert text.count(".save") == 1


@pytest.mark.parametrize("bad", [0, -1, True])
def test_solver_threads_rejects_invalid_values(tmp_path, bad):
    with pytest.raises(ValueError, match="solver_threads"):
        _adapter(tmp_path, solver_threads=bad)


def test_run_batch_handles_empty_and_failing_jobs(tmp_path, fake_popen):
    ad = _adapter(tmp_path)
    assert ad.run_batch([]) == []
    results = ad.run_batch([
        dict(netlist_content_or_path=BUCK, circuit_name="ok", treat_as_text=True),
        dict(netlist_content_or_path=BUCK, circuit_name="bad", treat_as_text=True, save_signals=["V(x);hack"]),
    ])
    assert len(results) == 2
    # Sahte Popen log üretmediği için ikisi de success=False; ayırt edici olan hatanın kaynağıdır:
    # geçerli iş normal akıştan geçer, geçersiz iş toplu koşum hatasıyla yalıtılır ve diğerini düşürmez.
    assert not any("Toplu koşum hatası" in e for e in results[0].errors)
    assert "Toplu koşum hatası" in results[1].errors[0]
    assert "Geçersiz .save sinyali" in results[1].errors[0]
