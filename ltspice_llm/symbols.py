"""
LTspice Symbol (.asy) Pins
~~~~~~~~~~~~~~~~~~~~~~~~~~
`AscSchematic.get_pin` yalnızca `SYMBOL_PIN_DEFS` tablosundaki 24 standart sembolü bilir. ADI kütüphanesindeki
sembollerin (LTspice 26.1.1 ``lib/sym``: 6642 dosya) pinleri burada gerektiğinde ``.asy`` dosyasından okunur.

Biçim (ADI SCHEMATIC-REFERENCE "Pin Properties"; ``lib/sym`` dosyalarıyla doğrulandı): her pin bir
``PIN x y YÖN ofset`` satırı ve onu izleyen ``PINATTR PinName <ad>`` / ``PINATTR SpiceOrder <n>`` satırlarıdır.
SpiceOrder pinin netlist satırındaki sırasıdır ve dosya sırasından farklı olabilir (LT8610: PGND 13, FB 6).
Ölçüm (examples/16): 6642 sembolün hepsinde her PIN'in SpiceOrder'ı var, 7'sinde bazı pinlerin PinName'i yok;
126'sı BOM'suz UTF-16LE, gerisi cp1252 (`.asc` ile aynı kurallar). Bütün kütüphaneyi ayrıştırmak 1,5-2,7 sn,
yalnızca dosya adlarını listelemek 0,05 sn sürüyor: dizin bir kez listelenir, sembol gerektiğinde okunur; ayrı bir
JSON önbelleği gerekmez.
"""

from __future__ import annotations

import functools
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union

from .schematic import read_asc_file

PathLike = Union[str, Path]


@dataclass(frozen=True)
class SymbolPin:
    """Bir sembol pini: yerel koordinat, ad (yoksa None) ve netlist sırası."""
    name: Optional[str]
    x: int
    y: int
    spice_order: int


@dataclass(frozen=True)
class SymbolDefinition:
    """Bir ``.asy`` sembolü. ``pins`` netlist (SpiceOrder) sırasıyladır."""
    name: str
    path: Path
    prefix: str
    description: str
    pins: Tuple[SymbolPin, ...]

    @property
    def spice_order(self) -> List[str]:
        """Pin adları netlist sırasıyla; adı olmayan pin SpiceOrder numarasıyla yazılır."""
        return [p.name if p.name is not None else str(p.spice_order) for p in self.pins]

    def pin(self, key: str) -> Tuple[int, int]:
        """
        Pinin yerel (x, y) koordinatı. ``key``: PinName (büyük/küçük harf duyarsız) ya da SpiceOrder numarası
        (``"1"``, ``"2"``...). Adı olmayan pinler yalnızca numarayla bulunur.
        """
        wanted = str(key).strip()
        for p in self.pins:
            if p.name is not None and p.name.lower() == wanted.lower():
                return p.x, p.y
        if wanted.isdigit():
            for p in self.pins:
                if p.spice_order == int(wanted):
                    return p.x, p.y
        raise KeyError(f"'{self.name}' sembolünde '{key}' pini yok. Pinler (netlist sırasıyla): {self.spice_order}")


def read_symbol(path: PathLike) -> SymbolDefinition:
    """Bir ``.asy`` dosyasını okur (cp1252 ya da UTF-16LE; bkz. `schematic.decode_asc_bytes`)."""
    p = Path(path)
    text, _ = read_asc_file(p)
    raw: List[Dict[str, object]] = []
    prefix = description = ""
    for line in text.splitlines():
        head, _, rest = line.strip().partition(" ")
        if head == "PIN":
            fields = rest.split()
            raw.append({"x": int(fields[0]), "y": int(fields[1]), "name": None, "order": None})
        elif head == "PINATTR" and raw:
            key, _, value = rest.partition(" ")
            if key == "PinName":
                raw[-1]["name"] = value.strip()
            elif key == "SpiceOrder":
                raw[-1]["order"] = int(value)
        elif head == "SYMATTR":
            key, _, value = rest.partition(" ")
            if key == "Prefix":
                prefix = value.strip()
            elif key == "Description":
                description = value.strip()
    pins = [
        SymbolPin(r["name"], r["x"], r["y"], r["order"] if r["order"] is not None else i + 1)  # type: ignore[arg-type]
        for i, r in enumerate(raw)
    ]
    pins.sort(key=lambda pin: pin.spice_order)
    return SymbolDefinition(p.with_suffix("").name if p.suffix.lower() == ".asy" else p.name, p, prefix, description,
                            tuple(pins))


def library_symbol_dir() -> Optional[Path]:
    """ADI'nin sembol kütüphanesi (``<LTspice veri klasörü>/lib/sym``); yoksa None."""
    from .reference import ltspice_data_dir

    data = ltspice_data_dir()
    sym = data / "lib" / "sym" if data else None
    return sym if sym is not None and sym.is_dir() else None


def user_symbol_dir() -> Optional[Path]:
    """LTspice'ın kullanıcı dosyaları klasörü (``LTspice.ini`` UserCmpDir; bkz. `reference.ltspice_user_dir`)."""
    from .reference import ltspice_user_dir

    return ltspice_user_dir()


def _user_files_lookup(folder: Path, key: str) -> Optional[Path]:
    """
    User Files klasöründe LTspice gibi arar. Ölçüm (LTspice 26.1.1 ``-netlist``, ``-ini`` ile geçici UserCmpDir): yalın
    ad yalnızca klasörün kendisinde bulunuyor; alt klasördeki sembol (``sub``, ``sym``, ``lib/sym``) yalnızca göreli
    yolla (``sub\\\\X``). Klasör listelenmez, yalnızca istenen yerdeki dosyaya bakılır (ad büyük/küçük harf duyarsız).
    """
    parts = key.split("/")
    parent = folder.joinpath(*parts[:-1])
    wanted = f"{parts[-1]}.asy"
    try:
        with os.scandir(parent) as entries:
            for entry in entries:
                if entry.name.lower() == wanted and entry.is_file():
                    return Path(entry.path)
    except OSError:
        return None
    return None


def _normalize(name: str) -> str:
    """``PowerProducts\\\\LT8610`` (.asc'deki yazım), ``PowerProducts\\LT8610``, ``powerproducts/lt8610`` -> aynı anahtar."""
    key = re.sub(r"[\\/]+", "/", name.strip()).strip("/").lower()
    return key[:-4] if key.endswith(".asy") else key


def _scan(folder: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    """({göreli yol: dosya}, {yalın ad: dosya}) — yalın adda ilk bulunan (sıralı) kazanır."""
    by_path: Dict[str, str] = {}
    by_name: Dict[str, str] = {}
    root = Path(folder)
    for path in sorted(root.rglob("*.asy")):
        key = _normalize(path.relative_to(root).as_posix())
        by_path[key] = str(path)
        by_name.setdefault(key.rsplit("/", 1)[-1], str(path))
    return by_path, by_name


#: ADI kütüphanesi LTspice güncellenene kadar değişmez: süreç başına bir kez listelenir (0,05 sn). Kullanıcı
#: dizinleri her aramada listelenir (oturum içinde sembol eklenebilir).
_library_index = functools.lru_cache(maxsize=None)(_scan)


def find_symbol(name: str, search_dirs: Optional[Sequence[PathLike]] = None) -> Optional[Path]:
    """
    Sembol adını ``.asy`` dosyasına çevirir; bulunamazsa None.

    Dizinler sırayla aranır, sonra LTspice'ın kullanıcı klasörü (`user_symbol_dir`) ve ADI kütüphanesi (ADI
    SCHEMATIC-REFERENCE "Default Search Priority": şemanın klasörü, kullanıcı yolları, User Files, ADI kütüphanesi).
    Verilen dizinlerde ve kütüphanede önce göreli yol (``PowerProducts\\\\LT8610``), yoksa herhangi bir alt klasördeki
    yalın ad (``LT8610``) eşleşir; dizin ilk kullanımda bir kez listelenir. Kullanıcı klasöründe yalın ad yalnızca
    klasörün kendisinde aranır (`_user_files_lookup`).
    """
    key = _normalize(name)
    if not key:
        return None
    base = key.rsplit("/", 1)[-1]
    for folder in (Path(d) for d in (search_dirs or ())):
        hit = _indexed_lookup(folder, _scan, key, base)
        if hit is not None:
            return hit
    user = user_symbol_dir()
    if user is not None:
        hit = _user_files_lookup(user, key)
        if hit is not None:
            return hit
    library = library_symbol_dir()
    return _indexed_lookup(library, _library_index, key, base) if library is not None else None


def _indexed_lookup(folder: Path, index: Callable[[str], Tuple[Dict[str, str], Dict[str, str]]], key: str,
                    base: str) -> Optional[Path]:
    if not folder.is_dir():
        return None
    by_path, by_name = index(os.path.normcase(str(folder.resolve())))
    hit = by_path.get(key) or by_name.get(base)
    return Path(hit) if hit else None


@functools.lru_cache(maxsize=None)
def _read_cached(path: str, mtime_ns: int) -> SymbolDefinition:
    return read_symbol(path)


def load_symbol(name: str, search_dirs: Optional[Sequence[PathLike]] = None) -> Optional[SymbolDefinition]:
    """`find_symbol` + `read_symbol` (dosya değişmedikçe önbellekten); bulunamazsa None."""
    path = find_symbol(name, search_dirs)
    if path is None:
        return None
    return _read_cached(str(path), path.stat().st_mtime_ns)
