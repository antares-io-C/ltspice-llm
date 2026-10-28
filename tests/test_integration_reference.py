"""
ADI LTspice AI Reference'tan kazanılan davranışların gerçek LTspice ile uçtan uca testleri
(LTspice kurulu değilse atlanır). Ölçümler: docs/LESSONS_LEARNED_LTSPICE_REFERENCE.md.
"""

import json
import re
import sys
from pathlib import Path

import pytest

from ltspice_llm import AscSchematic, LTSpiceAdapter, handle_tool_call, ltspice_data_dir, read_measure_db
from ltspice_llm.diagnostics import SPICEDiagnostics
from ltspice_llm.schematic import SYMBOL_PIN_DEFS

DATA = Path(__file__).parent / "data"

RC_ASC = (
    "Version 4\r\nSHEET 1 880 680\r\nWIRE 112 96 32 96\r\nWIRE 208 96 192 96\r\nWIRE 224 96 208 96\r\n"
    "WIRE 32 112 32 96\r\nWIRE 224 112 224 96\r\nFLAG 32 192 0\r\nFLAG 224 176 0\r\nFLAG 208 96 out\r\n"
    "SYMBOL voltage 32 96 R0\r\nSYMATTR InstName V1\r\nSYMATTR Value PULSE(0 1 0 1n 1n 1 2)\r\n"
    "SYMBOL cap 208 112 R0\r\nSYMATTR InstName C1\r\nSYMATTR Value 1µ\r\n"
    "SYMBOL res 208 80 R90\r\nWINDOW 0 0 56 VBottom 2\r\nWINDOW 3 32 56 VTop 2\r\nSYMATTR InstName R1\r\n"
    "SYMATTR Value 1k\r\nTEXT 0 240 Left 2 !.tran 0 8m 0 5u\r\nTEXT 0 272 Left 2 !.meas tran tau WHEN V(out)=0.632\r\n"
)

AC_RC = """* rc ac
V1 in 0 AC 1
R1 in out 1k
C1 out 0 159n
.ac dec 100 1 1Meg
.options meascplxfmt=polar
.meas AC g0 FIND mag(V(out)) AT 1
.meas AC fc WHEN mag(V(out))=g0/sqrt(2)
.meas AC p1k FIND ph(V(out)) AT 1k
.end
"""


@pytest.fixture(scope="module")
def adapter(tmp_path_factory):
    try:
        return LTSpiceAdapter(workspace_dir=tmp_path_factory.mktemp("ref_ws"))
    except FileNotFoundError:
        pytest.skip("LTspice bu makinede kurulu değil")


def test_ltspice_version_is_reported(adapter):
    assert re.match(r"^\d+\.\d+", adapter.ltspice_version() or "")


def test_dc_operating_point_failure_is_not_reported_as_success(adapter):
    """Eskiden success=True, errors=[] dönüyordu ('Simulation Failed' hata sayılmıyordu)."""
    res = adapter.run_netlist("* x\nB1 a 0 V=if(V(a)>0.5,0,1)\nR1 a 0 1k\n.op\n.end\n",
                              circuit_name="dcop_fail", treat_as_text=True)
    assert not res.success
    assert res.diagnostics["issue_type"] == "DC_OP_CONVERGENCE_FAILURE"


def test_zero_rise_pulse_succeeds_with_a_warning(adapter):
    """Eskiden 'Limiting rise time' satırı hata sayılıyor, koşum FAILED görünüyordu."""
    res = adapter.run_netlist(
        "* z\nV1 in 0 PULSE(0 1 0 0 0 1u 2u)\nR1 in out 1k\nC1 out 0 100p\n.tran 10u\n.meas tran vmax MAX V(out)\n.end\n",
        circuit_name="zero_rise", treat_as_text=True,
    )
    assert res.success and res.errors == []
    assert res.measurements["vmax"] == pytest.approx(1.0, abs=1e-3)
    assert any("Ton/10" in w for w in res.warnings)


@pytest.mark.parametrize("encoding", ["cp1252", "utf-16-le"])
def test_asc_with_micro_sign_simulates_the_right_value(adapter, tmp_path, encoding):
    """cp1252 '1µ' eskiden '1' (1 F) okunuyordu; UTF-16 şema hiç çalışmıyordu. tau = R*C = 1 ms."""
    src = tmp_path / f"rc_{encoding}.asc"
    src.write_bytes(RC_ASC.encode(encoding))
    res = adapter.run_netlist(src, circuit_name=f"rc_{encoding.replace('-', '')}")
    assert res.success, res.errors
    assert res.measurements["tau"] == pytest.approx(1e-3, rel=1e-3)


def test_parameters_reach_a_schematic_whose_value_line_comes_first(adapter, tmp_path):
    """LTspice'ın kendi şemalarında Value InstName'den önce de gelir (LTspice örneği LT1963-1.5.asc, C1). Eski hata: C1 bulunamadı,
    etkisiz bir .param eklendi ve τ 1 ms kaldı; PULSE değerli V1'e verilen sayı değeri bozuyordu. Elle: τ = 1k·2µ = 2 ms
    (V(out) = 1 - e^(-t/τ), 0.632'ye τ·0.9997'de varır); V1 PULSE kalır, ad çözülemedi diye bildirilir."""
    value_first = RC_ASC.replace("SYMATTR InstName C1\r\nSYMATTR Value 1µ\r\n", "SYMATTR Value 1µ\r\nSYMATTR InstName C1\r\n")
    assert value_first != RC_ASC
    src = tmp_path / "rc_value_first.asc"
    src.write_bytes(value_first.encode("cp1252"))
    res = adapter.run_netlist(src, circuit_name="rc_value_first", parameters={"C1": "2µ", "V1": "5"})
    assert res.success, res.errors
    assert res.parameter_changes["C1"] == "eleman C1: 1µ -> 2µ"
    assert res.measurements["tau"] == pytest.approx(2e-3, rel=1e-3)
    assert sum("'V1'" in w and "etkisiz" in w for w in res.warnings) == 1


def test_when_with_expression_threshold_matches_ltspice_database(adapter):
    res = adapter.run_netlist(AC_RC, circuit_name="ac_when", treat_as_text=True)
    assert res.success and res.db_path is not None
    db = {r["name"]: r["value"] for r in read_measure_db(res.db_path)}
    assert res.measurements["fc"] == pytest.approx(db["fc"], rel=1e-9)
    assert res.measurements["fc"] == pytest.approx(1001.0, rel=1e-3)  # 1/(2*pi*1k*159n)
    assert res.measurements["p1k"] == pytest.approx(db["p1k"], rel=1e-9)  # polar: işaret korunur (-44.97)
    assert res.measurements["p1k"] < 0


def test_missing_standard_model_is_fixed_by_the_suggested_library(adapter):
    net = "* d\nV1 a 0 1\nR1 a b 1k\nD1 b 0 1N4148\n.op\n.meas op vb FIND V(b)\n.end\n"
    res = adapter.run_netlist(net, circuit_name="missing_model", treat_as_text=True)
    assert not res.success
    assert res.diagnostics["issue_type"] == "MISSING_MODEL_OR_SUBCKT"
    assert ".lib standard.dio" in res.diagnostics["suggested_netlist_patch"]

    report = SPICEDiagnostics.diagnose("", errors_list=res.errors)
    fixed = adapter.run_netlist(SPICEDiagnostics.apply_diagnostic_patch(net, report),
                                circuit_name="missing_model_fixed", treat_as_text=True)
    assert fixed.success, fixed.errors
    assert 0.5 < fixed.measurements["vb"] < 0.6  # gerçek 1N4148 modeli (0.548 V ölçüldü)


def test_four_reports_thd(adapter):
    res = adapter.run_netlist(
        "* four\nV1 in 0 SINE(0 1 1k)\nB1 out 0 V=tanh(2*V(in))\nR1 out 0 1k\n.tran 0 5m 0 1u\n.four 1k 5 -1 V(out)\n.end\n",
        circuit_name="four", treat_as_text=True,
    )
    assert res.success
    assert res.to_llm_dict()["fourier"][0]["thd_percent"] == pytest.approx(17.34, abs=0.05)


def _rc_schematic(open_r2: bool) -> AscSchematic:
    s = AscSchematic()
    v = s.add_symbol("voltage", 32, 96, "R0", "V1", "PULSE(0 1 0 1n 1n 1m 2m)")
    r = s.add_symbol("res", 208, 80, "R90", "R1", "1k")
    c = s.add_symbol("cap", 208, 112, "R0", "C1", "1µ")
    i = s.add_symbol("current", 400, 96, "R0", "I1", "1m")
    s.connect(s.get_pin(v, "+"), s.get_pin(r, "B"))
    s.connect(s.get_pin(r, "A"), s.get_pin(c, "1"))
    s.add_ground(*s.get_pin(v, "-"))
    s.add_ground(*s.get_pin(c, "2"))
    s.add_net_label(*s.get_pin(c, "1"), "out")
    s.connect(s.get_pin(i, "+"), s.get_pin(c, "1"))
    s.add_ground(*s.get_pin(i, "-"))
    if open_r2:
        r2 = s.add_symbol("res", 600, 96, "R0", "R2", "1k")
        s.connect(s.get_pin(r2, "A"), s.get_pin(c, "1"))
    s.add_directive(0, 300, ".tran 5m")
    return s


def test_generated_current_source_is_connected_and_open_pins_are_found(adapter, tmp_path):
    """Eski pin tablosuyla akım kaynağı 'I1 NC_01 NC_02' olarak tamamen kopuktu."""
    good = adapter.inspect_schematic(_rc_schematic(open_r2=False).save(tmp_path / "good.asc"))
    assert good["status"] == "SUCCESS", good["netlist"]
    assert re.search(r"^I1 out 0 1m$", good["netlist"], re.MULTILINE)

    bad = adapter.inspect_schematic(_rc_schematic(open_r2=True).render(), treat_as_text=True)
    assert bad["status"] == "WARNING"
    assert [p["element"] for p in bad["unconnected_pins"]] == ["R2"]


def test_run_sweep_accepts_asc_schematic(adapter, tmp_path):
    src = tmp_path / "rc_sweep.asc"
    src.write_bytes(RC_ASC.encode("cp1252"))
    res = adapter.run_sweep(src, parameter="R1", values=["1k", "2k"], circuit_name="rc_sweep")
    assert res.success, res.errors
    taus = [row["tau"] for row in res.stepped_measurements["tau"]]
    assert taus == [pytest.approx(1e-3, rel=1e-3), pytest.approx(2e-3, rel=1e-3)]
    assert not list(tmp_path.glob("_ltspice_llm_*"))  # geçici kopya temizlendi


# --------------------------------------------------------------------------- pin tablosu = LTspice sembolleri
@pytest.fixture(scope="module")
def symbol_dir():
    data = ltspice_data_dir()
    sym = data / "lib" / "sym" if data else None
    if sym is None or not sym.is_dir():
        pytest.skip("LTspice sembol kütüphanesi (lib/sym) bu makinede yok")
    return sym


def _asy_pins(path):
    """``PIN x y ...`` ve ardından gelen ``PINATTR PinName`` / ``SpiceOrder``: {ad: (x, y)}, netlist (SpiceOrder)
    sırasıyla; SpiceOrder yoksa dosya sırası."""
    pins, order = [], {}
    for line in path.read_text(encoding="cp1252", errors="replace").splitlines():
        parts = line.split()
        if parts[:1] == ["PIN"]:
            pins.append([None, (int(parts[1]), int(parts[2]))])
        elif parts[:2] == ["PINATTR", "PinName"] and pins:
            pins[-1][0] = parts[2]
        elif parts[:2] == ["PINATTR", "SpiceOrder"] and pins:
            order[len(pins) - 1] = int(parts[2])
    ranked = sorted(range(len(pins)), key=lambda i: order.get(i, i + 1))
    return {pins[i][0]: pins[i][1] for i in ranked if pins[i][0] is not None}


@pytest.fixture(scope="module")
def find_asy(symbol_dir):
    """Tablo adını .asy dosyasına çevirir: önce yoluyla (OPAMPS\\OPAMP), yoksa adıyla alt klasörlerde; yoksa None."""
    index = {p.relative_to(symbol_dir).with_suffix("").as_posix().lower(): p for p in symbol_dir.rglob("*.asy")}
    by_base = {}
    for key, path in index.items():
        by_base.setdefault(key.rsplit("/", 1)[-1], path)

    def find(name):
        key = name.replace("\\", "/").lower()
        return index.get(key) or by_base.get(key.rsplit("/", 1)[-1])

    return find


def test_every_symbol_in_the_pin_table_exists_in_ltspice_with_the_same_pins(find_asy):
    """SYMBOL_PIN_DEFS LTspice'ın lib/sym/*.asy PIN satırlarından yazılır (schematic.py). Eski hata: tabloda
    'inductor' vardı ama LTspice 26.1.1'de inductor.asy yok (ind.asy, ind2.asy); şema netliste çevrilemedi."""
    problems = []
    for name, table in SYMBOL_PIN_DEFS.items():
        path = find_asy(name)
        if path is None:
            problems.append(f"{name}: .asy yok")
            continue
        pins = _asy_pins(path)
        coords = set(pins.values())
        problems += [f"{name}.{pin}: {xy} {path.name}'da pin değil" for pin, xy in table.items() if xy not in coords]
        named = {k.lower(): v for k, v in pins.items()}
        problems += [f"{name}.{pin}: {xy} != {named[pin.lower()]}" for pin, xy in table.items()
                     if pin.lower() in named and named[pin.lower()] != xy]
    assert problems == []


_FIRST_PIN = {"1", "a", "+", "anode"}
_SECOND_PIN = {"2", "b", "k", "-", "cathode"}


def test_two_pin_symbol_aliases_point_at_the_right_ltspice_pin(find_asy):
    """İki pinli sembolde takma adlar netlist sırasına (``PINATTR SpiceOrder``) gider: 1/A/+/anode birinci,
    2/B/K/-/cathode ikinci pin (diode.asy: PIN 16 0 '+' SpiceOrder 1, PIN 16 64 '-' SpiceOrder 2; res.asy: 'A' 1,
    'B' 2). Yalnızca "koordinat bir pin mi" denetimi anode/cathode yer değiştirmesini görmez; sınıflanmamış takma ad
    da sorun sayılır, ters koordinatla sessizce geçmesin (test-reviewer, 2026-10-05)."""
    checked, problems = 0, []
    for name, table in SYMBOL_PIN_DEFS.items():
        path = find_asy(name)
        pins = list(_asy_pins(path).values()) if path else []
        if len(pins) != 2:
            continue
        for alias, xy in table.items():
            expected = pins[0] if alias.lower() in _FIRST_PIN else pins[1] if alias.lower() in _SECOND_PIN else None
            if expected is None:
                problems.append(f"{name}.{alias}: sınıflanmamış takma ad")
                continue
            checked += 1
            if xy != expected:
                problems.append(f"{name}.{alias}: {xy} != {expected}")
    assert checked > 0 and problems == []


# --------------------------------------------------------------------------- create_or_replace_schematic aracı
def _v_r_schematic(ground_r1):
    """V1 (5 V) + -> R1 pin 1; R1 pin 2 (316, 296) topraklı ya da boşta (examples/14 ``schematic`` girdisi)."""
    name = "connected" if ground_r1 else "open_pin"
    return json.loads((Path(__file__).parent / "data" / f"ref26_sch_{name}.json").read_text(encoding="utf-8"))


def test_schematic_tool_simulates_a_fully_connected_schematic(adapter):
    """LTspice netlisteri bağlantıyı doğrularsa şema simüle edilir. Elle: |I(R1)| = 5 V / 1k = 5 mA."""
    res = handle_tool_call(
        "create_or_replace_schematic", {"circuit_name": "it_sch_ok", "schematic_data": _v_r_schematic(True)},
        adapter=adapter,
    )
    assert res["status"] == "SUCCESS", res
    assert res["inspection"]["unconnected_pins"] == []
    assert abs(res["measurements"]["ir"]) == pytest.approx(5e-3, rel=1e-6)


def test_schematic_tool_refuses_to_simulate_an_open_pin(adapter):
    """Eski hata: kopuk pinli şema SUCCESS ile kaydedilip simüle ediliyordu. ADI: yapay zekânın konumla ürettiği
    şemalar hizasız pine açıktır, netlister bunları NC_xx diye adlandırır (adapter.inspect_schematic)."""
    res = handle_tool_call(
        "create_or_replace_schematic", {"circuit_name": "it_sch_open", "schematic_data": _v_r_schematic(False)},
        adapter=adapter,
    )
    assert res["status"] == "WARNING"
    assert [p["element"] for p in res["inspection"]["unconnected_pins"]] == ["R1"]
    assert not (adapter.workspace_dir / "it_sch_open.raw").exists()  # simülasyon yapılmadı


# --------------------------------------------------------------------------- çökme (Gemini incelemesi, 2026-10-06)
def test_recursive_subcircuit_crash_is_reported_as_a_crash(adapter):
    """LTspice 26.1.1 kendini çağıran alt devrede "Infinite subckt recursion." yazmak yerine yığın taşmasıyla çöküyor
    (Windows: 0xC00000FD; examples/14 ``crash``, examples/16). Eski hata: çıkış kodu kayboluyor, sonuç
    "Simülasyon dalga formu (.raw) üretilemedi. Devre sözdizimi veya bağlantılarını kontrol edin." diyordu."""
    net = (DATA / "ref26_crash_recursive.net").read_text(encoding="utf-8")
    res = adapter.run_netlist(net, circuit_name="it_crash_recursive", treat_as_text=True)
    assert not res.success
    assert any("LTspice çöktü" in e for e in res.errors)
    if sys.platform == "win32":
        assert any("0xC00000FD" in e for e in res.errors)
    assert not any("(.raw) üretilemedi" in e for e in res.errors)
