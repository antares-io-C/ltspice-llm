"""
KiCad Kart Okuyucu (salt okunur)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Bir ``.kicad_pcb`` dosyasından iki pad arasındaki iz yolunu çıkarır: uzunluk, genişlik, katman, via ve katman yapısı
(stackup) karttan okunur, elle girilmez. Yol `parasitics.calculate_trace_parasitics` ile R/L/C'ye çevrilir ve
`inject_board_path_into_netlist` ile netliste eklenir.

Dosya yalnızca okunur, hiçbir şey yazılmaz; KiCad'in kurulu olması gerekmez (dosya düz metin S-ifadesidir).

Tanık: KiCad 10.0.3'ün kendi ``pcbnew`` hesabı (``examples/kicad_oracle.py`` kaydeder, ``tests/data/kicad10_*``):
iz uzunlukları, pad konumları, izle bağlı pad adaları ve dökümün örttüğü noktalar bu okuyucununkiyle karşılaştırılır.

Modelin sınırları (rapora yazılır, `BoardPath.warnings` ve `PathParasitics.notes`):

- Yol, iki pad arasındaki **en kısa iz yoludur**; netin toplam iz uzunluğu değildir (dallı nette toplam anlamsızdır).
- Bağlantı bakır dökümden (zone) geçiyorsa iz modeli uygulanmaz: uzunluk uydurulmaz, hata verilir.
- IPC-2141 microstrip denklemi izin altında kesintisiz bir dönüş düzlemi varsayar. Okuyucu komşu bakır katmandaki dolu
  dökümün yolu ne oranda örttüğünü ölçer (`PathPiece.plane_coverage`); düşükse model o parçada geçersizdir.
- İç katmandaki iz stripline'dır: microstrip denklemi uygulanmaz, hata verilir.
- Via'lar sayılır ama modellenmez (ideal kısa devre); değer girdi olarak verilebilir.
- Uzunluk, izlerin orta çizgisi boyunca ölçülür (KiCad'in iz uzunluğuyla aynı tanım); geniş bir izin içinde eksenden
  kaçık başlayan dalın eksene kadarki kısmı sayılmaz (en çok geniş izin yarı genişliği).
"""

from __future__ import annotations

import hashlib
import heapq
import math
import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple, Union

from .netlist import insert_before_end
from .parasitics import COPPER_MM_PER_OZ, TraceParasitics, _spice, calculate_trace_parasitics

Point = Tuple[float, float]
#: Orta çizgi parçası: (x1, y1, x2, y2, s1, s2); s, iz boyunca gerçek uzunluk (arkta yay uzunluğu)
Chord = Tuple[float, float, float, float, float, float]

#: KiCad 6.0'ın dosya biçimi. Daha eskisi (KiCad 5: ``module``, farklı katman numaraları) okunmaz.
OLDEST_FORMAT_VERSION = 20211014
#: Okuyucunun KiCad 10.0.3'ün kendi hesabıyla karşılaştırıldığı biçim sürümleri (``(version N)``): sentetik test kartları,
#: 3 MB altındaki 13 demo kartı ve üç gerçek proje kartının kopyası (examples/20_kicad_board_checks.py, 2026-11-13).
#: 20250513 yalnız 3 MB'tan büyük bir demoda var, karşılaştırılmadı. Başka sürümde uyarı verilir.
TESTED_FORMAT_VERSIONS = (20241030, 20241229, 20250907, 20260206)
#: Ayrıştırılacak en büyük dosya (bayt). Saf Python ayrıştırıcı 70+ MB'lık kartta dakikalar ve GB'lar harcar.
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
#: "İzin altında bakır var mı" örneklemesinin adımı (mm); examples/kicad_oracle.py aynı kuralı kullanır.
COVERAGE_STEP_MM = 0.1
#: 1 nm: KiCad'in iç birimi. Bakırın "değmesi" bu kadar örtüşme ister.
_EPS = 1e-6
#: Arkı kirişlere bölerken izin verilen en büyük sehim (mm)
_ARC_SAG_MM = 0.001


class BoardPathError(ValueError):
    """İki pad arasında modellenebilir bir iz yolu yok (pad bulunamadı, farklı net, yalnız döküm, iç katman...)."""


# --------------------------------------------------------------------------------------------------- S-ifade
class _Quoted(str):
    """Dosyada tırnak içinde yazılmış değer: ``(net "5")`` bir ad, ``(net 5)`` bir numaradır."""

    __slots__ = ()


_TOKEN_RE = re.compile(r'\s*(?:(\()|(\))|("(?:[^"\\]|\\.)*")|([^\s()"]+))')
_ESCAPES = {"n": "\n", "r": "\r", "t": "\t"}
#: Okuyucunun bakmadığı alt ağaçlar: liste kurulmadan atlanır (büyük kartta bellek ve süre).
_SKIPPED_HEADS = frozenset({
    "gr_text", "gr_text_box", "gr_bbox",
    "fp_line", "fp_rect", "fp_arc", "fp_circle", "fp_poly", "fp_curve", "fp_text_box", "model", "dimension", "image",
    "embedded_files", "group", "generated", "target", "effects", "primitives", "uuid", "tstamp", "table", "teardrops",
})


def parse_sexpr(text: str, skip: Iterable[str] = _SKIPPED_HEADS) -> List[Any]:
    """
    KiCad S-ifadesini iç içe listelere çevirir: ``(a (b 1) "c")`` -> ``["a", ["b", "1"], "c"]``. Tırnaklı değerler
    `_Quoted` olur. Başı ``skip`` içinde olan alt ağaçlar atlanır. Kapanmayan parantez ya da tırnak ValueError.
    """
    skipped = frozenset(skip)
    root: List[Any] = []
    stack: List[List[Any]] = [root]
    skip_depth = 0
    pos, end = 0, len(text)
    match = _TOKEN_RE.match
    while True:
        m = match(text, pos)
        if m is None:
            if text[pos:].strip():
                raise ValueError(f"S-ifade ayrıştırılamadı (konum {pos}): {text[pos:pos + 40].strip()!r}")
            break
        pos = m.end()
        opened, closed, quoted, atom = m.groups()
        if skip_depth:
            if opened:
                skip_depth += 1
            elif closed:
                skip_depth -= 1
            continue
        if opened:
            head = match(text, pos)
            if head is not None and head.group(4) in skipped and len(stack) > 1:
                skip_depth = 1
                continue
            node: List[Any] = []
            stack[-1].append(node)
            stack.append(node)
        elif closed:
            if len(stack) == 1:
                raise ValueError(f"S-ifade: fazladan ')' (konum {pos})")
            stack.pop()
        elif quoted:
            body = quoted[1:-1]
            if "\\" in body:
                body = re.sub(r"\\(.)", lambda e: _ESCAPES.get(e.group(1), e.group(1)), body, flags=re.DOTALL)
            stack[-1].append(_Quoted(body))
        else:
            stack[-1].append(atom)
        if pos >= end:
            break
    if len(stack) != 1 or skip_depth:
        raise ValueError("S-ifade: kapanmayan '('")
    return root


def _nodes(node: Sequence[Any], head: str) -> Iterator[List[Any]]:
    for child in node:
        if isinstance(child, list) and child and child[0] == head:
            yield child


def _node(node: Sequence[Any], head: str) -> Optional[List[Any]]:
    return next(_nodes(node, head), None)


def _floats(node: Optional[Sequence[Any]], count: int, what: str) -> List[float]:
    if node is None or len(node) < count + 1:
        raise ValueError(f"kart dosyası: eksik alan ({what})")
    return [float(v) for v in node[1:count + 1]]


def _text(node: Optional[Sequence[Any]], default: str = "") -> str:
    return str(node[1]) if node is not None and len(node) > 1 else default


# --------------------------------------------------------------------------------------------------- geometri
def _closest_seg_seg(p1x: float, p1y: float, q1x: float, q1y: float,
                     p2x: float, p2y: float, q2x: float, q2y: float) -> Tuple[float, float, float]:
    """
    İki doğru parçası arasındaki en kısa uzaklık ve o noktaların parametreleri: ``(d, s, t)``,
    noktalar ``P1 + s (Q1 - P1)`` ve ``P2 + t (Q2 - P2)``. Sıfır uzunluklu parça nokta sayılır.
    (C. Ericson, Real-Time Collision Detection, 5.1.9; bağımsız sınama: tests/test_properties.py.)
    """
    d1x, d1y = q1x - p1x, q1y - p1y
    d2x, d2y = q2x - p2x, q2y - p2y
    rx, ry = p1x - p2x, p1y - p2y
    a = d1x * d1x + d1y * d1y
    e = d2x * d2x + d2y * d2y
    f = d2x * rx + d2y * ry
    tiny = 1e-24
    if a <= tiny and e <= tiny:
        s = t = 0.0
    elif a <= tiny:
        s, t = 0.0, min(max(f / e, 0.0), 1.0)
    else:
        c = d1x * rx + d1y * ry
        if e <= tiny:
            t, s = 0.0, min(max(-c / a, 0.0), 1.0)
        else:
            b = d1x * d2x + d1y * d2y
            denom = a * e - b * b
            s = min(max((b * f - c * e) / denom, 0.0), 1.0) if denom > 1e-12 * a * e else 0.0
            t = (b * s + f) / e
            if t < 0.0:
                t, s = 0.0, min(max(-c / a, 0.0), 1.0)
            elif t > 1.0:
                t, s = 1.0, min(max((b - c) / a, 0.0), 1.0)
    dx = (p1x + s * d1x) - (p2x + t * d2x)
    dy = (p1y + s * d1y) - (p2y + t * d2y)
    best = (math.hypot(dx, dy), s, t)
    # Paralele yakın parçalarda yukarıdaki s yuvarlamayla kayar (ortak uçlu 100 mm'lik iki parça 0 yerine 9e-5 mm
    # verdi; test-reviewer, 2026-11-13). Dört uç-parça adayı da denenir: her biri gerçek bir nokta çiftidir, en küçüğü alınır.
    for own, (px, py), (ax, ay, ux, uy, length_sq), first in (
            (0.0, (p1x, p1y), (p2x, p2y, d2x, d2y, e), True), (1.0, (q1x, q1y), (p2x, p2y, d2x, d2y, e), True),
            (0.0, (p2x, p2y), (p1x, p1y, d1x, d1y, a), False), (1.0, (q2x, q2y), (p1x, p1y, d1x, d1y, a), False)):
        u = 0.0 if length_sq <= tiny else min(max(((px - ax) * ux + (py - ay) * uy) / length_sq, 0.0), 1.0)
        distance = math.hypot(px - (ax + u * ux), py - (ay + u * uy))
        if distance < best[0]:
            best = (distance, own, u) if first else (distance, u, own)
    return best


def _arc_geometry(start: Point, mid: Point, end: Point) -> Optional[Tuple[float, float, float, float, float]]:
    """Üç noktadan geçen yay: ``(cx, cy, r, a0, süpürme)`` (radyan; süpürme işaretli). Doğrusal üç nokta: None."""
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    det = 2.0 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    scale = max(abs(x1 - x3), abs(y1 - y3), abs(x1 - x2), abs(y1 - y2), _EPS)
    if abs(det) <= 1e-9 * scale * scale:
        return None
    k1, k2, k3 = x1 * x1 + y1 * y1, x2 * x2 + y2 * y2, x3 * x3 + y3 * y3
    cx = (k1 * (y2 - y3) + k2 * (y3 - y1) + k3 * (y1 - y2)) / det
    cy = (k1 * (x3 - x2) + k2 * (x1 - x3) + k3 * (x2 - x1)) / det
    radius = math.hypot(x1 - cx, y1 - cy)
    a0 = math.atan2(y1 - cy, x1 - cx)
    to_mid = (math.atan2(y2 - cy, x2 - cx) - a0) % (2.0 * math.pi)
    to_end = (math.atan2(y3 - cy, x3 - cx) - a0) % (2.0 * math.pi)
    sweep = to_end if to_mid <= to_end else to_end - 2.0 * math.pi
    return cx, cy, radius, a0, sweep


def _inside_fill(px: float, py: float, polygon: Tuple[Any, Any, Any, Any, Tuple[float, float, float, float]]) -> bool:
    """
    Nokta dolu çokgenin içinde mi (çift-tek kuralı; KiCad delikleri sıfır genişlikli yarıkla dış hatta bağlar). Kenara
    1 nm'den yakın nokta içeride sayılır: KiCad'in ``HitTestFilledArea``'sı da öyle (interf_u demosunda döküm kenarı bir
    izin orta çizgisiyle çakışıyordu, KiCad 26 noktanın hepsini içeride saydı; examples/20, 2026-11-13).
    """
    import numpy as np

    xs, ys, xn, yn, (min_x, min_y, max_x, max_y) = polygon
    if not (min_x - _EPS <= px <= max_x + _EPS and min_y - _EPS <= py <= max_y + _EPS):
        return False
    crossing = (ys > py) != (yn > py)
    if crossing.any():
        x_at = xs[crossing] + (py - ys[crossing]) * (xn[crossing] - xs[crossing]) / (yn[crossing] - ys[crossing])
        if np.count_nonzero(x_at > px) % 2:
            return True
    near = ((np.minimum(xs, xn) - _EPS <= px) & (px <= np.maximum(xs, xn) + _EPS)
            & (np.minimum(ys, yn) - _EPS <= py) & (py <= np.maximum(ys, yn) + _EPS))
    if not near.any():
        return False
    dx, dy = xn[near] - xs[near], yn[near] - ys[near]
    cross = dx * (py - ys[near]) - dy * (px - xs[near])
    return bool(np.any(np.abs(cross) <= _EPS * np.maximum(np.hypot(dx, dy), _EPS)))


# --------------------------------------------------------------------------------------------------- veri modeli
@dataclass(frozen=True)
class BoardTrack:
    """Bir iz parçası (``segment``) ya da yay (``arc``)."""
    net: str
    layer: str
    width_mm: float
    start: Point
    end: Point
    mid: Optional[Point] = None

    @cached_property
    def chords(self) -> Tuple[Chord, ...]:
        """Orta çizgi: düz izde tek parça, arkta sehimi `_ARC_SAG_MM`'yi aşmayan kirişler (s gerçek yay uzunluğu)."""
        arc = _arc_geometry(self.start, self.mid, self.end) if self.mid is not None else None
        if arc is None:
            (x1, y1), (x2, y2) = self.start, self.end
            return ((x1, y1, x2, y2, 0.0, math.hypot(x2 - x1, y2 - y1)),)
        cx, cy, radius, a0, sweep = arc
        step = 2.0 * math.acos(max(1.0 - _ARC_SAG_MM / radius, 0.0)) if radius > _ARC_SAG_MM else math.pi / 2
        count = min(max(int(math.ceil(abs(sweep) / step)), 4), 720)
        chords: List[Chord] = []
        px, py = self.start
        for i in range(1, count + 1):
            angle = a0 + sweep * i / count
            nx, ny = (self.end if i == count else (cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
            chords.append((px, py, nx, ny, radius * abs(sweep) * (i - 1) / count, radius * abs(sweep) * i / count))
            px, py = nx, ny
        return tuple(chords)

    @property
    def length_mm(self) -> float:
        return self.chords[-1][5]

    def point_at(self, s: float) -> Point:
        """Orta çizgide, başlangıçtan ``s`` mm ilerideki nokta."""
        for x1, y1, x2, y2, s1, s2 in self.chords:
            if s <= s2 or s2 == self.chords[-1][5]:
                u = 0.0 if s2 <= s1 else min(max((s - s1) / (s2 - s1), 0.0), 1.0)
                return x1 + u * (x2 - x1), y1 + u * (y2 - y1)
        return self.end


@dataclass(frozen=True)
class BoardVia:
    net: str
    at: Point
    diameter_mm: float
    drill_mm: float
    layers: Tuple[str, ...]   # delinen bakır katmanlar (kör via'da yalnız aradakiler)


@dataclass(frozen=True)
class BoardPad:
    ref: str
    number: str
    net: str
    at: Point                 # kart koordinatı (footprint konumu ve dönüşü uygulanmış)
    size_mm: Tuple[float, float]
    angle_deg: float          # kart üzerindeki mutlak dönüş (dosyadaki pad açısı)
    shape: str                # circle | oval | rect | roundrect | trapezoid | chamfered_rect | custom
    kind: str                 # smd | thru_hole | np_thru_hole | connect
    layers: Tuple[str, ...]
    rratio: float = 0.0
    #: Footprint içi bağlantı (``duplicate_pad_numbers_are_jumpers`` ya da ``jumper_pad_groups``): aynı anahtarı taşıyan
    #: padler KiCad'e göre içten bağlıdır (ör. modül soketinin iki 3V3 pini). Boş: jumper değil.
    jumper: str = ""

    @property
    def label(self) -> str:
        return f"{self.ref}.{self.number}"

    @cached_property
    def _frame(self) -> Tuple[float, float, float, float, float]:
        """``(hx, hy, rr, cos, sin)``: pad = iç dikdörtgen (yarı boyut hx, hy) + rr yarıçaplı yuvarlatma."""
        sx, sy = self.size_mm
        if self.shape == "circle":
            hx, hy, rr = 0.0, 0.0, sx / 2.0
        elif self.shape == "oval":
            rr = min(sx, sy) / 2.0
            hx, hy = sx / 2.0 - rr, sy / 2.0 - rr
        elif self.shape == "roundrect":
            rr = min(max(self.rratio, 0.0), 0.5) * min(sx, sy)
            hx, hy = sx / 2.0 - rr, sy / 2.0 - rr
        else:   # rect; trapezoid, chamfered_rect ve custom çapa pedinin dikdörtgeniyle temsil edilir
            hx, hy, rr = sx / 2.0, sy / 2.0, 0.0
        angle = math.radians(self.angle_deg)
        return hx, hy, rr, math.cos(angle), math.sin(angle)

    @property
    def radius_mm(self) -> float:
        """Padin merkezinden en uzak bakır noktasına uzaklık (çevreleyen daire)."""
        hx, hy, rr, _, _ = self._frame
        return math.hypot(hx, hy) + rr

    def to_local(self, x: float, y: float) -> Point:
        """Kart noktasını padin kendi eksenlerine çevirir (KiCad: y aşağı, açı saat yönünün tersine)."""
        _, _, _, cos, sin = self._frame
        dx, dy = x - self.at[0], y - self.at[1]
        return dx * cos - dy * sin, dx * sin + dy * cos

    def distance_to_point(self, x: float, y: float) -> float:
        """Noktanın pad bakırına uzaklığı (içindeyse 0)."""
        hx, hy, rr, _, _ = self._frame
        lx, ly = self.to_local(x, y)
        return max(math.hypot(max(abs(lx) - hx, 0.0), max(abs(ly) - hy, 0.0)) - rr, 0.0)


@dataclass(frozen=True)
class BoardZone:
    """Bakır döküm. ``fills``: katman -> dolu çokgenler (her biri düz ``(x, y, x, y, ...)`` dizisi)."""
    net: str
    layers: Tuple[str, ...]
    fills: Dict[str, Tuple[Tuple[float, ...], ...]]

    def is_filled(self, layer: str) -> bool:
        return bool(self.fills.get(layer))


@dataclass(frozen=True)
class StackupLayer:
    name: str
    kind: str                 # copper | core | prepreg | ...
    thickness_mm: float
    epsilon_r: float = 0.0    # 0: dosyada yok
    material: str = ""


@dataclass(frozen=True)
class PathPiece:
    """Yolun aynı katmanda ve aynı genişlikte kesintisiz bir parçası."""
    layer: str
    width_mm: float
    length_mm: float
    #: Komşu bakır katmandaki dolu dökümün bu parçayı örtme oranı (0-1). None: ölçülemedi (iç katman, katman yapısı
    #: yok ya da dökümler doldurulmadan kaydedilmiş).
    plane_coverage: Optional[float] = None
    reference_layer: str = ""


@dataclass(frozen=True)
class PathVia:
    """Yolun katman değiştirdiği yer: bir via ya da delikli pad."""
    kind: str                 # via | pad
    label: str
    at: Point
    from_layer: str
    to_layer: str


PathElement = Union[PathPiece, PathVia]


# --------------------------------------------------------------------------------------------------- net grafiği
_Node = Tuple[Any, ...]   # ("t", iz, sıra) | ("v", via) | ("p", pad)
#: Jumper kenarının iz numarası: footprint'in içinden geçen bağlantı (kartta bakır değil, iz yolunda kullanılmaz)
_JUMPER = -3


def _overlap_pairs(boxes: Sequence[Tuple[float, float, float, float]]) -> Iterator[Tuple[int, int]]:
    """Kutuları kesişen (i < j) çiftler; x'e göre süpürme (n^2 karşılaştırma yerine)."""
    order = sorted(range(len(boxes)), key=lambda i: boxes[i][0])
    for a, i in enumerate(order):
        _, min_y, max_x, max_y = boxes[i]
        for j in order[a + 1:]:
            other = boxes[j]
            if other[0] > max_x:
                break
            if other[1] <= max_y and other[3] >= min_y:
                yield (i, j) if i < j else (j, i)


class _NetGraph:
    """Bir netin bakırı: izler boyunca uzunluklu kenarlar, bakırın değdiği yerlerde sıfır uzunluklu bağlar."""

    def __init__(self, tracks: Sequence[BoardTrack], vias: Sequence[BoardVia], pads: Sequence[BoardPad]):
        self.tracks, self.vias, self.pads = tracks, vias, pads
        self._splits: List[Set[float]] = [{0.0, round(t.length_mm, 9)} for t in tracks]
        self._joints: List[Tuple[_Node, _Node]] = []
        self._connect()
        self.adjacency: Dict[_Node, List[Tuple[_Node, float, int]]] = {}
        self._build()

    # -- bağ bulma
    def _split(self, track: int, s: float) -> _Node:
        s = round(min(max(s, 0.0), self.tracks[track].length_mm), 9)
        self._splits[track].add(s)
        return ("s", track, s)

    def _connect(self) -> None:
        """Kutuları kesişen her bakır çiftine bakar (x süpürmesi): iz-iz, iz-via, iz-pad, via-via, via-pad, pad-pad."""
        items: List[Tuple[str, int]] = []
        boxes: List[Tuple[float, float, float, float]] = []
        for i, track in enumerate(self.tracks):
            half = track.width_mm / 2.0 + _ARC_SAG_MM
            xs = [c[0] for c in track.chords] + [track.chords[-1][2]]
            ys = [c[1] for c in track.chords] + [track.chords[-1][3]]
            items.append(("t", i))
            boxes.append((min(xs) - half, min(ys) - half, max(xs) + half, max(ys) + half))
        for v, via in enumerate(self.vias):
            radius = via.diameter_mm / 2.0
            items.append(("v", v))
            boxes.append((via.at[0] - radius, via.at[1] - radius, via.at[0] + radius, via.at[1] + radius))
        for p, pad in enumerate(self.pads):
            radius = pad.radius_mm
            items.append(("p", p))
            boxes.append((pad.at[0] - radius, pad.at[1] - radius, pad.at[0] + radius, pad.at[1] + radius))
        for a, b in _overlap_pairs(boxes):
            (kind_a, i), (kind_b, j) = items[a], items[b]   # a < b: sıra iz < via < pad
            if kind_a == "t" and kind_b == "t":
                if self.tracks[i].layer == self.tracks[j].layer:
                    self._join_tracks(i, j)
            elif kind_a == "t":
                track = self.tracks[i]
                if kind_b == "v" and track.layer in self.vias[j].layers:
                    via = self.vias[j]
                    for s in _point_contacts(track, via.at, (via.diameter_mm + track.width_mm) / 2.0 - _EPS):
                        self._joints.append((("v", j), self._split(i, s)))
                elif kind_b == "p" and track.layer in self.pads[j].layers:
                    for s in _pad_track_contacts(self.pads[j], track):
                        self._joints.append((("p", j), self._split(i, s)))
            elif kind_a == "v" and kind_b == "v":
                via, other = self.vias[i], self.vias[j]
                if (set(via.layers) & set(other.layers) and math.hypot(via.at[0] - other.at[0], via.at[1] - other.at[1])
                        <= (via.diameter_mm + other.diameter_mm) / 2.0 - _EPS):
                    self._joints.append((("v", i), ("v", j)))
            elif kind_a == "v":
                via, pad = self.vias[i], self.pads[j]
                if set(via.layers) & set(pad.layers) and pad.distance_to_point(*via.at) <= via.diameter_mm / 2.0 - _EPS:
                    self._joints.append((("v", i), ("p", j)))
            elif set(self.pads[i].layers) & set(self.pads[j].layers) and _pads_touch(self.pads[i], self.pads[j]):
                self._joints.append((("p", i), ("p", j)))

    def _join_tracks(self, i: int, j: int) -> None:
        """
        Aynı katmandaki iki izin bağları. Orta çizgiler kesişiyorsa kesişim noktası (zincir, çapraz, bölünmemiş T);
        bir izin ucu ötekinin bakırına değiyorsa o uç ile öteki izin orta çizgisindeki en yakın nokta (geniş izin içinde
        kaçık başlayan dal). Arkın kiriş uçları iz ucu değildir: bağ kurulmaz (yoksa her arkta kısayol olur).
        Bakırlar değdiği hâlde ikisi de yoksa (ark yan yana değiyor) en yakın nokta çifti.
        """
        ta, tb = self.tracks[i], self.tracks[j]
        reach = (ta.width_mm + tb.width_mm) / 2.0 - _EPS
        closest: Optional[Tuple[float, float, float]] = None
        joints: List[Tuple[float, float]] = []
        for ax1, ay1, ax2, ay2, as1, as2 in ta.chords:
            for bx1, by1, bx2, by2, bs1, bs2 in tb.chords:
                if (min(ax1, ax2) - reach > max(bx1, bx2) or max(ax1, ax2) + reach < min(bx1, bx2)
                        or min(ay1, ay2) - reach > max(by1, by2) or max(ay1, ay2) + reach < min(by1, by2)):
                    continue
                d, s, t = _closest_seg_seg(ax1, ay1, ax2, ay2, bx1, by1, bx2, by2)
                if d > reach:
                    continue
                pair = (d, as1 + s * (as2 - as1), bs1 + t * (bs2 - bs1))
                if d <= _EPS:
                    joints.append(pair[1:])
                elif closest is None or d < closest[0]:
                    closest = pair
        if not joints and closest is None:
            return
        for s_own, point in ((0.0, ta.start), (ta.length_mm, ta.end)):
            d, s_other = _closest_on_track(tb, point)
            if d <= reach:
                joints.append((s_own, s_other))
        for s_own, point in ((0.0, tb.start), (tb.length_mm, tb.end)):
            d, s_other = _closest_on_track(ta, point)
            if d <= reach:
                joints.append((s_other, s_own))
        if not joints and closest is not None:
            joints.append(closest[1:])
        for s, t in joints:
            self._joints.append((self._split(i, s), self._split(j, t)))

    # -- grafik
    def _build(self) -> None:
        adjacency = self.adjacency
        order: List[Dict[float, int]] = []
        for index, splits in enumerate(self._splits):
            ordered = sorted(splits)
            order.append({s: k for k, s in enumerate(ordered)})
            for k in range(len(ordered) - 1):
                a, b, length = ("t", index, k), ("t", index, k + 1), ordered[k + 1] - ordered[k]
                adjacency.setdefault(a, []).append((b, length, index))
                adjacency.setdefault(b, []).append((a, length, index))
            if len(ordered) == 1:
                adjacency.setdefault(("t", index, 0), [])
        self.split_positions = [sorted(s) for s in self._splits]

        def resolve(node: _Node) -> _Node:
            return ("t", node[1], order[node[1]][node[2]]) if node[0] == "s" else node

        for a, b in self._joints:
            a, b = resolve(a), resolve(b)
            adjacency.setdefault(a, []).append((b, 0.0, -1))
            adjacency.setdefault(b, []).append((a, 0.0, -1))
        jumpers: Dict[str, List[int]] = {}
        for p, pad in enumerate(self.pads):
            adjacency.setdefault(("p", p), [])
            if pad.jumper:
                jumpers.setdefault(pad.jumper, []).append(p)
        for members in jumpers.values():
            for a_pad, b_pad in zip(members, members[1:]):
                adjacency[("p", a_pad)].append((("p", b_pad), 0.0, _JUMPER))
                adjacency[("p", b_pad)].append((("p", a_pad), 0.0, _JUMPER))
        for v in range(len(self.vias)):
            adjacency.setdefault(("v", v), [])

    def shortest(self, sources: Iterable[_Node], targets: Iterable[_Node], without_track: int = -2,
                 jumpers: bool = False) -> Optional[Tuple[float, List[_Node]]]:
        """
        Dijkstra: kaynaklardan herhangi birinden hedeflerden herhangi birine; ``without_track`` izi kullanılmaz.
        Footprint içi jumper bağlantıları yalnız ``jumpers=True`` ile geçilir (kartta bakır değildir).
        """
        goal = set(targets)
        best: Dict[_Node, float] = {}
        previous: Dict[_Node, _Node] = {}
        heap: List[Tuple[float, int, _Node]] = []
        counter = 0
        for source in sources:
            best[source] = 0.0
            heap.append((0.0, counter, source))
            counter += 1
        heapq.heapify(heap)
        while heap:
            dist, _, node = heapq.heappop(heap)
            if dist > best.get(node, math.inf):
                continue
            if node in goal:
                path = [node]
                while path[-1] in previous:
                    path.append(previous[path[-1]])
                return dist, path[::-1]
            for neighbor, length, track in self.adjacency[node]:
                if track == without_track or (track == _JUMPER and not jumpers):
                    continue
                candidate = dist + length
                if candidate < best.get(neighbor, math.inf) - 1e-12:
                    best[neighbor] = candidate
                    previous[neighbor] = node
                    counter += 1
                    heapq.heappush(heap, (candidate, counter, neighbor))
        return None

    def pad_groups(self) -> List[List[int]]:
        """İz, via ve pad bakırıyla ya da footprint içi jumper'la (KiCad gibi; döküm hariç) bağlı pad kümeleri."""
        seen: Set[_Node] = set()
        groups: List[List[int]] = []
        for p in range(len(self.pads)):
            start: _Node = ("p", p)
            if start in seen:
                continue
            seen.add(start)
            stack, group = [start], []
            while stack:
                node = stack.pop()
                if node[0] == "p":
                    group.append(node[1])
                for neighbor, _, _ in self.adjacency[node]:
                    if neighbor not in seen:
                        seen.add(neighbor)
                        stack.append(neighbor)
            groups.append(sorted(group))
        return groups


def _closest_on_track(track: BoardTrack, point: Point) -> Tuple[float, float]:
    """Noktanın izin orta çizgisine uzaklığı ve en yakın noktanın iz boyunca konumu: ``(d, s)``."""
    px, py = point
    best = (math.inf, 0.0)
    for x1, y1, x2, y2, s1, s2 in track.chords:
        d, u, _ = _closest_seg_seg(x1, y1, x2, y2, px, py, px, py)
        if d < best[0]:
            best = (d, s1 + u * (s2 - s1))
    return best


def _runs_best(candidates: Iterable[Optional[Tuple[float, float]]]) -> List[float]:
    """
    Kiriş sırasıyla ``(uzaklık, s)`` adayları (değmeyen kiriş None): art arda değen kirişlerin her dizisinden en
    yakın olanın ``s``'i. Arkın birkaç kirişi aynı pada / via'ya değer; dizi başına tek bağ kurulur, yoksa ark ucundaki
    pad ya da via, uca değil komşu kirişin başına bağlanır (kısayol).
    """
    contacts: List[float] = []
    best: Optional[Tuple[float, float]] = None
    for candidate in candidates:
        if candidate is None:
            if best is not None:
                contacts.append(best[1])
            best = None
        elif best is None or candidate[0] < best[0]:
            best = candidate
    if best is not None:
        contacts.append(best[1])
    return contacts


def _point_contacts(track: BoardTrack, point: Point, reach: float) -> List[float]:
    """Merkezi ``point`` olan yuvarlak bakırın (via) ize değdiği yerler: değen kiriş dizisi başına en yakın nokta."""
    px, py = point

    def candidate(chord: Chord) -> Optional[Tuple[float, float]]:
        x1, y1, x2, y2, s1, s2 = chord
        d, u, _ = _closest_seg_seg(x1, y1, x2, y2, px, py, px, py)
        return (d, s1 + u * (s2 - s1)) if d <= reach else None

    return _runs_best(candidate(chord) for chord in track.chords)


def _pad_track_contacts(pad: BoardPad, track: BoardTrack) -> List[float]:
    """İzin pad bakırına değdiği yerler (iz boyunca s). Bağ, orta çizginin pad merkezine en yakın noktasına kurulur."""
    hx, hy, rr, _, _ = pad._frame
    reach = rr + track.width_mm / 2.0 - _EPS

    def candidate(chord: Chord) -> Optional[Tuple[float, float]]:
        x1, y1, x2, y2, s1, s2 = chord
        ax, ay = pad.to_local(x1, y1)
        bx, by = pad.to_local(x2, y2)
        if (min(ax, bx) > hx + reach or max(ax, bx) < -hx - reach or min(ay, by) > hy + reach
                or max(ay, by) < -hy - reach):
            return None
        if _segment_rect_distance(ax, ay, bx, by, hx, hy) > reach:
            return None
        # Merkeze en yakın nokta, izin genişletilmiş pad kutusu içinde kalan kısmına kırpılır (Liang-Barsky)
        dx, dy = bx - ax, by - ay
        lo, hi = 0.0, 1.0
        for delta, origin, limit in ((dx, ax, hx + reach), (dy, ay, hy + reach)):
            if abs(delta) < 1e-15:
                continue
            first, second = (-limit - origin) / delta, (limit - origin) / delta
            lo, hi = max(lo, min(first, second)), min(hi, max(first, second))
        length_sq = dx * dx + dy * dy
        u = 0.0 if length_sq <= 0.0 else -(ax * dx + ay * dy) / length_sq
        u = min(max(u, lo), hi) if lo <= hi else min(max(u, 0.0), 1.0)
        return math.hypot(ax + u * dx, ay + u * dy), s1 + u * (s2 - s1)

    return _runs_best(candidate(chord) for chord in track.chords)


def _segment_rect_distance(ax: float, ay: float, bx: float, by: float, hx: float, hy: float) -> float:
    """Parçanın (yerel eksende) merkezli ``2hx x 2hy`` dikdörtgene uzaklığı; içinden geçiyorsa 0."""
    if (abs(ax) <= hx and abs(ay) <= hy) or (abs(bx) <= hx and abs(by) <= hy):
        return 0.0
    corners = ((-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy))
    return min(_closest_seg_seg(ax, ay, bx, by, *corners[k], *corners[(k + 1) % 4])[0] for k in range(4))


def _pads_touch(a: BoardPad, b: BoardPad) -> bool:
    """İki padin bakırı değiyor mu: iç dikdörtgenler arası uzaklık <= iki yuvarlatma yarıçapının toplamı."""
    hx, hy, rr, _, _ = a._frame
    bhx, bhy, brr, cos, sin = b._frame
    corners = [(b.at[0] + cx * cos + cy * sin, b.at[1] - cx * sin + cy * cos)
               for cx, cy in ((-bhx, -bhy), (bhx, -bhy), (bhx, bhy), (-bhx, bhy))]
    local = [a.to_local(x, y) for x, y in corners]
    reach = rr + brr - _EPS
    distance = min(_segment_rect_distance(*local[k], *local[(k + 1) % 4], hx, hy) for k in range(4))
    if distance > 0.0:
        # b'nin dikdörtgeni a'nınkini tamamen içeriyorsa kenarlar uzakta kalır: a'nın merkezi b'nin içinde mi
        bx, by = b.to_local(*a.at)
        if abs(bx) <= bhx and abs(by) <= bhy:
            distance = 0.0
    return distance <= reach


# --------------------------------------------------------------------------------------------------- yol
@dataclass
class BoardPath:
    """İki pad (grubu) arasındaki en kısa iz yolu. `KicadBoard.trace_path` üretir."""
    board: "KicadBoard"
    net: str
    start: str
    end: str
    elements: List[PathElement]
    warnings: List[str] = field(default_factory=list)

    @property
    def pieces(self) -> List[PathPiece]:
        return [e for e in self.elements if isinstance(e, PathPiece)]

    @property
    def vias(self) -> List[PathVia]:
        return [e for e in self.elements if isinstance(e, PathVia)]

    @property
    def length_mm(self) -> float:
        return sum(piece.length_mm for piece in self.pieces)

    @property
    def via_count(self) -> int:
        return len(self.vias)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "net": self.net,
            "from_pad": self.start,
            "to_pad": self.end,
            "length_mm": round(self.length_mm, 6),
            "via_count": self.via_count,
            "pieces": [{"layer": p.layer, "width_mm": p.width_mm, "length_mm": round(p.length_mm, 6),
                        "plane_coverage": None if p.plane_coverage is None else round(p.plane_coverage, 4),
                        "reference_layer": p.reference_layer} for p in self.pieces],
            "layer_changes": [{"kind": v.kind, "label": v.label, "from_layer": v.from_layer, "to_layer": v.to_layer}
                              for v in self.vias],
            "warnings": list(self.warnings),
        }

    def parasitics(self, temp_c: float = 25.0, height_mm: Optional[float] = None, er: Optional[float] = None,
                   copper_oz: Optional[float] = None, via_inductance_h: Optional[float] = None,
                   via_resistance_ohm: Optional[float] = None) -> "PathParasitics":
        """
        Yolun her parçasını `calculate_trace_parasitics` ile R/L/C'ye çevirir. Dielektrik kalınlığı H, bağıl
        geçirgenlik ve bakır kalınlığı kartın katman yapısından okunur; ``height_mm`` / ``er`` / ``copper_oz`` verilirse
        (girdi) onlar kullanılır. Via'lar modellenmez (ideal kısa devre); via başına ``via_inductance_h`` ve
        ``via_resistance_ohm`` girdi olarak verilirse seri eklenir.

        Katman yapısı dosyada yoksa ve değerler verilmediyse, ya da parça iç katmandaysa (stripline) BoardPathError.
        """
        if (via_inductance_h is not None and via_inductance_h < 0) or (via_resistance_ohm is not None and via_resistance_ohm < 0):
            raise ValueError("via_inductance_h ve via_resistance_ohm negatif olamaz.")
        if not self.pieces:
            raise BoardPathError(f"{self.start} ile {self.end} arasındaki yolda iz yok (bakır doğrudan değiyor): "
                                 "modellenecek iz parçası yok.")
        traces: List[TraceParasitics] = []
        notes: List[str] = []
        for number, piece in enumerate(self.pieces, start=1):
            h, e_r, oz = height_mm, er, copper_oz
            if h is None or e_r is None:
                file_h, file_er, _ = self.board.reference_dielectric(piece.layer)
                h = file_h if h is None else h
                e_r = file_er if e_r is None else e_r
            if oz is None:
                oz = self.board.copper_thickness_mm(piece.layer) / COPPER_MM_PER_OZ
            try:
                trace = calculate_trace_parasitics(length_mm=piece.length_mm, width_mm=piece.width_mm, height_mm=h,
                                                   copper_oz=oz, er=e_r, temp_c=temp_c)
            except ValueError as exc:
                raise BoardPathError(f"{number}. parça ({piece.layer}, {piece.width_mm:g} mm): {exc}") from exc
            traces.append(trace)
            if trace.accuracy_note and trace.accuracy_note not in notes:
                notes.append(trace.accuracy_note)
            if piece.plane_coverage is None:
                notes.append(f"{number}. parça ({piece.layer}): altındaki dönüş düzlemi ölçülemedi; microstrip modeli "
                             "kesintisiz düzlem varsayar (varsayım).")
            elif piece.plane_coverage < 1.0:
                notes.append(f"{number}. parça ({piece.layer}): {piece.reference_layer} katmanındaki dökümün örttüğü "
                             f"kısım %{piece.plane_coverage * 100:.0f}; örtülmeyen kısımda microstrip modeli geçersiz "
                             "(dönüş yolu uzar, endüktans modeldekinden büyük olur).")
        if self.via_count:
            modeled = via_inductance_h is not None or via_resistance_ohm is not None
            notes.append(f"{self.via_count} katman geçişi (via / delikli pad) "
                         + ("girdi olarak verilen değerlerle eklendi." if modeled else "modellenmedi (ideal kısa devre)."))
        notes.extend(w for w in self.warnings if w not in notes)
        return PathParasitics(path=self, traces=traces, via_inductance_h=via_inductance_h,
                              via_resistance_ohm=via_resistance_ohm, notes=notes)


@dataclass
class PathParasitics:
    """Bir yolun parça parça RLC modeli (`BoardPath.parasitics`). ``traces`` yol sırasıyla ``path.pieces``'e karşılıktır."""
    path: BoardPath
    traces: List[TraceParasitics]
    via_inductance_h: Optional[float] = None
    via_resistance_ohm: Optional[float] = None
    notes: List[str] = field(default_factory=list)

    @property
    def r_dc(self) -> float:
        return sum(t.r_dc for t in self.traces) + self.path.via_count * (self.via_resistance_ohm or 0.0)

    @property
    def l_trace(self) -> float:
        return sum(t.l_trace for t in self.traces) + self.path.via_count * (self.via_inductance_h or 0.0)

    @property
    def c_trace(self) -> float:
        return sum(t.c_trace for t in self.traces)

    @property
    def tpd(self) -> float:
        return sum(t.tpd for t in self.traces)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "r_dc_ohm": self.r_dc,
            "l_trace_nh": self.l_trace * 1e9,
            "c_trace_pf": self.c_trace * 1e12,
            "tpd_ps": self.tpd * 1e12,
            "length_mm": round(self.path.length_mm, 6),
            "via_count": self.path.via_count,
            "pieces": [dict(t.to_dict(), layer=p.layer) for t, p in zip(self.traces, self.path.pieces)],
            "notes": list(self.notes),
        }

    def to_spice_subckt(self, subckt_name: str = "PCB_PATH") -> str:
        """
        Yolu tek alt devre olarak yazar: ``.subckt <ad> in out ref``. Her parça bir T hücresidir (R/2 - L/2 - C - L/2 -
        R/2); C ``ref`` portuna gider. İç düğümler alt devreye yereldir. Via değeri girdi olarak verildiyse parçaların
        arasına seri eklenir, verilmediyse via ideal kısa devredir. Bobinlere ``Rser=0`` yazılır (LTspice varsayılanı
        1 mΩ). Referans portunun adı ``gnd`` değildir: LTspice'ta GND, 0'ın eş adıdır.
        """
        lines = [f".subckt {subckt_name} in out ref"]
        modeled_vias = self.via_inductance_h is not None or self.via_resistance_ohm is not None
        series = [e for e in self.path.elements if isinstance(e, PathPiece) or modeled_vias]
        node, piece_no, via_no = "in", 0, 0
        for position, element in enumerate(series):
            target = "out" if position == len(series) - 1 else f"j{position + 1}"
            if isinstance(element, PathPiece):
                trace = self.traces[piece_no]
                piece_no += 1
                k = piece_no
                lines += [
                    f"* {k}: {element.layer} {element.width_mm:g} mm x {element.length_mm:.6g} mm",
                    f"R{k}a {node} n{k}a {_spice(trace.r_dc / 2.0, unit='R')}",
                    f"L{k}a n{k}a n{k}m {_spice(trace.l_trace / 2.0, unit='H')} Rser=0",
                    f"C{k} n{k}m ref {_spice(trace.c_trace, unit='F')}",
                    f"L{k}b n{k}m n{k}b {_spice(trace.l_trace / 2.0, unit='H')} Rser=0",
                    f"R{k}b n{k}b {target} {_spice(trace.r_dc / 2.0, unit='R')}",
                ]
            else:
                via_no += 1
                lines.append(f"* via {via_no}: {element.label} ({element.from_layer} -> {element.to_layer}), girdi")
                r, l_h = self.via_resistance_ohm or 0.0, self.via_inductance_h or 0.0
                if r > 0 and l_h > 0:
                    lines += [f"Rv{via_no} {node} v{via_no} {_spice(r, unit='R')}",
                              f"Lv{via_no} v{via_no} {target} {_spice(l_h, unit='H')} Rser=0"]
                elif l_h > 0:
                    lines.append(f"Lv{via_no} {node} {target} {_spice(l_h, unit='H')} Rser=0")
                else:   # LTspice'a 0 Ohm yazılmaz: değersiz via 1 µΩ ile geçilir
                    lines.append(f"Rv{via_no} {node} {target} {_spice(r if r > 0 else 1e-6, unit='R')}")
            node = target
        lines.append(f".ends {subckt_name}")
        return "\n".join(lines) + "\n"


def inject_board_path_into_netlist(netlist_text: str, from_node: str, to_node: str, parasitics: PathParasitics,
                                   element_prefix: str = "TR1", reference_node: str = "0") -> str:
    """
    Karttan okunan yolu ``from_node`` ile ``to_node`` arasına bağlar (alt devre + çağrısı, ilk ``.end``'den önce).
    Var olan elemanlara dokunmaz: bir bağlantının araya girmesi için önce o bağlantıyı iki ayrı düğüme ayırın.
    Açıklama satırı sayıların hangi dosyadan geldiğini taşır (dosya adı, SHA-256'nın başı, padler, uzunluk).
    """
    name = f"PCB_PATH_{element_prefix}"
    path = parasitics.path
    comment = (f"* --- Karttan okunan iz yolu: {path.board.path.name} sha256 {path.board.sha256[:12]} "
               f"{path.start} -> {path.end} ({path.net}), {path.length_mm:.6g} mm, {path.via_count} via ---")
    call = f"X_{element_prefix} {from_node} {to_node} {reference_node} {name}"
    return insert_before_end(netlist_text, [comment, *parasitics.to_spice_subckt(name).strip().splitlines(), call])


# --------------------------------------------------------------------------------------------------- kart
@dataclass
class KicadBoard:
    """Okunmuş ``.kicad_pcb``. `read_kicad_board` üretir."""
    path: Path
    sha256: str
    format_version: int
    generator: str
    title: str
    revision: str
    date: str
    thickness_mm: Optional[float]
    copper_layers: Tuple[str, ...]          # üstten alta: F.Cu, In1.Cu, ..., B.Cu
    stackup: Tuple[StackupLayer, ...]       # dosyada yoksa boş
    tracks: Tuple[BoardTrack, ...]
    vias: Tuple[BoardVia, ...]
    pads: Tuple[BoardPad, ...]
    zones: Tuple[BoardZone, ...]
    warnings: List[str] = field(default_factory=list)
    #: Bakır katmanda nete bağlı çizimlerin (gr_rect, gr_poly, gr_line...) netleri. Okuyucu çizimleri bakır olarak
    #: modellemez; KiCad bağlantıyı onlardan da kurar (One-Air-Max demosunda dolu gr_rect USB kabuk padlerini bağlıyor).
    drawing_nets: Tuple[str, ...] = ()
    _graphs: Dict[str, _NetGraph] = field(default_factory=dict, repr=False)
    _fills: Dict[str, List[Any]] = field(default_factory=dict, repr=False)

    # -- özet (rapora girer)
    def summary(self) -> Dict[str, Any]:
        """Raporun "kart dosyası" özeti: sayıların hangi dosyadan geldiği (ad, SHA-256, revizyon, katman yapısı)."""
        return {
            "file": self.path.name,
            "sha256": self.sha256,
            "format_version": self.format_version,
            "generator": self.generator,
            "title": self.title,
            "revision": self.revision,
            "date": self.date,
            "board_thickness_mm": self.thickness_mm,
            "copper_layers": list(self.copper_layers),
            "stackup": [{"name": layer.name, "type": layer.kind, "thickness_mm": layer.thickness_mm,
                         "epsilon_r": layer.epsilon_r or None, "material": layer.material}
                        for layer in self.stackup if layer.kind == "copper" or layer.epsilon_r],
            "counts": {"segments": sum(1 for t in self.tracks if t.mid is None),
                       "arcs": sum(1 for t in self.tracks if t.mid is not None), "vias": len(self.vias),
                       "pads": len(self.pads), "zones": len(self.zones), "nets": len(self.nets())},
            "warnings": list(self.warnings),
        }

    def nets(self) -> Dict[str, List[str]]:
        """Net adı -> o netteki pad etiketleri (``REF.PAD``), sıralı."""
        result: Dict[str, List[str]] = {}
        for pad in self.pads:
            if pad.net:
                result.setdefault(pad.net, []).append(pad.label)
        for net in [track.net for track in self.tracks] + [via.net for via in self.vias]:
            if net:
                result.setdefault(net, [])
        return {net: sorted(labels) for net, labels in sorted(result.items())}

    def net_track_length_mm(self, net: str) -> float:
        """Netin toplam iz uzunluğu (KiCad'in Net Inspector'ındaki iz uzunluğu). Yol uzunluğu değildir."""
        return sum(t.length_mm for t in self.tracks if t.net == net)

    def find_pads(self, ref: str, number: str) -> List[BoardPad]:
        return [pad for pad in self.pads if pad.ref == ref and pad.number == number]

    # -- katman yapısı
    def _stack_position(self, layer: str) -> int:
        if layer not in self.copper_layers:
            raise BoardPathError(f"'{layer}' bu kartta bir bakır katman değil ({', '.join(self.copper_layers)}).")
        return self.copper_layers.index(layer)

    def reference_layer(self, layer: str) -> str:
        """Dış katmandaki izin microstrip referansı: komşu bakır katman. İç katman ya da tek katmanlı kart: hata."""
        position = self._stack_position(layer)
        if len(self.copper_layers) < 2:
            raise BoardPathError("Kart tek bakır katmanlı: microstrip modelinin referans düzlemi yok.")
        if position == 0:
            return self.copper_layers[1]
        if position == len(self.copper_layers) - 1:
            return self.copper_layers[-2]
        raise BoardPathError(f"'{layer}' iç katman: buradaki iz stripline'dır, IPC-2141 microstrip denklemi uygulanmaz.")

    def reference_dielectric(self, layer: str) -> Tuple[float, float, str]:
        """
        ``(H mm, bağıl geçirgenlik, referans katman)``: dış katmandaki iz ile komşu bakır katman arasındaki dielektrik,
        dosyanın katman yapısından. Aradaki dielektrik katmanların kalınlıkları toplanır; geçirgenlikleri farklıysa
        tek bir değer seçilmez, hata verilir (``er`` girdi olarak verilmeli).
        """
        reference = self.reference_layer(layer)
        names = [entry.name for entry in self.stackup]
        if layer not in names or reference not in names:
            raise BoardPathError(
                "Kart dosyasında katman yapısı (stackup) yok. KiCad'de Board Setup > Physical Stackup'ı açıp kartı "
                "kaydedin ya da height_mm, er ve copper_oz değerlerini girdi olarak verin.")
        first, second = sorted((names.index(layer), names.index(reference)))
        between = [entry for entry in self.stackup[first + 1:second] if entry.kind != "copper"]
        height = sum(entry.thickness_mm for entry in between)
        values = sorted({entry.epsilon_r for entry in between})
        if not between or height <= 0 or not values or values[0] <= 1.0:
            raise BoardPathError(f"Katman yapısında {layer} ile {reference} arasındaki dielektriğin kalınlığı ya da "
                                 "bağıl geçirgenliği yok; height_mm ve er değerlerini girdi olarak verin.")
        if values[-1] - values[0] > 1e-9:
            raise BoardPathError(f"{layer} ile {reference} arasında farklı geçirgenlikte dielektrikler var "
                                 f"({', '.join(f'{v:g}' for v in values)}); er değerini girdi olarak verin.")
        return height, values[0], reference

    def copper_thickness_mm(self, layer: str) -> float:
        for entry in self.stackup:
            if entry.name == layer and entry.kind == "copper" and entry.thickness_mm > 0:
                return entry.thickness_mm
        raise BoardPathError(f"Katman yapısında {layer} bakır kalınlığı yok; copper_oz değerini girdi olarak verin.")

    # -- döküm
    def zone_layers(self, net: str) -> List[str]:
        """Netin bakır dökümü olan katmanlar."""
        return [layer for layer in self.copper_layers if any(z.net == net and layer in z.layers for z in self.zones)]

    def _layer_fills(self, layer: str) -> List[Any]:
        if layer not in self._fills:
            import numpy as np

            polygons = []
            for zone in self.zones:
                for flat in zone.fills.get(layer, ()):
                    xs, ys = np.asarray(flat[0::2], dtype=float), np.asarray(flat[1::2], dtype=float)
                    polygons.append((xs, ys, np.roll(xs, -1), np.roll(ys, -1),
                                     (float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max()))))
            self._fills[layer] = polygons
        return self._fills[layer]

    def copper_fill_at(self, layer: str, x: float, y: float) -> bool:
        """Nokta, ``layer`` katmanındaki dolu bir dökümün içinde mi (hangi net olursa olsun)?"""
        return any(_inside_fill(x, y, polygon) for polygon in self._layer_fills(layer))

    def _coverage(self, layer: str, ranges: Sequence[Tuple[BoardTrack, float, float]]) -> Tuple[Optional[float], str]:
        """Yol parçasının referans katmandaki dolu dökümle örtülme oranı; ölçülemiyorsa (None, neden)."""
        try:
            reference = self.reference_layer(layer)
        except BoardPathError:
            return None, ""
        zones = [zone for zone in self.zones if reference in zone.layers]
        if zones and not any(zone.is_filled(reference) for zone in zones):
            return None, reference
        inside = total = 0
        for track, s_from, s_to in ranges:
            count = max(1, int(math.ceil(abs(s_to - s_from) / COVERAGE_STEP_MM - 1e-9)))
            for i in range(count):
                x, y = track.point_at(s_from + (s_to - s_from) * (i + 0.5) / count)
                inside += self.copper_fill_at(reference, x, y)
                total += 1
        return (inside / total if total else None), reference

    # -- bağlantı
    def _graph(self, net: str) -> _NetGraph:
        if net not in self._graphs:
            self._graphs[net] = _NetGraph([t for t in self.tracks if t.net == net], [v for v in self.vias if v.net == net],
                                          [p for p in self.pads if p.net == net])
        return self._graphs[net]

    def connected_pad_groups(self, net: str) -> List[List[str]]:
        """
        Netin birbirine bağlı pad kümeleri; etiketler sıralı. KiCad'in bağlantı kümeleriyle aynı tanım: iz, via ve pad
        bakırı ile footprint içi jumper (``duplicate_pad_numbers_are_jumpers``, ``jumper_pad_groups``) sayılır, bakır
        döküm sayılmaz.
        """
        graph = self._graph(net)
        return sorted(sorted(graph.pads[p].label for p in group) for group in graph.pad_groups())

    def _resolve(self, spec: Union[str, Tuple[str, str]]) -> Tuple[str, List[BoardPad]]:
        if isinstance(spec, str):
            ref, dot, number = spec.partition(".")
            if not dot:
                raise BoardPathError(f"Pad 'REF.PAD' biçiminde verilmeli (ör. 'U3.2'): {spec!r}")
        else:
            ref, number = spec
        pads = self.find_pads(ref, str(number))
        if not pads:
            numbers = sorted({pad.number for pad in self.pads if pad.ref == ref})
            if not numbers:
                raise BoardPathError(f"Kartta '{ref}' adlı bir bileşen yok.")
            raise BoardPathError(f"'{ref}' bileşeninde '{number}' numaralı pad yok; padleri: {', '.join(numbers)}.")
        return f"{ref}.{number}", pads

    def trace_path(self, start: Union[str, Tuple[str, str]], end: Union[str, Tuple[str, str]]) -> BoardPath:
        """
        İki pad arasındaki en kısa iz yolu. Pad ``"U3.2"`` ya da ``("U3", "2")`` biçiminde verilir; bileşende aynı
        numarayı taşıyan birden çok pad varsa (ekran, soğutma pedi) herhangi biri uç sayılır.

        BoardPathError: pad yok, pad bir nete bağlı değil, padler farklı netlerde, ya da aralarında iz yolu yok
        (yalnız bakır dökümle bağlı ya da yönlendirilmemiş).
        """
        start_label, start_pads = self._resolve(start)
        end_label, end_pads = self._resolve(end)
        nets = {pad.net for pad in (*start_pads, *end_pads)}
        if "" in nets:
            raise BoardPathError(f"{start_label} ya da {end_label} bir nete bağlı değil.")
        if len(nets) != 1:
            raise BoardPathError(f"{start_label} ({start_pads[0].net}) ile {end_label} ({end_pads[0].net}) aynı nette değil.")
        if start_label == end_label:
            raise BoardPathError(f"Başlangıç ve bitiş aynı pad: {start_label}.")
        net = nets.pop()
        graph = self._graph(net)
        sources = [("p", i) for i, pad in enumerate(graph.pads) if pad in start_pads]
        targets = [("p", i) for i, pad in enumerate(graph.pads) if pad in end_pads]
        zone_layers = self.zone_layers(net)
        drawing = (f" '{net}' netinin bakır katmanda çizimi (gr_rect, gr_poly...) var; okuyucu çizimi bakır saymaz, "
                   "KiCad sayar." if net in self.drawing_nets else "")
        found = graph.shortest(sources, targets)
        if found is None:
            through = graph.shortest(sources, targets, jumpers=True)
            if through is not None:
                inside = sorted({graph.pads[n[1]].ref for a, n in zip(through[1], through[1][1:])
                                 if a[0] == "p" and n[0] == "p" and graph.pads[a[1]].jumper
                                 and graph.pads[a[1]].jumper == graph.pads[n[1]].jumper})
                raise BoardPathError(
                    f"{start_label} ile {end_label} arasındaki bağlantı {', '.join(inside)} bileşeninin içinden geçiyor "
                    "(KiCad'de aynı numaralı padler ya da jumper_pad_groups iç bağlantı sayılır). Bu iç bağlantı kartta "
                    "bir iz değildir; iz yolunu iç bağlantının iki ucuna ayrı ayrı sorun.")
            if zone_layers:
                raise BoardPathError(
                    f"{start_label} ile {end_label} arasında iz yolu yok; '{net}' netinin {', '.join(zone_layers)} "
                    "katmanında bakır dökümü var, bağlantı dökümden geçiyor olabilir. Döküm bir iz değildir: iz "
                    "modeli uygulanmaz.")
            if drawing:
                raise BoardPathError(f"{start_label} ile {end_label} arasında iz yolu yok;{drawing} Bağlantı o çizimden "
                                     "geçiyor olabilir: iz modeli uygulanmaz.")
            raise BoardPathError(f"{start_label} ile {end_label} arasında iz yolu yok ('{net}' yönlendirilmemiş olabilir).")
        length, nodes = found
        warnings: List[str] = []
        if zone_layers:
            warnings.append(f"'{net}' netinin {', '.join(zone_layers)} katmanında bakır dökümü var: akım dökümden de "
                            "akar, iz modeli yalnız iz yolunu temsil eder (R ve L olduğundan büyük çıkar).")
        if drawing:
            warnings.append(drawing.strip() + " Akım çizimden de akabilir; iz modeli yalnız iz yolunu temsil eder.")
        alternative = self._parallel_route(graph, sources, targets, nodes, length)
        if alternative is not None:
            warnings.append(f"Paralel iz yolu var: en kısa yol {length:.4g} mm, başka bir yol {alternative:.4g} mm. "
                            "Yalnız en kısa yol modellendi; paralel bakır R ve L'yi düşürür.")
        elements = self._elements(graph, nodes, warnings)
        return BoardPath(board=self, net=net, start=start_label, end=end_label, elements=elements, warnings=warnings)

    @staticmethod
    def _parallel_route(graph: _NetGraph, sources: Sequence[_Node], targets: Sequence[_Node], nodes: Sequence[_Node],
                        length: float) -> Optional[float]:
        """Yoldaki bir iz çıkarılınca uçlar hâlâ bağlıysa o alternatif yolun uzunluğu (en kısası); yoksa None."""
        used: List[int] = []
        for a, b in zip(nodes, nodes[1:]):
            if a[0] == "t" and b[0] == "t" and a[1] == b[1] and a[1] not in used:
                used.append(a[1])

        def same_copper(i: int, j: int) -> bool:
            a, b = graph.tracks[i], graph.tracks[j]
            return (a.layer == b.layer and a.mid == b.mid
                    and {a.start, a.end} == {b.start, b.end})

        best: Optional[float] = None
        for index in used[:200]:
            if any(same_copper(index, other) for other in range(len(graph.tracks)) if other != index):
                continue   # üst üste çizilmiş aynı iz: ayrı bir yol değil
            other_route = graph.shortest(sources, targets, without_track=index)
            if other_route is not None and (best is None or other_route[0] < best):
                best = other_route[0]
        return best if best is not None and best >= length - 1e-9 else None

    def _layer_changes(self, graph: _NetGraph, passed: Sequence[_Node], from_layer: str, to_layer: str) -> List[PathVia]:
        """
        İki iz arasında geçilen via / pad düğümlerinden katman geçişleri. İki katmanı da taşıyan bir iletken varsa tek
        geçiştir (sonuncusu); yoksa (üst üste kör/gömülü via) her iletken sırayla hedefe en yakın katmanına indirir.
        """
        items: List[Tuple[str, Union[BoardVia, BoardPad]]] = [
            ("via", graph.vias[n[1]]) if n[0] == "v" else ("pad", graph.pads[n[1]]) for n in passed]
        steps: List[Tuple[str, Union[BoardVia, BoardPad], str, str]] = []
        spanning = [(kind, item) for kind, item in items if from_layer in item.layers and to_layer in item.layers]
        if spanning:
            steps.append((*spanning[-1], from_layer, to_layer))
        else:
            current, goal = from_layer, self._stack_position(to_layer)
            for kind, item in items:
                if current not in item.layers:
                    continue
                target = min(item.layers, key=lambda name: abs(self._stack_position(name) - goal))
                if target != current:
                    steps.append((kind, item, current, target))
                    current = target
        return [PathVia(kind, f"via ({item.at[0]:g}, {item.at[1]:g})" if isinstance(item, BoardVia) else f"pad {item.label}",
                        item.at, a, b) for kind, item, a, b in steps]

    def _elements(self, graph: _NetGraph, nodes: Sequence[_Node], warnings: List[str]) -> List[PathElement]:
        """
        Düğüm dizisini parçalara ve katman geçişlerine çevirir. Katman, iz düğümünden iz düğümüne izlenir; arada
        geçilen via / pad düğümleri katmanı değiştirir. Uçtaki SMD padin katmanı da sayılır (pad içindeki via).
        """
        elements: List[PathElement] = []
        runs: List[List[Tuple[BoardTrack, float, float]]] = []
        first, last = graph.pads[nodes[0][1]], graph.pads[nodes[-1][1]]
        layer: Optional[str] = first.layers[0] if len(first.layers) == 1 else None
        passed: List[_Node] = []
        for index, node in enumerate(nodes):
            if node[0] != "t":
                if index:
                    passed.append(node)
                continue
            track = graph.tracks[node[1]]
            if layer is not None and track.layer != layer:
                elements.extend(self._layer_changes(graph, passed, layer, track.layer))
            layer, passed = track.layer, []
            following = nodes[index + 1] if index + 1 < len(nodes) else None
            if following is None or following[0] != "t" or following[1] != node[1]:
                continue
            s_from, s_to = graph.split_positions[node[1]][node[2]], graph.split_positions[node[1]][following[2]]
            if s_to == s_from:
                continue
            previous = elements[-1] if elements else None
            if (isinstance(previous, PathPiece) and previous.layer == track.layer
                    and abs(previous.width_mm - track.width_mm) < 1e-9):
                elements[-1] = PathPiece(track.layer, track.width_mm, previous.length_mm + abs(s_to - s_from))
                runs[-1].append((track, s_from, s_to))
            else:
                elements.append(PathPiece(track.layer, track.width_mm, abs(s_to - s_from)))
                runs.append([(track, s_from, s_to)])
        if layer is not None and len(last.layers) == 1 and last.layers[0] != layer:
            elements.extend(self._layer_changes(graph, passed[:-1], layer, last.layers[0]))
        unfilled: Set[str] = set()
        piece_index = 0
        for position, element in enumerate(elements):
            if isinstance(element, PathPiece):
                coverage, reference = self._coverage(element.layer, runs[piece_index])
                piece_index += 1
                if coverage is None and reference:
                    unfilled.add(reference)
                elements[position] = PathPiece(element.layer, element.width_mm, element.length_mm, coverage, reference)
        for reference in sorted(unfilled):
            warnings.append(f"{reference} katmanındaki dökümler doldurulmadan kaydedilmiş: izin altındaki dönüş düzlemi "
                            "ölçülemedi. KiCad'de dökümleri doldurup (B) kartı kaydedin.")
        return elements


# --------------------------------------------------------------------------------------------------- okuma
def _copper_order(names: Iterable[str]) -> Tuple[str, ...]:
    """Bakır katmanlar üstten alta: F.Cu, In1.Cu ... InN.Cu, B.Cu (kanonik adlardan; dosyadaki numara sürüme göre değişir)."""
    def key(name: str) -> Tuple[int, int]:
        if name == "F.Cu":
            return (0, 0)
        if name == "B.Cu":
            return (2, 0)
        inner = re.fullmatch(r"In(\d+)\.Cu", name)
        return (1, int(inner.group(1))) if inner else (1, 10 ** 6)

    return tuple(sorted(names, key=key))


def _expand_layers(patterns: Sequence[str], copper: Sequence[str]) -> Tuple[str, ...]:
    """Pad katmanları: ``*.Cu`` bütün bakır katmanlar, ``F&B.Cu`` iki dış katman; bakır olmayanlar atılır."""
    selected: Set[str] = set()
    for pattern in patterns:
        if pattern == "*.Cu":
            selected.update(copper)
        elif pattern == "F&B.Cu":
            selected.update(name for name in ("F.Cu", "B.Cu") if name in copper)
        elif pattern in copper:
            selected.add(pattern)
    return tuple(name for name in copper if name in selected)


def _net_name(node: Sequence[Any], numbered: Dict[str, str]) -> str:
    """``(net "ad")`` (KiCad 10), ``(net 5)`` ya da ``(net 5 "ad")`` (KiCad 6-9; numara dosya başındaki tablodan)."""
    net = _node(node, "net")
    if net is None or len(net) < 2:
        return ""
    if len(net) >= 3:
        return str(net[2])
    return str(net[1]) if isinstance(net[1], _Quoted) else numbered.get(str(net[1]), "")


def _stackup(setup: Optional[Sequence[Any]]) -> Tuple[StackupLayer, ...]:
    stack = _node(setup, "stackup") if setup is not None else None
    if stack is None:
        return ()
    layers: List[StackupLayer] = []
    for entry in _nodes(stack, "layer"):
        # Bir dielektrik katman "addsublayer" ile bölünmüş alt katmanlar taşıyabilir: her biri ayrı satır olur
        groups: List[List[Any]] = [[]]
        for item in entry[2:]:
            if item == "addsublayer":
                groups.append([])
            else:
                groups[-1].append(item)
        kind = _text(_node(groups[0], "type"))
        for group in groups:
            thickness = _node(group, "thickness")
            epsilon = _node(group, "epsilon_r")
            layers.append(StackupLayer(
                name=str(entry[1]), kind=kind,
                thickness_mm=float(thickness[1]) if thickness is not None and len(thickness) > 1 else 0.0,
                epsilon_r=float(epsilon[1]) if epsilon is not None and len(epsilon) > 1 else 0.0,
                material=_text(_node(group, "material"))))
    return tuple(layers)


def read_kicad_board(path: Union[str, Path], max_bytes: Optional[int] = DEFAULT_MAX_BYTES) -> KicadBoard:
    """
    Bir ``.kicad_pcb`` dosyasını okur. Dosyaya yazmaz, KiCad'i çalıştırmaz. KiCad 6'dan eski biçim reddedilir; KiCad 6-7
    biçimi (``fp_text reference``, ``F&B.Cu``) okunmaya çalışılır ama gerçek bir KiCad 6-7 dosyasıyla sınanmadı: o sürümler
    `TESTED_FORMAT_VERSIONS`'ta olmadığı için uyarı verilir.

    ValueError: dosya bir KiCad kartı değil, biçimi KiCad 6'dan eski ya da ``max_bytes``'tan büyük. Okuyucunun
    KiCad'in kendi hesabıyla karşılaştırılmadığı bir biçim sürümü `KicadBoard.warnings`'e yazılır.
    """
    path = Path(path)
    data = path.read_bytes()
    if max_bytes is not None and len(data) > max_bytes:
        raise ValueError(f"{path.name}: {len(data) / 1e6:.0f} MB, sınır {max_bytes / 1e6:.0f} MB (max_bytes ile yükseltin).")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path.name}: UTF-8 değil; bir .kicad_pcb dosyası olmayabilir.") from exc
    parsed = parse_sexpr(text)
    if len(parsed) != 1 or not isinstance(parsed[0], list) or not parsed[0] or parsed[0][0] != "kicad_pcb":
        raise ValueError(f"{path.name}: bir KiCad kart dosyası (.kicad_pcb) değil.")
    root = parsed[0]
    version_node = _node(root, "version")
    version = int(version_node[1]) if version_node is not None and len(version_node) > 1 and str(version_node[1]).isdigit() else 0
    if version < OLDEST_FORMAT_VERSION:
        raise ValueError(f"{path.name}: biçim sürümü {version} KiCad 6'dan eski; kartı güncel KiCad'de açıp kaydedin.")
    warnings: List[str] = []
    if version not in TESTED_FORMAT_VERSIONS:
        warnings.append(f"Dosyanın biçim sürümü ({version}) bu okuyucunun KiCad'le karşılaştırıldığı sürümlerden değil "
                        f"({', '.join(str(v) for v in TESTED_FORMAT_VERSIONS)}); sonuçları KiCad'deki uzunlukla karşılaştırın.")

    layer_table = _node(root, "layers") or []
    copper = _copper_order(str(entry[1]) for entry in layer_table[1:]
                           if isinstance(entry, list) and len(entry) > 1 and str(entry[1]).endswith(".Cu"))
    numbered = {str(net[1]): str(net[2]) for net in _nodes(root, "net") if len(net) >= 3}

    def span(first: str, last: str) -> Tuple[str, ...]:
        if first not in copper or last not in copper:
            return tuple(name for name in (first, last) if name in copper)
        lo, hi = sorted((copper.index(first), copper.index(last)))
        return copper[lo:hi + 1]

    tracks: List[BoardTrack] = []
    for head in ("segment", "arc"):
        for node in _nodes(root, head):
            mid_node = _node(node, "mid") if head == "arc" else None
            tracks.append(BoardTrack(
                net=_net_name(node, numbered), layer=_text(_node(node, "layer")),
                width_mm=_floats(_node(node, "width"), 1, f"{head} width")[0],
                start=tuple(_floats(_node(node, "start"), 2, f"{head} start")),   # type: ignore[arg-type]
                end=tuple(_floats(_node(node, "end"), 2, f"{head} end")),         # type: ignore[arg-type]
                mid=tuple(_floats(mid_node, 2, "arc mid")) if head == "arc" else None))   # type: ignore[arg-type]

    vias: List[BoardVia] = []
    for node in _nodes(root, "via"):
        layers = _node(node, "layers") or []
        drill = _node(node, "drill")
        vias.append(BoardVia(
            net=_net_name(node, numbered), at=tuple(_floats(_node(node, "at"), 2, "via at")),   # type: ignore[arg-type]
            diameter_mm=_floats(_node(node, "size"), 1, "via size")[0],
            drill_mm=float(drill[1]) if drill is not None and len(drill) > 1 else 0.0,
            layers=span(str(layers[1]), str(layers[2])) if len(layers) >= 3 else copper))

    pads: List[BoardPad] = []
    for footprint in _nodes(root, "footprint"):
        position = _node(footprint, "at")
        fx, fy = _floats(position, 2, "footprint at")
        rotation = math.radians(float(position[3])) if position is not None and len(position) > 3 else 0.0
        cos, sin = math.cos(rotation), math.sin(rotation)
        ref = next((str(prop[2]) for prop in _nodes(footprint, "property") if len(prop) > 2 and prop[1] == "Reference"), "")
        if not ref:   # KiCad 6-7: (fp_text reference "R1" ...)
            ref = next((str(t[2]) for t in _nodes(footprint, "fp_text") if len(t) > 2 and t[1] == "reference"), "")
        # KiCad 9+: footprint içi jumper. Sözdizimi KiCad 10.0.3'ün kendi yazdığı biçim (examples/kicad_oracle.py):
        # (duplicate_pad_numbers_are_jumpers yes) ve (jumper_pad_groups ("1" "3") ...)
        fp_key = f"{len(pads)}:{ref}"
        duplicates = _text(_node(footprint, "duplicate_pad_numbers_are_jumpers")) == "yes"
        grouped: Dict[str, str] = {}
        for k, group in enumerate((_node(footprint, "jumper_pad_groups") or [])[1:]):
            if isinstance(group, list):
                for number in group:
                    grouped[str(number)] = f"{fp_key}:g{k}"
        for node in _nodes(footprint, "pad"):
            if len(node) < 4:
                continue
            at = _node(node, "at")
            px, py = _floats(at, 2, "pad at")
            size = _node(node, "size")
            ratio = _node(node, "roundrect_rratio")
            pads.append(BoardPad(
                ref=ref, number=str(node[1]), net=_net_name(node, numbered),
                # KiCad: y aşağı; footprint dönüşü pad konumunu saat yönünün tersine çevirir
                at=(fx + px * cos + py * sin, fy - px * sin + py * cos),
                size_mm=tuple(_floats(size, 2, "pad size")) if size is not None else (0.0, 0.0),   # type: ignore[arg-type]
                angle_deg=float(at[3]) if at is not None and len(at) > 3 else 0.0,
                shape=str(node[3]), kind=str(node[2]),
                layers=_expand_layers([str(v) for v in (_node(node, "layers") or [])[1:]], copper),
                rratio=float(ratio[1]) if ratio is not None and len(ratio) > 1 else 0.0,
                jumper=grouped.get(str(node[1])) or (f"{fp_key}:#{node[1]}" if duplicates else "")))

    zones: List[BoardZone] = []
    for node in _nodes(root, "zone"):
        name_node = _node(node, "net_name")
        net = _text(name_node) if name_node is not None else _net_name(node, numbered)
        layer_node = _node(node, "layers") or _node(node, "layer") or []
        zone_layers = _expand_layers([str(v) for v in layer_node[1:]], copper)
        if not net or not zone_layers:
            continue   # kural alanı (keepout) ya da bakır dışı katman
        fills: Dict[str, List[Tuple[float, ...]]] = {}
        for polygon in _nodes(node, "filled_polygon"):
            points = _node(polygon, "pts") or []
            flat = tuple(float(v) for xy in _nodes(points, "xy") for v in xy[1:3])
            if len(flat) >= 6:
                fills.setdefault(_text(_node(polygon, "layer")), []).append(flat)
        zones.append(BoardZone(net=net, layers=zone_layers, fills={k: tuple(v) for k, v in fills.items()}))

    drawing_nets = sorted({_net_name(node, numbered)
                           for head in ("gr_line", "gr_rect", "gr_arc", "gr_circle", "gr_poly", "gr_curve")
                           for node in _nodes(root, head) if _text(_node(node, "layer")) in copper} - {""})
    title_block = _node(root, "title_block") or []
    general = _node(root, "general") or []
    thickness = _node(general, "thickness")
    generator = " ".join(part for part in (_text(_node(root, "generator")), _text(_node(root, "generator_version"))) if part)
    return KicadBoard(
        path=path, sha256=hashlib.sha256(data).hexdigest(), format_version=version, generator=generator,
        title=_text(_node(title_block, "title")), revision=_text(_node(title_block, "rev")),
        date=_text(_node(title_block, "date")),
        thickness_mm=float(thickness[1]) if thickness is not None and len(thickness) > 1 else None,
        copper_layers=copper, stackup=_stackup(_node(root, "setup")), tracks=tuple(tracks), vias=tuple(vias),
        pads=tuple(pads), zones=tuple(zones), warnings=warnings, drawing_nets=tuple(drawing_nets))
