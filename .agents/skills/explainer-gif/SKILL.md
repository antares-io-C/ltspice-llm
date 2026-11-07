---
name: explainer-gif
description: >-
  Kullanıcı bir dalga formunu, zaman izini ya da parametre taramasını hareketli GIF olarak göstermek istediğinde
  kullan: "GIF", "hareketli grafik", "dalga formunu canlandır", "taramayı canlandır", "animated GIF",
  "animate waveform", "animate sweep", HTML/WebGL (Three.js) sahnesini GIF'e çevirme. Bütün bir sunum isteniyorsa
  narrative-pptx'i kullan; PowerPoint geçiş animasyonları bu skill'in işi değildir.
compatibility: Python 3.10+, numpy, matplotlib, Pillow (pip install -e ".[media]"); koyu tema için scipy; HTML/WebGL sahnesi için isteğe bağlı Playwright.
---

# Hareketli açıklayıcı GIF

Bir zaman izini ya da eğri ailesini, sunumda kendiliğinden dönen ve yazıları okunacak kadar ekranda kalan bir
GIF'e çevirir. Betikler `ltspice_llm`'e bağlı değildir. `SimulationReport` GIF gömmez; GIF raporun yerine geçmez,
aynı ölçümlerden üretilir.

## Kurulum

```bash
pip install -e ".[media]"                  # Pillow (+ python-pptx)
pip install playwright                     # yalnızca HTML/WebGL sahnesi için
python -m playwright install chromium
```

## Hangi araç

| İhtiyaç | Fonksiyon (`scripts/`) |
|---|---|
| Bir ya da birkaç izi imleçle soldan sağa çizmek (açık tema) | `gif_archetypes.render_scope_sweep_gif` |
| Aynısı koyu zemin + ışıma teması | `gif_archetypes.render_phosphor_scope_gif` |
| Bir parametrenin değerleri arasında eğri geçişi | `gif_archetypes.render_parameter_morph_gif` |
| Three.js / GLSL / Canvas sahnesi | `webgl_capture.capture_html_scene_to_gif` |
| Kendi çizimin (devre, mekanik benzetme) | kendi `Figure` döngün + `gif_encoder` yardımcıları |
| Her GIF'i denetlemek | `validate_gif.py` |

İmzalar, girdi yapıları ve kendi kare döngünü yazma kalıbı: [references/archetypes.md](references/archetypes.md).

## Okunabilirlik kuralları (zorunlu)

Arketipler bunları kendisi uygular; kendi döngünü yazarken sen uygularsın.

1. **Veri simülasyondan gelir.** İzleri `.raw`'dan oku (bu depoda `ltspice_llm.report.read_trace`), balon ve
   aşama metnindeki sayıları `.meas`'ten biçimlendir. Gerçek veri varken sentetik eğri çizme. `.step`'li koşumda
   `read_trace` yalnızca ilk adımı okur: tarama için [references/archetypes.md](references/archetypes.md) "Tarama".
2. **Kare 0 posterdir** (eğri geçişi hariç: orada kare 0 ilk durumdur). PDF, küçük resim ve baskı ilk kareyi gösterir: kare 0'da izlerin tamamı, bütün balonlar ve
   en önemli an (`poster_time`) olur ve uzun bekler (tarama arketiplerinde 1400 ms). Boş eksenle başlayan GIF PDF'te
   boş görünür.
3. **Metin zaman penceresine bağlıdır, anlık sinyale değil.** Aşama metnini `stages=[{"t": ...}]` ile ver.
   `v < -0.5` gibi bir eşik, tepe sönünce metni okunmadan kaldırır.
4. **Balonlar kalıcıdır.** Olay balonu tarama o ana gelince açılır ve döngü sonuna kadar kalır.
5. **Dik bölgede ağır çekim.** Kenar ve tepe bölgelerinde kareler sıklaşır (`adaptive_event_samples`,
   `event_windows` verilmezse eğimden bulunur); kritik anlar (`highlight_times`, olay anları) tam bir kareye düşer.
6. **En az okuma süresi.** Her durum metni en az `min_text_dwell_ms` (arketipe göre 1750-1800 ms) ve
   `450 ms + 230 ms × kelime` (en çok 2700 ms) görünür; `enforce_minimum_dwell` eksik süreyi metnin kendi karelerine
   ekler. Hızlı salınımda metni her yarım periyotta değiştirme: hareketi eğri göstersin, metin 3-4 sakin aşama olsun.
7. **Tek palet.** Bütün kareler ortak bir 256 renk paletiyle kodlanır (`encode_master_palette_gif`); kare başına
   palet arka planı ve yazıyı titretir.

Süre eşikleri bu skill'in varsayılanlarıdır; ölçülmüş bir okuma standardı değildir.

## Kendi sahneni tasarlarken

- Soyut sembol yerine somut bir benzetme, simülasyon dizileriyle sürülür (ör. kondansatör için esneyen zar, bobin için
  ağır volan). Benzetmenin her hareketi bir izden gelir.
- Göstergeyi mekanizmanın yanına koy; göz paneller arasında gidip gelmesin.
- Tuvalin çoğu tek bir sahne, altta tek bir zaman çizgisi: üst üste beş osiloskop yerine.
- Tel üstündeki taneciklerin hızı ve yönü akım dizisinden gelir (`integrate_particle_phases`): akım sıfırsa durur,
  işaret değişince geri döner. Hız nitel bir göstergedir (her GIF kendi tepe akımına göre ölçeklenir); iki GIF'i
  karşılaştıracaksan aynı `ylim`'i ver ve hızı karşılaştırma aracı yapma.

## İş akışı

1. İzleri ve ölçümleri oku, olay anlarını `.meas`'ten al. Uçtan uca örnek: `examples/19_simulation_to_slides.py`.
2. Arketipi çağır ya da kendi döngünü yaz.
3. Denetle ve film şeridine bak:

   ```bash
   python .agents/skills/explainer-gif/scripts/validate_gif.py anim.gif --storyboard sb.png
   ```

   `DURUM: PASSED` olmalı (film şeridi yalnızca geçen GIF için yazılır); `[METİN]` satırlarında her metnin süresini
   gör. `sb.png`'de balonların eksen, lejant ya da eğriyle çakışmadığını kontrol et; çakışıyorsa `offset`'i
   değiştirip yeniden üret. Dar bir tepe ağır çekime girmediyse `event_windows`'u elle ver.
4. Sunuma koy: GIF yolunu `narrative-pptx` slaytının `image` alanına ver. PowerPoint slayt gösterisinde döngüyle
   oynatır. Boyut `figsize × dpi` ile belirlenir (varsayılan 7,55" × 4,35" @ 150 dpi ≈ 1132 × 652 px);
   doğrulayıcı 3,5 MB üstünde uyarır.
5. Bu depoda GIF'i üreten betik `examples/` altında kalır; çıktı yolunu `examples/study_helpers.report_path(ad)` ile
   kur (testler `reports/`'u ezmesin).
