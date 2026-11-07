# narrative-pptx: JSON şeması ve yedi düzen

`deck_builder.build_deck(spec, output_path, base_dir=None)` şu yapıyı alır. Eksiksiz örnek:
[../assets/example_spec.json](../assets/example_spec.json).

```json
{
  "title": "Sunum başlığı",
  "author": "Yazar / ekip",
  "story_arc": "Sunumun tek cümlelik ana fikri",
  "theme": "midnight_editorial",
  "language": "tr",
  "slides": [ ... ]
}
```

- `title`, `author`, `story_arc`: .pptx belge özelliklerine (başlık, yazar, konu) yazılır.
- `theme`: `midnight_editorial` (varsayılan, Segoe UI + Consolas), `nordic_light`, `obsidian_pro` (Calibri).
  Yazı tipi adı dosyaya yazılır; makinede yoksa PowerPoint benzeriyle değiştirir.
- `language`: `en` (varsayılan) ya da `tr`. Yalnızca büyük harfe çevrilen alanları etkiler (`chapter`, `eyebrow`,
  `tag`, `label`, `phase`, `badge`, `scene_title`): Python'un `upper()`'ı "Geçiş"i "GEÇIŞ" yapar, `tr` "GEÇİŞ" yapar.
  Şemada verilmeyen alanların varsayılan metni de bu dildedir ("MEKANİZMA" / "MECHANISM", "▶ CANLI GIF" /
  "▶ LIVE GIF").
- `layout` verilmeyen slayt `evidence_bento` olur. Bilinmeyen düzen adı hata verir.

## Roller

`role` / `accent` değerleri: `blue`, `amber`, `emerald`, `rose`, `violet`, `slate`. Bilinmeyen rol slaytta `blue`, grafikte (`chart_artist`) gri olur.
Bir kavrama bir rol ver ve sunum boyunca değiştirme. Yaygın kullanım: `blue` birincil kavram, `amber` onun karşıtı
ya da ikinci varyant, `emerald` çözüm/doğrulama, `rose` uyarı/aşım, `violet` frekans ya da ikincil mekanizma,
`slate` nötr referans.

## Satır içi biçim

Metin alanlarında `**kalın**`, `*italik*` ve `` `değer` `` (eş aralıklı, rol renginde) PowerPoint biçimine çevrilir.

## Görseller

`image` alanı (yalnızca `mental_model` ve `evidence_bento`) isteğe bağlıdır. PNG, JPG ya da hareketli GIF olabilir;
en-boy oranı korunarak kartın ortasına yerleşir. Göreli yol komut satırında JSON dosyasının klasörüne, Python'da
`base_dir`'e (verilmezse çalışma klasörüne) göre çözülür. GIF verilen `evidence_bento` kartına "▶ CANLI GIF" çipi
eklenir.

## Düzenler

### 1. `hero_cover`: koyu kapak
- `eyebrow`: üst rozet, `title`: ana başlık, `subtitle`: 1-2 cümlelik giriş
- `pillars`: en çok 4 bölüm kartı `[{"role", "tag", "title", "body"}]`
- `meta`: alt bilgi satırı, `speaker_notes`

### 2. `mental_model`: düzeneği ve kavramları tanıtan sahne
- `chapter`, `accent`, `assertion`
- `image`: sistemin yatay şeması (önerilen ~8,1" × 2,2"; `scene_summary` yoksa tam genişlik ~11,8" × 2,2")
- `scene_title`, `scene_summary`: şemanın sağındaki özet kutusu (isteğe bağlı)
- `characters`: 2-3 kavram kartı `[{"role", "title", "law", "body", "trait"}]` (`law`: sağ üst çip, `trait`: alt çip)
- `narrative_bridge`, `speaker_notes`

### 3. `evidence_bento`: solda görsel kanıt, sağda ölçüm ve mekanizma
- `chapter`, `accent`, `assertion`
- `image`: sol karta (~7,5" × 4,3") yerleşen grafik ya da GIF, `image_caption`: tek satırlık alt yazı
- `metrics`: 1-2 KPI kartı `[{"role", "label", "value", "note"}]`
- `mechanism`: `{"role", "badge", "title", "body", "takeaway_pill"}`
- `narrative_bridge`, `speaker_notes`

### 4. `split_duality`: yan yana karşılaştırma (önce/sonra, A/B)
- `chapter`, `accent`, `assertion`, `center_badge` (varsayılan `"↔"`)
- `left`, `right`: `{"role", "title", "subtitle", "rows": [{"label", "value"}], "summary"}`
- `narrative_bridge`, `speaker_notes`

### 5. `process_arc`: 3-4 aşamalı zaman akışı
- `chapter`, `accent`, `assertion`
- `stages`: `[{"step", "phase", "title", "metric", "body", "role"}]`
- `narrative_bridge`, `speaker_notes`

### 6. `comparison_matrix`: tablo ve yorum kartı
- `chapter`, `accent`, `assertion`
- `headers`: sütun başlıkları, `rows`: satır listeleri, `highlight_row`: vurgulanan satır (0 tabanlı)
- `insight`: `{"role", "title", "body"}`
- `narrative_bridge`, `speaker_notes`

### 7. `synthesis_closing`: koyu kapanış
- `chapter`, `accent`, `assertion`
- `pillars`: 3 özet kartı `[{"role", "badge", "title", "body", "rule"}]`
- `closing_banner`: alt tam genişlik mesaj
- `speaker_notes`

## Açıklamalı grafik (`chart_artist.render_annotated_panels`)

`render_annotated_panels(out_path, panels, figsize=(7.55, 4.35), dpi=300, theme_name=None, sharex=True)`; her panel:

- `title`, `xlabel`, `ylabel`, `xlim`, `ylim`
- `traces`: `[{"x", "y", "label", "color", "width", "style", "fill"}]` (`color`: rol ya da `#RRGGBB`)
- `hlines`: `[{"y", "label", "color", "align"}]`, `vspans`: `[{"x0", "x1", "label", "role", "alpha"}]`
- `callouts`: `[{"xy": (x, y), "text", "offset": (dx, dy), "role", "rad"}]`: `xy` eğrinin üstündeki ölçülen nokta
- `legend_loc`: `"upper right"` vb.; `None` lejantı gizler
