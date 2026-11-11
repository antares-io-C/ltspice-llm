"""
Circuit Comparison
~~~~~~~~~~~~~~~~~~
İki devrenin netlist farkı ve ``.meas`` sonuçlarının önce/sonra karşılaştırması. LTspice gerektirmez.

Bir ölçüm farkı ancak iki devre arasında tek değişken değiştiyse o değişkene atfedilebilir (GEMINI.md "Simülasyon
doğrulama kuralları": aynı düğümü ölç, bir seferde tek değişken değiştir). `compare_netlists` değişen değişkenleri
sayar ve ölçüm tanımı değişikliklerini ayrı tutar; `compare_measurements` ortak ölçümlerin farkını verir.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from itertools import zip_longest
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .adapter import _PARAM_PAIR_RE, strip_auto_directives
from .netlist import _logical_lines
from .quantizer import parse_spice_value

#: Netlistte tek olması gereken analiz komutları; iki devrede aynı yuvada karşılaştırılır.
_ANALYSIS_HEADS = frozenset({".tran", ".ac", ".dc", ".op", ".noise", ".tf"})
_MEAS_HEADS = frozenset({".meas", ".measure"})
#: ``.meas`` adından önce gelebilen analiz türü (``.meas tran v_max MAX V(out)``)
_MEAS_ANALYSES = frozenset({"ac", "dc", "op", "tran", "tf", "noise"})
#: Devreyi değil yalnız kaydedilen çıktıyı etkileyen direktifler: fark sayılmaz
_OUTPUT_ONLY_HEADS = frozenset({".save", ".backanno"})
_OPTIONS_HEAD_RE = re.compile(r"^\.opt(?:ion)?s?$")
#: Değer alanı sayı olarak karşılaştırılan iki uçlu pasifler (``R1 a b 1k``): ``1k`` ile ``1000`` aynı değerdir
_VALUE_FIELD_KINDS = "RCL"

_Loose = List[Tuple[Tuple[str, ...], str]]


@dataclass(frozen=True)
class _Item:
    """Netlistin adıyla eşleştirilen bir parçası (eleman, parametre, seçenek, analiz, model, ölçüm)."""
    kind: str
    name: str
    text: str
    fields: Tuple[str, ...]
    value_index: Optional[int] = None  # fields içinde sayı olarak karşılaştırılan alan


def _fold(tokens: Sequence[str]) -> Tuple[str, ...]:
    """ADI README "Node Naming: Names are case-insensitive"; sonekler de öyle (``1K`` = ``1k``)."""
    return tuple(token.casefold() for token in tokens)


def _same_number(a: str, b: str) -> bool:
    try:
        return math.isclose(parse_spice_value(a), parse_spice_value(b), rel_tol=1e-12, abs_tol=0.0)
    except (TypeError, ValueError):
        return False


def _same(a: _Item, b: _Item) -> bool:
    if a.fields == b.fields:
        return True
    index = a.value_index
    if index is None or index != b.value_index or len(a.fields) != len(b.fields):
        return False
    rest_equal = a.fields[:index] + a.fields[index + 1:] == b.fields[:index] + b.fields[index + 1:]
    return rest_equal and _same_number(a.fields[index], b.fields[index])


def _parse(content: str) -> Tuple[Dict[Tuple[str, str], _Item], Dict[str, _Loose]]:
    """
    (adıyla eşleşen parçalar, adı olmayan direktifler). Yalnız LTspice'ın okuduğu satırlar: başlık, yorumlar ve ilk
    ``.end`` sonrası atılır, ``+`` devam satırları birleşir (`netlist._logical_lines`); adaptörün kendi eklediği
    ``.options threads`` satırı devrenin parçası değildir. Alt devre içindeki ad ``ALTDEVRE:R1`` olur.
    """
    keyed: Dict[Tuple[str, str], _Item] = {}
    loose: Dict[str, _Loose] = {}
    scope = ""
    for line in _logical_lines(strip_auto_directives(content)):
        tokens = line.split()
        head = tokens[0].casefold()
        text = " ".join(tokens)
        if not head.startswith("."):
            name = scope + tokens[0]
            value_index = 2 if tokens[0][0].upper() in _VALUE_FIELD_KINDS and len(tokens) > 3 else None
            keyed[("element", name.casefold())] = _Item("element", name, text, _fold(tokens[1:]), value_index)
        elif head == ".subckt":
            loose.setdefault(head, []).append((_fold(tokens), text))
            scope = (tokens[1] if len(tokens) > 1 else "") + ":"
        elif head == ".ends":
            scope = ""
        elif head == ".param":
            pairs = [(m.group("name"), m.group("val")) for m in _PARAM_PAIR_RE.finditer(line[len(tokens[0]):])]
            if not pairs and len(tokens) >= 3:  # eski sözdizimi: ".param AD DEĞER"
                pairs = [(tokens[1], tokens[2])]
            for param, value in pairs:
                name = scope + param
                keyed[("param", name.casefold())] = _Item("param", name, f".param {param}={value}",
                                                          (value.casefold(),), 0)
        elif _OPTIONS_HEAD_RE.match(head):
            for token in re.sub(r"\s*=\s*", "=", " ".join(tokens[1:])).split():
                option, has_value, value = token.partition("=")
                keyed[("option", option.casefold())] = _Item("option", option, f".options {token}",
                                                             (value.casefold(),), 0 if has_value else None)
        elif head in _ANALYSIS_HEADS:
            keyed[("analysis", "")] = _Item("analysis", tokens[0], text, _fold(tokens))
        elif head in _MEAS_HEADS:
            rest = tokens[1:]
            if len(rest) > 1 and rest[0].casefold() in _MEAS_ANALYSES:
                rest = rest[1:]
            name = rest[0] if rest else ""
            keyed[("measurement", name.casefold())] = _Item("measurement", name, text, _fold(rest[1:]))
        elif head == ".model":
            name = scope + (tokens[1] if len(tokens) > 1 else "")
            keyed[("model", name.casefold())] = _Item("model", name, text, _fold(tokens[2:]))
        elif head not in _OUTPUT_ONLY_HEADS:
            loose.setdefault(scope + head, []).append((_fold(tokens), text))
    return keyed, loose


def _measurement_names(netlist: str) -> List[str]:
    """Netlistte ``.meas`` ile tanımlı ölçüm adları (tanım sırasıyla)."""
    keyed, _ = _parse(netlist)
    return [item.name for item in keyed.values() if item.kind == "measurement"]


def _unmatched(lines: _Loose, other: _Loose) -> List[str]:
    """``lines`` içinde ``other``'da karşılığı olmayan satırlar (aynı satır iki kez varsa iki kez sayılır)."""
    remaining = Counter(fields for fields, _ in other)
    left: List[str] = []
    for fields, text in lines:
        if remaining[fields] > 0:
            remaining[fields] -= 1
        else:
            left.append(text)
    return left


def _entry(kind: str, name: str, a: Optional[str], b: Optional[str]) -> Dict[str, Any]:
    return {"kind": kind, "name": name, "a": a, "b": b}


def compare_netlists(netlist_a: str, netlist_b: str) -> Dict[str, Any]:
    """
    İki netlist arasında LTspice'ın gördüğü farklar.

    Dönen sözlük:

    - ``changes``: değişen değişkenler, ``{"kind", "name", "a", "b"}``; yalnız bir devrede olanın öbür tarafı ``None``.
      ``kind``: ``element``, ``param``, ``option`` (``.options`` içindeki tek seçenek), ``analysis``, ``model``,
      ``directive`` (``.temp``, ``.ic``, ``.step``, ``.lib`` gibi adı olmayan direktifler).
    - ``changed_variable_count`` ve ``single_variable`` (tam olarak bir değişken değiştiyse True).
    - ``measurement_changes``: ``.meas`` tanımı farkları. Devreyi değiştirmedikleri için değişken sayılmazlar ama
      tanımı farklı bir ölçümün iki değeri aynı şeyi ölçmez.

    Fark sayılmayanlar: başlık, yorum, boşluk, satır sırası, büyük/küçük harf, ``.end`` sonrası, ``.save`` ve
    ``.backanno``. R, C ve L'nin değer alanı ile ``.param`` ve ``.options`` değerleri sayı olarak karşılaştırılır
    (``1k`` = ``1000``; ``1M`` mili, ``1Meg`` megadır); öbür alanlar metin olarak karşılaştırılır.
    """
    keyed_a, loose_a = _parse(netlist_a)
    keyed_b, loose_b = _parse(netlist_b)
    changes: List[Dict[str, Any]] = []
    measurement_changes: List[Dict[str, Any]] = []
    for key in list(keyed_a) + [k for k in keyed_b if k not in keyed_a]:
        item_a, item_b = keyed_a.get(key), keyed_b.get(key)
        if item_a is not None and item_b is not None and _same(item_a, item_b):
            continue
        shown = item_a if item_a is not None else keyed_b[key]
        entry = _entry(shown.kind, shown.name, item_a.text if item_a else None, item_b.text if item_b else None)
        (measurement_changes if shown.kind == "measurement" else changes).append(entry)
    for head in list(loose_a) + [h for h in loose_b if h not in loose_a]:
        left_a = _unmatched(loose_a.get(head, []), loose_b.get(head, []))
        left_b = _unmatched(loose_b.get(head, []), loose_a.get(head, []))
        changes += [_entry("directive", head, a, b) for a, b in zip_longest(left_a, left_b)]
    return {
        "changes": changes,
        "changed_variable_count": len(changes),
        "single_variable": len(changes) == 1,
        "measurement_changes": measurement_changes,
    }


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _finite_or_none(value: float) -> Optional[float]:
    return value if math.isfinite(value) else None


def compare_measurements(
    measurements_a: Mapping[str, Any],
    measurements_b: Mapping[str, Any],
    names: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """
    İki koşumun ortak ``.meas`` sonuçlarının farkı (değerler yuvarlanmaz).

    Her satır ``{"name", "a", "b", "delta", "relative_change_percent"}``: ``delta = b - a``,
    ``relative_change_percent = (b - a) / |a| * 100``. ``a`` sıfırsa yüzde tanımsızdır (``None``); sayı olmayan ya da
    sonlu olmayan değerlerde ``delta`` da ``None`` olur. ``names`` verilirse yalnız o ölçümler karşılaştırılır (ad
    büyük/küçük harfe duyarsız); ortak olmayanlar ``not_found``'da döner. ``only_in_a`` / ``only_in_b``: yalnız bir
    koşumda bulunan ölçümler.
    """
    common = [name for name in measurements_a if name in measurements_b]
    not_found: List[str] = []
    if names:
        wanted = [str(name).strip() for name in names]
        folded = {name.casefold() for name in wanted}
        common = [name for name in common if name.casefold() in folded]
        present = {name.casefold() for name in common}
        not_found = [name for name in wanted if name.casefold() not in present]
    rows: List[Dict[str, Any]] = []
    for name in common:
        a, b = measurements_a[name], measurements_b[name]
        delta: Optional[float] = None
        percent: Optional[float] = None
        if _is_number(a) and _is_number(b):
            delta = _finite_or_none(float(b) - float(a))
            if delta is not None and a != 0:
                percent = _finite_or_none(delta / abs(float(a)) * 100.0)
        rows.append({"name": name, "a": a, "b": b, "delta": delta, "relative_change_percent": percent})
    return {
        "measurements": rows,
        "only_in_a": [name for name in measurements_a if name not in measurements_b],
        "only_in_b": [name for name in measurements_b if name not in measurements_a],
        "not_found": not_found,
    }
