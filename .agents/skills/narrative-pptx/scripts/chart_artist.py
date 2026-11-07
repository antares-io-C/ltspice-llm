"""
Açıklamalı grafik çizici (slayt kartlarıyla aynı renk ve yazı tipleri)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Eğrinin üstündeki noktaya okla bağlı açıklama balonu, yatay referans çizgisi ve aşama bölgesi (vspan) çizen,
slayta konacak PNG üretir. pyplot'un genel durumu yerine nesne tabanlı ``Figure`` kullanır. Tema yazı tipi bu
makinede yoksa matplotlib DejaVu Sans'a düşer (``.pptx`` içindeki yazı tipi adı değişmez).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Sequence, Tuple

from matplotlib import font_manager
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

if __package__:
    from .themes import DeckTheme, get_theme
else:
    from themes import DeckTheme, get_theme


@lru_cache(maxsize=None)
def mpl_font(name: str) -> str:
    """Yazı tipi kuruluysa adını, değilse ``"DejaVu Sans"`` döndürür (eksik font uyarısı basılmaz)."""
    return name if name in {f.name for f in font_manager.fontManager.ttflist} else "DejaVu Sans"


def _resolve_color(color_or_role: str, theme: DeckTheme) -> str:
    if color_or_role.startswith("#"):
        return color_or_role
    if color_or_role in theme.accents:
        return theme.accents[color_or_role].primary
    return theme.ink_muted


def style_axis(ax: Any, theme: DeckTheme, title: str = "", xlabel: str = "", ylabel: str = "") -> None:
    """Tufte veri-mürekkep oranına uygun temiz eksen biçimlendirmesi."""
    ax.set_facecolor(theme.card_bg)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#CBD5E1")
    ax.spines["bottom"].set_color("#CBD5E1")
    ax.spines["left"].set_linewidth(0.9)
    ax.spines["bottom"].set_linewidth(0.9)
    ax.grid(True, axis="y", color="#E2E8F0", linestyle="-", linewidth=0.75, alpha=0.9)
    ax.grid(False, axis="x")
    ax.set_axisbelow(True)
    ax.tick_params(axis="both", colors=theme.ink_muted, labelsize=9.5, length=3, width=0.8)
    for lbl in ax.get_xticklabels() + ax.get_yticklabels():
        lbl.set_fontfamily(mpl_font(theme.font_body))
    if title:
        ax.set_title(
            title,
            loc="left",
            fontsize=11.0,
            fontweight="bold",
            fontfamily=mpl_font(theme.font_heading),
            color=theme.ink,
            pad=8,
        )
    if xlabel:
        ax.set_xlabel(xlabel, fontsize=9.8, fontfamily=mpl_font(theme.font_body), color=theme.ink_secondary, labelpad=4)
    if ylabel:
        ax.set_ylabel(ylabel, fontsize=9.8, fontfamily=mpl_font(theme.font_body), color=theme.ink_secondary, labelpad=5)


def add_callout(
    ax: Any,
    xy: Tuple[float, float],
    text: str,
    offset_pts: Tuple[float, float],
    theme: DeckTheme,
    role: str = "blue",
    rad: float = 0.18,
    fontsize: float = 9.0,
) -> None:
    """Eğri üzerindeki tam `(x, y)` noktasına işaret noktası ve okla bağlı açıklama kutusu ekler."""
    acc = theme.accent(role)
    ax.plot([xy[0]], [xy[1]], marker="o", markersize=5.8, color=acc.primary, markeredgecolor="#FFFFFF",
            markeredgewidth=1.4, zorder=7)
    ax.annotate(
        text,
        xy=xy,
        xytext=offset_pts,
        textcoords="offset points",
        fontsize=fontsize,
        fontfamily=mpl_font(theme.font_body),
        fontweight="normal",
        color=theme.ink,
        linespacing=1.22,
        zorder=8,
        bbox={
            "boxstyle": "round,pad=0.42,rounding_size=0.35",
            "facecolor": acc.soft_bg,
            "edgecolor": acc.primary,
            "linewidth": 1.0,
        },
        arrowprops={
            "arrowstyle": "-|>,head_length=0.45,head_width=0.25",
            "color": acc.primary,
            "lw": 1.15,
            "connectionstyle": f"arc3,rad={rad}",
            "shrinkA": 2,
            "shrinkB": 4,
        },
    )


def render_annotated_panels(
    out_path: str | Path,
    panels: Sequence[Dict[str, Any]],
    figsize: Tuple[float, float] = (7.55, 4.35),
    dpi: int = 300,
    theme_name: str | None = None,
    sharex: bool = True,
) -> Path:
    """
    Bir veya birden çok senkronize grafik panelini yüksek çözünürlüklü PNG olarak çizer.

    Her panel sözlüğü şunları içerebilir:
    - ``title``, ``xlabel``, ``ylabel``, ``xlim``, ``ylim``
    - ``traces``: ``[{"x": ..., "y": ..., "label": "...", "color": "blue", "width": 2.3, "style": "-", "fill": False}]``
    - ``hlines``: ``[{"y": 1.0, "label": "referans", "color": "slate", "align": "right"}]``
    - ``vspans``: ``[{"x0": 0, "x1": 2, "label": "1. aşama", "role": "blue", "alpha": 0.06}]``
    - ``callouts``: ``[{"xy": (x, y), "text": "...", "offset": (dx, dy), "role": "blue", "rad": 0.18}]``
    - ``legend_loc``: ``"upper right"`` vb. (``None`` ise lejant gizlenir)
    """
    theme = get_theme(theme_name)
    fig = Figure(figsize=figsize, dpi=dpi, facecolor=theme.card_bg)
    FigureCanvasAgg(fig)
    axes = fig.subplots(len(panels), 1, sharex=sharex if len(panels) > 1 else False)
    ax_list = [axes] if len(panels) == 1 else list(axes)

    for idx, (ax, spec) in enumerate(zip(ax_list, panels)):
        is_bottom = idx == len(panels) - 1
        style_axis(
            ax,
            theme,
            title=spec.get("title", ""),
            xlabel=spec.get("xlabel", "") if is_bottom or not sharex else "",
            ylabel=spec.get("ylabel", ""),
        )

        if "xlim" in spec and spec["xlim"] is not None:
            ax.set_xlim(*spec["xlim"])
        if "ylim" in spec and spec["ylim"] is not None:
            ax.set_ylim(*spec["ylim"])

        for span in spec.get("vspans", ()):
            acc = theme.accent(span.get("role", "slate"))
            ax.axvspan(span["x0"], span["x1"], color=acc.primary, alpha=span.get("alpha", 0.055), zorder=1)
            if span.get("label"):
                y_top = ax.get_ylim()[1] if "ylim" in spec and spec["ylim"] else None
                ax.text(
                    (span["x0"] + span["x1"]) / 2.0,
                    0.95,
                    span["label"],
                    transform=ax.get_xaxis_transform(),
                    ha="center",
                    va="top",
                    fontsize=8.6,
                    fontweight="bold",
                    fontfamily=mpl_font(theme.font_body),
                    color=acc.primary,
                    zorder=5,
                )
                _ = y_top

        for hl in spec.get("hlines", ()):
            col = _resolve_color(hl.get("color", "slate"), theme)
            ax.axhline(hl["y"], color=col, linestyle="--", linewidth=0.95, alpha=0.75, zorder=2)
            if hl.get("label"):
                align = hl.get("align", "right")
                va = hl.get("va", "bottom")
                x_frac = 0.985 if align == "right" else 0.015
                dy_pts = float(hl.get("dy_pts", 2.5 if va == "bottom" else -2.5))
                ax.annotate(
                    hl["label"],
                    xy=(x_frac, hl["y"]),
                    xycoords=ax.get_yaxis_transform(),
                    xytext=(0.0, dy_pts),
                    textcoords="offset points",
                    ha=align,
                    va=va,
                    fontsize=8.5,
                    fontfamily=mpl_font(theme.font_body),
                    color=col,
                    bbox={"boxstyle": "round,pad=0.18", "facecolor": theme.card_bg, "edgecolor": "none", "alpha": 0.92},
                    zorder=6,
                )

        has_labels = False
        for tr in spec.get("traces", ()):
            col = _resolve_color(tr.get("color", "blue"), theme)
            lbl = tr.get("label")
            if lbl:
                has_labels = True
            lw = float(tr.get("width", 2.3))
            ls = tr.get("style", "-")
            alpha = float(tr.get("alpha", 1.0))
            zorder = int(tr.get("zorder", 4))
            ax.plot(tr["x"], tr["y"], label=lbl, color=col, linewidth=lw, linestyle=ls, alpha=alpha, zorder=zorder)
            if tr.get("fill"):
                ax.fill_between(tr["x"], 0, tr["y"], color=col, alpha=0.08, zorder=2)

        for co in spec.get("callouts", ()):
            add_callout(
                ax,
                xy=(float(co["xy"][0]), float(co["xy"][1])),
                text=str(co["text"]),
                offset_pts=(float(co["offset"][0]), float(co["offset"][1])),
                theme=theme,
                role=co.get("role", "blue"),
                rad=float(co.get("rad", 0.18)),
                fontsize=float(co.get("fontsize", 8.8)),
            )

        legend_loc = spec.get("legend_loc", "upper right")
        if has_labels and legend_loc is not None:
            leg = ax.legend(
                loc=legend_loc,
                frameon=True,
                facecolor=theme.card_bg,
                edgecolor="#E2E8F0",
                framealpha=0.95,
                fontsize=8.8,
            )
            for txt in leg.get_texts():
                txt.set_fontfamily(mpl_font(theme.font_body))
                txt.set_color(theme.ink_secondary)

    fig.tight_layout(pad=0.65, h_pad=0.95)
    target = Path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(target), dpi=dpi, facecolor=theme.card_bg)
    return target
