"""
Parametre/eleman çözümleme testleri (LTspice gerektirmez).

`modify_parameters` eskiden bileşen adı verildiğinde etkisiz bir `.param` ekleyip `SUCCESS` dönüyordu.
"""

import sys
from pathlib import Path

import pytest

from ltspice_llm import LTSpiceAdapter, SimulationResult, apply_parameters, handle_tool_call
from ltspice_llm.adapter import list_element_names, list_param_names

NET = """* divider
V1 in 0 10V
R1 in out 1k
R2 out 0 {R2_V}
C1 out 0 1u ic=0
L1 out x 1m ; bobin
R3 x 0 R=2k
.param R2_V=1k
.op
.end
"""

PLAIN = "* t\nV1 in 0 DC 5\nI1 a 0 1m\nR1 in out 1k tc=1e-3\nR2 out 0 1k\n.op\n.end\n"


def test_existing_param_is_updated_first():
    app = apply_parameters(NET, {"R2_V": "3k"})
    assert ".param R2_V=3k" in app.content
    assert app.unresolved == []
    assert app.changes["R2_V"] == ".param R2_V=3k"


def test_element_value_is_replaced_when_no_param_exists():
    app = apply_parameters(PLAIN, {"R2": "3k"})
    assert "R2 out 0 3k\n" in app.content
    assert ".param R2" not in app.content  # etkisiz .param eklenmez
    assert app.unresolved == []
    assert app.changes["R2"] == "eleman R2: 1k -> 3k"


@pytest.mark.parametrize("name,line_before,line_after", [
    ("R1", "R1 in out 1k tc=1e-3", "R1 in out 4.7k tc=1e-3"),     # sonraki parametreler korunur
    ("C1", "C1 out 0 1u ic=0", "C1 out 0 4.7k ic=0"),
    ("L1", "L1 out x 1m ; bobin", "L1 out x 4.7k ; bobin"),        # yorum korunur
    ("R3", "R3 x 0 R=2k", "R3 x 0 R=4.7k"),                        # R=değer sözdizimi
    ("r1", "R1 in out 1k tc=1e-3", "R1 in out 4.7k tc=1e-3"),      # SPICE büyük/küçük harf duyarsızdır
])
def test_element_value_variants(name, line_before, line_after):
    src = NET if name in ("C1", "L1", "R3") else PLAIN
    app = apply_parameters(src, {name: "4.7k"})
    assert line_after in app.content.splitlines()
    assert app.unresolved == []


def test_dc_source_value_is_replaced_but_pulse_source_is_not():
    app = apply_parameters(PLAIN, {"V1": "12"})
    assert "V1 in 0 DC 12" in app.content
    assert apply_parameters(PLAIN, {"I1": "2m"}).content.count("I1 a 0 2m") == 1

    pulse = "* t\nV1 in 0 PULSE(0 5 0 1n 1n 1m 2m)\nR1 in 0 1k\n.tran 1m\n.end\n"
    app = apply_parameters(pulse, {"V1": "12"})
    assert app.unresolved == ["V1"]  # PULSE bir sayı değil: eleman düzenlenmez
    assert "PULSE(0 5 0 1n 1n 1m 2m)" in app.content


def test_referenced_but_undefined_param_is_injected_not_unresolved():
    net = "* t\nV1 in 0 10\nR1 in out {RX}\nR2 out 0 1k\n.op\n.end\n"
    app = apply_parameters(net, {"RX": "2k"})
    assert app.unresolved == []
    assert ".param RX=2k" in app.content
    assert app.content.strip().endswith(".end")


def test_unknown_name_is_reported_as_unresolved():
    app = apply_parameters(PLAIN, {"R99": "1k"})
    assert app.unresolved == ["R99"]
    assert "eklendi ama kullanılmıyor" in app.changes["R99"]


def test_element_changes_do_not_touch_comments_directives_or_title():
    net = "R2 title line\n* R2 out 0 9k\n.param R2=5\nR2 out 0 1k\n.end\n"
    # .param tanımı öncelikli
    assert "* R2 out 0 9k" in apply_parameters(net, {"R2": "7k"}).content
    net2 = "R2 title line\n* R2 out 0 9k\nR2 out 0 1k\n.end\n"
    out = apply_parameters(net2, {"R2": "7k"}).content.splitlines()
    assert out[0] == "R2 title line"     # ilk satır başlıktır
    assert out[1] == "* R2 out 0 9k"     # yorum satırı değişmez
    assert out[2] == "R2 out 0 7k"


@pytest.mark.parametrize("bad", ["1k\n.include x", "1k;x", ""])
def test_element_and_param_values_are_validated(bad):
    with pytest.raises(ValueError):
        apply_parameters(PLAIN, {"R2": bad})


# ---------------------------------------------------------------------------- şema (.asc)
# LTspice'ın kaydettiği bir örnekle-tut şemasının iki kaynak bloğu, satır sonları (CRLF) dahil aynen. Dosya okunmaz:
# mutant kopyası .asc dosyalarını kopyalamaz (scripts/mutants.py). V1'in Value satırı InstName'den önce.
SAMPLE_AND_HOLD_SOURCES = (
    "Version 4\r\nSHEET 1 1316 680\r\n"
    "SYMBOL voltage -160 272 R0\r\nWINDOW 3 23 92 Left 2\r\nWINDOW 123 0 0 Left 2\r\nWINDOW 39 0 0 Left 2\r\n"
    "SYMATTR Value SINE(0 1 300)\r\nSYMATTR InstName V1\r\n"
    "SYMBOL voltage 64 384 R0\r\nWINDOW 0 18 18 Left 2\r\nWINDOW 3 23 97 Left 2\r\nSYMATTR InstName V2\r\n"
    "SYMATTR Value PULSE(0 1 0 1u 1u 50u 100u)\r\n"
    "TEXT -208 584 Left 2 !.tran 0 20m 0 1u\r\n"
)
# LTspice 26.1.1 örneği Applications/LT1963-1.5.asc'deki C1 bloğunun biçimi: Value ve SpiceLine, InstName'den önce.
VALUE_FIRST = (
    "Version 4\nSHEET 1 880 680\n"
    "SYMBOL cap 384 64 R0\nWINDOW 3 40 33 Left 2\nWINDOW 39 24 59 Left 2\n"
    "SYMATTR Value 10µ\nSYMATTR SpiceLine Rser=50m\nSYMATTR InstName C1\n"
    "SYMBOL res 544 48 R0\nSYMATTR InstName Rload\nSYMATTR Value 1K\n"
    "TEXT 376 216 Left 2 !.tran 1\n"
)


def test_schematic_source_with_a_pulse_value_is_not_overwritten_by_a_number():
    """Netlistteki kural (yukarıdaki PULSE testi) şemada da geçerli: LTspice netlisterı 'SYMATTR Value'yu olduğu gibi
    değer alanına yazar (ref26_sch_connected .json -> .net: 'SYMATTR Value 5' -> 'V1 N001 0 5'). Eski hata: şemada
    yalnızca ilk sözcük değişiyordu; 'PULSE(0 1 0 1u 1u 50u 100u)' '3.3 1 0 1u 1u 50u 100u)' oldu ve değişiklik
    başarılı diye raporlandı (ölçüldü, 2026-10-11)."""
    app = apply_parameters(SAMPLE_AND_HOLD_SOURCES, {"V2": "3.3", "V1": "2"})
    assert app.unresolved == ["V2", "V1"]
    assert app.content.count("SYMATTR Value PULSE(0 1 0 1u 1u 50u 100u)") == 1
    assert app.content.count("SYMATTR Value SINE(0 1 300)") == 1
    assert "eklendi ama kullanılmıyor" in app.changes["V2"]


def test_schematic_value_line_is_found_before_the_instance_name():
    """Eski hata: arama 'InstName' satırının hemen ardında 'Value' bekliyordu; C1 bulunamadı, etkisiz .param eklendi."""
    app = apply_parameters(VALUE_FIRST, {"C1": "22µ", "Rload": "2K"})
    assert app.unresolved == []
    assert app.changes == {"C1": "eleman C1: 10µ -> 22µ", "Rload": "eleman Rload: 1K -> 2K"}
    lines = app.content.splitlines()
    assert lines.count("SYMATTR Value 22µ") == 1 and lines.count("SYMATTR Value 2K") == 1
    assert lines.count("SYMATTR SpiceLine Rser=50m") == 1 and ".param" not in app.content


def test_list_names():
    assert list_param_names(NET) == ["R2_V"]
    assert list_param_names("* t\n.param A=1 B=2\n+ C=3\n.end\n") == ["A", "B", "C"]
    assert list_param_names("* t\n.param Rx 1k\n.end\n") == ["Rx"]
    assert list_element_names(PLAIN) == ["V1", "I1", "R1", "R2"]


# ----------------------------------------------------------------- araç ve adaptör katmanı
class _RecordingAdapter:
    """Gerçek LTspice çalıştırmayan, run_netlist çağrısını kaydeden sahte adaptör."""

    def __init__(self, workspace: Path):
        self.workspace_dir = workspace
        self.calls = []

    def run_netlist(self, netlist_content_or_path, circuit_name, parameters=None, **kw):
        self.calls.append((netlist_content_or_path, circuit_name, parameters))
        return SimulationResult(success=True, netlist_path=self.workspace_dir / "x.net")


def test_modify_parameters_tool_rejects_unknown_name_without_simulating(tmp_path):
    (tmp_path / "c.net").write_text(PLAIN, encoding="utf-8")
    ad = _RecordingAdapter(tmp_path)
    res = handle_tool_call("modify_parameters", {"circuit_name": "c", "parameters": {"R99": "1k"}}, adapter=ad)
    assert res["status"] == "ERROR"
    assert "R99" in res["message"]
    assert res["available_elements"] == ["V1", "I1", "R1", "R2"]
    assert ad.calls == []  # simüle edilmedi
    assert (tmp_path / "c.net").read_text(encoding="utf-8") == PLAIN  # dosya değişmedi


def test_modify_parameters_tool_accepts_element_names(tmp_path):
    (tmp_path / "c.net").write_text(PLAIN, encoding="utf-8")
    ad = _RecordingAdapter(tmp_path)
    res = handle_tool_call("modify_parameters", {"circuit_name": "c", "parameters": {"R2": "3k"}}, adapter=ad)
    assert res["status"] == "SUCCESS"
    assert ad.calls[0][2] == {"R2": "3k"}


def test_run_netlist_reports_parameter_changes_and_unused_params(tmp_path):
    ad = LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path / "ws")
    res = ad.run_netlist(PLAIN, circuit_name="p", parameters={"R2": "3k", "R99": "1k"},
                         treat_as_text=True, timeout=5)
    assert res.parameter_changes["R2"] == "eleman R2: 1k -> 3k"
    assert any("'R99'" in w and "etkisiz" in w for w in res.warnings)
    assert res.to_llm_dict()["parameter_changes"]["R2"].startswith("eleman R2")
    assert "R2 out 0 3k" in (tmp_path / "ws" / "p.net").read_text(encoding="utf-8")
