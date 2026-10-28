# 📘 ADI LTspice AI Reference: Doğrulama ve Lessons Learned

**Tarih:** 2026-10-01 · **Ortam:** LTspice 26.1.1 (Windows 11) · **Kaynak:** `%LOCALAPPDATA%\LTspice\reference\*.md`
(15 belge; [github.com/analogdevicesinc/ltspice-reference](https://github.com/analogdevicesinc/ltspice-reference))

LTspice 26, yapay zekâ asistanları için yazılmış resmî bir başvuru seti kuruyor: sözdizimi, eleman
parametreleri, `.meas` örnekleri, yakınsama sorun giderme, `.asc` biçimi, `.meas` SQLite veritabanı ve
komut satırı anahtarları. Bu belge, bu setteki iddiaların LTspice 26.1.1 üzerinde **ölçülerek**
doğrulanmasını ve bu sayede `ltspice_llm`'de bulunan hataları kaydeder. Buradaki her sayı
`examples/12_ltspice_reference_checks.py` (~15 sn) ile ya da `tests/data/ref26_*` gerçek loglarıyla
yeniden üretilebilir.

Belgeler depoya **kopyalanmadı**. `ltspice_llm.reference.ReferenceDocs` ve `search_ltspice_reference` aracı
onları kurulumdan okur, böylece LTspice güncellendiğinde kendiliğinden yenilenirler. ADI'nin kendi kuralı:
"Eğitim verisiyle yerel belge çelişirse yerel belgeye güvenin."

---

## 0. Sonuç özeti

Referansla karşılaştırma, adaptörde **sessizce yanlış sonuç üreten** beş hata ortaya çıkardı:

| Durum | Önce | Sonra |
| --- | --- | --- |
| DC çalışma noktası bulunamadı (`Simulation Failed: Iteration limit reached`) | **`success=True`, `errors=[]`** | FAILED, `DC_OP_CONVERGENCE_FAILURE` |
| cp1252 `.asc` içinde `1µ` | `µ` siliniyordu, C = 1 F oldu, τ ölçülemedi | τ = 0,99967 ms (R·C = 1 ms) |
| UTF-16 `.asc` | "log üretilemedi" | Çalışıyor |
| `.meas AC fc WHEN mag(V(out))=1/sqrt(2)` | **fc = 1.0** | fc = 1000,97 Hz (.db ile birebir) |
| `meascplxfmt=polar` ile `ph()`/`im()` | **+44,97° / +0,5** (işaret kayıp) | −44,97° / −0,5 |
| `PULSE(0 1 0 0 0 ...)` (Trise = 0) | FAILED + ilgisiz `method=gear` önerisi | SUCCESS + uyarı |
| `AscSchematic` ile akım kaynağı | Netlistte `I1 NC_01 NC_02` (kopuk) | `I1 N001 0 1m` |

Ayrıca teşhis motoru LTspice 26'nın gerçek mesajlarının çoğunu tanımıyordu. Eksik model, eksik alt
devre, eksik dosya, iki analiz ve reddedilen `.step` mesajlarının hepsi `UNKNOWN_ERROR` oluyor, ilgisiz bir
`gear` yaması öneriliyordu.

---

## 1. Referans iddialarının doğrulaması

| İddia (belge) | Ölçüm (LTspice 26.1.1) | Karar |
| --- | --- | --- |
| `1M` = 1 mili, `1Meg` = 1 mega (SPICE-SYNTAX) | 1 V / `1M` → **1000 A**; 1 V / `1Meg` → 1 µA | ✅ Doğru |
| `4Farads` = 4 fF (SPICE-SYNTAX) | \|I(C)\| @1 kHz = 2,513e-11 A = 2π·f·4 fF | ✅ Doğru |
| `.options reject_number_tails` sayı kuyruklarını hata yapar | `4Farads`, **`100pF` ve `5V` de** sözdizimi hatası | ✅ Doğru ama katı. Varsayılan yapılmadı |
| Netlistte `µ`/`μ` kullanma | UTF-8 `µ` (U+00B5), Yunanca `μ` (U+03BC) ve cp1252 0xB5: dördü de τ = 0,99967 ms | ⚠️ Netlistte çalışıyor. Taşınabilirlik için `u` yaz |
| `.asc` dosyaları Windows-1252 (SCHEMATIC) | ADI örneklerinin 3088/4334'ü cp1252, 21'i BOM'suz UTF-16LE, 1225'i ASCII. 3062 dosyada `SYMATTR Value …µ` var. UTF-8 `.asc` → `1Âµ` → τ FAIL | ✅ Doğru (UTF-16 da kabul ediliyor) |
| `current` sembolü pinleri (0,0)/(0,80) (SCHEMATIC) | `lib/sym/current.asy`: `PIN 0 0`, `PIN 0 80` | ✅ Doğru. Bizim tablo yanlıştı |
| Aynalama: önce px negatiflenir, sonra döndürülür | `transform_pin` M0..M270 bu kurala uyuyordu | ✅ (testle sabitlendi) |
| Tek adımlık `.step` koşulmadan reddedilir (TROUBLESHOOTING) | `This results in only one step.` / `Only duplicate values listed.` / `Increment must not be zero.` / `Expected "(" here.` (param eksik) / `Expected number here.` (dec yanlış yerde), rc=1, `.raw` yok | ✅ Metinler birebir |
| `.dc` yalnızca bağımsız kaynak tarar | `DC sweep source must be an independent source.` / `This sweep spec results in only one point.` / `Expected expression or literal here.` | ✅ Birebir |
| Kısmen tekrarlı `.step list` | `Removed duplicate values.` (rc=0, koşum sürüyor) | ➕ Belgede yok. Uyarı olarak işlendi |
| Bir koşumda tek analiz (SCHEMATIC) | `.tran` + `.ac` → `More than one analysis specified.`, rc=1, `.raw` yok | ✅ Doğru |
| `convreport` / `.options debugtran` zorluk puanları | `Device Convergence Difficulty Score: B1 : 98.6`, `Node …: n : 83.4`; başarılı buck'ta S1 = 0,21 | ✅ Biçim doğrulandı, ayrıştırılıyor |
| `uic` + V kaynağı ∥ boş C → "time step too small" (TROUBLESHOOTING) | `V1 5V ∥ 1u ∥ 1k`, `.tran 1m uic`: **hata yok**, vmax = 5 | ❌ Tekrarlanamadı. "Çözüm değildir" tavsiyesi yine de kaldı (fiziksel olmayan başlangıç) |
| Bobin varsayılan `Rser=1m` (DEVICE-MODELS) | 1 V / 1 µH → I = **1000 A**; `.options thev_induc=1` → `over-defined circuit matrix` | ✅ Doğru |
| Netlist ile standart modeller (DEVICE-MODELS) | `D1 b 0 1N4148` → `Undefined model "1n4148"`; `.lib standard.dio` ile V(b) = **0,548 V**. `.model 1N4148 D` (varsayılan parametre) V(b) = **0,629 V** | ➕ Belgede açık değil. Kütüphane araması eklendi |
| `.lib` göreli yolu `lib/sub` içinde aranır | `X1 … LT1001` + `.lib LTC.lib` → izleyici çıkışı 0,1 V. `.lib LT1001.sub` → `File not found.` | ✅ Doğru (dosya adı ≠ parça adı) |
| AC `.meas` sonuçları karmaşık, `meascplxfmt` biçimi seçer (MEAS) | bode `(-3.006dB,-44.97°)`, polar `(0.7075,-44.97°)`, cartesian `(0.5005,-0.4999)`; ph @1k polar `(44.97,180°)` = −44,97° | ✅ Doğru |
| `.meas` sonuçları SQLite `.db`'ye yazılır (MEASURE-DATABASE) | 5 tablo; Results'ta `Value` yok; WHEN → `AT`, diğerleri → `Re`; `Success=0` başarısızda. Değerler biçimden bağımsız, tam double | ✅ Şema birebir |
| `-netlist` şemayı başsız netliste çevirir (QUICKSTART) | 0,5 sn; bağlantısız pinler `NC_01…` adını alıyor; netlister `u`'yu `µ` yazıyor | ✅ Doğru |
| `-version` | `26.1.1` basar | ✅ Doğru. Not: `-v` sürüm basmaz, arayüzü açıp bekler |
| `-I<path>` arama yolu ekler (QUICKSTART) | `-netlist -I<dizin> x.asc` → `Trouble reading file "-I..."`, `-I <dizin>` de aynı | ❌ Çalışmıyor. Geçici kopya kaynağın klasörüne konuyor |
| `.four` çıktısı logda | `Total Harmonic Distortion: 17.342839%` (tanh(2·sin)) | ✅ Ayrıştırılıyor (`fourier` alanı) |
| Dalga sıkıştırma varsayılanı `plotwinsize=300` (SIMULATION-COMMANDS) | LTspice 26.0.0 değişiklik günlüğü: "Change default value for waveform compression to disabled" | ⚠️ Belgeler kendi içinde çelişiyor. Önceki ölçüm (`plotwinsize=0` ~1,02x) değişmedi |

---

## 2. Belgede olmayan ama ölçülen davranışlar

1. **Sıfır kenar süresi düzeltilir, dalga biçimi değişir.** `PULSE(0 1 0 0 0 Ton T)` için LTspice
   `Limiting rise time to Ton/10` yazar (1u → 1e-07, 50n → 5e-09, 1m → 1e-04). `PULSE(0 1 0 0 0 50n 100n)` ile
   AVG V(in) = **0,550**, açık `1n` kenarla 0,510. Bu bir hata değil, rc=0. Ancak dalga biçimi değişiyor,
   bu yüzden adaptör artık uyarı veriyor.
2. **LTspice'ın çıkış kodu güvenilir değil.** Sözdizimi ve topoloji hatalarında rc=1 dönüyor. `convreport` ile
   koşan ve `Simulation Failed: Iteration limit reached` yazan bir koşumda ise **rc=0** döndü. Başarı yalnızca
   log metninden belirlenmeli.
3. **Hata metinleri.** `Convergence Failure:  Time step too small; time = 2e-13, timestep = 0` (aradaki
   boşluğa dikkat: bizim anahtar kelime bitişik "timestep"ti) ve ardından çok satırlı
   `convergence trouble with nodes :` / `n` geliyor. Diğerleri: `trouble with instance "B1"`,
   `ERROR: Node n is floating and connected to current source B1`, `Undefined model "x"`,
   `This sub-circuit name is not defined.`, `File not found.`. Derleyici mesajlarının altında sorunlu satır ve
   `^^^^` işareti bulunur. Parser artık hataya `| satır: …` olarak ekliyor.
4. **Ölümcül olmayan derleyici bildirimleri:** `Limiting rise/fall time`, `Removed duplicate values`,
   `Limiting TrapDamp` (LTspice.exe mesaj kataloğundan). Bunlar `file(n): mesaj` biçiminde yazıldığı için
   hata sanılıyordu.
5. **Standart ADI parçaları birleşik kütüphanelerde.** `LT1001` → `lib/sub/LTC.lib`. Anahtarlamalı
   regülatörler şifreli `.sub` dosyalarında (yalnızca dosya adından bulunur). `lib/cmp/standard.mos` BOM'suz
   UTF-16LE. Kütüphane indeksi 5000 dosya / 61,6 MB / 8956 ad için 1,1-1,8 sn sürüyor, yalnızca hata yolunda
   kuruluyor.
6. **Sayı yazımı (2026-10-02).** Belge "Embedded decimal: `6K34` means `6.34K`" ve "Unrecognized letters after a
   number are ignored" diyor; ayrıntılar ölçüldü (R = 1 V / I(R1)). Baştaki rakam isteğe bağlı: `.5k` = 500,
   `-.5k` = −500, `1.k` = 1000, `5.` = 5. Gömülü ondalık çarpanlarla ve `R` ile çalışıyor: `4k7` = 4700,
   `2Meg2` = 2,2 M, `1M5` = 1,5 m, `1u5` = 1,5 µ, `4R7` = 4,7. Kesir sayıya **eklenir**, işaretten bağımsız:
   `1.5k3` = (1,5 + 0,3)k = 1,8k, `-4k7` = (−4 + 0,7)k = **−3,3k** (−4,7k değil), `-1.5k3` = −1,2k.
   Bilinmeyen harften sonraki rakam yok sayılıyor: `4V7` = `4x7` = 4. Hata veren yazımlar: `4k7ohm`, `1..2`,
   `12 3`, `1.5.3` → `Unknown parameter`; `k`, `.` → `Undefined model`. `1a` = 1 (atto yok), `10Fa` = 10 f.
   `.47µ` (cp1252 `.asc`) = 0,47 µF (τ = 0,470 ms, R = 1k). `parse_spice_value` 2026-10-02'ye kadar bu yazımların
   hepsinde ValueError veriyordu; şimdi 26 yazımın 26'sı LTspice ile aynı, hata veren 6 yazımı da reddediyor.
7. **İlk `.end`'den sonrası okunmuyor (belge: "All lines after .end are ignored").** İki `.end`'li netlistte
   ikincinin önündeki `.meas` üretilmedi. Aynı devrede `.options threads=2` ilk `.end`'den sonra → logda
   `Maximum thread count: 12`, önce → `2`. `insert_before_end` satırları son `.end`'in önüne yazıyordu, yani
   iki `.end`'li netlistte kilitlenme korumasının sınırı LTspice'a ulaşmıyordu. Düzeltildi; direktif algılama da
   artık `ltspice_visible()` ile yalnızca okunan kısma bakıyor.
8. **İlk satır her zaman başlık (SPICE-SYNTAX-REFERENCE "Netlist Structure").** Başlıksız netlistte ilk eleman
   kayboluyor (V1 ilk satırdayken I(R2) = 0). Başlığı `.end` olan netlistte adaptör satırlarını başlığın önüne
   yazıyordu: sınır satırı başlık oldu, LTspice hiçbir analiz koşmadı. Artık `.end` aranırken ilk satır atlanıyor.
9. **Bobinin varsayılan seri direnci 1 mΩ (CIRCUIT-ELEMENTS-REFERENCE: "1m (for SMPS; set Rser=0 to disable)").**
   Belgede var ama etkisi kolay gözden kaçıyor (2026-10-05). İdeal seri RLC tepesiyle karşılaştırmada fark %0.1-0.4
   çıktı (R = 0'lı 12 V devre 24 V yerine 23.96 V). `Rser=0` ile fark %0.001'in altına indi (`examples/15`).
   `parasitics.py`'nin iz ve kablo alt devreleri de bobinlere `Rser=0` yazmıyordu: 50 mm izin R_dc'si LTspice'ta
   ~%2 büyüktü. Ayrıca değerler 3 haneye yuvarlanıyordu (48.48 mΩ → `48.5mR`). Artık `Rser=0` ve 6 hane yazılıyor.
10. **Logun satır sonları karışık: başlık LF, `.meas` satırları CRLF.** `read_text` ile okunup Windows'ta metin
    modunda yazılan kayıtta (`write_text`, `newline=""` olmadan) CRLF, CRCRLF'ye dönüşüyordu. Araya giren boş satır
    `.step` ölçüm tablosunu ilk satırda kesiyordu. `tests/data`'daki 7 log böyleydi; `ref26_step_tran_db`'nin adım
    tablosu logdan hiç okunmuyordu. Hepsi `\r\n` → `\n` ile geri çevrildi; bu, metin modunun tam tersidir, çünkü
    dosyalarda hiç çıplak LF yoktu. Aynı yolla yazılmış 13 log daha baştan sona CRLF'ydi. Yalnız LF içeren bir log,
    metin modunda tümüyle CRLF olur ve iz bırakmaz; aynı netlistlerle yeniden kayıtta (`ref26_syntax_bad_line`,
    `ref26_four_thd`) LTspice baştan sona LF yazdı, bu yüzden bunlar da LF'ye geri çevrildi. Kayıt artık
    `newline=""` ile yazılıyor. `test_log_fixtures_keep_ltspice_line_endings` iki şeyi denetliyor: hiçbir logda CRCRLF
    yok, ilk satır her zaman LF.
11. **Her adımda başarısız bir ölçüm için tablo yazılmaz.** LTspice 26.1.1 yalnızca tek satır
    `Measurement "x" FAIL'ed` yazıyor. `simulate_monte_carlo` bu spec'i verim sonucundan sessizce düşürüyordu; artık
    %0 verim ve bir not dönüyor.
12. **Kendini çağıran tanım çöküyor (2026-10-06, `examples/16` T1).** LTspice.exe `Infinite subckt recursion.` ve
    `Infinite recursion encountered in "{:s}"` dizgilerini içeriyor, ama 26.1.1 kendini çağıran `.subckt`'ta,
    A → B → A döngüsünde ve kendini çağıran `.func`'ta hata yazmıyor, **0xC00000FD (yığın taşması) ile çöküyor**:
    stderr boş, log yalnızca 3 başlık satırı (`tests/data/ref26_crash_recursive.*`). Windows'un varsayılan hata
    kipinde WerFault çalışıyor ve dönüş 3,4-4,5 sn sürüyor. Adaptör artık hata önem dereceli her NTSTATUS'u çökme
    olarak yazıyor; eskiden "dalga formu üretilemedi, sözdizimini kontrol edin" diyordu.
13. **Sıfıra bölme çökertmiyor.** `.param b={int(1)/int(a)}` (a = 0) → `Division by zero.`, rc=1. B kaynağında
    `V={1/V(in)}` (V(in) = 0) koşum başarılı, hata yok. Gemini'nin 0xC0000094 örneği LTspice'ta üretilemedi.
14. **Netlister alt devre örneğine `X§` ekliyor.** InstName `X` ile başlasa da (`XU_R0`) satır `X§XU_R0 ...`
    oluyor. Satır sonundaki `;§pnba SYNC)TR/SS)...` yorumu pin adlarını netlist sırasıyla veriyor; LT8610'da
    `PINATTR SpiceOrder` ile aynı.
15. **Sembol kütüphanesi (`lib/sym`).** 6642 `.asy`: 6516 cp1252, 126 BOM'suz UTF-16LE (`.asc` ile aynı kurallar).
    Her pinde `PINATTR SpiceOrder` var. 7 sembolde (ör. `OpAmps/ADA4254`, `ADC/AD4880`) bazı pinlerin `PinName`'i
    yok. Farklı klasörlerde aynı adlı sembol yok. SpiceOrder dosya sırasından farklı olabilir (LT8610: PGND 13,
    FB 6). Adda nokta olabilir (`ADP7182-1.2`). ADI örnekleri sembole klasörlü (`.asc`'de `PowerProducts\\LT8610A`, büyük/
    küçük harf karışık) ya da yalın (`LT8610`) adla başvuruyor. Hepsini ayrıştırmak 1,1-2,7 sn, dosya adlarını
    listelemek 0,07-0,11 sn.
16. **Topoloji hatalarının hangisini LTspice söylüyor (`examples/16` T6).** Paralel gerilim kaynağı: hata
    (`... are paralleled making an over-defined circuit matrix.`). DC yolu olmayan düğüm: uyarı
    (`WARNING: Node a is floating.`), koşum başarılı. **Tek bağlantılı düğüm** (açık uçlu direnç, boşta LT1001
    `In+`): hiçbir şey yazmıyor, koşum başarılı. Şemada `inspect_schematic` bunu `NC_xx` / açık uçlu ağ olarak
    buluyor. Netlistte artık `run_netlist` uyarıyor (`netlist.single_connection_nodes`, `examples/17` T4): ADI örnek
    netlistlerinde LTspice'ın `NC_xx` dediği pinlerin hepsi yakalandı, işaretlerin hepsi gerçekten tek pinliydi; ama
    çoğu bilerek boş bırakılmış (`NC_`/`MP_` pinleri, yüksüz trafo sekonderi, E çıkışı). Bu yüzden hata değil uyarı.
17. **Ayar dosyası ve `-ini` (2026-10-06, `examples/17` T3).** `%APPDATA%\LTspice.ini` BOM'lu UTF-16LE (cp1252 diye
    okununca hiçbir anahtar eşleşmiyor). Kullanıcı klasörü `UserCmpDir`; LTspice varsayılanı da yazıyor. Bu makinede
    Belgeler OneDrive'a yönlendirilmiş: `~/Documents/LTspice` yanlış klasör olurdu. `LTspice.exe -ini <yol>` 26.1.1'de
    çalışıyor (belgelenen `-I<yol>` çalışmıyordu): gerçek ayar dosyasına dokunmadan ayar denemek için geçici kopya
    verilir; LTspice kopyaya yazar, gerçek dosyanın özeti değişmedi.
18. **Alt klasörler yalnızca göreli yolun kökü.** ADI SCHEMATIC-REFERENCE "Default Search Priority" şema klasörü için
    "(and subfolders)" diyor. Ölçümde (`-netlist`) şemanın alt klasöründeki sembol yalın adla bulunmuyor, göreli yolla
    (`local\\X`) bulunuyor. User Files'ta da aynı: klasörün kendisindeki sembol yalın adla, `sub/`, `sym/`, `lib/sym/`
    altındaki yalnızca göreli yolla. ADI kütüphanesi (`lib/sym`) ise alt klasörlerde yalın adla aranıyor (`LT8610`).
    `find_symbol` kullanıcı klasöründe bunu izliyor; verilen `search_dirs`'te yalın adı alt klasörlerde de arıyor
    (LTspice'tan geniş, açık konu).
19. **Steplenmiş `.raw`'ın adımları `.log`'da (2026-10-06, `examples/18` T1, spicelib 1.6.3).** `.raw` başlığında
    yalnızca `Flags: ... stepped` var; adım sayısını ve parametreleri spicelib yanındaki aynı adlı `.log`'un `.step`
    satırlarından alıyor. `.log` yoksa ya da okunamıyorsa adım sayısını eksenden saymaya çalışıyor, ikili dosyada
    eksen henüz okunmadığı için 0 buluyor ve ilk iz okunurken `IndexError` veriyor (tembel açılış geçiyor).
    `.log` okunabilir ama `.step` satırı yoksa (steplenmemiş bir koşumun logu) hata vermeden bütün adımları tek adım
    sayıyor: iki adımlı bölücüde V(out) = 0, 0,5, 1, 0, 0,25, 0,5 tek dizi. `.log`'da veriden fazla adım varsa spicelib
    `Expecting 5 got 2` veriyor; azsa yine `IndexError`. Steplenmiş ASCII `.raw` `.log` olsa da olmasa da okunmuyor
    (`.tran` ve `.dc`). İkili `.raw`'ın başlığı UTF-16LE, ASCII `.raw`'ınki 8 bit (`tests/data/ref26_step_dc_*`).
    Tek değerli `.step` LTspice'ta hata olduğu için (`ref26_step_single`) steplenmiş `.raw` en az iki adımdır;
    `open_raw` daha azını nedeniyle reddediyor.
20. **Tstart > 0'da `.raw`'ın zaman ekseni 0'dan başlıyor (2026-10-06, `examples/18` T3).** `.tran 0 3m 1m` kaydında
    eksen 0-2 ms, başlıkta `Offset: 1.0e-03`. LTspice'ın kendi `.meas` zamanları mutlak (`WHEN` 1,052154 ms;
    `AVG ... FROM 0.001 TO 0.003`): gerçek zaman eksen + Offset. İzden bulunan geçişler `.meas`'ten 1e-15 s içinde,
    RC elle hesabına 8 µV; Offset'siz okununca Tstart kadar kayık. ASCII ve steplenmiş `.raw`'da da aynı (tek Offset,
    her adım 0'dan). `.ac`, `.dc`, `.op`, `.noise`, `.tf` ve Tstart = 0'da Offset 0. ADI belgesi `Offset` alanını
    anlatmıyor, spicelib 1.6.3 uygulamıyor; `raw_axis_values` artık ekliyor (grafik ve `read_trace` doğru).
21. **İlk açılış onay kutusu başsız koşumu kilitliyor (2026-10-03, proje ikinci makineye taşınırken).**
    - Yeni bir Windows profilinde LTspice 26.1.1 her açılışta modal "Anonymously Share LTspice Usage Data" kutusunu
      gösteriyor ("Yes, send..." / "No, do not send my usage data" + OK).
    - Kutu cevaplanana kadar `LTspice.exe -b` ve `-version` hata vermeden asılı kalıyor: log yazılmıyor,
      `ltspice_version()` None dönüyor, koşumlar zaman aşımına düşüyor.
    - Kutu yalnızca sürecin pencere listesinde görünüyor (Popen + EnumWindows, pencere sınıfı `#32770`).
    - Cevap `%APPDATA%\LTspice.ini`'ye `[Options] CaptureAnalytics=false|true` olarak yazılıyor (dosya BOM'lu
      UTF-16LE, §2.17). Cevaptan sonra `-version` 0,23 sn'de `26.1.1` döndü.
    - Veri paylaşımı kullanıcının kararıdır: ajan kutuyu kendisi cevaplamamalı, kullanıcıya sormalı.
22. **`.options threads` makinenin mantıksal çekirdek sayısına kırpılıyor (2026-10-11, `examples/12` S2).**
    - i7-7500U'da (4 mantıksal çekirdek) netlist `.options threads=6` verince log `Maximum thread count: 4` yazıyor.
      Bu, LTspice'ın varsayılanı (bütün mantıksal çekirdekler).
    - LTspice affinity'yi bilmiyor: süreç tek çekirdeğe bağlıyken (`start /affinity 1`) de varsayılan 4.
    - ADI referansında `threads` hiç geçmiyor.
    - Adaptör netlistin kendi değerine dokunmuyor. `SimulationResult.solver_threads` onu `min(N, mantıksal çekirdek)`
      olarak bildiriyor (`cpu_guard.ltspice_thread_limit`). `threads={N}` gibi bir ifadeyi çözmüyor ve None diyor.
23. **`.include` yolundaki ortam değişkeni açılıyor (2026-10-28, `examples/12` S3).**
    - ADI SCHEMATIC-REFERENCE "Symbol and Library Search Paths" ortam değişkenini yalnızca Settings > Search Paths
      girdileri için yazıyor; netlistteki `.include` için belge yok.
    - Ölçüm: çalışma alanının dışındaki klasörde `.param probe=4.2` içeren dosya (`.include`), 4.2 V'luk alt devre
      (`.lib`) ve sabit 4.2 V'luk PWL verisi; klasör bir ortam değişkeninde. Her yapının mutlak yollu kontrol koşumu
      dosyayı okudu. `%VAR%` açıldı: `.include %VAR%\probe.lib`, tırnaklı hâli, küçük harfli ad (`%var%`), parantezli
      ad (`%VAR(X)%`) ve `.lib %VAR%\probe.sub` (`.meas` V(a) = 4.2 V). `PWL file=%VAR%`, `$VAR` ve `${VAR}` açılmadı.
    - `.tran` dalga formu tek duyarlıkta: aynı sabit 4.2 V `.meas tran ... AT` ile 4.19999980927, `.meas op` ile tam
      4.2 çıktı. Karşılaştırma toleransı 1e-9 olunca okunan dosya okunmamış görünüyordu.
    - Dil modelinin yazdığı netlist böylece `%USERPROFILE%` ya da `%APPDATA%` altındaki bir dosyayı okutabiliyordu.
      `file_access_problems` artık `%VAR%` içeren her okuma yolunu reddediyor: değişkenin değeri denetim anında bilinmez.

---

## 2a. Yapay zekâyla üretilen şemalara uygulama (2026-10-01)

Yeni denetimler yapay zekâyla üretilmiş dört şemada sorun buldu. Birinde iki kondansatörün `.1µ` değeri **UTF-8**
yazılmıştı; LTspice bunu `.1Âµ` = 0,1 F okudu ve filtrenin ölçümleri tutmadı (µ baytı cp1252 yapılınca düzeldi).
Üçünde pinler bağlı değildi (`NC_xx`): bir direncin iki pini, genlik sınırlayıcı diyot çiftinden biri ve bir
regülatörün açık-drenaj durum çıkışı.

Bu bulgular ADI'nin uyarısını doğruluyor: konum hesabıyla üretilen şemalar simüle edilip ölçüm üretse bile
kopuk olabilir. Şema değiştikten sonra `inspect_schematic` çalıştırılmalı.

---

## 3. Koda yansıyanlar

| Modül | Değişiklik |
| --- | --- |
| `parser.py` | `Time step too small` / `Convergence Failure` / `Simulation Failed` / `More than one analysis` hata sayılıyor. Ölümcül olmayan derleyici bildirimleri uyarı oldu. Hataya kaynak satır ekleniyor. WHEN eşiği ifade ya da ad olabiliyor. bode/polar/cartesian çözümü eklendi. `.four` → `fourier`, `convreport` → `convergence`. Puan satırları sahte ölçüm üretmiyor. `read_measure_db()` eklendi |
| `diagnostics.py` | Yeni türler: `MULTIPLE_ANALYSES`, `SWEEP_DIRECTIVE_ERROR`, `MISSING_INCLUDE_FILE`; `FLOATING_NODE` artık gerçekten algılanıyor. Önerilerde ADI sırası izleniyor (time step: gear → itl4=50 → cshunt → abstol → gshunt → reltol 0.005; DC OP: gshunt → gmin → abstol → reltol → solver=alt; 2026-10-11'de ADI tablosunun kendi sırasına çevrildi: solver=alt → abstol → reltol → gmin → gshunt). `uic` artık önerilmiyor. Gear uyarısı eklendi. Otomatik yama yalnızca muhafazakâr seçenekleri uyguluyor (eskiden 5 seçeneğin hepsi, `reltol=0.01` dahil, aynı anda ekleniyordu). Eksik model için `.lib` önerisi ve yaması geliyor. `UNKNOWN` için kör `gear` yaması kaldırıldı |
| `schematic.py` | Pin tablosu `.asy` dosyalarına göre düzeltildi (current/bi/csw/sw/e/g/opamp2). `.asc` cp1252/UTF-16LE okunup yazılıyor (`decode_asc_bytes`, `encode_asc_text`), UTF-8 asla yazılmıyor |
| `adapter.py` | `.asc` okuma/yazma kodlamayı koruyor. Bayat `.db` siliniyor, `db_path` eklendi. `run_sweep` iki farklı değerden azını LTspice'a gitmeden reddediyor ve `.asc`'yi `-netlist` ile çeviriyor. Yeni metotlar: `schematic_to_netlist()`, `inspect_schematic()` (NC_xx / açık uçlu ağ) ve `ltspice_version()` |
| `reference.py` (yeni) | `ReferenceDocs` (başlık temelli arama), `find_model_definitions()` (kütüphane indeksi), `ltspice_data_dir()` |
| `tools.py` | Yeni araç: `search_ltspice_reference` (LTspice çalıştırmaz; isteğe bağlı `model_name` ile kütüphane araması yapar) |
| `symbols.py` (yeni, 2026-10-06) | `.asy` okuyucu (`read_symbol`, `find_symbol`, `load_symbol`). `AscSchematic.get_pin` tabloda olmayan sembolde pini `PinName` ya da SpiceOrder numarasıyla `.asy`'den alıyor; arama sırası: verilen dizinler, sonra ADI kütüphanesi |
| `adapter.py` (2026-10-06) | Çökme (NTSTATUS / sinyal) stderr boş olsa da hata; LTspice süreci Windows iş nesnesinde (Python'la birlikte ölür, WerFault'u beklemez) |
| `system_prompts.py` | "LTspice kritik kuralları" bölümü eklendi. `.model 1N4148 D` örneği düzeltildi (`.lib standard.dio`) |
| `netlist.py` (2026-10-28) | `file_access_problems` ortam değişkeni (`%VAR%`) içeren okuma yolunu reddediyor (§2.23) |

---

## 4. Kurallar (ajanlar için)

- **LTspice sözdizimini ezberden yazma.** Emin değilsen `search_ltspice_reference` aracıyla yerel ADI
  belgesine bak. Belge ile eğitim verin çelişirse belgeye güven. Belge ile ölçüm çelişirse **ölçüme** güven
  ve bu belgeye kaydet (`-I`, `uic` örnekleri).
- **`.asc` dosyasını UTF-8 metin araçlarıyla yazma ya da düzenleme.** `encode_asc_text` / `write_asc_file`
  (cp1252, gerekirse UTF-16LE) ya da `AscSchematic.save` kullan. Yalnızca SYMATTR değerini veya TEXT direktifini
  değiştirmek güvenlidir. Konum ya da tel değişikliğinden sonra `inspect_schematic` ile `NC_xx` ve açık uçlu ağ
  olmadığını doğrula.
- **Başarıyı LTspice'ın çıkış kodundan değil logdan belirle.** `Simulation Failed` geçen koşum başarısızdır.
- **Elle yazılan netlistte standart modeller yüklenmez.** `.lib standard.dio` / `.bjt` / `.mos`, ADI parçaları
  için `find_model_definitions("LT1001")` → `.lib LTC.lib`. `.model 1N4148 D` gerçek parça değildir
  (0,629 V; gerçek model 0,548 V).
- **Yakınsama seçeneklerini tek tek ve ADI sırasıyla dene.** Sorunlu cihazı `convreport` ile bul. Gear sonucu
  kararlılık kanıtı değildir.
- **Göreli eşikli WHEN kullan** (`=g0/sqrt(2)`). AC sonuçlarının birimi `meascplxfmt`'e bağlıdır: bode'da
  `mag()` dB, polar ve cartesian'da doğrusal. Kesin değer gerekiyorsa `read_measure_db(result.db_path)` kullan.
- **Tabloda olmayan bir sembolün pin koordinatını tahmin etme.** `AscSchematic.get_pin` `.asy`'den okur
  (`get_pin(u1, "OUT")` ya da SpiceOrder ile `"5"`); yerel semboller için `AscSchematic(symbol_dirs=[...])`. Sonra
  yine `inspect_schematic`.
- **Çıkış kodu 0xC0000000 ve üstüyse LTspice çökmüştür** (stderr boş, log yalnızca başlık). 0xC00000FD: kendini
  çağıran `.subckt` ya da `.func`.
- **LTspice güncellemesinden sonra** `python examples/12_ltspice_reference_checks.py` çalıştır. Hata
  metinleri ve davranışlar sürüme bağlıdır.
- **`-b` ya da `-version` hata vermeden asılı kalıyorsa önce açık bir iletişim kutusu ara.** Bu durum yeni bir makinede
  ya da Windows profilinde görülür (§2.21). Veri paylaşımı onayını kullanıcıya sor.

---

## 5. Yeniden üretme

- `python examples/12_ltspice_reference_checks.py`: 21 kontrol, ~30 sn; rapor
  `reports/12_ltspice_reference_checks.html`.
- `tests/test_quantizer.py::test_parse_spice_value_matches_ltspice` ve `tests/test_properties.py`: §2.6'daki
  ölçümler beklenen değer olarak, LTspice gerektirmez.
- `tests/test_ltspice_reference.py`: gerçek LTspice 26.1.1 logları ve `.db` dosyaları (`tests/data/ref26_*`),
  LTspice gerektirmez.
- `tests/test_integration_reference.py`: aynı davranışların gerçek LTspice ile uçtan uca testleri.
