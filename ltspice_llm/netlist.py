"""
Netlist Text Helpers
~~~~~~~~~~~~~~~~~~~~
Adaptör ve teşhis motorunun ortak kullandığı, LTspice gerektirmeyen saf netlist metni yardımcıları.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Set, Tuple

# ".end" veya ".end ; yorum" satırı (".ends" eşleşmez; CRLF metindeki ".end\r" eşleşir)
END_DIRECTIVE_RE = re.compile(r"^[ \t]*\.end[ \t\r]*(?:;.*)?$", re.IGNORECASE | re.MULTILINE)
# Yalnızca satır başındaki gerçek analiz direktifleri (".options", yorum satırı vb. sayılmaz)
ANALYSIS_DIRECTIVE_RE = re.compile(
    r"^[ \t]*\.(?:tran|ac|dc|op|noise|four|tf)\b", re.IGNORECASE | re.MULTILINE
)
# ".step" direktifi (parametre, sıcaklık, Monte Carlo taramaları)
STEP_DIRECTIVE_RE = re.compile(r"^[ \t]*\.step\b", re.IGNORECASE | re.MULTILINE)


def _first_end(content: str):
    """
    LTspice'ın netlisti bitirdiği ``.end`` satırı. İlk satır her zaman başlıktır (ADI SPICE-SYNTAX-REFERENCE
    "Netlist Structure"; ölçüm: başlığı ``.end`` olan netlist sonuna kadar okunur), bu yüzden atlanır.
    """
    first_eol = content.find("\n")
    return None if first_eol < 0 else END_DIRECTIVE_RE.search(content, first_eol + 1)


def ltspice_visible(content: str) -> str:
    """
    Netlistin LTspice'ın okuduğu kısmı: ilk ``.end``'e kadar (ADI ".END": "All lines after .end are ignored").

    Bir direktifin "var" sayılıp sayılmayacağı buna göre karar verilmeli: ``.end`` sonrasındaki
    ``.options threads=3`` kullanıcı ayarı sanılırsa adaptör kendi sınırını eklemez, LTspice de satırı okumaz.
    """
    match = _first_end(content)
    return content[: match.start()] if match else content


def insert_before_end(content: str, new_lines: Iterable[str]) -> str:
    """
    Verilen satırları netlistteki ilk ``.end`` satırından hemen önce ekler.

    LTspice ilk ``.end``'den sonrasını okumaz (ADI SIMULATION-COMMANDS-REFERENCE ".END"; ölçüm LTspice 26.1.1:
    ikinci ``.end``'den önceki ``.meas`` üretilmedi, oraya eklenen ``.options threads=2`` logda 12 göründü).
    ``.end`` satırında yorum olsa bile (``.end ; devre sonu``) doğru yeri bulur; ilk satır başlıktır, ``.end``
    olsa da sayılmaz. ``.end`` yoksa satırlar sona eklenir ve ``.end`` tamamlanır. Metnin sonundaki satır sonu
    korunur.
    """
    block = "".join(f"{line}\n" for line in new_lines)
    if not block:
        return content

    end_match = _first_end(content)
    if end_match:
        pos = end_match.start()
        prefix = content[:pos]
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        return prefix + block + content[pos:]

    if content and not content.endswith("\n"):
        content += "\n"
    return content + block + ".end\n"


# ".save" direktifi (kaydedilecek sinyal kümesini sınırlar)
SAVE_DIRECTIVE_RE = re.compile(r"^[ \t]*\.save\b", re.IGNORECASE | re.MULTILINE)
# ".meas"/".measure" satırı
_MEAS_LINE_RE = re.compile(r"^[ \t]*\.meas(?:ure)?\b", re.IGNORECASE)
# ".meas ... V(out) ... I(L1) ..." içindeki sinyal başvuruları
_SIGNAL_REF_RE = re.compile(
    r"\b(V|I|Ix|Ib|Ic|Ie|Id|Ig|Is)\s*\(\s*([^()\s,]+)\s*(?:,\s*([^()\s,]+)\s*)?\)", re.IGNORECASE
)
# Geçerli bir ".save" işleneni: V(a), V(a,b), I(L1), Ix(U1:D) ya da yalın düğüm adı
_SAVE_SIGNAL_RE = re.compile(
    r"^(?:(?:V|I|Ix|Ib|Ic|Ie|Id|Ig|Is)\([^()\s,]+(?:,[^()\s,]+)?\)|[A-Za-z_][\w:.$]*)$",
    re.IGNORECASE,
)


def validated_save_signal(signal: str) -> str:
    """
    Bir ``.save`` işlenenini doğrular ve normalleştirir (boşluklar atılır).

    Satır sonu, yorum karakteri veya direktif enjeksiyonunu engeller; ``V(out)``, ``V(a,b)``,
    ``I(L1)``, ``Ix(U1:D)`` ve yalın düğüm adları kabul edilir.
    """
    text = re.sub(r"\s+", "", str(signal))
    if not text or not _SAVE_SIGNAL_RE.match(text):
        raise ValueError(f"Geçersiz .save sinyali: {signal!r}")
    return text


def measured_signals(content: str) -> List[str]:
    """
    Netlistteki ``.meas`` satırlarında başvurulan sinyaller (``V(out)``, ``I(L1)``...), görülme sırasıyla.

    ``.save`` kümesi daraltılırken bu sinyaller saklanmazsa ölçümler üretilemez; ``run_netlist``
    bu listeyi otomatik olarak ``save_signals``'a ekler.
    """
    found: List[str] = []
    for line in content.splitlines():
        if not _MEAS_LINE_RE.match(line):
            continue
        code = line.split(";", 1)[0]
        for m in _SIGNAL_REF_RE.finditer(code):
            kind, node_a, node_b = m.group(1), m.group(2), m.group(3)
            sig = f"{kind.upper()}({node_a},{node_b})" if node_b else f"{kind.upper()}({node_a})"
            if sig.lower() not in (f.lower() for f in found):
                found.append(sig)
    return found


# --------------------------------------------------------------------------- dosya erişimi (güvenilmeyen netlist)
# ADI LTspice AI Reference'ta dosya yazan ve okuyan yapılar (tests/test_netlist_policy.py): SIMULATION-COMMANDS-REFERENCE
# ".WAVE", ".SAVEBIAS", ".INCLUDE", ".LIB", .savestate/.loadstate, .tran savestate/loadstate, ".ac/.dc/.noise/.step
# file="; CIRCUIT-ELEMENTS-REFERENCE "PWL FILE=" ve "wavefile=".
_WRITE_DIRECTIVES = {".wave", ".savebias", ".savestate"}
_READ_DIRECTIVES = {".include", ".inc", ".lib", ".loadstate"}
_STATE_OPTION_RE = re.compile(r"(?<![\w.])(?P<kind>savestate|loadstate)(?:\s*=\s*(?P<path>\"[^\"]*\"|\S+))?",
                              re.IGNORECASE)
_FILE_ARG_RE = re.compile(r"(?<![\w.])(?:wavefile|file)\s*=\s*(?P<path>\"[^\"]*\"|\S+)", re.IGNORECASE)
_ASC_TEXT_DIRECTIVE_RE = re.compile(r"^TEXT\s+[-0-9]+\s+[-0-9]+\s+\w+\s+\d+\s+!(?P<text>.*)$")
_ASC_VALUE_RE = re.compile(r"^SYMATTR\s+(?:Value2?|SpiceLine2?)\s+(?P<text>.*)$")


def _outside_workspace(path: str) -> bool:
    """
    Yol çalışma alanının (netlistin klasörü) ya da LTspice kütüphanesinin dışını gösterebilir mi? ``%VAR%``: LTspice
    26.1.1 ``.include`` yolundaki ortam değişkenini açıyor (examples/12 S3); değeri denetim anında bilinmez.
    """
    p = path.strip().strip('"').replace("\\", "/")
    return (p.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", p) is not None or ".." in p.split("/")
            or re.search(r"%[^%]+%", p) is not None)


def _code_lines(content: str) -> List[str]:
    """
    LTspice'ın okuduğu kod satırları. Netlist: başlık ve ``.end`` sonrası atılır, ``+`` birleşir, yorum silinir. ``.asc``:
    ``!`` ile başlayan TEXT direktifleri (çok satırlı metinde satırlar ``\\n`` ile ayrılır) ve SYMATTR değerleri.
    """
    if not (content.lstrip().startswith("Version ") and "SHEET " in content):
        return _logical_lines(content)
    lines: List[str] = []
    for raw in content.splitlines():
        text = _ASC_TEXT_DIRECTIVE_RE.match(raw.strip())
        if text:
            lines += [part.split(";", 1)[0].strip() for part in text.group("text").split("\\n")]
        value = _ASC_VALUE_RE.match(raw.strip())
        if value:
            lines.append(value.group("text").strip())
    return [ln for ln in lines if ln and not ln.startswith("*")]


def file_access_problems(content: str) -> List[str]:
    """
    Güvenilmeyen bir netlistin (ya da ``.asc``'nin) dosya sistemine dokunan yapıları; her biri bir açıklama, yoksa boş.

    Dosya yazan her yapı reddedilir (``.wave``, ``.savebias``, ``.savestate``, ``.tran ... savestate``): ``.wave``
    istenen yola yazar (ADI örneği ``.wave C:\\output.wav``). Okuyan yapılarda (``.include``/``.lib``/``.loadstate``,
    ``file=``, ``wavefile=``, ``loadstate=``) yalnızca çalışma alanının dışını gösterebilen yol reddedilir: mutlak yol,
    ``~``, ``..`` ya da ortam değişkeni (``%VAR%``). Kütüphane adları (``.lib standard.dio``, ``.include opamp.sub``) ve netlistin klasöründeki
    dosyalar serbesttir. LTspice'ın okumadığı kısım (başlık satırı, yorum, ilk ``.end`` sonrası) denetlenmez.
    """
    problems: List[str] = []

    def read(line: str, path: str) -> None:
        if _outside_workspace(path):
            problems.append(f"'{line}': çalışma alanı dışındaki bir dosyayı okur ({path.strip(chr(34))}).")

    for line in _code_lines(content):
        tokens = line.split()
        head = tokens[0].lower()
        if head in _WRITE_DIRECTIVES:
            problems.append(f"'{line}': dosya yazar ({head}).")
            continue
        if head in _READ_DIRECTIVES:
            if len(tokens) > 1:
                read(line, line.split(None, 1)[1].strip().split('" ', 1)[0] if tokens[1].startswith('"') else tokens[1])
            continue
        for state in _STATE_OPTION_RE.finditer(line):
            if state.group("kind").lower() == "savestate":
                problems.append(f"'{line}': dosya yazar (savestate).")
            elif state.group("path"):
                read(line, state.group("path"))
        for arg in _FILE_ARG_RE.finditer(line):
            read(line, arg.group("path"))
    return problems


# --------------------------------------------------------------------------- tek bağlantılı düğüm
# Eleman başına düğüm sayısı (ADI CIRCUIT-ELEMENTS-REFERENCE sözdizimi satırları). Q ve M'nin 4. düğümü (substrat /
# gövde) isteğe bağlıdır; o alan model adı da olabilir. E/G "value=" ve "POLY" biçiminde 2 düğümlüdür.
_NODE_COUNT = {"R": 2, "C": 2, "L": 2, "D": 2, "V": 2, "I": 2, "B": 2, "F": 2, "H": 2, "W": 2,
               "J": 3, "Z": 3, "U": 3, "Q": 3, "M": 3, "E": 4, "G": 4, "S": 4, "T": 4, "O": 4, "A": 8, "K": 0}
_OPTIONAL_FOURTH_NODE = {"Q", "M"}
# İfadedeki düğüm okumaları: V(a), V(a,b) (B kaynağı, value={...}); bağlantı sayılır, kendisi işaretlenmez
_VOLTAGE_REF_RE = re.compile(r"(?<![\w.])V\s*\(\s*([^()\s,]+)\s*(?:,\s*([^()\s,]+)\s*)?\)", re.IGNORECASE)
_GROUND_NODES = {"0", "gnd"}  # ADI SPICE-SYNTAX-REFERENCE: "GND is a special synonym for 0"


def _logical_lines(content: str) -> List[str]:
    """LTspice'ın okuduğu satırlar: başlık atılır, ``+`` devam satırları birleşir, ``;`` yorumları silinir."""
    lines: List[str] = []
    for raw in ltspice_visible(content).splitlines()[1:]:
        code = raw.split(";", 1)[0].strip()
        if not code or code.startswith("*"):
            continue
        if code.startswith("+") and lines:
            lines[-1] += " " + code[1:].strip()
        else:
            lines.append(code)
    return lines


def _element_nodes(tokens: List[str]) -> Tuple[List[str], List[str]]:
    """(kesin düğümler, belki düğüm olan alanlar). Bilinmeyen eleman: bütün alanlar "belki"."""
    kind = tokens[0][0].upper()
    fields = tokens[1:]
    if kind == "X":
        cut = next((i for i, f in enumerate(fields) if "=" in f or f.lower() == "params:"), len(fields))
        return fields[: max(cut - 1, 0)], []  # son alan alt devrenin adı
    if kind not in _NODE_COUNT:
        return [], fields
    count = _NODE_COUNT[kind]
    maybe: List[str] = []
    if kind in "EG" and len(fields) > 2:
        third = fields[2].lower()
        if third.startswith(("value", "poly")) or "=" in third or "{" in third:
            count = 2
            maybe = re.findall(r"[^\s(),]+", " ".join(fields[3:])) if third.startswith("poly") else []
    if kind in _OPTIONAL_FOURTH_NODE and len(fields) > 3:
        maybe.append(fields[3])
    return fields[:count], maybe


def single_connection_nodes(content: str) -> List[Dict[str, str]]:
    """
    Üst düzeyde tek bir eleman pinine bağlı düğümler: ``[{"node": "x", "element": "R1"}]``. LTspice bunları
    söylemiyor (ölçüm, examples/16 T6: açık uçlu direnç ve boşta op-amp girişiyle koşum başarılı, logda uyarı yok);
    çoğunlukla yazım hatasıdır (``out`` / ``vout``).

    İşaretlenmeyenler: toprak (``0``, ``GND``), ``.global`` ve ``$G_`` düğümleri, ``.subckt`` gövdeleri, ifadede
    okunan düğümler (``B1 y 0 V=V(x)``: x'i süren V1 tek pin olsa da okunuyor) ve isteğe bağlı alanlar (Q/M'nin 4.
    alanı model adı da olabilir). ``.include`` varsa denetim yapılmaz: dosya eleman ekleyebilir (``.lib`` eklemez,
    ADI SIMULATION-COMMANDS-REFERENCE ".LIB").
    """
    lines = _logical_lines(content)
    if any(re.match(r"\.inc(?:lude)?\b", line, re.IGNORECASE) for line in lines):
        return []
    definite: Dict[str, List[str]] = {}
    used: Set[str] = set()
    global_nodes: Set[str] = set()
    depth = 0
    for line in lines:
        tokens = line.split()
        head = tokens[0].lower()
        if head == ".subckt":
            depth += 1
            continue
        if head.startswith(".ends"):
            depth = max(depth - 1, 0)
            continue
        if head == ".global":
            global_nodes.update(t.lower() for t in tokens[1:])
            continue
        if depth or head.startswith("."):
            continue
        for match in _VOLTAGE_REF_RE.finditer(line):
            used.update(n.lower() for n in match.groups() if n)
        nodes, maybe = _element_nodes(tokens)
        used.update(n.lower() for n in maybe)
        for node in nodes:
            definite.setdefault(node.lower(), []).append(tokens[0])
    return [
        {"node": node, "element": elements[0]}
        for node, elements in definite.items()
        if len(elements) == 1 and node not in used and node not in _GROUND_NODES and node not in global_nodes
        and not node.startswith("$g_")
    ]
