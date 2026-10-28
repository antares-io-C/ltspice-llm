"""
LTspice Schematic (.asc) Builder and Helper
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
LTspice grafiksel şema (.asc) dosyalarını oluşturmak, parametrelerini güncellemek,
sembol ve bağlantı hatlarını (wire) LTspice ızgara koordinatlarına göre
otomatik yerleştirmek için yardımcı modül.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

# Standart sembollerin yerel pin ofsetleri (x, y): LTspice 26.1.1 `lib/sym/*.asy` dosyalarındaki PIN satırları.
# Dikkat: current/bi/csw sembollerinin pinleri (0,0)-(0,80)'dedir, voltage/bv'ninki (0,16)-(0,96). Eski tabloda
# current için (0,16)-(0,96) yazıyordu; bu tabloyla yerleştirilen akım kaynağı netlistte "I1 NC_01 NC_02" olarak
# tamamen kopuk çıkıyordu.
SYMBOL_PIN_DEFS: Dict[str, Dict[str, Tuple[int, int]]] = {
    "res": {"1": (16, 16), "2": (16, 96), "A": (16, 16), "B": (16, 96)},
    "cap": {"1": (16, 0), "2": (16, 64), "A": (16, 0), "B": (16, 64), "+": (16, 0), "-": (16, 64)},
    "polcap": {"1": (16, 0), "2": (16, 64), "A": (16, 0), "B": (16, 64), "+": (16, 0), "-": (16, 64)},
    "ind": {"1": (16, 16), "2": (16, 96), "A": (16, 16), "B": (16, 96)},
    "voltage": {"+": (0, 16), "-": (0, 96), "1": (0, 16), "2": (0, 96)},
    "current": {"+": (0, 0), "-": (0, 80), "1": (0, 0), "2": (0, 80)},
    "bv": {"+": (0, 16), "-": (0, 96), "1": (0, 16), "2": (0, 96)},
    "bi": {"+": (0, 0), "-": (0, 80), "1": (0, 0), "2": (0, 80)},
    "diode": {"+": (16, 0), "-": (16, 64), "1": (16, 0), "2": (16, 64), "anode": (16, 0), "cathode": (16, 64), "A": (16, 0), "K": (16, 64)},
    "zener": {"+": (16, 0), "-": (16, 64), "1": (16, 0), "2": (16, 64), "anode": (16, 0), "cathode": (16, 64), "A": (16, 0), "K": (16, 64)},
    "schottky": {"+": (16, 0), "-": (16, 64), "1": (16, 0), "2": (16, 64), "anode": (16, 0), "cathode": (16, 64), "A": (16, 0), "K": (16, 64)},
    "led": {"+": (16, 0), "-": (16, 64), "1": (16, 0), "2": (16, 64), "anode": (16, 0), "cathode": (16, 64), "A": (16, 0), "K": (16, 64)},
    "npn": {"C": (64, 0), "B": (0, 48), "E": (64, 96)},
    "pnp": {"C": (64, 0), "B": (0, 48), "E": (64, 96)},
    "nmos": {"D": (48, 0), "G": (0, 80), "S": (48, 96)},
    "pmos": {"D": (48, 0), "G": (0, 80), "S": (48, 96)},
    "sw": {"A": (0, 16), "B": (0, 96), "NC+": (-48, 80), "NC-": (-48, 32)},
    "csw": {"+": (0, 0), "-": (0, 80)},
    "e": {"+": (0, 16), "-": (0, 96), "P": (-48, 32), "N": (-48, 80)},
    "g": {"+": (0, 96), "-": (0, 16), "NC+": (-48, 32), "NC-": (-48, 80)},
    "OPAMPS\\OPAMP": {"in-": (-32, 48), "in+": (-32, 80), "out": (32, 64)},
    "opamp": {"in-": (-32, 48), "in+": (-32, 80), "out": (32, 64)},
    "opamp2": {"in+": (-32, 80), "in-": (-32, 48), "v+": (0, 32), "v-": (0, 96), "out": (32, 64)},
    "UniversalOpAmp2": {"in+": (-32, 16), "in-": (-32, -16), "v+": (0, -32), "v-": (0, 32), "out": (32, 0)},
}


# --------------------------------------------------------------------------- .asc kodlaması
# LTspice .asc dosyalarını Windows-1252 (ya da BOM'suz UTF-16LE) olarak okur ve yazar; UTF-8 kabul etmez.
# Ölçüm (LTspice 26.1.1): "SYMATTR Value 1µ" cp1252 ve UTF-16LE'de 1 µF, UTF-8'de (C2 B5) "1Âµ" -> 1 F okunur.
# ADI'nin örnek şemalarının 3088/4334'ü cp1252, 21'i UTF-16LE, gerisi saf ASCII.
ASC_ENCODINGS = ("cp1252", "utf-16-le")


def decode_asc_bytes(data: bytes) -> Tuple[str, str]:
    """
    .asc baytlarını LTspice'ın okuduğu gibi çözer; (metin, kodlama) döner.

    BOM'lu ya da baştaki NUL baytlarından anlaşılan UTF-16 dosyalar UTF-16 olarak, geri kalan her şey cp1252
    olarak çözülür (cp1252'de tanımsız 5 bayt latin-1 ile korunur, böylece yazarken bayt kaybı olmaz).
    """
    if data.startswith(codecs.BOM_UTF16_LE):
        return data[len(codecs.BOM_UTF16_LE):].decode("utf-16-le", errors="replace"), "utf-16-le"
    if data.startswith(codecs.BOM_UTF16_BE):
        return data[len(codecs.BOM_UTF16_BE):].decode("utf-16-be", errors="replace"), "utf-16-be"
    if b"\x00" in data[:256]:
        return data.decode("utf-16-le", errors="replace"), "utf-16-le"
    try:
        return data.decode("cp1252"), "cp1252"
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1"


def encode_asc_text(text: str, encoding: Optional[str] = None) -> bytes:
    """
    .asc metnini LTspice'ın okuyabileceği baytlara çevirir.

    ``encoding`` verilmezse (yeni şema) cp1252 seçilir; metin cp1252'de olmayan karakter içeriyorsa
    (ş, ğ, ı, Yunanca μ...) LTspice'ın da kullandığı BOM'suz UTF-16LE'ye geçilir. UTF-8 asla yazılmaz.
    """
    if not encoding or encoding in ("utf-8", "utf8", "ascii"):
        try:
            return text.encode("cp1252")
        except UnicodeEncodeError:
            return text.encode("utf-16-le")
    if encoding in ("cp1252", "latin-1"):
        try:
            return text.encode(encoding)
        except UnicodeEncodeError:
            return text.encode("utf-16-le")
    return text.encode(encoding)


def read_asc_file(path: Union[str, Path]) -> Tuple[str, str]:
    """Bir .asc dosyasını okur; (metin, kodlama) döner. Bkz. `decode_asc_bytes`."""
    return decode_asc_bytes(Path(path).read_bytes())


def write_asc_file(path: Union[str, Path], text: str, encoding: Optional[str] = None) -> Path:
    """Bir .asc metnini LTspice'ın okuyabileceği kodlamayla yazar. Bkz. `encode_asc_text`."""
    p = Path(path)
    p.write_bytes(encode_asc_text(text, encoding))
    return p


def transform_pin(
    sym_x: int, sym_y: int, pin_lx: int, pin_ly: int, rot: str = "R0"
) -> Tuple[int, int]:
    """
    LTspice sembol rotasyon ve aynalama (R0, R90, R180, R270, M0, M90, ...)
    kurallarına göre pinin mutlak şema koordinatını hesaplar.
    """
    rot = rot.upper()
    if rot == "R0":
        return sym_x + pin_lx, sym_y + pin_ly
    elif rot == "R90":
        return sym_x - pin_ly, sym_y + pin_lx
    elif rot == "R180":
        return sym_x - pin_lx, sym_y - pin_ly
    elif rot == "R270":
        return sym_x + pin_ly, sym_y - pin_lx
    elif rot == "M0":  # Yatay aynalama
        return sym_x - pin_lx, sym_y + pin_ly
    elif rot == "M90":
        return sym_x - pin_ly, sym_y - pin_lx
    elif rot == "M180":
        return sym_x + pin_lx, sym_y - pin_ly
    elif rot == "M270":
        return sym_x + pin_ly, sym_y + pin_lx
    return sym_x + pin_lx, sym_y + pin_ly


@dataclass
class SchematicSymbol:
    name: str
    x: int
    y: int
    rotation: str = "R0"
    inst_name: str = ""
    value: str = ""
    value2: str = ""
    extra_attrs: Dict[str, str] = field(default_factory=dict)

    def to_asc_lines(self) -> List[str]:
        lines = [f"SYMBOL {self.name} {self.x} {self.y} {self.rotation}"]
        if self.inst_name:
            lines.append(f"SYMATTR InstName {self.inst_name}")
        if self.value:
            lines.append(f"SYMATTR Value {self.value}")
        if self.value2:
            lines.append(f"SYMATTR Value2 {self.value2}")
        for k, v in self.extra_attrs.items():
            lines.append(f"SYMATTR {k} {v}")
        return lines


@dataclass
class SchematicWire:
    x1: int
    y1: int
    x2: int
    y2: int

    def to_asc_line(self) -> str:
        return f"WIRE {self.x1} {self.y1} {self.x2} {self.y2}"


@dataclass
class SchematicFlag:
    x: int
    y: int
    name: str  # "0" for GND

    def to_asc_line(self) -> str:
        return f"FLAG {self.x} {self.y} {self.name}"


@dataclass
class SchematicText:
    x: int
    y: int
    text: str
    is_directive: bool = True  # True ise '!', False ise ';' (yorum)
    alignment: str = "Left"
    size: int = 2

    def to_asc_line(self) -> str:
        prefix = "!" if self.is_directive else ";"
        clean_text = self.text.lstrip("!;")
        return f"TEXT {self.x} {self.y} {self.alignment} {self.size} {prefix}{clean_text}"


class AscSchematic:
    """
    LTspice grafiksel şema (.asc) oluşturucu.
    Şemayı UI'da açıldığında pürüzsüz görünecek şekilde formatlar.
    """

    def __init__(
        self, sheet_width: int = 1200, sheet_height: int = 800,
        symbol_dirs: Optional[Sequence[Union[str, Path]]] = None,
    ):
        """
        :param symbol_dirs: `SYMBOL_PIN_DEFS`'te olmayan semboller için ``.asy`` arama dizinleri (sırayla, sonra ADI
            kütüphanesi ``lib/sym``; bkz. `symbols.find_symbol`). Yerel semboller şemanın klasöründeyse onu verin.
        """
        self.sheet_width = sheet_width
        self.sheet_height = sheet_height
        self.symbol_dirs: List[Union[str, Path]] = list(symbol_dirs or [])
        self.symbols: List[SchematicSymbol] = []
        self.wires: List[SchematicWire] = []
        self.flags: List[SchematicFlag] = []
        self.texts: List[SchematicText] = []

    def add_symbol(
        self,
        name: str,
        x: int,
        y: int,
        rotation: str = "R0",
        inst_name: str = "",
        value: str = "",
        value2: str = "",
        extra_attrs: Optional[Dict[str, str]] = None,
    ) -> SchematicSymbol:
        sym = SchematicSymbol(
            name=name,
            x=x,
            y=y,
            rotation=rotation,
            inst_name=inst_name,
            value=value,
            value2=value2,
            extra_attrs=extra_attrs or {},
        )
        self.symbols.append(sym)
        return sym

    def get_pin(self, sym: SchematicSymbol, pin_name: str) -> Tuple[int, int]:
        """
        Sembolün verilen pin adına karşılık gelen mutlak şema koordinatını döner.

        Önce `SYMBOL_PIN_DEFS` (takma adlarıyla: ``1``/``A``/``+``), tabloda olmayan sembolde ``.asy`` dosyası
        (`symbols.load_symbol`): pin ``PinName`` ya da SpiceOrder numarasıyla verilir (``"OUT"``, ``"5"``).
        """
        lookup_name = sym.name
        if lookup_name not in SYMBOL_PIN_DEFS:
            # "OpAmps\\opamp2" gibi kütüphane yollu adlar son bileşenle eşleşir ("e" anahtarı "battery"ye uymaz)
            base = lookup_name.replace("/", "\\").split("\\")[-1].lower()
            for k in SYMBOL_PIN_DEFS:
                if k.lower() == lookup_name.lower() or k.split("\\")[-1].lower() == base:
                    lookup_name = k
                    break
        pins = SYMBOL_PIN_DEFS.get(lookup_name)
        if pins is None:
            from .symbols import load_symbol  # symbols bu modülü içe aktarır

            definition = load_symbol(sym.name, self.symbol_dirs)
            if definition is not None:
                lx, ly = definition.pin(pin_name)
                return transform_pin(sym.x, sym.y, lx, ly, sym.rotation)
            pins = {}
        if pin_name not in pins:
            raise KeyError(f"'{sym.name}' sembolünde '{pin_name}' pini tanımlı değil. Tanımlılar: {list(pins.keys())}")
        lx, ly = pins[pin_name]
        return transform_pin(sym.x, sym.y, lx, ly, sym.rotation)

    def add_wire(self, x1: int, y1: int, x2: int, y2: int) -> SchematicWire:
        wire = SchematicWire(x1, y1, x2, y2)
        self.wires.append(wire)
        return wire

    def connect(self, p1: Tuple[int, int], p2: Tuple[int, int]) -> List[SchematicWire]:
        """İki koordinat arasına dik açılı (orthogonal) wire bağlantısı çeker."""
        x1, y1 = p1
        x2, y2 = p2
        created: List[SchematicWire] = []
        if x1 == x2 or y1 == y2:
            created.append(self.add_wire(x1, y1, x2, y2))
        else:
            # L-şeklinde iki hat: önce yatay, sonra dikey
            created.append(self.add_wire(x1, y1, x2, y1))
            created.append(self.add_wire(x2, y1, x2, y2))
        return created

    def add_ground(self, x: int, y: int) -> SchematicFlag:
        flag = SchematicFlag(x, y, "0")
        self.flags.append(flag)
        return flag

    def add_net_label(self, x: int, y: int, net_name: str) -> SchematicFlag:
        flag = SchematicFlag(x, y, net_name)
        self.flags.append(flag)
        return flag

    def add_directive(self, x: int, y: int, directive: str) -> SchematicText:
        text = SchematicText(x, y, directive, is_directive=True)
        self.texts.append(text)
        return text

    def add_comment(self, x: int, y: int, comment: str) -> SchematicText:
        text = SchematicText(x, y, comment, is_directive=False)
        self.texts.append(text)
        return text

    def render(self) -> str:
        """Şemayı LTspice .asc biçiminde metne dönüştürür."""
        lines = [
            "Version 4",
            f"SHEET 1 {self.sheet_width} {self.sheet_height}",
        ]
        for wire in self.wires:
            lines.append(wire.to_asc_line())
        for flag in self.flags:
            lines.append(flag.to_asc_line())
        for sym in self.symbols:
            lines.extend(sym.to_asc_lines())
        for text in self.texts:
            lines.append(text.to_asc_line())
        return "\n".join(lines) + "\n"

    def save(self, file_path: Union[str, Path]) -> Path:
        """
        Şemayı dosyaya yazar (.asc uzantısı otomatik eklenir).

        Kodlama cp1252'dir (cp1252'de olmayan karakter varsa UTF-16LE): LTspice UTF-8 .asc dosyasındaki ``µ``'yü
        ``Âµ`` olarak okur ve değeri bozar. Konum hesabıyla üretilen şemalar hataya açıktır; bağlantıyı
        `LTSpiceAdapter.inspect_schematic` ile (LTspice'ın kendi netlisterıyla) doğrulayın.
        """
        p = Path(file_path)
        if p.suffix.lower() != ".asc":
            p = p.with_suffix(".asc")
        p.parent.mkdir(parents=True, exist_ok=True)
        return write_asc_file(p, self.render())

    def to_dict(self) -> Dict[str, Any]:
        """Şemayı JSON / sözlük temsiline dönüştürür."""
        return {
            "sheet_width": self.sheet_width,
            "sheet_height": self.sheet_height,
            "symbols": [
                {
                    "name": s.name,
                    "x": s.x,
                    "y": s.y,
                    "rotation": s.rotation,
                    "inst_name": s.inst_name,
                    "value": s.value,
                    "value2": s.value2,
                    "extra_attrs": dict(s.extra_attrs),
                }
                for s in self.symbols
            ],
            "wires": [[w.x1, w.y1, w.x2, w.y2] for w in self.wires],
            "flags": [{"x": f.x, "y": f.y, "name": f.name} for f in self.flags],
            "texts": [
                {
                    "x": t.x,
                    "y": t.y,
                    "text": t.text,
                    "is_directive": t.is_directive,
                    "alignment": t.alignment,
                    "size": t.size,
                }
                for t in self.texts
            ],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AscSchematic":
        """
        Sözlük (dict / JSON) yapısından programatik AscSchematic nesnesi kurar.

        Desteklenen anahtarlar:
        - ``sheet_width``, ``sheet_height``: Çizim alanı boyutları (varsayılan: 1200x800).
        - ``symbols``: Sembol listesi veya inst_name anahtarlı sözlük.
        - ``wires``: Doğrudan tel koordinatları ([[x1, y1, x2, y2], ...]).
        - ``connections``: Dik açılı otomatik bağlantı kuralları (örn: {"from_inst": "R1", "from_pin": "2", "to_inst": "C1", "to_pin": "1"} veya {"from": "R1:2", "to": "C1:1"}).
        - ``grounds``: Toprak bayrakları ([[x, y], ...]).
        - ``flags`` / ``net_labels``: Ağ etiketleri ([{"x": x, "y": y, "name": "..."}, ...]).
        - ``directives``: SPICE analiz veya ayar komutları ([".tran 10m", ...]).
        - ``comments``: Şema içi açıklamalar.
        """
        sheet_w = int(data.get("sheet_width", 1200))
        sheet_h = int(data.get("sheet_height", 800))
        sch = cls(sheet_width=sheet_w, sheet_height=sheet_h)
        inst_to_sym: Dict[str, SchematicSymbol] = {}

        # 1. Semboller
        sym_entries = data.get("symbols", [])
        if isinstance(sym_entries, dict):
            converted = []
            for inst, cfg in sym_entries.items():
                item = dict(cfg) if isinstance(cfg, dict) else {}
                item.setdefault("inst_name", inst)
                converted.append(item)
            sym_entries = converted

        for s_data in sym_entries:
            if not isinstance(s_data, dict):
                continue
            name = str(s_data.get("name", "res"))
            x = int(s_data.get("x", 0))
            y = int(s_data.get("y", 0))
            rotation = str(s_data.get("rotation", "R0"))
            inst_name = str(s_data.get("inst_name", ""))
            value = str(s_data.get("value", ""))
            value2 = str(s_data.get("value2", ""))
            extra = s_data.get("extra_attrs")
            sym = sch.add_symbol(
                name=name,
                x=x,
                y=y,
                rotation=rotation,
                inst_name=inst_name,
                value=value,
                value2=value2,
                extra_attrs=extra,
            )
            if inst_name:
                inst_to_sym[inst_name] = sym

        # 2. Doğrudan teller (wires)
        wires = data.get("wires", [])
        for w in wires:
            if isinstance(w, (list, tuple)) and len(w) == 4:
                sch.add_wire(int(w[0]), int(w[1]), int(w[2]), int(w[3]))
            elif isinstance(w, dict) and all(k in w for k in ("x1", "y1", "x2", "y2")):
                sch.add_wire(int(w["x1"]), int(w["y1"]), int(w["x2"]), int(w["y2"]))

        # 3. Yüksek düzey bağlantılar (connections / orthogonal routing)
        def _resolve_pt(val: Any) -> Tuple[int, int]:
            if isinstance(val, (list, tuple)) and len(val) == 2:
                if isinstance(val[0], (int, float)) and isinstance(val[1], (int, float)):
                    return int(val[0]), int(val[1])
                inst_k, pin_k = str(val[0]), str(val[1])
                if inst_k in inst_to_sym:
                    return sch.get_pin(inst_to_sym[inst_k], pin_k)
            elif isinstance(val, str):
                parts = re.split(r"[:\./]", val, maxsplit=1)
                if len(parts) == 2 and parts[0] in inst_to_sym:
                    return sch.get_pin(inst_to_sym[parts[0]], parts[1])
            elif isinstance(val, dict):
                if "x" in val and "y" in val:
                    return int(val["x"]), int(val["y"])
                inst_v = val.get("inst") or val.get("sym") or val.get("symbol")
                pin_v = val.get("pin")
                if inst_v and pin_v and str(inst_v) in inst_to_sym:
                    return sch.get_pin(inst_to_sym[str(inst_v)], str(pin_v))
            raise ValueError(f"Bağlantı noktası çözülemedi: {val!r}")

        connections = data.get("connections", [])
        for conn in connections:
            if not isinstance(conn, dict):
                continue
            if "from_inst" in conn and "to_inst" in conn:
                # Bilinmeyen instance "from"/"to" biçimindeki gibi ValueError verir (sessizce atlanıp kopuk kalmaz)
                p1 = _resolve_pt({"inst": conn["from_inst"], "pin": conn.get("from_pin", "1")})
                p2 = _resolve_pt({"inst": conn["to_inst"], "pin": conn.get("to_pin", "1")})
                sch.connect(p1, p2)
            elif "from" in conn and "to" in conn:
                p1 = _resolve_pt(conn["from"])
                p2 = _resolve_pt(conn["to"])
                sch.connect(p1, p2)

        # 4. Topraklar (grounds)
        grounds = data.get("grounds", [])
        for g in grounds:
            if isinstance(g, (list, tuple)) and len(g) == 2:
                sch.add_ground(int(g[0]), int(g[1]))
            elif isinstance(g, dict) and "x" in g and "y" in g:
                sch.add_ground(int(g["x"]), int(g["y"]))

        # 5. Etiketler (flags / net_labels)
        labels = data.get("flags") or data.get("net_labels") or data.get("labels") or []
        for fl in labels:
            if isinstance(fl, dict) and "name" in fl:
                sch.add_net_label(int(fl.get("x", 0)), int(fl.get("y", 0)), str(fl["name"]))
            elif isinstance(fl, (list, tuple)) and len(fl) >= 3:
                sch.add_net_label(int(fl[0]), int(fl[1]), str(fl[2]))

        # 6. Direktifler (directives)
        directives = data.get("directives", [])
        for i, d in enumerate(directives):
            if isinstance(d, str):
                sch.add_directive(100, 400 + i * 40, d)
            elif isinstance(d, dict) and "text" in d:
                sch.add_directive(int(d.get("x", 100)), int(d.get("y", 400 + i * 40)), str(d["text"]))

        # 7. Açıklamalar (comments)
        comments = data.get("comments", [])
        for i, c in enumerate(comments):
            if isinstance(c, str):
                sch.add_comment(100, 600 + i * 40, c)
            elif isinstance(c, dict) and "text" in c:
                sch.add_comment(int(c.get("x", 100)), int(c.get("y", 600 + i * 40)), str(c["text"]))

        # 8. Metinler (texts)
        texts = data.get("texts", [])
        for t in texts:
            if isinstance(t, dict) and "text" in t:
                sch.texts.append(
                    SchematicText(
                        x=int(t.get("x", 0)),
                        y=int(t.get("y", 0)),
                        text=str(t["text"]),
                        is_directive=bool(t.get("is_directive", True)),
                        alignment=str(t.get("alignment", "Left")),
                        size=int(t.get("size", 2)),
                    )
                )

        return sch



_ASC_PARAM_RE = re.compile(
    r"^(TEXT\s+[-0-9]+\s+[-0-9]+\s+\w+\s+\d+\s+!\.param\b)(.*)$",
    re.IGNORECASE | re.MULTILINE,
)


def is_asc_content(text: str) -> bool:
    """Verilen metnin bir LTspice .asc şema dosyası olup olmadığını tespit eder."""
    stripped = text.strip()
    return stripped.startswith("Version ") and "SHEET " in stripped
