"""
GIF doğrulayıcı: yapı, okunabilirlik ve film şeridi
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Hareketli `.gif` dosyasını sunum ve belge içinde kullanılabilirlik açısından denetler (herhangi bir GIF olabilir;
metin süresi denetimi yalnızca `gif_encoder` ile yazılmış GIF'lerde vardır):
1. Çok kareli yapı (`is_animated`), sonsuz döngü (`loop == 0`), kare süreleri (`>= 20 ms`),
   dosya boyutu bütçesi ve Kare 0'ın (PDF/statik önizleme poster karesi) dolu olması.
2. Okunabilirlik & Anti-Flash Denetimi:
   - Poster Kare (`Kare 0`) ve kapanış karesi bekleme süreleri (`>= 900 ms`).
   - GIF89a `explainer-gif:` üstverisindeki her metin/durum etiketinin kesintisiz ekranda kalma
     süresi (`dwell_runs`: `< 1200 ms` hata, `< 1500 ms` uyarı).
   - Tek karelik anlık parlama/kayıp (1-frame glitch/flash) taraması.
3. Görsel Denetim (`--storyboard`): Animasyonun poster, gelişim, kritik düşüş/tepe vurgu anları
   ve kapanış karelerini 2x3 film şeridi (`storyboard.png`) olarak dışa aktarır.

Kullanım:
    python .agents/skills/explainer-gif/scripts/validate_gif.py anim.gif --storyboard filmseridi.png

Eşikler (1200/1500 ms metin, 900 ms poster) bu skill'in varsayılanlarıdır, ölçülmüş bir okuma standardı değildir.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
from PIL import Image, ImageDraw, ImageSequence

#: gif_encoder.COMMENT_PREFIX ile aynı olmalı (tests/test_skills.py denetler)
COMMENT_PREFIX = "explainer-gif:"


def _parse_dwell_metadata(raw_comment: Any) -> List[Dict[str, Any]]:
    """GIF89a `comment` alanındaki `explainer-gif:{...}` metin süre özetini çözümler."""
    if not raw_comment:
        return []
    text = raw_comment.decode("utf-8", errors="ignore") if isinstance(raw_comment, (bytes, bytearray)) else str(raw_comment)
    if not text.startswith(COMMENT_PREFIX):
        return []
    try:
        payload = json.loads(text[len(COMMENT_PREFIX) :])
        runs = payload.get("dwell_runs", [])
        return runs if isinstance(runs, list) else []
    except Exception:
        return []


def _select_storyboard_indices(durations: List[int], n_slots: int) -> List[int]:
    """Poster kare, vurgu (highlight) duraksamaları ve kapanış karesini kapsayan dengeli kare dizisi seçer."""
    n = len(durations)
    if n <= n_slots:
        return list(range(n))

    chosen = {0, n - 1}
    # Uzun bekleyen (vurgu/olay) kareleri öne al
    ranked_hl = sorted(
        (idx for idx in range(1, n - 1) if durations[idx] >= 350),
        key=lambda idx: durations[idx],
        reverse=True,
    )
    min_gap = max(2, n // (n_slots * 2))
    for idx in ranked_hl:
        if len(chosen) >= n_slots:
            break
        if all(abs(idx - c) >= min_gap for c in chosen):
            chosen.add(idx)

    # Kalan yuvaları zaman ekseninde dengeli doldur
    even_candidates = [int(round(i * (n - 1) / max(n_slots - 1, 1))) for i in range(n_slots)]
    for idx in even_candidates:
        if len(chosen) >= n_slots:
            break
        chosen.add(idx)

    idx_step = 1
    while len(chosen) < n_slots and idx_step < n:
        chosen.add(idx_step)
        idx_step += 1

    return sorted(chosen)[:n_slots]


def audit_gif(gif_path: str | Path, max_size_mb: float = 3.5) -> Dict[str, Any]:
    """`.gif` dosyasının teknik, okunabilirlik (dwell-time) ve PowerPoint uyumluluk denetimini yapar."""
    path = Path(gif_path)
    errors: List[str] = []
    warnings: List[str] = []

    if not path.is_file():
        return {"passed": False, "errors": [f"GIF dosyası bulunamadı: {path}"], "warnings": []}

    size_mb = path.stat().st_size / (1024.0 * 1024.0)
    if size_mb > max_size_mb:
        warnings.append(f"GIF dosya boyutu yüksek ({size_mb:.2f} MB > {max_size_mb:.2f} MB): slayt geçişini yavaşlatabilir.")

    dwell_runs: List[Dict[str, Any]] = []

    with Image.open(path) as im:
        n_frames = getattr(im, "n_frames", 1)
        width, height = im.size
        loop_val = im.info.get("loop", None)
        raw_comment = im.info.get("comment", None)

        if n_frames < 2:
            errors.append(f"Dosya hareketli değil (n_frames={n_frames}).")
        if loop_val != 0:
            warnings.append(f"GIF sonsuz döngüye ayarlı değil (loop={loop_val!r}); PowerPoint'te tek sefer oynayıp durabilir.")

        durations: List[int] = []
        thumbs: List[np.ndarray] = []
        rgb_samples: List[np.ndarray] = []
        sample_indices = {0, n_frames // 2, n_frames - 1}

        for idx, frame in enumerate(ImageSequence.Iterator(im)):
            if idx == 0 and raw_comment is None:
                raw_comment = frame.info.get("comment", None)
            dur = int(frame.info.get("duration", 50))
            durations.append(dur)
            rgb_full = frame.convert("RGB")
            if idx in sample_indices:
                rgb_samples.append(np.asarray(rgb_full, dtype=np.float32))
            # Hızlı parlama/kayıp denetimi için küçültülmüş 160x96 özet
            thumbs.append(np.asarray(rgb_full.resize((160, 96), Image.Resampling.BILINEAR), dtype=np.float32))

        dwell_runs = _parse_dwell_metadata(raw_comment)

        if any(d < 20 for d in durations):
            warnings.append(f"20 ms'den kısa kare süresi var (min={min(durations)} ms); bazı oynatıcılar 100 ms'ye yuvarlar.")

        if durations and durations[0] < 900:
            warnings.append(f"Poster Kare (Kare 0) bekleme süresi kısa ({durations[0]} ms < 900 ms); başlangıç anı hızlı geçebilir.")
        if len(durations) > 1 and durations[-1] < 900:
            warnings.append(f"Kapanış karesi bekleme süresi kısa ({durations[-1]} ms < 900 ms); döngü başa çok ani sarabilir.")

        # Metin okunabilirlik (dwell-time) denetimi
        for run in dwell_runs:
            ms = int(run.get("ms", 0))
            txt = str(run.get("text", ""))
            f0, f1 = run.get("f0", "?"), run.get("f1", "?")
            if ms < 1200:
                errors.append(
                    f"Okunamayacak kadar hızlı geçen yazı saptandı ({ms} ms < 1200 ms): {txt!r} (kare {f0}..{f1})."
                )
            elif ms < 1500:
                warnings.append(
                    f"Kısa süreli yazı saptandı ({ms} ms < 1500 ms): {txt!r} (kare {f0}..{f1})."
                )

        # Tek karelik anlık parlama/kaybolma (1-frame glitch) taraması (Kare 1..N-2)
        for k in range(2, len(thumbs) - 1):
            if durations[k] < 600:
                neighbor_diff = float(np.mean(np.abs(thumbs[k - 1] - thumbs[k + 1])))
                pop_diff = float(np.mean(np.abs(thumbs[k] - thumbs[k - 1])))
                if neighbor_diff < 0.75 and pop_diff > 2.4:
                    warnings.append(
                        f"Kare {k}'de tek karelik ({durations[k]} ms) anlık görsel parlama/kaybolma saptandı."
                    )

        if rgb_samples:
            std_f0 = float(np.std(rgb_samples[0]))
            if std_f0 < 3.5:
                errors.append("Kare 0 (Poster Kare) neredeyse tamamen düz/boş; PDF ve statik görünümde boş çıkar.")
            if len(rgb_samples) >= 2:
                diff = max(float(np.mean(np.abs(rgb_samples[i] - rgb_samples[0]))) for i in range(1, len(rgb_samples)))
                if diff < 0.05:
                    errors.append("Kareler arasında görünür hareket saptanmadı.")

    total_s = sum(durations) / 1000.0
    min_dwell_ms: Optional[int] = min((int(r.get("ms", 0)) for r in dwell_runs), default=None)
    return {
        "path": str(path),
        "size_mb": round(size_mb, 3),
        "width": width,
        "height": height,
        "frame_count": n_frames,
        "duration_s": round(total_s, 2),
        "min_dwell_ms": min_dwell_ms,
        "dwell_runs": dwell_runs,
        "loop": loop_val,
        "errors": errors,
        "warnings": warnings,
        "passed": len(errors) == 0,
    }


def render_storyboard(gif_path: str | Path, out_png: str | Path, cols: int = 3, rows: int = 2) -> Path:
    """GIF'in kilit ve vurgu karelerini zaman damgalı tek bir `storyboard.png` film şeridi olarak kaydeder."""
    path = Path(gif_path)
    n_slots = cols * rows
    selected_imgs: List[Image.Image] = []
    selected_labels: List[str] = []

    with Image.open(path) as im:
        n_frames = getattr(im, "n_frames", 1)
        all_durations = [int(f.info.get("duration", 50)) for f in ImageSequence.Iterator(im)]
        target_set = set(_select_storyboard_indices(all_durations, n_slots))

        im.seek(0)
        cum_ms = 0
        for idx, frame in enumerate(ImageSequence.Iterator(im)):
            dur = all_durations[idx]
            if idx in target_set:
                selected_imgs.append(frame.convert("RGB"))
                tag = "POSTER (Kare 0)" if idx == 0 else f"Kare {idx}/{n_frames - 1}"
                selected_labels.append(f"{tag}  |  t_oynatma = {cum_ms / 1000.0:.2f} sn  (bekleme: {dur} ms)")
            cum_ms += dur

    thumb_w = 560
    thumb_h = int(round(thumb_w * selected_imgs[0].height / max(selected_imgs[0].width, 1)))
    pad = 18
    header_h = 28
    sheet_w = cols * thumb_w + (cols + 1) * pad
    sheet_h = rows * (thumb_h + header_h) + (rows + 1) * pad

    sheet = Image.new("RGB", (sheet_w, sheet_h), (15, 23, 42))
    draw = ImageDraw.Draw(sheet)

    for i, (img_f, lbl) in enumerate(zip(selected_imgs, selected_labels)):
        r = i // cols
        c = i % cols
        ox = pad + c * (thumb_w + pad)
        oy = pad + r * (thumb_h + header_h + pad)
        draw.text((ox + 4, oy + 4), lbl, fill=(226, 232, 240))
        resized = img_f.resize((thumb_w, thumb_h), Image.Resampling.LANCZOS)
        sheet.paste(resized, (ox, oy + header_h))

    target = Path(out_png)
    target.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(str(target), format="PNG")
    return target


def main(argv: Optional[List[str]] = None) -> int:
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Hareketli .gif kalite, okunabilirlik ve film şeridi denetçisi.")
    parser.add_argument("gif", help="Denetlenecek .gif dosyası")
    parser.add_argument("--storyboard", default=None, help="Film şeridi PNG çıktı yolu")
    args = parser.parse_args(argv)

    rep = audit_gif(args.gif)
    if "frame_count" in rep:
        dwell_info = f" | min yazı kalışı: {rep['min_dwell_ms']} ms" if rep.get("min_dwell_ms") is not None else ""
        print(
            f"GIF: {rep['width']}x{rep['height']} px | {rep['frame_count']} kare | "
            f"{rep['duration_s']} sn | {rep['size_mb']:.2f} MB{dwell_info}"
        )
        for run in rep.get("dwell_runs", []):
            print(f"  [METİN] {run['ms']:4d} ms (kare {run['f0']:2d}..{run['f1']:2d}): {run['text']}")
    for err in rep["errors"]:
        print(f"  [HATA]  {err}")
    for warn in rep["warnings"]:
        print(f"  [UYARI] {warn}")

    if args.storyboard and rep["passed"]:
        sb = render_storyboard(args.gif, args.storyboard)
        print(f"Film şeridi: {sb}")

    print("DURUM:", "PASSED" if rep["passed"] else "FAILED")
    return 0 if rep["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
