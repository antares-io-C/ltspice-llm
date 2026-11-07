"""
Narrative PPTX Deck Builder (Assertion-Evidence + Bento Grid Engine)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
JSON hikâye şemasından düzenlenebilir (yerel PowerPoint şekilleri ve metin kutuları) .pptx sunumu üretir.
``ltspice_llm``'e bağlı değildir; python-pptx ve Pillow yeterlidir. Şema: ``references/json_schema_guide.md``,
örnek: ``assets/example_spec.json``.

Kullanım (CLI):
    python .agents/skills/narrative-pptx/scripts/deck_builder.py spec.json -o cikti.pptx
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt

if __package__:
    from .themes import DeckTheme, get_theme, hex_to_rgb
else:
    from themes import DeckTheme, get_theme, hex_to_rgb

# 16:9 Widescreen sabitleri (inç)
SLIDE_W = 13.333
SLIDE_H = 7.500
MX = 0.60
CONTENT_W = SLIDE_W - 2 * MX  # 12.133"

#: Şemada verilmeyen alanların varsayılan metni, sunumun diline ("language") göre
_DEFAULT_TEXT = {
    "tr": {"chapter": "SAHNE", "eyebrow": "TEKNİK SUNUM", "title": "Başlık", "scene_title": "SİSTEM",
           "mechanism": "MEKANİZMA", "gif": "▶ CANLI GIF"},
    "en": {"chapter": "SCENE", "eyebrow": "TECHNICAL STORY", "title": "Title", "scene_title": "SYSTEM",
           "mechanism": "MECHANISM", "gif": "▶ LIVE GIF"},
}
#: Düzenin çizdiği en çok öğe; fazlası slayta girmez (spec_warnings bildirir)
ITEM_LIMITS = {("hero_cover", "pillars"): 4, ("mental_model", "characters"): 3, ("evidence_bento", "metrics"): 2,
               ("process_arc", "stages"): 4, ("synthesis_closing", "pillars"): 3}
PLACEHOLDER = "⟨"

_INLINE_TOKEN_RE = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`|\*[^*]+\*)")
_CODE_SUBTOKEN_RE = re.compile(r"(`[^`]+`)")


def _default_text(theme: DeckTheme, key: str) -> str:
    return _DEFAULT_TEXT[theme.language][key]


def spec_warnings(spec: Dict[str, Any]) -> List[str]:
    """Derlenince görünmeyecek ya da yanlış görünecek içerik: sınırı aşan öğeler ve kalan ⟨...⟩ yer tutucuları."""
    out: List[str] = []
    for idx, s_spec in enumerate(spec.get("slides", ()), start=1):
        layout = s_spec.get("layout", "evidence_bento")
        for (name, key), limit in ITEM_LIMITS.items():
            if name == layout and len(s_spec.get(key, ())) > limit:
                out.append(f"Slayt {idx}: {key} {len(s_spec[key])} öğe; {layout} en çok {limit} çizer, fazlası düşer.")
        if PLACEHOLDER in json.dumps(s_spec, ensure_ascii=False):
            out.append(f"Slayt {idx}: {PLACEHOLDER}...⟩ yer tutucusu kaldı; değeri .meas'ten yaz.")
    return out


def _rgb(hex_color: str) -> RGBColor:
    r, g, b = hex_to_rgb(hex_color)
    return RGBColor(r, g, b)


def _set_slide_bg(slide: Any, hex_color: str) -> None:
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = _rgb(hex_color)


def _add_card(
    slide: Any,
    x: float,
    y: float,
    w: float,
    h: float,
    fill_hex: str,
    border_hex: Optional[str] = None,
    border_pt: float = 0.85,
    radius_in: float = 0.11,
) -> Any:
    """Sabit fiziksel köşe yarıçaplı (`radius_in`) modern Bento kartı ekler."""
    shape_type = MSO_SHAPE.ROUNDED_RECTANGLE if radius_in > 0 else MSO_SHAPE.RECTANGLE
    shp = slide.shapes.add_shape(shape_type, Inches(x), Inches(y), Inches(w), Inches(h))
    if radius_in > 0 and shp.adjustments:
        short_side = max(min(w, h), 0.05)
        shp.adjustments[0] = min(0.48, radius_in / short_side)
    shp.fill.solid()
    shp.fill.fore_color.rgb = _rgb(fill_hex)
    if border_hex:
        shp.line.color.rgb = _rgb(border_hex)
        shp.line.width = Pt(border_pt)
    else:
        shp.line.fill.background()
    shp.shadow.inherit = False
    return shp


def _add_accent_bar(slide: Any, x: float, y: float, w: float, h: float, hex_color: str) -> Any:
    """Kart kenarına ya da üstüne ince renkli vurgu şeridi ekler."""
    bar = slide.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    if bar.adjustments:
        bar.adjustments[0] = 0.45
    bar.fill.solid()
    bar.fill.fore_color.rgb = _rgb(hex_color)
    bar.line.fill.background()
    bar.shadow.inherit = False
    return bar


def _write_rich_text(
    tf: Any,
    text: str,
    font_name: str,
    mono_font: str,
    size_pt: float,
    color_hex: str,
    code_color_hex: str,
    bold_default: bool = False,
    align: PP_ALIGN = PP_ALIGN.LEFT,
    line_spacing: float = 1.18,
    space_after_pt: float = 4.0,
) -> None:
    """`**kalın**` ve `` `kod` `` biçimlendirmesini yerel PowerPoint run nesnelerine çevirir."""
    tf.word_wrap = True
    paragraphs = [p for p in str(text).split("\n") if p != ""] or [""]
    for p_idx, para_text in enumerate(paragraphs):
        p = tf.paragraphs[0] if p_idx == 0 else tf.add_paragraph()
        p.alignment = align
        p.line_spacing = line_spacing
        p.space_after = Pt(space_after_pt if p_idx < len(paragraphs) - 1 else 0.0)
        tokens = [tok for tok in _INLINE_TOKEN_RE.split(para_text) if tok]
        if not tokens:
            run = p.add_run()
            run.text = ""
            continue
        for tok in tokens:
            if tok.startswith("**") and tok.endswith("**") and len(tok) > 4:
                inner = tok[2:-2]
                subtokens = [st for st in _CODE_SUBTOKEN_RE.split(inner) if st]
                for st in subtokens:
                    run = p.add_run()
                    if st.startswith("`") and st.endswith("`") and len(st) > 2:
                        run.text = st[1:-1]
                        run.font.name = mono_font
                        run.font.size = Pt(max(size_pt - 0.5, 8.5))
                        run.font.bold = True
                        run.font.color.rgb = _rgb(code_color_hex)
                    else:
                        run.text = st
                        run.font.name = font_name
                        run.font.size = Pt(size_pt)
                        run.font.bold = True
                        run.font.color.rgb = _rgb(color_hex)
            elif tok.startswith("`") and tok.endswith("`") and len(tok) > 2:
                run = p.add_run()
                run.text = tok[1:-1]
                run.font.name = mono_font
                run.font.size = Pt(max(size_pt - 0.5, 8.5))
                run.font.bold = True
                run.font.color.rgb = _rgb(code_color_hex)
            elif tok.startswith("*") and tok.endswith("*") and len(tok) > 2:
                run = p.add_run()
                run.text = tok[1:-1]
                run.font.name = font_name
                run.font.size = Pt(size_pt)
                run.font.bold = bold_default
                run.font.italic = True
                run.font.color.rgb = _rgb(color_hex)
            else:
                run = p.add_run()
                run.text = tok
                run.font.name = font_name
                run.font.size = Pt(size_pt)
                run.font.bold = bold_default
                run.font.color.rgb = _rgb(color_hex)


def _add_textbox(
    slide: Any,
    x: float,
    y: float,
    w: float,
    h: float,
    text: str,
    theme: DeckTheme,
    size_pt: float = 12.0,
    color_hex: Optional[str] = None,
    code_color_hex: Optional[str] = None,
    bold: bool = False,
    heading_font: bool = False,
    align: PP_ALIGN = PP_ALIGN.LEFT,
    anchor: MSO_ANCHOR = MSO_ANCHOR.TOP,
    line_spacing: float = 1.18,
    pad_in: Tuple[float, float, float, float] = (0.04, 0.02, 0.04, 0.02),
) -> Any:
    """İç boşlukları sıfırlanmış, zengin metin destekli yerel PowerPoint metin kutusu."""
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = Inches(pad_in[0])
    tf.margin_top = Inches(pad_in[1])
    tf.margin_right = Inches(pad_in[2])
    tf.margin_bottom = Inches(pad_in[3])
    _write_rich_text(
        tf,
        text=text,
        font_name=theme.font_heading if heading_font else theme.font_body,
        mono_font=theme.font_mono,
        size_pt=size_pt,
        color_hex=color_hex or theme.ink,
        code_color_hex=code_color_hex or theme.accent("blue").primary,
        bold_default=bold,
        align=align,
        line_spacing=line_spacing,
    )
    return box


def _add_pill(
    slide: Any,
    x: float,
    y: float,
    w: float,
    h: float,
    text: str,
    theme: DeckTheme,
    bg_hex: str,
    fg_hex: str,
    border_hex: Optional[str] = None,
    size_pt: float = 9.0,
    align: PP_ALIGN = PP_ALIGN.CENTER,
) -> Any:
    """Yuvarlatılmış rozet (pill badge) şekli."""
    shp = _add_card(slide, x, y, w, h, fill_hex=bg_hex, border_hex=border_hex, border_pt=0.75, radius_in=h * 0.46)
    tf = shp.text_frame
    tf.word_wrap = False
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    tf.margin_left = Inches(0.10)
    tf.margin_right = Inches(0.10)
    tf.margin_top = Inches(0.01)
    tf.margin_bottom = Inches(0.01)
    _write_rich_text(
        tf,
        text=text,
        font_name=theme.font_heading,
        mono_font=theme.font_mono,
        size_pt=size_pt,
        color_hex=fg_hex,
        code_color_hex=fg_hex,
        bold_default=True,
        align=align,
        line_spacing=1.0,
    )
    return shp


def _add_fitted_image(slide: Any, img_path: Path, x: float, y: float, w: float, h: float) -> Any:
    """Görseli en-boy oranını bozmadan verilen kutunun tam ortasına yerleştirir."""
    if not img_path.is_file():
        raise FileNotFoundError(f"Slayt görseli bulunamadı: {img_path}")
    with Image.open(img_path) as im:
        iw, ih = im.size
    scale = min(w / iw, h / ih)
    draw_w = iw * scale
    draw_h = ih * scale
    dx = x + (w - draw_w) / 2.0
    dy = y + (h - draw_h) / 2.0
    return slide.shapes.add_picture(str(img_path), Inches(dx), Inches(dy), width=Inches(draw_w), height=Inches(draw_h))


def _resolve_asset(path_str: str, base_dir: Path) -> Path:
    p = Path(path_str)
    return p if p.is_absolute() else (base_dir / p).resolve()


def _set_speaker_notes(slide: Any, notes_text: str) -> None:
    if not notes_text:
        return
    notes_slide = slide.notes_slide
    tf = notes_slide.notes_text_frame
    tf.text = str(notes_text).strip()


# --------------------------------------------------------------------------- ortak çerçeve bileşenleri
def _render_top_chrome(
    slide: Any,
    spec: Dict[str, Any],
    theme: DeckTheme,
    slide_num: int,
    total_slides: int,
    dark: bool = False,
) -> None:
    role = spec.get("accent", "blue")
    acc = theme.accent(role)
    chapter = spec.get("chapter", f"{_default_text(theme, 'chapter')} {slide_num:02d}")
    pill_w = min(max(len(chapter) * 0.095 + 0.36, 1.85), 4.60)
    if dark:
        _add_pill(slide, MX, 0.32, pill_w, 0.28, theme.caps(chapter), theme, theme.dark_surface, acc.on_dark,
                  border_hex=theme.dark_border, size_pt=8.8)
    else:
        _add_pill(slide, MX, 0.32, pill_w, 0.28, theme.caps(chapter), theme, acc.soft_bg, acc.primary,
                  border_hex=acc.soft_border, size_pt=8.8)

    # Sağ üst ilerleme göstergesi (sayfa numarası + hikâye ilerleme çubuğu)
    counter_txt = f"{slide_num:02d} / {total_slides:02d}"
    _add_textbox(
        slide,
        SLIDE_W - MX - 1.15,
        0.33,
        1.15,
        0.26,
        counter_txt,
        theme,
        size_pt=9.5,
        color_hex=theme.dark_muted if dark else theme.ink_muted,
        bold=True,
        align=PP_ALIGN.RIGHT,
        anchor=MSO_ANCHOR.MIDDLE,
    )
    track_w = 1.60
    track_x = SLIDE_W - MX - 1.15 - track_w - 0.05
    track_y = 0.435
    _add_card(slide, track_x, track_y, track_w, 0.055,
              fill_hex=theme.dark_border if dark else "#E2E8F0", border_hex=None, radius_in=0.025)
    fill_w = max(0.12, track_w * (slide_num / max(total_slides, 1)))
    _add_card(slide, track_x, track_y, fill_w, 0.055,
              fill_hex=acc.on_dark if dark else acc.primary, border_hex=None, radius_in=0.025)

    # Assertion (tam cümlelik iddia başlığı)
    assertion = str(spec.get("assertion", "")).strip()
    if assertion:
        plain_len = len(re.sub(r"[*`]", "", assertion))
        head_pt = 20.0 if plain_len <= 95 else (18.5 if plain_len <= 125 else 17.0)
        _add_textbox(
            slide,
            MX,
            0.67,
            CONTENT_W,
            0.78,
            assertion,
            theme,
            size_pt=head_pt,
            color_hex=theme.dark_text if dark else theme.ink,
            code_color_hex=acc.on_dark if dark else acc.primary,
            bold=True,
            heading_font=True,
            anchor=MSO_ANCHOR.MIDDLE,
            line_spacing=1.12,
        )


def _render_narrative_bridge(slide: Any, spec: Dict[str, Any], theme: DeckTheme) -> None:
    bridge = str(spec.get("narrative_bridge", "")).strip()
    if not bridge:
        return
    acc = theme.accent(spec.get("accent", "blue"))
    y, h = 6.50, 0.68
    _add_card(slide, MX, y, CONTENT_W, h, fill_hex=theme.bridge_bg, border_hex=theme.card_border, border_pt=0.75,
              radius_in=0.10)
    _add_accent_bar(slide, MX + 0.08, y + 0.11, 0.065, h - 0.22, acc.primary)
    _add_textbox(
        slide,
        MX + 0.24,
        y + 0.05,
        CONTENT_W - 0.36,
        h - 0.10,
        bridge,
        theme,
        size_pt=10.8,
        color_hex=theme.ink_secondary,
        code_color_hex=acc.primary,
        anchor=MSO_ANCHOR.MIDDLE,
        line_spacing=1.16,
    )


# --------------------------------------------------------------------------- 7 Bento slayt şablonu
def _build_hero_cover(slide: Any, spec: Dict[str, Any], theme: DeckTheme, idx: int, total: int, _: Path) -> None:
    _set_slide_bg(slide, theme.dark_bg)
    acc = theme.accent(spec.get("accent", "blue"))
    # Üst ince sinematik vurgu çizgisi
    _add_card(slide, 0.0, 0.0, SLIDE_W, 0.07, fill_hex=acc.primary, border_hex=None, radius_in=0.0)

    eyebrow = spec.get("eyebrow", _default_text(theme, "eyebrow"))
    ew = min(max(len(eyebrow) * 0.10 + 0.40, 2.40), 5.50)
    _add_pill(slide, MX, 0.62, ew, 0.32, theme.caps(eyebrow), theme, theme.dark_surface, acc.on_dark,
              border_hex=theme.dark_border, size_pt=9.2)

    title = spec.get("title", spec.get("assertion", _default_text(theme, "title")))
    _add_textbox(
        slide,
        MX,
        1.14,
        CONTENT_W - 0.5,
        1.55,
        title,
        theme,
        size_pt=32.0 if len(title) <= 75 else 28.0,
        color_hex=theme.dark_text,
        code_color_hex=acc.on_dark,
        bold=True,
        heading_font=True,
        line_spacing=1.10,
    )

    subtitle = spec.get("subtitle", "")
    if subtitle:
        _add_textbox(
            slide,
            MX,
            2.78,
            CONTENT_W - 1.0,
            0.92,
            subtitle,
            theme,
            size_pt=14.0,
            color_hex=theme.dark_muted,
            code_color_hex=acc.on_dark,
            line_spacing=1.24,
        )

    # Üç perde önizleme Bento kartları
    pillars: Sequence[Dict[str, Any]] = spec.get("pillars", ())
    if pillars:
        n = min(len(pillars), 4)
        gap = 0.22
        cw = (CONTENT_W - gap * (n - 1)) / n
        cy, ch = 4.10, 2.28
        for i, p in enumerate(pillars[:n]):
            px = MX + i * (cw + gap)
            p_acc = theme.accent(p.get("role", "blue"))
            _add_card(slide, px, cy, cw, ch, fill_hex=theme.dark_surface, border_hex=theme.dark_border, border_pt=0.9)
            _add_accent_bar(slide, px + 0.18, cy + 0.18, 0.46, 0.055, p_acc.on_dark)
            if p.get("tag"):
                _add_textbox(slide, px + 0.18, cy + 0.30, cw - 0.36, 0.26, theme.caps(str(p["tag"])), theme,
                             size_pt=8.8, color_hex=p_acc.on_dark, bold=True)
            _add_textbox(slide, px + 0.18, cy + 0.60, cw - 0.36, 0.52, str(p.get("title", "")), theme,
                         size_pt=13.5, color_hex=theme.dark_text, bold=True, heading_font=True, line_spacing=1.12)
            _add_textbox(slide, px + 0.18, cy + 1.16, cw - 0.36, 0.98, str(p.get("body", "")), theme,
                         size_pt=10.5, color_hex=theme.dark_muted, code_color_hex=p_acc.on_dark, line_spacing=1.20)

    meta = spec.get("meta", "")
    if meta:
        _add_textbox(slide, MX, 6.76, CONTENT_W, 0.36, meta, theme, size_pt=9.8, color_hex=theme.dark_muted)
    _ = idx, total


def _build_mental_model(slide: Any, spec: Dict[str, Any], theme: DeckTheme, idx: int, total: int, base: Path) -> None:
    """En baştan devreyi/sistemi ve karakterlerin zihinsel modelini tanıtan sahne kurulumu slaytı."""
    _set_slide_bg(slide, theme.light_bg)
    _render_top_chrome(slide, spec, theme, idx, total)

    top_y, top_h = 1.56, 2.46
    _add_card(slide, MX, top_y, CONTENT_W, top_h, fill_hex=theme.card_bg, border_hex=theme.card_border)
    if spec.get("image"):
        img_path = _resolve_asset(spec["image"], base)
        scene_note = str(spec.get("scene_summary", "")).strip()
        if scene_note:
            _add_fitted_image(slide, img_path, MX + 0.14, top_y + 0.10, 8.15, top_h - 0.20)
            _add_card(slide, MX + 8.42, top_y + 0.14, CONTENT_W - 8.56, top_h - 0.28,
                      fill_hex=theme.card_alt_bg, border_hex=theme.card_border, radius_in=0.09)
            _add_textbox(slide, MX + 8.58, top_y + 0.24, CONTENT_W - 8.88, 0.28,
                         theme.caps(spec.get("scene_title", _default_text(theme, "scene_title"))), theme,
                         size_pt=9.0, color_hex=theme.accent(spec.get("accent", "blue")).primary, bold=True)
            _add_textbox(slide, MX + 8.58, top_y + 0.54, CONTENT_W - 8.88, top_h - 0.78, scene_note, theme,
                         size_pt=10.5, color_hex=theme.ink_secondary, line_spacing=1.20)
        else:
            _add_fitted_image(slide, img_path, MX + 0.16, top_y + 0.10, CONTENT_W - 0.32, top_h - 0.20)

    chars: Sequence[Dict[str, Any]] = spec.get("characters", ())
    if chars:
        n = min(len(chars), 3)
        gap = 0.22
        cw = (CONTENT_W - gap * (n - 1)) / n
        cy, ch = 4.18, 2.16
        for i, ch_spec in enumerate(chars[:n]):
            cx = MX + i * (cw + gap)
            c_acc = theme.accent(ch_spec.get("role", "blue"))
            _add_card(slide, cx, cy, cw, ch, fill_hex=theme.card_bg, border_hex=c_acc.soft_border, border_pt=1.0)
            _add_accent_bar(slide, cx + 0.16, cy + 0.14, cw - 0.32, 0.055, c_acc.primary)
            _add_textbox(slide, cx + 0.18, cy + 0.26, cw - 2.15, 0.36, str(ch_spec.get("title", "")), theme,
                         size_pt=13.2, color_hex=theme.ink, bold=True, heading_font=True)
            if ch_spec.get("law"):
                _add_pill(slide, cx + cw - 1.92, cy + 0.25, 1.74, 0.30, str(ch_spec["law"]), theme,
                          c_acc.soft_bg, c_acc.primary, border_hex=c_acc.soft_border, size_pt=8.8)
            _add_textbox(slide, cx + 0.18, cy + 0.66, cw - 0.36, 1.04, str(ch_spec.get("body", "")), theme,
                         size_pt=10.6, color_hex=theme.ink_secondary, code_color_hex=c_acc.primary, line_spacing=1.18)
            if ch_spec.get("trait"):
                _add_pill(slide, cx + 0.18, cy + ch - 0.38, cw - 0.36, 0.27, str(ch_spec["trait"]), theme,
                          c_acc.soft_bg, c_acc.primary, size_pt=8.8, align=PP_ALIGN.LEFT)

    _render_narrative_bridge(slide, spec, theme)


def _build_evidence_bento(slide: Any, spec: Dict[str, Any], theme: DeckTheme, idx: int, total: int, base: Path) -> None:
    """Asimetrik Bento kanıt slaytı: solda işaretli dalga formu/diyagram, sağda KPI + mekanizma anlatısı."""
    _set_slide_bg(slide, theme.light_bg)
    _render_top_chrome(slide, spec, theme, idx, total)

    body_y, body_h = 1.56, 4.78
    left_w = 7.72
    gap = 0.22
    right_x = MX + left_w + gap
    right_w = CONTENT_W - left_w - gap  # 4.193"

    # Sol görsel kanıt kartı
    _add_card(slide, MX, body_y, left_w, body_h, fill_hex=theme.card_bg, border_hex=theme.card_border)
    caption = str(spec.get("image_caption", "")).strip()
    img_path = _resolve_asset(spec["image"], base) if spec.get("image") else None
    is_gif = bool(img_path and img_path.suffix.lower() == ".gif")
    img_h = body_h - (0.50 if (caption or is_gif) else 0.22)
    if img_path is not None:
        _add_fitted_image(slide, img_path, MX + 0.12, body_y + 0.10, left_w - 0.24, img_h)
    if caption:
        cap_w = (left_w - 1.78) if is_gif else (left_w - 0.36)
        _add_textbox(slide, MX + 0.18, body_y + body_h - 0.39, cap_w, 0.34, caption, theme,
                     size_pt=9.0 if is_gif else 9.2, color_hex=theme.ink_muted, anchor=MSO_ANCHOR.MIDDLE)
    if is_gif:
        g_acc = theme.accent(spec.get("accent", "blue"))
        _add_pill(slide, MX + left_w - 1.50, body_y + body_h - 0.35, 1.32, 0.26,
                  _default_text(theme, "gif"), theme, g_acc.soft_bg, g_acc.primary,
                  border_hex=g_acc.soft_border, size_pt=8.2)

    # Sağ üst KPI kutuları (1 veya 2 adet)
    metrics: Sequence[Dict[str, Any]] = spec.get("metrics", ())
    mech_y = body_y
    mech_h = body_h
    if metrics:
        kpi_h = 1.44
        m_count = min(len(metrics), 2)
        m_gap = 0.16
        mw = (right_w - m_gap * (m_count - 1)) / m_count
        for i, m in enumerate(metrics[:m_count]):
            mx = right_x + i * (mw + m_gap)
            m_acc = theme.accent(m.get("role", spec.get("accent", "blue")))
            _add_card(slide, mx, body_y, mw, kpi_h, fill_hex=theme.card_bg, border_hex=m_acc.soft_border, border_pt=1.0)
            _add_accent_bar(slide, mx + 0.14, body_y + 0.12, mw - 0.28, 0.05, m_acc.primary)
            _add_textbox(slide, mx + 0.14, body_y + 0.23, mw - 0.28, 0.26, theme.caps(str(m.get("label", ""))), theme,
                         size_pt=8.5, color_hex=theme.ink_muted, bold=True)
            val_str = str(m.get("value", ""))
            v_pt = 21.0 if (m_count == 2 and len(val_str) <= 9) else (17.5 if m_count == 2 else 25.0)
            _add_textbox(slide, mx + 0.14, body_y + 0.50, mw - 0.28, 0.52, val_str, theme,
                         size_pt=v_pt, color_hex=m_acc.primary, bold=True, heading_font=True, anchor=MSO_ANCHOR.MIDDLE)
            if m.get("note"):
                _add_textbox(slide, mx + 0.14, body_y + 1.04, mw - 0.28, 0.32, str(m["note"]), theme,
                             size_pt=8.8, color_hex=theme.ink_secondary, line_spacing=1.10)
        mech_y = body_y + kpi_h + 0.18
        mech_h = body_h - kpi_h - 0.18

    # Sağ alt mekanizma / hikâye açıklaması kartı
    mech = spec.get("mechanism", {})
    if mech:
        m_acc = theme.accent(mech.get("role", spec.get("accent", "blue")))
        _add_card(slide, right_x, mech_y, right_w, mech_h, fill_hex=theme.card_bg, border_hex=theme.card_border)
        badge = theme.caps(str(mech.get("badge", _default_text(theme, "mechanism"))))
        bw = min(max(len(badge) * 0.09 + 0.30, 1.65), right_w - 0.36)
        _add_pill(slide, right_x + 0.18, mech_y + 0.16, bw, 0.26, badge, theme, m_acc.soft_bg, m_acc.primary,
                  size_pt=8.3)
        cur_y = mech_y + 0.50
        if mech.get("title"):
            _add_textbox(slide, right_x + 0.18, cur_y, right_w - 0.36, 0.44, str(mech["title"]), theme,
                         size_pt=12.5, color_hex=theme.ink, bold=True, heading_font=True, line_spacing=1.12)
            cur_y += 0.46
        pill_text = str(mech.get("takeaway_pill", "")).strip()
        has_pill = bool(pill_text)
        pill_h = 0.44 if len(pill_text) > 60 else 0.32
        avail_h = (mech_y + mech_h) - cur_y - ((pill_h + 0.22) if has_pill else 0.16)
        body_txt = str(mech.get("body", ""))
        body_pt = 10.6 if len(body_txt) <= 340 else 9.9
        _add_textbox(slide, right_x + 0.18, cur_y, right_w - 0.36, max(avail_h, 0.6), body_txt, theme,
                     size_pt=body_pt, color_hex=theme.ink_secondary, code_color_hex=m_acc.primary, line_spacing=1.20)
        if has_pill:
            _add_pill(slide, right_x + 0.18, mech_y + mech_h - pill_h - 0.12, right_w - 0.36, pill_h,
                      pill_text, theme, m_acc.soft_bg, m_acc.primary,
                      border_hex=m_acc.soft_border, size_pt=8.8, align=PP_ALIGN.LEFT)

    _render_narrative_bridge(slide, spec, theme)


def _build_split_duality(slide: Any, spec: Dict[str, Any], theme: DeckTheme, idx: int, total: int, _: Path) -> None:
    """İki kavramın simetrik ikizliğini (örn. Kondansatör vs Bobin) yan yana kıyaslayan Bento düzeni."""
    _set_slide_bg(slide, theme.light_bg)
    _render_top_chrome(slide, spec, theme, idx, total)

    body_y, body_h = 1.56, 4.78
    col_w = 5.78
    right_x = SLIDE_W - MX - col_w

    for side_key, x_pos in (("left", MX), ("right", right_x)):
        panel = spec.get(side_key, {})
        s_acc = theme.accent(panel.get("role", "blue" if side_key == "left" else "amber"))
        _add_card(slide, x_pos, body_y, col_w, body_h, fill_hex=theme.card_bg, border_hex=s_acc.soft_border,
                  border_pt=1.1)
        # Üst başlık bandı
        _add_card(slide, x_pos + 0.12, body_y + 0.12, col_w - 0.24, 0.72, fill_hex=s_acc.soft_bg,
                  border_hex=s_acc.soft_border, radius_in=0.08)
        _add_textbox(slide, x_pos + 0.26, body_y + 0.17, col_w - 0.52, 0.34, str(panel.get("title", "")), theme,
                     size_pt=14.0, color_hex=s_acc.primary, bold=True, heading_font=True)
        if panel.get("subtitle"):
            _add_textbox(slide, x_pos + 0.26, body_y + 0.50, col_w - 0.52, 0.28, str(panel["subtitle"]), theme,
                         size_pt=9.6, color_hex=theme.ink_secondary)

        rows: Sequence[Dict[str, Any]] = panel.get("rows", ())
        has_summary = bool(panel.get("summary"))
        rows_top = body_y + 0.96
        rows_avail = body_h - 0.96 - (0.58 if has_summary else 0.16)
        if rows:
            rh = rows_avail / len(rows)
            for r_idx, r in enumerate(rows):
                ry = rows_top + r_idx * rh
                if r_idx > 0:
                    _add_card(slide, x_pos + 0.22, ry, col_w - 0.44, 0.012, fill_hex="#E2E8F0", border_hex=None,
                              radius_in=0.0)
                _add_textbox(slide, x_pos + 0.24, ry + 0.06, 1.85, rh - 0.10, str(r.get("label", "")), theme,
                             size_pt=9.6, color_hex=theme.ink_muted, bold=True, anchor=MSO_ANCHOR.MIDDLE)
                _add_textbox(slide, x_pos + 2.12, ry + 0.06, col_w - 2.34, rh - 0.10, str(r.get("value", "")), theme,
                             size_pt=10.5, color_hex=theme.ink, code_color_hex=s_acc.primary,
                             anchor=MSO_ANCHOR.MIDDLE, line_spacing=1.14)

        if has_summary:
            _add_pill(slide, x_pos + 0.18, body_y + body_h - 0.44, col_w - 0.36, 0.30, str(panel["summary"]), theme,
                      s_acc.soft_bg, s_acc.primary, border_hex=s_acc.soft_border, size_pt=9.0, align=PP_ALIGN.LEFT)

    # Orta ikizlik rozeti
    center_lbl = spec.get("center_badge", "↔")
    _add_pill(slide, (SLIDE_W - 0.48) / 2.0, body_y + body_h / 2.0 - 0.22, 0.48, 0.44, center_lbl, theme,
              theme.ink, "#FFFFFF", size_pt=12.0)

    _render_narrative_bridge(slide, spec, theme)


def _build_process_arc(slide: Any, spec: Dict[str, Any], theme: DeckTheme, idx: int, total: int, _: Path) -> None:
    """3 veya 4 aşamalı nedensel zaman zinciri slaytı."""
    _set_slide_bg(slide, theme.light_bg)
    _render_top_chrome(slide, spec, theme, idx, total)

    stages: Sequence[Dict[str, Any]] = spec.get("stages", ())
    if stages:
        n = min(len(stages), 4)
        gap = 0.22
        cw = (CONTENT_W - gap * (n - 1)) / n
        by, bh = 1.56, 4.78
        for i, st in enumerate(stages[:n]):
            cx = MX + i * (cw + gap)
            s_acc = theme.accent(st.get("role", spec.get("accent", "blue")))
            _add_card(slide, cx, by, cw, bh, fill_hex=theme.card_bg, border_hex=theme.card_border)
            _add_accent_bar(slide, cx + 0.16, by + 0.14, cw - 0.32, 0.055, s_acc.primary)
            _add_pill(slide, cx + 0.18, by + 0.28, 0.68, 0.26, str(st.get("step", f"0{i + 1}")), theme,
                      s_acc.soft_bg, s_acc.primary, size_pt=8.8)
            if st.get("phase"):
                _add_textbox(slide, cx + 0.94, by + 0.28, cw - 1.12, 0.26, theme.caps(str(st["phase"])), theme,
                             size_pt=8.6, color_hex=theme.ink_muted, bold=True, anchor=MSO_ANCHOR.MIDDLE)
            _add_textbox(slide, cx + 0.18, by + 0.66, cw - 0.36, 0.56, str(st.get("title", "")), theme,
                         size_pt=13.0, color_hex=theme.ink, bold=True, heading_font=True, line_spacing=1.12)
            if st.get("metric"):
                _add_card(slide, cx + 0.18, by + 1.30, cw - 0.36, 0.68, fill_hex=s_acc.soft_bg,
                          border_hex=s_acc.soft_border, radius_in=0.08)
                _add_textbox(slide, cx + 0.26, by + 1.34, cw - 0.52, 0.60, str(st["metric"]), theme,
                             size_pt=15.5, color_hex=s_acc.primary, bold=True, heading_font=True,
                             align=PP_ALIGN.CENTER, anchor=MSO_ANCHOR.MIDDLE)
                body_top = by + 2.12
            else:
                body_top = by + 1.32
            _add_textbox(slide, cx + 0.18, body_top, cw - 0.36, (by + bh) - body_top - 0.18,
                         str(st.get("body", "")), theme, size_pt=10.4, color_hex=theme.ink_secondary,
                         code_color_hex=s_acc.primary, line_spacing=1.20)

    _render_narrative_bridge(slide, spec, theme)


def _build_comparison_matrix(slide: Any, spec: Dict[str, Any], theme: DeckTheme, idx: int, total: int, _: Path) -> None:
    """Tufte tarzı sade ölçüm tablosu + sağ yorum kartı."""
    _set_slide_bg(slide, theme.light_bg)
    _render_top_chrome(slide, spec, theme, idx, total)

    by, bh = 1.56, 4.78
    tw = 8.10
    gap = 0.22
    rx = MX + tw + gap
    rw = CONTENT_W - tw - gap

    _add_card(slide, MX, by, tw, bh, fill_hex=theme.card_bg, border_hex=theme.card_border)
    headers: Sequence[str] = spec.get("headers", ())
    rows: Sequence[Sequence[str]] = spec.get("rows", ())
    hi_row: Optional[int] = spec.get("highlight_row")
    acc = theme.accent(spec.get("accent", "blue"))

    if headers and rows:
        n_rows = len(rows) + 1
        n_cols = len(headers)
        t_shape = slide.shapes.add_table(
            n_rows, n_cols, Inches(MX + 0.18), Inches(by + 0.20), Inches(tw - 0.36), Inches(min(bh - 0.40, n_rows * 0.62))
        )
        tbl = t_shape.table
        for c_idx, h_text in enumerate(headers):
            cell = tbl.cell(0, c_idx)
            cell.fill.solid()
            cell.fill.fore_color.rgb = _rgb(theme.card_alt_bg)
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            _write_rich_text(
                cell.text_frame,
                h_text,
                theme.font_heading,
                theme.font_mono,
                9.5,
                theme.ink_secondary,
                acc.primary,
                bold_default=True,
                align=PP_ALIGN.LEFT if c_idx == 0 else PP_ALIGN.RIGHT,
            )
        for r_idx, row_vals in enumerate(rows):
            is_hi = hi_row is not None and r_idx == hi_row
            bg = acc.soft_bg if is_hi else (theme.card_bg if r_idx % 2 == 0 else "#F8FAFC")
            for c_idx, val in enumerate(row_vals[:n_cols]):
                cell = tbl.cell(r_idx + 1, c_idx)
                cell.fill.solid()
                cell.fill.fore_color.rgb = _rgb(bg)
                cell.vertical_anchor = MSO_ANCHOR.MIDDLE
                _write_rich_text(
                    cell.text_frame,
                    str(val),
                    theme.font_body,
                    theme.font_mono,
                    10.0,
                    acc.primary if (is_hi and c_idx == 0) else theme.ink,
                    acc.primary,
                    bold_default=(c_idx == 0 or is_hi),
                    align=PP_ALIGN.LEFT if c_idx == 0 else PP_ALIGN.RIGHT,
                )

    insight = spec.get("insight", {})
    if insight:
        i_acc = theme.accent(insight.get("role", spec.get("accent", "blue")))
        _add_card(slide, rx, by, rw, bh, fill_hex=theme.card_bg, border_hex=i_acc.soft_border, border_pt=1.0)
        _add_accent_bar(slide, rx + 0.16, by + 0.14, rw - 0.32, 0.055, i_acc.primary)
        _add_textbox(slide, rx + 0.18, by + 0.28, rw - 0.36, 0.50, str(insight.get("title", "")), theme,
                     size_pt=13.5, color_hex=theme.ink, bold=True, heading_font=True)
        _add_textbox(slide, rx + 0.18, by + 0.84, rw - 0.36, bh - 1.04, str(insight.get("body", "")), theme,
                     size_pt=10.6, color_hex=theme.ink_secondary, code_color_hex=i_acc.primary, line_spacing=1.22)

    _render_narrative_bridge(slide, spec, theme)


def _build_synthesis_closing(slide: Any, spec: Dict[str, Any], theme: DeckTheme, idx: int, total: int, _: Path) -> None:
    """Sandviç yapısını tamamlayan koyu kapanış ve gerçek dünya mühendislik kuralları slaytı."""
    _set_slide_bg(slide, theme.dark_bg)
    _render_top_chrome(slide, spec, theme, idx, total, dark=True)

    pillars: Sequence[Dict[str, Any]] = spec.get("pillars", ())
    if pillars:
        n = min(len(pillars), 3)
        gap = 0.24
        cw = (CONTENT_W - gap * (n - 1)) / n
        py, ph = 1.66, 3.88
        for i, p in enumerate(pillars[:n]):
            px = MX + i * (cw + gap)
            p_acc = theme.accent(p.get("role", "blue"))
            _add_card(slide, px, py, cw, ph, fill_hex=theme.dark_surface, border_hex=theme.dark_border, border_pt=1.0)
            _add_accent_bar(slide, px + 0.18, py + 0.16, cw - 0.36, 0.06, p_acc.on_dark)
            if p.get("badge"):
                _add_pill(slide, px + 0.18, py + 0.32, min(cw - 0.36, 2.30), 0.27, theme.caps(str(p["badge"])), theme,
                          theme.dark_bg, p_acc.on_dark, border_hex=theme.dark_border, size_pt=8.5)
            _add_textbox(slide, px + 0.18, py + 0.70, cw - 0.36, 0.56, str(p.get("title", "")), theme,
                         size_pt=14.0, color_hex=theme.dark_text, bold=True, heading_font=True, line_spacing=1.12)
            _add_textbox(slide, px + 0.18, py + 1.30, cw - 0.36, 1.76, str(p.get("body", "")), theme,
                         size_pt=10.5, color_hex=theme.dark_muted, code_color_hex=p_acc.on_dark, line_spacing=1.20)
            if p.get("rule"):
                _add_pill(slide, px + 0.18, py + ph - 0.46, cw - 0.36, 0.30, str(p["rule"]), theme,
                          theme.dark_bg, p_acc.on_dark, border_hex=theme.dark_border, size_pt=8.8, align=PP_ALIGN.LEFT)

    banner = str(spec.get("closing_banner", "")).strip()
    if banner:
        acc = theme.accent(spec.get("accent", "emerald"))
        _add_card(slide, MX, 5.82, CONTENT_W, 1.12, fill_hex=theme.dark_surface, border_hex=acc.on_dark, border_pt=1.1)
        _add_textbox(
            slide,
            MX + 0.26,
            5.92,
            CONTENT_W - 0.52,
            0.92,
            banner,
            theme,
            size_pt=12.0,
            color_hex=theme.dark_text,
            code_color_hex=acc.on_dark,
            anchor=MSO_ANCHOR.MIDDLE,
            line_spacing=1.22,
        )


LAYOUT_BUILDERS = {
    "hero_cover": _build_hero_cover,
    "mental_model": _build_mental_model,
    "evidence_bento": _build_evidence_bento,
    "split_duality": _build_split_duality,
    "process_arc": _build_process_arc,
    "comparison_matrix": _build_comparison_matrix,
    "synthesis_closing": _build_synthesis_closing,
}


def build_deck(spec: Dict[str, Any], output_path: str | Path, base_dir: Optional[Path] = None) -> Path:
    """Verilen hikâye şemasından (`spec`) `.pptx` sunum dosyasını oluşturur ve kaydeder."""
    theme = get_theme(spec.get("theme"), language=spec.get("language", "en"))
    prs = Presentation()
    prs.slide_width = Inches(SLIDE_W)
    prs.slide_height = Inches(SLIDE_H)
    blank_layout = prs.slide_layouts[6]

    if spec.get("title"):
        prs.core_properties.title = str(spec["title"])
    if spec.get("author"):
        prs.core_properties.author = str(spec["author"])
    if spec.get("story_arc"):
        prs.core_properties.subject = str(spec["story_arc"])

    slides_data: List[Dict[str, Any]] = list(spec.get("slides", ()))
    if not slides_data:
        raise ValueError("Sunum şemasında en az bir slayt ('slides') bulunmalıdır.")

    asset_root = base_dir or Path.cwd()
    total = len(slides_data)
    for idx, s_spec in enumerate(slides_data, start=1):
        layout_name = s_spec.get("layout", "evidence_bento")
        if layout_name not in LAYOUT_BUILDERS:
            raise ValueError(f"Slayt {idx}: bilinmeyen şablon {layout_name!r}; seçenekler: {sorted(LAYOUT_BUILDERS)}")
        slide = prs.slides.add_slide(blank_layout)
        LAYOUT_BUILDERS[layout_name](slide, s_spec, theme, idx, total, asset_root)
        _set_speaker_notes(slide, str(s_spec.get("speaker_notes", "")))

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        prs.save(str(out))
        return out
    except PermissionError:
        # Windows'ta hedef .pptx PowerPoint'te açıksa kilitleme hatası yerine yeni sürüme yaz
        for ver in range(2, 20):
            alt = out.with_name(f"{out.stem}_v{ver}{out.suffix}")
            try:
                prs.save(str(alt))
                return alt
            except PermissionError:
                continue
        raise


def main(argv: Optional[Sequence[str]] = None) -> int:
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="JSON hikâye şemasından anlatısal Bento .pptx sunumu üretir.")
    parser.add_argument("spec_json", type=Path, help="Girdi JSON şema dosyası")
    parser.add_argument("-o", "--output", type=Path, required=True, help="Çıktı .pptx yolu")
    args = parser.parse_args(argv)

    spec_path = args.spec_json.resolve()
    data = json.loads(spec_path.read_text(encoding="utf-8"))
    for warning in spec_warnings(data):
        print(f"  [UYARI] {warning}")
    out = build_deck(data, args.output, base_dir=spec_path.parent)
    print(f"Sunum oluşturuldu ({len(data.get('slides', ()))} slayt): {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
