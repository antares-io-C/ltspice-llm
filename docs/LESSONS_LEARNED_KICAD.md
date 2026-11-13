# KiCad kart okuyucu: ölçülen tuzaklar ve dersler

`ltspice_llm/board.py` bir `.kicad_pcb` dosyasını KiCad olmadan, yalnız okuyarak açar; iki pad arasındaki iz yolunu
bulur ve `calculate_trace_parasitics` ile R/L/C'ye çevirir. Okuyucunun doğruluğu KiCad'in kendi hesabıyla ölçülür:
`examples/20_kicad_board_checks.py` kartın geçici kopyasını KiCad 10.0.3'ün Python'una (`pcbnew`) hesaplatır
(`examples/kicad_oracle.py`) ve sonucu okuyucununkiyle karşılaştırır. Aşağıdaki her madde o ölçümlerden çıktı
(2026-11-12/13).

## 1. KiCad'in Python'unu güvenle çalıştırmak

- **`pcbnew.LoadBoard` kartın yanına `.kicad_pro` / `.kicad_prl` yazar.** Kullanıcının proje klasöründe çalıştırma: kartı
  önce geçici klasöre kopyala. `examples/20` bunu her kartta kendisi yapar.
- **KiCad uyarı pencereleri kullanıcının ekranında açılır ve süreç cevap bekler.** İki ayrı kaynak var, ikisi de kapatılmalı:
  - assert'ler ("wxWidgets Debug Alert"; ör. katmansız `PCB_VIA.GetWidth()`): `wx.DisableAsserts()`.
  - wxLog bilgi/uyarı iletileri: `RoyalBlue54L-Feather` demosunu yüklemek "Font 'Ubuntu Sans' not found; substituting
    'Verdana Bold'." kutusunu açtı. `wx.Log.SetActiveTarget(wx.LogStderr())` iletiyi stderr'e yazar; hedef nesnesinin
    referansı modül düzeyinde tutulur.

  `DisableAsserts` log kutularını kapatmaz. KiCad süreçleri yine de zaman sınırıyla çalışır; zaman aşımı hata sayılır
  (`examples/20`: "asılı kaldı").
- `-I` (izole kip) ile `pcbnew` içe aktarılabiliyor; okunan kart güvenilmeyen veridir.
- `ZONE_FILLER.Fill` yeni kurulmuş (`CreateEmptyBoard`) kartta pad ya da via varsa süreci çökertir (rc 139); kaydet,
  `LoadBoard` ile yeniden yükle, sonra doldur.
- `BOARD_STACKUP` SWIG'de sarılı değil; yüklenmiş kartta `m_HasStackup = True` varsayılan katman yapısını yazdırır.
  `FOOTPRINT.JumperPadGroups()` de ayarlanamıyor: test kartı bu alanı metinle ekler, KiCad dosyayı yükleyip kendisi
  yeniden yazar (son dosyadaki sözdizimi KiCad'indir).
- `kicad-cli pcb export stats` net başına iz uzunluğu vermez ve JSON anahtarlarını KiCad'in diline çevirir.
- Büyük kartta KiCad'in örtme örneklemesi dakikalar sürer (`video.kicad_pcb` 5.9 MB, 7 dakikada bitmedi): `--demolar`
  3 MB'tan büyük demoları atlar.

## 2. Dosya biçimi

- KiCad 10 (`version 20260206`) neti adla yazar: `(net "/5V")`. KiCad 9 ve öncesi numarayla: segment ve via'da `(net 5)`,
  padde `(net 5 "/5V")`, dosya başında `(net 5 "/5V")` tablosu (`ecc83-pp.kicad_pcb` satır 120-122, 2183, 5196).
- Pad `(at x y açı)`: konum footprint'e göre (dönmemiş eksende), açı mutlak. Kart konumu
  `(fx + px·cos + py·sin, fy − px·sin + py·cos)` (y aşağı).
- Footprint içi bağlantı: `(duplicate_pad_numbers_are_jumpers yes)` aynı numaralı padleri, `(jumper_pad_groups ("1" "3"))`
  sayılan padleri içten bağlar. KiCad bağlantı kümesinde sayar; kartta iz değildir. Aynı adlı birden çok besleme pini
  olan bir geliştirme kartı soketi (ör. iki 3V3, üç GND pini) böyle tanımlanır.
- Bakır katmanda nete bağlı çizimler (`gr_rect ... (fill yes) (layer "F.Cu") (net 1)`) KiCad'de padleri bağlar.
  Okuyucu çizimi bakır saymaz, yalnız fark eder ve söyler (One-Air-Max demosunda USB kabuk padleri).
- Dolu döküm (`filled_polygon`) tek dış hattır, delikler sıfır genişlikli yarıkla bağlıdır: çift-tek kuralı çalışır.
  KiCad'in `HitTestFilledArea`'sı kenardaki noktayı içeride sayar (interf_u: döküm kenarı bir izin orta çizgisiyle
  çakışıyordu, okuyucu önce örtmeyi 0, KiCad 1 buldu).

## 3. Bağlantı ve uzunluk

- KiCad bakır çakışmasını bağlantı sayar: ucu ötekinin içinde olmayan çapraz (X) izler bağlı, bölünmemiş T bağlı,
  örtüşen iki pad bağlı.
- Ark kirişlere bölünerek izlenir. Kirişlerin her biri değdiği ize, pada ya da via'ya bağ kurarsa yol kısalır: ilk taslakta
  `TP5 -> TP6` 17.854 yerine 17.461 mm çıktı. Bağ yalnız gerçek kesişimde, iz ucunda ve değen kiriş dizisi başına en yakın
  noktada kurulur.
- Arkta KiCad'in iz uzunluğu, dosyadaki üç noktadan geçen yayın uzunluğundan biraz farklı: en çok 4.9e-5 mm
  (r = 0.24 mm, bağıl 1.3e-4). Merkezi nm'ye yuvarlamak açıklamadı, nedeni ölçülmedi; fiziksel önemi yok.
- Neredeyse paralel iki parçada en yakın nokta hesabı ortak uçta 0 yerine 9e-5 mm verdi (100 mm'lik parçalar); dört
  uç-parça adayı da denenir (test-reviewer, Hypothesis ci profili).

## 4. Karşılaştırma sonucu (KiCad 10.0.3, 2026-11-13)

| Kartlar | Sonuç |
|---|---|
| Sentetik test kartları (2) | aynı |
| 3 MB altındaki demolar (13) | 12 aynı; One-Air-Max'te 1 ada farkı (bakır çizimli net, bilinen sınır) |
| Gerçek iki katmanlı proje kartları, geçici kopya (3; depoya girmez) | aynı |

Karşılaştırılan: net başına iz uzunluğu ve iz/via sayısı, pad konumu/açısı/boyutu/katmanı/neti, pad adaları, düz izlerin
altındaki örtme oranı, döküm içi örnek noktalar. Ölçülmeyen: via endüktansı (kaynağı yok, yalnız girdi), stripline (iç
katman; hata verilir), KiCad 6-7 dosyaları (gerçek dosya yok; uyarı verilir).
