# Görev: <devre / inceleme adı>

<!--
Mission Brief (Hassan, "Agentic Software Engineering"): bir tasarım, doğrulama ya da inceleme işini ajana
vermeden önce doldurulan iş emri. Kopyala: docs/briefs/<ad>.md. Kısa tut; kesinlik uzunluktan önemli.

Doldurmak için ajana sorduğun yol:
  "<X>'i tasarlamak/doğrulamak istiyorum. AskUserQuestion ile kaynak, yük, sınır değerler ve varsayımlar
   hakkında beni sorgula, sonra docs/MISSION_BRIEF_TEMPLATE.md'ye göre docs/briefs/<ad>.md yaz."
Uygulama yeni bir oturumda: "docs/briefs/<ad>.md'yi uygula; GEMINI.md simülasyon kurallarına uy."
-->

## Amaç
Bir iki cümle: kartta / devrede ne değişecek, neden.

## Başarı ölçütleri (her biri bir `.meas` ve bir eşik)
| Ölçüt | Analiz ve düğüm | `.meas` | Eşik | Eşiğin kaynağı |
|---|---|---|---|---|
| ör. Modül besleme geriliminin en düşük değeri | tran, yük darbesi sırasında `V(vcc_mod)` | `.meas tran vmin MIN V(vcc_mod) FROM 5m TO 6m` | ≥ modülün en düşük besleme gerilimi | modül datasheet'i s.N |
| ör. Saat hattının tepe gerilimi | tran, alıcı girişinde `V(sck_rx)` | `.meas tran vpk MAX V(sck_rx)` | ≤ VDD + 0.3 V | alıcı datasheet'i s.N (mutlak en büyük değerler) |

Eşik datasheet'ten ya da app note'tan gelir (`docs/references/text/` altında ara, sayfa numarasıyla yaz).
Kaynağı olmayan eşik "varsayım" bölümüne yazılır.

## Girdiler ve varsayımlar
- Kaynak, kablo, iz değerleri (ölçülmediyse varsayım olduğunu yaz).
- Hazır modüller kendi regülatör ve kondansatörleriyle; yükler datasheet tepe değerlerinden.
- Modeller: `.lib standard.*` / `find_model_definitions(...)`; davranışsal regülatör girişten akım çeker.

## Varyant planı (bir seferde tek değişken)
| Varyant | Değişen tek şey | Karşılaştırılan düğüm |
|---|---|---|
| temel | — | — |
| ör. +100 µF | C_bulk | `V(v5)` |

## Dokunma / sınırlar
- ör. Hazır modüllerin üzerindeki parçalar değişmez; yalnızca ana karta parça eklenir.
- Mevcut testler ve `examples/` sayıları üreten betikler değiştirilmez (gerekirse kullanıcıya sor).

## Özerklik zarfı
- Ajan karar verir: varyant adları, simülasyon penceresi, ölçüm adları.
- Ajan sorar: parça değeri ya da topoloji değişikliği önermeden önce kaynak/yük varsayımı değişiyorsa; bir
  datasheet değeri bulunamıyorsa; ölçüt eşiği tutmuyorsa (eşiği gevşetmez).
- Ajan asla: ölçmediği sayıyı yazmaz, simülasyonu "donanımda ölçüldü" diye sunmaz, kilitlenme korumasını kapatıp
  paralel LTspice çalıştırmaz.

## Kanıt (bitti tanımı)
- `examples/<nn>_<ad>.py` betiği ve `reports/<ad>.html` (`SimulationReport`): her ölçüt için soru, yöntem, dalga
  formu, ölçüm tablosu, tek değişkenli etki.
- Güvenilirlik: başarı logdan, `Maximum thread count`, `method` (salınımda Gear tekrarı), DC çalışma noktası,
  son değerin pencerede oturması.
- `python scripts/check.py` yeşil; kod değiştiyse yeni testler önce kırmızı görüldü.
- Donanımda ölçülecekler listesi.
