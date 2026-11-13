"""
Devre karşılaştırma (compare_netlists, compare_measurements, compare_circuits aracı) ve CSV dışa aktarım
(export_waveforms_csv ve aracı) testleri. LTspice gerektirmez; dalga formları ve ölçümler gerçek LTspice 26.1.1
kayıtlarıdır (tests/data/ref26_*, examples/14_record_flow_fixtures.py).

Beklenen değerlerin kaynağı:
- Netlist okuma: ADI SPICE-SYNTAX-REFERENCE "Netlist Structure" (ilk satır başlık), "Comments and Line Continuation"
  (``*`` yorum, ``;`` satırın kalanı yorum, ``+`` devam), sonek tablosu (``K`` = 1e3, ``u`` = 1e-6) ve "Important
  Rules" (sonekler büyük/küçük harfe duyarsız, ``1M`` mili); SIMULATION-COMMANDS-REFERENCE ".END" (sonrası okunmaz) ve
  ``.meas [TRAN|AC|DC|NOISE] <name>``; ADI README "Node Naming: Names are case-insensitive".
- Tek değişken ve aynı düğüm kuralı: GEMINI.md "Simülasyon doğrulama kuralları".
- Ölçüm değerleri: kayıtlı logların kendi satırları (ilgili testte alıntılandı); satır ve sütunlar .raw başlığı.
"""

import csv
import json
import math
import re
import shutil
import sys
from decimal import Decimal, localcontext
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from hypothesis.extra import numpy as hnp
from spicelib import RawRead

from ltspice_llm import (
    LTSpiceAdapter,
    compare_measurements,
    compare_netlists,
    export_waveforms_csv,
    get_ltspice_tool_schemas,
    handle_tool_call,
)
from ltspice_llm.adapter import AUTO_THREADS_MARK
from ltspice_llm.export import CSV_DECIMALS, CSV_DELIMITERS, _text_column
from ltspice_llm.tools import _definition_owner

DATA = Path(__file__).parent / "data"

# tests/data/ref26_flow_rc.net ile aynı devre (examples/14 RC)
RC = """* rc flow fixture
V1 in 0 PULSE(0 5 0 1n 1n 1m 2m)
R1 in out 1k
C1 out 0 1u
.tran 3m
.save V(in) V(out)
.meas tran v_max MAX V(out)
.meas tran vr_max MAX V(in,out)
.end
"""


def _changes(result):
    return [(c["kind"], c["name"], c["a"], c["b"]) for c in result["changes"]]


# ------------------------------------------------------------------ compare_netlists
def test_one_changed_element_is_a_single_variable():
    result = compare_netlists(RC, RC.replace("R1 in out 1k", "R1 in out 2k"))
    assert _changes(result) == [("element", "R1", "R1 in out 1k", "R1 in out 2k")]
    assert result["changed_variable_count"] == 1 and result["single_variable"] is True
    assert result["measurement_changes"] == []


def test_two_changed_elements_are_not_a_single_variable():
    """GEMINI.md: bir seferde tek değişken. İkisi birden değişince fark tek bileşene atfedilemez."""
    result = compare_netlists(RC, RC.replace("R1 in out 1k", "R1 in out 2k").replace("C1 out 0 1u", "C1 out 0 2u"))
    assert _changes(result) == [
        ("element", "R1", "R1 in out 1k", "R1 in out 2k"),
        ("element", "C1", "C1 out 0 1u", "C1 out 0 2u"),
    ]
    assert result["changed_variable_count"] == 2 and result["single_variable"] is False


def test_identical_netlists_have_no_variable():
    result = compare_netlists(RC, RC)
    assert result == {"changes": [], "changed_variable_count": 0, "single_variable": False,
                      "measurement_changes": []}


@pytest.mark.parametrize("variant", [
    pytest.param(RC.replace("* rc flow fixture", "R9 out 0 1"), id="baslik-satiri"),
    pytest.param(RC.replace("R1 in out 1k\n", "* R9 out 0 1\nR1 in out 1k ; R9 out 0 1\n"), id="yorumlar"),
    pytest.param(RC + "R9 out 0 1\n", id="end-sonrasi"),
    pytest.param(RC.replace("PULSE(0 5 0 1n 1n 1m 2m)", "PULSE(0 5 0 1n 1n\n+ 1m 2m)"), id="devam-satiri"),
    pytest.param(RC.replace("R1 in out 1k", "R1   in\tout  1k"), id="bosluk"),
    pytest.param(RC.replace("R1 in out 1k", "r1 IN Out 1K"), id="buyuk-kucuk-harf"),
    pytest.param(RC.replace("R1 in out 1k\nC1 out 0 1u\n", "C1 out 0 1u\nR1 in out 1k\n"), id="satir-sirasi"),
    pytest.param(RC.replace("R1 in out 1k", "R1 in out 1000"), id="1k=1000"),
    pytest.param(RC.replace("R1 in out 1k", "R1 in out 1e3"), id="1k=1e3"),
    pytest.param(RC.replace("C1 out 0 1u", "C1 out 0 1e-6"), id="1u=1e-6"),
    pytest.param(RC.replace(".save V(in) V(out)\n", ""), id="save"),
    pytest.param(RC.replace(".end\n", ".backanno\n.end\n"), id="backanno"),
    pytest.param(RC.replace(".end\n", f".options threads=2 ; {AUTO_THREADS_MARK}\n.end\n"), id="otomatik-threads"),
])
def test_text_that_does_not_change_the_circuit_is_not_a_change(variant):
    """LTspice'ın okumadığı ya da aynı okuduğu metin (kaynaklar modül başlığında); `.save` yalnız kaydı, adaptörün
    işaretli `.options threads` satırı yalnız çözücü iş parçacığını etkiler (adapter.AUTO_THREADS_MARK). `.backanno`
    şemadan üretilen netliste kendiliğinden eklenir ve yalnız pin akımlarını işaretler (ADI SIMULATION-COMMANDS-REFERENCE
    ".BACKANNO — Pin Annotation": "Automatically included in schematics")."""
    assert variant != RC
    for result in (compare_netlists(RC, variant), compare_netlists(variant, RC)):
        assert result["changes"] == [] and result["measurement_changes"] == []


def test_1m_is_milli_and_1meg_is_mega():
    """ADI SPICE-SYNTAX-REFERENCE "Important Rules": `1M` means 1 milliohm (1e-3), NOT 1 megaohm."""
    milli, mega = RC.replace("R1 in out 1k", "R1 in out 1M"), RC.replace("R1 in out 1k", "R1 in out 1Meg")
    assert _changes(compare_netlists(milli, mega)) == [("element", "R1", "R1 in out 1M", "R1 in out 1Meg")]
    assert compare_netlists(milli, RC.replace("R1 in out 1k", "R1 in out 1e-3"))["changes"] == []
    assert compare_netlists(mega, RC.replace("R1 in out 1k", "R1 in out 1e6"))["changes"] == []


def test_changed_node_is_a_change_even_when_the_value_is_the_same():
    result = compare_netlists(RC, RC.replace("C1 out 0 1u", "C1 in 0 1u"))
    assert _changes(result) == [("element", "C1", "C1 out 0 1u", "C1 in 0 1u")]


def test_added_and_removed_elements():
    added = RC.replace("C1 out 0 1u\n", "C1 out 0 1u\nR2 out 0 10k\n")
    assert _changes(compare_netlists(RC, added)) == [("element", "R2", None, "R2 out 0 10k")]
    assert _changes(compare_netlists(added, RC)) == [("element", "R2", "R2 out 0 10k", None)]


PARAM = "* p\n.param rv=1k cv=1u\nV1 in 0 1\nR1 in out {rv}\nC1 out 0 {cv}\n.op\n.end\n"


def test_param_assignments_are_compared_one_by_one():
    """Bir `.param` satırında birden çok atama olabilir (".param R1=1k R2=2k", adapter._update_param); değişken
    satır değil atamadır."""
    reordered = PARAM.replace(".param rv=1k cv=1u", ".param cv = 1u\n.param RV=1000")
    assert compare_netlists(PARAM, reordered)["changes"] == []
    changed = compare_netlists(PARAM, PARAM.replace("rv=1k", "rv=2k"))
    assert _changes(changed) == [("param", "rv", ".param rv=1k", ".param rv=2k")]
    assert changed["single_variable"] is True
    both = compare_netlists(PARAM, PARAM.replace("rv=1k cv=1u", "rv=2k cv=2u"))
    assert [c["name"] for c in both["changes"]] == ["rv", "cv"]
    # eski sözdizimi ".param AD DEĞER" (adapter._PARAM_LEGACY_RE) aynı atamadır
    legacy = PARAM.replace(".param rv=1k cv=1u", ".param rv 1k\n.param cv 1u")
    assert compare_netlists(PARAM, legacy)["changes"] == []
    assert _changes(compare_netlists(legacy, PARAM.replace("rv=1k", "rv=2k"))) == [
        ("param", "rv", ".param rv=1k", ".param rv=2k")]


def test_solver_option_is_one_variable():
    """GEMINI.md: salınım görürsen `.options method=gear` ile tekrar koş. Bu karşılaştırmanın tek değişkeni seçenektir."""
    gear = RC.replace(".tran 3m\n", ".tran 3m\n.options method=gear plotwinsize=0\n")
    trap = RC.replace(".tran 3m\n", ".tran 3m\n.options plotwinsize=0\n")
    result = compare_netlists(trap, gear)
    assert _changes(result) == [("option", "method", None, ".options method=gear")]
    assert result["single_variable"] is True
    assert compare_netlists(gear, gear.replace("method=gear plotwinsize=0", "PLOTWINSIZE=0 METHOD=Gear"))["changes"] == []


def test_option_values_are_numbers_and_the_singular_command_is_the_same():
    """ADI SPICE-SYNTAX-REFERENCE "Engineering Multipliers": `m` = 1e-3, yani `0.1m` = 1e-4. LTspice kendi logunda tekil
    yazımı önerir (tests/data/ref26_dcop_fail.log.txt, 26.1.1: 'Use".option gminsteps=0"'): `.option` da aynı komuttur."""
    base = RC.replace(".tran 3m\n", ".tran 3m\n.options reltol=1e-4\n")
    assert compare_netlists(base, base.replace("reltol=1e-4", "reltol=0.1m"))["changes"] == []
    assert compare_netlists(base, base.replace(".options reltol", ".option reltol"))["changes"] == []
    assert _changes(compare_netlists(base, base.replace("reltol=1e-4", "reltol=1e-3"))) == [
        ("option", "reltol", ".options reltol=1e-4", ".options reltol=1e-3")]


def test_analysis_command_is_compared_in_one_slot():
    result = compare_netlists(RC, RC.replace(".tran 3m", ".ac dec 10 10 100k"))
    assert _changes(result) == [("analysis", ".tran", ".tran 3m", ".ac dec 10 10 100k")]


def test_measurement_definition_is_reported_apart_from_the_variables():
    """GEMINI.md: aynı düğümü ölç. `.meas` tanımı devreyi değiştirmez (değişken sayılmaz) ama ayrıca bildirilir."""
    result = compare_netlists(RC, RC.replace("MAX V(in,out)", "MAX V(in)"))
    assert result["changes"] == [] and result["changed_variable_count"] == 0
    assert result["measurement_changes"] == [
        {"kind": "measurement", "name": "vr_max", "a": ".meas tran vr_max MAX V(in,out)",
         "b": ".meas tran vr_max MAX V(in)"},
    ]
    # analiz türü yazılmamış aynı tanım (".meas [TRAN|AC|DC|NOISE] <name>": tür isteğe bağlı)
    assert compare_netlists(RC, RC.replace(".meas tran v_max", ".MEAS v_max"))["measurement_changes"] == []


SUBCKT = "* s\nV1 in 0 10\nX1 in out DIV\nR1 out 0 1k\n.subckt DIV a b\nR1 a b 1k\n.ends\n.op\n.end\n"


def test_same_name_inside_a_subcircuit_is_a_different_element():
    """Alt devrenin içindeki R1 ile üst düzeydeki R1 ayrı elemanlardır; hangisinin değiştiği adından okunur."""
    inner = compare_netlists(SUBCKT, SUBCKT.replace("R1 a b 1k", "R1 a b 3k"))
    assert _changes(inner) == [("element", "DIV:R1", "R1 a b 1k", "R1 a b 3k")]
    outer = compare_netlists(SUBCKT, SUBCKT.replace("R1 out 0 1k", "R1 out 0 3k"))
    assert _changes(outer) == [("element", "R1", "R1 out 0 1k", "R1 out 0 3k")]


@pytest.mark.parametrize("variant, expected", [
    # ADI SPICE-SYNTAX-REFERENCE "Netlist Structure": "Between the title and `.END`, line order is irrelevant". `.ends`
    # kapsamı kapatmazsa blok öne taşınınca arkasındaki elemanlar DIV:... olur, sahte ekleme ve silme görünür.
    pytest.param(SUBCKT.replace("X1 in out DIV\nR1 out 0 1k\n.subckt DIV a b\nR1 a b 1k\n.ends\n",
                                ".subckt DIV a b\nR1 a b 1k\n.ends\nX1 in out DIV\nR1 out 0 1k\n"), [], id="blok-onde"),
    pytest.param(SUBCKT.replace(".subckt DIV a b", ".subckt DIV a b c"),
                 [("directive", ".subckt", ".subckt DIV a b", ".subckt DIV a b c")], id="port"),
])
def test_subcircuit_block_can_move_and_its_header_is_compared(variant, expected):
    assert variant != SUBCKT
    assert _changes(compare_netlists(SUBCKT, variant)) == expected


MODEL = "* m\nV1 a 0 1\nR1 a b 1k\nD1 b 0 DX\n.model DX D(Is=1e-14)\n.op\n.end\n"


def test_model_card_is_its_own_variable_and_belongs_to_its_subcircuit():
    """`.model` adıyla eşleşir (compare_netlists belgesi: `kind` "model"); parametresi değişince tek değişken modeldir.
    Alt devrenin içindeki `.model` ve `.param` o alt devrenindir: adı alt devreyle başlar (`DIV:...`), üst düzeydeki aynı
    adla karışmaz."""
    top = compare_netlists(MODEL, MODEL.replace("Is=1e-14", "Is=2e-14"))
    assert _changes(top) == [("model", "DX", ".model DX D(Is=1e-14)", ".model DX D(Is=2e-14)")]
    assert top["single_variable"] is True
    inner = SUBCKT.replace("R1 a b 1k\n", "R1 a b {k}\n.param k=1k\n.model DY D(Is=1e-14)\n")
    changed = inner.replace("k=1k", "k=2k").replace("Is=1e-14", "Is=2e-14")
    assert _changes(compare_netlists(inner, changed)) == [
        ("param", "DIV:k", ".param k=1k", ".param k=2k"),
        ("model", "DIV:DY", ".model DY D(Is=1e-14)", ".model DY D(Is=2e-14)"),
    ]


@pytest.mark.parametrize("lines_a, lines_b, expected", [
    (".temp 27", ".temp 85", [("directive", ".temp", ".temp 27", ".temp 85")]),
    (".ic V(out)=1", "", [("directive", ".ic", ".ic V(out)=1", None)]),
    (".lib a.lib\n.lib b.lib", ".lib b.lib", [("directive", ".lib", ".lib a.lib", None)]),
    (".lib a.lib\n.lib b.lib", ".lib b.lib\n.lib a.lib", []),
    (".temp 27", ".temp 27\n.ic V(out)=1", [("directive", ".ic", None, ".ic V(out)=1")]),
    # docs/GUIDE.md "Comparing two circuits" (`changes`): adsız direktifte iki kez geçen satır iki kez sayılır
    (".ic V(out)=1\n.ic V(out)=1", ".ic V(out)=1", [("directive", ".ic", ".ic V(out)=1", None)]),
])
def test_unnamed_directives_count_once_per_changed_line(lines_a, lines_b, expected):
    def with_lines(lines):
        return RC.replace(".tran 3m\n", f"{lines}\n.tran 3m\n")

    assert _changes(compare_netlists(with_lines(lines_a), with_lines(lines_b))) == expected


def test_real_fixture_pair_differs_only_in_the_saved_window():
    """ref26_flow_rc (`.tran 3m`) ve ref26_rc_tstart (`.tran 0 3m 1m` ve üç ek `.meas`; examples/14 RC_TSTART)."""
    result = compare_netlists((DATA / "ref26_flow_rc.net").read_text(encoding="utf-8"),
                              (DATA / "ref26_rc_tstart.net").read_text(encoding="utf-8"))
    assert _changes(result) == [("analysis", ".tran", ".tran 3m", ".tran 0 3m 1m")]
    assert result["single_variable"] is True
    assert [(m["name"], m["a"]) for m in result["measurement_changes"]] == [
        ("t_fall", None), ("t_rise", None), ("v_avg", None)]


# Aynı değerin farklı yazımları aynı sınıftadır (ADI sonek tablosu; `1M` mili, `1Meg` mega).
_VALUES = [
    ("1k", "1e3"), ("1000", "1e3"), ("1e3", "1e3"), ("1K", "1e3"), ("2.2k", "2200"), ("47", "47"),
    ("1u", "1e-6"), ("1e-6", "1e-6"), ("1m", "1e-3"), ("1M", "1e-3"), ("1e-3", "1e-3"),
    ("1Meg", "1e6"), ("1e6", "1e6"), ("100n", "1e-7"), ("1e-7", "1e-7"),
]
_NODES = ["0", "in", "out", "n1", "N002"]
_ELEMENT = st.tuples(st.sampled_from(_NODES), st.sampled_from(_NODES), st.sampled_from(_VALUES))


def _netlist(rows):
    return "* gen\n" + "".join(f"{ref} {a} {b} {value[0]}\n" for ref, a, b, value in rows) + ".op\n.end\n"


@given(data=st.data())
def test_changed_variables_are_exactly_the_elements_whose_value_changed(data):
    """Bağımsız oracle: üreteç hangi elemanın değer sınıfını değiştirdiğini bilir. Satır sırası ve aynı değerin başka
    yazımı fark değildir; sonuç iki yönde aynı değişkenleri verir."""
    refs = data.draw(st.lists(st.builds("{}{}".format, st.sampled_from("RCL"), st.integers(1, 40)),
                              min_size=1, max_size=8, unique=True))
    before = [(ref, *data.draw(_ELEMENT)) for ref in refs]
    after = [(ref, a, b, data.draw(st.sampled_from(_VALUES))) for ref, a, b, _ in before]
    shuffled = data.draw(st.permutations(after))
    expected = [old[0] for old, new in zip(before, after) if old[3][1] != new[3][1]]

    result = compare_netlists(_netlist(before), _netlist(shuffled))
    assert [c["name"] for c in result["changes"]] == expected
    assert result["changed_variable_count"] == len(expected)
    assert result["single_variable"] is (len(expected) == 1)
    mirrored = compare_netlists(_netlist(shuffled), _netlist(before))
    assert sorted((c["name"], c["b"], c["a"]) for c in mirrored["changes"]) == sorted(
        (c["name"], c["a"], c["b"]) for c in result["changes"])


# ADI SPICE-SYNTAX-REFERENCE "Engineering Multipliers" tablosu ve "Important Rules": sonekler büyük/küçük harfe duyarsız
# (`M` de mili), tanınmayan harfler yok sayılır (`10V` = 10), gömülü ondalık `6K34` = 6.34K, bilimsel gösterim geçerli.
# Kuyruklar çarpan harfiyle başlamaz (`4Farads` femto olurdu); gömülü ondalıktan sonra kuyruk yazılmaz (LTspice hata verir,
# ltspice_llm/quantizer.py). Oracle değeri tablodan kesirli sayıyla kurar, paketin ayrıştırıcısını kullanmaz.
_MULTIPLIERS = [("T", Fraction(10) ** 12), ("G", Fraction(10) ** 9), ("Meg", Fraction(10) ** 6), ("K", Fraction(10) ** 3),
                ("", Fraction(1)), ("m", Fraction(10) ** -3), ("u", Fraction(10) ** -6), ("n", Fraction(10) ** -9),
                ("p", Fraction(10) ** -12), ("f", Fraction(10) ** -15), ("mil", Fraction(254, 10 ** 7))]
_TAILS = ["", "V", "Volts", "Hz", "Ohm"]


def _any_case(draw, text):
    return "".join(c.upper() if draw(st.booleans()) else c.lower() for c in text)


def _plain(value):
    """Sonlu ondalık açılımlı kesrin üssüz yazımı (tablodaki çarpanlarla kurulan her değerin açılımı sonludur)."""
    with localcontext() as ctx:
        ctx.prec = 80
        text = format((Decimal(value.numerator) / Decimal(value.denominator)).normalize(), "f")
    assert Fraction(text) == value
    return text


@st.composite
def _fresh_value(draw):
    """(yazım, değer): tablodan bir çarpan, 1-4 basamaklı mantis, isteğe bağlı işaret (test-reviewer 2026-11-11: strateji
    negatif değer üretmiyordu); ya kuyruklu ya da gömülü ondalıklı. Gömülü kesir LTspice'ta işaretten bağımsız eklenir:
    -4k7 = (-4 + 0.7)k = -3300 (LTspice 26.1.1 ölçümü, examples/12; tests/test_quantizer.py)."""
    sign = draw(st.sampled_from(["", "-", "+"]))
    whole_sign = -1 if sign == "-" else 1
    suffix, factor = draw(st.sampled_from(_MULTIPLIERS))
    digits, decimals = draw(st.integers(1, 9999)), draw(st.integers(0, 3))
    if suffix and not decimals and draw(st.booleans()):
        fraction = draw(st.integers(1, 99))
        return (f"{sign}{digits}{_any_case(draw, suffix)}{fraction}",
                (whole_sign * digits + Fraction(f"0.{fraction}")) * factor)
    text = str(digits).rjust(decimals + 1, "0")
    mantissa = f"{text[:-decimals]}.{text[-decimals:]}" if decimals else text
    return (f"{sign}{mantissa}{_any_case(draw, suffix)}{_any_case(draw, draw(st.sampled_from(_TAILS)))}",
            whole_sign * Fraction(mantissa) * factor)


@st.composite
def _same_value(draw, value):
    """Aynı değerin başka bir yazımı: onluk çarpanla düz mantis ya da çarpansız bilimsel gösterim."""
    if draw(st.booleans()):
        exponent = format(Decimal(_plain(value)).normalize(), "e").replace("e+", "e")
        return exponent, value
    suffix, factor = draw(st.sampled_from([m for m in _MULTIPLIERS if m[0] != "mil"]))
    return f"{_plain(value / factor)}{_any_case(draw, suffix)}{_any_case(draw, draw(st.sampled_from(_TAILS)))}", value


@given(data=st.data())
def test_value_spellings_nodes_and_membership_against_an_independent_oracle(data):
    """
    Bağımsız oracle: üreteç her elemanın değerini (kesirli), düğümlerini ve hangi devrede bulunduğunu bilir. Değişken,
    değeri ya da düğümü değişen, silinen ya da eklenen elemandır; yazım, büyük/küçük harf ve satır sırası fark değildir
    (ADI "Node Naming: Names are case-insensitive"). Sıra: A'nın elemanları A'daki sırayla, sonra yalnız B'dekiler B'deki
    sırayla (compare_netlists belgesi).
    """
    def pins():
        return data.draw(st.tuples(st.sampled_from(_NODES), st.sampled_from(_NODES)))

    def folded(nodes):
        return tuple(node.casefold() for node in nodes)

    def netlist(rows):
        return "* gen\n" + "".join(f"{ref} {a} {b} {text}\n" for ref, (a, b), text in rows) + ".op\n.end\n"

    pool = [f"{kind}{number}" for kind in "RCL" for number in range(1, 9)]
    before_refs = data.draw(st.lists(st.sampled_from(pool), min_size=1, max_size=6, unique=True))
    before = {ref: (pins(), *data.draw(_fresh_value())) for ref in before_refs}
    after = {}
    for ref in before_refs:
        if data.draw(st.booleans()):  # eleman B'de de var
            nodes = pins() if data.draw(st.booleans()) else before[ref][0]
            respelled = _same_value(before[ref][2]) if data.draw(st.booleans()) else _fresh_value()
            after[ref] = (nodes, *data.draw(respelled))
    for ref in data.draw(st.lists(st.sampled_from([p for p in pool if p not in before]), max_size=2, unique=True)):
        after[ref] = (pins(), *data.draw(_fresh_value()))
    order = data.draw(st.permutations(list(after)))
    shown = {ref: data.draw(st.sampled_from([ref, ref.lower()])) for ref in order}  # adın B'deki yazımı

    netlist_a = netlist([(ref, before[ref][0], before[ref][1]) for ref in before_refs])
    netlist_b = netlist([(shown[ref], tuple(_any_case(data.draw, node) for node in after[ref][0]), after[ref][1])
                         for ref in order])
    expected = []
    for ref in before_refs:
        if ref not in after:
            expected.append((ref, "silindi"))
        elif folded(after[ref][0]) != folded(before[ref][0]) or after[ref][2] != before[ref][2]:
            expected.append((ref, "değişti"))
    expected += [(shown[ref], "eklendi") for ref in order if ref not in before]

    result = compare_netlists(netlist_a, netlist_b)
    assert [(c["name"], "silindi" if c["b"] is None else "eklendi" if c["a"] is None else "değişti")
            for c in result["changes"]] == expected
    assert all(c["kind"] == "element" for c in result["changes"])
    assert result["changed_variable_count"] == len(expected)
    assert result["single_variable"] is (len(expected) == 1)


# ------------------------------------------------------------------ compare_measurements
@pytest.mark.parametrize("a, b, delta, percent", [
    (2.0, 3.0, 1.0, 50.0),
    (4.0, 1.0, -3.0, -75.0),
    (-2.0, -1.0, 1.0, 50.0),      # yüzde |a|'ya göredir: işareti farkın işaretidir
    (-2.0, -5.0, -3.0, -150.0),
    (5.0, 5.0, 0.0, 0.0),
    (0.0, 1.0, 1.0, None),        # sıfıra göre yüzde tanımsız
])
def test_delta_and_relative_change_by_hand(a, b, delta, percent):
    (row,) = compare_measurements({"m": a}, {"m": b})["measurements"]
    assert row == {"name": "m", "a": a, "b": b, "delta": delta, "relative_change_percent": percent}


@pytest.mark.parametrize("a, b", [(None, 1.0), ("FAILED", 2.0), (True, 1), (float("nan"), 1.0), (1.0, float("inf"))])
def test_values_that_are_not_finite_numbers_have_no_delta(a, b):
    (row,) = compare_measurements({"m": a}, {"m": b})["measurements"]
    assert row["delta"] is None and row["relative_change_percent"] is None


def test_measurement_names_and_filter():
    result = compare_measurements({"v_max": 1.0, "t_rise": 2.0, "only_a": 0.0},
                                  {"t_rise": 2.5, "v_max": 3.0, "only_b": 0.0}, ["V_MAX", " nope "])
    assert [row["name"] for row in result["measurements"]] == ["v_max"]
    assert result["not_found"] == ["nope"]
    assert result["only_in_a"] == ["only_a"] and result["only_in_b"] == ["only_b"]
    everything = compare_measurements({"v_max": 1.0, "t_rise": 2.0}, {"t_rise": 2.5, "v_max": 3.0})
    assert [row["name"] for row in everything["measurements"]] == ["v_max", "t_rise"]  # a'nın sırası
    assert everything["not_found"] == []
    # belgesi: ortak olmayan istenen ölçümlerin hepsi not_found'dadır, istenen sırayla
    assert compare_measurements({"m": 1.0}, {"m": 2.0}, ["x", "m", "y"])["not_found"] == ["x", "y"]


_FINITE = st.floats(allow_nan=False, allow_infinity=False)


def _assert_matches_exact_arithmetic(a, b):
    """Bağımsız oracle: kesirli (Fraction) aritmetik yuvarlamaz ve taşmaz. delta, b - a'nın en yakın float'ıdır; yüzde
    (b - a) / |a| * 100'dür; float aralığına sığmayan sonuç None'dır. Ters yönde fark işaret değiştirir."""
    (row,) = compare_measurements({"m": a}, {"m": b})["measurements"]
    (back,) = compare_measurements({"m": b}, {"m": a})["measurements"]
    limit = Fraction(sys.float_info.max)
    exact_delta = Fraction(b) - Fraction(a)
    if row["delta"] is None:
        assert abs(exact_delta) > limit and back["delta"] is None
        assert row["relative_change_percent"] is None
        return
    assert row["delta"] == float(exact_delta) and back["delta"] == -row["delta"]
    if a == 0:
        assert row["relative_change_percent"] is None
        return
    exact_percent = exact_delta / abs(Fraction(a)) * 100
    if row["relative_change_percent"] is None:
        assert abs(exact_percent) > limit * (1 - Fraction(1, 10**12))  # float aralığının ucunda ya da dışında
    else:
        error = abs(Fraction(row["relative_change_percent"]) - exact_percent)
        assert error <= abs(exact_percent) * Fraction(1, 10**12)


@given(a=_FINITE, b=_FINITE)
def test_delta_and_percent_rebuild_b_from_a(a, b):
    _assert_matches_exact_arithmetic(a, b)


@pytest.mark.parametrize("a, b", [
    # Hypothesis karşı örneği (2026-11-11): eski oracle a + |a| * yüzde / 100'ü float ile kuruyordu ve kendi taşıyordu.
    (15625.0, sys.float_info.max),
    (-sys.float_info.max, sys.float_info.max),  # fark float aralığına sığmaz: delta None
    (5e-324, 1.0),                              # yüzde float aralığına sığmaz: yüzde None, delta 1.0
])
def test_delta_and_percent_at_the_edges_of_the_float_range(a, b):
    _assert_matches_exact_arithmetic(a, b)
    (row,) = compare_measurements({"m": a}, {"m": b})["measurements"]
    assert (row["delta"] is None) == (a == -sys.float_info.max)
    assert (row["relative_change_percent"] is None) == (a != 15625.0)


# ------------------------------------------------------------------ export_waveforms_csv
def _raw_header(path):
    """LTspice .raw başlığı UTF-16LE metindir, veri "Binary:" satırından sonra gelir (paketin okuyucusundan bağımsız)."""
    data = Path(path).read_bytes()
    return data[: data.index("Binary:".encode("utf-16-le"))].decode("utf-16-le")


def _raw_points(path):
    return int(re.search(r"^No\. Points:\s*(\d+)\s*$", _raw_header(path), re.MULTILINE).group(1))


def _raw_variables(path):
    block = re.split(r"^Variables:\s*$", _raw_header(path), flags=re.MULTILINE)[1]
    return [line.split("\t")[2] for line in block.splitlines() if line.startswith("\t")]


def _read_csv(path, delimiter=","):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.reader(handle, delimiter=delimiter))


def test_csv_holds_every_sample_unrounded(tmp_path):
    """Satır sayısı ve sütunlar .raw başlığındandır. Her hücre, spicelib'in aynı dosyadan okuduğu değerin kendi türünde
    (zaman float64, izler float32) birebir aynısıdır; başlıktaki Offset 0 olduğu için eksen olduğu gibi yazılır."""
    raw_path = DATA / "ref26_flow_rc.raw"
    info = export_waveforms_csv(raw_path, tmp_path / "rc.csv")
    header, *rows = _read_csv(tmp_path / "rc.csv")
    assert header == info["columns"] == _raw_variables(raw_path) == ["time", "V(in)", "V(out)"]
    assert len(rows) == info["rows"] == _raw_points(raw_path)
    assert info["csv_path"] == str(tmp_path / "rc.csv") and info["steps"] == 1 and info["analysis"] == "tran"

    reader = RawRead(str(raw_path), traces_to_read="*")
    time, v_in, v_out = (np.array(column) for column in zip(*rows))
    assert np.array_equal(time.astype(np.float64), np.asarray(reader.get_axis(0)))
    assert np.array_equal(v_in.astype(np.float32), np.asarray(reader.get_trace("V(in)").get_wave(0)))
    assert np.array_equal(v_out.astype(np.float32), np.asarray(reader.get_trace("V(out)").get_wave(0)))
    assert v_out.astype(np.float32).dtype == reader.get_trace("V(out)").get_wave(0).dtype  # fixture gerçekten float32


def test_csv_reproduces_ltspice_own_measurements(tmp_path):
    """tests/data/ref26_flow_rc.log.txt (LTspice 26.1.1): "v_max: MAX(V(out))=3.58834481239 FROM 0 TO 0.003" ve
    "vr_max: MAX(V(in,out))=4.99999749785 FROM 0 TO 0.003". İzler float32 kaydedildiği için tolerans 1e-6."""
    export_waveforms_csv(DATA / "ref26_flow_rc.raw", tmp_path / "rc.csv")
    _, *rows = _read_csv(tmp_path / "rc.csv")
    v_in = np.array([float(row[1]) for row in rows])
    v_out = np.array([float(row[2]) for row in rows])
    assert v_out.max() == pytest.approx(3.58834481239, rel=1e-6)
    assert (v_in - v_out).max() == pytest.approx(4.99999749785, rel=1e-6)


def test_time_column_is_absolute_when_tstart_is_not_zero(tmp_path):
    """ref26_rc_tstart.net: `.tran 0 3m 1m` (Tstart 1 ms, Tstop 3 ms); log: "FROM 0.001 TO 0.003". LTspice ekseni
    0'dan yazar ve kaymayı başlığın Offset alanına koyar (docs/LESSONS_LEARNED_LTSPICE_REFERENCE.md §2.20)."""
    raw_path = DATA / "ref26_rc_tstart.raw"
    assert re.search(r"^Offset:\s*1\.0+e-03\s*$", _raw_header(raw_path), re.MULTILINE)  # fixture gerçekten kaymalı
    export_waveforms_csv(raw_path, tmp_path / "late.csv")
    _, *rows = _read_csv(tmp_path / "late.csv")
    times = [float(row[0]) for row in rows]
    assert times[0] == pytest.approx(1e-3, abs=1e-12) and times[-1] == pytest.approx(3e-3, abs=1e-12)
    assert times == sorted(times) and len(rows) == _raw_points(raw_path)


def test_stepped_run_is_written_step_by_step(tmp_path):
    """ref26_rc_tstart_step.net: `.step param r list 1k 2k`, `.tran 0 3m 1m`. Logun adım tablosu (LTspice 26.1.1):
    v_max adım 1 (r=1k) 3.58834481239, adım 2 (r=2k) 2.69109368324. Her adımın ekseni Tstart'tan başlar."""
    shutil.copy(DATA / "ref26_rc_tstart_step.raw", tmp_path / "swept.raw")
    shutil.copy(DATA / "ref26_rc_tstart_step.log.txt", tmp_path / "swept.log")
    info = export_waveforms_csv(tmp_path / "swept.raw", tmp_path / "swept.csv")
    header, *rows = _read_csv(tmp_path / "swept.csv")
    assert header == info["columns"] == ["step", "time", "V(in)", "V(out)"] and info["steps"] == 2
    assert len(rows) == info["rows"] == _raw_points(tmp_path / "swept.raw")
    steps = [row[0] for row in rows]
    assert set(steps) == {"1", "2"} and steps == sorted(steps)
    v_max = {step: max(float(row[3]) for row in rows if row[0] == step) for step in ("1", "2")}
    assert v_max["1"] == pytest.approx(3.58834481239, rel=1e-6)
    assert v_max["2"] == pytest.approx(2.69109368324, rel=1e-6)
    for step in ("1", "2"):
        times = [float(row[1]) for row in rows if row[0] == step]
        assert times[0] == pytest.approx(1e-3, abs=1e-12) and times[-1] == pytest.approx(3e-3, abs=1e-12)


def test_each_step_is_written_with_its_own_time_axis(tmp_path):
    """ref26_cmp_step_c (examples/14 CMP_STEP_C): `.step param c list 1u 100n`, `.tran 3m`. LTspice 26.1.1 adımları
    küçükten büyüğe koştu (log: ".step c=1e-07", sonra ".step c=1e-06") ve zaman adımını devreye göre seçti; logun adım
    tablosu: v_max adım 1 4.99977302551, adım 2 3.58834481239. Eksenler spicelib'le ayrıca okunur (Offset 0)."""
    shutil.copy(DATA / "ref26_cmp_step_c.raw", tmp_path / "c.raw")
    shutil.copy(DATA / "ref26_cmp_step_c.log.txt", tmp_path / "c.log")
    reader = RawRead(str(tmp_path / "c.raw"), traces_to_read="*")
    axes = [np.asarray(reader.get_axis(step)) for step in (0, 1)]
    assert len(axes[0]) != len(axes[1])  # fixture gerçekten ayırt ediyor: iki adımın ekseni farklı
    info = export_waveforms_csv(tmp_path / "c.raw", tmp_path / "c.csv", ["V(out)"])
    header, *rows = _read_csv(tmp_path / "c.csv")
    assert header == info["columns"] == ["step", "time", "V(out)"] and info["steps"] == 2
    assert len(rows) == info["rows"] == len(axes[0]) + len(axes[1])
    for number, axis in zip(("1", "2"), axes):
        assert np.array_equal(np.array([float(row[1]) for row in rows if row[0] == number]), axis)
    v_max = {step: max(float(row[2]) for row in rows if row[0] == step) for step in ("1", "2")}
    assert v_max["1"] == pytest.approx(4.99977302551, rel=1e-6)
    assert v_max["2"] == pytest.approx(3.58834481239, rel=1e-6)


def test_ac_traces_are_written_as_real_and_imaginary_parts(tmp_path):
    """ref26_ac_rc.net: V1 AC 1, R1 = 1k, C1 = 159n, `.ac dec 10 10 100k`: 4 dekat x 10 + 1 = 41 nokta, 10 Hz -
    100 kHz. RC alçak geçiren (elle): H(f) = 1 / (1 + j 2 pi f R C)."""
    info = export_waveforms_csv(DATA / "ref26_ac_rc.raw", tmp_path / "ac.csv")
    header, *rows = _read_csv(tmp_path / "ac.csv")
    assert header == ["frequency", "re(V(in))", "im(V(in))", "re(V(out))", "im(V(out))"]
    assert len(rows) == 41 and info["analysis"] == "ac"
    assert float(rows[0][0]) == 10.0 and float(rows[-1][0]) == pytest.approx(1e5, rel=1e-9)
    for row in rows:
        frequency, in_re, in_im, out_re, out_im = (float(cell) for cell in row)
        expected = 1 / (1 + 2j * math.pi * frequency * 1e3 * 159e-9)
        assert (in_re, in_im) == (1.0, 0.0)
        assert complex(out_re, out_im) == pytest.approx(expected, rel=1e-6)


def test_operating_point_has_no_axis_column(tmp_path):
    """ref26_flow_op (examples/14 DIVIDER: V1 = 10 V, R1 = R2 = 1k, `.op`). Elle: V(out) = 5 V, I(R1) = 10 / 2k = 5 mA."""
    raw_path = DATA / "ref26_flow_op.raw"
    info = export_waveforms_csv(raw_path, tmp_path / "op.csv")
    header, *rows = _read_csv(tmp_path / "op.csv")
    assert header == _raw_variables(raw_path) and header[:3] == ["V(in)", "V(out)", "I(R1)"]
    assert len(rows) == 1 and info["analysis"] == "op"
    assert float(rows[0][0]) == 10.0 and float(rows[0][1]) == 5.0
    assert float(rows[0][2]) == pytest.approx(5e-3, rel=1e-6)


def test_signals_select_and_order_the_columns(tmp_path):
    """Sütun sırası istenen sıradır, ad .raw'daki yazımıyla çıkar. Sütunlar yer değiştirince değerler de yer değiştirir:
    V(in) tepe 5 V (PULSE(0 5 ...)), V(out) tepe 3.58834481239 (log)."""
    info = export_waveforms_csv(DATA / "ref26_flow_rc.raw", tmp_path / "x.csv", ["v(OUT)", "V( in )", "V(out)"])
    header, *rows = _read_csv(tmp_path / "x.csv")
    assert header == info["columns"] == ["time", "V(out)", "V(in)"]
    assert max(float(row[2]) for row in rows) == 5.0
    assert max(float(row[1]) for row in rows) == pytest.approx(3.58834481239, rel=1e-6)


def test_unknown_signal_is_refused_before_anything_is_written(tmp_path):
    target = tmp_path / "x.csv"
    with pytest.raises(ValueError, match=r"'I\(R9\)'.*Kayıtlı izler: \['V\(in\)', 'V\(out\)'\]"):
        export_waveforms_csv(DATA / "ref26_flow_rc.raw", target, ["V(out)", "I(R9)"])
    assert not target.exists()


def test_decimal_comma_changes_only_the_separators(tmp_path):
    export_waveforms_csv(DATA / "ref26_flow_rc.raw", tmp_path / "point.csv")
    export_waveforms_csv(DATA / "ref26_flow_rc.raw", tmp_path / "comma.csv", delimiter=";", decimal=",")
    header, *rows = _read_csv(tmp_path / "point.csv")
    comma_header, *comma_rows = _read_csv(tmp_path / "comma.csv", delimiter=";")
    assert comma_header == header
    assert not any("." in cell for row in comma_rows for cell in row)
    assert [[cell.replace(",", ".") for cell in row] for row in comma_rows] == rows
    assert rows[-1][0] == "0.003" and comma_rows[-1][0] == "0,003"  # `.tran 3m`: son örnek 3 ms


def test_tab_delimiter(tmp_path):
    export_waveforms_csv(DATA / "ref26_flow_rc.raw", tmp_path / "tab.csv", ["V(out)"], delimiter="\t")
    header, *rows = _read_csv(tmp_path / "tab.csv", delimiter="\t")
    assert header == ["time", "V(out)"] and len(rows) == _raw_points(DATA / "ref26_flow_rc.raw")


@pytest.mark.parametrize("kwargs", [{"delimiter": "|"}, {"decimal": ";"}, {"delimiter": ",", "decimal": ","}])
def test_invalid_separators_are_refused(tmp_path, kwargs):
    """Ondalık virgülle sütun ayırıcı virgül olursa `1,5` iki sütun okunur."""
    target = tmp_path / "x.csv"
    with pytest.raises(ValueError):
        export_waveforms_csv(DATA / "ref26_flow_rc.raw", target, **kwargs)
    assert not target.exists()


@given(values=st.one_of(
    hnp.arrays(np.float32, st.integers(1, 12), elements=st.floats(width=32)),
    hnp.arrays(np.float64, st.integers(1, 12), elements=st.floats()),
))
def test_text_column_round_trips_every_float(values):
    _assert_text_round_trips(values)


def _assert_text_round_trips(values):
    """Gidiş-dönüş: yazılan metin kendi türünde okununca aynı sayıdır (float32 ve float64, sonsuz dahil); NaN `nan`
    yazılır. NaN ayrı denetlenir: `nan` metnini float32'ye çeviren NumPy dönüşümü "invalid value" uyarısı verir."""
    is_nan = np.isnan(values)
    for decimal in (".", ","):
        text = _text_column(values, decimal)
        assert not (decimal == "," and any("." in cell for cell in text))
        point = np.char.replace(text, ",", ".")
        assert point[is_nan].tolist() == ["nan"] * int(is_nan.sum())
        assert np.array_equal(point[~is_nan].astype(values.dtype), values[~is_nan])


@pytest.mark.parametrize("values", [
    np.array([np.nan], dtype=np.float32),  # Hypothesis karşı örneği (2026-11-11): geri okuma dönüşümü uyarı veriyordu
    np.array([np.nan, 1.5, np.inf, -np.inf, 1e-45], dtype=np.float32),
    np.array([np.nan, 0.1, 1 / 3, 5e-324, sys.float_info.max], dtype=np.float64),
], ids=["nan32", "float32", "float64"])
def test_text_column_special_values(values):
    _assert_text_round_trips(values)


def test_float32_is_written_with_its_own_shortest_text():
    """LTspice izleri float32 kaydeder (ref26_flow_rc). 0.1'in float32'si float64'e genişletilirse 0.10000000149011612
    olur; hücreye kendi türünde geri okunan en kısa yazım (0.1) girer."""
    values = np.array([0.1, 2.5, -1e-5], dtype=np.float32)
    assert _text_column(values, ".").tolist() == ["0.1", "2.5", "-1e-05"]
    assert _text_column(values, ",").tolist() == ["0,1", "2,5", "-1e-05"]


# ------------------------------------------------------------------ araçlar
@pytest.fixture
def adapter(tmp_path):
    """Gerçek LTspice çalıştırmayan ama çalışma alanı mantığını kullanan adaptör."""
    adapter = LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path / "ws")
    adapter.workspace_dir.mkdir(parents=True, exist_ok=True)
    return adapter


def _put(adapter, circuit, fixture, netlist=None):
    """Gerçek LTspice kaydını çalışma alanına, adaptörün yazdığı adlarla koyar."""
    text = netlist if netlist is not None else (DATA / f"{fixture}.net").read_text(encoding="utf-8")
    (adapter.workspace_dir / f"{circuit}.net").write_text(text, encoding="utf-8")
    shutil.copy(DATA / f"{fixture}.log.txt", adapter.workspace_dir / f"{circuit}.log")
    shutil.copy(DATA / f"{fixture}.raw", adapter.workspace_dir / f"{circuit}.raw")


def _compare(adapter, **args):
    return handle_tool_call("compare_circuits", args, adapter=adapter)


def test_new_tools_are_offered_only_in_the_extended_set():
    """Varsayılan küme temel 10 araç kalır (docs/KILAVUZ.md "LLM Function Calling"); iki araç genişletilmiş kümededir."""
    base = [tool["name"] for tool in get_ltspice_tool_schemas()]
    extended = [tool["name"] for tool in get_ltspice_tool_schemas(include_extended=True)]
    assert len(base) == 10 and not {"compare_circuits", "export_waveforms_csv"} & set(base)
    assert extended[:10] == base and extended[-2:] == ["compare_circuits", "export_waveforms_csv"]


def test_compare_tool_reports_the_two_real_runs(adapter):
    """
    ref26_flow_rc (`.tran 3m`) ile ref26_rc_tstart (`.tran 0 3m 1m`) aynı RC devresidir; tek fark kayıt penceresi.
    Log satırları (LTspice 26.1.1): flow_rc "v_max: MAX(V(out))=3.58834481239 FROM 0 TO 0.003" ve "vr_max:
    MAX(V(in,out))=4.99999749785 FROM 0 TO 0.003"; rc_tstart "v_max: MAX(V(out))=3.58834481239 FROM 0.001 TO 0.003" ve
    "vr_max: MAX(V(in,out))=3.83727574348 FROM 0.001 TO 0.003". Elle: 3.83727574348 - 4.99999749785 = -1.16272175437;
    -1.16272175437 / 4.99999749785 = -%23.2544467. Yalnız rc_tstart'ta olanlar logun sırasıyla t_fall, t_rise (WHEN
    ölçümleri; ayrıştırıcı eşiği `<ad>_val` diye ekler, ltspice_llm/parser.py) ve v_avg.
    """
    _put(adapter, "full", "ref26_flow_rc")
    _put(adapter, "late", "ref26_rc_tstart")
    res = _compare(adapter, circuit_a="full", circuit_b="late")
    assert res["status"] == "SUCCESS" and (res["circuit_a"], res["circuit_b"]) == ("full", "late")
    assert _changes(res) == [("analysis", ".tran", ".tran 3m", ".tran 0 3m 1m")]
    assert res["changed_variable_count"] == 1 and res["single_variable"] is True

    rows = {row["name"]: row for row in res["measurements"]}
    assert set(rows) == {"v_max", "vr_max"}
    assert rows["v_max"] == {"name": "v_max", "a": 3.58834481239, "b": 3.58834481239, "delta": 0.0,
                             "relative_change_percent": 0.0, "same_definition": True}
    assert (rows["vr_max"]["a"], rows["vr_max"]["b"]) == (4.99999749785, 3.83727574348)
    assert rows["vr_max"]["delta"] == pytest.approx(-1.16272175437, abs=1e-11)
    assert rows["vr_max"]["relative_change_percent"] == pytest.approx(-23.2544467, abs=1e-6)
    assert rows["vr_max"]["same_definition"] is True
    assert res["only_in_a"] == [] and res["only_in_b"] == ["t_fall", "t_fall_val", "t_rise", "t_rise_val", "v_avg"]
    assert res["warnings"] == []
    json.dumps(res)  # araç sonucu modele JSON olarak gider


def test_compare_tool_flags_a_measurement_defined_differently(adapter):
    """GEMINI.md: önce/sonra karşılaştırmasında aynı düğümü ölç. vr_max bir devrede V(in,out), öbüründe V(in) ise iki
    değer aynı şeyi ölçmez; v_max'ın tanımı aynıdır. Ölçüm tanımı değişken sayılmaz: tek değişken R1."""
    _put(adapter, "ref", "ref26_flow_rc")
    _put(adapter, "probe", "ref26_flow_rc",
         netlist=RC.replace("MAX V(in,out)", "MAX V(in)").replace("R1 in out 1k", "R1 in out 2k"))
    res = _compare(adapter, circuit_a="ref", circuit_b="probe")
    assert _changes(res) == [("element", "R1", "R1 in out 1k", "R1 in out 2k")] and res["single_variable"] is True
    assert {row["name"]: row["same_definition"] for row in res["measurements"]} == {"v_max": True, "vr_max": False}
    assert len(res["warnings"]) == 1 and res["warnings"][0].count("'vr_max'") == 1


def test_compare_tool_flags_every_result_of_a_redefined_measurement(adapter):
    """Bir WHEN ölçümü logda birden çok sonuç verir (ref26_rc_tstart: t_rise ve ek sonucu). Tanım değişince hepsi başka
    şeyi ölçer; t_fall ve öbür ölçümlerin tanımı aynıdır."""
    netlist = (DATA / "ref26_rc_tstart.net").read_text(encoding="utf-8")
    _put(adapter, "ref", "ref26_rc_tstart")
    _put(adapter, "probe", "ref26_rc_tstart",
         netlist=netlist.replace("t_rise WHEN V(out)=3 RISE=1", "t_rise WHEN V(out)=2 RISE=1"))
    res = _compare(adapter, circuit_a="ref", circuit_b="probe")
    flags = {row["name"]: row["same_definition"] for row in res["measurements"]}
    from_t_rise = [name for name in flags if name.startswith("t_rise")]
    assert "t_rise" in from_t_rise and len(from_t_rise) > 1  # kayıtta ek sonuç gerçekten var
    assert {name for name, same in flags.items() if not same} == set(from_t_rise)
    assert res["changes"] == [] and len(res["warnings"]) == 2  # netlist aynı + t_rise tanımı farklı
    assert sum(w.count("'t_rise'") for w in res["warnings"]) == 1


@pytest.mark.parametrize("row, owner", [
    ("t_rise", "t_rise"),      # satır bir tanımın kendisi; "t" de önek olsa bile
    ("t_rise_val", "t_rise"),  # ek sonuç, adı önek olan en uzun tanımındır
    ("t_val", "t"),
    ("v_max", "v_max"),        # tanımı olmayan satır kendi adıdır
])
def test_measurement_row_belongs_to_the_longest_definition_it_extends(row, owner):
    """Ayrıştırıcı bir `.meas` için `<ad>_val` (WHEN eşiği), `<ad>_at`, `<ad>_deg` ve `<ad>_im` ek sonuçlarını üretir
    (ltspice_llm/parser.py). `.meas t` ile `.meas t_rise` birlikteyken t_rise_val t_rise'ındır: yalnız t_rise'ın tanımı
    değişirse t'nin satırı "aynı tanım" kalmalı (_definition_owner belgesi)."""
    assert _definition_owner(row, {"t", "t_rise"}) == owner


# tests/data/ref26_missing_model.log.txt'yi üreten devre (tests/test_integration_reference.py, 4. satır D1 b 0 1N4148)
MISSING_MODEL = "* d\nV1 a 0 1\nR1 a b 1k\nD1 b 0 1N4148\n.op\n.meas op vb FIND V(b)\n.end\n"


@pytest.mark.parametrize("order", [("ok", "broken"), ("broken", "ok")])
def test_compare_tool_names_the_circuit_whose_simulation_failed(adapter, order):
    """ref26_missing_model.log.txt (LTspice 26.1.1): 'missing_model.net(4): Undefined model "1n4148".' Uyarı hatalı
    devreyi adıyla ve logun hatasıyla söyler; öbür devrenin adı uyarılarda geçmez."""
    _put(adapter, "ok", "ref26_flow_rc")
    (adapter.workspace_dir / "broken.net").write_text(MISSING_MODEL, encoding="utf-8")
    shutil.copy(DATA / "ref26_missing_model.log.txt", adapter.workspace_dir / "broken.log")
    res = _compare(adapter, circuit_a=order[0], circuit_b=order[1])
    assert res["status"] == "SUCCESS"
    failed = [w for w in res["warnings"] if "simülasyonu hata verdi" in w]
    assert len(failed) == 1 and failed[0].count("'broken'") == 1
    assert failed[0].count('Undefined model "1n4148"') == 1
    assert sum(w.count("'ok'") for w in res["warnings"]) == 0


def test_compare_tool_warns_about_a_measurement_that_failed(adapter):
    """ref26_cmp_meas_fail (examples/14 CMP_MEAS_FAIL): ref26_flow_rc'nin devresi ve hiç gerçekleşmeyen v_never; log
    (LTspice 26.1.1) 'Measurement "v_never" FAIL'ed'. Ölçüm tanımı değişken değildir: devreler aynı."""
    _put(adapter, "ref", "ref26_flow_rc")
    _put(adapter, "probe", "ref26_cmp_meas_fail")
    res = _compare(adapter, circuit_a="ref", circuit_b="probe")
    assert res["changes"] == [] and [m["name"] for m in res["measurement_changes"]] == ["v_never"]
    assert [row["name"] for row in res["measurements"]] == ["v_max", "vr_max"]
    failed = [w for w in res["warnings"] if "başarısız ölçüm" in w]
    assert len(failed) == 1 and failed[0].count("'probe'") == 1 and failed[0].count("v_never") == 1
    assert sum(w.count("'ref'") for w in res["warnings"]) == 0


def test_compare_tool_warns_when_no_requested_measurement_is_shared(adapter):
    _put(adapter, "full", "ref26_flow_rc")
    _put(adapter, "late", "ref26_rc_tstart")
    res = _compare(adapter, circuit_a="full", circuit_b="late", measurements=["nope"])
    assert res["status"] == "SUCCESS" and res["measurements"] == [] and len(res["warnings"]) == 2
    assert sum(w.count("['nope']") for w in res["warnings"]) == 1
    assert sum(w.count("ortak .meas ölçümü yok") for w in res["warnings"]) == 1


@pytest.mark.parametrize("netlist, count, fragment", [
    (RC.replace("R1 in out 1k", "R1 in out 2k").replace("C1 out 0 1u", "C1 out 0 2u"), 2, "2 değişken birden"),
    (RC.replace("* rc flow fixture", "* same circuit"), 0, "netlisti aynı"),
])
def test_compare_tool_warns_when_the_difference_has_no_single_cause(adapter, netlist, count, fragment):
    _put(adapter, "ref", "ref26_flow_rc")
    _put(adapter, "other", "ref26_flow_rc", netlist=netlist)
    res = _compare(adapter, circuit_a="ref", circuit_b="other")
    assert res["status"] == "SUCCESS"
    assert res["changed_variable_count"] == count and res["single_variable"] is False
    assert len(res["warnings"]) == 1 and res["warnings"][0].count(fragment) == 1


def test_compare_tool_limits_the_table_to_the_requested_measurements(adapter):
    _put(adapter, "full", "ref26_flow_rc")
    _put(adapter, "late", "ref26_rc_tstart")
    res = _compare(adapter, circuit_a="full", circuit_b="late", measurements=["VR_MAX", "t_rise"])
    assert [row["name"] for row in res["measurements"]] == ["vr_max"]
    assert len(res["warnings"]) == 1 and res["warnings"][0].count("t_rise") == 1  # yalnız 'late' devresinde var


@pytest.mark.parametrize("order", [("single", "swept"), ("swept", "single")])
def test_compare_tool_refuses_a_stepped_run(adapter, order):
    """Steplenmiş koşumun `measurements` değeri adımların ortalamasıdır (ref26_rc_tstart_step logu: v_max 3.58834481239
    ve 2.69109368324); iki ortalamanın farkı bir bileşenin etkisi değildir."""
    _put(adapter, "single", "ref26_rc_tstart")
    _put(adapter, "swept", "ref26_rc_tstart_step")
    res = _compare(adapter, circuit_a=order[0], circuit_b=order[1])
    assert res["status"] == "ERROR" and res["message"].count("['swept']") == 1
    assert "measurements" not in res and "changes" not in res


def test_compare_tool_argument_errors(adapter):
    _put(adapter, "full", "ref26_flow_rc")
    (adapter.workspace_dir / "nolog.net").write_text(RC, encoding="utf-8")
    cases = [
        ({}, "'circuit_a' ve 'circuit_b' gereklidir."),
        ({"circuit_a": "full"}, "'circuit_a' ve 'circuit_b' gereklidir."),
        ({"circuit_a": "full", "circuit_b": "yok"}, f"Devre dosyası bulunamadı: {adapter.workspace_dir / 'yok.net'}"),
        ({"circuit_a": "full", "circuit_b": "nolog"},
         "Devre simülasyon logu bulunamadı: 'nolog'. Önce devreyi simüle edin."),
    ]
    for args, message in cases:
        assert handle_tool_call("compare_circuits", args, adapter=adapter) == {"status": "ERROR", "message": message}
    same = _compare(adapter, circuit_a="full", circuit_b="full")
    assert same["status"] == "ERROR" and same["message"].count("'full'") == 1


def test_compare_tool_reads_only_the_workspace(adapter, tmp_path):
    """Devre adı dosya yoluna dönüşmeden temizlenir (handle_tool_call): `../evil` çalışma alanının dışını okutmaz."""
    _put(adapter, "full", "ref26_flow_rc")
    (tmp_path / "evil.net").write_text(RC.replace("1k", "2k"), encoding="utf-8")
    shutil.copy(DATA / "ref26_flow_rc.log.txt", tmp_path / "evil.log")
    res = _compare(adapter, circuit_a="full", circuit_b="../evil")
    assert res == {"status": "ERROR", "message": f"Devre dosyası bulunamadı: {adapter.workspace_dir / 'evil.net'}"}


def test_export_tool_writes_the_csv_into_the_workspace(adapter):
    _put(adapter, "rc", "ref26_flow_rc")
    res = handle_tool_call("export_waveforms_csv", {"circuit_name": "rc"}, adapter=adapter)
    csv_path = adapter.workspace_dir / "rc.csv"
    header, *rows = _read_csv(csv_path)
    assert res["status"] == "SUCCESS" and res["csv_saved_at"] == str(csv_path)
    assert res["columns"] == header == _raw_variables(DATA / "ref26_flow_rc.raw")
    assert res["rows"] == len(rows) == _raw_points(DATA / "ref26_flow_rc.raw")
    assert res["steps"] == 1 and res["analysis"] == "tran"
    assert len(json.dumps(res)) < 600  # veri araç sonucunda dönmez


def test_export_tool_accepts_model_style_arguments(adapter):
    """Model tek sinyali metin olarak, sekmeyi "tab" diye gönderebilir."""
    _put(adapter, "rc", "ref26_flow_rc")
    res = handle_tool_call("export_waveforms_csv",
                           {"circuit_name": "rc", "signals": "v(out)", "delimiter": "tab", "decimal": ","},
                           adapter=adapter)
    header, *rows = _read_csv(adapter.workspace_dir / "rc.csv", delimiter="\t")
    assert res["status"] == "SUCCESS" and res["columns"] == header == ["time", "V(out)"]
    assert rows[-1][0] == "0,003" and len(rows) == res["rows"]


@pytest.mark.parametrize("args, fragment", [
    ({"circuit_name": "yok"}, ".raw dosyası bulunamadı"),
    ({"circuit_name": "rc", "signals": ["I(R9)"]}, "Kayıtlı izler: ['V(in)', 'V(out)']"),
    ({"circuit_name": "rc", "decimal": ","}, "delimiter=';' kullanın"),
    # "delimiter" sözcüğü ondalık-virgül çatışması iletisinde de geçer: geçersiz ayırıcıyı yalnız bu parça ayırt eder
    ({"circuit_name": "rc", "delimiter": "|"}, "değerlerinden biri olmalı: '|'"),
])
def test_export_tool_errors_leave_no_file(adapter, args, fragment):
    _put(adapter, "rc", "ref26_flow_rc")
    res = handle_tool_call("export_waveforms_csv", args, adapter=adapter)
    assert res["status"] == "ERROR" and res["message"].count(fragment) == 1
    assert list(adapter.workspace_dir.glob("*.csv")) == []


def test_remove_circuit_also_removes_the_exported_csv(adapter):
    """remove_circuit bir devrenin çalışma alanındaki bütün dosyalarını siler (belgesi: .net/.log/.raw/.db/.png/.csv)."""
    _put(adapter, "rc", "ref26_flow_rc")
    _put(adapter, "keep", "ref26_flow_rc")
    for circuit in ("rc", "keep"):
        assert handle_tool_call("export_waveforms_csv", {"circuit_name": circuit}, adapter=adapter)["status"] == "SUCCESS"
    assert "rc.csv" in adapter.remove_circuit("rc")
    assert sorted(path.name for path in adapter.workspace_dir.glob("*.csv")) == ["keep.csv"]


def test_workspace_cleanup_also_removes_the_exported_csv(adapter):
    """cleanup_workspace(max_age_seconds=0) çalışma alanındaki koşum dosyalarının hepsini siler (CHANGELOG 2026-11-08:
    "belgedeki gibi hepsini siler"); dışa aktarılan CSV de bir koşum dosyasıdır."""
    _put(adapter, "rc", "ref26_flow_rc")
    assert handle_tool_call("export_waveforms_csv", {"circuit_name": "rc"}, adapter=adapter)["status"] == "SUCCESS"
    assert (adapter.workspace_dir / "rc.csv").exists()
    adapter.cleanup_workspace(max_age_seconds=0)
    assert list(adapter.workspace_dir.glob("rc.*")) == []


# ------------------------------------------------------------------ GND ve µ yazımları (test-reviewer 2026-11-11)
_RC_SPELL = "* t\nV1 in 0 1\nR1 in out 1k\nC1 out 0 1u\n.param c=1u\n.tran 1m\n.end\n"


@pytest.mark.parametrize("old, new", [
    ("C1 out 0 1u", "C1 out GND 1u"),   # ADI README.md: "`GND` is a synonym for `0`"
    ("C1 out 0 1u", "C1 out gnd 1u"),   # ADI README: "Node Naming: Names are case-insensitive"
    ("C1 out 0 1u", "C1 out 0 1µ"),     # SPICE-SYNTAX-REFERENCE.md "Convert 'µ' to 'u'": µ (0xB5) LTspice'ta 1e-6
    ("C1 out 0 1u", "C1 out 0 1μ"),     # Yunanca mu (U+03BC): casefold µ'yü bu harfe çevirir
    (".param c=1u", ".param c=1µ"),
])
def test_ground_and_micro_spellings_are_not_differences(old, new):
    assert compare_netlists(_RC_SPELL, _RC_SPELL.replace(old, new))["changes"] == []


def test_ground_synonym_does_not_hide_a_real_node_change():
    result = compare_netlists(_RC_SPELL, _RC_SPELL.replace("C1 out 0 1u", "C1 out in 1u"))
    assert [change["name"] for change in result["changes"]] == ["C1"]


def test_compare_tool_lists_measurements_only_in_a(adapter):
    """Yukarıdaki iki gerçek koşum ters sırada: rc_tstart'a özgü ölçümler bu kez only_in_a'da, logun sırasıyla
    (docs/GUIDE.md "Comparing two circuits": listeler logların sırasını korur)."""
    _put(adapter, "full", "ref26_flow_rc")
    _put(adapter, "late", "ref26_rc_tstart")
    res = _compare(adapter, circuit_a="late", circuit_b="full")
    assert res["only_in_a"] == ["t_fall", "t_fall_val", "t_rise", "t_rise_val", "v_avg"] and res["only_in_b"] == []


def test_compare_tool_warns_about_a_failed_measurement_in_circuit_a(adapter):
    """test_compare_tool_warns_about_a_measurement_that_failed ters sırada: başarısız ölçüm a devresindeyken de uyarılır
    ve uyarı doğru devreyi adlandırır (test-reviewer 2026-11-11: yalnız b sırası sınanıyordu)."""
    _put(adapter, "ref", "ref26_flow_rc")
    _put(adapter, "probe", "ref26_cmp_meas_fail")
    res = _compare(adapter, circuit_a="probe", circuit_b="ref")
    failed = [w for w in res["warnings"] if "başarısız ölçüm" in w]
    assert len(failed) == 1 and failed[0].count("'probe'") == 1 and failed[0].count("v_never") == 1
    assert sum(w.count("'ref'") for w in res["warnings"]) == 0


@pytest.mark.parametrize("args, expected", [
    ({"circuit_name": "rc", "signals": ["I(R9)"]},
     "Sinyal .raw dosyasında yok: 'I(R9)'. Kayıtlı izler: ['V(in)', 'V(out)']"),
    ({"circuit_name": "rc", "delimiter": "|"}, f"delimiter {list(CSV_DELIMITERS)} değerlerinden biri olmalı: '|'"),
    ({"circuit_name": "rc", "decimal": ";"}, f"decimal {list(CSV_DECIMALS)} değerlerinden biri olmalı: ';'"),
    ({"circuit_name": "rc", "decimal": ","},
     "Ondalık ayırıcı virgülken sütun ayırıcı virgül olamaz; delimiter=';' kullanın."),
])
def test_export_tool_error_messages_name_the_rejected_value(adapter, args, expected):
    """İletinin tamamı: reddedilen değer ve kabul edilenler (test-reviewer 2026-11-11: parça araması iletinin geri
    kalanını sınamıyordu, geçersiz decimal dalı hiç sınanmıyordu). Kayıtlı izler ref26_flow_rc.net'in `.save`'inden."""
    _put(adapter, "rc", "ref26_flow_rc")
    assert handle_tool_call("export_waveforms_csv", args, adapter=adapter) == {"status": "ERROR", "message": expected}
