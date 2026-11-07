"""
Genel GIF arketipleri (herhangi bir zaman izi ya da eğri ailesi)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
1. ``render_scope_sweep_gif``: bir ya da birkaç panelde izleri soldan sağa çizen osiloskop taraması (açık tema).
2. ``render_phosphor_scope_gif``: aynı girdiyle koyu zemin + ışıma (HDR) teması.
3. ``render_parameter_morph_gif``: bir parametrenin değerleri arasında eğri ailesinin yumuşak geçişi.

Ortak girdiler:

- ``panels``: ``[{"title": str, "ylabel": str, "ylim": (alt, üst) | None,
  "traces": [{"y": dizi, "label": str, "color": rol}]}]``. Her ``y``, ``t`` ile aynı uzunluktadır.
- ``stages``: durum metinleri, zaman pencereleriyle: ``[{"t": başlangıç, "text": str, "role": rol}]``. Etkin metin,
  ``t``'si geçilen son aşamadır. Metni anlık sinyal eşiğine bağlamak (``v < -0.5``) kısa süre yanıp sönen yazı verir;
  bu API buna izin vermez.
- ``events``: olay balonları ``[{"t": an, "panel": dizin, "xy": (x, y), "offset": (dx, dy) punto, "text": str,
  "role": rol, "rad": eğrilik}]``. Balon tarama ``t``'ye ulaşınca görünür ve döngü sonuna kadar kalır.
- Roller: ``blue``, ``amber``, ``emerald``, ``rose``, ``violet``, ``slate`` (koyu temada ayrıca ``cyan``).

Kare 0 poster karesidir: izlerin tamamı, bütün balonlar ve ``poster_time`` anı. PDF'e ya da küçük resme bu kare
düşer. Ardından tarama kareleri gelir. ``ltspice_llm``'e bağlı değildir; numpy, matplotlib, Pillow (koyu tema için
ayrıca scipy) yeterlidir.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from matplotlib import font_manager
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

if __package__:
    from .gif_encoder import (
        adaptive_event_samples,
        build_pacing_schedule,
        encode_master_palette_gif,
        enforce_minimum_dwell,
    )
else:
    from gif_encoder import (
        adaptive_event_samples,
        build_pacing_schedule,
        encode_master_palette_gif,
        enforce_minimum_dwell,
    )


def _pick_font(*candidates: str) -> str:
    """Kurulu ilk yazı tipini seçer (Windows'ta Segoe UI, başka yerde DejaVu Sans); eksik font uyarısı basmaz."""
    names = {f.name for f in font_manager.fontManager.ttflist}
    return next((c for c in candidates if c in names), "DejaVu Sans")


FONT_UI = _pick_font("Segoe UI", "Helvetica Neue", "Arial", "DejaVu Sans")

PALETTE: Dict[str, str] = {
    "ink": "#0F172A",
    "ink_sec": "#334155",
    "muted": "#64748B",
    "border": "#CBD5E1",
    "grid": "#E2E8F0",
    "card_alt": "#F8FAFC",
    "blue": "#2563EB",
    "amber": "#D97706",
    "emerald": "#059669",
    "rose": "#E11D48",
    "violet": "#7C3AED",
    "slate": "#475569",
}

SOFT_BG_BY_ROLE: Dict[str, str] = {
    "blue": "#DBEAFE",
    "amber": "#FEF3C7",
    "emerald": "#D1FAE5",
    "rose": "#FFE4E6",
    "violet": "#EDE9FE",
    "slate": "#F1F5F9",
}


# --------------------------------------------------------------------------- ortak girdi işleme
def _normalize_panels(t_arr: np.ndarray, panels: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not panels:
        raise ValueError("En az bir panel gerekli.")
    out: List[Dict[str, Any]] = []
    for p_idx, panel in enumerate(panels):
        traces = []
        for tr in panel.get("traces", ()):
            y = np.asarray(tr["y"], dtype=float)
            if y.shape != t_arr.shape:
                raise ValueError(f"Panel {p_idx}: iz uzunluğu {y.shape} zaman ekseniyle {t_arr.shape} aynı değil.")
            traces.append({"y": y, "label": str(tr.get("label", "")), "color": str(tr.get("color", "blue"))})
        if not traces:
            raise ValueError(f"Panel {p_idx}: en az bir iz (traces) gerekli.")
        ylim = panel.get("ylim")
        if ylim is None:
            lo = min(float(np.min(tr["y"])) for tr in traces)
            hi = max(float(np.max(tr["y"])) for tr in traces)
            pad = max((hi - lo) * 0.12, abs(hi) * 0.05, 1e-9)
            ylim = (lo - pad, hi + pad)
        out.append({"title": str(panel.get("title", "")), "ylabel": str(panel.get("ylabel", "")),
                    "ylim": (float(ylim[0]), float(ylim[1])), "traces": traces})
    return out


def _auto_transient_windows(t_arr: np.ndarray, ys: Sequence[np.ndarray], pad_frac: float = 0.08) -> List[Tuple[float, float]]:
    """Normalize eğimi yüksek bölgeleri (dik kenar, tepe) ağır çekim penceresi olarak döndürür."""
    if len(t_arr) < 8:
        return []
    span = float(t_arr[-1] - t_arr[0])
    if span <= 0.0:
        return []
    grid = np.linspace(float(t_arr[0]), float(t_arr[-1]), 400)
    score = np.zeros_like(grid)
    for y in ys:
        slope = np.abs(np.gradient(np.interp(grid, t_arr, y)))
        score += slope / max(float(np.max(slope)), 1e-12)
    active = score / max(len(ys), 1) > 0.16

    windows: List[Tuple[float, float]] = []
    start: Optional[float] = None
    pad = span * pad_frac
    for g, flag in zip(grid, active):
        if flag and start is None:
            start = max(float(t_arr[0]), float(g) - pad * 0.25)
        elif not flag and start is not None:
            windows.append((start, min(float(t_arr[-1]), float(g) + pad)))
            start = None
    if start is not None:
        windows.append((start, float(t_arr[-1])))
    return windows


def _stage_at(stages: Sequence[Dict[str, Any]], tk: float) -> Tuple[str, str]:
    text, role = "", "slate"
    for st in stages:
        if tk >= float(st["t"]) - 1e-12:
            text, role = str(st.get("text", "")), str(st.get("role", "slate"))
    return text, role


def _sweep_plan(
    t_arr: np.ndarray,
    panels: List[Dict[str, Any]],
    events: Sequence[Dict[str, Any]],
    event_windows: Optional[Sequence[Tuple[float, float]]],
    n_sweep_frames: int,
    poster_time: Optional[float],
    highlight_times: Sequence[float],
) -> Tuple[np.ndarray, List[int], float]:
    """Tarama örnek anları, vurgu kare dizinleri (kare 0 poster olduğundan +1) ve poster anı."""
    if n_sweep_frames < 2:
        raise ValueError("n_sweep_frames en az 2 olmalı.")
    t0, t1 = float(t_arr[0]), float(t_arr[-1])
    ys = [tr["y"] for p in panels for tr in p["traces"]]
    wins = list(event_windows) if event_windows is not None else _auto_transient_windows(t_arr, ys)
    snaps = [float(h) for h in highlight_times] + [float(ev["t"]) for ev in events]
    if poster_time is not None:
        snaps.append(float(poster_time))
    sample_t = adaptive_event_samples(t0, t1, n_sweep_frames, event_windows=wins, snap_points=snaps)
    marks = [float(h) for h in highlight_times] + [float(ev["t"]) for ev in events]
    anchors = sorted({int(np.argmin(np.abs(sample_t - m))) + 1 for m in marks})
    return sample_t, anchors, (t1 if poster_time is None else float(poster_time))


def _finish_gif(
    frames: List[np.ndarray],
    labels: List[str],
    anchors: Sequence[int],
    out_path: str | Path,
    fps: int,
    min_text_dwell_ms: int,
    method: str,
) -> Path:
    durations = build_pacing_schedule(len(frames), fps=fps, poster_hold_ms=1400, highlight_indices=anchors,
                                      highlight_hold_ms=1150, end_hold_ms=1550)
    durations = enforce_minimum_dwell(durations, labels, min_dwell_ms=min_text_dwell_ms, anchor_indices=anchors)
    return encode_master_palette_gif(frames, durations, out_path, state_labels=labels, method=method)


def _capture_rgb(canvas: FigureCanvasAgg) -> np.ndarray:
    canvas.draw()
    return np.asarray(canvas.buffer_rgba())[:, :, :3].copy()


def _capture_rgba(canvas: FigureCanvasAgg) -> np.ndarray:
    canvas.draw()
    return np.asarray(canvas.buffer_rgba()).copy()


def _panel_gridspec(fig: Figure, n: int, has_title: bool, has_stages: bool):
    top = 0.93 - (0.06 if has_title else 0.0) - (0.06 if has_stages else 0.0)
    return fig.add_gridspec(n, 1, left=0.095, right=0.975, top=top, bottom=0.12, hspace=0.45)


# --------------------------------------------------------------------------- açık tema
def _style_scope_ax(ax: Any, title: str, ylabel: str, xlabel: str = "") -> None:
    ax.set_facecolor("#FFFFFF")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(PALETTE["border"])
    ax.grid(True, axis="y", color=PALETTE["grid"], linewidth=0.75)
    ax.set_axisbelow(True)
    ax.tick_params(axis="both", colors=PALETTE["muted"], labelsize=8.6, length=3)
    for lbl in ax.get_xticklabels() + ax.get_yticklabels():
        lbl.set_fontfamily(FONT_UI)
    if title:
        ax.set_title(title, loc="left", fontsize=9.8, fontweight="bold", fontfamily=FONT_UI, color=PALETTE["ink"],
                     pad=6)
    ax.set_ylabel(ylabel, fontsize=8.8, fontfamily=FONT_UI, color=PALETTE["ink_sec"], labelpad=4)
    if xlabel:
        ax.set_xlabel(xlabel, fontsize=8.8, fontfamily=FONT_UI, color=PALETTE["ink_sec"], labelpad=3)


def _add_latched_callout(ax: Any, ev: Dict[str, Any], dark: bool = False) -> Tuple[Any, Any]:
    """Olay anında görünüp döngü sonuna kadar kalan nokta + açıklama balonu (başta gizli)."""
    role = str(ev.get("role", "rose"))
    if dark:
        if __package__:
            from .hdr_compositor import CINEMA_PALETTE, HUD_BG_BY_ROLE
        else:
            from hdr_compositor import CINEMA_PALETTE, HUD_BG_BY_ROLE
        fg, bg, ink = CINEMA_PALETTE.get(role, CINEMA_PALETTE["cyan"]), HUD_BG_BY_ROLE.get(role, "#0F172A"), \
            CINEMA_PALETTE["ink"]
    else:
        fg, bg, ink = PALETTE.get(role, PALETTE["blue"]), SOFT_BG_BY_ROLE.get(role, "#F8FAFC"), PALETTE["ink"]
    xy = (float(ev["xy"][0]), float(ev["xy"][1]))
    (marker,) = ax.plot([xy[0]], [xy[1]], marker="o", markersize=5.8, color=fg, markeredgecolor="#FFFFFF",
                        markeredgewidth=1.3, zorder=8, visible=False)
    offset = ev.get("offset", (24, 18))
    ann = ax.annotate(
        str(ev["text"]), xy=xy, xytext=(float(offset[0]), float(offset[1])), textcoords="offset points",
        fontsize=8.2, fontfamily=FONT_UI, fontweight="bold", color=ink, linespacing=1.18, zorder=9, visible=False,
        bbox={"boxstyle": "round,pad=0.34,rounding_size=0.30", "facecolor": bg, "edgecolor": fg, "linewidth": 1.1},
        arrowprops={"arrowstyle": "-|>,head_length=0.42,head_width=0.24", "color": fg, "lw": 1.1,
                    "connectionstyle": f"arc3,rad={float(ev.get('rad', 0.16))}", "shrinkA": 2, "shrinkB": 4},
    )
    return marker, ann


def render_scope_sweep_gif(
    out_path: str | Path,
    t: Sequence[float],
    panels: Sequence[Dict[str, Any]],
    *,
    title: str = "",
    xlabel: str = "",
    stages: Sequence[Dict[str, Any]] = (),
    events: Sequence[Dict[str, Any]] = (),
    event_windows: Optional[Sequence[Tuple[float, float]]] = None,
    n_sweep_frames: int = 52,
    poster_time: Optional[float] = None,
    highlight_times: Sequence[float] = (),
    min_text_dwell_ms: int = 1750,
    fps: int = 18,
    figsize: Tuple[float, float] = (7.55, 4.35),
    dpi: int = 150,
) -> Path:
    """
    İzleri imleçle soldan sağa çizen açık temalı osiloskop GIF'i. ``event_windows`` verilmezse dik bölgeler
    eğimden bulunur ve orada kareler sıklaşır. Kare sayısı ``n_sweep_frames + 1`` (poster dahil).
    """
    t_arr = np.asarray(t, dtype=float)
    pnl = _normalize_panels(t_arr, panels)
    sample_t, anchors, t_poster = _sweep_plan(t_arr, pnl, events, event_windows, n_sweep_frames, poster_time,
                                              highlight_times)

    fig = Figure(figsize=figsize, dpi=dpi, facecolor="#FFFFFF")
    canvas = FigureCanvasAgg(fig)
    gs = _panel_gridspec(fig, len(pnl), bool(title), bool(stages))
    if title:
        fig.text(0.025, 0.955, title, ha="left", va="center", fontsize=11, fontweight="bold", fontfamily=FONT_UI,
                 color=PALETTE["ink"])
    stage_txt = None
    if stages:
        stage_txt = fig.text(0.025, 0.955 - (0.065 if title else 0.0), "", ha="left", va="center", fontsize=9.4,
                             fontweight="bold", fontfamily=FONT_UI, color=PALETTE["ink"],
                             bbox={"boxstyle": "round,pad=0.30", "facecolor": "#F1F5F9", "edgecolor": "none"})

    live: List[Tuple[np.ndarray, Any, Any]] = []
    cursors = []
    callouts: List[Tuple[float, Any, Any]] = []
    for p_idx, panel in enumerate(pnl):
        ax = fig.add_subplot(gs[p_idx, 0])
        _style_scope_ax(ax, panel["title"], panel["ylabel"], xlabel if p_idx == len(pnl) - 1 else "")
        ax.set_xlim(float(t_arr[0]), float(t_arr[-1]))
        ax.set_ylim(*panel["ylim"])
        for tr in panel["traces"]:
            col = PALETTE.get(tr["color"], tr["color"])
            ax.plot(t_arr, tr["y"], color=col, lw=1.3, ls="--", alpha=0.24, zorder=2)   # soluk referans iz
            (line,) = ax.plot([], [], color=col, lw=2.4, zorder=4, label=tr["label"] or None)
            (dot,) = ax.plot([], [], "o", ms=6.0, color=col, mec="#FFFFFF", mew=1.3, zorder=6)
            live.append((tr["y"], line, dot))
        if sum(1 for tr in panel["traces"] if tr["label"]) > 1:
            ax.legend(loc="upper right", fontsize=7.6, frameon=False, prop={"family": FONT_UI, "size": 7.6})
        cursors.append(ax.axvline(float(t_arr[0]), color=PALETTE["ink_sec"], lw=1.0, ls=":", alpha=0.75, zorder=3))
        for ev in events:
            if int(ev.get("panel", 0)) == p_idx:
                marker, ann = _add_latched_callout(ax, ev)
                callouts.append((float(ev["t"]), marker, ann))

    def draw(tk: float, full: bool) -> str:
        mask = t_arr <= tk + 1e-12
        for y, line, dot in live:
            yk = float(np.interp(tk, t_arr, y))
            if full:
                line.set_data(t_arr, y)
            else:
                line.set_data(np.append(t_arr[mask], tk), np.append(y[mask], yk))
            dot.set_data([tk], [yk])
        for cur in cursors:
            cur.set_xdata([tk, tk])
        for ev_t, marker, ann in callouts:
            show = full or tk >= ev_t - 1e-12
            marker.set_visible(show)
            ann.set_visible(show)
        text, role = _stage_at(stages, tk)
        if stage_txt is not None:
            stage_txt.set_text(text)
            stage_txt.set_color(PALETTE.get(role, PALETTE["ink"]))
            stage_txt.set_visible(bool(text))
        return text

    frames = []
    labels = [draw(t_poster, full=True)]
    frames.append(_capture_rgb(canvas))
    for tk in sample_t:
        labels.append(draw(float(tk), full=False))
        frames.append(_capture_rgb(canvas))
    return _finish_gif(frames, labels, anchors, out_path, fps, min_text_dwell_ms, method="mediancut")


# --------------------------------------------------------------------------- koyu tema (HDR ışıma)
def render_phosphor_scope_gif(
    out_path: str | Path,
    t: Sequence[float],
    panels: Sequence[Dict[str, Any]],
    *,
    title: str = "",
    xlabel: str = "",
    stages: Sequence[Dict[str, Any]] = (),
    events: Sequence[Dict[str, Any]] = (),
    event_windows: Optional[Sequence[Tuple[float, float]]] = None,
    n_sweep_frames: int = 50,
    poster_time: Optional[float] = None,
    highlight_times: Sequence[float] = (),
    min_text_dwell_ms: int = 1800,
    fps: int = 18,
    figsize: Tuple[float, float] = (7.55, 4.35),
    dpi: int = 150,
) -> Path:
    """
    ``render_scope_sweep_gif`` ile aynı girdiler; koyu zemin, ışıyan izler, ışımadan sonra bindirilen keskin yazılar.
    Varsayılan iz rengi ``cyan``. scipy gerektirir.
    """
    if __package__:
        from .hdr_compositor import CINEMA_PALETTE, blend_static_base, build_studio_vignette, compose_hdr_frame, \
            make_bayer_dither_field
    else:
        from hdr_compositor import CINEMA_PALETTE, blend_static_base, build_studio_vignette, compose_hdr_frame, \
            make_bayer_dither_field

    t_arr = np.asarray(t, dtype=float)
    pnl = _normalize_panels(t_arr, panels)
    sample_t, anchors, t_poster = _sweep_plan(t_arr, pnl, events, event_windows, n_sweep_frames, poster_time,
                                              highlight_times)

    fig_base = Figure(figsize=figsize, dpi=dpi)
    fig_emit = Figure(figsize=figsize, dpi=dpi, facecolor="#000000")
    fig_hud = Figure(figsize=figsize, dpi=dpi)
    fig_base.patch.set_alpha(0.0)
    fig_hud.patch.set_alpha(0.0)
    c_base, c_emit, c_hud = FigureCanvasAgg(fig_base), FigureCanvasAgg(fig_emit), FigureCanvasAgg(fig_hud)
    grids = [_panel_gridspec(f, len(pnl), bool(title), bool(stages)) for f in (fig_base, fig_emit, fig_hud)]

    if title:
        fig_hud.text(0.025, 0.955, title, ha="left", va="center", fontsize=11, fontweight="bold", fontfamily=FONT_UI,
                     color=CINEMA_PALETTE["ink"])
    stage_txt = None
    if stages:
        stage_txt = fig_hud.text(0.025, 0.955 - (0.065 if title else 0.0), "", ha="left", va="center", fontsize=9.4,
                                 fontweight="bold", fontfamily=FONT_UI, color=CINEMA_PALETTE["ink"],
                                 bbox={"boxstyle": "round,pad=0.30", "facecolor": CINEMA_PALETTE["bg_hud"],
                                       "edgecolor": CINEMA_PALETTE["grid_hi"]})

    live: List[Tuple[np.ndarray, Any, Any, Any]] = []
    cursors = []
    callouts: List[Tuple[float, Any, Any]] = []
    for p_idx, panel in enumerate(pnl):
        ax_b, ax_e, ax_h = (f.add_subplot(g[p_idx, 0]) for f, g in zip((fig_base, fig_emit, fig_hud), grids))
        for ax in (ax_b, ax_e, ax_h):
            ax.set_xlim(float(t_arr[0]), float(t_arr[-1]))
            ax.set_ylim(*panel["ylim"])
        ax_b.set_facecolor(CINEMA_PALETTE["bg_card"])
        for side in ("top", "right"):
            ax_b.spines[side].set_visible(False)
            ax_h.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax_b.spines[side].set_color(CINEMA_PALETTE["grid_hi"])
            ax_h.spines[side].set_color(CINEMA_PALETTE["grid_hi"])
        ax_b.grid(True, axis="y", color=CINEMA_PALETTE["grid"], linewidth=0.75)
        ax_b.tick_params(left=False, bottom=False, labelleft=False, labelbottom=False)
        ax_e.axis("off")
        ax_e.patch.set_visible(False)
        ax_h.patch.set_visible(False)
        ax_h.tick_params(axis="both", colors=CINEMA_PALETTE["muted"], labelsize=8.4, length=3)
        for lbl in ax_h.get_xticklabels() + ax_h.get_yticklabels():
            lbl.set_fontfamily(FONT_UI)
        if panel["title"]:
            ax_h.set_title(panel["title"], loc="left", fontsize=9.5, fontweight="bold", fontfamily=FONT_UI,
                           color=CINEMA_PALETTE["ink"], pad=6)
        ax_h.set_ylabel(panel["ylabel"], fontsize=8.6, fontfamily=FONT_UI, color=CINEMA_PALETTE["ink_sec"])
        if p_idx == len(pnl) - 1 and xlabel:
            ax_h.set_xlabel(xlabel, fontsize=8.6, fontfamily=FONT_UI, color=CINEMA_PALETTE["ink_sec"])
        for tr in panel["traces"]:
            role = tr["color"] if tr["color"] in CINEMA_PALETTE else "cyan"
            col = CINEMA_PALETTE[role]
            (glow,) = ax_e.plot([], [], color=col, lw=3.2, zorder=4)
            (core,) = ax_e.plot([], [], color="#FFFFFF", lw=1.1, zorder=5)
            (dot,) = ax_e.plot([], [], "o", ms=7.0, color=col, mec="#FFFFFF", mew=1.3, zorder=6)
            live.append((tr["y"], glow, core, dot))
            ax_h.plot([], [], color=col, lw=2.4, label=tr["label"] or None)   # yalnızca lejant için
        if sum(1 for tr in panel["traces"] if tr["label"]) > 1:
            legend = ax_h.legend(loc="upper right", frameon=False, prop={"family": FONT_UI, "size": 7.6})
            for text in legend.get_texts():
                text.set_color(CINEMA_PALETTE["ink_sec"])
        cursors.append(ax_h.axvline(float(t_arr[0]), color=CINEMA_PALETTE["muted"], lw=0.9, ls=":", alpha=0.7))
        for ev in events:
            if int(ev.get("panel", 0)) == p_idx:
                marker, ann = _add_latched_callout(ax_h, {"role": "cyan", **ev}, dark=True)
                callouts.append((float(ev["t"]), marker, ann))

    base_rgba = _capture_rgba(c_base)   # boyut tampondan: Agg inç × dpi'yi kırpar, round() bir piksel fazla verebilir
    px_h, px_w = base_rgba.shape[:2]
    static_base = blend_static_base(build_studio_vignette(px_h, px_w), base_rgba)
    bayer = make_bayer_dither_field(px_h, px_w, amplitude_lsb=2.1)

    def draw(tk: float, full: bool) -> str:
        mask = t_arr <= tk + 1e-12
        for y, glow, core, dot in live:
            yk = float(np.interp(tk, t_arr, y))
            xs, ys = (t_arr, y) if full else (np.append(t_arr[mask], tk), np.append(y[mask], yk))
            glow.set_data(xs, ys)
            core.set_data(xs, ys)
            dot.set_data([tk], [yk])
        for cur in cursors:
            cur.set_xdata([tk, tk])
        for ev_t, marker, ann in callouts:
            show = full or tk >= ev_t - 1e-12
            marker.set_visible(show)
            ann.set_visible(show)
        text, role = _stage_at(stages, tk)
        if stage_txt is not None:
            stage_txt.set_text(text)
            stage_txt.set_color(CINEMA_PALETTE.get(role, CINEMA_PALETTE["ink"]))
            stage_txt.set_visible(bool(text))
        return text

    def frame() -> np.ndarray:
        return compose_hdr_frame(static_base, _capture_rgb(c_emit), _capture_rgba(c_hud), bayer_cache=bayer)

    labels = [draw(t_poster, full=True)]
    frames = [frame()]
    for tk in sample_t:
        labels.append(draw(float(tk), full=False))
        frames.append(frame())
    return _finish_gif(frames, labels, anchors, out_path, fps, min_text_dwell_ms, method="fastoctree")


# --------------------------------------------------------------------------- parametre geçişi
def render_parameter_morph_gif(
    out_path: str | Path,
    x_common: Sequence[float],
    cases: Sequence[Dict[str, Any]],
    *,
    title: str = "",
    xlabel: str = "",
    ylabel: str = "",
    ylim: Optional[Tuple[float, float]] = None,
    interp_steps: int = 12,
    min_text_dwell_ms: int = 1800,
    fps: int = 18,
    figsize: Tuple[float, float] = (7.55, 4.35),
    dpi: int = 150,
) -> Path:
    """
    ``cases = [{"label": "R = 1 kΩ", "y": dizi, "note": "ölçülen değer", "color": rol}, ...]`` arasında yumuşak geçiş.
    Her durumun etiketi en az ``min_text_dwell_ms`` görünür. Kare sayısı ``(len(cases) - 1) * interp_steps + 1``.
    """
    if len(cases) < 2:
        raise ValueError("render_parameter_morph_gif en az 2 durum (case) bekler.")
    x_arr = np.asarray(x_common, dtype=float)
    ys = [np.asarray(c["y"], dtype=float) for c in cases]
    for idx, y in enumerate(ys):
        if y.shape != x_arr.shape:
            raise ValueError(f"Durum {idx}: eğri uzunluğu {y.shape} x ekseniyle {x_arr.shape} aynı değil.")

    fig = Figure(figsize=figsize, dpi=dpi, facecolor="#FFFFFF")
    canvas = FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    fig.subplots_adjust(left=0.09, right=0.97, top=0.88, bottom=0.14)
    _style_scope_ax(ax, title, ylabel, xlabel)
    ax.set_xlim(float(x_arr[0]), float(x_arr[-1]))
    if ylim is not None:
        ax.set_ylim(*ylim)
    else:
        lo = min(float(np.min(y)) for y in ys)
        hi = max(float(np.max(y)) for y in ys)
        pad = max((hi - lo) * 0.12, abs(hi) * 0.05, 1e-9)
        ax.set_ylim(lo - pad, hi + pad)

    for c, y in zip(cases, ys):
        ax.plot(x_arr, y, color=PALETTE.get(c.get("color", "slate"), PALETTE["muted"]), lw=1.2, ls="--", alpha=0.25,
                zorder=2)
    (active_line,) = ax.plot(x_arr, ys[0], color=PALETTE["blue"], lw=2.8, zorder=5)
    badge = ax.text(0.97, 0.92, "", transform=ax.transAxes, ha="right", va="top", fontsize=9.2, fontweight="bold",
                    fontfamily=FONT_UI, color=PALETTE["ink"], zorder=6,
                    bbox={"boxstyle": "round,pad=0.32", "facecolor": PALETTE["card_alt"], "edgecolor": PALETTE["border"]})

    def label_of(c: Dict[str, Any]) -> str:
        return f"{c['label']} · {c['note']}" if c.get("note") else str(c["label"])

    frames: List[np.ndarray] = []
    labels: List[str] = []
    anchors: List[int] = []
    for idx in range(len(cases) - 1):
        for step in range(interp_steps):
            s = 0.5 - 0.5 * math.cos(math.pi * step / float(interp_steps))
            active = cases[idx] if s < 0.5 else cases[idx + 1]
            active_line.set_ydata((1.0 - s) * ys[idx] + s * ys[idx + 1])
            active_line.set_color(PALETTE.get(active.get("color", "blue"), PALETTE["blue"]))
            badge.set_text(label_of(active))
            if step == 0:
                anchors.append(len(frames))
            frames.append(_capture_rgb(canvas))
            labels.append(label_of(active))

    last = cases[-1]
    active_line.set_ydata(ys[-1])
    active_line.set_color(PALETTE.get(last.get("color", "emerald"), PALETTE["emerald"]))
    badge.set_text(label_of(last))
    anchors.append(len(frames))
    frames.append(_capture_rgb(canvas))
    labels.append(label_of(last))

    durations = build_pacing_schedule(len(frames), fps=fps, poster_hold_ms=1300, highlight_indices=anchors,
                                      highlight_hold_ms=1100, end_hold_ms=1500)
    durations = enforce_minimum_dwell(durations, labels, min_dwell_ms=min_text_dwell_ms, anchor_indices=anchors,
                                      skip_frame_zero=False)
    return encode_master_palette_gif(frames, durations, out_path, state_labels=labels, skip_frame_zero=False)
