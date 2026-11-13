"""
Stability Margins
~~~~~~~~~~~~~~~~~
Döngü kazancının frekans yanıtından kazanç geçişi, faz payı ve kazanç payı. LTspice gerektirmez.

Girdi, bir ``.ac`` taramasındaki döngü kazancı T(jω)'dır; işareti kapalı döngü T/(1+T) olacak biçimde verilir
(negatif geri beslemeli döngüde düşük frekansta ∠T 0° ya da integratör başına -90°). Döngüyü açma yöntemi
(enjeksiyon kaynağı, T'nin ``.raw``'dan kurulması) bu modülün işi değildir.

- Kazanç geçişi |T| = 1'dir (0 dB). dB ve faz log f'e göre doğrusal aradeğerlenir: Bode asimptotları bu eksende
  düzdür; seyrek taramada (``.ac dec 10``) |T|'yi ya da f'yi doğrusal aradeğerlemek geçişi kaydırır.
- Faz payı PM = 180° + ∠T(fc), (-180°, 180°] aralığına indirgenir; açılmış (unwrapped) faz gerekmez.
- Faz geçişi, T'nin negatif gerçel ekseni kestiği yerdir (∠T = -180° mod 360°). Kazanç payı GM = -20·log10|T|.
- Birden çok geçiş varsa hepsi listelenir. Başlık değerleri karamsar seçilir: işaretli en küçük PM ve mutlak değeri en
  küçük GM, yani -1 noktasına ulaşmak için gereken en küçük kazanç değişikliği (artış ya da azalış).

Paylar kararlılığı tek başına kanıtlamaz: birden çok kazanç geçişi (koşullu kararlı ya da rezonanslı döngü) varsa
Nyquist eğrisine bakılmalı. Tarama, ardışık iki noktada faz 180°'den az değişecek kadar sık olmalı.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, List, Tuple, Union

import numpy as np
from numpy.typing import ArrayLike

from .parser import ALL_TRACES, LogParser, open_raw, raw_analysis_kind, raw_axis_values, raw_steps, raw_wave


def _validated(freq_hz: ArrayLike, loop_gain: ArrayLike) -> Tuple[np.ndarray, np.ndarray]:
    f = np.asarray(freq_hz, dtype=float)
    t = np.asarray(loop_gain, dtype=complex)
    if f.ndim != 1 or t.ndim != 1 or f.size != t.size:
        raise ValueError("freq_hz ve loop_gain aynı uzunlukta tek boyutlu diziler olmalı")
    if f.size < 2:
        raise ValueError("en az iki frekans noktası gerekir")
    if not (np.isfinite(f).all() and np.isfinite(t).all()):
        raise ValueError("frekans ve döngü kazancı sonlu olmalı (NaN ya da inf olamaz)")
    if f[0] <= 0 or (np.diff(f) <= 0).any():
        raise ValueError("frekanslar pozitif ve kesin artan olmalı")
    if (t == 0).any():
        raise ValueError("döngü kazancı sıfır olamaz (dB tanımsız)")
    return f, t


def _zero_crossings(values: np.ndarray, max_step: float = math.inf) -> List[Tuple[int, float]]:
    """Değerlerin sıfırı kestiği yerler: ``(i, kesir)``, kesir i ile i+1 arasında (0 ise tam örnek).

    Tam sıfır olan örnek bir kez sayılır, sıfıra dokunup dönen de. İşaret değiştirirken ``max_step``'ten büyük
    sıçrayan iki komşu kesişme değil sarmadır (açının ±180° sınırı).
    """
    out: List[Tuple[int, float]] = []
    for i in range(values.size):
        v = float(values[i])
        if v == 0.0:
            out.append((i, 0.0))
        elif i + 1 < values.size:
            w = float(values[i + 1])
            if v * w < 0.0 and abs(w - v) < max_step:
                out.append((i, v / (v - w)))
    return out


def _freq_at(f: np.ndarray, i: int, frac: float) -> float:
    """f[i] ile f[i+1] arasında, log f'te ``frac`` kesrindeki frekans."""
    if frac == 0.0:
        return float(f[i])
    return float(f[i] * (f[i + 1] / f[i]) ** frac)


def _phase_margin(phase_deg: float) -> float:
    """PM = 180° + ∠T, (-180°, 180°] aralığına indirgenmiş."""
    return 180.0 - ((-phase_deg) % 360.0)


def stability_margins(freq_hz: ArrayLike, loop_gain: ArrayLike) -> Dict[str, Any]:
    """Döngü kazancı T(jω)'dan kazanç geçişi, faz payı ve kazanç payı.

    Args:
        freq_hz: Pozitif, kesin artan frekanslar (Hz).
        loop_gain: Her frekanstaki karmaşık döngü kazancı T (kapalı döngü T/(1+T) işaretiyle).

    Returns:
        ``crossover_hz`` ve ``phase_margin_deg``: faz payı en küçük kazanç geçişi. ``phase_crossover_hz`` ve
        ``gain_margin_db``: kazanç payının mutlak değeri en küçük faz geçişi. Geçiş yoksa ``None``.
        ``gain_crossovers`` ve ``phase_crossovers`` bütün geçişleri frekans sırasıyla, ``warnings`` açıklamaları verir.

    Raises:
        ValueError: Diziler uyumsuzsa, frekanslar pozitif ve kesin artan değilse, değerler sonlu değilse ya da T sıfırsa.
    """
    f, t = _validated(freq_hz, loop_gain)
    mag_db = 20.0 * np.log10(np.abs(t))
    phase_deg = np.degrees(np.angle(t))

    gain_rows: List[Dict[str, float]] = []
    for i, frac in _zero_crossings(mag_db):
        phase = float(phase_deg[i])
        if frac:
            phase += frac * math.degrees(float(np.angle(t[i + 1] / t[i])))
        gain_rows.append({"freq_hz": _freq_at(f, i, frac), "phase_margin_deg": _phase_margin(phase)})

    phase_rows: List[Dict[str, float]] = []
    for i, frac in _zero_crossings(np.degrees(np.angle(-t)), max_step=180.0):
        db = float(mag_db[i])
        if frac:
            db += frac * float(mag_db[i + 1] - mag_db[i])
        phase_rows.append({"freq_hz": _freq_at(f, i, frac), "gain_margin_db": -db})

    worst_pm = min(gain_rows, key=lambda r: r["phase_margin_deg"], default=None)
    worst_gm = min(phase_rows, key=lambda r: abs(r["gain_margin_db"]), default=None)

    warnings: List[str] = []
    if not gain_rows:
        side = "üstünde" if mag_db[0] > 0 else "altında"
        warnings.append(f"Kazanç geçişi yok: |T| taranan aralığın tamamında 0 dB'nin {side}; faz payı tanımsız.")
    elif len(gain_rows) > 1:
        warnings.append(
            f"{len(gain_rows)} kazanç geçişi var (koşullu kararlı ya da rezonanslı döngü): paylar kararlılığı tek "
            "başına göstermez, Nyquist eğrisine bak."
        )
    if not phase_rows:
        warnings.append("Faz geçişi yok: ∠T taranan aralıkta -180°'ye ulaşmıyor; kazanç payı bu aralıkta tanımsız.")

    return {
        "crossover_hz": worst_pm["freq_hz"] if worst_pm is not None else None,
        "phase_margin_deg": worst_pm["phase_margin_deg"] if worst_pm is not None else None,
        "phase_crossover_hz": worst_gm["freq_hz"] if worst_gm is not None else None,
        "gain_margin_db": worst_gm["gain_margin_db"] if worst_gm is not None else None,
        "gain_crossovers": gain_rows,
        "phase_crossovers": phase_rows,
        "warnings": warnings,
    }


def _trace_name(names: Dict[str, str], given: str, role: str) -> str:
    text = given.strip()
    if not text:
        raise ValueError(f"'{role}' gereklidir (düğüm adı ya da V(...) izi)")
    expr = text if "(" in text else f"V({text})"
    real = names.get(expr.lower())
    if real is None:
        raise ValueError(f"'{expr}' izi yok; mevcut izler: {', '.join(sorted(names.values()))}")
    return real


def _step_params(raw_path: Union[str, Path]) -> List[Dict[str, Any]]:
    """Adımların parametre değerleri, ``.raw``'ın yanındaki ``.log``'un ``.step`` satırlarından (``.step kloop=4``),
    LTspice'ın koştuğu sırayla. Log yoksa ya da koşum steplenmemişse boş liste."""
    log = Path(raw_path).with_suffix(".log")
    if not log.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in LogParser.read_text(log).splitlines():
        if line.startswith(".step "):
            params: Dict[str, Any] = {}
            for pair in line.split()[1:]:
                name, _, value = pair.partition("=")
                try:
                    params[name] = float(value)
                except ValueError:
                    params[name] = value
            rows.append(params)
    return rows


def raw_loop_margins(raw_path: Union[str, Path], loop_input: str, loop_output: str) -> Dict[str, Any]:
    """Bir ``.ac`` koşumunun ``.raw``'ından döngü kazancı T = -V(loop_output)/V(loop_input) ve her adımın payları.

    Döngü seri bir AC enjeksiyon kaynağıyla açılır (Middlebrook): ``Vinj x y AC 1``'de x kaynağın döngüye sürdüğü
    düğümdür (``loop_input``), y döngünün geri döndüğü düğümdür (``loop_output``). Ölçüm, kaynağın y tarafındaki
    empedans x tarafındakinden çok küçükken T'ye eşittir.

    Args:
        raw_path: ``.raw`` yolu; steplenmişse yanında aynı adlı ``.log`` olmalı (`open_raw`).
        loop_input, loop_output: Düğüm adı (``x``) ya da iz (``V(x)``); büyük/küçük harf duyarsız.

    Returns:
        ``loop_gain`` (kullanılan ifade) ve ``steps``: adım başına (1'den) `stability_margins` sonucu; steplenmiş
        koşumda ``params`` o adımın parametre değerleridir (``.log``'dan, ör. ``{"kloop": 4.0}``).

    Raises:
        ValueError: Koşum ``.ac`` değilse, iz yoksa ya da veri `stability_margins`'in reddettiği türdense.
    """
    raw = open_raw(raw_path, traces=ALL_TRACES)
    kind = raw_analysis_kind(raw)
    if kind != "ac":
        raise ValueError(f"döngü kazancı .ac koşumu ister; bu .raw '{kind}'")
    names = {n.lower(): n for n in raw.get_trace_names()}
    inp = _trace_name(names, loop_input, "loop_input")
    out = _trace_name(names, loop_output, "loop_output")
    steps = []
    for number, step in enumerate(raw_steps(raw), start=1):
        axis = raw_axis_values(raw, step)
        if axis is None:
            raise ValueError(f"'{raw_path}' dosyasında frekans ekseni yok")
        freq = np.real(axis)
        with np.errstate(divide="ignore", invalid="ignore"):
            loop_gain = -raw_wave(raw, out, step) / raw_wave(raw, inp, step)
        steps.append({"step": number, **stability_margins(freq, loop_gain)})
    params = _step_params(raw_path)
    if len(params) == len(steps):
        for entry, values in zip(steps, params):
            entry["params"] = values
    return {"loop_gain": f"-{out}/{inp}", "steps": steps}
