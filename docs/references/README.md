# Dış kaynaklar: app note, datasheet, el kitabı

Bu dosya `fetch_references.py` tarafından `manifest.json`'dan üretilir; elle düzenleme, manifest'i düzenleyip betiği çalıştır.

```
python docs/references/fetch_references.py
```

PDF'ler `files/`'a, düz metinleri `text/`'e iner; ikisi de git'e girmez (üretici belgeleri: kişisel ve kurum içi kullanım, yeniden dağıtım yok). Belgede arama: `text/` altında grep (ör. `rg -i "inductor" docs/references/text`). Metinde `=== s.N ===` satırı N. sayfanın başıdır.

Kurallar (GEMINI.md): bir app note ya da datasheet iddiasını kullanmadan önce buradaki belgeden doğrula ve sayfa numarasıyla atıf yap. Belge ile ölçüm çelişirse ölçüme güven ve ders belgesine yaz. analog.com ve st.com tarayıcı başlıkları olmayan isteği engelliyor; betik tarayıcı başlıklarıyla ister, olmazsa Internet Archive kopyasına düşer (`files/index.json` `source`).

## Kurulumda zaten olanlar (indirmeye gerek yok)

| Kaynak | Konum | Ne işe yarar |
|---|---|---|
| LTspice 26 yardımı (752 HTML sayfası) | `%LOCALAPPDATA%\Programs\ADI\LTspice\LTspiceHelp\` | Resmî ve güncel eleman, komut, `.meas` ve seçenek belgeleri. İnternetteki XVII yardım PDF'leri eski. |
| ADI LTspice AI Reference (15 `.md`) | `%LOCALAPPDATA%\LTspice\reference\` | `ReferenceDocs` / `search_ltspice_reference` ile aranır. Doğrulaması: `docs/LESSONS_LEARNED_LTSPICE_REFERENCE.md`. |
| ADI uygulama devreleri (~4200 `.asc`) | `%LOCALAPPDATA%\LTspice\examples\Applications\` | Her ADI parçasının datasheet'teki tipik uygulamasının çalışan şeması: referans tasarım. Ör. `LT8610.asc`, `LT1117-3.3.asc`. |
| Eğitim örnekleri (142) | `%LOCALAPPDATA%\LTspice\examples\Educational\` | `LoopGain*.asc`, `FRA\` (frekans tepkisi analizi), `MonteCarlo.asc`, `SampleAndHold.asc`, `Wien.asc`, `butter.asc`, `noise.asc`, `steptemp.asc`. |
| LTspice MCP köprüsü | `%LOCALAPPDATA%\Programs\ADI\LTspice\ltspice-mcp-bridge.exe` | LTspice 26'nın yerleşik MCP sunucusu; yardımda "Third Party AI Tools" sayfası. |

## LTspice ve SPICE yöntemi

| Belge | Neden | Projede | Sayfa |
|---|---|---|---|
| [SPICE Differentiation (Mike Engelhardt, LT Journal, Ocak 2015)](https://www.analog.com/media/en/technical-documentation/lt-journal-article/ltjournal-v24n4-01-df-spicedifferentiation-mikeengelhardt.pdf)<br>Analog Devices · `adi-ltj-spice-differentiation` | LTspice yazarından Newton iterasyonu, seyrek matris ve örtük integrasyon; trapez çınlaması (adımdan adıma salınım, s.3), Gear'ın yapay sönümü ve modified trap (s.4–6). LTspice 26'da `.options method` varsayılanı `trap`; modified trap yalnızca kaydedilen dalga formunu sonradan düzeltir (yerel yardım: integrationmethods.htm, options--setsimulatoroptions.htm). | GEMINI.md 'method=gear' kuralı | 7 |
| [Ngspice User's Manual](https://ngspice.sourceforge.io/docs/ngspice-manual.pdf)<br>ngspice projesi · `ngspice-manual` | Berkeley SPICE eleman ve model parametreleri (D, Q, M, BSIM), .model sözdizimi, yakınsama seçenekleri. LTspice'tan farklı yerler var: önce LTspice yardımına bak. | ltspice_llm/diagnostics.py, elle yazılan .model satırları | 763 |

## Güç: kompanzasyon, takma anı, LDO

| Belge | Neden | Projede | Sayfa |
|---|---|---|---|
| [AN149: Modeling and Loop Compensation Design of Switching Mode Power Supplies](https://www.analog.com/media/en/technical-documentation/application-notes/an149fa.pdf)<br>Analog Devices (Linear Technology) · `adi-an149` | Akım ve gerilim modlu SMPS küçük sinyal modeli, çevrim kazancı ölçümü ve kompanzasyon; LTspice FRA örnekleriyle birlikte kullan. | LTspice examples/Educational/FRA, LoopGain*.asc | 22 |
| [AN88: Ceramic Input Capacitors Can Cause Overvoltage Transients](https://www.analog.com/media/en/technical-documentation/application-notes/an88f.pdf)<br>Analog Devices (Linear Technology) · `adi-an88` | Kablo endüktansı + seramik giriş kondansatörü takma anında girişin iki katına kadar çıkan aşım üretir (s.1); elektrolitik/ESR ile sönümleme. | ltspice_llm/parasitics.py analyze_hotplug_transient, examples/15_hotplug_damping_validation.py | 4 |
| [Understanding the Terms and Definitions of LDO Voltage Regulators (SLVA079)](https://www.ti.com/lit/pdf/slva079)<br>Texas Instruments · `ti-slva079` | Dropout, Iq, PSRR, yük/hat regülasyonu tanımları; davranışsal LDO modelinin hangi parametreleri taşıması gerektiği. | GEMINI.md 'Davranışsal regülatör' kuralı | 13 |

## Analog: op-amp, E serisi

| Belge | Neden | Projede | Sayfa |
|---|---|---|---|
| [Op Amps for Everyone (SLOD006B, Ron Mancini)](http://www.ti.com/lit/an/slod006b/slod006b.pdf)<br>Texas Instruments · `ti-slod006` | Op-amp temel kitabı: geri besleme, kararlılık, aktif filtre tasarımı (Butterworth/Bessel/Chebyshev tabloları), osilatörler. TI artık sunmuyor; 2011 Internet Archive kopyası. | ltspice_llm/parasitics.py get_cable_parasitics (s.362: 6-12 nH/cm) | 464 |
| [Analog Engineer's Pocket Reference (SLYW038)](https://www.ti.com/lit/pdf/slyw038)<br>Texas Instruments · `ti-slyw038`<br>**Elle indir:** ti.com ücretsiz myTI girişi istiyor. Bağlantıyı tarayıcıda aç, indir, aşağıdaki yola kaydet ve betiği yeniden çalıştır. | Birim dönüşümleri, E-serisi değerleri, RC/LC formülleri, gürültü ve ADC formülleri tek yerde. | ltspice_llm/quantizer.py (E-serisi) | — |

## PCB: iz parazitikleri, dekuplaj, modül beslemesi

| Belge | Neden | Projede | Sayfa |
|---|---|---|---|
| [MT-101: Decoupling Techniques](https://www.analog.com/media/en/training-seminars/tutorials/MT-101.pdf)<br>Analog Devices · `adi-mt101` | Dekuplaj kondansatörü seçimi, yerleşimi, ESL ve ferrit kullanımı. | ltspice_llm/parasitics.py (s.6: NP0/C0G gerilim katsayısı) | 14 |
| [MT-094: Microstrip and Stripline Design](https://www.analog.com/media/en/training-seminars/tutorials/MT-094.pdf)<br>Analog Devices · `adi-mt094` | Microstrip karakteristik empedansı ve yayılma gecikmesi formülleri, geçerlilik aralıkları. | ltspice_llm/parasitics.py calculate_trace_parasitics | 7 |
| [Basic Linear Design, Chapter 12: Printed Circuit Board Design Issues](https://www.analog.com/media/en/training-seminars/design-handbooks/Basic-Linear-Design/Chapter12.pdf)<br>Analog Devices · `adi-basic-linear-design-ch12` | PCB iz direnci (bakır özdirenci, sac direnci), iz ve tel endüktansı, topraklama. | ltspice_llm/parasitics.py calculate_trace_parasitics (R_dc) | 97 |
| [NEO-M8 Hardware Integration Manual (UBX-13003557)](https://content.u-blox.com/sites/default/files/NEO-M8_HardwareIntegrationManual_(UBX-13003557).pdf)<br>u-blox · `ublox-neo-m8-him` | Besleme tasarımı (VCC hattına bobin/direnç koyma uyarısı), anten ve RF yerleşimi, V_BCKP. | GEMINI.md datasheet doğrulama kuralı | 26 |
