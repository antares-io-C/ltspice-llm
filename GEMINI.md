# Proje Kuralları

Bu dosyayı Claude Code da okur (`CLAUDE.md` içe aktarır). Kuralları ajandan bağımsız yaz; yalnızca Antigravity'ye
özgü olanlar "Terminal komutları" bölümünde durur.

## Terminal komutları (izin sormadan çalışması için)

Bu projede `command(python)`, `command(pytest)`, `command(git status)`, `command(git diff)`,
`command(git log)`, `command(dir)` ve `command(pip list)` önceden onaylıdır. Onay, komutun
başındaki program ve alt komutla (önek) eşleşir. Bazı kabuk yapıları bu eşleşmeyi bozar ve
her komut için yeniden onay istenir. Bu yüzden:

- Komutları **doğrudan** çalıştır. `cmd /c "..."`, `powershell -Command "..."` gibi sarmalayıcılar kullanma.
  - Doğru: `python -m pytest tests -v`
  - Yanlış: `cmd /c "python -m pytest tests -v"`
- **Çok satırlı `python -c "..."` kullanma.** Birden fazla satırlık denemeleri proje içinde
  `.scratch/` klasörüne bir `.py` dosyası olarak yaz ve `python .scratch/<ad>.py` ile çalıştır.
  İş bitince dosyayı sil.
- Komutları `&&`, `;`, `|` ile zincirleme. Her komutu ayrı çalıştır.
- Dosya listelemek, okumak ve aramak için terminal yerine dosya araçlarını kullan.

## Performans ve eşzamanlılık kuralları

Ayrıntı ve ölçümler: `docs/LESSONS_LEARNED_PERFORMANCE.md`. Önce onu oku.

- **Ölçmeden performans iddiası yazma.** Her öneri için ölçüm betiği yaz; önceki/sonraki süreyi ve sonucun
  aynı kaldığını birlikte raporla. Önceki incelemedeki `method=gear` (2-3x), `plotwinsize=0` ve
  `reltol/trtol` (2x) iddiaları ölçümde ~1,0-1,1x çıktı; "8 paralel simülasyonla 4-12x" makineyi kilitledi.
- **LTspice'ı aynı anda birden fazla kez çalıştırmak için kilitlenme korumasını kapatma**
  (`LTSpiceAdapter(cpu_guard=True)`, varsayılan). LTspice çözücüsü tüm çekirdekler kadar iş parçacığı açar ve
  birden fazla örnekte livelock'a girer. Aynı anda çalışan örneklerin `.options threads` toplamı çekirdek
  sayısını aşmamalı.
- Çözücü iş parçacığı seçeneğinin adı `.options threads=N`. `numthreads`/`nthreads` sözdizimi hatasıdır;
  `OMP_NUM_THREADS` etkisizdir.
- Kendi `subprocess` / `ProcessPoolExecutor` ile LTspice başlatma; `run_netlist`, `run_sweep`, `run_batch`
  kullan. Parametre taraması için `run_sweep` (tek süreçte `.step`), bağımsız devreler için `run_batch`. Adaptör
  LTspice'ı Windows iş nesnesine bağlar: Python ölünce LTspice de ölür. Kendi başlattığın LTspice yetim kalır ve
  çekirdek bütçesini atlatır (ölçüm: 10 sn'de 19,7 CPU-sn, bütçe 12/12 boş göründü).
- `.raw`'dan birden çok iz okuyacaksan `open_raw(path, traces=ALL_TRACES)` kullan: spicelib tembel okumada her iz
  için bütün dosyayı okuyup tutuyor (67 MB `.raw`, 13 sinyal: 907 MB yerine 99 MB). Uzun izi seyreltirken LTTB değil
  kova başına min/max (`report._decimate`); LTTB tepe ve dipleri kaybediyor.
- Çökme ya da WER ölçerken alt süreci `CREATE_DEFAULT_ERROR_MODE` ile başlat. Claude Code hata kipi 3'ü miras
  bırakıyor; bu kipte WerFault hiç çalışmıyor ve ölçüm sorunu göstermiyor.
- Varsayılan `.raw` biçimi ikilidir; ASCII'ye dönme (3-4x yavaş, 6x büyük).
- Paralellik kazancı bu makinede donanımla sınırlı (i7-1255U hibrit, 15 W): en iyi ~2,4x, tavan ~3,3x.
- Bir seçeneğin uygulandığını logdan doğrula (`Maximum thread count: N`, `method = Gear`, `syntax error` yok).
  Zaman aşımına uğrayan koşumun logunu oku: hata değil livelock olabilir.

## Simülasyon doğrulama kuralları

Bu kurallar, ölçüldüğünde çoğu yanlış çıkan bir donanım incelemesinden (2026-10-01) çıktı.

- **Raporlanan her sayı `.meas`'ten ya da `.raw`'dan gelmeli.** Grafik lejantına, tabloya ya da belgeye
  simülasyondan önce sayı yazma. Ölçmediğin değeri (akım, kuplaj, empedans) ölçülmüş gibi yazma.
- **Salınım görürsen `.options method=gear` ile tekrar koş** ve logda `method = Gear` satırını gör. Her zaman
  adımında işaret değiştiren salınım trapez integrasyon artefaktıdır, fiziksel değildir.
- Hiçbir düğümü yalnızca endüktans ve ideal akım kaynağıyla sürme. Hazır modülleri (geliştirme kartı, ekran,
  GPS modülü) kendi regülatör ve kondansatörleriyle modelle; yükleri datasheet'teki tepe değerlerden al.
- Davranışsal regülatör (`B` kaynağı) girişten akım çekmeli (çıkış akımı + Iq); dropout'u ve akım sınırı
  olmalı. Önce DC çalışma noktasının beklenen gerilimde olduğunu kontrol et.
- Basılan bir kartı modellerken bağlantıları ve iz uzunluklarını kart dosyasından oku, dosyanın özetini rapora yaz:
  fotoğraftan okunan bir kart basılan karttan farklı çıktı. Modülün takılma yönünü gerçek modülle, yazılımın pin
  haritasını kartla karşılaştır: bir kart modülün pin 1'ini yanlış uca koymuştu, yonga seçme (CS) pini kartta ve
  yazılımda farklıydı.
- Önce/sonra karşılaştırmasında aynı düğümü ölç ve bir seferde tek değişken değiştir.
- Son değerin simülasyon penceresi içinde oturduğunu kontrol et.
- `.tran` izinin ortalamasını ya da RMS'ini örneklerin aritmetik ortalamasıyla alma: LTspice'ın adımı değişken
  (oran 1e5), 10 ns kenarlı darbede ortalama %16,8 fazla çıktı. `.meas AVG`/`RMS` ya da
  `parser.time_weighted_mean_rms` kullan (2026-10-06, `examples/17` T2).
- Her devreye ayrı `circuit_name` ver. Belgeye ya da rapora giren sayıları üreten betikleri `.scratch/`'ta
  bırakıp silme; `examples/` altına kalıcı betik olarak koy.
- Simülasyon sonucunu "sahada doğrulandı" ya da "ölçüldü" diye yazma. Donanımda ölçülmediyse "simülasyon" de
  ve varsayımları listele.
- Datasheet ve uygulama notu iddialarını kaynaktan doğrula. Örnek: u-blox NEO-M8 kılavuzu VCC hattına bobin
  ya da direnç konmamasını söylüyor; eski belgedeki ferrite bead önerisi bununla çelişiyordu.
- App note, datasheet ve el kitaplarının yerel kopyaları `docs/references/`'ta (katalog: `README.md`; indirme:
  `python docs/references/fetch_references.py`). Belge numarasını ezberden yazma: `docs/references/text/` altında
  ara ve sayfa numarasıyla atıf yap (`=== s.N ===`). Bir ders belgesindeki beş atıftan üçü başka konudaki bir belgeyi
  gösteriyordu (2026-10-02).

## Simülasyon raporu (her süreçte zorunlu)

Her simülasyon süreci (doğrulama, tasarım, inceleme, yeniden koşum) kullanıcıya bir raporla biter.
`ltspice_llm.report.SimulationReport` kullan; örnek: `examples/15_hotplug_damping_validation.py`.

- Rapor şunları içerir:
  - Her testin sorusu (neden yapıldı) ve yöntemi (nasıl modellendi, hangi tek değişken, hangi düğüm).
  - Dalga formları ve ölçüm tablosu.
  - Bileşen etkileri: bileşen → nerede → hangi ölçüm → önce → sonra.
  - Yeni etkiler ve nasıl ele alındıkları.
  - Güvenilirlik: başarı logdan, `Maximum thread count`, `method`, uyarılar, Gear farkı, DC çalışma noktası,
    pencerede oturma.
  - Varsayımlar ve donanımda ölçülecekler.
- Rapordaki her sayı `.meas`'ten ya da `.raw`'dan gelir. Metne sayı yazacaksan ölçümden biçimlendir
  (`format_value`); elle yazılan tek sayı varsayımlardır (girdi).
- Etkiyi yalnızca tek değişkenle ayrışan iki varyanttan hesapla. Bilerek birden çok şey değişiyorsa (köşe durumu)
  bunu etkinin notunda söyle.
- Raporu üreten betik `examples/` altında kalır. Rapor `reports/` altına yazılır; git'e girmez, betik yeniden
  üretir. Kullanıcıya dosya yolunu ver.
- Raporu bir kez görsel olarak kontrol et (ekran görüntüsü): etiket çakışması, boş değer (`—`), taşma.
- Rapor tuzakları (2026-10-06):
  - Tekrarlanan koşumlara ayrı `circuit_name` ver: aynı ad önceki sonucun `.raw`'ını geçersiz kılar, `read_trace`
    hata verir.
  - Bilerek başarısız devreyi `add_runs`'a koyma: otomatik denetim "başarılı koşum" satırını hata gösterir.
  - Tablo hücresine `None` koyma, sayımı `decimals=0` sütununa koy.
  - Etki tablosunda çok uzun, bölünmeyen adlar yatay taşma yapar.

## LTspice referans kuralları

Ayrıntı ve ölçümler: `docs/LESSONS_LEARNED_LTSPICE_REFERENCE.md`. ADI'nin resmî LTspice AI Reference belgeleri
`%LOCALAPPDATA%\LTspice\reference\` altında; `ltspice_llm.ReferenceDocs` / `search_ltspice_reference` ile aranır.

- LTspice sözdizimini ezberden yazma, önce yerel ADI belgesine bak. Belge ile ölçüm çelişirse ölçüme güven ve
  ders belgesine kaydet (ör. `-I<yol>` anahtarı LTspice 26.1.1'de çalışmıyor).
- **`.asc` dosyasını UTF-8 ile yazma ya da düzenleme.** LTspice `.asc`'yi cp1252 (veya UTF-16LE) okur; UTF-8
  `1µ` değeri `1Âµ` = 1 F olur. `AscSchematic.save` / `write_asc_file` kullan. Konum değişikliğinden sonra
  `adapter.inspect_schematic()` ile `NC_xx` (bağlantısız pin) kalmadığını doğrula.
- Başarıyı LTspice çıkış kodundan değil logdan belirle: `Simulation Failed` olan koşum rc=0 dönebiliyor. Tek istisna
  çökme: çıkış kodu 0xC0000000 ve üstüyse LTspice çökmüştür (stderr boş, log yalnızca başlık). 0xC00000FD kendini
  çağıran bir `.subckt` ya da `.func` demektir.
- Tabloda olmayan sembolün pin koordinatını tahmin etme: `AscSchematic.get_pin` `.asy`'den okur (`PinName` ya da
  SpiceOrder numarası). Sonra yine `inspect_schematic`.
- LTspice'ın ayar dosyası `%APPDATA%\LTspice.ini` BOM'lu UTF-16LE. Kullanıcı klasörü oradaki `UserCmpDir`
  (`ltspice_user_dir`); Belgeler yolunu tahmin etme (bu makinede OneDrive). Bir ayarı denemek için gerçek dosyayı
  değiştirme: `LTspice.exe -ini <kopya>` çalışıyor (`-I<yol>` çalışmıyor).
- LTspice `-b` ya da `-version` hata vermeden asılı kalıyorsa (yeni makine ya da Windows profili) önce açık iletişim
  kutusuna bak. İlk açılıştaki "Anonymously Share LTspice Usage Data" kutusu cevaplanana kadar her koşum bekler.
  Veri paylaşımı kararını kullanıcıya bırak (`docs/LESSONS_LEARNED_LTSPICE_REFERENCE.md` §2.21).
- Elle yazılan netlistte standart modeller yüklenmez: `.lib standard.dio` / `.bjt` / `.mos`. ADI parçası
  için `find_model_definitions("LT1001")` (-> `.lib LTC.lib`). `.model 1N4148 D` gerçek parça değildir.
- `1M` = 1 mili (mega `1Meg`). PULSE'ta Trise/Tfall'u açık yaz (0 verilirse LTspice Ton/10 kullanır).
  `.step`'te `param` zorunlu ve en az iki farklı değer olmalı. Netlistte tek analiz komutu olmalı.
- Steplenmiş `.raw`'ı yanındaki aynı adlı `.log` olmadan kopyalama ya da taşıma: spicelib adımları `.log`'un `.step`
  satırlarından okur. `.log` yoksa okuma hata verir; steplenmemiş bir koşumun `.log`'u ise adımları sessizce tek adımda
  birleştirirdi (`open_raw` artık reddediyor; `docs/LESSONS_LEARNED_LTSPICE_REFERENCE.md` §2.19).
- `.raw` eksenini spicelib'den (`get_axis`, `get_trace("time")`) değil `raw_axis_values` / `read_trace` ile oku.
  Tstart'ı sıfırdan büyük `.tran`'da LTspice ekseni 0'dan yazar, kaymayı başlığın `Offset` alanına koyar; spicelib
  uygulamaz, bu yardımcılar uygular (LTspice'ın `.meas` zamanları mutlak; §2.20).
- LTspice ilk `.end`'den sonrasını okumaz. Netliste satır eklerken ya da bir direktifin "var" olup olmadığına
  bakarken `insert_before_end` / `ltspice_visible` kullan: `.end` sonrasına düşen `.options threads` çözücüyü
  bütün çekirdeklerle çalıştırdı (ölçüm: 2 yerine 12).
- LTspice güncellemesinden sonra `python examples/12_ltspice_reference_checks.py` çalıştır.

## Test kuralları

Ajanların yazdığı testlerin bilinen zaafları (kodu yansıtan assert, aşırı mock, testi değiştirerek geçirme)
burada ölçüldü: %87 branch coverage'da 10 mutantın 6'sı testlerden sağ çıktı ve iki gerçek hata (E serisi
floor/ceil, `.1u`/`4k7` yazımı) yeşil testlerin altında duruyordu (2026-10-02).

- **Testi geçirmek için testi değiştirme.** Test silme, skip/xfail ekleme, assert ya da tolerans gevşetme yok.
  Test ya da görev yanlış görünüyorsa dur ve nedenini kaynağıyla kullanıcıya söyle; durmak kabul edilebilir bir
  sonuçtur. (Claude Code'da `guard_tests.py` hook'u bu tür düzenlemede kullanıcıya sorar.)
- **Beklenen değer koddan değil kaynaktan gelir:** IEC 60063, ADI referansı (belge + başlık), datasheet
  (`docs/references/text/`, sayfa), elle hesap ya da `examples/` altındaki bir betiğin LTspice ölçümü (sürümüyle).
  Kodu çalıştırıp çıktısını assert'e yapıştırma; standart tek cevap veriyorsa "a ya da b" kabul etme. Kaynağı
  testin yanına yaz.
- Hata düzeltmesinde önce hatayı yakalayan testi yaz ve **kırmızı olduğunu gör**; sonra düzelt.
- Saf fonksiyonlarda (değer ayrıştırma, E serisi, `.asc` kodlaması, netlist metni) Hypothesis property testi
  yaz: gidiş-dönüş, idempotence, sıralama/monotonluk, bağımsız oracle. Stratejiyi fonksiyonun iddia ettiği
  alandan dar tutma. Bulunan her karşı örnek parametrize edilmiş kalıcı vakaya dönüşür (`tests/test_properties.py`).
- Mock yalnızca gerçek sınırlarda: LTspice süreci (`subprocess.Popen`, sahte `.bat`), işletim sistemi süreç listesi
  ve çekirdek sayısı, saat, LLM API'si. Test edilen modülü ya da saf fonksiyonları mock'lama.
- LTspice davranışını taklit eden fixture gerçek bir LTspice logu olmalı (`tests/data/ref26_*`), elle yazılmış değil.
- **Use case akışı** (gdb tracer / gcov karşılığı; gdb ve gcov Python koduna bakamaz): `tests/usecases/*.json` her
  use case'in eylemlerini ve senaryolarını tutar; bağlı testin çağrı dizisini `tests/flowtrace.py` (`sys.setprofile`)
  pytest içinde denetler, `python scripts/flows.py` aynı testlerde coverage.py bağlamlarıyla ikinci tanık olur
  (`--uncovered`: hiçbir testin girmediği fonksiyonlar). Dizi ve eylemler belgeden (`source`) yazılır, tracer
  kaydından değil. Sınırda ya da yalnızca LTspice çıktısında görünen eylem (`boundary`/`ltspice`), testten birebir
  alıntılanan assert'le (`evidence`) kanıtlanır. Manifesti testi geçirmek için zayıflatma (eylem/senaryo silme,
  `strict` → `subsequence`, `forbidden`/`counts` kaldırma) testi zayıflatmakla aynıdır. Başarı yolu LTspice'sız
  sahte süreçle sınanırken o süreç gerçek LTspice kaydını yeniden oynatır (`tests/data/ref26_flow_rc.*`,
  `examples/14_record_flow_fixtures.py`; LTspice güncellenince yeniden kaydet).
- Test izolasyonu: `python scripts/isolation.py [dosya|düğüm ...]` her testi tek başına ayrı süreçte koşar, birlikte
  koşumla sonucu değişen testi (önceki testin durumuna dayanan) bildirir. Test ya da senaryo ekleyince çalıştır.
- Testin sonucu makinenin çekirdek sayısına ve işletim sistemine bağlı olmamalı. CI 2 çekirdekli Linux ve Windows'ta
  koşuyor ve ilk commit'ten 2026-10-11'e kadar hep kırmızıydı:
  - Koruma `solver_threads`'i `available_cpus()`'a kırpıyor.
  - `os.path` Linux'ta `posixpath` ve `%VAR%`'ı açmıyor.

  Çekirdek sayısını sahtele ve sahte makine tutarlı olsun. `adapter_mod.available_cpus`'u sabitleyen test
  `os.cpu_count`'u da sabitler, çünkü LTspice'ın sınırı `cpu_guard.ltspice_thread_limit` = `os.cpu_count()`. Affinity
  için `cpu_guard._affinity_count` sahtelenir.

  CI'ı yerelde taklit etmek için iki koşul birlikte gerekir: `start /b /wait /affinity 3` (2 çekirdek) ve
  `PYTHON_CPU_COUNT=2` (Python 3.13+). Affinity `os.cpu_count()`'u değiştirmez: yalnız affinity'yle yapılan koşum bir
  CI kırılmasını kaçırdı (2026-10-11). Tek çekirdek için maske 1 ve `PYTHON_CPU_COUNT=1`. `PYTHON_CPU_COUNT`'u
  LTspice'lı koşumda kullanma: LTspice gerçek çekirdek sayısını görür, örnek 12 yanlış alarm verir.
  `LTSPICE_LLM_CPU_BUDGET` bu koşulu kurmaz: `tests/conftest.py` onu siliyor.

  LTspice'lı testleri ve örnekleri de tek çekirdekte (yalnız affinity) koş: örnek 12 LTspice'ı adaptörsüz
  başlatıyordu ve orada takıldı. LTspice testi sabit bir `threads` bekleyecekse 1 seç: kırpma onu değiştirmez ve
  LTspice'a çekirdekten fazla iş parçacığı verilmez. Yerel kapı yine de CI'ın kendisi değildir: push'tan sonra CI
  sonucuna bak (`gh run list`; kırık job'ın kaydı `gh run view <id> --log-failed`).
- Test gücü: `python scripts/mutants.py`. Bir hata düzelttiğinde kataloğa "düzeltmeyi geri al" mutantını ekle;
  hepsi ölmeli. Claude Code'da test değişikliklerinden sonra `test-reviewer` subagent'ı bağımsız inceleme yapar.
  Mutantı hangi testin öldürdüğüne bak. Yanlış nedenle ölen mutant (sahte süreçte olmayan metot, uç noktayı
  düşüren seyreltme, gövdesi silinip sözdizimi hatası veren döngü: çıktıda öldüren test adı yok) boşluğu gizler.
- Bu kalıplar yanlış kodla da geçer (2026-10-06'da altı mutant sağ çıktı):
  - Sıralamayı tek adayla sınamak.
  - Genel tepe ya da dip fixture'ın ucundayken yalnız uç değeri denetlemek.
  - Kurulu kütüphanedekiyle aynı fixture (kod kütüphaneye düşüp geçer).
  - Yalnız üst sınırı olan sayaç.
  - Modülün kendi fonksiyonunu mock'layıp "çağrıldı mı" saymak.
  - Aranan metin sayfanın başka yerinde de geçerken `in` ile aramak (2026-10-10: uyarı, rapordaki denetim açıklamasında
    da yazıyordu). `count(...) == 1` kullan.
  - Seyreltilen izde dışlanacak tepeyi pencereden uzağa koymak: seyreltme onu zaten atar, maske sınanmaz. Pencerenin
    hemen dışına koy.
  - Log satırını `in` ile aramak (2026-10-11): `"Maximum thread count: 1" in log`, 12 çekirdeğin varsayılanı olan
    `...: 12` satırında da doğru. Satırı tam eşleştir (`^...: (\d+)\s*$`, `re.MULTILINE`).
- Refactor girintiyi değiştirince katalogdaki metin eskir (`ESKİ`). Mutantı aynı anlamla yeni metne taşı, silme.

## Proje

- Paket: `ltspice_llm/`, testler: `tests/`, örnekler: `examples/`, dersler: `docs/`.
- Yeni örnek betiği depo kökünü `sys.path`'e ekleyerek başlasın (`sys.path.insert(0, str(ROOT))`). Koşudan önce
  `ltspice_llm.__file__`'a bak. `pip install -e` başka bir kopyayı gösteriyorsa kökü eklemeyen betik o kopyanın
  paketini sessizce çalıştırır (`docs/LESSONS_LEARNED_PERFORMANCE.md` §7).
- Örneklerde iskeleti kopyalama: rapor yolu ve kaydı `examples/study_helpers.py`'de. Rapor yolu `report_path(ad)` ile
  kurulur: testler `LTSPICE_LLM_REPORT_DIR` ile geçici klasöre yazdırır, `reports/` ezilmez.
- Örnekler test paketinde (`tests/test_examples.py`):
  - Her örnek içe aktarılır.
  - `--full` her örneği LTspice ile kendi sürecinde, hızlı kipte koşar (`-m "not examples"` atlar).
  - Örnek 14 koşulmaz: `tests/data`'daki kayıtları yeniden yazar.
  - API değiştirince örnek testleri de yeşil olmalı. Etkileşimli soru soran örneğe etkileşimsiz kip ekle.
- **Kalite kapısı:** `python scripts/check.py` (`.asc` bütünlüğü + ruff F/B + LTspice'sız testler + use case coverage tanığı, ~20 sn);
  `--full` testleri ve örnekleri LTspice ile koşar (i7-7500U'da ~8 dk; örneksiz `pytest -m "not examples"` ~1.5 dk). Bir işi "bitti" demeden önce çalıştır ve özet satırını göster.
  CI ve Claude Code Stop hook'u aynı komutu kullanır.
- Testler tek tek: `python -m pytest tests -q`. LTspice gerektiren testler (`tests/test_adapter.py`,
  `test_integration_*`) LTspice yoksa atlanır. Uyarılar hata sayılır, tek test 120 sn'de kesilir (`pyproject.toml`).
- Büyük tasarım/doğrulama işleri `docs/MISSION_BRIEF_TEMPLATE.md` ile başlar: başarı ölçütleri `.meas` eşikleri,
  özerklik zarfı ve kanıt (`SimulationReport`).
