"""
Kod incelemesinde bulunan hataların regresyon testleri.

Bu dosyadaki testler LTspice KURULU OLMASINI GEREKTİRMEZ: simülatör sahte bir
adaptörle taklit edilir veya yalnızca saf fonksiyonlar test edilir.
"""

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from ltspice_llm import (
    AutonomousCircuitAgent,
    CircuitOptimizer,
    LTSpiceAdapter,
    LogParser,
    SimulationResult,
    WaveformVisualizer,
    get_ltspice_tool_schemas,
    handle_tool_call,
)
import ltspice_llm.adapter as adapter_mod
from ltspice_llm.adapter import _ANALYSIS_DIRECTIVE_RE, _set_param, sanitize_circuit_name
from ltspice_llm.parser import open_raw, raw_axis_values
from ltspice_llm.system_prompts import CIRCUIT_DESIGNER_SYSTEM_PROMPT, CIRCUIT_DESIGNER_SYSTEM_PROMPT_EN


@pytest.fixture
def adapter(tmp_path):
    """Gerçek LTspice çalıştırmayan ama dosya hazırlama mantığını kullanan adaptör."""
    return LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path / "ws")


# --------------------------------------------------------------------------- .param
MULTI = "* t\n.param R1=15.915k R2=15.915k C1=1.414n C2=0.707n\nR1 in out {R1}\n.end\n"


def _params(text):
    """Netlistteki tüm (isim -> [değerler]) tanımlarını döner."""
    found = {}
    for line in text.splitlines():
        if line.strip().lower().startswith(".param"):
            for name, val in re.findall(r"(\w+)\s*=\s*(\{[^}]*\}|[^\s;]+)", line):
                found.setdefault(name.lower(), []).append(val)
    return found


def test_param_first_of_many_keeps_the_rest():
    out = _set_param(MULTI, "R1", "22k")
    assert _params(out) == {"r1": ["22k"], "r2": ["15.915k"], "c1": ["1.414n"], "c2": ["0.707n"]}


def test_param_middle_of_many_is_replaced_not_duplicated():
    out = _set_param(MULTI, "R2", "22k")
    params = _params(out)
    assert params["r2"] == ["22k"]  # tek tanım: satıra eklenen ikinci .param olmamalı
    assert params["r1"] == ["15.915k"]
    assert out.count(".param") == 1


def test_param_legacy_syntax_case_insensitive_and_comment_kept():
    out = _set_param("* t\n.PARAM  Rx 1k ; not\nR1 a 0 {Rx}\n.end\n", "rx", "4.7k")
    assert ".PARAM  Rx 4.7k ; not" in out


def test_param_brace_expression_value_is_replaced_whole():
    out = _set_param("* t\n.param A={2*3} B=1\n.end\n", "A", "5")
    assert _params(out) == {"a": ["5"], "b": ["1"]}


def test_param_new_is_inserted_before_final_end_not_ends():
    net = "* t\n.subckt X a b\nR1 a b 1k\n.ends X\nX1 n 0 X\n.end\n"
    out = _set_param(net, "K", "3")
    lines = out.splitlines()
    assert lines[-1] == ".end"
    assert lines[-2] == ".param K=3"
    assert ".ends X" in lines


def test_param_new_is_inserted_before_crlf_end():
    """CRLF metinde ``.end\\r`` tanınmıyordu: yeni satır ve ikinci bir ``.end`` LTspice'ın okumadığı sona ekleniyordu."""
    out = _set_param("* t\r\nR1 a 0 1k\r\n.end\r\n", "K", "3")
    assert out.splitlines()[-2:] == [".param K=3", ".end"]
    assert out.count(".end") == 1


def test_param_new_is_inserted_before_first_end():
    """LTspice ilk ``.end``'de durur (ADI ".END": "All lines after .end are ignored"; ölçüm: examples/12)."""
    out = _set_param("* t\nR1 a 0 {K}\n.end\n* not\n.end\n", "K", "3")
    assert out.splitlines() == ["* t", "R1 a 0 {K}", ".param K=3", ".end", "* not", ".end"]


def test_title_line_is_never_taken_as_end():
    """İlk satır başlıktır (ADI "Netlist Structure"). Ölçüm (examples/12): başlığı ``.end`` olan netlistte satır
    başlığın önüne eklenince sınır satırı başlık oldu ve LTspice hiçbir şey koşmadı."""
    out = _set_param(".end\nR1 a 0 {K}\n.end\n", "K", "3")
    assert out.splitlines() == [".end", "R1 a 0 {K}", ".param K=3", ".end"]


def test_param_new_without_end_appends_end():
    out = _set_param("* t\nR1 a 0 1k", "K", "3")
    assert out.endswith(".param K=3\n.end\n")


@pytest.mark.parametrize("bad_value", ["1k\n.include C:\\secret.txt", "1k;x", "", "a\x00b"])
def test_param_rejects_injection_values(bad_value):
    with pytest.raises(ValueError):
        _set_param(MULTI, "R1", bad_value)


@pytest.mark.parametrize("bad_name", ["", "1R", "R 1", "R1\n.include x", "../x"])
def test_param_rejects_bad_names(bad_name):
    with pytest.raises(ValueError):
        _set_param(MULTI, bad_name, "1")


# ---------------------------------------------------------------- isim / dosya güvenliği
def test_sanitize_strips_path_components():
    assert sanitize_circuit_name("../../secret") == "secret"
    assert sanitize_circuit_name("C:\\Windows\\win") == "CWindowswin"
    assert sanitize_circuit_name("!!!") == "circuit"


def test_tools_cannot_read_netlists_outside_workspace(adapter):
    outside = adapter.workspace_dir.parent / "secret_outside.net"
    outside.write_text("TOP SECRET")
    res = handle_tool_call("get_circuit_netlist", {"circuit_name": "../secret_outside"}, adapter=adapter)
    assert res["status"] == "ERROR"
    assert "TOP SECRET" not in str(res)


def test_same_name_resolves_to_same_file_across_tools(adapter):
    """Oluşturma ve sonraki araçlar aynı (temizlenmiş) dosyaya bakmalı."""
    adapter._prepare_netlist_file("* x\nR1 a 0 1k\n.end\n", "my circuit!")
    res = handle_tool_call("get_circuit_netlist", {"circuit_name": "my circuit!"}, adapter=adapter)
    assert res["status"] == "SUCCESS"
    assert "R1 a 0 1k" in res["netlist"]


def test_prepare_netlist_treats_text_as_text_when_asked(adapter, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("PASSWORD=hunter2")
    out = adapter._prepare_netlist_file(str(secret), "leak", treat_as_text=True)
    assert "hunter2" not in out.read_text()


def test_tool_call_never_passes_llm_code_as_a_path(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("PASSWORD=hunter2")
    seen = {}

    class FakeAdapter:
        workspace_dir = tmp_path

        def run_netlist(self, netlist_content_or_path, circuit_name, parameters=None, **kw):
            seen.update(kw, content=netlist_content_or_path)
            return SimulationResult(success=True, netlist_path=tmp_path / "x.net")

    handle_tool_call("create_or_replace_circuit",
                     {"circuit_name": "c", "netlist_code": str(secret)}, adapter=FakeAdapter())
    assert seen["treat_as_text"] is True


def test_adapter_still_accepts_real_paths_by_default(adapter, tmp_path):
    src = tmp_path / "in.net"
    src.write_text("* from file\nR1 a 0 1k\n.end\n")
    out = adapter._prepare_netlist_file(src, "fromfile")
    assert "from file" in out.read_text()
    out = adapter._prepare_netlist_file(str(src), "fromfile2")  # geriye dönük: str yol
    assert "from file" in out.read_text()


# ------------------------------------------------------------------------------ parser
def test_questionable_use_is_warning_not_error():
    res = LogParser.parse_text('Questionable use of curly braces in "R1 a b {x}"\nv_max: MAX(V(out))=3.6 FROM 0 TO 1\n')
    assert res.errors == []
    assert any("Questionable" in w for w in res.warnings)
    assert res.measurements["v_max"] == 3.6


def test_measurement_name_containing_mag_does_not_trigger_when_logic():
    res = LogParser.parse_text("magx: MAX(V(out))=3.6 AT 0.002\n")
    assert res.measurements["magx"] == 3.6
    assert res.measurements["magx_at"] == 0.002


def test_when_query_still_returns_the_frequency():
    res = LogParser.parse_text("f_3db: mag(V(out))=0.7071 AT 1000.05\n")
    assert res.measurements["f_3db"] == 1000.05
    assert res.measurements["f_3db_val"] == 0.7071


def test_tran_when_returns_crossing_time_not_threshold():
    # LTspice 26 çıktısı: .meas tran t_cross WHEN V(out)=2.5
    res = LogParser.parse_text("t_cross: V(out)=2.5 AT 0.000693147910694\n")
    assert res.measurements["t_cross"] == pytest.approx(0.000693147910694)
    assert res.measurements["t_cross_val"] == 2.5


def test_find_mag_at_returns_value_not_frequency():
    # LTspice 26 çıktısı: .meas ac g1k FIND mag(V(out)) AT 1k
    res = LogParser.parse_text("g1k: mag(V(out))=(-3.01016507189dB,0°) at 1000\n")
    assert res.measurements["g1k"] == pytest.approx(-3.01016507189)
    assert res.measurements["g1k_at"] == 1000


def test_log_header_lines_are_not_measurements():
    log = ("LTspice 26.1.1 for Windows\nCircuit: C:\\x\\a.net\nStart Time: Wed Sep 30 09:29:29 2026\n"
           "solver = Normal\ntnom = 27\ntemp = 27\nmethod = trap\nDate: Wed Sep 30 09:29:29 2026\n"
           "Total elapsed time: 0.246 seconds.\nv_max: MAX(V(out))=3.6 FROM 0 TO 1\n")
    assert LogParser.parse_text(log).measurements == {"v_max": 3.6}


def test_utf16_log_without_bom_is_decoded(tmp_path):
    p = tmp_path / "x.log"
    p.write_bytes("v_max: MAX(V(out))=3.6 FROM 0 TO 1\n".encode("utf-16-le"))  # LTspice XVII biçimi
    assert LogParser.parse_file(p).measurements == {"v_max": 3.6}


def test_param_continuation_line_is_updated_not_duplicated():
    out = _set_param("* t\n.param R1=1k\n+ R2=2k\nR1 a 0 {R2}\n.end\n", "R2", "5k")
    assert "+ R2=5k" in out
    assert out.count("R2=") == 1


def test_plot_tool_accepts_signals_as_string(monkeypatch, adapter):
    (adapter.workspace_dir / "c.raw").write_text("x")
    seen = {}

    def fake_plot(raw_path, signals, title):
        seen["signals"] = signals
        return Path(raw_path).with_suffix(".png")

    monkeypatch.setattr(WaveformVisualizer, "plot_traces", staticmethod(fake_plot))
    res = handle_tool_call("plot_transient_waveforms", {"circuit_name": "c", "signals": "V(in), V(a,b)"},
                           adapter=adapter)
    assert res["status"] == "SUCCESS"
    assert seen["signals"] == ["V(in)", "V(a,b)"]


def test_tool_call_without_ltspice_returns_error_not_exception(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("LTspice yok")

    monkeypatch.setattr("ltspice_llm.tools.LTSpiceAdapter", boom)
    res = handle_tool_call("list_circuits", {})
    assert res["status"] == "ERROR"


@pytest.mark.parametrize("text,expected", [
    ("* t\nR1 a 0 1k\n.options plotwinsize=0\n.end\n", False),
    ("* t\n* .tran 1m\nR1 a 0 1k\n.end\n", False),
    ("* t\nR1 a 0 1k\n.tran 1m\n.end\n", True),
    ("* t\nR1 a 0 1k\n  .AC dec 10 1 1k\n.end\n", True),
    ("* t\nR1 a 0 1k\n.op\n.end\n", True),
])
def test_analysis_directive_detection(text, expected):
    assert bool(_ANALYSIS_DIRECTIVE_RE.search(text)) is expected


# --------------------------------------------------------------------- süreç sonlandırma
@pytest.mark.skipif(sys.platform != "win32", reason="Sahte LTspice olarak .bat kullanılıyor")
def test_timeout_kills_only_its_own_process(tmp_path):
    fake = tmp_path / "fake_ltspice.bat"
    fake.write_text("@echo off\nping -n 60 127.0.0.1 >nul\n")
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        ad = LTSpiceAdapter(executable_path=fake, workspace_dir=tmp_path / "ws")
        t0 = time.time()
        res = ad.run_netlist("* t\nR1 a 0 1k\n.tran 1m\n.end\n", circuit_name="hang", timeout=2)
        assert time.time() - t0 < 30
        assert res.success is False
        assert any("zaman aşımı" in e for e in res.errors)
        assert bystander.poll() is None  # başka süreçlere dokunulmadı
    finally:
        bystander.kill()
        bystander.wait()


# --------------------------------------------------------------------------- optimizer
class _FakeSim:
    """metric = f(param). fail(param) True ise simülasyon başarısız olur."""

    def __init__(self, f, fail=lambda p: False):
        self.f, self.fail, self.calls = f, fail, 0

    def run_netlist(self, netlist_content_or_path, circuit_name, parameters=None, **kw):
        self.calls += 1
        (p,) = parameters.values()
        if self.fail(p):
            return SimulationResult(success=False, netlist_path=Path("x.net"), errors=["fail"])
        return SimulationResult(success=True, netlist_path=Path("x.net"), measurements={"m": self.f(p)})


def _optimize(sim, **kw):
    args = dict(base_netlist="n", circuit_name="c", param_name="P", target_meas_name="m")
    args.update(kw)
    return CircuitOptimizer(adapter=sim).optimize_single_parameter(**args)


def test_optimizer_recovers_when_midpoint_simulation_fails():
    sim = _FakeSim(lambda p: p, fail=lambda p: abs(p - 50) < 1)  # ilk orta nokta (50) hep patlar
    res = _optimize(sim, target_value=30.0, min_param=0.0, max_param=100.0, max_iterations=20)
    assert res.converged is True
    assert abs(res.best_value - 30.0) <= 0.3
    assert len({s.parameters["P"] for s in res.history}) == len(res.history)  # aynı nokta tekrarlanmadı


def test_optimizer_gives_up_cleanly_when_whole_interval_fails():
    sim = _FakeSim(lambda p: p, fail=lambda p: 1 < p < 99)  # yalnızca uçlar simüle edilebilir
    res = _optimize(sim, target_value=30.0, min_param=0.0, max_param=100.0, max_iterations=50)
    assert res.converged is False
    assert sim.calls <= 2 + 5  # iki uç + en çok 5 farklı deneme, sonsuz tekrar yok


def test_optimizer_skips_search_when_target_is_outside_bracket():
    sim = _FakeSim(lambda p: p)
    res = _optimize(sim, target_value=500.0, min_param=1.0, max_param=100.0, max_iterations=15)
    assert res.converged is False
    assert sim.calls == 2  # yalnızca iki uç noktayı simüle etti


def test_optimizer_geometric_search_handles_many_decades():
    target = 1000.0
    geo = _optimize(_FakeSim(lambda p: p), target_value=target, min_param=1.0, max_param=1e6, max_iterations=15)
    lin = _optimize(_FakeSim(lambda p: p), target_value=target, min_param=1.0, max_param=1e6,
                    max_iterations=15, geometric=False)
    assert geo.converged is True
    assert lin.converged is False


def test_optimizer_linear_range_unchanged_for_narrow_bounds():
    res = _optimize(_FakeSim(lambda p: 1.0 / p), target_value=0.005, min_param=100.0, max_param=1000.0,
                    max_iterations=20)
    assert res.converged is True
    assert res.best_parameters["P"] == pytest.approx(200.0, rel=0.02)


# --------------------------------------------------------------------------- görselleştirme
def test_phase_is_unwrapped_for_high_order_response():
    f = np.logspace(0, 3, 200)
    h = 1.0 / (1.0 + 1j * f / 10.0) ** 3  # faz -270°'ye iner
    wrapped = np.angle(h, deg=True)
    assert wrapped.min() > -180.0001 and np.any(np.diff(wrapped) > 180)  # ham açıda ±180 atlaması var
    phase = WaveformVisualizer._phase_degrees(h)
    assert phase.min() < -250
    assert np.all(np.diff(phase) <= 1e-9)


def test_importing_package_does_not_force_backend_or_import_pyplot():
    code = ("import sys, matplotlib, ltspice_llm; "
            "print(matplotlib.get_backend().lower(), 'matplotlib.pyplot' in sys.modules)")
    env = dict(os.environ, MPLBACKEND="svg")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                         cwd=str(Path(__file__).resolve().parent.parent)).stdout.split()
    assert out == ["svg", "False"]


# ------------------------------------------------------------------------------- ajan/prompt
def test_agent_does_not_read_api_keys_from_environment(monkeypatch, adapter):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-be-read")
    monkeypatch.setenv("GEMINI_API_KEY", "gm-should-not-be-read")
    assert AutonomousCircuitAgent(adapter=adapter).api_key is None


def test_system_prompt_only_mentions_real_tool_names():
    tool_names = {t["name"] for t in get_ltspice_tool_schemas()}
    mentioned = set(re.findall(
        r"`(plot_\w+|get_\w+|list_\w+|modify_\w+|create_\w+|quantize_\w+|diagnose_\w+|optimize_\w+)`",
        CIRCUIT_DESIGNER_SYSTEM_PROMPT,
    ))
    assert mentioned, "prompt araç adı içermiyor"
    assert mentioned <= tool_names, f"Şemada olmayan araçlar: {mentioned - tool_names}"


def _code_spans(prompt):
    return re.findall(r"`([^`\n]+)`", prompt)


def test_english_system_prompt_keeps_every_directive_option_and_tool_of_the_turkish_one():
    """İngilizce istem Türkçenin çevirisidir (kaynak: Türkçe istem): Türkçedeki her direktif ve seçenek kod parçası
    (``.meas ...``, ``method=gear``, ``Rser=0`` ...) İngilizcede birebir geçer, iki istem aynı araçları anar."""
    def technical(prompt):
        return {s for s in _code_spans(prompt) if s.startswith(".") or "=" in s}

    def tools(prompt):
        return {s for s in _code_spans(prompt)
                if re.fullmatch(r"(?:plot|get|list|modify|create|quantize|diagnose|optimize|search)_\w+", s)}

    assert technical(CIRCUIT_DESIGNER_SYSTEM_PROMPT) - technical(CIRCUIT_DESIGNER_SYSTEM_PROMPT_EN) == set()
    assert tools(CIRCUIT_DESIGNER_SYSTEM_PROMPT_EN) == tools(CIRCUIT_DESIGNER_SYSTEM_PROMPT)
    assert tools(CIRCUIT_DESIGNER_SYSTEM_PROMPT) <= {t["name"] for t in get_ltspice_tool_schemas()}


def test_english_system_prompt_differs_from_the_turkish_one_only_in_translated_examples():
    """Kaynak: Türkçe istem. Bütün kod parçaları (komut, seçenek, örnek eleman satırı, sonuç alanı, araç adı) iki
    istemde birebir aynıdır; yalnızca Türkçe sözcük taşıyan iki örnek çevrilir. Bir PULSE örneğindeki Trise/Tfall ya da
    bir sonuç alanı adı çeviride değişirse model yanlış sözdizimi öğrenir (test-reviewer, 2026-10-29)."""
    from collections import Counter

    translated = {"D1 anot katot 1N4148": "D1 anode cathode 1N4148", "{ad}": "{name}"}
    tr = Counter(_code_spans(CIRCUIT_DESIGNER_SYSTEM_PROMPT))
    en = Counter(_code_spans(CIRCUIT_DESIGNER_SYSTEM_PROMPT_EN))
    assert tr - en == Counter(translated.keys())
    assert en - tr == Counter(translated.values())


def test_agent_uses_the_given_system_prompt_and_keeps_it_after_reset(adapter):
    """Belge (AutonomousCircuitAgent): system_prompt geçmişin ilk mesajıdır, reset de onu kullanır; verilmezse Türkçe
    varsayılan istem."""
    expected = [{"role": "system", "content": CIRCUIT_DESIGNER_SYSTEM_PROMPT_EN}]
    agent = AutonomousCircuitAgent(adapter=adapter, system_prompt=CIRCUIT_DESIGNER_SYSTEM_PROMPT_EN)
    assert agent.get_history() == expected
    agent.add_user_message("Design a 10 kHz low-pass filter")
    agent.reset()
    assert agent.get_history() == expected
    assert AutonomousCircuitAgent(adapter=adapter).get_history() == [
        {"role": "system", "content": CIRCUIT_DESIGNER_SYSTEM_PROMPT}]


# LTspice 26.1.1'in gerçek çıktısı: 1 kHz'te H = 1/(1+j) -> |H|=-3.01 dB, faz=-45°, re=0.5, im=-0.5
_AC_COMPLEX_LOG = """\
m_ph: ph(V(out))=(33.0642522527dB,180°) at 1000
m_re: re(V(out))=(-6.02060301904dB,0°) at 1000
m_im: im(V(out))=(-6.02059991328dB,180°) at 1000
m_mag: mag(V(out))=(-3.01030150952dB,0°) at 1000
m_db: db(V(out))=(17.4507484812dB,-113.810492776°) at 1000
m_v: V(out)=(-3.01030150952dB,-45.0000102435°) at 1000
m_par: 2.5=(7.95880017344dB,0°)
max_gain: MAX(mag(V(out)))=(-8.69e-05dB,0°) FROM 100 TO 1000000
max_ph: MAX(ph(V(out)))=(-0.0716dB,180°) FROM 10 TO 100000
"""


@pytest.mark.parametrize("name,expected", [
    ("m_ph", -45.0),        # eskiden 33.06 (|−45| değerinin dB karşılığı) okunuyordu
    ("m_re", 0.5),          # eskiden -6.02
    ("m_im", -0.5),         # eskiden -6.02
    ("m_par", 2.5),         # eskiden 7.96
    ("m_db", -3.0103),      # eskiden 17.45
    ("m_mag", -3.0103),     # mag(): dB olarak kalır (geriye dönük uyumlu)
    ("m_v", -3.0103),       # gerçek karmaşık: dB genlik ...
    ("m_v_deg", -45.0),     # ... ve faz açısı ayrıca verilir
    ("max_gain", -8.69e-05),
    ("max_ph", -0.99179),   # toplama sarmalayıcısı içindeki ph() da gerçel değere çevrilir
])
def test_ac_complex_measurements_are_decoded(name, expected):
    meas = LogParser.parse_text(_AC_COMPLEX_LOG).measurements
    assert meas[name] == pytest.approx(expected, rel=1e-3, abs=1e-6)
    assert meas["m_ph_at"] == 1000
    assert "m_mag_deg" not in meas  # gerçel sonuçlar için gereksiz _deg anahtarı eklenmez


# --------------------------------------------------------------------------- LTspice 26 log başlığı
@pytest.mark.parametrize("options_line", [
    "Options:method=gear plotwinsize=0 threads=12",  # eskiden 'Options': 0.0
    "Options:threads=12",                            # eskiden 'Options': 12.0
])
def test_options_header_line_is_not_a_measurement(options_line):
    log = (
        "LTspice 26.1.1 for Windows\n"
        "Circuit: C:\\ws\\pdn1_gear.net\n"
        f"{options_line}\n"
        "solver = Normal\n"
        "Maximum thread count: 12\n"
        "method = Gear\n"
        "vmin_gps_a: MIN(V(vcc_gps_a_node,gnd_gps_a_node))=2.77796795964 FROM 0 TO 2e-05\n"
    )
    assert LogParser.parse_text(log).measurements == {"vmin_gps_a": pytest.approx(2.77796795964)}


# --------------------------------------------------------------------------- ikili .raw ekseni
class _LazyBinaryRaw:
    """spicelib 1.6 ikili .raw davranışı: eksen ilk iz okunana kadar yüklenmez, get_axis hata verir."""

    has_axis = True

    def __init__(self):
        self.loaded = False

    def get_trace_names(self):
        return ["time", "V(out)"]

    def get_trace(self, name):
        self.loaded = True
        return name

    def get_axis(self, step=0):
        if not self.loaded:
            raise RuntimeError("This RAW file does not have an axis.")
        return [0.0, 1e-9, 2e-9]


def test_raw_axis_values_loads_lazy_binary_axis_on_first_call():
    assert list(raw_axis_values(_LazyBinaryRaw())) == [0.0, 1e-9, 2e-9]


def test_open_raw_without_path_explains_why():
    with pytest.raises(ValueError, match="circuit_name"):
        open_raw(None)


# --------------------------------------------------------------------------- aynı adla art arda koşum
class _WritingPopen:
    """Anında biten sahte LTspice: netlistin yanına .log ve içeriği netliste özgü bir .raw yazar."""

    def __init__(self, cmd, **kwargs):
        net = Path(cmd[-1])
        net.with_suffix(".log").write_text("vout: MAX(V(out))=1.0 FROM 0 TO 0.001\n", encoding="utf-8")
        net.with_suffix(".raw").write_bytes(net.read_bytes())
        self.pid, self.returncode = 999, None

    def communicate(self, timeout=None):
        self.returncode = 0
        return "", ""


NET_A = "* devre A\nV1 out 0 1\n.tran 1m\n.end\n"
NET_B = "* devre B\nV1 out 0 2\n.tran 1m\n.end\n"


def _writing_adapter(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter_mod.subprocess, "Popen", _WritingPopen)
    return LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path / "ws", cpu_guard=False)


def test_rerun_with_same_name_supersedes_previous_result(tmp_path, monkeypatch):
    """Eskiden ilk sonucun raw_path'i, üzerine yazılmış dosyadan sessizce B devresinin verisini okuyordu."""
    ad = _writing_adapter(tmp_path, monkeypatch)
    first = ad.run_netlist(NET_A, treat_as_text=True, summarize_raw=False)
    assert first.raw_path.read_bytes().startswith(b"* devre A")
    second = ad.run_netlist(NET_B, treat_as_text=True, summarize_raw=False)

    assert first.superseded and first.raw_path is None and first.log_path is None
    assert any("üzerine yazıldı" in w for w in first.warnings)
    assert first.measurements == {"vout": 1.0}  # zaten okunmuş sonuçlar korunur
    assert not second.superseded and second.raw_path.read_bytes().startswith(b"* devre B")
    with pytest.raises(ValueError, match="circuit_name"):
        open_raw(first.raw_path)


def test_supersede_spans_adapters_sharing_a_workspace(tmp_path, monkeypatch):
    first = _writing_adapter(tmp_path, monkeypatch).run_netlist(NET_A, treat_as_text=True, summarize_raw=False)
    _writing_adapter(tmp_path, monkeypatch).run_netlist(NET_B, treat_as_text=True, summarize_raw=False)
    assert first.superseded and first.raw_path is None


def test_different_names_do_not_supersede_each_other(tmp_path, monkeypatch):
    ad = _writing_adapter(tmp_path, monkeypatch)
    a = ad.run_netlist(NET_A, circuit_name="a", treat_as_text=True, summarize_raw=False)
    b = ad.run_netlist(NET_B, circuit_name="b", treat_as_text=True, summarize_raw=False)
    assert not a.superseded and a.raw_path.read_bytes().startswith(b"* devre A")
    assert not b.superseded and b.raw_path.read_bytes().startswith(b"* devre B")
