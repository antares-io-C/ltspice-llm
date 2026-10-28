import codecs
from pathlib import Path
from unittest.mock import patch

import pytest

from ltspice_llm import (
    AscSchematic,
    LTSpiceAdapter,
    apply_parameters,
    is_asc_content,
)
from ltspice_llm.schematic import decode_asc_bytes, encode_asc_text, read_asc_file, transform_pin, write_asc_file


def test_transform_pin():
    # res has (16, 16) and (16, 96)
    p1 = transform_pin(100, 200, 16, 16, "R0")
    assert p1 == (116, 216)

    p1_90 = transform_pin(100, 200, 16, 16, "R90")
    # x - py, y + px -> 100 - 16, 200 + 16 -> (84, 216)
    assert p1_90 == (84, 216)

    p1_180 = transform_pin(100, 200, 16, 16, "R180")
    assert p1_180 == (84, 184)


def test_asc_schematic_render_and_save(tmp_path: Path):
    sch = AscSchematic(1000, 600)
    sch.add_symbol("voltage", 100, 200, "R0", inst_name="V1", value="SINE(0 1 1k)")
    sch.add_symbol("res", 300, 200, "R0", inst_name="R1", value="1k")
    sch.add_ground(100, 300)
    sch.add_net_label(300, 200, "OUT")
    sch.add_wire(100, 200, 300, 200)
    sch.add_directive(100, 400, ".tran 10m")
    sch.add_comment(100, 450, "Test Comment")

    rendered = sch.render()
    assert "Version 4" in rendered
    assert "SHEET 1 1000 600" in rendered
    assert "SYMBOL voltage 100 200 R0" in rendered
    assert "SYMATTR InstName V1" in rendered
    assert "WIRE 100 200 300 200" in rendered
    assert "FLAG 100 300 0" in rendered
    assert "FLAG 300 200 OUT" in rendered
    assert "TEXT 100 400 Left 2 !.tran 10m" in rendered
    assert "TEXT 100 450 Left 2 ;Test Comment" in rendered

    out_file = tmp_path / "test_circuit.asc"
    saved = sch.save(out_file)
    assert saved.is_file()
    assert is_asc_content(saved.read_text(encoding="utf-8"))


def test_asc_parameter_updates():
    asc_code = """Version 4
SHEET 1 1000 600
SYMBOL res 100 100 R0
SYMATTR InstName R1
SYMATTR Value 10k
TEXT 100 300 Left 2 !.param R_bias=5k C_load=10n
"""
    # Test updating .param
    res = apply_parameters(asc_code, {"R_bias": "12k", "C_load": "22n"})
    assert "R_bias=12k" in res.content
    assert "C_load=22n" in res.content

    # Test updating element value directly
    res_elem = apply_parameters(asc_code, {"R1": "47k"})
    assert "SYMATTR Value 47k" in res_elem.content

    # Test adding new parameter
    res_new = apply_parameters(asc_code, {"V_supply": "15V"})
    assert "!.param V_supply=15V" in res_new.content


def test_adapter_prepare_netlist_with_asc(tmp_path: Path):
    asc_code = """Version 4
SHEET 1 800 600
SYMBOL res 100 100 R0
SYMATTR InstName R1
SYMATTR Value 1k
"""
    asc_file = tmp_path / "sample.asc"
    asc_file.write_text(asc_code, encoding="utf-8")

    with patch.object(LTSpiceAdapter, "_locate_executable", return_value=Path("mock_ltspice.exe")):
        adapter = LTSpiceAdapter(workspace_dir=tmp_path)
        target_path, _ = adapter._prepare_netlist(asc_file, "sample_circuit")
        assert target_path.suffix == ".asc"
        assert is_asc_content(target_path.read_text(encoding="utf-8"))


def test_adapter_open_in_gui(tmp_path: Path):
    asc_file = tmp_path / "open_test.asc"
    asc_file.write_text("Version 4\nSHEET 1 100 100\n", encoding="utf-8")

    with patch.object(LTSpiceAdapter, "_locate_executable", return_value=Path("mock_ltspice.exe")), \
         patch("subprocess.Popen") as mock_popen:
        adapter = LTSpiceAdapter(workspace_dir=tmp_path)
        adapter.open_in_gui(asc_file)
        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert str(asc_file.resolve()) in args


# --------------------------------------------------------------------- .asc kodlaması (GEMINI.md "LTspice referans kuralları")
@pytest.mark.parametrize("text, encoding", [
    ("SYMATTR Value 1µ\r\n", "cp1252"),           # µ cp1252'de var (0xB5)
    ("TEXT 0 0 Left 2 ;ş\r\n", "utf-16-le"),      # ş cp1252'de yok: LTspice'ın da yazdığı BOM'suz UTF-16LE
])
def test_read_asc_file_returns_the_text_and_the_encoding_ltspice_reads(tmp_path, text, encoding):
    path = write_asc_file(tmp_path / "x.asc", text)
    assert read_asc_file(path) == (text, encoding)


def test_decode_utf16_be_bom_and_latin1_fallback_keep_every_byte():
    assert decode_asc_bytes(codecs.BOM_UTF16_BE + "Version 4".encode("utf-16-be")) == ("Version 4", "utf-16-be")
    data = b"Version 4\n\x81\x8d\x8f\x90\x9d"          # cp1252'de tanımsız beş bayt
    text, encoding = decode_asc_bytes(data)
    assert encoding == "latin-1" and encode_asc_text(text, encoding) == data


def test_cp1252_schematic_with_a_non_cp1252_character_switches_to_utf16():
    assert encode_asc_text("ş", "cp1252") == "ş".encode("utf-16-le")


def test_connect_draws_one_wire_on_a_shared_axis_and_an_l_otherwise():
    """Belge: dik açılı bağlantı; eksen farklıysa L şeklinde iki tel, önce yatay sonra dikey."""
    sch = AscSchematic()
    lines = lambda wires: [(w.x1, w.y1, w.x2, w.y2) for w in wires]  # noqa: E731
    assert lines(sch.connect((0, 0), (0, 96))) == [(0, 0, 0, 96)]
    assert lines(sch.connect((0, 0), (64, 0))) == [(0, 0, 64, 0)]
    assert lines(sch.connect((0, 0), (64, 96))) == [(0, 0, 64, 0), (64, 0, 64, 96)]


def test_from_dict_and_to_dict(tmp_path: Path):
    data = {
        "sheet_width": 1400,
        "sheet_height": 900,
        "symbols": [
            {"name": "voltage", "x": 100, "y": 200, "inst_name": "V1", "value": "SINE(0 1 1k)"},
            {"name": "res", "x": 300, "y": 200, "inst_name": "R1", "value": "1k"},
            {"name": "cap", "x": 400, "y": 200, "inst_name": "C1", "value": "100n"},
        ],
        "connections": [
            {"from_inst": "V1", "from_pin": "+", "to_inst": "R1", "to_pin": "1"},
            {"from": "R1:2", "to": "C1:1"},
        ],
        "grounds": [[100, 296], [416, 264]],
        "net_labels": [{"x": 316, "y": 296, "name": "VMID"}],
        "directives": [".tran 10m"],
        "comments": ["RC Lowpass Circuit"],
    }
    sch = AscSchematic.from_dict(data)
    assert len(sch.symbols) == 3
    assert len(sch.flags) == 3  # 2 grounds + 1 label
    assert any(t.is_directive and ".tran 10m" in t.text for t in sch.texts)
    assert any(not t.is_directive and "RC Lowpass" in t.text for t in sch.texts)

    d = sch.to_dict()
    assert d["sheet_width"] == 1400
    assert len(d["symbols"]) == 3
    assert len(d["wires"]) >= 2

    # Save to file and verify content
    out_file = tmp_path / "rc_gen.asc"
    saved = sch.save(out_file)
    assert saved.is_file()
    text = saved.read_text(encoding="utf-8", errors="ignore")
    assert "SHEET 1 1400 900" in text
    assert "SYMATTR InstName V1" in text
    assert "SYMATTR InstName R1" in text
    assert "FLAG 316 296 VMID" in text


def test_from_dict_rejects_a_connection_to_an_unknown_instance():
    """Eski hata: from_inst/to_inst biçiminde olmayan instance'a bağlantı sessizce atlanıyordu ("from"/"to" biçimi
    aynı durumda ValueError veriyordu); kopuk pin ancak LTspice netlisterında görünürdü."""
    data = {
        "symbols": [{"name": "res", "x": 0, "y": 0, "inst_name": "R1", "value": "1k"}],
        "connections": [{"from_inst": "R1", "from_pin": "2", "to_inst": "VX", "to_pin": "1"}],
    }
    with pytest.raises(ValueError, match="VX"):
        AscSchematic.from_dict(data)

