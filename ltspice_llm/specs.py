"""
Spec Check
~~~~~~~~~~
``.meas`` sonuçlarını alt/üst sınırlarla karşılaştırır: her ölçüm için geçti/kaldı ve pay. LTspice gerektirmez.

Devrenin doğru çalışıp çalışmadığı modelin yorumuna kalmasın diye ölçüt açık yazılır, kararı bu modül verir.

- Sınırlar dahildir (Monte Carlo verimiyle aynı kural, ``tools._yield_analysis``).
- Steplenmiş koşumda her adım ayrı sınanır: spec ancak bütün adımlarda sağlanırsa geçer, en kötü adım raporlanır.
  Steplenmiş koşumun ``measurements`` alanı adımların ortalamasıdır; ortalama tek bir adımdaki ihlali gizlerdi.
- Doğrulanamayan spec geçmiş sayılmaz: ölçüm logda yoksa ya da LTspice hesaplayamadıysa (``FAIL'ed``) durum
  ``UNVERIFIED``'dır ve genel karar ``PASS`` olamaz.
- Belirsizlik bir koruma bandıdır (JCGM 106:2012, docs/references jcgm-106-2012: s.18 3.3.11 "guard band", s.33 8.3.2
  korumalı kabul, s.34 8.3.3 korumalı ret). Pay >= U ise kabul, pay < -U ise ret; arası ne kabul ne rettir
  (``UNVERIFIED``). U, spec'in ``uncertainty``'si ile ölçülen sayısal farkın toplamıdır: ikisi de üst sınır olarak
  verildiği için doğrusal toplanır (ihtiyatlı taraf). U = 0'da karar düz karşılaştırmadır.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .quantizer import parse_spice_value

STATUSES = ("PASS", "FAIL", "UNVERIFIED")
_FIELDS = {"min", "max", "uncertainty", "source"}


def _bound(name: Any, key: str, value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, str):
        value = parse_spice_value(value.strip())  # model "1u" gönderebilir
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"'{name}' spec'inin '{key}' sınırı sonlu bir sayı olmalı: {value!r}")
    return float(value)


def _bounds(name: Any, spec: Any) -> Tuple[Optional[float], Optional[float], float, Optional[str]]:
    """(alt, üst, belirsizlik, kaynak)."""
    if not isinstance(spec, Mapping):
        raise ValueError(f"'{name}' spec'i {{'min': ..., 'max': ...}} biçiminde olmalı: {spec!r}")
    unknown = sorted(set(spec) - _FIELDS)
    if unknown:
        raise ValueError(f"'{name}' spec'inde bilinmeyen alan: {unknown} (yalnız {sorted(_FIELDS)})")
    low, high = _bound(name, "min", spec.get("min")), _bound(name, "max", spec.get("max"))
    if low is None and high is None:
        raise ValueError(f"'{name}' spec'inde en az bir sınır ('min' ya da 'max') olmalı")
    if low is not None and high is not None and low > high:
        raise ValueError(f"'{name}' spec'inde min ({low:g}) max'tan ({high:g}) büyük")
    uncertainty = _bound(name, "uncertainty", spec.get("uncertainty")) or 0.0
    if uncertainty < 0:
        raise ValueError(f"'{name}' spec'inin belirsizliği negatif olamaz: {uncertainty:g}")
    source = spec.get("source")
    if source is not None and not isinstance(source, str):
        raise ValueError(f"'{name}' spec'inin kaynağı metin olmalı: {source!r}")
    return low, high, uncertainty, (source.strip() or None) if source else None


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _margin(value: float, low: Optional[float], high: Optional[float]) -> float:
    """Değerin en yakın sınıra uzaklığı: sınırlar içinde >= 0, dışında ihlalin büyüklüğü kadar negatif."""
    candidates = [value - low] if low is not None else []
    if high is not None:
        candidates.append(high - value)
    return min(candidates)


def check_specs(
    steps: Sequence[Mapping[str, Any]],
    specs: Mapping[Any, Any],
    failed: Sequence[str] = (),
    numerical: Optional[Mapping[str, Optional[float]]] = None,
) -> Dict[str, Any]:
    """
    ``.meas`` sonuçlarını spec'lerle karşılaştırır.

    Args:
        steps: Adım başına ``{ölçüm: değer}``; steplenmemiş koşumda tek eleman. Adlar büyük/küçük harf duyarsız
            (LTspice ölçüm adlarını küçük harfe çevirir).
        specs: ``{ölçüm: {"min": alt, "max": üst, "uncertainty": U, "source": "kaynak"}}``; sınırlardan biri yeter,
            ikisi de dahil. Sayılar SPICE yazımıyla metin de olabilir (``"1u"``). ``uncertainty`` (>= 0, ölçümün
            biriminde) iki yanlı koruma bandıdır; ``source`` sınırın kaynağıdır (yoksa ``warnings``'e yazılır).
        failed: LTspice'ın hesaplayamadığı ölçümler (logdaki ``Measurement "x" FAIL'ed``).
        numerical: Sayısal kontrol istendiyse ölçüm başına sayısal belirsizlik (yeniden koşumlardaki en büyük fark);
            ``None`` değer ya da eksik ad, kontrolün o ölçümü üretemediği demektir ve spec doğrulanamaz.

    Returns:
        ``verdict``: ``FAIL`` (en az bir spec kaldı), yoksa ``UNVERIFIED`` (en az biri doğrulanamadı), yoksa ``PASS``;
        ``passed``/``failed``/``unverified`` sayıları, ``steps``, ``warnings`` ve ``results``. Her sonuçta ``status``,
        ``min``, ``max`` (varsa ``source``); değeri okunanlarda en kötü adımın değeri (``worst_value``) ve numarası
        (``worst_step``, 1'den), ``margin`` (o adımda en yakın sınıra uzaklık; negatifse ihlal), ``uncertainty``
        (kullanılan koruma bandı), sınırı aşan adımlar (``failed_steps``); doğrulanamayanlarda ``reason``.

    Raises:
        ValueError: Spec yoksa ya da bir spec geçersizse.
    """
    if not specs:
        raise ValueError("en az bir spec gerekir: {ölçüm: {'min': ..., 'max': ...}}")
    folded_failed = {str(name).casefold() for name in failed}
    numeric = None if numerical is None else {str(k).casefold(): v for k, v in numerical.items()}
    tables = [{str(k).casefold(): v for k, v in step.items()} for step in steps]
    results: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for name, spec in specs.items():
        low, high, uncertainty, source = _bounds(name, spec)
        key = str(name).casefold()
        row: Dict[str, Any] = {"name": name, "min": low, "max": high}
        if source:
            row["source"] = source
        else:
            warnings.append(f"'{name}' spec'inin kaynağı yok: sınırın datasheet sayfasını ya da gereksinimini "
                            "'source' alanına yazın.")
        values = [_number(table.get(key)) for table in tables]
        missing = [i + 1 for i, value in enumerate(values) if value is None]
        if not values or len(missing) == len(values):
            reason = ("LTspice ölçümü hesaplayamadı (FAIL'ed): koşulu taranan pencerede gerçekleşmedi"
                      if key in folded_failed else "ölçüm logda yok: netliste bu adla bir .meas ekleyin")
            row.update(status="UNVERIFIED", reason=reason)
        elif missing:
            row.update(status="UNVERIFIED", reason=f"ölçüm {len(missing)} adımda hesaplanamadı (adım {missing})")
        elif numeric is not None and numeric.get(key) is None:
            row.update(status="UNVERIFIED", reason="sayısal kontrol bu ölçüm için sonuç vermedi (yeniden koşum)")
        else:
            numbers = [float(value) for value in values if value is not None]
            margins = [_margin(value, low, high) for value in numbers]
            worst = min(range(len(numbers)), key=margins.__getitem__)
            margin = margins[worst]
            u = uncertainty + (float(numeric[key] or 0.0) if numeric is not None else 0.0)
            status = "PASS" if margin >= u else "FAIL" if margin < -u else "UNVERIFIED"
            row.update(status=status, worst_value=numbers[worst], worst_step=worst + 1, margin=margin,
                       uncertainty=u, failed_steps=[i + 1 for i, margin in enumerate(margins) if margin < 0])
            if numeric is not None:
                row["numerical_uncertainty"] = numeric[key]
            if status == "UNVERIFIED":
                row["reason"] = (f"sonuç belirsizlik bandında: pay {margin:.6g}, belirsizlik {u:.6g}; ne kabul ne ret "
                                 "(JCGM 106:2012 8.3.2 korumalı kabul, 8.3.3 korumalı ret)")
        results.append(row)
    return {**_summary(results), "steps": len(tables), "warnings": warnings, "results": results}


def _summary(results: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    counts = {status: sum(row["status"] == status for row in results) for status in STATUSES}
    verdict = "FAIL" if counts["FAIL"] else "UNVERIFIED" if counts["UNVERIFIED"] else "PASS"
    return {"verdict": verdict, "passed": counts["PASS"], "failed": counts["FAIL"], "unverified": counts["UNVERIFIED"]}


def require_model(report: Mapping[str, Any], model: Mapping[str, Any]) -> Dict[str, Any]:
    """Tasarım kararını model doğrulamasına bağlar: önce modelin kendisi doğrulanır.

    ``model``, modelin kendi datasheet değerleri için bir `check_specs` raporudur (op-amp'ın GBW'si, regülatörün
    dropout'u). Kararı PASS değilse tasarımın PASS satırları UNVERIFIED olur, çünkü karar doğrulanmamış bir modele
    dayanır. FAIL satırları kalır: yanlış kabul doğurmazlar, ama nedenleri model de olabilir.
    """
    if model["verdict"] == "PASS":
        return dict(report)
    names = ", ".join(f"{row['name']} {row['status']}" for row in model["results"] if row["status"] != "PASS")
    reason = f"model doğrulanmadı ({names}): karar doğrulanmamış bir modele dayanır"
    results = [{**row, "status": "UNVERIFIED", "reason": reason} if row["status"] == "PASS" else dict(row)
               for row in report["results"]]
    return {**report, **_summary(results), "results": results}


def measurement_steps(parsed: Any) -> List[Dict[str, Any]]:
    """`check_specs`'in adım tabloları bir `LogParser` sonucundan: steplenmiş koşumda adım başına ölçümler (ortalama
    değil), yoksa ``measurements``. Bir adımda hesaplanamayan ölçüm o adımın tablosunda yoktur."""
    if not parsed.stepped_measurements:
        return [dict(parsed.measurements)]
    steps: Dict[int, Dict[str, Any]] = {}
    for name, rows in parsed.stepped_measurements.items():
        for row in rows:
            steps.setdefault(int(row["step"]), {})[name] = row.get(name)
    return [steps[number] for number in sorted(steps)]


def numerical_spread(base: Sequence[Mapping[str, Any]], reruns: Sequence[Optional[Sequence[Mapping[str, Any]]]],
                     names: Sequence[str]) -> Dict[str, Optional[float]]:
    """Sayısal belirsizlik: her ölçümün yeniden koşumlardaki en büyük mutlak farkı (bütün adımlarda). Bir yeniden koşum
    başarısızsa (``None``), adım sayısı tutmuyorsa ya da ölçüm bir adımda sayı değilse o ölçüm için ``None``."""
    spread: Dict[str, Optional[float]] = {}
    base_tables = [{str(k).casefold(): v for k, v in step.items()} for step in base]
    for name in names:
        key = str(name).casefold()
        worst: Optional[float] = 0.0
        for rerun in reruns:
            if rerun is None or len(rerun) != len(base_tables) or worst is None:
                worst = None
                break
            for table, step in zip(base_tables, rerun):
                a, b = _number(table.get(key)), _number({str(k).casefold(): v for k, v in step.items()}.get(key))
                if a is None or b is None:
                    worst = None
                    break
                worst = max(worst, abs(b - a))
        spread[str(name)] = worst
    return spread
