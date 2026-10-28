"""
Standart E-Serisi Komponent Kuantalama Testleri
"""

import re

import pytest
from ltspice_llm.quantizer import (
    SERIES_MAP,
    parse_spice_value,
    format_spice_value,
    snap_to_e_series,
    quantize_circuit_parameters,
)
from ltspice_llm.tools import handle_tool_call


def test_parse_spice_value():
    assert parse_spice_value(100) == 100.0
    assert parse_spice_value(2.5e-3) == 0.0025
    assert parse_spice_value("10k") == 10000.0
    assert parse_spice_value("15.915k") == 15915.0
    assert parse_spice_value("1.414n") == pytest.approx(1.414e-9)
    assert parse_spice_value("100u") == pytest.approx(1e-4)
    assert parse_spice_value("10MEG") == 1e7
    assert parse_spice_value("10meg") == 1e7
    assert parse_spice_value("22p") == pytest.approx(22e-12)
    assert parse_spice_value("4.7m") == pytest.approx(0.0047)


# Beklenen değerler LTspice 26.1.1 ölçümüdür (R1 = 1 V / I(R1); examples/12_ltspice_reference_checks.py) ve ADI
# SPICE-SYNTAX-REFERENCE "Engineering Multipliers" / "Important Rules" ile tutarlıdır.
@pytest.mark.parametrize("text,expected", [
    (".5k", 500.0),         # baştaki rakam isteğe bağlı
    ("-.5k", -500.0),
    ("1.k", 1000.0),
    ("5.", 5.0),
    (".47µ", 0.47e-6),      # .asc'de (cp1252) yaygın yazım; ölçüm: C = .47µ, R = 1k -> tau = 0.470 ms
    ("4k7", 4700.0),        # gömülü ondalık: "6K34 means 6.34K"
    ("+4k7", 4700.0),
    ("-4k7", -3300.0),      # LTspice kesri işaretten bağımsız ekler: (-4 + 0.7)k
    ("-1.5k3", -1200.0),    # (-1.5 + 0.3)k
    ("6K34", 6340.0),
    ("2Meg2", 2.2e6),
    ("1M5", 1.5e-3),        # M = mili
    ("1u5", 1.5e-6),
    ("2f2", 2.2e-15),
    ("4R7", 4.7),           # R ondalık ayırıcı olarak
    ("1.5k3", 1800.0),      # LTspice: 1.5 + 0.3 -> 1.8k
    ("4V7", 4.0),           # bilinmeyen harften sonrası yok sayılır
    ("4x7", 4.0),
    ("10Volts", 10.0),      # "Unrecognized letters after a number are ignored"
    ("100Hz", 100.0),
    ("1a", 1.0),            # atto yok
    ("10Fa", 10e-15),       # F = femto
    ("1mil", 25.4e-6),
    ("1e-3k", 1.0),
    ("47E-1", 4.7),
], ids=lambda v: v if isinstance(v, str) else None)
def test_parse_spice_value_matches_ltspice(text, expected):
    assert parse_spice_value(text) == pytest.approx(expected, rel=1e-12)


@pytest.mark.parametrize("text", ["4k7ohm", "k", ".", "1..2", "12 3", "1.5.3"])
def test_parse_spice_value_rejects_what_ltspice_rejects(text):
    # LTspice 26.1.1 (examples/12): "4k7ohm", "1..2", "12 3", "1.5.3" -> "Unknown parameter";
    # "k", "." -> "Undefined model"
    with pytest.raises(ValueError):
        parse_spice_value(text)


# IEC 60063 resmî değerleri (Wikipedia "E series of preferred numbers", 2026-10-02): E24 listesi aynen;
# E96 = round(10^(i/96), 2), istisnasız ("all other series (E3, E6, E12, E24, E192) have ... values that do not
# match"); E12 ve E6, E24'ün her 2. ve 4. değeridir.
IEC_E24 = (1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2.0, 2.2, 2.4, 2.7, 3.0,
           3.3, 3.6, 3.9, 4.3, 4.7, 5.1, 5.6, 6.2, 6.8, 7.5, 8.2, 9.1)


def test_series_tables_match_iec_60063():
    assert SERIES_MAP["E24"] == IEC_E24
    assert SERIES_MAP["E12"] == IEC_E24[::2]
    assert SERIES_MAP["E6"] == IEC_E24[::4]
    assert SERIES_MAP["E96"] == tuple(round(10 ** (i / 96), 2) for i in range(96))


def test_format_spice_value():
    assert format_spice_value(10000.0) == "10k"
    assert format_spice_value(16000.0) == "16k"
    assert format_spice_value(1.5e-9) == "1.5n"
    assert format_spice_value(0.001) == "1m"
    assert format_spice_value(0.0) == "0"


@pytest.mark.parametrize("value,expected", [
    (999600.0, "1MEG"),     # eskiden "1e+03k"
    (999.6, "1k"),          # eskiden "1e+03"
    (0.9996, "1"),          # eskiden "1e+03m"
    (9.996e-7, "1u"),       # eskiden "1e+03n"
    (999499.0, "999k"),     # sınırın hemen altı dekadda kalır
    (-999600.0, "-1MEG"),
    (15915.0, "15.9k"),
    (4.7e-12, "4.7p"),
    (2.2e15, "2.2e+15"),    # SI önek aralığı dışı: bilimsel gösterim (SPICE okur)
    (3e-18, "3e-18"),
])
def test_format_spice_value_decade_boundaries(value, expected):
    assert format_spice_value(value) == expected


@pytest.mark.parametrize("value", [999600.0, 999.6, 0.9996, 1e15, 3e-18, 123456.789, 1.0000001e6])
def test_formatted_value_never_has_invalid_spice_exponent_suffix(value):
    """Çıktı SPICE'ta geçerli olmalı: bir SI önekiyle birleşmiş bilimsel gösterim ('1e+03k') olmamalı."""
    text = format_spice_value(value)
    assert re.fullmatch(r"-?\d+(?:\.\d+)?(?:k|MEG|G|T|m|u|n|p|f)?|-?\d+(?:\.\d+)?e[+-]\d+", text), text
    assert parse_spice_value(text.replace("MEG", "meg")) == pytest.approx(value, rel=5e-3)


def test_snap_to_e_series_e24():
    # 15.915kΩ -> E24'te 16kΩ olmalı (%0.53 hata)
    res = snap_to_e_series("15.915k", series="E24", mode="nearest")
    assert res["standard_value"] == 16000.0
    assert res["formatted"] == "16k"
    assert res["series"] == "E24"
    assert abs(res["error_pct"]) < 1.0


def test_snap_to_e_series_modes():
    # 15.5k için E12 adayları: 15k ve 18k
    res_floor = snap_to_e_series("15.5k", series="E12", mode="floor")
    assert res_floor["standard_value"] == 15000.0
    assert res_floor["formatted"] == "15k"

    res_ceil = snap_to_e_series("15.5k", series="E12", mode="ceil")
    assert res_ceil["standard_value"] == 18000.0
    assert res_ceil["formatted"] == "18k"


@pytest.mark.parametrize("series", sorted(SERIES_MAP))
@pytest.mark.parametrize("mode", ["nearest", "floor", "ceil"])
def test_standard_value_snaps_to_itself(series, mode):
    """IEC 60063: serideki bir değer her kipte kendisidir (1p..1M). Eskiden 330n E6 ceil -> 470n oluyordu."""
    wrong = []
    for exponent in range(-12, 7):
        for base in SERIES_MAP[series]:
            value = float(f"{base * 10.0 ** exponent:.6g}")
            got = snap_to_e_series(value, series=series, mode=mode)["standard_value"]
            if got != pytest.approx(value, rel=1e-9):
                wrong.append((value, got))
    assert wrong == []


@pytest.mark.parametrize("text,series,expected", [
    # E serileri geometriktir (~10^(i/N)); en yakın değer göreli (logaritmik) uzaklıkla seçilir. Sınır iki
    # komşunun geometrik ortalamasıdır: E6'da 1.0 ile 1.5 arası sqrt(1.5) = 1.2247, aritmetik orta 1.25.
    ("1.23k", "E6", 1500.0),    # 1.2247 < 1.23 < 1.25: log -> 1.5k, doğrusal -> 1.0k
    ("1.22k", "E6", 1000.0),
    ("8.3", "E6", 10.0),        # 6.8 ile 10: sqrt(68) = 8.246 < 8.3 < 8.4
    ("1.097", "E12", 1.2),      # 1.0 ile 1.2: sqrt(1.2) = 1.0954 < 1.097 < 1.1
])
def test_nearest_uses_logarithmic_distance(text, series, expected):
    assert snap_to_e_series(text, series=series)["standard_value"] == expected


def test_snap_to_e_series_negative_value_raises():
    with pytest.raises(ValueError, match="pozitif"):
        snap_to_e_series(-100)


def test_snap_to_e_series_invalid_series_raises():
    with pytest.raises(ValueError, match="Bilinmeyen E-serisi"):
        snap_to_e_series("10k", series="E50")


def test_quantize_circuit_parameters():
    params = {
        "R1": "15.915k",
        "R2": "15.915k",
        "C1": "1.414n",
        "C2": "0.707n",
    }
    res = quantize_circuit_parameters(params, series="E24")
    assert res["status"] == "SUCCESS"
    assert res["series"] == "E24"
    assert res["quantized_parameters"]["R1"] == "16k"
    assert res["quantized_parameters"]["R2"] == "16k"
    assert res["quantized_parameters"]["C1"] == "1.5n"
    # 707p: |ln(707/680)| = 0.039 < |ln(750/707)| = 0.059
    assert res["quantized_parameters"]["C2"] == "680p"
    assert "details" in res
    assert res["max_abs_error_pct"] > 0


def test_quantize_parameters_tool_call():
    res = handle_tool_call(
        "quantize_parameters",
        {"parameters": {"R_load": "4.67k"}, "series": "E12"},
    )
    assert res["status"] == "SUCCESS"
    assert res["quantized_parameters"]["R_load"] == "4.7k"


def test_formula_or_text_parameters_are_left_as_they_are():
    """Belge (quantize_circuit_parameters): sayısal olmayan ya da formül içeren parametre olduğu gibi bırakılır."""
    res = quantize_circuit_parameters({"R": "4.67k", "X": "{R_base*2}", "M": "NPN"}, series="E12")
    assert res["quantized_parameters"] == {"R": "4.7k", "X": "{R_base*2}", "M": "NPN"}   # IEC 60063 E12: 4.7
    assert "skipped_reason" in res["details"]["X"] and "skipped_reason" in res["details"]["M"]
    assert set(res["raw_quantized_values"]) == {"R"}
