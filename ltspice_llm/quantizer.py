"""
Standard Component Quantizer (E-Series IEC 60063)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
LLM ve nümerik optimizasyon algoritmalarının ürettiği teorik/sürekli komponent değerlerini
(örn: 15.915kΩ, 1.414nF) ticari olarak üretilen standart E-serisi (E6, E12, E24, E96)
bileşen değerlerine yuvarlar (snapping) ve hata/sapma analizi yapar.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, Literal, Tuple, Union

# IEC 60063 Standart Temel Değerler (1.0 - 10.0 arası çarpanlar)
E6_BASE: Tuple[float, ...] = (1.0, 1.5, 2.2, 3.3, 4.7, 6.8)

E12_BASE: Tuple[float, ...] = (
    1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2,
)

E24_BASE: Tuple[float, ...] = (
    1.0, 1.1, 1.2, 1.3, 1.5, 1.6, 1.8, 2.0, 2.2, 2.4, 2.7, 3.0,
    3.3, 3.6, 3.9, 4.3, 4.7, 5.1, 5.6, 6.2, 6.8, 7.5, 8.2, 9.1,
)

# E96: %1 toleranslı standart EIA-96 tabanı (96 değer)
E96_BASE: Tuple[float, ...] = (
    1.00, 1.02, 1.05, 1.07, 1.10, 1.13, 1.15, 1.18, 1.21, 1.24, 1.27, 1.30,
    1.33, 1.37, 1.40, 1.43, 1.47, 1.50, 1.54, 1.58, 1.62, 1.65, 1.69, 1.74,
    1.78, 1.82, 1.87, 1.91, 1.96, 2.00, 2.05, 2.10, 2.15, 2.21, 2.26, 2.32,
    2.37, 2.43, 2.49, 2.55, 2.61, 2.67, 2.74, 2.80, 2.87, 2.94, 3.01, 3.09,
    3.16, 3.24, 3.32, 3.40, 3.48, 3.57, 3.65, 3.74, 3.83, 3.92, 4.02, 4.12,
    4.22, 4.32, 4.42, 4.53, 4.64, 4.75, 4.87, 4.99, 5.11, 5.23, 5.36, 5.49,
    5.62, 5.76, 5.90, 6.04, 6.19, 6.34, 6.49, 6.65, 6.81, 6.98, 7.15, 7.32,
    7.50, 7.68, 7.87, 8.06, 8.25, 8.45, 8.66, 8.87, 9.09, 9.31, 9.53, 9.76,
)

SERIES_MAP: Dict[str, Tuple[float, ...]] = {
    "E6": E6_BASE,
    "E12": E12_BASE,
    "E24": E24_BASE,
    "E96": E96_BASE,
}

# SPICE SI Çarpanları
SPICE_PREFIXES: Dict[str, float] = {
    "t": 1e12,
    "g": 1e9,
    "meg": 1e6,
    "k": 1e3,
    "m": 1e-3,
    "u": 1e-6,
    "µ": 1e-6,
    "n": 1e-9,
    "p": 1e-12,
    "f": 1e-15,
}

# LTspice sayı yazımı (ölçüm LTspice 26.1.1, examples/12): baştaki rakam isteğe bağlı (".5k", "-.5k", "1.k"),
# çarpandan sonra gömülü ondalık ("4k7" = 4.7k, "1.5k3" = 1.8k, "4R7" = 4.7, "-4k7" = -3.3k; ADI
# SPICE-SYNTAX-REFERENCE "Embedded decimal"). Bilinmeyen harften sonraki rakamlar yok sayılır ("4V7" = 4);
# gömülü ondalıktan sonra harf gelirse ("4k7ohm") LTspice hata verir.
_SPICE_VAL_RE = re.compile(
    r"^\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*([a-zA-Zµ]*)(\d*)\s*$"
)


def parse_spice_value(val: Union[str, float, int]) -> float:
    """
    SPICE formatındaki metin veya sayıları standart float değere çevirir.
    Örn: '15.915k' -> 15915.0, '1.414n' -> 1.414e-9, '10meg' -> 1e7, '100u' -> 1e-4, '4k7' -> 4700.0,
    '.1u' -> 1e-7.
    """
    if isinstance(val, (int, float)):
        return float(val)

    if not isinstance(val, str):
        raise TypeError(f"Geçersiz SPICE değeri tipi: {type(val)}")

    clean = val.strip()
    match = _SPICE_VAL_RE.match(clean)
    if not match:
        # Doğrudan float çevirisi dene
        return float(clean)

    number_part = float(match.group(1))
    unit_part = match.group(2).lower()
    embedded = match.group(3)

    if not unit_part:
        if embedded:  # yalnızca rakamlar: "1e3" gibi tam eşleşme yoksa (ör. "12 3") geçersiz
            raise ValueError(f"Geçersiz SPICE değeri: {val!r}")
        return number_part

    # SPICE kuralları: 'meg' önceliklidir, 'mil' 25.4e-6'dır, diğerlerinde ilk harf önemlidir.
    if unit_part.startswith("meg"):
        multiplier, known = SPICE_PREFIXES["meg"], unit_part == "meg"
    elif unit_part.startswith("mil"):
        multiplier, known = 25.4e-6, unit_part == "mil"
    else:
        prefix = unit_part[0]
        multiplier = SPICE_PREFIXES.get(prefix, 1.0)
        known = len(unit_part) == 1 and (prefix in SPICE_PREFIXES or prefix == "r")

    if embedded:
        if not known:  # "4V7": bilinmeyen harften sonrası yok sayılır
            return number_part
        # LTspice kesri işaretten bağımsız ekler: "-4k7" = (-4 + 0.7)k = -3.3k (ölçüm, examples/12)
        number_part += float(f"0.{embedded}")

    return number_part * multiplier


def format_spice_value(val: float, precision: int = 3, unit: str = "") -> str:
    """
    Float değeri mühendislik SPICE formatına çevirir.
    Örn: 15915.0 -> '15.9k', 1.414e-9 -> '1.41n', 0.001 -> '1m'.
    """
    if math.isnan(val) or math.isinf(val):
        return str(val)

    abs_val = abs(val)
    if abs_val == 0.0:
        return f"0{unit}"

    sign = "-" if val < 0 else ""

    # Önce gösterilecek anlamlı hane sayısına yuvarla: 999.6k -> 1MEG ("1e+03k" üretilmesin).
    abs_val = float(f"{abs_val:.{precision}g}")

    # SI önekleri (f..T) dışında kalan değerler bilimsel gösterimle yazılır; SPICE bunu da okur.
    if abs_val >= 1e15 or abs_val < 1e-15:
        return f"{sign}{abs_val:.{precision}g}{unit}"

    prefixes = [
        (1e12, "T"),
        (1e9, "G"),
        (1e6, "MEG"),
        (1e3, "k"),
        (1.0, ""),
        (1e-3, "m"),
        (1e-6, "u"),
        (1e-9, "n"),
        (1e-12, "p"),
        (1e-15, "f"),
    ]

    for scale, prefix in prefixes:
        if abs_val >= scale * 0.999999:
            scaled = abs_val / scale
            # Tam sayıya çok yakınsa tamsayı gibi bas
            if abs(scaled - round(scaled)) < 1e-5:
                formatted = f"{int(round(scaled))}"
            else:
                formatted = f"{scaled:.{precision}g}".rstrip("0").rstrip(".")
            return f"{sign}{formatted}{prefix}{unit}"

    return f"{val:.{precision}g}{unit}"


_SNAP_REL_TOL = 1e-9


def snap_to_e_series(
    value: Union[str, float, int],
    series: str = "E24",
    mode: Literal["nearest", "floor", "ceil"] = "nearest",
) -> Dict[str, Any]:
    """
    Verilen bir değeri en yakın standart E-serisi (E6, E12, E24, E96) değerine yuvarlar.

    Dönüş:
    {
        "nominal": 15915.0,
        "standard_value": 16000.0,
        "formatted": "16k",
        "series": "E24",
        "error_pct": 0.534,
        "difference": 85.0
    }
    """
    num_val = parse_spice_value(value)
    if num_val <= 0:
        raise ValueError("E-serisi kuantalama için değer pozitif bir sayı olmalıdır.")

    series_upper = series.upper().strip()
    if series_upper not in SERIES_MAP:
        raise ValueError(
            f"Bilinmeyen E-serisi '{series}'. Desteklenen seriler: {list(SERIES_MAP.keys())}"
        )

    base_values = SERIES_MAP[series_upper]

    # Değerin ondalık mertebesini (decade) bul: örn. 15915 -> decade=10000, normalized=1.5915
    exponent = math.floor(math.log10(num_val))
    decade = 10.0 ** exponent
    normalized = num_val / decade

    # Normalized değer 1.0 ile 10.0 arasında olmalı. Hassasiyet nedeniyle 9.9999.. olabilir.
    if normalized < 1.0:
        decade /= 10.0
        normalized *= 10.0
    elif normalized >= 10.0:
        decade *= 10.0
        normalized /= 10.0

    # Serideki en yakın, bir alt (floor) ve bir üst (ceil) adayları bul. Bölme kayan noktada serideki değeri
    # biraz kaydırabilir (3.3e-7 / 1e-7 = 3.3000000000000003): karşılaştırma göreli toleransla yapılır, yoksa
    # serideki bir değer floor/ceil kipinde komşusuna atlar (330n E6 ceil -> 470n).
    candidates = list(base_values) + [10.0]

    floor_cand = base_values[0]
    ceil_cand = 10.0
    nearest_cand = base_values[0]
    min_log_diff = float("inf")

    for c in candidates:
        if c <= normalized * (1.0 + _SNAP_REL_TOL):
            floor_cand = c
        if c >= normalized * (1.0 - _SNAP_REL_TOL) and c < ceil_cand:
            ceil_cand = c

        # Logaritmik oran farkına göre en yakın adayı seç
        log_diff = abs(math.log(normalized / c))
        if log_diff < min_log_diff:
            min_log_diff = log_diff
            nearest_cand = c

    if mode == "floor":
        chosen_norm = floor_cand
    elif mode == "ceil":
        chosen_norm = ceil_cand
    else:  # nearest
        chosen_norm = nearest_cand

    # Standart gerçek değeri hesapla
    standard_value = chosen_norm * decade
    # Yüzen nokta hassasiyetini temizle (örn. 1.6 * 10000 -> 16000.0)
    standard_value = float(f"{standard_value:.6g}")

    diff = standard_value - num_val
    error_pct = (diff / num_val) * 100.0

    return {
        "nominal": num_val,
        "standard_value": standard_value,
        "formatted": format_spice_value(standard_value),
        "series": series_upper,
        "error_pct": round(error_pct, 4),
        "difference": round(diff, 6),
    }


def quantize_circuit_parameters(
    parameters: Dict[str, Any],
    series: str = "E24",
    mode: Literal["nearest", "floor", "ceil"] = "nearest",
) -> Dict[str, Any]:
    """
    Birden fazla devre parametresini (direnç, kondansatör vb.) standart değerlere yuvarlar.

    Parametreler:
        parameters: {"R1": "15.915k", "C1": "1.414n", ...}
        series: "E12", "E24", "E96" (varsayılan: "E24")
        mode: "nearest", "floor", "ceil"

    Dönüş:
        {
            "status": "SUCCESS",
            "series": "E24",
            "quantized_parameters": {"R1": "16k", "C1": "1.5n"},
            "raw_quantized_values": {"R1": 16000.0, "C1": 1.5e-9},
            "details": {
                "R1": {"nominal": 15915.0, "standard": 16000.0, "error_pct": 0.534},
                ...
            },
            "max_abs_error_pct": 0.534
        }
    """
    quantized_params: Dict[str, str] = {}
    raw_quantized: Dict[str, float] = {}
    details: Dict[str, Any] = {}
    max_error = 0.0

    for name, val in parameters.items():
        try:
            snapped = snap_to_e_series(val, series=series, mode=mode)
            quantized_params[name] = snapped["formatted"]
            raw_quantized[name] = snapped["standard_value"]
            details[name] = {
                "nominal": snapped["nominal"],
                "standard": snapped["standard_value"],
                "formatted": snapped["formatted"],
                "error_pct": snapped["error_pct"],
            }
            if abs(snapped["error_pct"]) > max_error:
                max_error = abs(snapped["error_pct"])
        except Exception as e:
            # Sayısal olmayan veya formül içeren parametreleri olduğu gibi bırak
            quantized_params[name] = str(val)
            details[name] = {"nominal": str(val), "skipped_reason": str(e)}

    return {
        "status": "SUCCESS",
        "series": series.upper(),
        "quantized_parameters": quantized_params,
        "raw_quantized_values": raw_quantized,
        "details": details,
        "max_abs_error_pct": round(max_error, 4),
    }
