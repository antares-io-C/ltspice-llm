"""
KiCad kart okuyucusu (``ltspice_llm.board``) testleri.

Beklenen değerlerin kaynağı iki tanesidir, okuyucunun kendi çıktısı değildir:

- **KiCad tanığı**: ``tests/data/kicad10_*.oracle.json``. KiCad 10.0.3'ün kendi ``pcbnew`` hesabı (iz uzunluğu, pad
  konumu, bağlı pad adaları, dökümün içindeki noktalar); ``examples/20_kicad_board_checks.py --kaydet`` sentetik kartı
  KiCad'le kurar ve kaydeder. Tanığın SHA-256'sı kart dosyasınınkiyle aynı olmalı.
- **Elle hesap**: sentetik kartın tasarımı ``examples/kicad_oracle.py`` (``build_paths``, ``build_4layer``) belge
  dizgisinde ve koordinatlarında; her beklentinin hesabı testin yanında.

KiCad kuruluysa son test kartları KiCad'e yeniden hesaplatır (taze tanık); değilse atlanır.
"""

import hashlib
import importlib.util
import json
import math
import os
import shutil
import sys
from pathlib import Path

import pytest

from kicad_text import board_text, footprint, point_pad, segment, via
from ltspice_llm.adapter import LTSpiceAdapter
from ltspice_llm.tools import handle_tool_call
from ltspice_llm.board import (
    TESTED_FORMAT_VERSIONS,
    BoardPathError,
    PathPiece,
    PathVia,
    inject_board_path_into_netlist,
    parse_sexpr,
    read_kicad_board,
)
from ltspice_llm.parasitics import COPPER_MM_PER_OZ, calculate_trace_parasitics
from ltspice_llm.quantizer import parse_spice_value

DATA = Path(__file__).parent / "data"
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
STEMS = ["kicad10_paths", "kicad10_4layer"]
#: Tanık mm'yi 6 ondalıkla (1 nm) yazar
NM = 2e-6
#: ADI Basic Linear Design Bl.12 s.7: bakırın 25 °C özdirenci 1.724e-6 Ohm*cm; s.8: sıcaklık katsayısı %0.4/°C
RHO_25C = 1.724e-8
TEMPCO = 0.004


def _oracle(stem):
    return json.loads((DATA / f"{stem}.oracle.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def boards():
    return {stem: read_kicad_board(DATA / f"{stem}.kicad_pcb") for stem in STEMS}


@pytest.fixture(scope="module")
def paths(boards):
    return boards["kicad10_paths"]


@pytest.fixture(scope="module")
def four(boards):
    return boards["kicad10_4layer"]


def _write(tmp_path, text, name="board.kicad_pcb"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------------------------------- KiCad tanığı
@pytest.mark.parametrize("stem", STEMS)
def test_witness_was_recorded_from_this_board_file(stem, boards):
    digest = hashlib.sha256((DATA / f"{stem}.kicad_pcb").read_bytes()).hexdigest()
    oracle = _oracle(stem)
    assert oracle["sha256"] == digest and boards[stem].sha256 == digest
    assert oracle["kicad_version"] == "10.0.3" and boards[stem].format_version in TESTED_FORMAT_VERSIONS
    assert boards[stem].warnings == []


@pytest.mark.parametrize("stem", STEMS)
def test_copper_layers_are_the_ones_kicad_enables(stem, boards):
    # KiCad katman numarası sırasıyla yazar (F.Cu, B.Cu, In1.Cu...); okuyucu üstten alta dizer
    assert sorted(boards[stem].copper_layers) == sorted(_oracle(stem)["copper_layers"])
    assert boards[stem].copper_layers[0] == "F.Cu" and boards[stem].copper_layers[-1] == "B.Cu"


@pytest.mark.parametrize("stem", STEMS)
def test_net_track_lengths_and_counts_match_kicad(stem, boards):
    board, oracle = boards[stem], _oracle(stem)
    assert oracle["nets"], "tanıkta net yok"
    for net, stat in oracle["nets"].items():
        assert board.net_track_length_mm(net) == pytest.approx(stat["track_length_mm"], abs=NM), net
        assert sum(t.net == net for t in board.tracks) == stat["tracks"], net
        assert sum(v.net == net for v in board.vias) == stat["vias"], net


@pytest.mark.parametrize("stem", STEMS)
def test_pad_positions_angles_layers_and_nets_match_kicad(stem, boards):
    board, oracle = boards[stem], _oracle(stem)
    assert len(board.pads) == len(oracle["pads"])
    for entry in oracle["pads"]:
        same = [p for p in board.find_pads(entry["ref"], entry["number"])
                if math.hypot(p.at[0] - entry["at"][0], p.at[1] - entry["at"][1]) <= NM]
        assert len(same) == 1, entry
        pad = same[0]
        assert (pad.angle_deg - entry["angle_deg"]) % 360.0 == pytest.approx(0.0, abs=1e-6), entry
        assert set(pad.layers) == set(entry["layers"]) and pad.net == entry["net"], entry
        assert sorted(pad.size_mm) == pytest.approx(sorted(entry["size_mm"]), abs=NM), entry


@pytest.mark.parametrize("stem", STEMS)
def test_connected_pad_islands_match_kicad(stem, boards):
    board, oracle = boards[stem], _oracle(stem)
    for net, groups in oracle["islands"].items():
        assert board.connected_pad_groups(net) == groups, net


def test_islands_cover_the_hard_cases(paths):
    """Tanıkta bu durumlar gerçekten var: yalnız dökümle bağlı, boşluklu, çapraz, aynı numaralı padler, jumper."""
    oracle = _oracle("kicad10_paths")["islands"]
    assert oracle["/POUR"] == [["TP20.1"], ["TP21.1"]] and oracle["/GAP"] == [["TP25.1"], ["TP26.1"]]
    assert oracle["/CROSS"] == [["TP10.1", "TP7.1", "TP8.1", "TP9.1"]]
    assert oracle["/SHLD"] == [["J2.S"], ["J2.S", "TP22.1"]]
    assert oracle["/JMP"] == [["JP1.1", "JP1.1", "TP27.1", "TP28.1"]]
    assert oracle["/JGRP"] == [["JP2.1", "JP2.2", "TP29.1", "TP30.1"]]


@pytest.mark.parametrize("stem", STEMS)
def test_zone_fill_samples_match_kicad(stem, boards):
    board, oracle = boards[stem], _oracle(stem)
    checked = 0
    for zone in oracle["zones"]:
        ours = [z for z in board.zones if z.net == zone["net"] and set(z.layers) == set(zone["layers"])]
        assert len(ours) == 1 and bool(ours[0].fills) == zone["is_filled"], zone["net"]
        for layer, x, y, inside in zone["samples"]:
            # copper_fill_at her netin dökümüne bakar: aynı katmanda başka dökümün bulunmadığı örnekler
            others = [z for z in board.zones if z is not ours[0] and layer in z.layers]
            if not others:
                assert board.copper_fill_at(layer, x, y) == inside, (zone["net"], layer, x, y)
                checked += 1
    assert checked >= 100   # tanıkta 192 (paths) / 120 (4layer) örnek, hepsi tek dökümlü katmanda


@pytest.mark.parametrize("stem", STEMS)
def test_plane_coverage_under_each_straight_track_matches_kicad(stem, boards):
    """Tanığın örnekleme kuralı: n = ceil(uzunluk / adım) nokta, t = (i + 0.5) / n (examples/kicad_oracle.py)."""
    board, oracle = boards[stem], _oracle(stem)
    step, checked = oracle["coverage_step_mm"], 0
    for entry in oracle["tracks"]:
        if entry["type"] != "segment":
            continue
        track = next(t for t in board.tracks if t.net == entry["net"] and t.layer == entry["layer"]
                     and t.start == pytest.approx(tuple(entry["start"]), abs=NM)
                     and t.end == pytest.approx(tuple(entry["end"]), abs=NM))
        count = max(1, math.ceil(entry["length_mm"] / step))
        for layer, expected in entry["plane_under"].items():
            inside = sum(board.copper_fill_at(layer, *track.point_at(track.length_mm * (i + 0.5) / count))
                         for i in range(count))
            assert inside / count == pytest.approx(expected, abs=1e-6), (entry["net"], layer)
            checked += 1
    assert checked >= 5


# --------------------------------------------------------------------------------------------------- iz yolu
# Elle hesap (examples/kicad_oracle.py build_paths koordinatları). Her yol iki yönde de aynı uzunlukta olmalı.
# /ARC: 5 + çeyrek daire (r = 5: 2.5 pi) + 5. Ark orta noktası dosyada nm'ye yuvarlanır: yarıçap ~1e-6 mm sapar.
# /CROSS: köşegenler (62,26)-(70,34) ve (62,34)-(70,26), kesişim (66,30); yarım köşegen sqrt(32).
# /WIDE: TP15 (30,30) - kesişim (40,30) 10 + dal (40,30.8)-(40,35) 4.2; dalın eksene kadarki 0.8 mm'si iz değil.
# /OFFC: iz 2 mm'lik padin içinde 30.7'den başlar: 40 - 30.7 = 9.3. /STUB: pad (50,42) izin ortasında: 60 - 50.
# /ROT: R1.2 (70 + cos45, 42 - sin45) -> (76, y) 5.2929 + (76, 54) 12.7071 = 18 (F.Cu) + via + (76,54)-(72,54) 4 (B.Cu).
PATHS = [
    ("J1.1", "TP1.1", 25.0, NM),
    ("TP2.1", "TP3.1", 20.0, NM), ("TP2.1", "TP4.1", 25.0, NM), ("TP3.1", "TP4.1", 25.0, NM),
    ("TP5.1", "TP6.1", 10.0 + 2.5 * math.pi, 1e-5),
    ("TP7.1", "TP8.1", 2 * math.sqrt(32), NM), ("TP7.1", "TP9.1", 2 * math.sqrt(32), NM),
    ("TP7.1", "TP10.1", 2 * math.sqrt(32), NM), ("TP9.1", "TP10.1", math.sqrt(128), NM),
    ("TP15.1", "TP16.1", 20.0, NM), ("TP15.1", "TP17.1", 14.2, NM), ("TP16.1", "TP17.1", 14.2, NM),
    ("TP18.1", "TP19.1", 12.0, NM), ("TP13.1", "TP14.1", 10.0, NM), ("J2.S", "TP22.1", 6.0, NM),
    ("TP23.1", "TP24.1", 9.3, NM), ("R1.2", "C1.1", 22.0, NM), ("TP11.1", "TP12.1", 10.0, NM),
    ("TP27.1", "JP1.1", 6.0, NM), ("TP28.1", "JP1.1", 6.0, NM), ("TP29.1", "JP2.1", 6.0, NM),
    ("TP30.1", "JP2.2", 6.0, NM),
]


@pytest.mark.parametrize("start, end, expected, tol", PATHS)
def test_path_length_by_hand_in_both_directions(paths, start, end, expected, tol):
    forward, backward = paths.trace_path(start, end), paths.trace_path(end, start)
    assert forward.length_mm == pytest.approx(expected, abs=tol)
    assert backward.length_mm == pytest.approx(expected, abs=tol)
    assert forward.length_mm == pytest.approx(sum(p.length_mm for p in forward.pieces), abs=1e-12)


def test_chain_pieces_and_layer_changes(paths):
    path = paths.trace_path("J1.1", "TP1.1")
    kinds = [type(e).__name__ for e in path.elements]
    assert kinds == ["PathPiece", "PathPiece", "PathVia", "PathPiece", "PathVia", "PathPiece"]
    assert [(p.layer, p.width_mm, round(p.length_mm, 9)) for p in path.pieces] == [
        ("F.Cu", 0.25, 5.0), ("F.Cu", 0.5, 6.0), ("B.Cu", 0.5, 8.0), ("F.Cu", 0.25, 6.0)]
    assert [(v.kind, v.label, v.from_layer, v.to_layer) for v in path.vias] == [
        ("via", "via (13, 20)", "F.Cu", "B.Cu"), ("via", "via (21, 20)", "B.Cu", "F.Cu")]
    assert path.net == "/CHAIN" and path.via_count == 2 and path.warnings == []


def test_rotated_and_flipped_footprints_put_pads_where_kicad_does(paths):
    # R1 (70, 42) 45 derece: pad 2 (+1, 0) -> (70 + cos45, 42 - sin45) (KiCad: y aşağı, açı saat yönünün tersine).
    # C1 (71, 54) alt yüze çevrildi (sol-sağ): pad 1 (-1, 0) -> (72, 54).
    (r1,), (c1,) = paths.find_pads("R1", "2"), paths.find_pads("C1", "1")
    assert r1.at == pytest.approx((70 + math.sqrt(0.5), 42 - math.sqrt(0.5)), abs=NM)
    assert c1.at == pytest.approx((72.0, 54.0), abs=NM) and c1.layers == ("B.Cu",)
    path = paths.trace_path("R1.2", "C1.1")
    assert [(p.layer, round(p.length_mm, 9)) for p in path.pieces] == [("F.Cu", 18.0), ("B.Cu", 4.0)]
    assert [(v.from_layer, v.to_layer) for v in path.vias] == [("F.Cu", "B.Cu")]


def test_branch_inside_a_wide_track_is_two_pieces(paths):
    path = paths.trace_path("TP15.1", "TP17.1")
    assert [(p.width_mm, round(p.length_mm, 9)) for p in path.pieces] == [(2.0, 10.0), (0.25, 4.2)]


def test_parallel_route_is_reported_and_only_there(paths):
    loop = paths.trace_path("TP18.1", "TP19.1")
    assert len(loop.warnings) == 1 and "Paralel" in loop.warnings[0] and "20 mm" in loop.warnings[0]
    for start, end, _, _ in PATHS:
        if not start.startswith("TP18"):
            assert not any("Paralel" in w for w in paths.trace_path(start, end).warnings), (start, end)


@pytest.mark.parametrize("start, end, words", [
    ("TP20.1", "TP21.1", ["iz yolu yok", "dökümü"]),         # /POUR: yalnız bakır dökümle bağlı
    ("TP25.1", "TP26.1", ["iz yolu yok", "yönlendirilmemiş"]),  # /GAP: 2 mm boşluk
    ("TP27.1", "TP28.1", ["JP1", "içinden"]),                  # /JMP: aynı numaralı padler (KiCad jumper)
    ("TP29.1", "TP30.1", ["JP2", "içinden"]),                  # /JGRP: jumper_pad_groups
    ("TP1.1", "TP2.1", ["aynı nette değil"]),
    ("TP1.1", "TP1.1", ["aynı pad"]),
    ("TP99.1", "TP1.1", ["'TP99' adlı bir bileşen yok"]),
    ("TP1.7", "TP1.1", ["'7' numaralı pad yok", "padleri: 1"]),
    ("TP1", "TP2.1", ["REF.PAD"]),
])
def test_unmodelable_connections_are_errors_not_guesses(paths, start, end, words):
    with pytest.raises(BoardPathError) as info:
        paths.trace_path(start, end)
    for word in words:
        assert word in str(info.value)


def test_pad_without_net_is_an_error(paths):
    with pytest.raises(BoardPathError, match="nete bağlı değil"):
        paths.trace_path("R1.1", "R1.2")   # R1.1 bir nete bağlanmadı (kicad_oracle.py build_paths)


def test_plane_coverage_of_path_pieces(paths):
    # /XSLOT B.Cu dökümünün /CHAIN izinin açtığı yarığın üstünden geçer: tanıkta örtme 0.84
    (piece,) = paths.trace_path("TP11.1", "TP12.1").pieces
    expected = next(t for t in _oracle("kicad10_paths")["tracks"] if t["net"] == "/XSLOT")["plane_under"]["B.Cu"]
    assert expected == pytest.approx(0.84) and piece.reference_layer == "B.Cu"
    assert piece.plane_coverage == pytest.approx(expected, abs=1e-9)
    # /CHAIN'in B.Cu parçası (y = 20): F.Cu'daki tek döküm /POUR (27..45, 42..50), parçanın altında değil
    chain = paths.trace_path("J1.1", "TP1.1").pieces
    assert (chain[2].reference_layer, chain[2].plane_coverage) == ("F.Cu", 0.0)
    # /ROT'un F.Cu parçası iki izden birleşir: örtme iki izin tanıktaki örneklerinin toplamından (test-reviewer)
    tracks = [t for t in _oracle("kicad10_paths")["tracks"] if t["net"] == "/ROT" and t["layer"] == "F.Cu"]
    counts = [max(1, math.ceil(t["length_mm"] / 0.1)) for t in tracks]
    inside = sum(round(t["plane_under"]["B.Cu"] * n) for t, n in zip(tracks, counts))
    assert len(tracks) == 2 and 0.0 < inside / sum(counts) < 1.0
    rot = paths.trace_path("R1.2", "C1.1").pieces[0]
    assert rot.plane_coverage == pytest.approx(inside / sum(counts), abs=1e-12)


def test_four_layer_paths(four):
    outer = four.trace_path("TP1.1", "TP2.1")
    (piece,) = outer.pieces
    # F.Cu'nun komşusu In1.Cu'da döküm yok (GND In2.Cu'da): örtme 0, model "düzlem yok" der
    assert (piece.layer, piece.length_mm, piece.reference_layer, piece.plane_coverage) == ("F.Cu", 20.0, "In1.Cu", 0.0)
    blind = four.trace_path("TP3.1", "J3.1")
    assert [type(e) for e in blind.elements] == [PathPiece, PathVia, PathPiece]
    assert [(p.layer, round(p.length_mm, 9)) for p in blind.pieces] == [("F.Cu", 10.0), ("In1.Cu", 10.0)]
    assert (blind.vias[0].from_layer, blind.vias[0].to_layer) == ("F.Cu", "In1.Cu")
    with pytest.raises(BoardPathError, match="iz yolu yok"):
        four.trace_path("TP3.1", "TP4.1")   # kör via B.Cu'ya inmez
    inner = four.trace_path("J1.1", "J2.1")
    assert [(p.layer, round(p.length_mm, 9)) for p in inner.pieces] == [("In1.Cu", 20.0)]
    with pytest.raises(BoardPathError, match="iç katman"):
        inner.parasitics()


# --------------------------------------------------------------------------------------------------- parazitikler
def test_dielectric_and_copper_come_from_the_stackup(paths, four):
    # Dosyalardaki katman yapısı (KiCad 10.0.3 varsayılanı): 2 katman F.Cu 0.035 / core 1.51 er 4.5 / B.Cu 0.035;
    # 4 katman her dielektrik 0.48 er 4.5
    assert paths.reference_dielectric("F.Cu") == (pytest.approx(1.51), pytest.approx(4.5), "B.Cu")
    assert paths.reference_dielectric("B.Cu") == (pytest.approx(1.51), pytest.approx(4.5), "F.Cu")
    assert paths.copper_thickness_mm("F.Cu") == pytest.approx(0.035)
    assert four.reference_dielectric("F.Cu") == (pytest.approx(0.48), pytest.approx(4.5), "In1.Cu")
    assert four.reference_dielectric("B.Cu") == (pytest.approx(0.48), pytest.approx(4.5), "In2.Cu")


LAYERS_4L = '(layers (0 "F.Cu" signal) (4 "In1.Cu" signal) (6 "In2.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))'


@pytest.mark.parametrize("second_er, expected", [("4.3", (0.13626, 4.3)), ("3.9", None)])
def test_dielectric_split_into_sublayers(tmp_path, second_er, expected):
    """
    ``addsublayer`` sözdizimi KiCad 10.0.3 demosu ``tiny_tapeout/tinytapeout-demo.kicad_pcb`` satır 70-82: dielectric 1
    iki alt katman, her biri 0.06813 mm, er 4.3 -> H = 0.13626 (elle). Alt katmanların er'i farklıysa tek değer
    seçilmez. B.Cu tarafı (elle): dielectric 3 tek katman 0.2104 mm, er 4.5.
    """
    sub = '(thickness 0.06813) (material "R-1551(W)") (epsilon_r {}) (loss_tangent 0.02)'
    setup = ('(setup (stackup (layer "F.Cu" (type "copper") (thickness 0.035)) (layer "dielectric 1" (type "prepreg") '
             + sub.format("4.3") + " addsublayer " + sub.format(second_er) + ') (layer "In1.Cu" (type "copper") '
             '(thickness 0.0152)) (layer "dielectric 2" (type "core") (thickness 1.065) (epsilon_r 4.6)) '
             '(layer "In2.Cu" (type "copper") (thickness 0.0152)) (layer "dielectric 3" (type "prepreg") '
             '(thickness 0.2104) (epsilon_r 4.5)) (layer "B.Cu" (type "copper") (thickness 0.035))))')
    board = read_kicad_board(_write(tmp_path, board_text(point_pad("TP1", 0, 0, "/A"), setup=setup, layers=LAYERS_4L)))
    assert board.copper_layers == ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
    assert board.reference_dielectric("B.Cu") == (pytest.approx(0.2104), pytest.approx(4.5), "In2.Cu")
    if expected is None:
        with pytest.raises(BoardPathError, match="farklı geçirgenlikte"):
            board.reference_dielectric("F.Cu")
    else:
        assert board.reference_dielectric("F.Cu") == (pytest.approx(expected[0]), pytest.approx(expected[1]), "In1.Cu")


def test_chain_dc_resistance_by_hand(paths):
    # R = rho * sum(L / W) / T; sum(L/W) = 5/0.25 + 6/0.5 + 8/0.5 + 6/0.25 = 72; T = 0.035 mm (dosya)
    model = paths.trace_path("J1.1", "TP1.1").parasitics()
    assert model.r_dc == pytest.approx(RHO_25C * 72 / 0.035e-3, rel=1e-9)
    hot = paths.trace_path("J1.1", "TP1.1").parasitics(temp_c=125.0)
    assert hot.r_dc == pytest.approx(RHO_25C * (1 + TEMPCO * 100) * 72 / 0.035e-3, rel=1e-9)


def test_each_piece_uses_the_file_geometry(paths):
    model = paths.trace_path("J1.1", "TP1.1").parasitics()
    for trace, piece in zip(model.traces, model.path.pieces):
        expected = calculate_trace_parasitics(length_mm=piece.length_mm, width_mm=piece.width_mm, height_mm=1.51,
                                              copper_oz=0.035 / COPPER_MM_PER_OZ, er=4.5)
        assert (trace.r_dc, trace.l_trace, trace.c_trace) == pytest.approx(
            (expected.r_dc, expected.l_trace, expected.c_trace), rel=1e-12)
    assert len(model.traces) == 4
    assert model.l_trace == pytest.approx(sum(t.l_trace for t in model.traces), rel=1e-12)
    assert model.c_trace == pytest.approx(sum(t.c_trace for t in model.traces), rel=1e-12)


def test_inputs_override_the_stackup_and_vias_add_in_series(paths):
    path = paths.trace_path("J1.1", "TP1.1")
    base = path.parasitics()
    vias = path.parasitics(via_inductance_h=1e-9, via_resistance_ohm=1e-3)
    assert vias.r_dc == pytest.approx(base.r_dc + 2e-3, rel=1e-12)
    assert vias.l_trace == pytest.approx(base.l_trace + 2e-9, rel=1e-12)
    thin = path.parasitics(height_mm=0.2, er=4.0, copper_oz=2.0)
    assert thin.traces[0].r_dc == pytest.approx(RHO_25C * 5e-3 / (0.25e-3 * 2 * COPPER_MM_PER_OZ * 1e-3), rel=1e-9)
    assert thin.c_trace > base.c_trace   # daha ince dielektrik: daha büyük C
    with pytest.raises(ValueError, match="negatif"):
        path.parasitics(via_resistance_ohm=-1.0)
    assert any("modellenmedi" in n for n in base.notes) and any("girdi" in n for n in vias.notes)


def _elements(subckt):
    return [line.split() for line in subckt.splitlines() if line and line[0] in "RLC"]


def test_subcircuit_is_a_series_chain_from_in_to_out(paths):
    model = paths.trace_path("J1.1", "TP1.1").parasitics()
    text = model.to_spice_subckt("P")
    assert text.startswith(".subckt P in out ref\n") and text.endswith(".ends P\n")
    parts = _elements(text)
    resistors = [p for p in parts if p[0][0] == "R"]
    assert sum(parse_spice_value(p[3]) for p in resistors) == pytest.approx(model.r_dc, rel=1e-5)
    values = {p[0]: parse_spice_value(p[3]) for p in parts}
    for k, trace in enumerate(model.traces, start=1):   # T hücresi: R/2 - L/2 - C - L/2 - R/2 (6 anlamlı hane)
        assert values[f"R{k}a"] + values[f"R{k}b"] == pytest.approx(trace.r_dc, rel=1e-5)
        assert values[f"L{k}a"] + values[f"L{k}b"] == pytest.approx(trace.l_trace, rel=1e-5)
        assert values[f"C{k}"] == pytest.approx(trace.c_trace, rel=1e-5)
    assert [p[2] for p in parts if p[0][0] == "C"] == ["ref"] * 4
    assert all(p[-1] == "Rser=0" for p in parts if p[0][0] == "L")
    # Seri zincir: her iç düğüm tam iki elemana değer (C'nin orta düğümü üç), in ve out bir elemana
    counts = {}
    for p in parts:
        for node in p[1:3]:
            counts[node] = counts.get(node, 0) + 1
    assert counts.pop("in") == 1 and counts.pop("out") == 1 and counts.pop("ref") == 4
    assert {n: c for n, c in counts.items() if c != 2} == {f"n{k}m": 3 for k in range(1, 5)}


def test_subcircuit_with_via_values_puts_them_between_the_pieces(paths):
    text = paths.trace_path("J1.1", "TP1.1").parasitics(via_inductance_h=1e-9, via_resistance_ohm=1e-3).to_spice_subckt()
    parts = {p[0]: p[1:] for p in _elements(text)}
    assert parts["Rv1"][:2] == ["j2", "v1"] and parts["Lv1"][:2] == ["v1", "j3"]
    assert parts["R2b"][1] == "j2" and parts["R3a"][0] == "j3"
    assert parse_spice_value(parts["Lv1"][2]) == pytest.approx(1e-9)


@pytest.mark.parametrize("given, expected", [
    ({"via_inductance_h": 2e-9}, {"Lv1": ("j2", "j3", 2e-9)}),
    ({"via_resistance_ohm": 5e-3}, {"Rv1": ("j2", "j3", 5e-3)}),
])
def test_subcircuit_with_only_one_via_value(paths, given, expected):
    model = paths.trace_path("J1.1", "TP1.1").parasitics(**given)
    parts = {p[0]: p[1:] for p in _elements(model.to_spice_subckt())}
    vias = {name: (a, b, parse_spice_value(v)) for name, (a, b, v, *_) in parts.items() if name[1] == "v"}
    assert set(vias) == {name[:2] + "1" for name in expected} | {name[:2] + "2" for name in expected}
    for name, (a, b, value) in expected.items():
        assert vias[name][:2] == (a, b) and vias[name][2] == pytest.approx(value)
    total_r = sum(parse_spice_value(v) for n, (_, _, v, *_) in parts.items() if n[0] == "R")
    total_l = sum(parse_spice_value(v) for n, (_, _, v, *_) in parts.items() if n[0] == "L")
    assert (total_r, total_l) == (pytest.approx(model.r_dc, rel=1e-5), pytest.approx(model.l_trace, rel=1e-5))


@pytest.mark.parametrize("given", [{"er": 4.0}, {"height_mm": 0.2}, {"copper_oz": 2.0}])
def test_one_input_overrides_only_that_stackup_value(paths, given):
    # Verilmeyenler dosyadan: H 1.51, er 4.5, T 0.035 mm (tests/data/kicad10_paths.kicad_pcb setup)
    inputs = {"height_mm": 1.51, "er": 4.5, "copper_oz": 0.035 / COPPER_MM_PER_OZ, **given}
    (trace,) = paths.trace_path("TP11.1", "TP12.1").parasitics(**given).traces
    expected = calculate_trace_parasitics(length_mm=10.0, width_mm=0.25, **inputs)
    assert (trace.r_dc, trace.l_trace, trace.c_trace) == pytest.approx(
        (expected.r_dc, expected.l_trace, expected.c_trace), rel=1e-12)


def test_injected_netlist_says_where_the_numbers_came_from(paths):
    model = paths.trace_path("J1.1", "TP1.1").parasitics()
    netlist = "* test\nV1 a 0 1\nR1 b 0 1k\n.op\n.end\n"
    out = inject_board_path_into_netlist(netlist, "a", "b", model)
    head, tail = out.split(".end\n", 1)[0], out.split(".end\n", 1)[1]
    assert tail == "" and head.splitlines().count("X_TR1 a b 0 PCB_PATH_TR1") == 1
    assert head.splitlines().count(".subckt PCB_PATH_TR1 in out ref") == 1
    assert head.splitlines().count(".ends PCB_PATH_TR1") == 1
    comment = next(line for line in head.splitlines() if "Karttan okunan" in line)
    assert "kicad10_paths.kicad_pcb" in comment and paths.sha256[:12] in comment and "J1.1 -> TP1.1" in comment
    assert str(DATA) not in out and "\\" not in comment


# --------------------------------------------------------------------------------------------------- dosya okuma
def test_board_without_stackup_needs_the_values_as_inputs(tmp_path):
    board = read_kicad_board(_write(tmp_path, board_text(
        point_pad("TP1", 0, 0, "/A"), segment("/A", 0, 0, 10, 0), point_pad("TP2", 10, 0, "/A"), setup="")))
    path = board.trace_path("TP1.1", "TP2.1")
    assert path.length_mm == pytest.approx(10.0)
    with pytest.raises(BoardPathError, match="stackup"):
        path.parasitics()
    model = path.parasitics(height_mm=1.51, er=4.5, copper_oz=1.0)
    assert model.r_dc == pytest.approx(RHO_25C * 10e-3 / (0.25e-3 * COPPER_MM_PER_OZ * 1e-3), rel=1e-9)


def test_kicad9_numbered_nets(tmp_path):
    """
    KiCad 9 ve öncesi netleri numarayla yazar. Sözdiziminin kaynağı KiCad 10.0.3 demosu
    ``ecc83/ecc83-pp.kicad_pcb`` (version 20241229): satır 120-122 net tablosu ``(net 2 "Net-(P3-P1)")``, satır 2183 pad
    ``(net 2 "Net-(P3-P1)")``, satır 5196 segment ``(net 2)``.
    """
    text = board_text(
        footprint("P1", 0, 0, [("1", 0, 0, '2 "Net-(P3-P1)"', {})]),
        footprint("P3", 10, 0, [("1", 0, 0, '2 "Net-(P3-P1)"', {"layers": '"B.Cu"'})]),
        footprint("P4", 0, 5, [("1", 0, 0, '1 "GND"', {})]),
        '(segment (start 0 0) (end 4 0) (width 0.8) (layer "F.Cu") (net 2))',
        '(via (at 4 0) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 2))',
        '(segment (start 4 0) (end 10 0) (width 0.8) (layer "B.Cu") (net 2))',
        version=20241229, header='(net 0 "") (net 1 "GND") (net 2 "Net-(P3-P1)")')
    board = read_kicad_board(_write(tmp_path, text))
    assert {t.net for t in board.tracks} == {"Net-(P3-P1)"} and board.vias[0].net == "Net-(P3-P1)"
    assert board.nets() == {"GND": ["P4.1"], "Net-(P3-P1)": ["P1.1", "P3.1"]}
    path = board.trace_path("P1.1", "P3.1")
    assert path.length_mm == pytest.approx(10.0) and path.via_count == 1
    assert board.warnings == []   # 20241229 karşılaştırılan sürümlerden


def test_through_hole_pad_counts_as_a_layer_change(tmp_path):
    # Delikli pad (F.Cu + B.Cu) via gibi katman değiştirir: TP1 -> J1.1 F.Cu 5, J1.1 -> TP2 B.Cu 5
    th = {"kind": "thru_hole", "shape": "circle", "size": (1.6, 1.6), "layers": '"*.Cu" "*.Mask"'}
    board = read_kicad_board(_write(tmp_path, board_text(
        point_pad("TP1", 0, 0, "/A"), segment("/A", 0, 0, 5, 0), footprint("J1", 5, 0, [("1", 0, 0, '"/A"', th)]),
        segment("/A", 5, 0, 10, 0, layer="B.Cu"), point_pad("TP2", 10, 0, "/A", layers='"B.Cu"'))))
    path = board.trace_path("TP1.1", "TP2.1")
    assert [(v.kind, v.label, v.from_layer, v.to_layer) for v in path.vias] == [("pad", "pad J1.1", "F.Cu", "B.Cu")]
    assert [p.layer for p in path.pieces] == ["F.Cu", "B.Cu"]


def test_via_in_smd_pad_is_a_layer_change(tmp_path):
    # SMD pad F.Cu'da, içindeki via B.Cu izine iner: yolun ucundaki padin katmanı da sayılır
    board = read_kicad_board(_write(tmp_path, board_text(
        point_pad("TP1", 0, 0, "/A", size=(1.0, 1.0)), via("/A", 0, 0), segment("/A", 0, 0, 10, 0, layer="B.Cu"),
        point_pad("TP2", 10, 0, "/A", layers='"B.Cu"'))))
    path = board.trace_path("TP1.1", "TP2.1")
    assert [(v.kind, v.from_layer, v.to_layer) for v in path.vias] == [("via", "F.Cu", "B.Cu")]
    reverse = board.trace_path("TP2.1", "TP1.1")
    assert [(v.from_layer, v.to_layer) for v in reverse.vias] == [("B.Cu", "F.Cu")]


@pytest.mark.parametrize("point, inside", [
    ((5, 5), True), ((5, 0), True), ((0, 5), True), ((10, 5), True), ((5, 10), True), ((10, 10), True),
    ((5, -0.001), False), ((10.001, 5), False), ((5, 10.001), False),
])
def test_fill_boundary_counts_as_inside(point, inside):
    """
    KiCad 10.0.3 ``HitTestFilledArea`` döküm kenarındaki noktayı içeride sayar: interf_u demosunda GND dökümünün
    kenarı /PC-DB7 izinin orta çizgisiyle çakışıyordu, tanık örtmeyi 1.0 verdi (26/26 nokta; examples/20, 2026-11-13).
    """
    import numpy as np

    from ltspice_llm.board import _inside_fill

    xs, ys = np.array([0.0, 10.0, 10.0, 0.0]), np.array([0.0, 0.0, 10.0, 10.0])
    assert _inside_fill(*point, (xs, ys, np.roll(xs, -1), np.roll(ys, -1), (0.0, 0.0, 10.0, 10.0))) == inside


def test_copper_drawing_on_a_net_is_reported(tmp_path):
    """
    Bakır katmandaki nete bağlı dolu çizim (KiCad 10.0.3 demosu ``openair-max/One-Air-Max.kicad_pcb`` satır 45573:
    ``(gr_rect (start ..) (end ..) (fill yes) (layer "F.Cu") (net 1))``) KiCad'de padleri bağlar; okuyucu onu bakır
    saymaz, sayılmadığını söyler.
    """
    rect = '(gr_rect (start -1 -1) (end 11 1) (stroke (width 0.1) (type solid)) (fill yes) (layer "F.Cu") (net 1))'
    pads = [footprint("TP1", 0, 0, [("1", 0, 0, '1 "/A"', {})]), footprint("TP2", 10, 0, [("1", 0, 0, '1 "/A"', {})])]
    header = '(net 0 "") (net 1 "/A")'
    only = read_kicad_board(_write(tmp_path, board_text(rect, *pads, version=20241229, header=header), "a.kicad_pcb"))
    assert only.drawing_nets == ("/A",)
    with pytest.raises(BoardPathError, match="çizimi"):
        only.trace_path("TP1.1", "TP2.1")
    routed = read_kicad_board(_write(tmp_path, board_text(
        rect, *pads, '(segment (start 0 0) (end 10 0) (width 0.25) (layer "F.Cu") (net 1))', version=20241229,
        header=header), "b.kicad_pcb"))
    path = routed.trace_path("TP1.1", "TP2.1")
    assert path.length_mm == pytest.approx(10.0) and sum("çizimi" in w for w in path.warnings) == 1
    silk = '(gr_rect (start -1 -1) (end 11 1) (stroke (width 0.1) (type solid)) (fill no) (layer "F.SilkS"))'
    assert read_kicad_board(_write(tmp_path, board_text(silk, *pads, version=20241229, header=header))).drawing_nets == ()


def _zone(net, layer, fill_xy=None):
    """Döküm (sözdizimi tests/data/kicad10_paths.kicad_pcb satır 2672); ``fill_xy`` yoksa doldurulmamış kaydedilmiş."""
    outline = "(xy -5 -5) (xy 15 -5) (xy 15 5) (xy -5 5)"
    filled = (f'(filled_polygon (layer "{layer}") (pts {" ".join(f"(xy {x} {y})" for x, y in fill_xy)}))'
              if fill_xy else "")
    return f'(zone (net "{net}") (layer "{layer}") (polygon (pts {outline})) {filled})'


def _straight(*extra):
    return board_text(point_pad("TP1", 0, 0, "/A"), segment("/A", 0, 0, 10, 0), point_pad("TP2", 10, 0, "/A"), *extra)


def test_warnings_for_own_pour_and_unfilled_reference_plane(tmp_path):
    square = [(-5, -5), (15, -5), (15, 5), (-5, 5)]
    own = read_kicad_board(_write(tmp_path, _straight(_zone("/A", "F.Cu", square)), "own.kicad_pcb"))
    path = own.trace_path("TP1.1", "TP2.1")
    assert sum("akım dökümden de akar" in w for w in path.warnings) == 1
    # B.Cu'daki GND dökümü doldurulmadan kaydedilmiş: örtme ölçülemez (None), uyarı ve not
    unfilled = read_kicad_board(_write(tmp_path, _straight(_zone("/GND", "B.Cu")), "unfilled.kicad_pcb"))
    path = unfilled.trace_path("TP1.1", "TP2.1")
    assert (path.pieces[0].plane_coverage, path.pieces[0].reference_layer) == (None, "B.Cu")
    assert sum("doldurulmadan" in w for w in path.warnings) == 1
    assert sum("ölçülemedi" in n for n in path.parasitics().notes) >= 1
    filled = read_kicad_board(_write(tmp_path, _straight(_zone("/GND", "B.Cu", square)), "filled.kicad_pcb"))
    assert filled.trace_path("TP1.1", "TP2.1").pieces[0].plane_coverage == 1.0 and filled.trace_path(
        "TP1.1", "TP2.1").warnings == []


def test_track_drawn_twice_is_not_a_parallel_route(tmp_path):
    board = read_kicad_board(_write(tmp_path, _straight(segment("/A", 0, 0, 10, 0))))
    path = board.trace_path("TP1.1", "TP2.1")
    assert path.length_mm == pytest.approx(10.0) and path.warnings == []
    looped = read_kicad_board(_write(tmp_path, _straight(segment("/A", 0, 0, 0, 3), segment("/A", 0, 3, 10, 3),
                                                         segment("/A", 10, 3, 10, 0)), "loop.kicad_pcb"))
    assert sum("Paralel" in w and "16 mm" in w for w in looped.trace_path("TP1.1", "TP2.1").warnings) == 1


@pytest.mark.parametrize("end_item", ["pad", "via"])
def test_arc_ending_on_a_pad_or_via_is_measured_to_its_end(tmp_path, end_item):
    """
    Ark kirişlere bölünür; ucundaki pada / via'ya değen birkaç kirişin her biri bağ kurarsa yol ucu kısaltır. Çeyrek
    daire: merkez (5, 0), r = 5, (0, 0) -> (5, 5); uzunluk 2.5 pi (elle). Sözdizimi tests/data/kicad10_paths.kicad_pcb
    satır 2503'teki ``(arc (start) (mid) (end) ...)``.
    """
    half = 5 * math.sqrt(0.5)
    arc = f'(arc (start 0 0) (mid {5 - half!r} {half!r}) (end 5 5) (width 0.25) (layer "F.Cu") (net "/A"))'
    items = [point_pad("TP1", 0, 0, "/A", size=(1.2, 1.2)), arc]
    if end_item == "pad":
        items.append(point_pad("TP2", 5, 5, "/A", size=(1.2, 1.2)))
    else:   # ark via'da biter, via'dan B.Cu izi TP2'ye gider: 5 mm
        items += [via("/A", 5, 5, size=1.2), segment("/A", 5, 5, 10, 5, layer="B.Cu"),
                  point_pad("TP2", 10, 5, "/A", layers='"B.Cu"')]
    board = read_kicad_board(_write(tmp_path, board_text(*items)))
    expected = 2.5 * math.pi + (0.0 if end_item == "pad" else 5.0)
    for start, end in (("TP1.1", "TP2.1"), ("TP2.1", "TP1.1")):
        assert board.trace_path(start, end).length_mm == pytest.approx(expected, abs=1e-5)


def test_touching_pads_only_is_not_a_trace(paths):
    # /TOUCH: iz yok, iki padin bakırı örtüşüyor; KiCad tanığı ikisini tek ada sayar
    assert _oracle("kicad10_paths")["islands"]["/TOUCH"] == [["TP31.1", "TP32.1"]]
    path = paths.trace_path("TP31.1", "TP32.1")
    assert path.pieces == [] and path.length_mm == 0.0
    with pytest.raises(BoardPathError, match="iz yok"):
        path.parasitics()


@pytest.mark.parametrize("text, message", [
    ("(kicad_sch (version 20260206))", "kart dosyası"),
    ("(kicad_pcb (version 20171130))", "KiCad 6'dan eski"),
    ("(kicad_pcb (version 20260206)", "kapanmayan"),
    ("(kicad_pcb (version 20260206)))", "fazladan"),
])
def test_files_that_are_not_readable_boards(tmp_path, text, message):
    with pytest.raises(ValueError, match=message):
        read_kicad_board(_write(tmp_path, text))


def test_size_limit_and_untested_format_version(tmp_path):
    path = _write(tmp_path, board_text(point_pad("TP1", 0, 0, "/A"), version=20230121))
    with pytest.raises(ValueError, match="sınır"):
        read_kicad_board(path, max_bytes=100)
    board = read_kicad_board(path)
    assert len(board.warnings) == 1 and "20230121" in board.warnings[0]


def test_summary_names_the_file_not_the_folder(paths):
    summary = paths.summary()
    assert summary["file"] == "kicad10_paths.kicad_pcb" and summary["sha256"] == paths.sha256
    assert summary["title"] == "ltspice-llm test karti: yollar" and summary["revision"] == "A"
    assert summary["counts"]["arcs"] == 1 and summary["counts"]["vias"] == 3
    assert "tests" not in json.dumps(summary)


def test_parse_sexpr_keeps_quoted_values_apart_and_skips_subtrees():
    tree = parse_sexpr('(a (net 5) (net "5") (s "x \\"y\\" \\\\ z") (fp_line (start 0 0)) (b))')
    assert tree == [["a", ["net", "5"], ["net", "5"], ["s", 'x "y" \\ z'], ["b"]]]
    assert type(tree[0][1][1]).__name__ == "str" and type(tree[0][2][1]).__name__ == "_Quoted"


# --------------------------------------------------------------------------------------------------- araç
@pytest.fixture
def workspace(tmp_path):
    """Gerçek LTspice çalıştırmayan, çalışma alanı mantığını kullanan adaptör (tests/test_compare_export.py gibi);
    çalışma alanında sentetik yol kartı 'kart.kicad_pcb' adıyla durur."""
    adapter = LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path / "ws")
    adapter.workspace_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(DATA / "kicad10_paths.kicad_pcb", adapter.workspace_dir / "kart.kicad_pcb")
    return adapter


def _board_tool(adapter, board_path="kart.kicad_pcb", **args):
    return handle_tool_call("read_board_trace", {"board_path": board_path, **args}, adapter=adapter)


def _c_pf_per_mm(width_mm, height_mm=1.51, er=4.5, copper_mm=0.035):
    """MT-094 Denk. 4 (IPC-2141): C0 = 0.67 (er + 1.41) / ln(5.98 H / (0.8 W + T)) pF/in; mm başına 25.4'e bölünür."""
    return 0.67 * (er + 1.41) / math.log(5.98 * height_mm / (0.8 * width_mm + copper_mm)) / 25.4


def _z0_ohm(width_mm, height_mm=1.51, er=4.5, copper_mm=0.035):
    """MT-094 Denk. 3 (IPC-2141): Z0 = 87 / sqrt(er + 1.41) * ln(5.98 H / (0.8 W + T)) Ohm."""
    return 87.0 / math.sqrt(er + 1.41) * math.log(5.98 * height_mm / (0.8 * width_mm + copper_mm))


#: /CHAIN (examples/kicad_oracle.py build_paths): yol sırasıyla (genişlik mm, uzunluk mm); parçalar arasında iki via
CHAIN = [(0.25, 5.0), (0.5, 6.0), (0.5, 8.0), (0.25, 6.0)]


def test_tool_reads_the_chain_path_from_the_board_file(workspace):
    """
    J1.1 -> TP1.1 (/CHAIN): F.Cu 0.25 mm x 5 + F.Cu 0.5 x 6 + via + B.Cu 0.5 x 8 + via + F.Cu 0.25 x 6 = 25 mm (kartın
    koordinatları). Katman yapısı kart dosyasından (KiCad 10.0.3'ün yazdığı): bakır 0.035 mm, çekirdek 1.51 mm, er 4.5.
    Elle, 25 °C: R = rho / T * sum(L / W) = 1.724e-8 / 35e-6 * (20 + 12 + 16 + 24) = 35.4651 mOhm (ADI Bl.12 s.7);
    C = sum(C0(W) * L), C0 MT-094 Denk. 4.
    """
    res = _board_tool(workspace, from_pad="J1.1", to_pad="TP1.1")
    assert res["status"] == "SUCCESS", res
    path = res["path"]
    assert (path["net"], path["from_pad"], path["to_pad"]) == ("/CHAIN", "J1.1", "TP1.1")
    assert [(p["layer"], p["width_mm"]) for p in path["pieces"]] == [
        ("F.Cu", 0.25), ("F.Cu", 0.5), ("B.Cu", 0.5), ("F.Cu", 0.25)]
    assert [p["length_mm"] for p in path["pieces"]] == pytest.approx([5.0, 6.0, 8.0, 6.0], abs=NM)
    assert path["length_mm"] == pytest.approx(25.0, abs=NM) and path["via_count"] == 2
    assert res["resistance_ohm"] == pytest.approx(RHO_25C / 35e-6 * sum(l / w for w, l in CHAIN), rel=1e-9)
    assert res["capacitance_pf"] == pytest.approx(sum(_c_pf_per_mm(w) * l for w, l in CHAIN), rel=1e-9)
    # Kayıpsız hat L0 = Z0^2 C0 (Ohm^2 x pF = pH); tpd MT-094 Denk. 5, genişlikten bağımsız: 1.017 sqrt(0.475 er + 0.67)
    # ns/ft, 25 mm = 25 / 304.8 ft
    assert res["inductance_nh"] == pytest.approx(
        sum(_z0_ohm(w) ** 2 * _c_pf_per_mm(w) * l for w, l in CHAIN) / 1e3, rel=1e-9)
    assert res["propagation_delay_ps"] == pytest.approx(1.017e3 * math.sqrt(0.475 * 4.5 + 0.67) * 25.0 / 304.8, rel=1e-9)
    assert res["board"]["file"] == "kart.kicad_pcb"
    assert res["board"]["sha256"] == hashlib.sha256((DATA / "kicad10_paths.kicad_pcb").read_bytes()).hexdigest()
    lines = res["spice_subcircuit"].splitlines()
    assert lines[0] == ".subckt PCB_PATH_J1_1_TP1_1 in out ref" and lines[-1] == ".ends PCB_PATH_J1_1_TP1_1"
    assert [line.split()[0] for line in lines if line[:1] == "C"] == ["C1", "C2", "C3", "C4"]   # parça başına bir T
    assert not [line for line in lines if line.startswith(("Lv", "Rv"))]   # via değeri verilmedi: ideal kısa devre
    # Araç yalnız okur: çalışma alanında yeni dosya yok, kart değişmedi
    assert sorted(p.name for p in workspace.workspace_dir.iterdir()) == ["kart.kicad_pcb"]
    assert (workspace.workspace_dir / "kart.kicad_pcb").read_bytes() == (DATA / "kicad10_paths.kicad_pcb").read_bytes()


def test_tool_adds_the_given_via_values_in_series(workspace):
    """Via'nın değeri girdidir (board.py modül belgesi: via'lar sayılır, modellenmez). Verilirse her via'ya seri eklenir:
    /CHAIN'in iki via'sı için R +2 x 0.5 mOhm, L +2 x 1.2 nH; C değişmez."""
    bare = _board_tool(workspace, from_pad="J1.1", to_pad="TP1.1")
    vias = _board_tool(workspace, from_pad="J1.1", to_pad="TP1.1", via_inductance_nh=1.2, via_resistance_mohm=0.5)
    assert vias["status"] == "SUCCESS", vias
    assert vias["resistance_ohm"] - bare["resistance_ohm"] == pytest.approx(2 * 0.5e-3, rel=1e-9)
    assert vias["inductance_nh"] - bare["inductance_nh"] == pytest.approx(2 * 1.2, rel=1e-9)
    assert vias["capacitance_pf"] == bare["capacitance_pf"]
    lines = vias["spice_subcircuit"].splitlines()
    assert [line.split()[0] for line in lines if line.startswith(("Lv", "Rv"))] == ["Rv1", "Lv1", "Rv2", "Lv2"]


def test_tool_uses_the_given_inputs_instead_of_the_stackup(workspace):
    """Şemadaki isteğe bağlı girdiler kartın değerinin yerine geçer. Elle: 70 µm bakırda R = rho / 70e-6 * 72 (ADI
    Bl.12 s.7); 125 °C'de R, 25 °C değerinin 1 + 0.004 * 100 katı (Bl.12 s.8, "about 0.4%/°C"); H = 0.2 mm, er = 4.0
    ile C = sum(C0(W; 0.2 mm, 4.0) * L) (MT-094 Denk. 4)."""
    base = _board_tool(workspace, from_pad="J1.1", to_pad="TP1.1")
    thick = _board_tool(workspace, from_pad="J1.1", to_pad="TP1.1", copper_thickness_um=70)
    assert thick["resistance_ohm"] == pytest.approx(RHO_25C / 70e-6 * sum(l / w for w, l in CHAIN), rel=1e-9)
    hot = _board_tool(workspace, from_pad="J1.1", to_pad="TP1.1", temperature_c=125)
    assert hot["resistance_ohm"] / base["resistance_ohm"] == pytest.approx(1 + TEMPCO * 100, rel=1e-9)
    thin = _board_tool(workspace, from_pad="J1.1", to_pad="TP1.1", height_mm=0.2, relative_permittivity=4.0)
    assert thin["capacitance_pf"] == pytest.approx(sum(_c_pf_per_mm(w, 0.2, 4.0) * l for w, l in CHAIN), rel=1e-9)


def test_tool_refuses_a_board_outside_the_workspace_by_default(workspace, tmp_path):
    """handle_tool_call: allow_file_access False iken model çalışma alanının dışını okuyamaz (netlistteki .include
    politikası, netlist.file_access_problems). Dosya gerçekten var: ret yoldan gelir, dosyanın yokluğundan değil."""
    outside = tmp_path / "dis.kicad_pcb"
    shutil.copy(DATA / "kicad10_paths.kicad_pcb", outside)
    res = _board_tool(workspace, str(outside), from_pad="J1.1", to_pad="TP1.1")
    assert res["status"] == "ERROR" and res["message"].startswith("Kart dosyası çalışma alanının içinde olmalı")


def test_tool_reads_an_outside_board_when_file_access_is_allowed(workspace, tmp_path):
    """Girdi güvenilirse (allow_file_access=True) mutlak yol okunur; aynı yol izinsiz reddedilir."""
    outside = tmp_path / "dis.kicad_pcb"
    shutil.copy(DATA / "kicad10_paths.kicad_pcb", outside)
    args = {"board_path": str(outside), "from_pad": "J1.1", "to_pad": "TP1.1"}
    assert handle_tool_call("read_board_trace", args, adapter=workspace)["status"] == "ERROR"
    allowed = handle_tool_call("read_board_trace", args, adapter=workspace, allow_file_access=True)
    assert allowed["status"] == "SUCCESS" and allowed["path"]["length_mm"] == pytest.approx(25.0, abs=NM)


def test_tool_resolves_a_relative_path_in_the_workspace_even_with_file_access(workspace, tmp_path, monkeypatch):
    """İzinli kipte de göreli yol çalışma alanına göredir, sürecin çalışma klasörüne göre değil (tools._board_file).
    Süreç, kartın olmadığı bir klasörde çalışıyor."""
    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / "kart.kicad_pcb").exists()
    args = {"board_path": "kart.kicad_pcb", "from_pad": "J1.1", "to_pad": "TP1.1"}
    res = handle_tool_call("read_board_trace", args, adapter=workspace, allow_file_access=True)
    assert res["status"] == "SUCCESS" and res["path"]["length_mm"] == pytest.approx(25.0, abs=NM)


@pytest.mark.parametrize("given", [
    "../dis.kicad_pcb", "alt/../../dis.kicad_pcb", "~/dis.kicad_pcb", "%TEMP%/dis.kicad_pcb", "C:dis.kicad_pcb",
])
def test_tool_refuses_paths_that_can_leave_the_workspace(workspace, tmp_path, given):
    """Netlist politikasının reddettiği yol biçimleri (netlist._outside_workspace): '..', '~', '%VAR%', sürücü harfi.
    '../dis.kicad_pcb' gerçekten var (çalışma alanının bir üstü)."""
    shutil.copy(DATA / "kicad10_paths.kicad_pcb", tmp_path / "dis.kicad_pcb")
    res = _board_tool(workspace, given, from_pad="J1.1", to_pad="TP1.1")
    assert res["status"] == "ERROR" and res["message"].startswith("Kart dosyası çalışma alanının içinde olmalı")


def _link_directory(target, link):
    """Dizin bağlantısı: Windows'ta junction (yönetici izni istemez), öbür sistemlerde symlink."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def test_tool_refuses_a_link_that_leaves_the_workspace(workspace, tmp_path):
    """Yol metni çalışma alanının içinde, ama oradaki dizin bağlantısı dışarıyı gösteriyor: çözülmüş yol denetlenir.
    Hedefin adı çalışma alanınınkiyle ('ws') başlıyor: metin öneki karşılaştırması ('ws-dis' 'ws' ile başlar) yetmez.
    Önce korumasız kol: bağlantıdan geçen yol dosyaya gerçekten ulaşıyor."""
    outside = tmp_path / "ws-dis"
    outside.mkdir()
    shutil.copy(DATA / "kicad10_paths.kicad_pcb", outside / "kart.kicad_pcb")
    _link_directory(outside, workspace.workspace_dir / "bag")
    assert (workspace.workspace_dir / "bag" / "kart.kicad_pcb").read_bytes()[:10] == b"(kicad_pcb"
    res = _board_tool(workspace, "bag/kart.kicad_pcb", from_pad="J1.1", to_pad="TP1.1")
    assert res["status"] == "ERROR" and res["message"].startswith("Kart dosyası çalışma alanının içinde olmalı")


@pytest.mark.parametrize("name", ["kart.txt", "kart.kicad_pro", "kart"])
def test_tool_reads_only_kicad_board_files(workspace, name):
    """Yalnız .kicad_pcb okunur. Dosyanın içi geçerli bir kart: ret uzantıdan gelir."""
    shutil.copy(DATA / "kicad10_paths.kicad_pcb", workspace.workspace_dir / name)
    res = _board_tool(workspace, name, from_pad="J1.1", to_pad="TP1.1")
    assert res["status"] == "ERROR" and res["message"].startswith("Yalnız KiCad kart dosyası (.kicad_pcb) okunur")


def test_tool_accepts_an_upper_case_extension(workspace):
    """Windows'ta dosya adı büyük/küçük harfe duyarsızdır: 'BUYUK.KICAD_PCB' de bir kart dosyasıdır."""
    shutil.copy(DATA / "kicad10_paths.kicad_pcb", workspace.workspace_dir / "BUYUK.KICAD_PCB")
    res = _board_tool(workspace, "BUYUK.KICAD_PCB", from_pad="J1.1", to_pad="TP1.1")
    assert res["status"] == "SUCCESS" and res["path"]["length_mm"] == pytest.approx(25.0, abs=NM)


def test_tool_reports_a_missing_board_and_missing_pads(workspace):
    missing = _board_tool(workspace, "yok.kicad_pcb", from_pad="J1.1", to_pad="TP1.1")
    assert missing == {"status": "ERROR", "message": "Kart dosyası bulunamadı: 'yok.kicad_pcb'"}
    no_pad = _board_tool(workspace, from_pad="J1.1")
    assert no_pad["status"] == "ERROR" and no_pad["message"].startswith("'from_pad' ve 'to_pad' gereklidir")
    unknown = _board_tool(workspace, from_pad="J9.1", to_pad="TP1.1")
    assert unknown["status"] == "ERROR" and unknown["message"] == "Kartta 'J9' adlı bir bileşen yok."


SECRET = "GIZLI-7f3a"
UNREADABLE = "sir.kicad_pcb: KiCad kart dosyası olarak okunamadı (bozuk ya da bir .kicad_pcb dosyası değil)."


@pytest.mark.parametrize("text", [
    f'(kicad_pcb (version 20260206) "{SECRET}',
    f'(kicad_pcb (version 20260206) (segment (start {SECRET} 1) (end 2 2) (width 0.2) (layer "F.Cu") (net "/A")))',
], ids=["kapanmayan-tirnak", "sayi-donusumu"])
def test_tool_does_not_echo_the_contents_of_an_unreadable_file(workspace, text):
    """Ayrıştırıcının iletisi dosyadan 40 karaktere kadar metin, float dönüşümünün iletisi bir belirteç taşır. Araç
    bunu modele yansıtmaz: model çalışma alanındaki bir dosyayı .kicad_pcb adıyla okutup içeriğini hata iletisinden
    öğrenememeli. Önce korumasız kol: okuyucunun kendi iletisi metni gerçekten taşıyor."""
    target = workspace.workspace_dir / "sir.kicad_pcb"
    target.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=SECRET):
        read_kicad_board(target)
    res = _board_tool(workspace, "sir.kicad_pcb", from_pad="A.1", to_pad="B.1")
    assert res == {"status": "ERROR", "message": UNREADABLE}


@pytest.mark.parametrize("text, prefix", [
    ("(kicad_sch (version 20260206))", "sema.kicad_pcb: "),
    ('(kicad_pcb (version 20260206) (segment (start 0 0) (end 1 1) (layer "F.Cu") (net "/A")))',
     "kart dosyası: eksik alan"),
], ids=["kart-degil", "eksik-alan"])
def test_tool_keeps_the_readers_own_messages(workspace, text, prefix):
    """Okuyucunun kendi iletileri dosya içeriği taşımaz (dosya adı, sürüm numarası, sabit alan adı) ve araçtan olduğu
    gibi döner (board.read_kicad_board): genel iletiye çevrilmez."""
    target = workspace.workspace_dir / "sema.kicad_pcb"
    target.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError) as raised:
        read_kicad_board(target)
    assert str(raised.value).startswith(prefix)
    res = _board_tool(workspace, "sema.kicad_pcb", from_pad="A.1", to_pad="B.1")
    assert res == {"status": "ERROR", "message": str(raised.value)}


def test_tool_does_not_invent_a_length_for_a_pour_connection(workspace):
    """/POUR: TP20.1 ile TP21.1 yalnız F.Cu dökümüyle bağlı, aralarında iz yok (examples/kicad_oracle.py build_paths).
    Döküm bir iz değildir: uzunluk uydurulmaz, model kurulmaz."""
    res = _board_tool(workspace, from_pad="TP20.1", to_pad="TP21.1")
    assert res["status"] == "ERROR" and res["message"].count("Döküm bir iz değildir") == 1 and "spice_subcircuit" not in res


@pytest.mark.parametrize("stem, start, end, reason", [
    ("kicad10_paths", "TP25.1", "TP26.1", "yönlendirilmemiş olabilir"),   # /GAP: iki iz ucu arasında 2 mm boşluk
    ("kicad10_4layer", "J1.1", "J2.1", "stripline"),                     # /INNER: In1.Cu'daki iz
])
def test_tool_reports_why_there_is_no_model(workspace, stem, start, end, reason):
    shutil.copy(DATA / f"{stem}.kicad_pcb", workspace.workspace_dir / "k.kicad_pcb")
    res = _board_tool(workspace, "k.kicad_pcb", from_pad=start, to_pad=end)
    assert res["status"] == "ERROR" and res["message"].count(reason) == 1 and "spice_subcircuit" not in res


# --------------------------------------------------------------------------------------------------- taze tanık
@pytest.fixture(scope="module")
def checks():
    spec = importlib.util.spec_from_file_location("example_20_kicad_board_checks", EXAMPLES / "20_kicad_board_checks.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.timeout(600)
def test_fresh_kicad_witness_agrees_with_the_reader(checks, capsys):
    """KiCad kuruluysa test kartlarını geçici kopyada KiCad'e yeniden hesaplatır (examples/20)."""
    if checks.find_kicad_python() is None:
        pytest.skip("KiCad kurulu değil (KICAD_PYTHON ile verilebilir)")
    assert checks.main([]) == 0, capsys.readouterr().out
    assert capsys.readouterr().out.count(" aynı\n") == len(STEMS)
