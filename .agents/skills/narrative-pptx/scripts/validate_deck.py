"""
Narrative PPTX Structural & Visual QA Validator
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Üretilen `.pptx` dosyasını iki katmanda denetler:
1. Yapısal & Geometrik Denetim: Tuval taşması, metin kutusu dikey taşma tahmini, çakışan metin
   kutuları, konuşmacı notu (anlatı sürekliliği) varlığı ve madde işareti duvarı yasağı.
2. Görsel Denetim (`--render-dir`): Windows'ta kuruluysa Microsoft PowerPoint'in gerçek çizim motoruyla
   (kullanıcının açık sunumlarına dokunmadan, penceresiz kipte), değilse saf Python `Pillow` önizleme
   motoruyla her slaytı PNG'ye ve toplu `contact_sheet.png` ızgarasına çevirir.

Kullanım:
    python .agents/skills/narrative-pptx/scripts/validate_deck.py sunum.pptx --render-dir onizleme/
"""

from __future__ import annotations

import argparse
import math
import ntpath
import os
import subprocess
import sys
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw
from pptx import Presentation

EMU_PER_INCH = 914400.0


def _emu_to_in(val: int) -> float:
    return float(val) / EMU_PER_INCH


def _shape_bounds_in(shp: Any) -> Tuple[float, float, float, float]:
    x = _emu_to_in(shp.left)
    y = _emu_to_in(shp.top)
    w = _emu_to_in(shp.width)
    h = _emu_to_in(shp.height)
    return x, y, x + w, y + h


def _intersection_area(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> float:
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    return max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)


def _estimate_text_overflow(shp: Any) -> Optional[str]:
    """Metin kutusunun yaklaşık sarılmış satır yüksekliğini kutu yüksekliğiyle karşılaştırır."""
    if not getattr(shp, "has_text_frame", False):
        return None
    tf = shp.text_frame
    full_text = tf.text.strip()
    if not full_text:
        return None

    w_in = _emu_to_in(shp.width) - _emu_to_in(tf.margin_left or 0) - _emu_to_in(tf.margin_right or 0)
    h_in = _emu_to_in(shp.height) - _emu_to_in(tf.margin_top or 0) - _emu_to_in(tf.margin_bottom or 0)
    if w_in <= 0.05 or h_in <= 0.05:
        return f"Metin kutusu iç alanı çok dar ({w_in:.2f}x{h_in:.2f} inç): {full_text[:40]!r}"

    # En büyük puntoyu bul
    max_pt = 0.0
    for p in tf.paragraphs:
        for r in p.runs:
            if r.font.size is not None:
                max_pt = max(max_pt, float(r.font.size.pt))
    if max_pt <= 0.0:
        max_pt = 11.0

    avg_char_w_in = (max_pt * 0.53) / 72.0
    chars_per_line = max(1, int(w_in / max(avg_char_w_in, 0.01)))
    total_lines = 0
    for p in tf.paragraphs:
        txt = p.text
        total_lines += max(1, math.ceil(len(txt) / chars_per_line)) if txt else 1

    line_h_in = (max_pt * 1.24) / 72.0
    needed_h = total_lines * line_h_in
    # Yalnız belirgin taşmaları bildir (%18 tolerans: orantılı fontlarda i/l gibi dar harfler var)
    if needed_h > h_in * 1.18 and total_lines > 1:
        return (
            f"Olası metin taşması ({needed_h:.2f}\" > kutu {h_in:.2f}\", ~{total_lines} satır @ {max_pt:.1f}pt): "
            f"{full_text[:55]!r}"
        )
    return None


def audit_pptx(pptx_path: str | Path) -> Dict[str, Any]:
    """`.pptx` dosyasının geometri, metin sığma ve anlatı bütünlüğü denetimini yapar."""
    path = Path(pptx_path)
    prs = Presentation(str(path))
    sw = _emu_to_in(prs.slide_width)
    sh = _emu_to_in(prs.slide_height)

    errors: List[str] = []
    warnings: List[str] = []

    if len(prs.slides) == 0:
        errors.append("Sunumda hiç slayt yok.")

    for s_idx, slide in enumerate(prs.slides, start=1):
        notes = ""
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
            notes = slide.notes_slide.notes_text_frame.text.strip()
        if not notes:
            warnings.append(f"Slayt {s_idx}: Konuşmacı notu (anlatı metni) boş.")

        text_boxes: List[Tuple[Tuple[float, float, float, float], str]] = []
        for shp in slide.shapes:
            x0, y0, x1, y1 = _shape_bounds_in(shp)
            if x0 < -0.05 or y0 < -0.05 or x1 > sw + 0.05 or y1 > sh + 0.05:
                errors.append(
                    f"Slayt {s_idx}: Şekil tuval dışına taşıyor ({x0:.2f}, {y0:.2f})–({x1:.2f}, {y1:.2f})."
                )
            overflow = _estimate_text_overflow(shp)
            if overflow:
                warnings.append(f"Slayt {s_idx}: {overflow}")

            if getattr(shp, "has_text_frame", False) and shp.text_frame.text.strip():
                txt = shp.text_frame.text.strip()
                bullet_lines = [ln for ln in txt.splitlines() if ln.lstrip().startswith(("•", "- ", "* "))]
                if len(bullet_lines) >= 4:
                    warnings.append(f"Slayt {s_idx}: Madde işareti duvarı tespit edildi ({len(bullet_lines)} madde).")
                # Arka plan kartı olmayan saf metin kutularının çakışmasını kontrol et
                text_boxes.append(((x0, y0, x1, y1), txt[:35]))

        for i in range(len(text_boxes)):
            for j in range(i + 1, len(text_boxes)):
                area = _intersection_area(text_boxes[i][0], text_boxes[j][0])
                if area > 0.08:
                    errors.append(
                        f"Slayt {s_idx}: İki metin alanı çakışıyor ({area:.2f} inç²): "
                        f"{text_boxes[i][1]!r} <-> {text_boxes[j][1]!r}"
                    )

    return {
        "slide_count": len(prs.slides),
        "width_in": round(sw, 3),
        "height_in": round(sh, 3),
        "errors": errors,
        "warnings": warnings,
        "passed": len(errors) == 0,
    }


def _export_via_powerpoint_com(pptx_path: Path, out_dir: Path) -> List[Path]:
    """
    Windows'ta Microsoft PowerPoint kuruluysa COM otomasyonuyla gerçek slayt PNG'lerini dışa aktarır.
    Kullanıcının açık PowerPoint'ini ve PowerPoint'te zaten açık olan bu sunumu kapatmaz. PowerPoint yanıt vermezse
    (ilk açılış ya da lisans penceresi) 90 sn sonra boş liste döner ve kaba önizlemeye düşülür.
    """
    if sys.platform != "win32":
        return []
    ps_script = (
        "$ErrorActionPreference = 'Stop'\n"
        "$pptx = $env:NARRATIVE_PPTX_PATH\n"
        "$outDir = $env:NARRATIVE_OUT_DIR\n"
        "$app = New-Object -ComObject PowerPoint.Application\n"
        "$hadOpen = ($app.Presentations.Count -gt 0)\n"
        "$pres = $null\n"
        "foreach ($p in $app.Presentations) { if ($p.FullName -eq $pptx) { $pres = $p } }\n"
        "$opened = ($null -eq $pres)\n"
        "if ($opened) { $pres = $app.Presentations.Open($pptx, -1, 0, 0) }\n"
        "try {\n"
        "  $idx = 0\n"
        "  foreach ($s in $pres.Slides) {\n"
        "    $idx++\n"
        "    $target = Join-Path $outDir ('slide_{0:D2}.png' -f $idx)\n"
        "    $s.Export($target, 'PNG', 1920, 1080)\n"
        "  }\n"
        "} finally {\n"
        "  if ($opened) { $pres.Close() }\n"
        "  if (-not $hadOpen) { $app.Quit() }\n"
        "}\n"
    )
    # Yalın ad kullanılmaz: CreateProcess sistem klasöründen önce çalışma klasörüne bakar, oradaki powershell.exe çalışırdı.
    powershell = ntpath.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32", "WindowsPowerShell", "v1.0",
                             "powershell.exe")
    env = dict(os.environ, NARRATIVE_PPTX_PATH=str(pptx_path.resolve()), NARRATIVE_OUT_DIR=str(out_dir.resolve()))
    try:
        proc = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", ps_script],
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return sorted(out_dir.glob("slide_*.png"))


def _export_fallback_wireframe(pptx_path: Path, out_dir: Path) -> List[Path]:
    """PowerPoint kurulu olmayan ortamlar için saf Pillow tabanlı yerleşim/görsel önizlemesi."""
    prs = Presentation(str(pptx_path))
    sw = _emu_to_in(prs.slide_width)
    sh = _emu_to_in(prs.slide_height)
    px_w, px_h = 1600, int(1600 * sh / sw)
    scale = px_w / sw
    exported: List[Path] = []

    for idx, slide in enumerate(prs.slides, start=1):
        canvas = Image.new("RGB", (px_w, px_h), (248, 250, 252))
        draw = ImageDraw.Draw(canvas)
        for shp in slide.shapes:
            x0, y0, x1, y1 = _shape_bounds_in(shp)
            box = (int(x0 * scale), int(y0 * scale), int(x1 * scale), int(y1 * scale))
            if hasattr(shp, "image"):
                with Image.open(BytesIO(shp.image.blob)) as im:
                    resized = im.convert("RGB").resize((max(1, box[2] - box[0]), max(1, box[3] - box[1])))
                    canvas.paste(resized, (box[0], box[1]))
            else:
                draw.rectangle(box, outline=(203, 213, 225), width=2)
                if getattr(shp, "has_text_frame", False) and shp.text_frame.text.strip():
                    draw.text((box[0] + 8, box[1] + 6), shp.text_frame.text.strip()[:90], fill=(15, 23, 42))
        target = out_dir / f"slide_{idx:02d}.png"
        canvas.save(target)
        exported.append(target)
    return exported


def build_contact_sheet(slide_pngs: Sequence[Path], out_path: Path, cols: int = 2) -> Optional[Path]:
    """Tüm slaytları tek bakışta görsel denetim (Visual QA) için 2 sütunlu ızgara görüntüsünde birleştirir."""
    if not slide_pngs:
        return None
    thumb_w, thumb_h = 920, 518
    pad = 24
    rows = math.ceil(len(slide_pngs) / cols)
    sheet = Image.new("RGB", (cols * thumb_w + (cols + 1) * pad, rows * thumb_h + (rows + 1) * pad), (226, 232, 240))
    for i, p in enumerate(slide_pngs):
        r, c = divmod(i, cols)
        with Image.open(p) as im:
            thumb = im.convert("RGB").resize((thumb_w, thumb_h), Image.Resampling.LANCZOS)
        sheet.paste(thumb, (pad + c * (thumb_w + pad), pad + r * (thumb_h + pad)))
    sheet.save(out_path)
    return out_path


def render_slides(pptx_path: str | Path, out_dir: str | Path) -> Tuple[List[Path], Optional[Path], str]:
    """Slaytları PNG'ye ve toplu `contact_sheet.png` dosyasına çevirir; (dosyalar, ızgara, motor_adı) döner."""
    p = Path(pptx_path).resolve()
    out = Path(out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("slide_*.png"):
        old.unlink()

    slides = _export_via_powerpoint_com(p, out)
    engine = "powerpoint_com"
    if not slides:
        slides = _export_fallback_wireframe(p, out)
        engine = "pillow_wireframe"
    sheet = build_contact_sheet(slides, out / "contact_sheet.png")
    return slides, sheet, engine


def main(argv: Optional[Sequence[str]] = None) -> int:
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=".pptx sunumunun geometri ve görsel QA doğrulamasını yapar.")
    parser.add_argument("pptx_file", type=Path, help="Denetlenecek .pptx dosyası")
    parser.add_argument("--render-dir", type=Path, default=None, help="Slayt PNG önizlemelerinin yazılacağı klasör")
    args = parser.parse_args(argv)

    report = audit_pptx(args.pptx_file)
    print(f"Slayt sayısı: {report['slide_count']} ({report['width_in']}\" x {report['height_in']}\")")
    for w in report["warnings"]:
        print(f"  [UYARI] {w}")
    for e in report["errors"]:
        print(f"  [HATA]  {e}")

    if args.render_dir is not None:
        slides, sheet, engine = render_slides(args.pptx_file, args.render_dir)
        print(f"Görsel önizleme ({engine}): {len(slides)} slayt -> {args.render_dir}")
        if sheet:
            print(f"Toplu ızgara (Contact Sheet): {sheet}")

    print("DURUM:", "PASSED" if report["passed"] else "FAILED")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
