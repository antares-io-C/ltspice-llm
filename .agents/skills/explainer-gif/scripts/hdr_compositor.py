"""
Koyu zemin + ışıma (HDR bloom) kare birleştirici
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Koyu temalı (osiloskop ekranı gibi) GIF'ler için üç katmanlı kare:

1. ``base``: kartlar ve ızgara; döngüden önce bir kez çizilir (`build_studio_vignette` ile birleştirilir).
2. ``emit``: yalnızca ışık yayan eğriler, saf siyah üstünde. Yarım ve çeyrek çözünürlükte iki Gauss bulanıklığıyla
   ışıma (bloom) eklenir, sonra üstel ton eşleme uygulanır.
3. ``hud``: yazılar ve balonlar, saydam RGBA katmanda; ışımadan **sonra** bindirilir, bu yüzden bulanıklaşmaz.

Koyu degradelerde 256 renk basamaklanmasın diye karelere sabit 4x4 Bayer deseni eklenir; desen kareden kareye
değişmediği için duran pikseller titremez. Kodlama ``gif_encoder.encode_master_palette_gif(..., method="fastoctree")``
ile yapılır. numpy, scipy ve Pillow yeterlidir.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

import numpy as np
from scipy.ndimage import gaussian_filter

CINEMA_PALETTE: Dict[str, str] = {
    "bg_deep": "#060A17",
    "bg_card": "#0C1428",
    "bg_hud": "#0F172A",
    "grid": "#1E293B",
    "grid_hi": "#334155",
    "ink": "#F8FAFC",
    "ink_sec": "#CBD5E1",
    "muted": "#94A3B8",
    "cyan": "#38BDF8",
    "blue": "#60A5FA",
    "amber": "#FBBF24",
    "rose": "#FB7185",
    "emerald": "#34D399",
    "violet": "#A78BFA",
    "slate": "#94A3B8",
}

#: Balon arka planı, rol rengine göre
HUD_BG_BY_ROLE: Dict[str, str] = {
    "cyan": "#082F49",
    "blue": "#172554",
    "amber": "#451A03",
    "rose": "#4C0519",
    "emerald": "#022C22",
    "violet": "#2E1065",
    "slate": "#1E293B",
}

_BAYER_4X4 = (
    np.array(
        [
            [0.0, 8.0, 2.0, 10.0],
            [12.0, 4.0, 14.0, 6.0],
            [3.0, 11.0, 1.0, 9.0],
            [15.0, 7.0, 13.0, 5.0],
        ],
        dtype=np.float32,
    )
    / 16.0
    - 0.5
)


def hex_to_rgb_float(hex_color: str) -> Tuple[float, float, float]:
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0)


def _upsample_2x_bilinear(arr: np.ndarray, target_h: int, target_w: int) -> np.ndarray:
    """``(h, w, 3)`` diziyi ara pikselleri ortalayarak 2 kat büyütür, hedef boyuta kırpar."""
    h, w, c = arr.shape
    out = np.empty((h * 2, w * 2, c), dtype=np.float32)
    out[0::2, 0::2, :] = arr
    out[0::2, 1:-1:2, :] = 0.5 * (arr[:, :-1, :] + arr[:, 1:, :])
    out[0::2, -1, :] = arr[:, -1, :]
    out[1:-1:2, :, :] = 0.5 * (out[0:-2:2, :, :] + out[2::2, :, :])
    out[-1, :, :] = out[-2, :, :]
    return out[:target_h, :target_w, :]


def build_studio_vignette(height: int, width: int) -> np.ndarray:
    """Ortası hafif aydınlık, köşeleri koyu lacivert ``(H, W, 3)`` float32 zemin."""
    yy = np.linspace(-1.0, 1.0, height, dtype=np.float32)[:, None]
    xx = np.linspace(-1.0, 1.0, width, dtype=np.float32)[None, :]
    r2 = np.clip(0.55 * (xx * xx) + 0.75 * (yy * yy), 0.0, 1.35)
    glow = np.exp(-1.6 * r2)
    base = np.zeros((height, width, 3), dtype=np.float32)
    base[:, :, 0] = (5.0 + 9.0 * glow) / 255.0
    base[:, :, 1] = (8.0 + 16.0 * glow) / 255.0
    base[:, :, 2] = (20.0 + 32.0 * glow) / 255.0
    return base


def blend_static_base(vignette_f: np.ndarray, base_rgba_u8: np.ndarray) -> np.ndarray:
    """Saydam zeminli ``base`` katmanını (RGBA) vinyet üstüne bindirir, ``uint8`` RGB döndürür."""
    alpha = base_rgba_u8[:, :, 3:4].astype(np.float32) * (1.0 / 255.0)
    fg = base_rgba_u8[:, :, :3].astype(np.float32) * (1.0 / 255.0)
    comp = vignette_f * (1.0 - alpha) + fg * alpha
    return np.clip(comp * 255.0, 0.0, 255.0).astype(np.uint8)


def make_bayer_dither_field(height: int, width: int, amplitude_lsb: float = 2.1) -> np.ndarray:
    """Konumu sabit ``(H, W, 1)`` float32 Bayer deseni (genlik: ``amplitude_lsb`` / 255)."""
    tiled = np.tile(_BAYER_4X4, (height // 4 + 1, width // 4 + 1))[:height, :width, None]
    return tiled * (float(amplitude_lsb) / 255.0)


def compose_hdr_frame(
    base_rgb_u8: np.ndarray,
    emit_rgb_u8: np.ndarray,
    hud_rgba_u8: np.ndarray,
    *,
    tight_strength: float = 0.92,
    wide_strength: float = 0.72,
    exposure: float = 1.18,
    bayer_cache: Optional[np.ndarray] = None,
) -> np.ndarray:
    """``base`` (RGB), ``emit`` (siyah üstünde ışık, RGB) ve ``hud`` (RGBA) katmanlarını ``uint8`` RGB kareye birleştirir."""
    h, w, _ = base_rgb_u8.shape
    base_f = base_rgb_u8.astype(np.float32) * (1.0 / 255.0)
    emit_f = emit_rgb_u8.astype(np.float32) * (1.0 / 255.0)

    # Yarım çözünürlükte dar, çeyrek çözünürlükte geniş ışıma
    emit_half = emit_f[::2, ::2, :]
    tight_h = gaussian_filter(emit_half, sigma=(1.65, 1.65, 0.0))
    emit_qtr = emit_half[::2, ::2, :]
    wide_q = gaussian_filter(emit_qtr, sigma=(3.8, 3.8, 0.0))

    tight = _upsample_2x_bilinear(tight_h, h, w)
    wide_half = _upsample_2x_bilinear(wide_q, (h + 1) // 2, (w + 1) // 2)
    wide = _upsample_2x_bilinear(wide_half, h, w)

    hdr_scene = base_f + emit_f * 1.08 + tight_strength * tight + wide_strength * wide
    lit_rgb = 1.0 - np.exp(-exposure * hdr_scene)

    alpha = hud_rgba_u8[:, :, 3:4].astype(np.float32) * (1.0 / 255.0)
    hud_f = hud_rgba_u8[:, :, :3].astype(np.float32) * (1.0 / 255.0)
    comp = lit_rgb * (1.0 - alpha) + hud_f * alpha

    if bayer_cache is None:
        bayer_cache = make_bayer_dither_field(h, w)
    return np.clip((comp + bayer_cache) * 255.0, 0.0, 255.0).astype(np.uint8)
