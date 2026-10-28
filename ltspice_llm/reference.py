"""
Local LTspice Installation Knowledge
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
LTspice 26 kurulumuyla gelen iki bilgi kaynağını LLM ajanına açar:

1. **ADI LTspice AI Reference** (``%LOCALAPPDATA%\\LTspice\\reference\\*.md``): Analog Devices'ın yapay zekâ
   asistanları için yazdığı resmî başvuru belgeleri. `ReferenceDocs` belgeleri başlıklara göre bölümlere ayırır
   ve anahtar kelimeyle arar. Belgeler projeye kopyalanmaz; kurulumdan okunur ve LTspice güncellemesiyle
   kendiliğinden yenilenir. ADI: "eğitim verisiyle yerel belge çelişirse yerel belgeye güvenin".
2. **Model kütüphanesi** (``lib/cmp``, ``lib/sub``): `find_model_definitions` bir ``.model``/``.subckt`` adının
   hangi kütüphane dosyasında olduğunu bulur. Elle yazılan netlistlerde standart modeller otomatik yüklenmez
   (ölçüm: ``D1 b 0 1N4148`` -> ``Undefined model "1n4148"``; ``.lib standard.dio`` eklenince çalışıyor).

Belgelerdeki iddiaların LTspice 26.1.1 üzerindeki doğrulaması: ``docs/LESSONS_LEARNED_LTSPICE_REFERENCE.md``.
"""

from __future__ import annotations

import ntpath
import os
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Union

#: LTspice veri klasörünü (``reference``, ``lib``, ``examples`` içeren) açıkça belirtir
ENV_DATA_DIR = "LTSPICE_DATA_DIR"
#: ADI referans belgelerinin klasörünü açıkça belirtir (öncelikli)
ENV_REFERENCE_DIR = "LTSPICE_REFERENCE_DIR"


def ltspice_data_dir() -> Optional[Path]:
    """
    LTspice'ın kütüphane, örnek ve referans dosyalarını tuttuğu klasör (yoksa None).

    Sırayla: ``LTSPICE_DATA_DIR``; Windows'ta ``%LOCALAPPDATA%\\LTspice``; macOS'ta
    ``~/Library/Application Support/LTspice``. Bu klasör LTspice güncellemesinde yeniden yazılır: içine dosya
    koymayın, kullanıcı modelleri ``Documents\\LTspice`` altında durur.
    """
    candidates: List[Path] = []
    env = os.environ.get(ENV_DATA_DIR)
    if env:
        candidates.append(Path(env).expanduser())
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "LTspice")
    if sys.platform == "darwin":
        candidates.append(Path.home() / "Library" / "Application Support" / "LTspice")
    for cand in candidates:
        if cand.is_dir():
            return cand
    return None


def ltspice_user_dir() -> Optional[Path]:
    """
    LTspice'ın kullanıcı dosyaları (User Files) klasörü: kendi sembol ve modellerinizin yeri, güncellemede silinmez
    (ADI SCHEMATIC-REFERENCE "Default Search Priority" 3, varsayılan ``Documents\\LTspice``). Değer LTspice'ın ayar
    dosyasındaki ``UserCmpDir``'dir (``%APPDATA%\\LTspice.ini``; 26.1.1: BOM'lu UTF-16LE). Belgeler klasörü
    yönlendirilmiş olabilir (ör. OneDrive), bu yüzden ``~/Documents`` tahmin edilmez. Ayar ya da klasör yoksa None.
    """
    appdata = os.environ.get("APPDATA")
    ini = Path(appdata) / "LTspice.ini" if appdata else None
    if ini is None or not ini.is_file():
        return None
    from .schematic import decode_asc_bytes

    try:
        text, _ = decode_asc_bytes(ini.read_bytes())
    except OSError:
        return None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "UserCmpDir" and value.strip():
            # Ayar dosyası Windows biçiminde (%VAR%). os.path Linux'ta posixpath olur ve %VAR%'ı açmaz (CI, 2026-10-11).
            folder = Path(ntpath.expandvars(value.strip()))
            return folder if folder.is_dir() else None
    return None


def reference_dir() -> Optional[Path]:
    """ADI LTspice AI Reference belgelerinin klasörü (yoksa None)."""
    env = os.environ.get(ENV_REFERENCE_DIR)
    if env:
        p = Path(env).expanduser()
        return p if p.is_dir() else None
    data = ltspice_data_dir()
    if data and (data / "reference").is_dir():
        return data / "reference"
    return None


# --------------------------------------------------------------------------- ADI referans belgeleri
@dataclass
class ReferenceSection:
    """Bir referans belgesinin başlığıyla birlikte tek bölümü."""
    document: str
    heading: str
    text: str

    def to_dict(self, max_chars: int = 2500) -> Dict[str, str]:
        text = self.text if len(self.text) <= max_chars else self.text[:max_chars].rstrip() + "\n[...kısaltıldı]"
        return {"document": self.document, "section": self.heading, "excerpt": text}


_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
_WORD_RE = re.compile(r"[\w.+-]+", re.UNICODE)
# Arayüze özgü (klavye, menü) belgeler arama sonucunu doldurmasın diye düşük ağırlık alır
_LOW_PRIORITY_DOCS = {"LTSPICE-KEYBOARD-SHORTCUTS.md", "LTSPICE-MENU-REFERENCE.md"}


class ReferenceDocs:
    """
    ADI LTspice AI Reference belgelerinde başlık temelli arama.

    >>> docs = ReferenceDocs()          # kurulumdaki belgeler (ya da ReferenceDocs("yol"))
    >>> docs.search(".step only one step")[0].heading
    '.STEP Rejected Before Simulating'
    """

    def __init__(self, directory: Optional[Union[str, Path]] = None):
        self.directory = Path(directory) if directory else reference_dir()
        self._sections: Optional[List[ReferenceSection]] = None

    @property
    def available(self) -> bool:
        return bool(self.directory and self.directory.is_dir() and any(self.directory.glob("*.md")))

    def documents(self) -> List[str]:
        """Belge dosya adları."""
        if not self.available:
            return []
        return sorted(p.name for p in self.directory.glob("*.md"))  # type: ignore[union-attr]

    def sections(self) -> List[ReferenceSection]:
        """Tüm belgelerin başlıklara göre bölünmüş hali (ilk çağrıda okunur)."""
        if self._sections is None:
            self._sections = []
            if self.available:
                for path in sorted(self.directory.glob("*.md")):  # type: ignore[union-attr]
                    self._sections.extend(self._split(path.name, path.read_text(encoding="utf-8", errors="replace")))
        return self._sections

    @staticmethod
    def _split(document: str, text: str) -> List[ReferenceSection]:
        # YAML ön bilgisi ve "<img>" satırları (araç çubuğu simgeleri) bağlam değil, gürültü
        text = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.DOTALL)
        sections: List[ReferenceSection] = []
        buf: List[str] = []
        heading, in_code = document, False
        for line in text.splitlines():
            if line.strip().startswith("```"):
                in_code = not in_code
            match = None if in_code else _HEADING_RE.match(line)
            if match and len(match.group(1)) >= 2:
                if "".join(buf).strip():
                    sections.append(ReferenceSection(document, heading, "\n".join(buf).strip()))
                heading, buf = match.group(2), [line]
                continue
            if "<img " not in line:
                buf.append(line)
        if "".join(buf).strip():
            sections.append(ReferenceSection(document, heading, "\n".join(buf).strip()))
        return sections

    def search(self, query: str, max_results: int = 3) -> List[ReferenceSection]:
        """
        Sorgu sözcüklerini en çok içeren bölümler (başlıktaki eşleşme 4 kat sayılır).

        Sözcükler büyük/küçük harf duyarsızdır; ``.step``, ``meascplxfmt``, ``-netlist`` gibi noktalı/tireli
        belirteçler tek sözcük sayılır.
        """
        terms = [t.lower() for t in _WORD_RE.findall(query or "") if len(t) > 1]
        if not terms:
            return []
        scored = []
        for order, sec in enumerate(self.sections()):
            head, body = sec.heading.lower(), sec.text.lower()
            if head == "table of contents":  # yalnızca bağlantı listesi
                continue
            hits = [t for t in terms if t in body or t in head]
            if not hits:
                continue
            score = float(sum(4 * head.count(t) + min(body.count(t), 10) for t in terms))
            score *= len(hits) / len(terms)  # tüm sözcükleri içeren bölüm öne çıkar
            if sec.document in _LOW_PRIORITY_DOCS:
                score *= 0.3
            scored.append((-score, order, sec))
        scored.sort(key=lambda item: (item[0], item[1]))
        return [sec for _, _, sec in scored[: max(1, int(max_results))]]


# --------------------------------------------------------------------------- model kütüphanesi indeksi
_DEF_RE = re.compile(rb"^[ \t]*\.(subckt|model)[ \t]+([^\s(]+)", re.IGNORECASE | re.MULTILINE)
_INDEX_LOCK = threading.Lock()
_INDEX_CACHE: Dict[str, Dict[str, List[Dict[str, str]]]] = {}


def _decode_library_bytes(data: bytes) -> bytes:
    """``standard.mos`` gibi BOM'suz UTF-16LE kütüphaneleri taranabilir baytlara çevirir."""
    if data[:200].count(b"\x00") > 20:
        try:
            return data.decode("utf-16-le", errors="ignore").encode("utf-8", errors="ignore")
        except Exception:
            return data
    return data


def _library_directive(lib_root: Path, path: Path) -> str:
    rel = path.relative_to(lib_root)
    return f".lib {rel.as_posix() if len(rel.parts) > 1 else rel.name}"


def _build_index(lib_dir: Path) -> Dict[str, List[Dict[str, str]]]:
    """``lib/cmp`` ve ``lib/sub`` altındaki tanımların (ad -> kayıtlar) indeksi. 61 MB / 5000 dosya ~1,1 sn."""
    index: Dict[str, List[Dict[str, str]]] = {}

    def add(name: str, entry: Dict[str, str]) -> None:
        entries = index.setdefault(name.lower(), [])
        if entry not in entries:
            entries.append(entry)

    for sub in ("cmp", "sub"):
        root = lib_dir / sub
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            directive = _library_directive(root, path)
            rel = f"{sub}/{path.relative_to(root).as_posix()}"
            # Şifreli (ikili) kütüphaneler: yalnızca dosya adı bilinir (LT8610.sub -> LT8610)
            add(path.stem, {"name": path.stem, "kind": "file", "file": rel, "directive": directive})
            try:
                data = _decode_library_bytes(path.read_bytes())
            except OSError:
                continue
            for m in _DEF_RE.finditer(data):
                name = m.group(2).decode("latin-1")
                add(name, {"name": name, "kind": m.group(1).decode().lower(), "file": rel, "directive": directive})
    return index


def find_model_definitions(name: str, lib_dir: Optional[Union[str, Path]] = None) -> List[Dict[str, str]]:
    """
    Bir ``.model``/``.subckt`` adının LTspice kütüphanesindeki tanımları; her biri
    ``{"name", "kind": "model"|"subckt"|"file", "file": "sub/LTC.lib", "directive": ".lib LTC.lib"}``.

    ``directive`` netliste eklenecek satırdır: LTspice göreli ``.lib`` yollarını ``lib/cmp`` ve ``lib/sub``
    içinde arar (ölçüm: ``X1 ... LT1001`` + ``.lib LTC.lib`` çalışıyor). İndeks ilk çağrıda kurulur ve süreç
    boyunca saklanır. LTspice veri klasörü yoksa boş liste döner.
    """
    if not name:
        return []
    data = ltspice_data_dir()
    base = Path(lib_dir) if lib_dir else (data / "lib" if data else None)
    if base is None or not base.is_dir():
        return []
    key = os.path.normcase(str(base.resolve()))
    with _INDEX_LOCK:
        if key not in _INDEX_CACHE:
            _INDEX_CACHE[key] = _build_index(base)
        index = _INDEX_CACHE[key]
    entries = index.get(str(name).lower(), [])
    # Tanım satırı (model/subckt) olan kayıtlar dosya adı eşleşmesinden önce gelir
    return sorted(entries, key=lambda e: e["kind"] == "file")
