"""
GIF kodlayıcı ve okuma süresi yardımcıları (explainer-gif'in çekirdeği)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Bütün arketipler ve kendi yazacağın kare döngüleri bu modülü kullanır:

1. **Ortak palet** (`encode_master_palette_gif`): bütün kareler tek bir 256 renk paletine kilitlenir; kare başına
   palet arka planı ve yazıyı titretir.
2. **Olay anında ağır çekim** (`adaptive_event_samples`): dik kenar ve tepe bölgelerinde kare sıklaşır, kritik anlar
   tam bir kareye denk gelir.
3. **Okuma süresi alt sınırı** (`required_reading_ms`, `enforce_minimum_dwell`): ekrandaki her durum metni en az
   `min_dwell_ms` görünür; eksik süre metnin kendi karelerine eklenir, hiçbir kare kısaltılmaz.
4. **Tel üstünde akan tanecikler** (`integrate_particle_phases`, `sample_polyline_points`): taneciklerin hızı ve yönü
   verilen akım dizisinden gelir.

Süre değerleri (1650-1800 ms alt sınır, 450 ms + 230 ms/kelime) bu skill'in varsayılanlarıdır, ölçülmüş bir okuma
standardı değildir. `ltspice_llm`'e bağlı değildir; numpy ve Pillow yeterlidir.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

#: Varsayılan okuma süresi alt sınırı (ms)
MIN_READABLE_DWELL_MS = 1650
#: ``validate_gif.py`` bu önekle başlayan GIF yorumundan metin kalış sürelerini okur.
COMMENT_PREFIX = "explainer-gif:"

_QUANTIZE_METHODS = {"mediancut": Image.Quantize.MEDIANCUT, "fastoctree": Image.Quantize.FASTOCTREE}


def _round_gif_ms(ms: float, minimum: int = 20) -> int:
    """Süreyi GIF'in 10 ms (1/100 sn) adımına yuvarlar."""
    return max(minimum, int(round(float(ms) / 10.0)) * 10)


def _ceil_gif_ms(ms: float) -> int:
    """Süreyi 10 ms'nin bir üst katına yuvarlar (alt sınır garantisi için)."""
    return int(math.ceil(float(ms) / 10.0 - 1e-9)) * 10


def required_reading_ms(
    text: str,
    min_dwell_ms: int = MIN_READABLE_DWELL_MS,
    ms_per_word: int = 230,
    max_auto_ms: int = 2700,
) -> int:
    """
    Kısa bir durum metninin ekranda kalması gereken süre (ms): ``450 + kelime × ms_per_word``, en çok ``max_auto_ms``.

    Sonuç hiçbir zaman ``min_dwell_ms``'nin altına inmez (üst sınır yalnızca kelime payını keser). Boş metin 0 döner.
    """
    cleaned = str(text).strip()
    if not cleaned:
        return 0
    by_words = min(450 + len(cleaned.split()) * ms_per_word, max_auto_ms)
    return _ceil_gif_ms(max(min_dwell_ms, by_words))


def adaptive_event_samples(
    x_min: float,
    x_max: float,
    n_frames: int,
    event_windows: Sequence[Tuple[float, float]] = (),
    density_boost: float = 3.8,
    snap_points: Sequence[float] = (),
) -> np.ndarray:
    """
    ``[x_min, x_max]`` aralığında ``n_frames`` örnek üretir; ``event_windows`` aralıklarında örnekler
    ``density_boost`` kat sıktır (ağır çekim). ``snap_points`` içindeki anlar tam bir örneğe denk getirilir: her ana
    ayrı bir iç örnek düşer, ilk ve son örnek ``x_min`` ve ``x_max`` kalır. Aralık dışındaki anlar yok sayılır; anlar iç
    örneklere sığmıyorsa ``ValueError``.
    """
    first, last = float(x_min), float(x_max)
    moments = sorted({float(sp) for sp in snap_points if first < float(sp) < last})
    if len(moments) > max(n_frames - 2, 0):
        raise ValueError(f"snap_points: {len(moments)} an {n_frames} örneğe sığmaz; örnek sayısını artırın.")
    if n_frames <= 1:
        return np.array([first], dtype=float)
    dense_x = np.linspace(first, last, 2400)
    weights = np.ones_like(dense_x)
    boost = max(float(density_boost), 1.0)
    for w0, w1 in event_windows:
        lo, hi = min(float(w0), float(w1)), max(float(w0), float(w1))
        weights[(dense_x >= lo) & (dense_x <= hi)] = boost

    cdf = np.cumsum(weights)
    cdf = (cdf - cdf[0]) / max(float(cdf[-1] - cdf[0]), 1e-12)
    samples = np.interp(np.linspace(0.0, 1.0, n_frames), cdf, dense_x)
    samples[0], samples[-1] = first, last

    # Her an sırayı bozmadan kendi iç örneğine: en yakını, doluysa sonraki; ardından gelen anlara yer bırakılır.
    slot = 0
    for remaining, moment in zip(range(len(moments), 0, -1), moments):
        nearest = int(np.argmin(np.abs(samples[1:-1] - moment))) + 1
        slot = min(max(nearest, slot + 1), n_frames - 1 - remaining)
        samples[slot] = moment
    samples.sort()   # birkaç ulp'lik aralıkta örnekler aynı float'a çöker, argmin eşitlerin ilkini seçer
    return samples


def build_pacing_schedule(
    n_frames: int,
    fps: int = 18,
    poster_hold_ms: int = 1350,
    highlight_indices: Iterable[int] = (),
    highlight_hold_ms: int = 1100,
    end_hold_ms: int = 1450,
) -> List[int]:
    """Kare süreleri (ms): akışta ``1000/fps``, kare 0'da (poster), vurgu karelerinde ve son karede bekleme."""
    if n_frames <= 0:
        raise ValueError("n_frames en az 1 olmalıdır.")
    base_ms = _round_gif_ms(1000.0 / max(fps, 1))
    durations = [base_ms] * n_frames

    if poster_hold_ms > 0:
        durations[0] = max(base_ms, _round_gif_ms(poster_hold_ms))

    hl_ms = max(base_ms, _round_gif_ms(highlight_hold_ms))
    for idx in highlight_indices:
        if 0 < idx < n_frames:
            durations[idx] = max(durations[idx], hl_ms)

    if n_frames > 1 and end_hold_ms > 0:
        durations[-1] = max(durations[-1], _round_gif_ms(end_hold_ms))

    return durations


def _label_runs(state_labels: Sequence[str], start: int) -> List[Tuple[str, int, int]]:
    """Ardışık aynı (boş olmayan) etiketlerin ``(metin, ilk, son)`` kare dizinleri."""
    runs: List[Tuple[str, int, int]] = []
    n = len(state_labels)
    i = start
    while i < n:
        lbl = str(state_labels[i]).strip()
        j = i
        while j + 1 < n and str(state_labels[j + 1]).strip() == lbl:
            j += 1
        if lbl:
            runs.append((lbl, i, j))
        i = j + 1
    return runs


def enforce_minimum_dwell(
    durations_ms: Sequence[int],
    state_labels: Sequence[str],
    min_dwell_ms: int = MIN_READABLE_DWELL_MS,
    anchor_indices: Sequence[int] = (),
    skip_frame_zero: bool = True,
) -> List[int]:
    """
    Her durum metninin kesintisiz kalış süresini en az ``required_reading_ms(metin, min_dwell_ms)`` yapar.

    Eksik süre, koşumda bir vurgu karesi (``anchor_indices``) varsa çoğu ona, kalanı komşularına; yoksa koşumun ilk
    üç karesine eklenir. Hiçbir kare kısaltılmaz. ``skip_frame_zero``: kare 0 poster karesidir, denetlenmez (tek kareli
    dizide ayrı poster yoktur, o kare denetlenir).
    """
    if len(durations_ms) != len(state_labels):
        raise ValueError("durations_ms ve state_labels aynı uzunlukta olmalıdır.")
    out = [int(d) for d in durations_ms]
    start = 1 if (skip_frame_zero and len(out) > 1) else 0
    anchors = set(int(a) for a in anchor_indices)

    for lbl, i, j in _label_runs(state_labels, start):
        needed = required_reading_ms(lbl, min_dwell_ms=min_dwell_ms)
        current = sum(out[i : j + 1])
        if current >= needed:
            continue
        deficit = _ceil_gif_ms(needed - current)
        run_anchors = [k for k in range(i, j + 1) if k in anchors]
        if run_anchors:
            target = run_anchors[0]
            neighbors = [k for k in (target - 1, target + 1) if i <= k <= j]
            main_add = _ceil_gif_ms(deficit * 0.72) if neighbors else deficit
            rest = deficit - main_add
            out[target] += main_add
            if rest > 0:
                per_neighbor = _ceil_gif_ms(rest / len(neighbors))
                for k in neighbors:
                    out[k] += per_neighbor
        else:
            focus = list(range(i, min(j, i + 2) + 1))
            per_frame = _ceil_gif_ms(deficit / len(focus))
            added = 0
            for k in focus:
                chunk = min(per_frame, deficit - added)
                if chunk > 0:
                    out[k] += chunk
                    added += chunk
    return out


def compute_state_dwell_summary(
    durations_ms: Sequence[int],
    state_labels: Sequence[str],
    skip_frame_zero: bool = True,
) -> List[Tuple[str, int, int, int]]:
    """Her durum metni koşumunun ``(metin, ilk_kare, son_kare, toplam_ms)`` özeti."""
    start = 1 if (skip_frame_zero and len(durations_ms) > 1) else 0
    return [(lbl, i, j, int(sum(durations_ms[i : j + 1]))) for lbl, i, j in _label_runs(state_labels, start)]


def _master_palette(frames_rgb: Sequence[np.ndarray], method: Image.Quantize, max_colors: int) -> Image.Image:
    """İlk, son ve aradaki ~12 karenin yarı çözünürlüklü şeridinden tek bir palet çıkarır."""
    n = len(frames_rgb)
    sample_count = min(n, 12)
    indices = sorted({0, n - 1, *[int(round(i * (n - 1) / max(sample_count - 1, 1))) for i in range(sample_count)]})
    composite = np.concatenate([frames_rgb[idx][::2, ::2, :] for idx in indices], axis=0)
    colors = min(max(max_colors, 16), 256)
    strip = Image.fromarray(np.ascontiguousarray(composite, dtype=np.uint8))
    return strip.quantize(colors=colors, method=method, dither=Image.Dither.NONE)


def encode_master_palette_gif(
    frames_rgb: Sequence[np.ndarray],
    durations_ms: Sequence[int],
    out_path: str | Path,
    max_colors: int = 256,
    loop: int = 0,
    state_labels: Optional[Sequence[str]] = None,
    *,
    method: str = "mediancut",
    skip_frame_zero: bool = True,
) -> Path:
    """
    ``H x W x 3`` ``uint8`` kareleri tek ortak paletle sonsuz döngülü ``.gif`` olarak yazar.

    ``method``: açık zeminli çizimlerde ``"mediancut"``, koyu zeminli ışıma (HDR) karelerinde ``"fastoctree"``.
    ``state_labels`` verilirse her metin koşumunun kalış süresi GIF yorumuna yazılır (``validate_gif.py`` okur);
    ``skip_frame_zero`` ``enforce_minimum_dwell``'e verilenle aynı olmalı (kare 0 poster değilse ``False``).
    Pillow, birbirinin aynısı ardışık kareleri süreleri toplanarak tek kareye indirebilir.
    """
    if not frames_rgb:
        raise ValueError("encode_master_palette_gif en az bir kare bekler.")
    if len(frames_rgb) != len(durations_ms):
        raise ValueError(f"Kare sayısı ({len(frames_rgb)}) ile süre sayısı ({len(durations_ms)}) eşleşmiyor.")
    if method not in _QUANTIZE_METHODS:
        raise ValueError(f"method {sorted(_QUANTIZE_METHODS)} değerlerinden biri olmalı: {method!r}")

    palette = _master_palette(frames_rgb, _QUANTIZE_METHODS[method], max_colors)
    frames = [
        Image.fromarray(np.ascontiguousarray(arr, dtype=np.uint8)).quantize(palette=palette, dither=Image.Dither.NONE)
        for arr in frames_rgb
    ]

    save_kwargs: dict = {
        "format": "GIF",
        "save_all": True,
        "append_images": frames[1:],
        "duration": [int(d) for d in durations_ms],
        "loop": int(loop),
        "optimize": True,
        "disposal": 2,
    }
    if state_labels is not None:
        runs = compute_state_dwell_summary(durations_ms, state_labels, skip_frame_zero=skip_frame_zero)
        payload = {"dwell_runs": [{"text": t, "f0": a, "f1": b, "ms": ms} for t, a, b, ms in runs]}
        save_kwargs["comment"] = (COMMENT_PREFIX + json.dumps(payload, ensure_ascii=False)).encode("utf-8")

    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(str(target), **save_kwargs)
    return target


def integrate_particle_phases(
    current_values: Sequence[float],
    n_particles: int = 12,
    max_step_per_frame: float = 0.048,
) -> np.ndarray:
    """
    Kapalı bir tel yolundaki taneciklerin her karedeki ``[0, 1)`` konumları, ``(kare, tanecik)`` dizisi.

    Adım, akımın tepe değerine göre ölçeklenir: akım sıfırken tanecikler durur, işaret değişince yön değişir.
    """
    cur = np.asarray(current_values, dtype=float)
    peak = float(np.max(np.abs(cur))) if cur.size else 0.0
    norm_v = (cur / peak) * max_step_per_frame if peak > 1e-12 else np.zeros_like(cur)

    base = np.linspace(0.0, 1.0, n_particles, endpoint=False)
    phases = np.zeros((len(cur), n_particles), dtype=float)
    cum = 0.0
    for k, step in enumerate(norm_v):
        if k > 0:
            cum = (cum + float(step)) % 1.0
        phases[k, :] = (base + cum) % 1.0
    return phases


def sample_polyline_points(
    vertices: Sequence[Tuple[float, float]],
    phases: Sequence[float],
    skip_zones: Sequence[Tuple[float, float]] = (),
) -> Tuple[List[float], List[float]]:
    """
    Köşe noktalarıyla verilen tel yolunda ``[0, 1)`` konumların ``(x, y)`` koordinatları.

    ``skip_zones``: tanecik çizilmeyecek konum aralıkları (ör. bir bileşen kutusunun içinden geçen kısım).
    """
    pts = [np.asarray(v, dtype=float) for v in vertices]
    segs = [pts[i + 1] - pts[i] for i in range(len(pts) - 1)]
    lengths = np.array([float(np.hypot(s[0], s[1])) for s in segs], dtype=float)
    total_len = float(np.sum(lengths))
    if total_len <= 1e-9:
        return [], []

    cum_frac = np.concatenate([[0.0], np.cumsum(lengths) / total_len])
    xs: List[float] = []
    ys: List[float] = []
    for ph in phases:
        p = float(ph) % 1.0
        if any(z0 <= p <= z1 for z0, z1 in skip_zones):
            continue
        seg_idx = int(np.searchsorted(cum_frac, p, side="right")) - 1
        seg_idx = min(max(seg_idx, 0), len(segs) - 1)
        span = max(float(cum_frac[seg_idx + 1] - cum_frac[seg_idx]), 1e-9)
        local_t = (p - float(cum_frac[seg_idx])) / span
        pos = pts[seg_idx] + local_t * segs[seg_idx]
        xs.append(float(pos[0]))
        ys.append(float(pos[1]))
    return xs, ys
