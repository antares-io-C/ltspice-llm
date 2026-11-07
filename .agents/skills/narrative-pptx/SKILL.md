---
name: narrative-pptx
description: >-
  Kullanıcı bir simülasyon sonucunu, devreyi ya da teknik bir konuyu PowerPoint sunumuna dönüştürmek istediğinde
  kullan: "sunum hazırla", "slaytlara dök", "bulguları sunuma dök", "pptx", "PowerPoint", "deck", "presentation",
  "slide deck"; sunuma konacak açıklamalı grafik (PNG) de bu skill'dedir. Yalnızca GIF/animasyon isteniyorsa
  explainer-gif'i, HTML simülasyon raporu için SimulationReport'u kullan.
compatibility: Python 3.10+, python-pptx ve Pillow (pip install -e ".[media]"). Gerçek slayt önizlemesi için Windows'ta PowerPoint; yoksa kaba tel kafes önizleme.
---

# Hikâye akışlı .pptx sunumu

Teknik bir konuyu birbirinden kopuk madde listeleri yerine, yalnızca başlıkları okunduğunda bile anlaşılan bir
hikâyeye çevirir. Çıktı yerel PowerPoint şekilleri ve metin kutularından oluşur; her şey PowerPoint'te düzenlenebilir.

## Kurulum

```bash
pip install -e ".[media]"        # python-pptx + Pillow (depo kökünde)
```

Betikler `ltspice_llm`'e bağlı değildir; yalnızca python-pptx, Pillow ve (grafik için) matplotlib ister.

## Kurallar

1. **Önce hikâye omurgası.** Slayt yazmadan önce konuyu bir insana baştan sona anlatır gibi sahnelere böl.
   İlk bir iki sahne düzeneği kurar (`mental_model`): konuyu bilmeyen biri ne ölçüldüğünü görmeli.
2. **Başlık bir iddia cümlesidir.** "Sonuçlar", "Bode diyagramı" gibi etiket başlık yok; her slaytın `assertion`
   alanı grafiğin gösterdiğini tam cümleyle söyler.
3. **Madde duvarı yok.** Gövde görsel kanıt (`evidence_bento`), ikili karşılaştırma (`split_duality`) ya da aşama
   kartlarıdır (`process_arc`). İçerik slaytlarının altında bir sonrakine bağlayan `narrative_bridge`, her slaytta
   tam paragraf `speaker_notes` olur.
4. **Renk anlam taşır.** Bir kavram sunum boyunca aynı rolde kalır (`blue`, `amber`, `emerald`, `rose`, `violet`,
   `slate`). Kapak ve kapanış koyu, içerik slaytları açık zeminlidir.
5. **Her sayı ölçümden gelir.** KPI kutusundaki, tablodaki ve metindeki her sayı `.meas`'ten ya da `.raw`'dan
   biçimlendirilir (bu depoda `ltspice_llm.report.format_value`); elle sayı yazılmaz. Simülasyon sonucu "ölçüldü"
   diye sunulmaz. Sunum, depo kuralının istediği `SimulationReport` HTML raporunun yerine geçmez; aynı
   ölçümlerden üretilir.

## İş akışı

Uçtan uca örnek (LTspice koşumu, grafik, GIF, sunum, rapor): `examples/19_simulation_to_slides.py`.

1. **Şemayı başlat.** [assets/example_spec.json](assets/example_spec.json) yedi düzenin hepsini içeren bir
   iskelettir; alanlar [references/json_schema_guide.md](references/json_schema_guide.md)'de. Türkçe sunumda
   `"language": "tr"` ver (çip ve rozetlerde i → İ).
2. **Görselleri üret.**
   - Durağan grafik: `chart_artist.render_annotated_panels`, kırılma noktasına okla bağlı açıklama balonu çizer.
   - Hareketli görsel: `explainer-gif` skill'i. GIF'i `image` alanına vermek yeter; PowerPoint slayt gösterisinde
     oynatır, PDF'e ilk kare (poster) düşer.
3. **Derle.** Asıl yol Python'dur: `examples/` altındaki betik şemayı sözlük olarak kurar, sayıları
   `format_value(result.measurements[...])` ile yazar (göreli `image` yolları `base_dir`'e göre çözülür):

   ```python
   sys.path.insert(0, str(ROOT / ".agents/skills/narrative-pptx/scripts"))
   from deck_builder import build_deck
   from study_helpers import report_path            # testler reports/'u ezmesin
   out = build_deck(spec, report_path("sunum.pptx"), base_dir=asset_dir)
   ```

   Komut satırı iskeleti önizlemek içindir (görsel yolları JSON dosyasına göre çözülür):

   ```bash
   python .agents/skills/narrative-pptx/scripts/deck_builder.py spec.json -o sunum.pptx
   ```

   Hedef dosya PowerPoint'te açıksa `build_deck` `_v2`, `_v3`... adıyla yazar; sonraki adımda **dönen ya da basılan
   yolu** denetle.
4. **Denetle (zorunlu).**

   ```bash
   python .agents/skills/narrative-pptx/scripts/validate_deck.py <sunum.pptx> --render-dir <klasör>
   ```

   `DURUM: PASSED` ve `[HATA]`/`[UYARI]` olmamalı. Sonra `<klasör>/contact_sheet.png`'ye bak: taşan metin, üst üste
   binen etiket, boş kalan kart varsa metni kısaltıp yeniden derle. Windows'ta PowerPoint kuruluysa gerçek slaytlar
   işlenir (açık sunumlara dokunulmaz). Yoksa (ya da PowerPoint 90 sn'de yanıt vermezse) kutuları ve görselleri gösteren
   kaba önizleme çıkar; taşma orada görünmez, taşma uyarılarını metni kısaltarak sıfırla.

## Sık hatalar

- Etiket başlık ("Sonuçlar") ya da JSON'a elle yazılmış sayı; örnek şemadaki `⟨.meas⟩` yer tutucusunun kalması.
- Türkçe sunumda `"language": "tr"` unutulunca çiplerde "GEÇIŞ".
- `_v2` yazıldıktan sonra eski dosyanın denetlenmesi.
- Fazla öğe: kapak en çok 4, `characters` 3, `metrics` 2, `stages` 4, kapanış 3 kart gösterir; fazlası düşer.
  `deck_builder.spec_warnings(spec)` bunu ve kalan yer tutucuları bildirir (komut satırı `[UYARI]` basar).
