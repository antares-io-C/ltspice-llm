"""
Şema görüntüleme
~~~~~~~~~~~~~~~~
LTspice şemasını (``.asc``) SVG'ye çizer: kablolar, semboller (``.asy`` çizimleri), toprak ve ağ etiketleri, yazılar ve
kavşak noktaları. `ltspice_llm.monitor` bunu tarayıcıda gösterir ve dosya kaydedildikçe yeniler.

Biçim ADI SCHEMATIC-REFERENCE'tan ("ASC File Format Overview", "Coordinate System and Grid", "WINDOW", "Graphic
Elements", "Rotation and Mirroring"); belgede olmayanlar LTspice 26.1.1'in ``examples`` (4334 şema) ve ``lib/sym``
(6642 sembol) dosyalarından ölçüldü:

- Koordinatlar tam sayı; +x sağa, +y aşağı. Sembol çizimi yerel koordinattadır; yönü (R0…M270) `schematic.transform_pin`
  ile aynı dönüşümle uygulanır: aynalamada önce x'in işareti değişir, sonra döndürülür.
- ``ARC Normal x1 y1 x2 y2 x3 y3 x4 y4``: (x1,y1)-(x2,y2) kutusundaki elipsin, merkezden (x3,y3)'e giden ışından
  (x4,y4)'e giden ışına kadar ekranda saat yönünün tersine giden parçası (Windows GDI ``Arc``). Belge yönü söylemiyor;
  kütüphanedeki bobin (``ind.asy``) ve polarize kondansatör (``polcap.asy``) yalnızca bu yönle sembol şeklini verir
  (ters yönde bobinin kıvrımları 4 birim derinliğinde kalır, kondansatörün eğri plakası tam daireye yaklaşır).
- Çizgi, dikdörtgen, daire ve yayın sondaki sayısı çizgi biçimidir; 0 dışındakiler kesikli çizilir.
- ``WINDOW n x y hiza boyut``: n özniteliğin numarası (0 InstName, 3 Value, 39 SpiceLine, 123 Value2; kütüphanede 38
  SpiceModel ve 40 SpiceLine2 ile geçer). Değeri boş öznitelik ve ``Invisible`` hiza çizilmez.
- Hiza ``Left/Right/Center/Top/Bottom``; ``V`` önekiyle yazı düşeydir (aşağıdan yukarı okunur). Sembol döndürülünce
  yazı da döner ama baş aşağı okunmaz: sol/sağ ve üst/alt hiza gerektiğinde yer değiştirir. ADI'nin R90 gerilim
  kaynağı örneğinde (``WINDOW 0 -32 56 VBottom``) isim gövdenin üstünde, yatay ve alta hizalı durur.
- ``PIN x y KONUM ofset``: pin adı KONUM hizasıyla (``NONE``: gösterilmez), ofset kadar sembolün içine doğru yazılır.
- ``TEXT`` içeriği ``!`` (SPICE komutu) ya da ``;`` (yorum) ile başlar; satır sonu metinde ``\\n`` olarak durur (ADI
  örneklerinde 839 yazı).
- Üç ya da daha çok kablonun buluştuğu noktaya kavşak noktası konur (ADI "Wires"); ortasından geçilen kablo iki sayılır.
  LTspice 26.1.1'in netlisteri (``-netlist``) böyle bölünmemiş bir T'yi ve eğik kablonun ortasına inen ucu bağlar,
  ucu olmayan kesişmeyi bağlamaz (ölçüm 2026-11-20, examples/23).

Yazı boyutu belgede yok: 0-7 numaralı boyutlar LTspice'ın yazı penceresindeki 0,625x-7x çarpanlarına göre ölçeklenir,
2 (1,5x) varsayılandır. Görsel bir yaklaşıklıktır; ölçümde kullanılmaz.
"""

from __future__ import annotations

import functools
import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .schematic import read_asc_file
from .symbols import _normalize, _scan, find_symbol

PathLike = Union[str, Path]
Matrix = Tuple[int, int, int, int]

#: Yön -> (a, b, c, d): x' = a·x + c·y, y' = b·x + d·y (SVG ``matrix(a b c d e f)``); `schematic.transform_pin` ile aynı
ORIENTATIONS: Dict[str, Matrix] = {
    "R0": (1, 0, 0, 1), "R90": (0, 1, -1, 0), "R180": (-1, 0, 0, -1), "R270": (0, -1, 1, 0),
    "M0": (-1, 0, 0, 1), "M90": (0, -1, -1, 0), "M180": (1, 0, 0, -1), "M270": (0, 1, 1, 0),
}
#: WINDOW numarası -> öznitelik
WINDOW_ATTRIBUTES = {0: "InstName", 3: "Value", 38: "SpiceModel", 39: "SpiceLine", 40: "SpiceLine2", 123: "Value2"}
#: Yazı boyutu numarası -> çarpan (görsel yaklaşıklık; bkz. modül belgesi)
SIZE_FACTORS = (0.625, 1.0, 1.5, 2.0, 2.5, 3.5, 5.0, 7.0)
#: 1,0x yazının şema birimi cinsinden boyu
FONT_UNIT = 16.0
#: Pin adları öznitelik yazısından küçük çizilir
PIN_LABEL_SCALE = 0.8

_SHAPES = {"LINE": ("line", 4), "RECTANGLE": ("rect", 4), "CIRCLE": ("circle", 4), "ARC": ("arc", 8)}
_HORIZONTAL = {"Left": ("start", "middle"), "Right": ("end", "middle"), "Center": ("middle", "middle"),
               "Top": ("middle", "top"), "Bottom": ("middle", "bottom")}
#: (okuma yönü, harflerin üstü): yatay yazı soldan sağa, düşey yazı aşağıdan yukarı okunur
_H_FRAME = ((1, 0), (0, -1))
_V_FRAME = ((0, -1), (-1, 0))
#: XML 1.0'ın taşıyamadığı karakterler (§2.2 Char): sekme/LF/CR dışındaki kontrol karakterleri, vekiller, U+FFFE/U+FFFF.
#: UTF-16 yazılmış bir .asc bunları taşıyabilir; bozuk SVG'yi tarayıcı hiç çizmez.
_NOT_XML = re.compile("[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def _xml_text(text: str) -> str:
    """SVG'ye yazılacak metin: XML'in taşıyamadığı karakterler atılır, kalan kaçışlanır."""
    return escape(_NOT_XML.sub("", text))


# --------------------------------------------------------------------------- ayrıştırma
@dataclass(frozen=True)
class Shape:
    """Çizim öğesi: ``line``/``rect``/``circle`` dört, ``arc`` sekiz koordinat."""
    kind: str
    coords: Tuple[int, ...]
    dashed: bool = False


@dataclass(frozen=True)
class Label:
    x: int
    y: int
    just: str
    size: int
    text: str


@dataclass
class PinMark:
    x: int
    y: int
    position: str
    offset: int
    name: Optional[str] = None


@dataclass
class SymbolDrawing:
    """Bir ``.asy`` sembolünün çizimi (yerel koordinatlarda)."""
    shapes: List[Shape] = field(default_factory=list)
    texts: List[Label] = field(default_factory=list)
    windows: Dict[int, Tuple[int, int, str, int]] = field(default_factory=dict)
    attrs: Dict[str, str] = field(default_factory=dict)
    pins: List[PinMark] = field(default_factory=list)


@dataclass
class Placement:
    """Şemadaki bir sembol: ad, konum, yön, pencere ve öznitelikleri."""
    name: str
    x: int
    y: int
    orient: str
    windows: Dict[int, Tuple[int, int, str, int]] = field(default_factory=dict)
    attrs: Dict[str, str] = field(default_factory=dict)


@dataclass
class AscDrawing:
    wires: List[Tuple[int, int, int, int]] = field(default_factory=list)
    flags: List[Tuple[int, int, str]] = field(default_factory=list)
    iopins: Dict[Tuple[int, int], str] = field(default_factory=dict)
    symbols: List[Placement] = field(default_factory=list)
    texts: List[Label] = field(default_factory=list)
    shapes: List[Shape] = field(default_factory=list)
    dataflags: List[Tuple[int, int, str]] = field(default_factory=list)


def _ints(fields: Sequence[str]) -> Optional[Tuple[int, ...]]:
    try:
        return tuple(int(f) for f in fields)
    except ValueError:
        return None


def _shape(head: str, rest: str) -> Optional[Shape]:
    kind, count = _SHAPES[head]
    parts = rest.split()
    if parts and _ints(parts[:1]) is None:  # kalem adı ("Normal")
        parts = parts[1:]
    coords = _ints(parts[:count])
    if coords is None or len(coords) != count:
        return None
    style = parts[count] if len(parts) > count else "0"
    return Shape(kind, coords, dashed=style != "0")


def _window(rest: str) -> Optional[Tuple[int, Tuple[int, int, str, int]]]:
    parts = rest.split()
    values = _ints(parts[:3])
    if values is None or len(values) != 3:
        return None
    size = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 2
    return values[0], (values[1], values[2], parts[3] if len(parts) > 3 else "Left", size)


def _label(rest: str) -> Optional[Label]:
    parts = rest.split(" ", 4)
    values = _ints(parts[:2])
    if values is None or len(values) != 2 or len(parts) < 4:
        return None
    size = int(parts[3]) if parts[3].isdigit() else 2
    return Label(values[0], values[1], parts[2], size, parts[4] if len(parts) > 4 else "")


def parse_asc(text: str) -> AscDrawing:
    """``.asc`` metnini çizim öğelerine ayırır; tanınmayan ya da bozuk satırlar atlanır."""
    drawing = AscDrawing()
    current: Optional[Placement] = None
    for raw in text.splitlines():
        head, _, rest = raw.strip().partition(" ")
        if head == "WIRE":
            wire = _ints(rest.split()[:4])
            if wire is not None and len(wire) == 4:
                drawing.wires.append((wire[0], wire[1], wire[2], wire[3]))
        elif head in ("FLAG", "IOPIN", "DATAFLAG"):
            parts = rest.split(" ", 2)
            point = _ints(parts[:2])
            if point is not None and len(point) == 2 and len(parts) == 3:
                if head == "FLAG":
                    drawing.flags.append((point[0], point[1], parts[2].strip()))
                elif head == "IOPIN":
                    drawing.iopins[(point[0], point[1])] = parts[2].strip()
                else:
                    drawing.dataflags.append((point[0], point[1], parts[2].strip().strip('"')))
        elif head == "SYMBOL":
            parts = rest.rsplit(" ", 3)
            point = _ints(parts[1:3]) if len(parts) == 4 else None
            current = None
            if point is not None and len(point) == 2:
                current = Placement(parts[0], point[0], point[1], parts[3].strip())
                drawing.symbols.append(current)
        elif head == "WINDOW" and current is not None:
            window = _window(rest)
            if window is not None:
                current.windows[window[0]] = window[1]
        elif head == "SYMATTR" and current is not None:
            key, _, value = rest.partition(" ")
            current.attrs[key] = value
        elif head == "TEXT":
            label = _label(rest)
            if label is not None:
                drawing.texts.append(label)
        elif head in _SHAPES:
            shape = _shape(head, rest)
            if shape is not None:
                drawing.shapes.append(shape)
    return drawing


def parse_asy(text: str) -> SymbolDrawing:
    """``.asy`` metnini sembol çizimine ayırır."""
    symbol = SymbolDrawing()
    for raw in text.splitlines():
        head, _, rest = raw.strip().partition(" ")
        if head in _SHAPES:
            shape = _shape(head, rest)
            if shape is not None:
                symbol.shapes.append(shape)
        elif head == "TEXT":
            label = _label(rest)
            if label is not None:
                symbol.texts.append(label)
        elif head == "WINDOW":
            window = _window(rest)
            if window is not None:
                symbol.windows[window[0]] = window[1]
        elif head == "SYMATTR":
            key, _, value = rest.partition(" ")
            symbol.attrs[key] = value
        elif head == "PIN":
            parts = rest.split()
            point = _ints(parts[:2])
            if point is not None and len(point) == 2:
                offset = int(parts[3]) if len(parts) > 3 and parts[3].lstrip("-").isdigit() else 0
                symbol.pins.append(PinMark(point[0], point[1], parts[2] if len(parts) > 2 else "NONE", offset))
        elif head == "PINATTR" and symbol.pins:
            key, _, value = rest.partition(" ")
            if key == "PinName":
                symbol.pins[-1].name = value.strip()
    return symbol


# --------------------------------------------------------------------------- geometri
def apply(m: Matrix, ox: float, oy: float, x: float, y: float) -> Tuple[float, float]:
    """Yerel (x, y) noktasının dünya koordinatı: ``(ox, oy) + m·(x, y)``."""
    a, b, c, d = m
    return a * x + c * y + ox, b * x + d * y + oy


@dataclass(frozen=True)
class ArcGeometry:
    """Elips yayı. Açılar radyan, matematik yönünde (ekranda saat yönünün tersi pozitif); ``sweep`` 2π ise tam elips."""
    cx: float
    cy: float
    rx: float
    ry: float
    start: float
    sweep: float

    def point(self, fraction: float) -> Tuple[float, float]:
        """Yayın ``fraction`` (0: başı, 1: sonu) açısındaki ışının elipsi kestiği nokta."""
        theta = self.start + fraction * self.sweep
        dx, dy = math.cos(theta), -math.sin(theta)
        t = 1.0 / math.hypot(dx / self.rx, dy / self.ry)
        return self.cx + t * dx, self.cy + t * dy


def arc_geometry(coords: Sequence[int]) -> Optional[ArcGeometry]:
    """``ARC`` koordinatlarından yay (bkz. modül belgesi); kutusu düz bir çizgiyse None."""
    x1, y1, x2, y2, x3, y3, x4, y4 = coords
    cx, cy, rx, ry = (x1 + x2) / 2, (y1 + y2) / 2, abs(x2 - x1) / 2, abs(y2 - y1) / 2
    if rx == 0 or ry == 0:
        return None
    start = math.atan2(-(y3 - cy), x3 - cx)
    sweep = (math.atan2(-(y4 - cy), x4 - cx) - start) % (2 * math.pi)
    return ArcGeometry(cx, cy, rx, ry, start, sweep if sweep > 1e-9 else 2 * math.pi)


def _num(value: float) -> str:
    rounded = round(value, 3)
    return str(int(rounded)) if float(rounded).is_integer() else f"{rounded:.3f}".rstrip("0")


def arc_path(geometry: ArcGeometry) -> str:
    """SVG yolu. SVG'de y aşağı olduğundan ``sweep-flag`` 1 ekranda saat yönüdür; bu yaylar için 0 (SVG 1.1, 8.3.8)."""
    (sx, sy), (ex, ey) = geometry.point(0.0), geometry.point(1.0)
    large = 1 if geometry.sweep > math.pi else 0
    return (f"M{_num(sx)} {_num(sy)}A{_num(geometry.rx)} {_num(geometry.ry)} 0 {large} 0 "
            f"{_num(ex)} {_num(ey)}")


def text_layout(just: str, m: Matrix = (1, 0, 0, 1)) -> Optional[Tuple[bool, str, str]]:
    """
    Hizanın dünyadaki karşılığı: (düşey mi, yatay hiza ``start``/``middle``/``end``, dik hiza ``top``/``middle``/
    ``bottom``). Yazı ``m`` ile döner ama baş aşağı okunmaz (bkz. modül belgesi). ``Invisible`` ya da bilinmeyen
    hizada None.
    """
    vertical = just[:1] == "V" and just[1:] in _HORIZONTAL
    base = just[1:] if vertical else just
    if base not in _HORIZONTAL:
        return None
    h, v = _HORIZONTAL[base]
    (r, u) = _V_FRAME if vertical else _H_FRAME
    a, b, c, d = m
    rw = (a * r[0] + c * r[1], b * r[0] + d * r[1])
    uw = (a * u[0] + c * u[1], b * u[0] + d * u[1])
    world_vertical = rw[0] == 0
    fr, fu = _V_FRAME if world_vertical else _H_FRAME
    if rw != fr:
        h = {"start": "end", "end": "start"}.get(h, h)
    if uw != fu:
        v = {"top": "bottom", "bottom": "top"}.get(v, v)
    return world_vertical, h, v


def _pin_anchor(pin: PinMark) -> Optional[Tuple[int, int, str]]:
    """Pin adının yerel konumu ve hizası; ``NONE`` ya da adsız pinde None."""
    position = pin.position.upper()
    vertical = position.startswith("V") and position[1:] in ("LEFT", "RIGHT", "TOP", "BOTTOM")
    base = position[1:] if vertical else position
    if not pin.name or base not in ("LEFT", "RIGHT", "TOP", "BOTTOM"):
        return None
    just = ("V" if vertical else "") + base.capitalize()
    r, u = _V_FRAME if vertical else _H_FRAME
    step = {"LEFT": r, "RIGHT": (-r[0], -r[1]), "TOP": (-u[0], -u[1]), "BOTTOM": u}[base]  # yazının uzandığı yön
    return pin.x + pin.offset * step[0], pin.y + pin.offset * step[1], just


def junctions(wires: Iterable[Tuple[int, int, int, int]]) -> List[Tuple[int, int]]:
    """Kavşak noktaları: üç ya da daha çok kablonun buluştuğu uçlar; ortasından geçilen kablo iki sayılır."""
    wire_list = list(wires)
    ends: Counter = Counter()
    rows: Dict[int, List[Tuple[int, int]]] = defaultdict(list)
    columns: Dict[int, List[Tuple[int, int]]] = defaultdict(list)
    slanted = []
    for x1, y1, x2, y2 in wire_list:
        ends[(x1, y1)] += 1
        ends[(x2, y2)] += 1
        if y1 == y2:
            rows[y1].append((min(x1, x2), max(x1, x2)))
        elif x1 == x2:
            columns[x1].append((min(y1, y2), max(y1, y2)))
        else:
            slanted.append((x1, y1, x2, y2))
    dots = []
    for (x, y), count in ends.items():
        through = sum(lo < x < hi for lo, hi in rows.get(y, ())) + sum(lo < y < hi for lo, hi in columns.get(x, ()))
        for x1, y1, x2, y2 in slanted:
            on_line = (x2 - x1) * (y - y1) == (y2 - y1) * (x - x1)
            if on_line and min(x1, x2) <= x <= max(x1, x2) and min(y1, y2) <= y <= max(y1, y2) \
                    and (x, y) not in ((x1, y1), (x2, y2)):
                through += 1
        if count + 2 * through >= 3:
            dots.append((x, y))
    return sorted(dots)


# --------------------------------------------------------------------------- SVG
@dataclass
class RenderedSchematic:
    """`render_asc` sonucu: SVG, görünüm kutusu (x, y, genişlik, yükseklik), uyarılar ve sayımlar."""
    svg: str
    viewbox: Tuple[float, float, float, float]
    warnings: List[str]
    missing_symbols: List[str]
    counts: Dict[str, int]
    encoding: str = ""


class _Canvas:
    def __init__(self) -> None:
        self.parts: List[str] = []
        self.xs: List[float] = []
        self.ys: List[float] = []

    def extend(self, points: Iterable[Tuple[float, float]]) -> None:
        for x, y in points:
            self.xs.append(x)
            self.ys.append(y)

    def text(self, x: float, y: float, layout: Tuple[bool, str, str], size: int, text: str, cls: str,
             scale: float = 1.0) -> None:
        vertical, h, v = layout
        font = FONT_UNIT * SIZE_FACTORS[min(max(size, 0), len(SIZE_FACTORS) - 1)] * scale
        lines = [_NOT_XML.sub("", line) or " " for line in text.split("\\n")]
        step = font * 1.2
        first = -(len(lines) - 1) * step if v == "bottom" else 0.0
        baseline = {"top": "text-before-edge", "middle": "central", "bottom": "text-after-edge"}[v]
        rotate = f' transform="rotate(-90 {_num(x)} {_num(y)})"' if vertical else ""
        spans = "".join(f'<tspan x="{_num(x)}" dy="{_num(first if i == 0 else step)}">{escape(line)}</tspan>'
                        for i, line in enumerate(lines))
        self.parts.append(f'<text class="{cls}" x="{_num(x)}" y="{_num(y)}" font-size="{_num(font)}" '
                          f'text-anchor="{h}" dominant-baseline="{baseline}"{rotate}>{spans}</text>')
        # sınır kutusu için kaba yazı alanı
        width, height = 0.6 * font * max(len(line) for line in lines), step * len(lines)
        along = {"start": (0.0, width), "middle": (-width / 2, width / 2), "end": (-width, 0.0)}[h]
        across = {"top": (0.0, height), "middle": (-height / 2, height / 2), "bottom": (-height, 0.0)}[v]
        if vertical:
            self.extend([(x + across[0], y - along[1]), (x + across[1], y - along[0])])
        else:
            self.extend([(x + along[0], y + across[0]), (x + along[1], y + across[1])])


def _shape_svg(shape: Shape, cls: str = "") -> Tuple[str, List[Tuple[float, float]]]:
    c = shape.coords
    attrs = f' class="{cls}"' if cls else ""
    dash = ' stroke-dasharray="8 6"' if shape.dashed else ""
    corners: List[Tuple[float, float]] = [(c[0], c[1]), (c[2], c[3])]
    if shape.kind == "line":
        return f'<line x1="{c[0]}" y1="{c[1]}" x2="{c[2]}" y2="{c[3]}"{attrs}{dash}/>', corners
    if shape.kind == "rect":
        x, y = min(c[0], c[2]), min(c[1], c[3])
        return (f'<rect x="{x}" y="{y}" width="{abs(c[2] - c[0])}" height="{abs(c[3] - c[1])}"{attrs}{dash}/>',
                corners)
    ellipse = (f'<ellipse cx="{_num((c[0] + c[2]) / 2)}" cy="{_num((c[1] + c[3]) / 2)}" '
               f'rx="{_num(abs(c[2] - c[0]) / 2)}" ry="{_num(abs(c[3] - c[1]) / 2)}"{attrs}{dash}/>')
    if shape.kind == "circle":
        return ellipse, corners
    geometry = arc_geometry(c)
    if geometry is None:
        return f'<line x1="{c[0]}" y1="{c[1]}" x2="{c[2]}" y2="{c[3]}"{attrs}{dash}/>', corners
    if geometry.sweep >= 2 * math.pi:
        return ellipse, corners
    return f'<path d="{arc_path(geometry)}"{attrs}{dash}/>', corners


def _matrix_text(m: Matrix, x: int, y: int) -> str:
    return f"matrix({m[0]} {m[1]} {m[2]} {m[3]} {x} {y})"


def _label_side(x: int, y: int, wires: Sequence[Tuple[int, int, int, int]]) -> str:
    """Ağ etiketinin yazılacağı yan: kablo gelmeyen taraf (yatay kabloda üst)."""
    directions = set()
    for x1, y1, x2, y2 in wires:
        for (px, py), (qx, qy) in (((x1, y1), (x2, y2)), ((x2, y2), (x1, y1))):
            if (px, py) == (x, y) and (qx, qy) != (x, y):
                directions.add("up" if qy < py else "down" if qy > py else "left" if qx < px else "right")
    if "up" in directions and "down" not in directions:
        return "below"
    if directions == {"left"}:
        return "right"
    if directions == {"right"}:
        return "left"
    return "above"


def render_drawing(drawing: AscDrawing, load: Callable[[str], Optional[SymbolDrawing]], title: str = "",
                   standalone: bool = False) -> RenderedSchematic:
    """
    Ayrıştırılmış şemayı SVG'ye çizer. ``load(sembol_adı)`` sembol çizimini ya da bulunamazsa None döner.
    ``standalone``: SVG kendi stiliyle (açık/koyu tema) tek başına açılabilsin.
    """
    canvas = _Canvas()
    warnings: List[str] = []
    missing: List[str] = []
    wires_svg = "".join(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}"/>' for x1, y1, x2, y2 in drawing.wires)
    canvas.parts.append(f'<g class="wire">{wires_svg}</g>')
    for x1, y1, x2, y2 in drawing.wires:
        canvas.extend([(x1, y1), (x2, y2)])
    notes = [_shape_svg(shape) for shape in drawing.shapes]
    if notes:
        canvas.parts.append('<g class="note">' + "".join(svg for svg, _ in notes) + "</g>")
        for _, points in notes:
            canvas.extend(points)
    for placement in drawing.symbols:
        _render_symbol(canvas, placement, load, warnings, missing)
    for x, y, name in drawing.flags:
        if name == "0":
            canvas.parts.append(f'<path class="ground" d="M{x - 16} {y}H{x + 16}L{x} {y + 16}Z"/>')
            canvas.extend([(x - 16, y), (x + 16, y + 16)])
            continue
        if (x, y) in drawing.iopins:
            canvas.parts.append(f'<path class="io" d="M{x} {y}l-8 -8h-16v16h16Z"/>')
            canvas.extend([(x - 24, y - 8), (x, y + 8)])
        side = _label_side(x, y, drawing.wires)
        anchor = {"above": (x, y - 4, "Bottom"), "below": (x, y + 4, "Top"), "left": (x - 6, y, "Right"),
                  "right": (x + 6, y, "Left")}[side]
        layout = text_layout(anchor[2])
        assert layout is not None
        canvas.text(anchor[0], anchor[1], layout, 2, name, "t-flag")
    for x, y, expr in drawing.dataflags:
        layout = text_layout("Left")
        assert layout is not None
        canvas.text(x + 4, y, layout, 1, expr, "t-data")
    dots = junctions(drawing.wires)
    if dots:
        canvas.parts.append('<g class="junction">' + "".join(f'<circle cx="{x}" cy="{y}" r="5"/>' for x, y in dots)
                            + "</g>")
    for label in drawing.texts:
        layout = text_layout(label.just)
        if layout is None:
            continue
        kind = label.text[:1]
        cls = "t-dir" if kind == "!" else "t-cmt"
        canvas.text(label.x, label.y, layout, label.size, label.text[1:] if kind in "!;" else label.text, cls)
    viewbox = _viewbox(canvas)
    style = f"<style>{STANDALONE_CSS}</style>" if standalone else ""
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" class="asc" viewBox="{" ".join(_num(v) for v in viewbox)}">'
           f"{style}<title>{_xml_text(title)}</title>{''.join(canvas.parts)}</svg>")
    counts = {"symbols": len(drawing.symbols), "wires": len(drawing.wires), "flags": len(drawing.flags),
              "texts": len(drawing.texts), "junctions": len(dots)}
    return RenderedSchematic(svg, viewbox, warnings, missing, counts)


def _render_symbol(canvas: _Canvas, p: Placement, load: Callable[[str], Optional[SymbolDrawing]],
                   warnings: List[str], missing: List[str]) -> None:
    m = ORIENTATIONS.get(p.orient.upper())
    if m is None:
        warnings.append(f"{p.attrs.get('InstName', p.name)}: bilinmeyen yön '{p.orient}', R0 çizildi.")
        m = ORIENTATIONS["R0"]
    symbol = load(p.name)
    inst = p.attrs.get("InstName", "")
    tip = " · ".join(part for part in (inst, p.name.replace("\\\\", "\\"), p.attrs.get("Value", "")) if part)
    canvas.parts.append(f'<g class="part"><title>{_xml_text(tip)}</title>')
    if symbol is None:
        if p.name not in missing:
            missing.append(p.name)
        canvas.parts.append(f'<g class="missing" transform="{_matrix_text(m, p.x, p.y)}">'
                            f'<rect x="0" y="0" width="64" height="64"/></g>')
        canvas.extend([apply(m, p.x, p.y, 0, 0), apply(m, p.x, p.y, 64, 64)])
        layout = text_layout("Left")
        assert layout is not None
        canvas.text(p.x + 4, p.y - 12, layout, 1, f"{inst} ({p.name}?)".strip(), "t-missing")
        canvas.parts.append("</g>")
        return
    shapes = [_shape_svg(shape) for shape in symbol.shapes]
    canvas.parts.append(f'<g class="sym" transform="{_matrix_text(m, p.x, p.y)}">'
                        + "".join(svg for svg, _ in shapes) + "</g>")
    for _, points in shapes:
        canvas.extend(apply(m, p.x, p.y, x, y) for x, y in points)
    for label in symbol.texts:
        _symbol_text(canvas, m, p, label.x, label.y, label.just, label.size, label.text, "t-sym")
    windows = dict(symbol.windows)
    windows.update(p.windows)
    for number, (x, y, just, size) in sorted(windows.items()):
        attribute = WINDOW_ATTRIBUTES.get(number)
        value = (p.attrs.get(attribute) or symbol.attrs.get(attribute) or "") if attribute else ""
        if value.strip():
            _symbol_text(canvas, m, p, x, y, just, size, value, "t-attr")
    for pin in symbol.pins:
        anchor = _pin_anchor(pin)
        if anchor is not None:
            _symbol_text(canvas, m, p, anchor[0], anchor[1], anchor[2], 2, pin.name or "", "t-pin", PIN_LABEL_SCALE)
    canvas.parts.append("</g>")


def _symbol_text(canvas: _Canvas, m: Matrix, p: Placement, x: int, y: int, just: str, size: int, text: str,
                 cls: str, scale: float = 1.0) -> None:
    layout = text_layout(just, m)
    if layout is not None:
        wx, wy = apply(m, p.x, p.y, x, y)
        canvas.text(wx, wy, layout, size, text, cls, scale)


def _viewbox(canvas: _Canvas, margin: float = 32.0) -> Tuple[float, float, float, float]:
    if not canvas.xs:
        return (0.0, 0.0, 100.0, 100.0)
    x0, x1, y0, y1 = min(canvas.xs), max(canvas.xs), min(canvas.ys), max(canvas.ys)
    return (x0 - margin, y0 - margin, x1 - x0 + 2 * margin, y1 - y0 + 2 * margin)


#: `render_drawing(standalone=True)` SVG'sinin kendi stili; monitör sayfası aynı sınıfları kendi temasıyla boyar
STANDALONE_CSS = (
    ".asc{background:#fff;--wire:#1f5fbf;--sym:#8a2b20;--text:#1d1d1b;--dir:#1d1d1b;--cmt:#1f5fbf;--muted:#6b6a64;"
    "--warn:#a15c00}"
    "@media (prefers-color-scheme:dark){.asc{background:#141414;--wire:#80aaff;--sym:#ff9f8a;--text:#e8e6e1;"
    "--dir:#e8e6e1;--cmt:#80aaff;--muted:#9b9890;--warn:#f0b04a}}"
    ".asc line,.asc rect,.asc ellipse,.asc path{fill:none;stroke-width:1.5px;vector-effect:non-scaling-stroke;"
    "stroke-linecap:round}"
    ".asc .wire,.asc .ground,.asc .io{stroke:var(--wire)}.asc .sym{stroke:var(--sym)}.asc .note{stroke:var(--muted)}"
    ".asc .missing rect{stroke:var(--warn);stroke-dasharray:6 4}.asc .junction{fill:var(--wire)}"
    ".asc text{font-family:system-ui,'Segoe UI',sans-serif;fill:var(--text);white-space:pre}"
    ".asc .t-dir{fill:var(--dir);font-weight:600}.asc .t-cmt,.asc .t-flag{fill:var(--cmt)}"
    ".asc .t-pin,.asc .t-sym,.asc .t-data{fill:var(--muted)}.asc .t-missing{fill:var(--warn)}"
)


# --------------------------------------------------------------------------- dosyadan
@functools.lru_cache(maxsize=1024)
def _symbol_cached(path: str, mtime_ns: int) -> SymbolDrawing:
    return parse_asy(read_asc_file(path)[0])


def load_symbol_drawing(path: PathLike) -> SymbolDrawing:
    """``.asy`` çizimi (dosya değişmedikçe önbellekten)."""
    p = Path(path)
    return _symbol_cached(str(p), p.stat().st_mtime_ns)


def _folder_loader(folder: Path) -> Callable[[str], Optional[SymbolDrawing]]:
    """
    Sembolleri LTspice'ın sırasıyla bulur: önce şemanın klasörü ve alt klasörleri, sonra kullanıcı klasörü ve ADI
    kütüphanesi (`symbols.find_symbol`; ADI SCHEMATIC-REFERENCE "Default Search Priority"). Klasör bir kez listelenir.
    """
    by_path, by_name = _scan(os.path.normcase(str(folder.resolve()))) if folder.is_dir() else ({}, {})
    found: Dict[str, Optional[SymbolDrawing]] = {}

    def load(name: str) -> Optional[SymbolDrawing]:
        if name not in found:
            key = _normalize(name)
            hit = by_path.get(key) or by_name.get(key.rsplit("/", 1)[-1])
            path = Path(hit) if hit else find_symbol(name)
            try:
                found[name] = load_symbol_drawing(path) if path is not None else None
            except OSError:
                found[name] = None
        return found[name]

    return load


def render_asc(path: PathLike, standalone: bool = False) -> RenderedSchematic:
    """
    ``.asc`` dosyasını SVG'ye çizer (cp1252 ya da UTF-16LE; bkz. `schematic.read_asc_file`). Bulunamayan semboller
    kesikli kutu olarak çizilir ve ``missing_symbols``'ta listelenir.
    """
    p = Path(path)
    text, encoding = read_asc_file(p)
    rendered = render_drawing(parse_asc(text), _folder_loader(p.parent), title=p.name, standalone=standalone)
    rendered.encoding = encoding
    if rendered.missing_symbols:
        rendered.warnings.insert(0, "Bulunamayan sembol: " + ", ".join(rendered.missing_symbols)
                                 + " (şemanın klasöründe, LTspice kullanıcı klasöründe ve kütüphanesinde arandı).")
    return rendered
