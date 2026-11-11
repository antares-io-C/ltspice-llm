"""
Waveform Export
~~~~~~~~~~~~~~~
``.raw`` dalga formlarını CSV'ye yazar (Excel, MATLAB, pandas gibi dış araçlar için).
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

import numpy as np

from .parser import ALL_TRACES, open_raw, raw_analysis_kind, raw_axis_name, raw_axis_values, raw_steps, raw_wave

#: `export_waveforms_csv(delimiter=...)` için geçerli sütun ayırıcılar
CSV_DELIMITERS = (",", ";", "\t")
#: `export_waveforms_csv(decimal=...)` için geçerli ondalık ayırıcılar
CSV_DECIMALS = (".", ",")


def _resolve_signals(available: Sequence[str], signals: Optional[Sequence[str]]) -> List[str]:
    """İstenen sinyallerin .raw'daki adları (büyük/küçük harf ve boşluk önemsiz); ``signals`` boşsa hepsi."""
    if not available:
        raise ValueError(".raw dosyasında dışa aktarılacak iz yok.")
    if not signals:
        return list(available)
    by_key: Dict[str, str] = {}
    for name in available:
        by_key.setdefault(name.casefold(), name)
    resolved: List[str] = []
    for signal in signals:
        match = by_key.get("".join(str(signal).split()).casefold())
        if match is None:
            raise ValueError(f"Sinyal .raw dosyasında yok: {signal!r}. Kayıtlı izler: {list(available)}")
        if match not in resolved:
            resolved.append(match)
    return resolved


def _text_column(values: np.ndarray, decimal: str) -> np.ndarray:
    """
    Sayı sütununun metni. NumPy her sayının kendi türünde geri okunabilen en kısa yazımını verir (float32 0.1 ->
    ``0.1``): değer yuvarlanmaz, float64'e genişletilmiş hâlinin gürültü basamakları da yazılmaz.
    """
    text = np.asarray(values).astype(str)
    return np.char.replace(text, ".", ",") if decimal == "," else text


def export_waveforms_csv(
    raw_path: Union[str, Path],
    csv_path: Union[str, Path],
    signals: Optional[Sequence[str]] = None,
    *,
    delimiter: str = ",",
    decimal: str = ".",
) -> Dict[str, Any]:
    """
    ``.raw`` dosyasındaki izleri CSV'ye yazar; ``{"csv_path", "rows", "columns", "steps", "analysis"}`` döner.

    - İlk sütun X eksenidir (``time``, ``frequency`` ya da ``.dc`` kaynağının adı); ``.op`` sonucunda eksen yoktur.
      Zaman ekseni `raw_axis_values` ile okunur: Tstart'ı sıfırdan büyük ``.tran``'da değerler mutlak zamandır.
    - ``.ac`` izleri karmaşıktır: her sinyal ``re(V(out))`` ve ``im(V(out))`` sütunlarına yazılır.
    - Steplenmiş koşumda en başa ``step`` sütunu gelir (LTspice logundaki gibi 1'den başlar), adımlar alt alta yazılır.
    - ``signals``: yazılacak izler (büyük/küçük harfe duyarsız); verilmezse kayıtlı bütün izler.
    - ``delimiter`` (``,`` ``;`` sekme) ve ``decimal`` (``.`` ``,``): ondalık virgül kullanan bölge ayarındaki
      tablolama programları için ``delimiter=";"``, ``decimal=","``.

    Geçersiz ayırıcıda ya da bulunmayan sinyalde ValueError; bu durumda dosya yazılmaz. Dosya UTF-8'dir.
    """
    if delimiter not in CSV_DELIMITERS:
        raise ValueError(f"delimiter {list(CSV_DELIMITERS)} değerlerinden biri olmalı: {delimiter!r}")
    if decimal not in CSV_DECIMALS:
        raise ValueError(f"decimal {list(CSV_DECIMALS)} değerlerinden biri olmalı: {decimal!r}")
    if delimiter == decimal:
        raise ValueError("Ondalık ayırıcı virgülken sütun ayırıcı virgül olamaz; delimiter=';' kullanın.")

    raw = open_raw(raw_path, traces=ALL_TRACES)
    axis_name = raw_axis_name(raw)
    names = [str(name) for name in raw.get_trace_names()]
    chosen = _resolve_signals([name for name in names if name != axis_name], signals)
    steps = raw_steps(raw)
    stepped = len(steps) > 1

    header = (["step"] if stepped else []) + ([axis_name] if axis_name else [])
    for name in chosen:
        header += [f"re({name})", f"im({name})"] if np.iscomplexobj(raw_wave(raw, name, steps[0])) else [name]

    rows = 0
    path = Path(csv_path)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, delimiter=delimiter)
        writer.writerow(header)
        for number, step in enumerate(steps, start=1):
            waves = [raw_wave(raw, name, step) for name in chosen]
            count = len(waves[0])
            columns: List[np.ndarray] = [np.full(count, str(number))] if stepped else []
            axis = raw_axis_values(raw, step)
            if axis is not None:
                columns.append(_text_column(np.real(axis), decimal))
            for wave in waves:
                if np.iscomplexobj(wave):
                    columns += [_text_column(wave.real, decimal), _text_column(wave.imag, decimal)]
                else:
                    columns.append(_text_column(wave, decimal))
            writer.writerows(zip(*columns))
            rows += count
    return {
        "csv_path": str(path),
        "rows": rows,
        "columns": header,
        "steps": len(steps),
        "analysis": raw_analysis_kind(raw),
    }
