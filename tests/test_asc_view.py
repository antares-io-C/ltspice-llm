"""
Şema görüntüleme (`ltspice_llm.asc_view`) ve monitörün şema sayfası (`ltspice_llm.monitor`).

Beklenen değerlerin kaynakları:
- ADI SCHEMATIC-REFERENCE (``%LOCALAPPDATA%/LTspice/reference``):
  - "Pin World-Coordinate Calculation": döndürme ve aynalama formülleri.
  - "Rotation Examples": R90 direncin pinleri.
  - "Complete Example: RC Low-Pass Filter": şema metni ``ADI_RC``'de birebir.
  - "Components (Symbols)": R90 gerilim kaynağının ``VBottom``/``VTop`` pencereleri.
  - "Wires": üç ya da daha çok kablo buluşunca kavşak noktası.
- LTspice 26.1.1 ``-netlist`` (examples/23, 2026-11-20): bölünmemiş T ve eğik kablonun ortasına inen uç bağlanır, ucu
  olmayan kesişme bağlanmaz. Kavşak testindeki T, eğik ve kesişme vakalarının kaynağı.
- LTspice 26.1.1 ``lib/sym``: ``ind.asy`` ve ``polcap.asy``'nin ARC satırları ``IND_ARCS``/``POLCAP_ARC``'ta birebir. Yay
  yönünün gerekçesi `ltspice_llm.asc_view` belgesinde.
- SVG 1.1 §8.3.8: ``sweep-flag`` 1, y aşağıyken ekranda saat yönüdür.

Sayısal beklentiler elle hesaplandı; hesap testin yanında. Kütüphane sembolü gereken testler, testte yazılan küçük
``.asy`` metinleriyle çalışır (CI'da LTspice yok). Gerçek kütüphaneyle yapılan tek test, LTspice verisi yoksa atlanır.
"""

import codecs
import http.client
import json
import math
import os
import posixpath
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ltspice_llm import asc_view, events, monitor, reference
from ltspice_llm.asc_view import ORIENTATIONS, apply, arc_geometry, arc_path, junctions, parse_asc, text_layout
from ltspice_llm.monitor_page import PAGE_JS, SCHEMA_HTML, SCHEMA_JS
from ltspice_llm.schematic import read_asc_file, transform_pin, write_asc_file
from ltspice_llm.symbols import library_symbol_dir

NS = "{http://www.w3.org/2000/svg}"

#: Sayfa politikası yönerge yönerge (monitor belge dizgisi): betik yalnız sunucunun kendi dosyası; veri yalnız aynı
#: sunucudan (fetch -> connect-src); stil sayfanın <style> bloğu; sayfa görsel yüklemez; <base> ve form ele geçirilemez
#: (bu ikisi default-src'ye düşmez, ayrıca yazılmalı).
PAGE_POLICY = {"default-src": "'none'", "script-src": "'self'", "connect-src": "'self'", "style-src": "'unsafe-inline'",
               "img-src": "'self' data:", "base-uri": "'none'", "form-action": "'none'"}

ADI_RC = """Version 4.1
SHEET 1 880 340
WIRE -32 96 -48 96
WIRE 16 96 -32 96
WIRE 128 96 96 96
WIRE 144 96 128 96
WIRE -48 112 -48 96
WIRE 144 112 144 96
WIRE -48 208 -48 192
WIRE 144 208 144 176
FLAG -48 208 0
FLAG 144 208 0
FLAG -32 96 in
FLAG 128 96 out
SYMBOL voltage -48 96 R0
SYMATTR InstName V1
SYMATTR Value AC 1
SYMBOL cap 128 112 R0
SYMATTR InstName C1
SYMATTR Value 100n
SYMBOL res 112 80 R90
WINDOW 0 0 56 VBottom 2
WINDOW 3 32 56 VTop 2
SYMATTR InstName R1
SYMATTR Value 1k
TEXT -48 256 Left 2 !.ac dec 100 1 10Meg
"""

#: LTspice 26.1.1 lib/sym/ind.asy
IND_ARCS = [(0, 40, 32, 72, 4, 68, 4, 44), (0, 64, 32, 96, 16, 96, 4, 68), (0, 16, 32, 48, 4, 44, 16, 16)]
#: LTspice 26.1.1 lib/sym/polcap.asy
POLCAP_ARC = (-16, 36, 48, 100, 32, 40, 0, 40)

#: Testte yazılan semboller: pencere konumları LTspice'ınkilerle aynı (res.asy WINDOW 0 36 40 / 3 36 76)
TEST_SYMBOLS = {
    "res": "Version 4\nSymbolType CELL\nLINE Normal 16 16 16 96\nWINDOW 0 36 40 Left 2\nWINDOW 3 36 76 Left 2\n"
           "SYMATTR Value R\nPIN 16 16 NONE 0\nPINATTR PinName A\nPIN 16 96 NONE 0\nPINATTR PinName B\n",
    "cap": "Version 4\nSymbolType CELL\nLINE Normal 0 28 32 28\nWINDOW 0 24 8 Left 2\nWINDOW 3 24 56 Left 2\n",
    "voltage": "Version 4\nSymbolType CELL\nCIRCLE Normal -32 24 32 88\nWINDOW 0 24 16 Left 2\nWINDOW 3 24 96 Left 2\n",
    "block": "Version 4\nSymbolType BLOCK\nRECTANGLE Normal -48 -32 48 32\nTEXT 0 0 Center 2 IC\n"
             "WINDOW 0 0 -40 Bottom 2\nPIN -48 0 LEFT 8\nPINATTR PinName VIN\nPIN 48 0 NONE 0\nPINATTR PinName X\n",
    "ring": "Version 4\nSymbolType CELL\nARC Normal 0 0 32 32 32 16 32 16\n",
    "ind": "Version 4\nSymbolType CELL\n" + "".join(f"ARC Normal {' '.join(map(str, arc))}\n" for arc in IND_ARCS),
    "pins4": "Version 4\nSymbolType BLOCK\nRECTANGLE Normal -48 -32 48 32\nPIN 48 0 RIGHT 8\nPINATTR PinName SAG\n"
             "PIN 0 -32 TOP 8\nPINATTR PinName UST\nPIN 0 32 BOTTOM 8\nPINATTR PinName ALT\n",
}


def _loader(name):
    text = TEST_SYMBOLS.get(name)
    return asc_view.parse_asy(text) if text is not None else None


def _render(text, loader=_loader):
    return asc_view.render_drawing(parse_asc(text), loader)


def _texts(svg):
    """(içerik, öznitelikler): çok satırlı yazının satırları \\n ile birleşir."""
    root = ET.fromstring(svg)  # geçerli XML olmalı
    return [("\n".join(span.text or "" for span in node.iter(NS + "tspan")), node.attrib)
            for node in root.iter(NS + "text")]


def _text(svg, content):
    found = [attrs for text, attrs in _texts(svg) if text == content]
    assert len(found) == 1, (content, _texts(svg))
    return found[0]


# --------------------------------------------------------------------------- geometri
@given(st.sampled_from(sorted(ORIENTATIONS)), st.integers(-500, 500), st.integers(-500, 500),
       st.integers(-500, 500), st.integers(-500, 500))
def test_orientation_matrices_match_the_pin_transform(orient, sx, sy, px, py):
    """Çizim, pinleri yerleştiren dönüşümle aynı olmalı (ADI "Pin World-Coordinate Calculation" = transform_pin)."""
    assert apply(ORIENTATIONS[orient], sx, sy, px, py) == transform_pin(sx, sy, px, py, orient)


def test_r90_resistor_pins_from_the_adi_rotation_example():
    """ADI "Rotation Examples": ``SYMBOL res 48 96 R90`` -> pin A (32, 112), pin B (-48, 112)."""
    assert apply(ORIENTATIONS["R90"], 48, 96, 16, 16) == (32, 112)
    assert apply(ORIENTATIONS["R90"], 48, 96, 16, 96) == (-48, 112)


@pytest.mark.parametrize("coords,start,sweep", [
    # merkez (16,56), r=16; (4,68) güneybatı 225°, (4,44) kuzeybatı 135°: saat yönünün tersine 225°->495° = 270°
    (IND_ARCS[0], 225.0, 270.0),
    # merkez (16,80); (16,96) güney 270°, (4,68) kuzeybatı 135°: 270°->495° = 225°
    (IND_ARCS[1], 270.0, 225.0),
    # merkez (16,32); (4,44) güneybatı 225°, (16,16) kuzey 90°: 225°->450° = 225°
    (IND_ARCS[2], 225.0, 225.0),
])
def test_inductor_arcs_bulge_away_from_their_joints(coords, start, sweep):
    """Bobinin yayları pin ekseninin (x=16) sağına taşar; eklem noktaları (x=4) solda kalır."""
    arc = arc_geometry(coords)
    assert math.degrees(arc.start) % 360 == pytest.approx(start)
    assert math.degrees(arc.sweep) == pytest.approx(sweep)
    assert arc.point(0.5)[0] > 16


def test_inductor_middle_arc_passes_through_its_east_point():
    """IND_ARCS[0]: 225° + 270°/2 = 360° -> doğu, (16+16, 56)."""
    assert arc_geometry(IND_ARCS[0]).point(0.5) == pytest.approx((32.0, 56.0))


def test_polarized_capacitor_plate_is_the_top_of_its_circle():
    """merkez (16,68), r=32; (32,40) açısı atan2(28,16)=60,255°, (0,40) 119,745°: tepesi (16,36) düz plakaya (y=28)
    bakan 59,49°'lik yay."""
    arc = arc_geometry(POLCAP_ARC)
    assert math.degrees(arc.sweep) == pytest.approx(2 * (90 - math.degrees(math.atan2(28, 16))))
    assert arc.point(0.5) == pytest.approx((16.0, 36.0))


def test_arc_path_draws_counterclockwise_on_screen():
    """IND_ARCS[0]: başlangıç merkez + 16·(-1,1)/√2 = (4,686; 67,314), bitiş (4,686; 44,686); 270° > 180°: büyük yay;
    ekranda saat yönünün tersi: sweep-flag 0."""
    assert arc_path(arc_geometry(IND_ARCS[0])) == "M4.686 67.314A16 16 0 1 0 4.686 44.686"


def test_partial_arcs_of_a_symbol_are_drawn_as_paths():
    """Bobinin üç yayı tam elips değil, yol olarak çizilir; ilki `test_arc_path_draws_counterclockwise_on_screen`'deki."""
    group = [g for g in ET.fromstring(_render("SYMBOL ind 0 0 R0\n").svg).iter(NS + "g") if g.get("class") == "sym"]
    assert [child.tag for child in group[0]] == [NS + "path"] * 3
    assert group[0][0].get("d") == "M4.686 67.314A16 16 0 1 0 4.686 44.686"


def test_elliptical_arc_points_lie_on_their_ellipse():
    """rx=32, ry=16, merkez (32,16): (1,-1)/√2 yönündeki ışın elipsi (t/√2)²·(1/32² + 1/16²) = 1 -> t/√2 = 32/√5'te
    keser. (64,16) doğu ve (0,16) batı arasındaki saat yönünün tersine yarım elipsin ortası tepedir: (32, 0)."""
    point = asc_view.ArcGeometry(32, 16, 32, 16, math.pi / 4, 1.0).point(0)
    assert point == pytest.approx((32 + 32 / math.sqrt(5), 16 - 32 / math.sqrt(5)))
    assert arc_geometry((0, 0, 64, 32, 64, 16, 0, 16)).point(0.5) == pytest.approx((32.0, 0.0))


def test_arc_with_the_same_start_and_end_ray_is_a_full_ellipse():
    svg = _render("SYMBOL ring 0 0 R0\n").svg
    group = [g for g in ET.fromstring(svg).iter(NS + "g") if g.get("class") == "sym"]
    assert [child.tag for child in group[0]] == [NS + "ellipse"]


@pytest.mark.parametrize("wires,dots", [
    ([(0, 0, 32, 0), (0, 0, 0, 32), (0, 0, -32, 0)], [(0, 0)]),            # üç uç
    ([(0, 0, 32, 0), (0, 0, 0, 32)], []),                                  # köşe: iki uç
    ([(-32, 0, 32, 0), (0, 0, 0, 32)], [(0, 0)]),                          # T: bir uç + ortasından geçen (LTspice bağlar)
    ([(-32, 0, 32, 0), (0, -32, 0, 32)], []),                              # kesişme: uç yok (LTspice bağlamaz)
    ([(0, 0, 32, 0), (0, 0, 0, 32), (0, 0, -32, 0), (0, 0, 0, -32)], [(0, 0)]),
    ([(-32, -32, 32, 32), (0, 0, 0, 64)], [(0, 0)]),                       # eğik kablonun ortasına uç (LTspice bağlar)
])
def test_junction_dots_where_three_or_more_wires_meet(wires, dots):
    assert junctions(wires) == dots


# --------------------------------------------------------------------------- yazı hizası
@pytest.mark.parametrize("just,orient,expected", [
    ("Left", "R0", (False, "start", "middle")),
    # ADI "Components": R90 gerilim kaynağının adı gövdenin üstünde: VBottom -> yatay, alta hizalı
    ("VBottom", "R90", (False, "middle", "bottom")),
    ("VTop", "R90", (False, "middle", "top")),
    # R180 yerel +x'i sola çevirir (world_x = sx - px): soldan sağa okunan yazı çapanın soluna uzanır -> sağa hizalı
    ("Left", "R180", (False, "end", "middle")),
    ("Left", "M0", (False, "end", "middle")),
    # R90 yerel +x'i aşağı çevirir (world_y = sy + px): aşağıdan yukarı okunan yazı çapada biter
    ("Left", "R90", (True, "end", "middle")),
    # R270 yerel +x'i yukarı çevirir (world_y = sy - px): aşağıdan yukarı okunan yazı çapada başlar
    ("Left", "R270", (True, "start", "middle")),
    # R90 yerel +y'yi sola çevirir: harflerin üstü sola bakan düşey yazıda alt kenar çapada
    ("Top", "R90", (True, "middle", "bottom")),
    ("Invisible", "R0", None),
    ("Sideways", "R0", None),
])
def test_text_turns_with_the_symbol_but_never_reads_upside_down(just, orient, expected):
    assert text_layout(just, ORIENTATIONS[orient]) == expected


# --------------------------------------------------------------------------- çizim
def test_adi_rc_example_is_parsed_line_by_line():
    drawing = parse_asc(ADI_RC)
    assert len(drawing.wires) == 8
    assert drawing.flags == [(-48, 208, "0"), (144, 208, "0"), (-32, 96, "in"), (128, 96, "out")]
    assert [(p.name, p.x, p.y, p.orient) for p in drawing.symbols] == [
        ("voltage", -48, 96, "R0"), ("cap", 128, 112, "R0"), ("res", 112, 80, "R90")]
    resistor = drawing.symbols[2]
    assert resistor.windows == {0: (0, 56, "VBottom", 2), 3: (32, 56, "VTop", 2)}
    assert resistor.attrs == {"InstName": "R1", "Value": "1k"}
    assert [(t.x, t.y, t.just, t.size, t.text) for t in drawing.texts] == [(-48, 256, "Left", 2, "!.ac dec 100 1 10Meg")]


def test_rotated_resistor_labels_sit_above_and_below_its_body():
    """R1 (112, 80) R90: InstName yerel (0,56) -> (112-56, 80+0) = (56, 80), alta hizalı (gövdenin üstünde);
    Value yerel (32,56) -> (112-56, 80+32) = (56, 112), üste hizalı (altında)."""
    svg = _render(ADI_RC).svg
    name, value = _text(svg, "R1"), _text(svg, "1k")
    assert (name["x"], name["y"], name["text-anchor"], name["dominant-baseline"]) == ("56", "80", "middle",
                                                                                      "text-after-edge")
    assert (value["x"], value["y"], value["text-anchor"], value["dominant-baseline"]) == ("56", "112", "middle",
                                                                                         "text-before-edge")
    transforms = [g.get("transform") for g in ET.fromstring(svg).iter(NS + "g") if g.get("class") == "sym"]
    assert transforms == ["matrix(1 0 0 1 -48 96)", "matrix(1 0 0 1 128 112)", "matrix(0 1 -1 0 112 80)"]


def test_symbol_default_window_and_attribute_fallback():
    """Şemada WINDOW yoksa sembolünki kullanılır; Value yoksa sembolün Value'su (res: "R"). Boş öznitelik yazılmaz."""
    svg = _render("SYMBOL res 0 0 R0\nWINDOW 39 0 120 Left 2\nSYMATTR InstName R9\nSYMATTR SpiceLine \n").svg
    assert (_text(svg, "R9")["x"], _text(svg, "R9")["y"]) == ("36", "40")
    assert (_text(svg, "R")["x"], _text(svg, "R")["y"]) == ("36", "76")
    assert len(_texts(svg)) == 2


def test_pin_labels_go_inside_the_body_and_follow_a_mirror():
    """block: PIN -48 0 LEFT 8 -> ad yerel (-40, 0)'da sola hizalı. M0 (world_x = sx - px): (100+40, 0), sağa hizalı
    (aynalı gövdenin içine, sola uzanır). NONE konumlu pinin adı yazılmaz."""
    plain = _text(_render("SYMBOL block 100 0 R0\n").svg, "VIN")
    mirrored = _text(_render("SYMBOL block 100 0 M0\n").svg, "VIN")
    assert (plain["x"], plain["text-anchor"]) == ("60", "start")
    assert (mirrored["x"], mirrored["text-anchor"]) == ("140", "end")
    assert all(text != "X" for text, _ in _texts(_render("SYMBOL block 100 0 R0\n").svg))


def test_right_top_and_bottom_pin_labels_go_inside_the_body():
    """PIN 48 0 RIGHT 8: ad (40, 0)'da sağa hizalı. TOP 8 (0,-32): (0,-24)'te üste hizalı, pinin altında. BOTTOM 8 (0,32):
    (0,24)'te alta hizalı, pinin üstünde."""
    svg = _render("SYMBOL pins4 0 0 R0\n").svg
    right, top, bottom = _text(svg, "SAG"), _text(svg, "UST"), _text(svg, "ALT")
    assert (right["x"], right["y"], right["text-anchor"]) == ("40", "0", "end")
    assert (top["x"], top["y"], top["dominant-baseline"]) == ("0", "-24", "text-before-edge")
    assert (bottom["x"], bottom["y"], bottom["dominant-baseline"]) == ("0", "24", "text-after-edge")


def test_vertical_text_reads_from_bottom_to_top():
    """Modül belgesi: V önekli yazı aşağıdan yukarı okunur, yani çapası etrafında -90° döndürülmüş yatay yazıdır."""
    assert _text(_render("TEXT 10 20 VLeft 2 ;dikey\n").svg, "dikey")["transform"] == "rotate(-90 10 20)"


def test_bottom_aligned_lines_grow_upward():
    """Alta hizalı çok satırlı yazının son satırı çapada durur: ilk satır yukarı kayar, sonraki aynı adımla aşağı iner."""
    spans = [float(s.get("dy")) for s in ET.fromstring(_render("TEXT 0 0 Bottom 2 ;a\\nb\n").svg).iter(NS + "tspan")]
    assert spans[0] < 0 < spans[1] and spans[0] == -spans[1]


def test_style_number_other_than_zero_draws_a_dashed_line():
    lines = list(ET.fromstring(_render("LINE Normal 0 0 64 0 2\nLINE Normal 0 32 64 32\n").svg).iter(NS + "line"))
    assert [line.get("stroke-dasharray") is not None for line in lines] == [True, False]


def test_io_pin_and_data_flag():
    """IOPIN aynı noktadaki ağ etiketine bir port şekli ekler; DATAFLAG ifadesi tırnaksız, noktanın 4 birim sağında."""
    svg = _render('FLAG 0 0 IN\nIOPIN 0 0 In\nDATAFLAG 64 0 "V(out)"\n').svg
    assert [p.get("class") for p in ET.fromstring(svg).iter(NS + "path")] == ["io"]
    assert _text(svg, "IN")["class"] == "t-flag"
    assert (_text(svg, "V(out)")["class"], _text(svg, "V(out)")["x"]) == ("t-data", "68")


def test_comments_and_directives_are_escaped_and_split_into_lines():
    text = "TEXT 0 0 Left 2 ;a <b> & c\\nikinci satır\nTEXT 0 64 Left 2 !.tran 1m\n"
    svg = _render(text).svg
    comment = _text(svg, "a <b> & c\nikinci satır")  # ElementTree kaçışı çözdü: metin olarak duruyor
    assert comment["class"] == "t-cmt"
    assert _text(svg, ".tran 1m")["class"] == "t-dir"
    assert "<b>" not in svg and svg.count("&lt;b&gt;") == 1


#: XML 1.0'ın Char kuralında olmayan karakterler (W3C XML 1.0 §2.2): kontrol karakterleri ve U+FFFE/U+FFFF. UTF-16
#: yazılmış bir .asc bunları taşıyabilir; bozuk SVG'yi tarayıcı hiç çizmez (test-reviewer, 2026-11-19).
@pytest.mark.parametrize("value", ["a\x02b", "a￿b", "a￾b"])
@pytest.mark.parametrize("schematic", [
    "SYMBOL res 0 0 R0\nSYMATTR InstName R1\nSYMATTR Value {}\n",   # öznitelik yazısı ve <title>
    "SYMBOL res 0 0 R0\nSYMATTR InstName {}\n",
    "FLAG 0 0 {}\n",
    "TEXT 0 0 Left 2 ;{}\n",
])
def test_characters_xml_cannot_carry_are_dropped(schematic, value):
    svg = _render(schematic.format(value)).svg
    assert [text for text, _ in _texts(svg)].count("ab") == 1


@given(st.text())
def test_any_text_in_a_schematic_gives_well_formed_svg(value):
    svg = _render(f"SYMBOL res 0 0 R0\nSYMATTR InstName {value}\nSYMATTR Value {value}\nFLAG 0 0 n{value}\n"
                  f"TEXT 0 64 Left 2 ;{value}\n").svg
    ET.fromstring(svg)


def test_ground_and_net_label_placement():
    """Toprak bir üçgen; ağ etiketi kablonun gelmediği yana yazılır (çapadan 4-6 birim uzakta)."""
    svg = _render("WIRE 0 0 64 0\nWIRE 64 0 96 0\nWIRE 128 0 128 64\nWIRE 200 0 264 0\nFLAG 64 0 mid\nFLAG 96 0 end\n"
                  "FLAG 128 64 low\nFLAG 200 0 start\nFLAG 0 0 0\n").svg
    grounds = [p.get("d") for p in ET.fromstring(svg).iter(NS + "path") if p.get("class") == "ground"]
    assert grounds == ["M-16 0H16L0 16Z"]
    mid, end, low, start = _text(svg, "mid"), _text(svg, "end"), _text(svg, "low"), _text(svg, "start")
    assert (mid["x"], mid["y"], mid["dominant-baseline"]) == ("64", "-4", "text-after-edge")  # iki yanda kablo: üst
    assert (end["x"], end["y"], end["text-anchor"]) == ("102", "0", "start")                  # soldan gelen: sağ
    assert (start["x"], start["y"], start["text-anchor"]) == ("194", "0", "end")              # sağdan gelen: sol
    assert (low["x"], low["y"], low["dominant-baseline"]) == ("128", "68", "text-before-edge")  # yukarıdan: alt


def test_missing_symbol_is_drawn_as_a_box_and_reported():
    rendered = _render("SYMBOL nosuch 0 0 R0\nSYMATTR InstName U7\nSYMBOL nosuch 99 0 R0\n")
    assert rendered.missing_symbols == ["nosuch"]
    boxes = [g for g in ET.fromstring(rendered.svg).iter(NS + "g") if g.get("class") == "missing"]
    assert len(boxes) == 2
    assert _text(rendered.svg, "U7 (nosuch?)")["class"] == "t-missing"


def test_unknown_orientation_is_drawn_upright_with_a_warning():
    rendered = _render("SYMBOL res 0 0 R45\nSYMATTR InstName R1\n")
    assert rendered.warnings == ["R1: bilinmeyen yön 'R45', R0 çizildi."]
    transforms = [g.get("transform") for g in ET.fromstring(rendered.svg).iter(NS + "g") if g.get("class") == "sym"]
    assert transforms == ["matrix(1 0 0 1 0 0)"]


def test_viewbox_covers_the_drawing_with_a_margin():
    """Kablolar x -100..200, y 0..50; kenar boşluğu 32."""
    rendered = _render("WIRE -100 0 200 0\nWIRE 0 0 0 50\n")
    assert rendered.viewbox == (-132, -32, 364, 114)


def test_viewbox_covers_symbols_and_texts():
    """voltage: CIRCLE -32 24 32 88 -> kutu (-32,24)-(32,88), kenar 32 -> (-64, -8, 128, 128). ADI_RC'de topraklar y=224'te
    biter ama .ac yazısının çapası y=256'da: görünüm en az çapa + kenar boşluğuna (288) uzanmalı."""
    assert _render("SYMBOL voltage 0 0 R0\n").viewbox == (-64, -8, 128, 128)
    _, y, _, height = _render(ADI_RC).viewbox
    assert y + height >= 256 + 32


def test_render_asc_reads_utf16_and_finds_a_symbol_next_to_the_schematic(tmp_path):
    """π cp1252'de yok: write_asc_file LTspice gibi UTF-16LE yazar. Şemanın klasöründeki sembol önce aranır."""
    (tmp_path / "mybox.asy").write_bytes(TEST_SYMBOLS["block"].replace("IC", "kutu").encode("cp1252"))
    asc = write_asc_file(tmp_path / "t.asc", "Version 4\nSYMBOL mybox 0 0 R0\nTEXT 0 96 Left 2 ;fc = 1/(2π·R·C)\n")
    assert read_asc_file(asc)[1] == "utf-16-le"
    rendered = asc_view.render_asc(asc)
    assert rendered.encoding == "utf-16-le"
    assert rendered.missing_symbols == [] and rendered.warnings == []
    assert _text(rendered.svg, "fc = 1/(2π·R·C)")["class"] == "t-cmt"
    assert _text(rendered.svg, "kutu")["class"] == "t-sym"


def test_symbol_next_to_the_schematic_wins_over_the_library(tmp_path, monkeypatch):
    """ADI "Default Search Priority": 1) şemanın klasörü ve alt klasörleri ... 4) ADI kütüphanesi. Klasördeki res
    kütüphanedekini gölgeler; alt klasördeki sembol yalın adıyla bulunur; yalnız kütüphanede olan oradan gelir."""
    library = tmp_path / "kutuphane"
    library.mkdir()
    for name in ("res", "only"):
        (library / f"{name}.asy").write_bytes(f"Version 4\nTEXT 0 0 Left 2 kutuphane-{name}\n".encode("cp1252"))

    def from_library(name):  # sınır: kurulu LTspice kütüphanesi
        path = library / f"{name}.asy"
        return path if path.exists() else None

    monkeypatch.setattr(asc_view, "find_symbol", from_library)
    folder = tmp_path / "proje"
    (folder / "alt").mkdir(parents=True)
    (folder / "res.asy").write_bytes(b"Version 4\nTEXT 0 0 Left 2 yerel\n")
    (folder / "alt" / "mybox.asy").write_bytes(b"Version 4\nTEXT 0 0 Left 2 altklasor\n")
    asc = write_asc_file(folder / "t.asc", "Version 4\nSYMBOL res 0 0 R0\nSYMBOL mybox 0 99 R0\nSYMBOL only 0 199 R0\n")
    rendered = asc_view.render_asc(asc)
    assert sorted(text for text, _ in _texts(rendered.svg)) == ["altklasor", "kutuphane-only", "yerel"]
    assert rendered.missing_symbols == []


def test_render_asc_reports_symbols_it_cannot_find(tmp_path, monkeypatch):
    monkeypatch.setattr(asc_view, "find_symbol", lambda name: None)  # sınır: LTspice kütüphanesi kurulu değil
    asc = write_asc_file(tmp_path / "t.asc", "Version 4\nSYMBOL yok_boyle 0 0 R0\n")
    rendered = asc_view.render_asc(asc)
    assert rendered.missing_symbols == ["yok_boyle"]
    assert rendered.warnings[0].startswith("Bulunamayan sembol: yok_boyle")


@pytest.mark.skipif(library_symbol_dir() is None, reason="LTspice sembol kütüphanesi yok")
def test_adi_rc_example_with_the_installed_library(tmp_path):
    rendered = asc_view.render_asc(write_asc_file(tmp_path / "rc.asc", ADI_RC))
    assert rendered.missing_symbols == []
    assert rendered.counts == {"symbols": 3, "wires": 8, "flags": 4, "texts": 1, "junctions": 0}


# --------------------------------------------------------------------------- monitörün şema sayfası
@pytest.fixture
def project(tmp_path):
    """İzlenen klasör: iki şema (biri alt klasörde), şema olmayan dosya, .git'teki ve adaptörün geçici kopyası. Testin
    sembolleri ``alt/``'ta: iki şema da onları kendi klasöründe ya da alt klasöründe bulur (CI'da LTspice yok)."""
    root = tmp_path / "proje"
    (root / "alt").mkdir(parents=True)
    (root / ".git").mkdir()
    for name in ("res", "cap", "voltage"):
        (root / "alt" / f"{name}.asy").write_bytes(TEST_SYMBOLS[name].encode("cp1252"))
    write_asc_file(root / "eski.asc", ADI_RC)
    write_asc_file(root / "alt" / "yeni.asc", ADI_RC.replace("1k", "4.7k"))
    write_asc_file(root / ".git" / "gizli.asc", ADI_RC)
    write_asc_file(root / "_ltspice_llm_x_1.asc", ADI_RC)
    (root / "notlar.txt").write_text("asc değil", encoding="utf-8")
    now = time.time()
    os.utime(root / "eski.asc", (now - 100, now - 100))
    os.utime(root / "alt" / "yeni.asc", (now - 10, now - 10))
    return root


@pytest.fixture
def viewer(tmp_path, project, monkeypatch):
    monkeypatch.setattr(asc_view, "find_symbol", lambda name: None)  # sınır: kurulu LTspice kütüphanesi kullanılmaz
    server = monitor.make_server(monitor.Monitor(tmp_path / "events", asc_roots=[project]), port=0)
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


def _schematic(server, path, host=None):
    from urllib.parse import quote
    status, _, body = _get(server, "/api/schematic?path=" + quote(str(path)), host)
    return status, json.loads(body)


def test_list_has_the_watched_schematics_newest_first(viewer, project):
    status, _, body = _get(viewer, "/api/schematics")
    listing = json.loads(body)
    assert status == 200 and listing["roots"] == [str(project.resolve())] and listing["truncated"] is False
    assert [(f["name"], f["folder"]) for f in listing["files"]] == [("yeni.asc", str(Path("proje") / "alt")),
                                                                      ("eski.asc", "proje")]


def test_schematic_is_drawn_and_redrawn_after_a_save(viewer, project):
    target = project / "alt" / "yeni.asc"
    status, first = _schematic(viewer, target)
    assert status == 200 and first["name"] == "yeni.asc" and first["counts"]["symbols"] == 3
    assert _text(first["svg"], "4.7k")["class"] == "t-attr"
    write_asc_file(target, ADI_RC.replace("1k", "10k"))
    later = time.time() + 5
    os.utime(target, (later, later))
    status, second = _schematic(viewer, target)
    assert status == 200 and second["stamp"] != first["stamp"]
    assert _text(second["svg"], "10k")["class"] == "t-attr"
    listing = json.loads(_get(viewer, "/api/schematics")[2])
    assert listing["files"][0]["stamp"] == second["stamp"]


def test_schematic_is_answered_in_the_listing_spelling(viewer, project):
    """Sayfa yolları birebir karşılaştırır. ``..`` ile ya da Windows'un kısa (8.3) adıyla (geçici klasör çoğu zaman öyle)
    istenen şema listedeki yazımla dönmeli; yoksa sayfa onu listede bulamaz ve kaydedilince yenilemez."""
    listing = json.loads(_get(viewer, "/api/schematics")[2])
    listed = [f["path"] for f in listing["files"] if f["name"] == "yeni.asc"]
    status, body = _schematic(viewer, project / "alt" / ".." / "alt" / "yeni.asc")
    assert status == 200 and [body["path"]] == listed


@pytest.mark.skipif(sys.platform != "win32", reason="8.3 kısa adlar Windows'ta")
def test_schematic_asked_by_its_short_name_is_answered_in_the_listing_spelling(viewer, project):
    """Kısa adı Windows verir (GetShortPathNameW); bu sürücüde 8.3 ad üretimi kapalıysa test bir şey sınamaz, atlanır."""
    import ctypes

    long_path = str((project / "alt" / "yeni.asc").resolve())
    buffer = ctypes.create_unicode_buffer(1024)
    if not ctypes.windll.kernel32.GetShortPathNameW(long_path, buffer, len(buffer)) or buffer.value == long_path:
        pytest.skip("bu sürücüde 8.3 adı yok")
    listed = [f["path"] for f in json.loads(_get(viewer, "/api/schematics")[2])["files"] if f["name"] == "yeni.asc"]
    status, body = _schematic(viewer, buffer.value)
    assert status == 200 and [body["path"]] == listed


@pytest.mark.parametrize("name", ["notlar.txt", "../disarida.asc", "../proje2/x.asc"])
def test_files_outside_the_watched_schematics_are_refused(viewer, project, name):
    """``proje2`` ``proje`` ile başlar ama onun içinde değil: yol karşılaştırması önek değil klasör sınırıyla yapılmalı."""
    (project.parent / "disarida.asc").write_bytes(b"Version 4\n")
    (project.parent / "proje2").mkdir()
    (project.parent / "proje2" / "x.asc").write_bytes(b"Version 4\n")
    status, body = _schematic(viewer, project / name)
    assert status == 403 and "izlenen" in body["error"]


def test_upper_case_extension_is_drawn_where_paths_keep_their_case(tmp_path, monkeypatch):
    """Tarama ``.asc``'yi harfe duyarsız bulur (``scan_schematics``: ``name.lower()``); çizim izni de öyle olmalı. Windows'ta
    ``os.path.normcase`` harfleri küçülttüğü için fark görünmez; Linux'ta ``posixpath.normcase`` yolu değiştirmez (Python
    ``os.path.normcase`` belgesi) ve ``DEVRE.ASC`` listelenip 403 alıyordu (test-reviewer 2026-11-19). Linux'un yol kuralı
    işletim sistemi sınırıdır: Windows'ta da Linux'unkiyle sınanır."""
    monkeypatch.setattr(asc_view, "find_symbol", lambda name: None)
    root = tmp_path / "proje"
    root.mkdir()
    upper = write_asc_file(root / "DEVRE.ASC", ADI_RC)
    monkeypatch.setattr(monitor.os.path, "normcase", posixpath.normcase)
    mon = monitor.Monitor(tmp_path / "events", asc_roots=[root])
    assert [f["name"] for f in mon.schematic_list()["files"]] == ["DEVRE.ASC"]
    assert mon.schematic_payload(str(upper))[0] == 200


def test_a_file_root_is_drawn_but_not_its_neighbours(tmp_path, monkeypatch):
    monkeypatch.setattr(asc_view, "find_symbol", lambda name: None)
    single = write_asc_file(tmp_path / "tek.asc", ADI_RC)
    neighbour = write_asc_file(tmp_path / "komsu.asc", ADI_RC)
    mon = monitor.Monitor(tmp_path / "events", asc_roots=[single, tmp_path / "." / "tek.asc"])
    assert mon.asc_roots == [single.resolve()]  # aynı kök bir kez
    assert (mon.schematic_payload(str(single))[0], mon.schematic_payload(str(neighbour))[0]) == (200, 403)
    assert mon.schematic_payload("")[0] == 400


def test_big_folders_are_cut_and_the_cut_is_reported(tmp_path, project, monkeypatch):
    """Tarama ``limit`` girdide kesilir (proje klasöründe beş girdi var); liste ``MAX_LISTED``'de kesilir, ikisi de
    ``truncated`` olarak bildirilir."""
    found, cut = monitor.scan_schematics(project, limit=1)
    assert cut is True and len(found) <= 1
    monkeypatch.setattr(monitor, "MAX_LISTED", 1)
    listing = monitor.Monitor(tmp_path / "events", asc_roots=[project]).schematic_list()
    assert ([f["name"] for f in listing["files"]], listing["truncated"]) == (["yeni.asc"], True)


def test_a_cut_scan_is_reported_in_the_listing(tmp_path, project, monkeypatch):
    """`test_big_folders_are_cut_and_the_cut_is_reported`: iki kesim de ``truncated`` olarak bildirilir. Orada liste
    ``MAX_LISTED``'de kesiliyor; burada yalnız tarama (`monitor.MAX_SCAN_ENTRIES`; proje klasöründe beş girdi var)."""
    monkeypatch.setattr(monitor, "MAX_SCAN_ENTRIES", 1)
    listing = monitor.Monitor(tmp_path / "events", asc_roots=[project]).schematic_list()
    assert listing["truncated"] is True and len(listing["files"]) <= 1 < monitor.MAX_LISTED


def test_netlist_sources_of_runs_are_not_listed_as_schematics(viewer, tmp_path, monkeypatch):
    net = tmp_path / "devre.net"
    net.write_text("* x\n.end\n", encoding="utf-8")
    monkeypatch.setenv(events.ENV_EVENTS, "1")
    monkeypatch.setenv(events.ENV_EVENT_DIR, str(viewer.monitor.reader.directory))
    events.emit("run_start", run="n1", circuit="n", source=str(net))
    assert [f["name"] for f in json.loads(_get(viewer, "/api/schematics")[2])["files"]] == ["yeni.asc", "eski.asc"]


def test_deleted_schematic_is_not_found(viewer, project):
    (project / "eski.asc").unlink()
    assert _schematic(viewer, project / "eski.asc")[0] == 404


def test_schematic_run_from_elsewhere_is_listed_from_its_run_event(viewer, tmp_path, monkeypatch):
    """Koşum olayındaki ``source``: izlenen klasör dışındaki .asc de listelenir ve çizilir."""
    (tmp_path / "baska").mkdir()
    elsewhere = write_asc_file(tmp_path / "baska" / "kosulan.asc", ADI_RC)
    monkeypatch.setenv(events.ENV_EVENTS, "1")
    monkeypatch.setenv(events.ENV_EVENT_DIR, str(viewer.monitor.reader.directory))
    events.emit("run_start", run="r1", circuit="kosulan", source=str(elsewhere))
    names = [f["name"] for f in json.loads(_get(viewer, "/api/schematics")[2])["files"]]
    assert names.count("kosulan.asc") == 1
    assert _schematic(viewer, elsewhere)[0] == 200


def test_schematic_page_is_served_with_the_page_policy(viewer):
    assert viewer.server_address[0] == "127.0.0.1"  # monitor belge dizgisi: yalnız yerel adres dinlenir
    status, headers, body = _get(viewer, "/sema")
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    policy = dict(part.strip().split(" ", 1) for part in headers["Content-Security-Policy"].split(";") if part.strip())
    assert policy == PAGE_POLICY
    assert (headers["X-Content-Type-Options"], headers["Cache-Control"]) == ("nosniff", "no-store")
    assert body.count(b'<script src="/sema.js"></script>') == 1
    status, headers, _ = _get(viewer, "/sema.js")
    assert status == 200 and headers["Content-Type"].startswith("text/javascript")


@pytest.mark.parametrize("path", ["/sema", "/sema.js", "/api/schematics", "SCHEMATIC"])
def test_schematic_endpoints_refuse_a_foreign_host(viewer, project, path):
    from urllib.parse import quote
    if path == "SCHEMATIC":
        path = "/api/schematic?path=" + quote(str(project / "eski.asc"))
        assert _get(viewer, path)[0] == 200  # gerçek, izinli bir şema: 403'ün nedeni yalnız Host
    assert _get(viewer, path, host="evil.example")[0] == 403


def _ltspice_ini(appdata, monkeypatch, text):
    """LTspice 26.1.1 gibi BOM'lu UTF-16LE ayar dosyası (tests/data/ref26_LTspice.ini); APPDATA onu gösterir
    (işletim sistemi sınırı: ortam değişkeni)."""
    appdata.mkdir(parents=True, exist_ok=True)
    (appdata / "LTspice.ini").write_bytes(codecs.BOM_UTF16_LE + text.encode("utf-16-le"))
    monkeypatch.setenv("APPDATA", str(appdata))


def test_ltspice_working_folder_and_recent_files_come_from_its_settings(tmp_path, monkeypatch):
    """``[Options] WorkingDirectory`` ve ``[Recent File List] File1=…`` (bu makinedeki LTspice 26.1.1 ayar dosyasının
    biçimi, 2026-11-19). Son açılanlar numara sırasıyla (File10, File9'dan sonra), yalnız var olanlar; başka bölümdeki
    ``File1`` sayılmaz."""
    work = tmp_path / "ltspice_calisma"
    work.mkdir()
    files = [write_asc_file(tmp_path / f"s{i}.asc", "Version 4\n") for i in range(3)]
    _ltspice_ini(tmp_path / "appdata", monkeypatch,
                 f"[Options]\r\nWorkingDirectory={work}\r\n[Recent File List]\r\nFile10={files[2]}\r\n"
                 f"File1={files[0]}\r\nFile2={tmp_path / 'silinmis.asc'}\r\nFile9={files[1]}\r\n[Colors]\r\n"
                 f"File1={tmp_path / 's0.asc'}\r\n")
    assert reference.ltspice_working_dir() == work
    assert reference.ltspice_recent_files() == [files[0], files[1], files[2]]


def test_without_ltspice_settings_there_is_no_working_folder_or_recent_file(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert (reference.ltspice_working_dir(), reference.ltspice_recent_files()) == (None, [])


def test_working_folder_that_does_not_exist_is_none(tmp_path, monkeypatch):
    """Gerçek LTspice 26.1.1 ayar dosyası (tests/data/ref26_LTspice.ini, examples/14; yollar ``C:\\ws``'ye çevrilmiş):
    ``[Options] WorkingDirectory=C:\\ws\\user``, bu klasör yok. `reference.ltspice_working_dir`: "ayar ya da klasör yoksa
    None"."""
    appdata = tmp_path / "appdata"
    appdata.mkdir()
    (appdata / "LTspice.ini").write_bytes((Path(__file__).parent / "data" / "ref26_LTspice.ini").read_bytes())
    monkeypatch.setenv("APPDATA", str(appdata))
    assert reference.ltspice_ini_section("Options").count(("WorkingDirectory", "C:\\ws\\user")) == 1
    assert not Path("C:\\ws\\user").is_dir()  # önkoşul: kayıttaki klasör bu makinede yok
    assert reference.ltspice_working_dir() is None


@pytest.mark.parametrize("recent,listed,status", [(True, 1, 200), (False, 0, 403)])
def test_recently_opened_schematics_are_shown_when_asked(tmp_path, project, monkeypatch, recent, listed, status):
    (tmp_path / "masaustu").mkdir()
    opened = write_asc_file(tmp_path / "masaustu" / "son.asc", ADI_RC)
    _ltspice_ini(tmp_path / "appdata", monkeypatch, f"[Recent File List]\r\nFile1={opened}\r\n")
    mon = monitor.Monitor(tmp_path / "events", asc_roots=[project], ltspice_recent=recent)
    assert [f["name"] for f in mon.schematic_list()["files"]].count("son.asc") == listed
    assert mon.schematic_payload(str(opened))[0] == status


def test_recent_schematic_is_listed_in_the_spelling_it_is_answered_in(tmp_path, project, monkeypatch):
    """LTspice'ın listesindeki yazım (``..``, küçük harfli sürücü, 8.3 ad) olduğu gibi listelenirse sayfa şemayı listede
    bulamaz ve kaydedilince yenilemez: liste ve cevap aynı, çözülmüş yolu vermeli (test-reviewer, 2026-11-19)."""
    (tmp_path / "masaustu").mkdir()
    write_asc_file(tmp_path / "masaustu" / "son.asc", ADI_RC)
    _ltspice_ini(tmp_path / "appdata", monkeypatch,
                 f"[Recent File List]\r\nFile1={tmp_path / 'masaustu' / '..' / 'masaustu' / 'son.asc'}\r\n")
    mon = monitor.Monitor(tmp_path / "events", asc_roots=[project], ltspice_recent=True)
    listed = [f["path"] for f in mon.schematic_list()["files"] if f["name"] == "son.asc"]
    assert len(listed) == 1
    status, body = mon.schematic_payload(listed[0])
    assert status == 200 and body["path"] == listed[0]


# --------------------------------------------------------------------------- sayfadan klasör ekleme
def _names(mon):
    return [f["name"] for f in mon.schematic_list()["files"]]


def test_added_folder_is_watched_saved_and_released(tmp_path, project, monkeypatch):
    monkeypatch.setattr(asc_view, "find_symbol", lambda name: None)
    extra = tmp_path / "baska klasor"
    extra.mkdir()
    write_asc_file(extra / "yeni_cizim.asc", ADI_RC)
    saved = tmp_path / "ayar" / "monitor.json"
    mon = monitor.Monitor(tmp_path / "events", asc_roots=[project], saved_roots=saved)
    assert _names(mon).count("yeni_cizim.asc") == 0
    status, body = mon.add_root(f'  "{extra}"  ')  # Explorer'ın "Yol olarak kopyala"sı tırnaklı verir
    assert status == 200 and body["added"] == str(extra.resolve())
    assert body["roots"] == [str(project.resolve()), str(extra.resolve())]
    assert _names(mon).count("yeni_cizim.asc") == 1
    assert mon.schematic_payload(str(extra / "yeni_cizim.asc"))[0] == 200
    assert json.loads(saved.read_text(encoding="utf-8")) == {"roots": [str(extra.resolve())]}
    assert monitor.Monitor(tmp_path / "events", saved_roots=saved).asc_roots == [extra.resolve()]  # hatırlanır
    status, body = mon.remove_root(str(extra))
    assert status == 200 and body["roots"] == [str(project.resolve())]
    assert json.loads(saved.read_text(encoding="utf-8")) == {"roots": []}
    assert mon.schematic_payload(str(extra / "yeni_cizim.asc"))[0] == 403


def test_bad_or_repeated_folders_are_refused(tmp_path, project):
    mon = monitor.Monitor(tmp_path / "events", asc_roots=[project])
    assert mon.add_root("  ")[0] == 400
    assert mon.add_root(str(tmp_path / "yok"))[0] == 404
    assert mon.add_root(str(project / "notlar.txt"))[0] == 404  # .asc olmayan dosya
    status, body = mon.add_root(str(project / "alt" / ".."))  # aynı klasör başka yazımla
    assert status == 409 and body["roots"] == [str(project.resolve())]
    assert mon.remove_root(str(tmp_path / "yok"))[0] == 404
    assert mon.add_root(str(project / "eski.asc"))[0] == 200  # tek bir .asc de eklenebilir


def test_folder_path_may_use_an_environment_variable(tmp_path, project, monkeypatch):
    """``$VAR`` hem ntpath hem posixpath'te açılır (Windows'ta ``%VAR%`` de)."""
    monkeypatch.setenv("SEMA_KLASORU", str(project))
    mon = monitor.Monitor(tmp_path / "events")
    assert mon.add_root("$SEMA_KLASORU")[0] == 200
    assert mon.asc_roots == [project.resolve()]


def test_unreadable_or_stale_saved_folders_are_skipped(tmp_path, project):
    saved = tmp_path / "monitor.json"
    saved.write_text("{bozuk", encoding="utf-8")
    assert monitor.Monitor(tmp_path / "events", saved_roots=saved).asc_roots == []
    saved.write_text(json.dumps({"roots": [str(project), str(tmp_path / "silinmis")]}), encoding="utf-8")
    assert monitor.Monitor(tmp_path / "events", saved_roots=saved).asc_roots == [project.resolve()]


def test_folder_is_watched_even_if_it_cannot_be_saved(tmp_path, project):
    blocker = tmp_path / "dosya"
    blocker.write_text("x", encoding="utf-8")
    mon = monitor.Monitor(tmp_path / "events", saved_roots=blocker / "monitor.json")  # bir dosyanın altı: yazılamaz
    status, body = mon.add_root(str(project))
    assert status == 200 and "kaydedilemedi" in body["warning"]
    assert mon.asc_roots == [project.resolve()]


def _post(server, path, body, headers=None):
    """Monitörün kendi sayfasının gönderdiği gibi POST; ``headers``'taki None başlığı çıkarır."""
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        payload = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        sent = {"Host": f"127.0.0.1:{server.port}", "Origin": f"http://127.0.0.1:{server.port}",
                monitor.POST_HEADER: "1", "Content-Type": "application/json", **(headers or {})}
        conn.request("POST", path, body=payload, headers={k: v for k, v in sent.items() if v is not None})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def test_page_adds_and_removes_a_folder(viewer, tmp_path):
    extra = tmp_path / "eklenen"
    extra.mkdir()
    write_asc_file(extra / "e.asc", ADI_RC)
    assert _post(viewer, "/api/roots/add", {"path": str(extra)})[0] == 200
    listing = json.loads(_get(viewer, "/api/schematics")[2])
    assert listing["roots"].count(str(extra.resolve())) == 1
    assert [f["name"] for f in listing["files"]].count("e.asc") == 1
    assert _post(viewer, "/api/roots/remove", {"path": str(extra)})[0] == 200
    assert json.loads(_get(viewer, "/api/schematics")[2])["roots"].count(str(extra.resolve())) == 0


@pytest.mark.parametrize("headers", [
    {monitor.POST_HEADER: None},          # başka bir sitenin formu bu başlığı koyamaz
    {"Origin": "http://evil.example"},    # başka bir sitenin betiği kendi kökenini yazar
    {"Origin": None},
    {"Host": "evil.example"},             # DNS rebinding
], ids=["baslik-yok", "yabanci-koken", "koken-yok", "yabanci-host"])
def test_folders_change_only_from_the_monitors_own_page(viewer, tmp_path, headers):
    extra = tmp_path / "eklenmemeli"
    extra.mkdir()
    assert _post(viewer, "/api/roots/add", {"path": str(extra)}, headers)[0] == 403
    assert extra.resolve() not in viewer.monitor.asc_roots


def test_origin_must_be_exactly_this_monitor(viewer, tmp_path):
    """Başka porttaki yerel bir servis, https kökeni ve adı ``localhost``/``127.0.0.1`` ile başlayan yabancı alan da "başka
    site"dir (köken bütün olarak karşılaştırılır); sayfa localhost adıyla açılmışsa ekleyebilir."""
    extra = tmp_path / "x"
    extra.mkdir()
    for origin in (f"http://127.0.0.1:{viewer.port + 1}", f"https://127.0.0.1:{viewer.port}", "null",
                   f"http://localhost.evil.example:{viewer.port}", f"http://127.0.0.1.evil.example:{viewer.port}"):
        assert _post(viewer, "/api/roots/add", {"path": str(extra)}, {"Origin": origin})[0] == 403, origin
    local = {"Host": f"localhost:{viewer.port}", "Origin": f"http://localhost:{viewer.port}"}
    assert _post(viewer, "/api/roots/add", {"path": str(extra)}, local)[0] == 200


def test_saved_folders_keep_each_other(tmp_path, project):
    """Kalıcı listede A varken B eklenir: [A, B]; yeniden açılan monitör ikisini de izler; A bırakılınca [B]. Başarılı
    işlemde uyarı yoktur. Türkçe ve Yunan harfli ad (cp1252/cp1254'te Ω yok) gidip gelir."""
    first, second = tmp_path / "Masaüstü", tmp_path / "Ω devre"
    first.mkdir()
    second.mkdir()
    saved = tmp_path / "monitor.json"
    saved.write_text(json.dumps({"roots": [str(first)]}, ensure_ascii=False), encoding="utf-8")
    mon = monitor.Monitor(tmp_path / "events", saved_roots=saved)
    status, body = mon.add_root(str(second))
    assert status == 200 and "warning" not in body
    reopened = monitor.Monitor(tmp_path / "events", saved_roots=saved)
    assert reopened.asc_roots == [first.resolve(), second.resolve()]
    status, body = reopened.remove_root(str(first))
    assert status == 200 and "warning" not in body
    assert json.loads(saved.read_text(encoding="utf-8")) == {"roots": [str(second.resolve())]}


def test_empty_path_releases_nothing(tmp_path, project, monkeypatch):
    """Boş yol çalışma klasörüne çözülür: kök olan çalışma klasörü yanlışlıkla bırakılmamalı."""
    monkeypatch.chdir(project)
    mon = monitor.Monitor(tmp_path / "events", asc_roots=[project])
    assert mon.remove_root("  ")[0] == 400
    assert mon.asc_roots == [project.resolve()]


def test_home_folder_shorthand_and_config_location(tmp_path, project, monkeypatch):
    """``~`` açılır; APPDATA yoksa ayar ``~/.config/ltspice_llm/monitor.json`` (monitor.config_path belgesi)."""
    for name in ("HOME", "USERPROFILE"):  # posixpath HOME'u, ntpath USERPROFILE'ı okur
        monkeypatch.setenv(name, str(tmp_path))
    mon = monitor.Monitor(tmp_path / "events")
    assert mon.add_root("~/proje")[0] == 200 and mon.asc_roots == [project.resolve()]
    monkeypatch.delenv("APPDATA", raising=False)
    assert monitor.config_path() == Path.home() / ".config" / "ltspice_llm" / "monitor.json"


def _xml_char(code):
    """XML 1.0 §2.2 Char: #x9 | #xA | #xD | [#x20-#xD7FF] | [#xE000-#xFFFD] | [#x10000-#x10FFFF]."""
    return code in (0x9, 0xA, 0xD) or 0x20 <= code <= 0xD7FF or 0xE000 <= code <= 0xFFFD or 0x10000 <= code <= 0x10FFFF


@given(st.characters())
def test_xml_text_keeps_exactly_the_characters_xml_allows(char):
    from html import escape

    assert asc_view._xml_text(char) == (escape(char) if _xml_char(ord(char)) else "")


def test_a_link_cannot_add_a_folder(viewer, tmp_path):
    """GET durum değiştirmez: bir bağlantı ya da <img src> klasör ekleyemez."""
    from urllib.parse import quote

    extra = tmp_path / "x"
    extra.mkdir()
    assert _get(viewer, "/api/roots/add?path=" + quote(str(extra)))[0] == 404
    assert extra.resolve() not in viewer.monitor.asc_roots


@pytest.mark.parametrize("body,headers,status", [
    (b"{bozuk", None, 400),
    (b'["liste"]', None, 400),
    (b'{"path": 5}', None, 400),
    (b"x" * (64 * 1024 + 1), None, 413),
], ids=["bozuk-json", "liste", "yol-sayi-degil", "cok-buyuk"])
def test_malformed_folder_requests_are_refused(viewer, body, headers, status):
    assert _post(viewer, "/api/roots/add", body, headers)[0] == status
    assert _post(viewer, "/api/bilinmeyen", {"path": "x"})[0] == 404


@pytest.mark.parametrize("length,status", [("1000000000", 413), ("-5", 400), ("abc", 400)],
                         ids=["bir-gb", "negatif", "sayi-degil"])
def test_impossible_body_length_is_answered_without_reading_the_body(viewer, length, status):
    """``monitor._Handler.do_POST``: reddedilecek gövde de ``DRAIN_BYTES``'e kadar okunur, "çok büyüğü okunmaz"; geçersiz
    uzunluk okunmaz. İstemci gövde göndermez; sunucu okumaya kalkarsa yanıt gelmez. Üç vaka üç ayrı davranışı sınar:
    üst sınır yoksa 1 GB'lık okuma bekler (5 sn'lik zaman aşımı dolar). Alt sınır yoksa ``read(-5)`` ``ValueError`` verir
    ve işleyici yanıtsız çöker (CPython BufferedReader: "read length must be non-negative or -1"). ``abc`` -1'e döner ve
    ``read(-1)`` bağlantı kapanana kadar okur (zaman aşımı). Gövde gönderilmez, çünkü okunmamış veriyle kapanan bağlantıyı
    Windows sıfırlar ve doğru kod da aralıklı kırmızı olurdu (monitor.py, ölçüm 2026-11-19)."""
    conn = http.client.HTTPConnection("127.0.0.1", viewer.port, timeout=5)
    try:
        conn.putrequest("POST", "/api/roots/add", skip_host=True, skip_accept_encoding=True)
        for name, value in (("Host", f"127.0.0.1:{viewer.port}"), ("Origin", f"http://127.0.0.1:{viewer.port}"),
                            (monitor.POST_HEADER, "1"), ("Content-Type", "application/json"), ("Content-Length", length)):
            conn.putheader(name, value)
        conn.endheaders()
        response = conn.getresponse()
        response.read()
        assert response.status == status
    finally:
        conn.close()


def test_page_scripts_write_data_only_as_text():
    """Koşumlar sayfası hiçbir HTML ayrıştırıcısı kullanmaz; şema sayfasının tek istisnası sunucunun SVG'sini
    ``image/svg+xml`` olarak ayrıştırmak (monitor_page belgesi)."""
    sinks = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "createContextualFragment", "eval(",
             "Function(", "srcdoc")
    for script in (PAGE_JS, SCHEMA_JS):
        assert [sink for sink in sinks if sink in script] == []
    assert "DOMParser" not in PAGE_JS
    assert SCHEMA_JS.count("new DOMParser()") == 1 and SCHEMA_JS.count('parseFromString(data.svg, "image/svg+xml")') == 1
    # durum değiştiren tek istek: POST ve sunucunun beklediği başlıkla; sayfada form yok (CSP form-action 'none')
    assert SCHEMA_JS.count('method: "POST"') == 1 and SCHEMA_JS.count(f'"{monitor.POST_HEADER}": "1"') == 1
    assert "<form" not in SCHEMA_HTML
    # sayfa ile sunucunun sözleşmesi: rota /api/roots/<eylem>, gövde {"path": ...}; iki eylemin sunucu yolu
    # test_page_adds_and_removes_a_folder'da sınanıyor
    assert SCHEMA_JS.count('fetch("/api/roots/" + action,') == 1 and SCHEMA_JS.count("JSON.stringify({ path: path })") == 1
    assert sorted(set(re.findall(r'changeRoots\("(\w+)"', SCHEMA_JS))) == ["add", "remove"]
