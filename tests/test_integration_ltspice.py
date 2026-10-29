"""
Gerçek LTspice ile uçtan uca doğrulama testleri (LTspice kurulu değilse atlanır).

Sentetik girdilerle geçen ama gerçek LTspice çıktısında yanlış sonuç veren hataların (.step istatistikleri,
FAIL'ed ölçümler, etkisiz parametre değişikliği, steplenmiş raw okuma) bir daha gizlenmemesini sağlar.
"""

import base64
import re
import shutil

import pytest

from ltspice_llm import (
    AscSchematic,
    CircuitOptimizer,
    LTSpiceAdapter,
    WaveformVisualizer,
    find_symbol,
    handle_tool_call,
)
from ltspice_llm.parser import LogParser, RawParser, open_raw, raw_axis_values

RC_STEP = """* rc step
.param RV=1k
V1 in 0 PULSE(0 5 0 1n 1n 1m 2m)
R1 in out {RV}
C1 out 0 1u
.tran 3m
.step param RV list 1k 2k 3k
.meas tran v_max MAX V(out)
.end
"""
RC_SINGLE = RC_STEP.replace(".step param RV list 1k 2k 3k\n", "")

AC_STEP = """* rc ac step
.param RV=1k
V1 in 0 AC 1 0
R1 in out {RV}
C1 out 0 1u
.ac dec 10 10 100k
.step param RV list 1k 2k 3k
.meas ac f3 WHEN mag(V(out))=0.7071
.end
"""
AC_SINGLE = AC_STEP.replace(".step param RV list 1k 2k 3k\n", "")

DIVIDER = """* comp value
V1 in 0 10V
R1 in out 1k
R2 out 0 1k
.op
.meas vout FIND V(out)
.end
"""


@pytest.fixture(scope="module")
def adapter(tmp_path_factory):
    try:
        return LTSpiceAdapter(workspace_dir=tmp_path_factory.mktemp("ltspice_ws"))
    except FileNotFoundError:
        pytest.skip("LTspice bu makinede kurulu değil")


# --------------------------------------------------------------------------- .step
def test_step_statistics_equal_the_values_of_individual_runs(adapter):
    stepped = adapter.run_netlist(RC_STEP, circuit_name="it_step", treat_as_text=True)
    assert stepped.success, stepped.errors
    expected = []
    for rv in ("1k", "2k", "3k"):
        single = adapter.run_netlist(RC_SINGLE, circuit_name="it_single", parameters={"RV": rv}, treat_as_text=True)
        assert single.success, single.errors
        expected.append(single.measurements["v_max"])

    rows = stepped.stepped_measurements["v_max"]
    assert [r["v_max"] for r in rows] == pytest.approx(expected, rel=1e-4)
    assert [r["rv"] for r in rows] == [1000.0, 2000.0, 3000.0]  # adım parametreleri satırlarda
    stats = stepped.step_statistics["v_max"]
    assert stats["min"] == pytest.approx(min(expected), rel=1e-4)
    assert stats["max"] == pytest.approx(max(expected), rel=1e-4)
    assert stats["mean"] == pytest.approx(sum(expected) / 3, rel=1e-4)
    assert stats["std"] > 0.3 and stats["count"] == 3
    assert stepped.step_parameters == [{"rv": 1000.0}, {"rv": 2000.0}, {"rv": 3000.0}]
    assert "step_parameters" in stepped.to_llm_dict()


def test_step_run_uses_binary_raw_and_summarizes_each_step(adapter):
    res = adapter.run_netlist(RC_STEP, circuit_name="it_step_raw", treat_as_text=True)
    assert res.success, res.errors
    assert not any("özetlenemedi" in w for w in res.warnings)  # eskiden: list assignment index out of range
    keys = set(res.raw_summary)
    assert {f"V(out) [step {k}]" for k in (1, 2, 3)} <= keys
    assert not any(k.startswith("time") for k in keys)  # eksen sinyal olarak özetlenmez (ikili raw dahil)
    maxima = [res.raw_summary[f"V(out) [step {k}]"].max_val for k in (1, 2, 3)]
    assert maxima == pytest.approx([r["v_max"] for r in res.stepped_measurements["v_max"]], rel=1e-3)
    assert RawParser.analysis_kind(res.raw_path) == "tran"


def test_fresh_binary_raw_axis_and_no_fake_options_measurement(adapter):
    """Eskiden ilk raw_axis_values çağrısı 'This RAW file does not have an axis' veriyordu ve log başlığındaki
    'Options:threads=N' satırı 'Options' adlı sahte bir ölçüm olarak sonuca giriyordu."""
    res = adapter.run_netlist(RC_SINGLE, circuit_name="it_axis", treat_as_text=True, summarize_raw=False)
    assert res.success, res.errors
    assert set(res.measurements) == {"v_max"}
    t = raw_axis_values(open_raw(res.raw_path))  # hiçbir iz okunmadan ilk çağrı
    assert t[0] == 0.0 and t[-1] == pytest.approx(3e-3)


def test_sequential_runs_with_same_name_supersede_instead_of_sharing_raw(adapter):
    first = adapter.run_netlist(RC_SINGLE, circuit_name="it_same", treat_as_text=True)
    v_max = first.measurements["v_max"]
    second = adapter.run_netlist(AC_SINGLE, circuit_name="it_same", treat_as_text=True)
    assert second.success, second.errors
    assert first.superseded and first.raw_path is None
    assert first.measurements["v_max"] == v_max  # tran sonucunun ölçümleri korunur
    assert RawParser.analysis_kind(second.raw_path) == "ac"


def test_stepped_raw_is_plotted_and_bode_overlays_steps(adapter, tmp_path):
    tran = adapter.run_netlist(RC_STEP, circuit_name="it_step_plot", treat_as_text=True)
    png = WaveformVisualizer.plot_traces(tran.raw_path, ["V(out)"], output_png_path=tmp_path / "t.png")
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    ac = adapter.run_netlist(AC_STEP, circuit_name="it_step_ac", treat_as_text=True)
    assert ac.success, ac.errors
    assert RawParser.analysis_kind(ac.raw_path) == "ac"
    fig = WaveformVisualizer._build_bode_figure(ac.raw_path, "V(out)", "V(in)")
    assert [len(a.get_lines()) for a in fig.axes] == [3, 3]
    b64 = WaveformVisualizer.plot_bode_to_base64(ac.raw_path)
    assert base64.b64decode(b64)[:8] == b"\x89PNG\r\n\x1a\n"


def test_ascii_and_binary_raw_give_identical_summaries(tmp_path):
    """raw_format='auto' yalnızca .step için binary seçer; iki biçimin aynı veriyi verdiği doğrulanır."""
    try:
        ascii_ad = LTSpiceAdapter(workspace_dir=tmp_path / "a", raw_format="ascii")
    except FileNotFoundError:
        pytest.skip("LTspice bu makinede kurulu değil")
    bin_ad = LTSpiceAdapter(workspace_dir=tmp_path / "b", raw_format="binary")
    for name, text in (("ac", AC_SINGLE), ("tran", RC_SINGLE)):
        a = ascii_ad.run_netlist(text, circuit_name=name, treat_as_text=True).raw_summary
        b = bin_ad.run_netlist(text, circuit_name=name, treat_as_text=True).raw_summary
        assert set(a) == set(b) and a  # ikili raw'da eksen sinyal gibi görünmemeli
        for key in a:
            for field in ("min_val", "max_val", "mean_val", "rms_val", "sample_count"):
                assert getattr(a[key], field) == pytest.approx(getattr(b[key], field), rel=1e-6, abs=1e-9), (name, key)


# ------------------------------------------------------------------------ FAIL'ed
def test_failed_measurement_is_visible_to_the_llm(adapter):
    net = RC_SINGLE.replace(".meas tran v_max MAX V(out)",
                            ".meas tran v_never WHEN V(out)=100\n.meas tran v_max MAX V(out)")
    res = adapter.run_netlist(net, circuit_name="it_failed", treat_as_text=True)
    assert res.success is True and "v_never" not in res.measurements and "v_max" in res.measurements
    assert res.failed_measurements == ["v_never"]
    assert any("v_never" in w and "FAIL'ed" in w for w in res.warnings)
    llm = res.to_llm_dict()
    assert llm["failed_measurements"] == ["v_never"] and llm["warnings"]


# --------------------------------------------------------- parametre/eleman değişikliği
def test_modify_parameters_changes_component_value_and_rejects_typos(adapter):
    created = handle_tool_call("create_or_replace_circuit", {"circuit_name": "it_div", "netlist_code": DIVIDER},
                               adapter=adapter)
    assert created["measurements"]["vout"] == pytest.approx(5.0)

    changed = handle_tool_call("modify_parameters", {"circuit_name": "it_div", "parameters": {"R2": "3k"}},
                               adapter=adapter)
    assert changed["status"] == "SUCCESS"
    assert changed["measurements"]["vout"] == pytest.approx(7.5)  # eskiden 5.0 kalıyordu (etkisiz .param)
    assert changed["parameter_changes"] == {"R2": "eleman R2: 1k -> 3k"}

    typo = handle_tool_call("modify_parameters", {"circuit_name": "it_div", "parameters": {"R22": "3k"}},
                            adapter=adapter)
    assert typo["status"] == "ERROR" and "R22" in typo["message"]
    assert "R2 out 0 3k" in handle_tool_call("get_circuit_netlist", {"circuit_name": "it_div"},
                                             adapter=adapter)["netlist"]


# ------------------------------------------------------------------------- çizim araçları
def test_bode_tool_refuses_transient_circuit_and_transient_tool_refuses_ac(adapter):
    handle_tool_call("create_or_replace_circuit", {"circuit_name": "it_tran", "netlist_code": RC_SINGLE}, adapter=adapter)
    bode = handle_tool_call("plot_bode_diagram", {"circuit_name": "it_tran"}, adapter=adapter)
    assert bode["status"] == "ERROR" and "tran" in bode["message"]

    handle_tool_call("create_or_replace_circuit", {"circuit_name": "it_ac", "netlist_code": AC_SINGLE}, adapter=adapter)
    wave = handle_tool_call("plot_transient_waveforms", {"circuit_name": "it_ac"}, adapter=adapter)
    assert wave["status"] == "ERROR" and "plot_bode_diagram" in wave["message"]


def test_plot_tools_return_base64_images(adapter):
    handle_tool_call("create_or_replace_circuit", {"circuit_name": "it_b64t", "netlist_code": RC_SINGLE}, adapter=adapter)
    wave = handle_tool_call("plot_transient_waveforms",
                            {"circuit_name": "it_b64t", "signals": ["V(out)"], "include_image_base64": True},
                            adapter=adapter)
    assert wave["status"] == "SUCCESS" and base64.b64decode(wave["image_base64"])[:4] == b"\x89PNG"

    handle_tool_call("create_or_replace_circuit", {"circuit_name": "it_b64b", "netlist_code": AC_SINGLE}, adapter=adapter)
    bode = handle_tool_call("plot_bode_diagram", {"circuit_name": "it_b64b", "include_image_base64": True},
                            adapter=adapter)
    assert bode["status"] == "SUCCESS" and base64.b64decode(bode["image_base64"])[:4] == b"\x89PNG"


# ------------------------------------------------------------------------ optimizer
def test_optimizer_tunes_an_element_value_and_leaves_no_intermediate_files(adapter):
    net = """* opt
V1 in 0 PULSE(0 5 0 1n 1n 2m 4m)
R1 in out 2k
C1 out 0 1u
.tran 2m
.meas tran v_target FIND V(out) WHEN time=1m
.end
"""
    before = {p.name for p in adapter.workspace_dir.iterdir()}
    res = CircuitOptimizer(adapter=adapter).optimize_single_parameter(
        base_netlist=net, circuit_name="it_opt", param_name="R1", target_meas_name="v_target",
        target_value=3.0, min_param=200.0, max_param=3000.0, tolerance=0.01, max_iterations=12,
    )
    assert res.converged is True and res.message == ""
    assert abs(res.best_value - 3.0) <= 0.05
    after = {p.name for p in adapter.workspace_dir.iterdir()}
    assert not [n for n in after - before if "_opt_" in n]  # ara dosyalar temizlendi


def test_optimizer_explains_when_the_parameter_is_not_in_the_netlist(adapter):
    net = RC_SINGLE.replace(".meas tran v_max MAX V(out)", ".meas tran v_target FIND V(out) WHEN time=1m")
    res = CircuitOptimizer(adapter=adapter).optimize_single_parameter(
        base_netlist=net, circuit_name="it_opt_bad", param_name="NOPE", target_meas_name="v_target",
        target_value=3.0, min_param=1.0, max_param=10.0,
    )
    assert res.converged is False
    assert "duyarsız" in res.message and "bulunamadı" in res.message


def test_optimize_parameter_tool_end_to_end(adapter):
    net = RC_SINGLE.replace(".meas tran v_max MAX V(out)", ".meas tran v_target FIND V(out) WHEN time=1m")
    handle_tool_call("create_or_replace_circuit", {"circuit_name": "it_opt_tool", "netlist_code": net}, adapter=adapter)
    res = handle_tool_call(
        "optimize_parameter",
        {"circuit_name": "it_opt_tool", "param_name": "RV", "target_meas_name": "v_target",
         "target_value": 3.0, "min_value": 200, "max_value": 3000, "max_iterations": 12},
        adapter=adapter,
    )
    assert res["status"] == "SUCCESS" and res["converged"] is True
    assert abs(res["best_value"] - 3.0) <= 0.05 and "modify_parameters" in res["hint"]


# ------------------------------------------------------------------------ Monte Carlo
# LTspice yardımı (param--user-definedparameters.htm): "mc(x,y) A random number between x*(1+y) and x*(1-y) with
# uniform distribution". 10 V bölücüde R1 = R2 = 10k ±%5 -> elle: V(out) = 10*R2/(R1+R2) en az 10*9.5/(9.5+10.5)
# = 4.75 V, en çok 10*10.5/(10.5+9.5) = 5.25 V.
MC_DIVIDER = """* mc divider
V1 in 0 10
R1 in out 10k
R2 out 0 10k
.tran 1m
.meas tran v_out AVG V(out)
.end
"""


def _monte_carlo(adapter, name, netlist, **args):
    handle_tool_call("create_or_replace_circuit", {"circuit_name": name, "netlist_code": netlist}, adapter=adapter)
    return handle_tool_call("simulate_monte_carlo", {"circuit_name": name, **args}, adapter=adapter)


def test_monte_carlo_on_element_tolerances_stays_within_hand_bounds(adapter):
    """Eski hata: ``.param R1_nom`` başlık satırına yazılıyordu (ADI SPICE-SYNTAX-REFERENCE "First line: Always
    treated as a comment"); LTspice 26.1.1 'No such parameter defined' verdi."""
    res = _monte_carlo(adapter, "it_mc_elem", MC_DIVIDER, tolerances={"R1": 0.05, "R2": 0.05}, runs=6)
    assert res["status"] == "SUCCESS", res.get("errors")
    stats = res["step_statistics"]["v_out"]
    assert stats["count"] == 6
    assert 4.75 <= stats["min"] and stats["max"] <= 5.25
    assert stats["std"] > 0  # mc() uygulandı: adımların hepsi nominal 5 V değil


def test_monte_carlo_yield_against_hand_bounds(adapter):
    """Eski hata: ``stepped_measurements`` sözlüğü liste sanılıyordu ("'str' object has no attribute 'get'")."""
    tol = {"R1": 0.05, "R2": 0.05}
    inside = _monte_carlo(adapter, "it_mc_yield", MC_DIVIDER, tolerances=tol, runs=5,
                          target_specs={"v_out": {"min": 4.75, "max": 5.25}})
    assert inside["status"] == "SUCCESS", inside.get("message") or inside.get("errors")
    assert inside["yield_analysis"]["v_out"]["passed_runs"] == 5
    above = _monte_carlo(adapter, "it_mc_yield", MC_DIVIDER, tolerances=tol, runs=5,
                         target_specs={"v_out": {"min": 5.25}})
    assert above["yield_analysis"]["v_out"]["passed_runs"] == 0


# ------------------------------------------------------------------------ PCB iz parazitiği
TRACE_FLAT = """* trace flat load
V1 in 0 PULSE(0 1 0 1n 1n 50n 100n)
Rs in a 50
Rl out 0 50
.tran 0 200n 0 0.1n
.meas tran vout_max MAX V(out)
.end
"""
TRACE_SUBCKT = TRACE_FLAT.replace("Rl out 0 50\n", "X1 out 0 LOAD\n.subckt LOAD p n\nRl p n 50\n.ends LOAD\n")


def test_injected_trace_gives_the_same_result_with_a_subcircuit_load(adapter):
    """Aynı devre: yük düz ``Rl`` ya da ``LOAD`` alt devresi. Eski hata: iz ``.ends LOAD``'un önüne, alt devrenin içine
    eklendi; ana devrede a-out arası açık kaldı (ölçüldü: 0.505 V yerine 0 V)."""
    from ltspice_llm.parasitics import calculate_trace_parasitics, inject_trace_parasitics_into_netlist

    trace = calculate_trace_parasitics(length_mm=100.0)
    flat = adapter.run_netlist(inject_trace_parasitics_into_netlist(TRACE_FLAT, "a", "out", trace),
                               circuit_name="it_trace_flat", treat_as_text=True)
    sub = adapter.run_netlist(inject_trace_parasitics_into_netlist(TRACE_SUBCKT, "a", "out", trace),
                              circuit_name="it_trace_subckt", treat_as_text=True)
    assert flat.success and sub.success, (flat.errors, sub.errors)
    assert flat.measurements["vout_max"] > 0.4  # elle: 1 V, 50 Ω kaynak + 50 Ω yük -> ~0.5 V
    assert sub.measurements["vout_max"] == pytest.approx(flat.measurements["vout_max"], rel=1e-6)


def test_injected_trace_has_exactly_its_dc_resistance_in_ltspice(adapter):
    """Elle: 1 V -> iz -> 1 Ohm yük, DC'de V(out) = 1 / (1 + R_dc). Eski hata: iz alt devresinin bobinlerine Rser=0
    yazılmıyordu; LTspice her birine 1 mΩ ekleyip (ADI CIRCUIT-ELEMENTS-REFERENCE) R_dc'yi ~%2 büyütüyordu."""
    from ltspice_llm.parasitics import calculate_trace_parasitics, inject_trace_parasitics_into_netlist

    trace = calculate_trace_parasitics(length_mm=50.0, width_mm=0.25)
    net = inject_trace_parasitics_into_netlist("* trace dc\nV1 in 0 1\nRl out 0 1\n.op\n.meas op vout FIND V(out)\n"
                                               ".end\n", "in", "out", trace)
    res = adapter.run_netlist(net, circuit_name="it_trace_dc", treat_as_text=True)
    assert res.success, res.errors
    assert res.measurements["vout"] == pytest.approx(1.0 / (1.0 + trace.r_dc), rel=1e-6)


# ------------------------------------------------------------------------ takma anı (AN-88 seri RLC)
def _hotplug_net(v, l_h, c_f, r, cb=None, rb=None, periods=3):
    """1 ns kenarlı basamak -> R -> L -> C (+ C_b + ESR). ``Rser=0``: LTspice bobin varsayılanı 1 mΩ (ADI
    CIRCUIT-ELEMENTS-REFERENCE: "1m (for SMPS; set Rser=0 to disable)"); yazılmazsa ideal modelden %0.1-0.4 kayar."""
    import math

    period = 2 * math.pi * math.sqrt(l_h * (c_f + (cb or 0.0)))
    lines = ["* hotplug", f"V1 src 0 PULSE(0 {v} 0 1n 1n 1 2)", f"R1 src a {r:.6g}" if r else "R1 src a 1u",
             f"L1 a c {l_h:.6g} Rser=0", f"C1 c 0 {c_f:.6g}"]
    if cb:
        lines += [f"C2 c nb {cb:.6g}", f"R2 nb 0 {rb:.6g}"]
    lines += [f".tran 0 {periods * period:.6g} 0 {period / 2000:.6g}", ".meas tran vpk MAX V(c)", ".end"]
    return "\n".join(lines) + "\n"


def test_hotplug_peaks_predicted_by_the_tool_match_ltspice(adapter):
    """analyze_hotplug_transient'in kapalı form tepesi ve hotplug_step_peak'in sönümlü tepesi LTspice .meas'iyle aynı
    olmalı (examples/15, LTspice 26.1.1: en büyük fark %0.00011; zaman adımı periyodun 1/2000'i). Öneri iki ölçekte
    aynı tepe / V_in'i verir."""
    from ltspice_llm.parasitics import analyze_hotplug_transient, hotplug_step_peak

    cases = []
    closed = analyze_hotplug_transient(5.0, 1e-6, 0.5e-6, 0.15)
    cases.append(("kapalı form", closed.v_peak, _hotplug_net(5.0, 0.5e-6, 1e-6, 0.15)))
    for v, l_h, c_f in ((24.0, 1e-6, 22e-6), (12.0, 2.4e-6, 10e-6)):
        rec = analyze_hotplug_transient(v, c_f, l_h)
        cases.append((f"öneri {v:g} V", rec.recommended_damped_peak,
                      _hotplug_net(v, l_h, c_f, 0.0, rec.recommended_bulk_c, rec.recommended_bulk_esr, periods=20)))
    an88 = hotplug_step_peak(24.0, 1e-6, 22e-6, 0.0, 47e-6, 0.44)  # AN-88 s.4 örneği, ideal model
    cases.append(("AN-88 47 µF / 0.44 Ω", an88, _hotplug_net(24.0, 1e-6, 22e-6, 0.0, 47e-6, 0.44, periods=20)))
    # Seri R ile sönüm kolu birlikte (R = 0.3·Z0, Z0 = 1 Ω): transfer fonksiyonundaki R·C·Rb·Cb terimi
    both = hotplug_step_peak(1.0, 1e-6, 1e-6, 0.3, 4e-6, 0.8)
    cases.append(("seri R + sönüm kolu", both, _hotplug_net(1.0, 1e-6, 1e-6, 0.3, 4e-6, 0.8, periods=20)))
    for i, (label, predicted, net) in enumerate(cases):
        res = adapter.run_netlist(net, circuit_name=f"it_hotplug_{i}", treat_as_text=True)
        assert res.success, (label, res.errors)
        assert res.measurements["vpk"] == pytest.approx(predicted, rel=1e-5), label
    assert cases[1][1] / 24.0 == pytest.approx(cases[2][1] / 12.0, rel=1e-6)


def test_critical_series_resistance_removes_the_overshoot_in_ltspice(adapter):
    """zeta = 1 için toplam seri direnç 2·Z0: adım yanıtı aşımsızdır (tepe = V_in). 12 V, 2.4 µH, 10 µF."""
    from ltspice_llm.parasitics import analyze_hotplug_transient

    a = analyze_hotplug_transient(12.0, 10e-6, 2.4e-6, 0.0)
    res = adapter.run_netlist(_hotplug_net(12.0, 2.4e-6, 10e-6, a.recommended_damping_r, periods=20),
                              circuit_name="it_hotplug_crit", treat_as_text=True)
    assert res.success, res.errors
    assert res.measurements["vpk"] == pytest.approx(12.0, rel=1e-5)


def test_cable_subcircuit_runs_in_ltspice(adapter):
    """CableParasitics.to_spice_subckt LTspice'ta çalışır ve basamakta tepe kapalı forma eşittir (L ve R iki iletkene
    bölünmüş, toplamları aynı). Eski hata: bobinlere Rser=0 yazılmadığından LTspice her birine 1 mΩ ekliyordu
    (ölçüldü: %0.1-0.5 kayma)."""
    from ltspice_llm.parasitics import analyze_hotplug_transient, get_cable_parasitics

    cable = get_cable_parasitics(1.0, resistance_ohm=0.1)
    net = ("* cable\nV1 src 0 PULSE(0 12 0 1n 1n 1 2)\nX1 src 0 c 0g CABLE\nC1 c 0g 10u\n"
           + cable.to_spice_subckt() + ".tran 0 30u 0 10n\n.meas tran vpk MAX V(c,0g)\n.end\n")
    res = adapter.run_netlist(net, circuit_name="it_cable_sub", treat_as_text=True)
    assert res.success, res.errors
    expected = analyze_hotplug_transient(12.0, 10e-6, cable.l_cable, cable.r_cable).v_peak
    assert res.measurements["vpk"] == pytest.approx(expected, rel=5e-4)


def test_solver_thread_limit_reaches_ltspice(adapter, tmp_path):
    """docs/KILAVUZ.md "Kilitlenme koruması" 1: kiralanan çekirdek LTspice'a ``.options threads`` olarak verilir. GEMINI.md:
    bir seçeneğin uygulandığını logdan doğrula (LTspice 26.1.1 logu ``Maximum thread count: N`` yazar).

    Sınır 1: koruma onu kırpamaz, her makinede tek cevap; 2 tek çekirdekli makinede 1'e kırpılıyordu (test-reviewer,
    2026-10-11). Birden çok çekirdekte LTspice'ın varsayılanından (bütün çekirdekler) ayrışır. Satır tam eşleşir:
    ``"...: 1" in log`` 12 çekirdekli makinenin varsayılanı ``...: 12``'de de doğru olurdu."""
    limited = LTSpiceAdapter(executable_path=adapter.executable_path, workspace_dir=tmp_path, solver_threads=1)
    res = limited.run_netlist(RC_SINGLE, circuit_name="threads1", treat_as_text=True)
    assert res.success and res.solver_threads == 1
    log = LogParser.read_text(res.log_path)
    assert re.findall(r"^Maximum thread count: (\d+)\s*$", log, re.MULTILINE) == ["1"]


# --------------------------------------------------------------------- tasarımcının dosyası kendi klasöründe koşar
# 10 V, üstte 1k, altta 2k: V(out) = 10 * 2k / (1k + 2k) = 6.6667 V (elle hesap). Eski hata (LTspice 26.1.1, 2026-10-29):
# dosya yolu verilen devre çalışma alanına kopyalanıp orada koşuluyordu; göreli .include "File not found" verdi,
# şemanın yanındaki sembol (R2) netlistten uyarısız düştü ve koşum V(out) = 10 V ile başarılı göründü.
LOAD_LIB = ".subckt LOAD p n\nRl p n 2k\n.ends LOAD\n"
DIVIDER_INCLUDE = ("* relative include\n.include load.lib\nV1 in 0 10\nR1 in out 1k\nX1 out 0 LOAD\n.op\n"
                   ".meas vout FIND V(out)\n.end\n")


def _divider_project(folder):
    (folder / "load.lib").write_text(LOAD_LIB, encoding="ascii")
    src = folder / "divider.net"
    src.write_text(DIVIDER_INCLUDE, encoding="ascii")
    return src


def test_netlist_file_finds_the_model_file_next_to_it(adapter, tmp_path):
    res = adapter.run_netlist(_divider_project(tmp_path), circuit_name="it_rel_include")
    assert res.success, res.errors
    assert res.measurements["vout"] == pytest.approx(20 / 3, rel=1e-6)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["divider.net", "load.lib"]  # LTspice'ın yazdığı her şey taşındı


def test_sweep_of_a_netlist_file_finds_the_model_file_next_to_it(adapter, tmp_path):
    """Üstteki direnç taranır: R1 = 1k -> 10 * 2k / 3k = 6.6667 V, R1 = 2k -> 10 * 2k / 4k = 5 V (elle hesap). Analiz
    .tran: .op + .step'te LTspice .meas'i adım başına değil adım ekseninde bir kez yazar (26.1.1 logu:
    ``vout: V(out)=6.66666650772 at 1000``)."""
    src = _divider_project(tmp_path)
    src.write_text(DIVIDER_INCLUDE.replace(".op\n.meas vout FIND V(out)", ".tran 1u\n.meas tran vout MAX V(out)"),
                   encoding="ascii")
    res = adapter.run_sweep(src, "R1", ["1k", "2k"], circuit_name="it_rel_include_sweep")
    assert res.success, res.errors
    assert [row["vout"] for row in res.stepped_measurements["vout"]] == pytest.approx([20 / 3, 5.0], rel=1e-6)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["divider.net", "load.lib"]


def test_schematic_file_finds_the_symbol_next_to_it(adapter, tmp_path):
    shutil.copyfile(find_symbol("res"), tmp_path / "myres.asy")  # tasarımcının sembolü: yalnız şemanın klasöründe
    sch = AscSchematic(symbol_dirs=[tmp_path])
    v1 = sch.add_symbol("voltage", 0, 0, inst_name="V1", value="10")
    r1 = sch.add_symbol("res", 160, 0, inst_name="R1", value="1k")
    r2 = sch.add_symbol("myres", 320, 0, inst_name="R2", value="2k")
    for sym, pin, net in ((v1, "+", "in"), (r1, "1", "in"), (r1, "2", "out"), (r2, "1", "out")):
        sch.add_net_label(*sch.get_pin(sym, pin), net)
    for sym, pin in ((v1, "-"), (r2, "2")):
        sch.add_ground(*sch.get_pin(sym, pin))
    sch.add_directive(0, 200, ".op")
    sch.add_directive(0, 240, ".meas vout FIND V(out)")
    res = adapter.run_netlist(sch.save(tmp_path / "divider.asc"), circuit_name="it_local_symbol")
    assert res.success, res.errors
    assert res.measurements["vout"] == pytest.approx(20 / 3, rel=1e-6)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["divider.asc", "myres.asy"]
