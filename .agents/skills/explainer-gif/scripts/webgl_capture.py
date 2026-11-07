"""
HTML5 Canvas / WebGL sahnesini sanal saatle kare kare yakalayıp GIF'e çevirir (isteğe bağlı)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
matplotlib'in yetmediği sahneler (Three.js, GLSL) için. Sayfa ``window.renderAtTime(t_ms)`` tanımlar; betik
``performance.now``, ``Date.now`` ve ``requestAnimationFrame``'i sanal saate bağlar, her ``t`` için kanvasın ekran
görüntüsünü alır. Gerçek zamanlı kayıttaki kare düşmesi olmaz, aynı girdi aynı GIF'i verir. Sahne ağ kapalı çalışır.

Gereksinim: ``pip install playwright`` ve ``python -m playwright install chromium``. Chromium indirilmemişse
kurulu Edge ya da Chrome kanalı denenir.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Callable, List, Optional, Sequence

import numpy as np
from PIL import Image

if __package__:
    from .gif_encoder import build_pacing_schedule, encode_master_palette_gif, enforce_minimum_dwell
    from .hdr_compositor import make_bayer_dither_field
else:
    from gif_encoder import build_pacing_schedule, encode_master_palette_gif, enforce_minimum_dwell
    from hdr_compositor import make_bayer_dither_field

_SCENE_URL = "http://explainer-gif.invalid/scene.html"

VIRTUAL_CLOCK_INIT_JS = """
(() => {
  let virtualNow = 0;
  let rafQueue = [];
  performance.now = () => virtualNow;
  Date.now = () => 1700000000000 + virtualNow;
  window.requestAnimationFrame = (cb) => {
    rafQueue.push(cb);
    return rafQueue.length;
  };
  window.__setVirtualTime = (targetMs) => {
    virtualNow = targetMs;
    if (typeof window.renderAtTime === 'function') {
      window.renderAtTime(virtualNow);
    }
    const pending = rafQueue;
    rafQueue = [];
    for (const cb of pending) {
      cb(virtualNow);
    }
  };
})();
"""


def _launch_available_chromium(playwright_obj):
    """Önce Playwright'ın Chromium'u, sonra kurulu Edge ve Chrome kanalları denenir."""
    last_err: Optional[Exception] = None
    for channel in (None, "msedge", "chrome"):
        try:
            kwargs = {"headless": True}
            if channel is not None:
                kwargs["channel"] = channel
            return playwright_obj.chromium.launch(**kwargs)
        except Exception as exc:
            last_err = exc
    raise RuntimeError(
        "Başsız tarayıcı başlatılamadı. `python -m playwright install chromium` çalıştırın ya da Edge/Chrome kurun. "
        f"Son hata: {last_err}"
    )


def capture_html_scene_to_gif(
    html_content: str,
    sample_times_ms: Sequence[float],
    out_path: str | Path,
    *,
    poster_index: int = 0,
    highlight_indices: Sequence[int] = (),
    state_label_fn: Optional[Callable[[int, float], str]] = None,
    min_dwell_ms: int = 1750,
    width: int = 1132,
    height: int = 652,
    canvas_selector: str = "canvas",
    fps: int = 18,
) -> Path:
    """
    ``window.renderAtTime(t_ms)`` tanımlayan kendi kendine yeten bir HTML sayfasını ``sample_times_ms`` anlarında
    yakalar. Kare 0 ``sample_times_ms[poster_index]`` anının posteridir; ``highlight_indices`` ve
    ``state_label_fn``'in aldığı dizin ``sample_times_ms`` içindeki dizindir.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ImportError(
            "webgl_capture için Playwright gerekli: `pip install playwright` ve `python -m playwright install chromium`."
        ) from exc

    raw_frames: List[np.ndarray] = []
    state_labels: List[str] = []
    bayer = make_bayer_dither_field(height, width, amplitude_lsb=1.9)
    ordered_indices = [poster_index, *range(len(sample_times_ms))]

    with sync_playwright() as p:
        browser = _launch_available_chromium(p)
        try:
            # offline: sahne kendi kendine yeter, ağa çıkamaz (page.route WebSocket'i kesmez; sahte adres yine karşılanır).
            page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1, offline=True)
            # add_init_script yalnızca gezinmede çalışır, set_content gezinmez: sahne sahte bir adresten yüklenir.
            page.add_init_script(VIRTUAL_CLOCK_INIT_JS)
            page.route(_SCENE_URL, lambda route: route.fulfill(body=html_content, content_type="text/html"))
            page.goto(_SCENE_URL, wait_until="load")
            locator = page.locator(canvas_selector)
            for idx in ordered_indices:
                t_val = float(sample_times_ms[idx])
                page.evaluate("(t) => window.__setVirtualTime(t)", t_val)
                png_bytes = locator.screenshot(type="png")
                with Image.open(io.BytesIO(png_bytes)) as shot:
                    rgb = np.asarray(shot.convert("RGB"), dtype=np.float32) / 255.0
                raw_frames.append(np.clip((rgb + bayer[: rgb.shape[0], : rgb.shape[1]]) * 255.0, 0.0, 255.0)
                                  .astype(np.uint8))
                state_labels.append(state_label_fn(idx, t_val) if state_label_fn is not None else "")
        finally:
            browser.close()

    # Kare 0 poster olduğundan sample_times_ms dizini k, GIF'te k + 1. karedir.
    anchors = [int(k) + 1 for k in highlight_indices]
    base_durs = build_pacing_schedule(
        len(raw_frames), fps=fps, poster_hold_ms=1450, highlight_indices=anchors, highlight_hold_ms=1200,
        end_hold_ms=1600,
    )
    final_durs = enforce_minimum_dwell(base_durs, state_labels, min_dwell_ms=min_dwell_ms, anchor_indices=anchors)
    return encode_master_palette_gif(raw_frames, final_durs, out_path, state_labels=state_labels, method="fastoctree")
