# ⚡ Performans ve Kararlılık: Lessons Learned

**Tarih:** 2026-09-30 (§10: 2026-10-06) · **Sürüm:** 1.4.0 · **Ortam:** LTspice 26.1.1, Windows 11, Intel Core i7-1255U
(2 P + 8 E çekirdek, 12 mantıksal işlemci, 15 W), "Balanced" güç planı

Bu doküman, bir yapay zekâ aracının (Antigravity) ürettiği performans incelemesinin doğrulanması,
düzeltilmesi ve bu süreçte ortaya çıkan kilitlenme (livelock) sorununun kök nedeninin bulunmasıyla
öğrenilenleri kaydeder. Buradaki her sayı bu depodaki kodla **ölçülmüştür**; tahmin yoktur.

---

## 0. Sonuç özeti

Referans devre: 500 kHz senkron buck, 2 ms `.tran`, 20 ns maksimum adım. "Önce" = eski varsayılanlar
(ASCII `.raw`, sinyal budaması yok, sıralı çağrılar, kilitlenme koruması yok).

| İş | Önce | Sonra | Hız | Disk |
| --- | --- | --- | --- | --- |
| Tek simülasyon | 5,72 sn / 39,2 MB | 1,34 sn / 6,8 MB | 4,3x | 5,8x az |
| Tek simülasyon + `save_signals` | 5,72 sn / 39,2 MB | 1,03 sn / 1,4 MB | 5,6x | 28,8x az |
| 8 noktalı parametre taraması | 55,7 sn / 312 MB | 6,1 sn / 10,8 MB | 9,2x | 28,8x az |
| 12 bağımsız devre | 81,3 sn / 474 MB | 12,3 sn / 16,4 MB | 6,6x | 28,8x az |
| Optimizasyon (9 iterasyon) | 23,6 sn / 177 MB | 9,5 sn / 6,1 MB | 2,5x | 28,8x az |
| 8 eşzamanlı `run_netlist` (ayrı adaptörler) | **1/8 başarılı** | **8/8** | — | — |
| 4 ayrı Python süreci × 3 koşum | **0/12 başarılı** | **12/12** | — | — |

Optimum parametre ve `.meas` değerleri değişmedi.

---

## 1. Ölçmeden hiçbir optimizasyon önerisini kabul etme

Antigravity'nin incelemesi makul görünüyordu ama ölçüldüğünde büyük kısmı tutmadı:

| Öneri | İddia | Ölçülen | Karar |
| --- | --- | --- | --- |
| `ProcessPoolExecutor` / `n_jobs=8` | 4-12x | Olduğu gibi uygulanınca **simülasyonlar kilitlendi** (0,04x, 16'dan 7'si öldürüldü) | Kök neden bulunup düzeltildi → 2-2,4x |
| `.options method=gear` | 2-3x | 1,07x (gürültü) | Hız ayarı değil; yalnızca yakınsama için |
| `.options plotwinsize=0` | Sıkıştırmayı kapatıp hızlandırır | 1,02x, `.raw` boyutu değişmedi | Etkisiz |
| `.options reltol=0.005 trtol=7` | "Doğruluğu bozmadan" 2x | 1,10x, ölçüm değerlerini değiştirdi | Reddedildi |
| `.save` ile sinyal budama | 3-5x ayrıştırma | Dosya 2,5x küçük, ayrıştırma 2,4x hızlı | Doğru, uygulandı |
| RAMDisk | 1,5-2x | Ölçülmedi; disk darboğaz değil | Uygulanmadı |
| Dil değişimi (Rust/C++) | %0,2-1 | — | Doğru: darboğaz LTspice süreci |
| Optimizasyon döngüsünü paralelleştir | 50 iterasyon 10 dk → 1-2 dk | İkili arama **sırasaldır**, paralelleştirilemez | İddia yanlış |

**Kural:** Her performans önerisi için önce ölçüm betiği yaz, önceki/sonraki değerleri ve sonucun
doğruluğunu (ölçüm değerleri aynı mı?) birlikte raporla.

---

## 2. En büyük kazanç varsayılanlarda saklıydı ve incelemede hiç yoktu

`raw_format="auto"`, `.step` içermeyen netlistleri **ASCII** `.raw` ile çalıştırıyordu. Bu, steplenmiş
ASCII dosyaları okuyamayan spicelib için yapılmış bir düzeltmenin yan etkisiydi: steplenmiş netlistler
ikili biçime alınmış, geri kalanı ise hiç ölçülmeden ASCII'de bırakılmıştı.

- ASCII: 3,54 sn simülasyon / 39,2 MB / 0,95 sn ayrıştırma. İkili: 1,09 sn / 6,8 MB / 0,05 sn.
- Özet değerleri tran/ac/dc/op/noise analizlerinde bağıl fark 0 ile aynı; `.meas` değerleri ~1e-5 farkla aynı
  (LTspice iki biçimde dalga formunu farklı sıkıştırıyor).
- Tek başına tüm kazançların en büyüğü (~4,3x). Artık `auto` her zaman ikili biçim.

**Ders:** Bir hatayı düzeltirken seçilen varsayılanın performansa etkisini de ölç. Profil çıkarmadan
"yeni özellik ekleyelim" demeden önce mevcut varsayılanlara bak.

---

## 3. `.save` budaması ölçümleri bozmamalı

`.save` yalnızca listelenen sinyalleri kaydeder; `.meas`'in kullandığı sinyal listede yoksa ölçüm
**sessizce üretilemez**. Bu yüzden `save_signals` her zaman `.meas` satırlarında geçen sinyalleri
(`measured_signals`) otomatik ekler. Sinyal adları doğrulanır (satır sonu, `;` ile enjeksiyon reddedilir).
Netlistte zaten `.save` varsa ya da `.asc` şemada `TEXT ... !.save` satırı varsa dokunulmaz.

---

## 4. Paralel LTspice neden kilitleniyordu (kök neden)

### Belirti

Eşzamanlı örnek sayısı arttıkça hızlanma önce artıyor, sonra **çöküyordu** ve bu rastgele oluyordu:
12 çekirdekte 4 örnek → 2,66x, 6 örnek → 0,39x, 8 örnek → 0,04x ve simülasyonlar zaman aşımına uğradı.
Aynı yapılandırma bir koşumda sorunsuz, bir sonrakinde çöküyordu.

### Kök neden

LTspice 26'nın çözücüsü (`Multi-threaded, self-authoring solver`) **tüm mantıksal çekirdekler kadar** iş
parçacığı açıyor (log: `Maximum thread count: 12`). Bu sayı sürecin affinity maskesini ve makinedeki diğer
yükü yok sayıyor: 3 çekirdeğe sabitlenmiş süreç de "12" yazıyor. Birden fazla örnek aynı anda çalışınca
çözücü **livelock**'a giriyor, yani CPU yakıyor ama ilerlemiyor.

Livelock'un imzası (bunu gördüğünüzde bu sorundur):

- Süreç bloklanmamış, izin verilen **tüm** çekirdekleri yakıyor: 3 çekirdeğe sabitli örnek 60 sn'de
  176 CPU-saniyesi harcadı (CPU/duvar = 2,9).
- Log `Direct Newton iteration succeeded in finding operating point.` satırında kalıyor; `.log`/`.raw`
  dosyaları hiç büyümüyor.
- Süreçteki iş parçacığı sayısı normal koşumdaki 6'dan 10'a çıkıyor.
- Zaman aşımıyla öldürülen koşum "başarısız simülasyon" gibi görünüyor ama logda hata yok.

Tek başına çalışan süreç, 3 çekirdeğe sabitlense bile kilitlenmedi. Livelock için birden fazla örneğin
aynı anda çalışması gerekiyor. Örnekler **ayrık** çekirdek kümelerine sabitlendiğinde de kilitlendiler;
yani etkileşim yalnızca çekirdek paylaşımından gelmiyor.

### Düzeltme ve nedensellik kanıtı

Belgelenmemiş `.options threads=N` çözücünün iş parçacığı sayısını sınırlıyor. Tek değişkenin bu satır
olduğu test:

| Yapılandırma | `threads` varsayılan (12) | `threads` sınırlı |
| --- | --- | --- |
| 8 serbest örnek | 1/24 başarılı | 24/24 (`threads=1`) |
| 4 örnek, her biri ayrı 3 çekirdeğe sabit | 0/12 başarılı | 12/12 (`threads=3`) |

**Kural:** Aynı anda çalışan tüm LTspice örneklerinin `threads` toplamı çekirdek sayısını aşmamalı.

### Yanlış izler (tekrar düşmeyin)

- `.options numthreads=N`, `nthreads=N`, `maxnthreads=N`, `internal_load_threads=N` → **syntax error**.
  Doğru ad `threads`.
- `OMP_NUM_THREADS` etkisiz. `LTspice.exe` içindeki `OMP_NUM_THREADS`, `__kmpc_*` dizgileri gömülü
  **LLVM derleyicisinin** OpenMPOpt geçişine ait; LTspice OpenMP çalışma zamanı kullanmıyor.
- Exe içindeki `vcomp` eşleşmeleri AVX-512 komut adları (`vcompresspd`), MSVC OpenMP değil.
- `LTspice.ini`'de thread ayarı yok.
- `.db` dosyası SQLite; kilitlenmeyle ilgisi yok.

### Hızlanmanın tavanı donanım

Kilitlenme çözüldükten sonra bile verim ~2-2,4x'te kaldı. Sebep yazılım değil:

- i7-1255U hibrit: aynı çözücü işi E-çekirdekte P-çekirdeğe göre **1,95x** yavaş.
- Tüm çekirdekler ortak 15 W bütçeyi paylaşıyor: 10 çekirdek yüklüyken P-çekirdek çözücü süresi
  0,53 → 0,73-0,85 sn.
- Her işi ayrı fiziksel çekirdeğe sabitleyince bile teorik tavan ~3,3x.
- 12 işlik yükte ölçülen: 4 örnek × 3 thread → 2,39x, 6 × 2 → 2,38x, 12 × 1 → 1,93x.

**Ders:** Paralellik kazancını tahmin etmeden önce işlemci tipine bak (`Get-CimInstance Win32_Processor`).
Fiziksel/mantıksal çekirdek oranı 10/12 gibi tuhafsa hibrit işlemcidir.

---

## 5. Teşhis sürecinde yaptığımız hatalar (neden ilk seferde bulamadık)

1. **Uzun bir benchmark'ı durdurunca tamamlanmış kısımların sonucu da kayboldu.** `.options threads=1`
   adayı ilk denemelerden birindeydi. O betik 8 eşzamanlı aşamada makineyi kilitleyince durdurdum ve çıktı
   arabelleğe alındığı için o satırın sonucunu hiç görmedim. Sonra `numthreads` geçersiz çıkınca thread
   sınırlamanın imkânsız olduğunu varsaydım.
   → **Arka plan betiklerinde `python -u` / `print(..., flush=True)` kullan; her sonucu anında yaz.**
2. **Tutarsız sonucu "makine gürültüsü" saydım.** Aynı yapılandırmanın bir koşumda 2,5x, diğerinde
   0,4x vermesi gürültü değil, rastgele tetiklenen bir hatanın belirtisiydi.
   → **Bimodal dağılım gördüğünde sebebi ara; medyanla örtme.**
3. **Kanıt olmadan kural koydum.** "En fazla 4 işçi" sınırı, kök neden bilinmeden konmuş bir yamaydı ve
   ağır devrede 4 işçi de kilitlendi.
4. **Başarı ölçütü betikleri yanıltıcıydı.** Bir betik logda yanlış dizgi aradığı için başarılı koşumları
   başarısız saydı. Geçersiz `.options` ile anında hata veren koşumlar ise "3,97x hızlı" göründü.
   → **Her yapılandırmada seçeneğin gerçekten uygulandığını logdan doğrula** (`Maximum thread count: N`,
   `method = Gear`, `syntax error` yok).
5. **Ölçümler birbirini kirletti.** Arka planda çalışan bir eşzamanlılık testi, aynı anda koşan diğer
   ölçümleri 3-10x yavaşlattı. → **Zamanlama ölçerken makinede başka LTspice çalışmadığından emin ol.**
6. **Zaman aşımını "başarısız simülasyon" sandım.** Loga bakınca koşumların hata değil, ilerlemeden
   durduğu (livelock) anlaşıldı. → **Başarısız koşumun logunu mutlaka oku.**

Kök nedeni bulan yöntem: her süreç için **CPU zamanı / duvar saati**, iş parçacığı sayısı ve bağlam
değişimini ölçmek (psutil). Spin/livelock'ta CPU patlar, kilit/disk beklemesinde CPU boşta kalır.

---

## 6. Kilitlenme korumasının tasarım kararları (`ltspice_llm/cpu_guard.py`)

| Karar | Neden |
| --- | --- |
| Koruma adaptörün içinde ve **varsayılan açık** | Kilitlenme `run_batch`'e özgü değil; kendi iş parçacıklarından çağrı, birden fazla ajan, ayrı süreçler de tetikliyor. |
| Bütçe **işletim sistemi dosya kilitleriyle** (`slot_NNN.lock`) | Farklı Python süreçleri arasında da geçerli; süreç çökünce kilidi işletim sistemi bırakıyor (test edildi). Windows `msvcrt.locking`, POSIX `flock` aynı süreçteki farklı tanıtıcılar arasında da çakışıyor. |
| Bütçe = affinity'ye göre kullanılabilir çekirdek | LTspice affinity'yi kendisi hesaba katmıyor. `LTSPICE_LLM_CPU_BUDGET` ile kısılabilir. |
| Tek koşum tüm çekirdekleri alır | Tek başına koşumda hız kaybı olmasın (ölçüm: 1,080 sn vs 1,101 sn). |
| Sıra bekleyen koşum `çekirdek/4` pay ister | İlk sürüm tüm koşumları seri hale getirdi (güvenli ama yavaş: 9,5-13 sn). Adil payla 3,9-6,1 sn. |
| Bütçe doluysa bekle, süre dolarsa hata dön | Aşırı abone etmek kilitlenmenin kendisi; beklemek her zaman daha iyi. |
| Takılma bekçisi (`stall_timeout=20`) + `threads=1` ile tek yeniden deneme | Korumanın göremediği yükler için (LTspice arayüzü, başka araçlar). Normal koşumda dosyalar <0,3 sn aralıkla büyüyor, livelock'ta hiç değişmiyor. Yeniden denemede bekçi kapalı; yavaş ama sağlıklı koşum kesilmesin. |
| Kullanıcının `.options threads=N` değeri ezilmez | Kullanıcının kararı. Koşum N çekirdek boşalana kadar bekler, gerekirse uyarı verilir. |
| Yuva alma ile kira bırakma bir kapı kilidiyle (`gate.lock`) sıraya konur | Kira yuvalarını tek tek bırakıyor; kapı yokken bekleyen koşum yarım kalmış bırakmayı görüp payından az yuva alıyordu (tam test paketinde yük altında 3 yerine 2; güvenli ama yavaş). Kapı milisaniyeden kısa tutulur; 2 sn'de alınamazsa kapısız devam edilir. `__del__` kapıyı beklemez: çöp toplayıcı aynı iş parçacığı kapıyı tutarken çalışabilir. |
| Eklenen satır `; ltspice_llm:auto-threads` ile işaretli | Araç katmanı çalışma alanındaki `.net` dosyasını **hem kaynak hem hedef** kullanıyor. İşaretsiz satır dosyaya kalıcı yazılıyor ve sonraki koşumda kullanıcı ayarı sanılıyordu. |

Doğrulama (gerçek LTspice): 8 iş parçacığı/ayrı adaptör 1/8 → 8/8; 4 süreç × 3 koşum 0/12 → 12/12;
yalnızca bekçiyle (koruma kapalı) 8 takılmanın 8'i de kurtarıldı.

**Gerileme (2026-10-01, `.asc` kodlama düzeltmesinden sonra).** Netlist bayt olarak okunup metin kipinde
yazılmaya başlanınca her okuma-yazma turunda bir CR birikti (`.end\r\r\r\n`). `.end\r` tanınmadığı için
`.options threads` satırı LTspice'ın okumadığı `.end` sonrasına eklendi; koruma ve bekçinin `threads=1` yeniden
denemesi sessizce etkisiz kaldı. 22 işlik bir varyant çalışmasında (4 örnek) logların hepsinde `Maximum thread count: 12`
yazdı; takma anı koşumları livelock'a girdi, 3'ü 30 sn'de kesildi, toplam 66,9 sn. Düzeltmeden sonra hepsinde
`Maximum thread count: 3`, toplam 9,0 sn, ölçümler aynı. Testler yakalamadı: dosyayı `read_text()` ile
okuyorlardı (CR'leri gizler) ve threads satırının `.end`'den önce olduğuna bakmıyorlardı. → Eklenen direktifin
yerini LTspice gibi denetle (ilk `.end`'e kadar, bayt okuyarak) ve logdaki `Maximum thread count`'a bak.

---

## 7. Ortam ve araç tuzakları (bu makinede)

- **Kabuk heredoc'u `\\n`'i gerçek satır sonuna çevirdi.** Python kaynak kodunu `python - <<'EOF'` ile
  yazarken ters bölülü dizgiler bozuldu ve test dosyası sözdizimi hatasıyla kaldı. → Kaçış karakteri içeren
  düzenlemeleri doğrudan dosya düzenleme aracıyla yap ya da `chr(92)` ile üret.
- **Windows konsolu cp1252:** Türkçe karakterli `print` `UnicodeEncodeError` veriyor.
  → Betiklerin başına `sys.stdout.reconfigure(encoding="utf-8")`.
- **Git Bash'te `taskkill /F`** yol sanılıyor (`F:/`). → `taskkill //F //IM LTspice.exe` ya da PowerShell.
- **`tasklist` yoğun yük altında dakikalarca asılı kalabiliyor.** → Süreç sayımı için psutil.
- **LTspice logları NUL karakterli (UTF-16 benzeri).** → `tr -d '\000'` ya da `.replace("\x00", "")`.
- **Paralel testlerde gerçek bütçeye dokunma:** `tests/conftest.py` her test oturumuna ayrı bir
  `LTSPICE_LLM_SLOT_DIR` verir. Aksi halde testler gerçek LTspice koşumlarını bekler ya da bütçelerini tüketir.
- **Sahte Popen testlerinde `_kill_process_tree`'yi mutlaka taklit et:** Gerçek hali `taskkill /PID <sahte pid>`
  çalıştırır ve makinede o PID'e sahip gerçek bir süreci öldürebilir.
- **İkinci makine (2026-10-03'ten beri):** Intel i7-7500U (2 çekirdek / 4 mantıksal işlemci), Windows 10, global
  Python 3.14 (venv yok, hook'lar global `python`'u çağırır). LTspice logunda varsayılan `Maximum thread count: 4`.
  ANSI kod sayfası cp1254: Türkçe harfler boruya basılabiliyor, Ω, → ve ✓ basılamıyor. 2026-10-10 ölçümü
  (`examples/10`): `run_sweep` 2.34x, `run_batch` 1.94x (4 örnek, örnek başına 1 iş parçacığı).
  → Bu belgedeki süre ve paralellik sayıları orada geçerli değil; performans iddiasını o makinede yeniden ölç.
- **`pip install -e` başka bir kopyayı gösterebilir.** İkinci makinede `ltspice_llm`, deponun eski kopyasına editable
  kuruluydu; geliştirilen kopya ayrı bir klasördeydi. `python examples/x.py` `sys.path`'e betiğin dizinini koyar, depo
  kökünü koymaz. Bu yüzden geliştirilen kopyada kökü eklemeyen örnekler (01–11, 13, `interactive_designer.py`) eski
  paketi sessizce içe aktarıyordu (2026-10-10, `ltspice_llm.__file__` ile doğrulandı). pytest etkilenmez
  (`pythonpath = ["."]`). Kopyalar aynı klasörde birleşince sorun kalktı.
  → Koşudan önce `ltspice_llm.__file__`'a bak; yeni örnekler `sys.path.insert(0, str(ROOT))` ile başlasın.
- **Kopyalanan `__pycache__` eski yolu taşır.** Klasör tarihleri korunarak kopyalandığında Python ve pytest eski `.pyc`'leri
  geçerli sayar. Gömülü dosya yolu eski klasörü gösterir; kod konumuna dayanan araçlar oraya bakar. 2026-10-10 taşımasından
  sonra 78 `.pyc` eski klasörü gösteriyordu. `scripts/isolation.py` `tests/test_symbols.py`'nin 28 testinin hepsini
  "tek başına koşmadı" diye işaretledi; önbellek silinince 0. Testler ve örnekler etkilenmez, geri izlemeler ve yerel
  coverage etkilenir. → Klasörü taşıdıktan ya da kopyaladıktan sonra `__pycache__` ve `.pytest_cache`'i sil.

---

## 8. Gelecekteki performans çalışmaları için kontrol listesi

- [ ] Makineyi kaydet: işlemci modeli, fiziksel/mantıksal çekirdek, güç planı, AC/pil.
- [ ] Ölçüm sırasında başka LTspice/ağır iş çalışmıyor.
- [ ] Her yapılandırmayı en az 2-3 kez tekrarla; en iyi **ve** en kötü değeri raporla.
- [ ] Seçeneğin gerçekten uygulandığını logdan doğrula.
- [ ] Sonucun doğruluğunu karşılaştır (`.meas` değerleri, optimum parametre).
- [ ] Başarısız/zaman aşımı koşumlarının logunu oku.
- [ ] Arka plan betiklerinde çıktıyı anında yaz (`-u` / `flush=True`).
- [ ] Eşzamanlılık değişikliklerinde `cpu_guard=False` ile eski davranışı A/B olarak da ölç.
- [ ] Sayıları dokümana "ölçüldü" diye yazmadan önce son kodla yeniden ölç.

Hızlı tekrar: `python examples/10_performance_fast_paths.py`.

---

## 9. Açık konular / yapılmayanlar

- `.options threads` belgelenmemiş bir direktif; yalnızca LTspice 26.1.1'de doğrulandı. LTspice
  güncellendiğinde `Maximum thread count` satırıyla yeniden doğrulanmalı.
- Paralellik ölçümleri tek bir dizüstü makinede yapıldı. Masaüstü/iş istasyonunda tekrar ölçülmeli.
- LTspice arayüzünde elle başlatılan simülasyonlar ve başka araçların koşumları bütçeye dahil değil;
  bunlara karşı yalnızca bekçi var. `open_in_gui` penceresinin thread sayısı kontrol edilemiyor.
- Çalışma noktası hesabı 20 sn'den uzun süren çok büyük devrelerde bekçi yanlış alarm verebilir
  (sonuç doğru ama tek thread'le yavaş). Böyle devrelerde `stall_timeout` artırılmalı.
- Bütçe kullanıcı başına (`%TEMP%`); aynı makinedeki farklı Windows kullanıcıları birbirini görmüyor.
- Optimizasyon sırasal. İyileştirme fikri: önce `run_sweep` ile tek süreçte kaba bir tarama yapıp aralığı
  daraltmak, sonra ikili aramak.
- `run_sweep` / `run_batch` LLM araç şemalarında yok; LLM şu an yalnızca sırasal `optimize_parameter`'ı görüyor.
- ~~`WaveformSummary` ortalaması aritmetik~~ ve ~~netlistte tek bağlantılı düğüm~~: kapatıldı, §11.
- ~~`WaveformSummary.downsampled_points` önizlemesi indeks aralıklı~~: kapatıldı, §12.
- ~~Tstart > 0 olan `.tran`'da grafik ve `read_trace` zaman ekseni `Offset` kadar kayık~~: kapatıldı, §12.
- `find_symbol` verilen `search_dirs`'te yalın adı alt klasörlerde de arıyor; LTspice şemanın alt klasöründe yalnızca
  göreli yolla buluyor (§11, LESSONS_LEARNED_LTSPICE_REFERENCE §2.18).

---

## 10. Süreç, disk ve bellek: ikinci yapay zekâ incelemesi (Gemini, 2026-10-06)

Gemini'nin "Windows çökmelerine karşı zırh" ve "bellek/disk şişmesi" önerileri ölçüldü:
`python examples/16_process_and_memory_hardening.py` (rapor `reports/16_process_and_memory_hardening.html`, ~90 sn).
Süreler altı koşumun aralığıdır. Açık tarayıcı ve editör 1-2 çekirdek kullanırken süreler 2-3 katına çıkabildi
(sembol taraması 1,1 sn yerine 11 sn). Bellek, dosya boyutu, sayım ve çıkış kodları koşumdan koşuma değişmedi.

| Öneri | İddia | Ölçülen | Karar |
| --- | --- | --- | --- |
| `SetErrorMode` | Çöken LTspice'ı WerFault timeout boyunca asılı tutar | Gerçek çökme (0xC00000FD) varsayılan hata kipinde WerFault ile 3,4-4,5 sn'de döndü; pencere açılmadı, asılı kalmadı. Hata kipi 3'te 0,27-0,32 sn | Kısmen tuttu. İş nesnesi bayrağıyla uygulandı (aşağıda) |
| Job Object `KILL_ON_JOB_CLOSE` | Python ölünce LTspice CPU yakmaya devam eder | Tuttu: ebeveyn ölünce LTspice 10 sn'de 19,6-19,95 CPU-sn harcadı ve **çekirdek bütçesi 12/12 boş göründü** | Uygulandı: LTspice 1 sn içinde ölüyor |
| NTSTATUS kodları | 0xC0000005 bozuk alt devre, 0xC00000FD döngüsel model, 0xC0000094 sıfıra bölme | 0xC00000FD tuttu (kendini çağıran `.subckt`, A→B→A, `.func`). 0xC0000094 ve 0xC0000005 üretilemedi: tamsayı 1/0 `Division by zero.` ile rc=1, B kaynağında 1/0 başarılı | Uygulandı. Neden yalnızca ölçülen kodda yazılıyor |
| `keep_raw=False` | Simülasyon %70 kısalır | `.raw`'ı sonradan silmek süreyi değiştirmez. En yakın şey (yalnız ölçülen sinyali `.save`): LTspice %10-20 kısa, duvar altı koşumda %1 uzundan %11 kısaya | Reddedildi. Disk için `_batch_*` temizliği ve kota |
| Otomatik `.save` | 39,2 → 1,4 MB (28x) | 39,2 MB eski ASCII varsayılanının boyutu (§0). İkili varsayılanla 6,79 → 1,36 MB (5,0x), ölçümler aynı | Reddedildi: `save_signals` zaten var (opt-in). Varsayılan olsa özet ve grafik araçları ölçülmeyen sinyalleri kaybeder |
| LTTB seyreltme | RAM 200 MB → 500 KB; tepe ve dipleri birebir korur | 67 MB `.raw` (1,12 M nokta, 13 sinyal) özeti **+907 MB**; neden okuma (aşağıda). LTTB 1000 kovanın 399'unda tepeyi, 627'sinde dibi ve global minimumu kaybetti; kova başına min/max 0 | Okuma düzeltildi (+99 MB). Çizimde min/max: 4,0-12 sn / 596 MB → 0,54-1,9 sn / 121 MB |
| Çalışma alanı kotası (500 MB, FIFO) | — | Varsayılan çalışma alanı 9 günde 3,16 GB (%99 `.raw`); 1,82 GB'ı `_batch_*` dizinlerinde ve `cleanup_workspace` alt dizinlere hiç girmiyordu | `_batch_*` temizleniyor. Kota isteğe bağlı (`workspace_quota_mb`) |

### Hata kipi ve iş nesnesi

- **Çocuk süreç yalnızca sürecin hata kipini miras alır.** `SetThreadErrorMode(3)` çocuğa geçmedi (çocuk 0),
  `SetErrorMode(3)` geçti. Gemini'nin önerisi bu yüzden Python sürecinin kendi kipini değiştirirdi: Python'un
  kendi çökmesi de bildirilmez. `JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION` aynı etkiyi (işteki her sürece
  SEM_NOGPFAULTERRORBOX) yalnızca LTspice'a verir: 3,4-4,5 sn → 0,26-0,55 sn (0,55 yüklü koşumda), WerFault 18/18 →
  0/18. `KILL_ON_JOB_CLOSE` tek başına süreyi değiştirmedi (3,3-4,4 sn).
- **Yetim LTspice kilitlenme korumasını atlatıyordu.** `cpu_guard` yuvaları dosya kilidi; ebeveyn ölünce işletim
  sistemi kilidi bırakıyor (§6, bilerek). LTspice ise 12 iş parçacığıyla çalışmayı sürdürüyordu. Bütçe boş
  göründüğü için yeni koşum da 12 iş parçacığı alır: §4'teki livelock koşulu. İş nesnesi bütçeyle gerçek yükü
  yeniden tutarlı kılıyor.
- **İş nesnesine yalnızca `_execute`'un başlattığı simülasyon girer.** `open_in_gui` penceresi Python bitince
  kapanmamalı; `-netlist` ve `-version` kısa süreli olduğu için dışarıda.

### Kök neden: spicelib'in tembel okuması

spicelib 1.6.3, LTspice'ın varsayılan ikili biçiminde ("Normal access", `plot_data.read_trace_data`) tembel
okunan her iz için bütün veri bloğunu (nokta × kayıt boyu) yeniden okuyor. İz, bu bloğa bakan yapılandırılmış bir
görünüm olarak önbellekte kaldığı için blok bırakılmıyor: 14 iz × 67 MB ≈ 940 MB. Ölçüm: tek iz okumak +71,5 MB
(4,5 MB'lık dizi için); 14 iz +955 MB, dizilerin toplamı 67,3 MB. `open_raw(path, traces=ALL_TRACES)` bütün izleri
tek geçişte okuyor: özet +907 MB → +99 MB, 0,52-1,15 sn → 0,22-0,48 sn, değerler birebir aynı. Tek iz okuyan yer
için iki biçim aynıdır. Çizim tek geçişte okuyor ve uzun izi `report._decimate` ile (eşit süreli kova başına min ve
max) 2000 kovaya indiriyor. 1500 piksellik PNG'de piksellerin %0,097'si farklı.

LTTB her kovadan tek nokta seçer. Anahtarlama düğümü gibi kova içinde iki uca da giden bir izde tepe ya da dipten
biri mutlaka kaybolur. Min/max her kovanın iki ucunu tutar ve nokta sayısı aynı (≈2000).

### Neden varsayılan kota yok

Grafik araçları (`tools.py`) `.raw`'ı sonradan `circuit_name` ile okuyor ve elde bir `SimulationResult` tutmuyor.
Kendiliğinden FIFO silme, LLM'in bir süre önce simüle ettiği devreyi çizdirmesini "dosya yok" hatasına çevirir.
Kota bu yüzden isteğe bağlı. Açıkken elde tutulan sonuçların dosyaları silinmiyor (`_LIVE_RESULTS`). Başka bir Python
sürecinin sonuçları görülmüyor. 1822 dosyalık çalışma alanını taramak 35 ms sürüyor.

### Teşhiste yaptığımız hatalar

1. **İlk çökme denemesi çökmedi.** `ctypes.string_at(0)` erişim ihlalini ctypes'ın SEH sarmalayıcısı `OSError`
   yapıyor (rc=1). Gerçek erişim ihlali için `faulthandler._read_null()` gerekti.
2. **Hata kipi mirası WER'i gizliyordu.** Claude Code'dan başlatılan süreçlerin hata kipi 3
   (SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX); bu ortamda WerFault hiç çalışmıyor. Ölçüm alt süreci
   `CREATE_DEFAULT_ERROR_MODE` ile başlatılınca 3,7 sn'lik bekleme göründü.
3. **Yerine koyulan programla ölçmek yanılttı.** Python'un gerçek erişim ihlali varsayılan kipte bile WerFault'suz
   0,09 sn'de bitti; LTspice ise 3,7 sn bekledi. Davranışı gerçek programla ölç.

---

## 11. Üçüncü Gemini incelemesi (2026-10-06)

Beş bulgu ölçüldü: `python examples/17_gemini_review_3.py` (rapor `reports/17_gemini_review_3.html`, ~5 dk).
İkisi (aritmetik ortalama, tek bağlantılı düğüm) bu belgelerin açık konularından sayılarıyla alınmıştı;
Gemini'nin transcript'inde o sayıları üreten betik yok. Her "eski" kol düzeltmenin monkeypatch'le geri alınması.

| Bulgu | İddia | Ölçülen | Karar |
| --- | --- | --- | --- |
| Kota `_bode.png` | Kota `x_bode.png`'yi `x_bode` koşumu sayar, elde tutulan `x`'in grafiği silinir | Tuttu. Gerçek `.ac` + `plot_bode_diagram`, kota 0: eski 6 dosya sildi (`filt_bode.png` dahil), yeni 5 | Uygulandı: `_run_names` iki adı da sahip sayar (`x_bode` adlı devrenin dalga formu grafiği de olabilir) |
| Özet ortalaması | `.meas AVG` 4,8408 V, özet 4,9848 V (+%3,0); "%3-10" | Tuttu, sayı bizim §9'dan. Buck V(vsw) eski 4,984767 / `.meas` 4,840820 / yeni 4,840820 V. 10 ns kenarlı darbede eski +%16,8 (0,589649 / 0,505000 V). RMS'te y²'nin yamuk integrali kenarda +%0,15; doğrusal ara değerin tam integrali `.meas RMS`'ten en çok 7,5e-6, elle hesaba 1e-6 içinde | Uygulandı (`time_weighted_mean_rms`, yalnız `.tran`) |
| Ajan araçları | `AutonomousCircuitAgent` genişletilmiş araçları göremiyor | Tuttu (10 araç). `calculate_mlcc_derating` araç değil, kütüphane fonksiyonu; genişletilmiş araç 5 | `include_extended` parametresi (varsayılan 10: mevcut sözleşme) |
| Sembol arama | `Documents\LTspice` aranmıyor | Tuttu, ama yol yanlış: kullanıcı klasörü `LTspice.ini` `UserCmpDir` (bu makinede OneDrive). LTspice klasörün kendisinde yalın adla, alt klasörde yalnızca göreli yolla buluyor | Uygulandı, LTspice'ın davranışıyla |
| Tek bağlantılı düğüm | Elle yazılan netlistte açık uç LTspice'tan SUCCESS döner | Tuttu (bizim `examples/16` T6). Kural ADI örneklerinin 312 netlistinde: LTspice'ın `NC_xx` dediği 158 pinin 158'i yakalandı; 458 işaretin (158 NC, 195 MP, 70 N###, 35 adlı) hepsi ikinci sayımda tek pinli. 134 netlistte en az bir işaret: çoğu bilerek boş pin ya da yüksüz çıkış | Uygulandı, **uyarı** olarak (`run_netlist`, yalnız `.net`) |

Ortalamanın neden saptığı (darbe, adım 1): adım düzlükte 48,8 ns (Tstop/1024, `.tran`'da en büyük adım verilmedi),
kenarda 0,49 ps'ye iniyor. Kenar çevresindeki fazla noktalar çoğunlukla 5 V köşelerinde duruyor: yüksek seviye
sürenin %10,0'ı ama noktaların %11,7'si; 5 V × 0,117 ≈ 0,587 V, eski özet 0,5896 V. İlk taslakta yazdığımız "düzlükte
~1 µs" ölçülmemişti ve yanlıştı.

Ölçümde yaptığımız hata: `LTspice.ini`'yi cp1252 diye okuduk, hiçbir anahtar eşleşmedi ve geçici ayar dosyası bozuk
çıktı; ilk `-ini` ölçümü "LTspice kullanıcı klasörünü aramıyor" gibi göründü. Dosya BOM'lu UTF-16LE. Gerçek dosya
ölçümden önce yedeklendi ve özeti her koşumdan sonra karşılaştırıldı (değişmedi). İkinci sayımımız da bir kez yanıldı:
`V4 Refin 0 {VREF}`'teki parametreyi düğüm saydı; `{...}` atlanınca uyuşmazlık kalmadı.

---

## 12. Dördüncü Gemini incelemesi (2026-10-06)

İki bulgu ölçüldü: `python examples/18_gemini_review_4.py` (rapor `reports/18_gemini_review_4.html`, ~6 sn). İkisi de
açık konularımızdandı: önizleme §9'da, `.log`/ASCII notu `examples/17` raporunun açık konularında (orada ölçülmemiş
bir tahmin olarak). Her "eski" kol düzeltmenin monkeypatch'le geri alınması.

| Bulgu | İddia | Ölçülen | Karar |
| --- | --- | --- | --- |
| Steplenmiş `.raw`, `.log` yok | spicelib `IndexError` veriyor, `open_raw` "steplenmiş ASCII" diyor | Tuttu, eksik söylenmiş. Tek geçişte (özet, grafik) yanlış neden; tembel açılışta (`read_trace`, Bode) açılış geçiyor, `IndexError` ilk izde açıklamasız. Ölçünce üçüncüsü çıktı: steplenmemiş bir koşumun `.log`'u adımları **sessizce tek adımda birleştiriyor**. 20 durum (biçim × log × açılış): eski 10, yeni 19 doğru | Uygulandı: adım sayısı 2'den azsa açılışta, nedeni söyleyerek hata; `IndexError`'da neden başlıktan (LTSPICE_REFERENCE §2.19) |
| Önizleme indeks aralıklı | Noktaların %80'i bir kenara yığılabilir; zamanda eşit aralıklı `np.interp` | Mekanizma tuttu, büyüklük tutmadı: en yoğun %10'luk sürede noktaların en çok %28'i (takma anı çınlaması, Gear); buck'ta %10,9. RC'de önizleme elle hesaptan 273 mV sapıyordu, yeni 0,007 mV. Olay pencerenin ortasındayken okuyanın eğrisinin RMS hatası %17,7 → %9,0 (pp'ye göre); 9 izin hiçbirinde kötüleşmedi | Uygulandı, yalnız `.tran` (`.ac dec` zaten logaritmik eşit aralıklı) |

Önizlemenin ölçütü: değerler zamansız verildiği için okuyan (LLM) onları zamanda eşit aralıklı sanar; bu varsayımla
kurulan eğrinin gerçek izden sapması. Noktaların neden az yığıldığı: LTspice düzlükte adımı süre/1024 civarında
tutuyor (§11), kenarlar birkaç düzine nokta ekliyor; yığılma ancak çınlama gibi uzun süren hızlı olaylarda büyüyor.
Eski önizlemenin hatası çözücünün nokta dağılımıyla değişiyor (aynı devre Gear'la %17,7 yerine %23,9), yeninin
değişmiyor (%9,0).

Ölçümde yaptığımız hatalar: (1) `.log`/ASCII notunu `examples/17`'de ölçmeden açık konuya yazmıştık; ölçünce asıl
sorun (sessiz birleşme) nottakinden büyük çıktı. (2) Yeni hata metninin ilk hali `IndexError` yolunda logun içine
bakmadan ".step satırı yok" diyordu; adım sayısı farklı bir koşumun logunda yanlıştı (`.step` satırlarını sayarak
düzeltildi). (3) Ölçüm betiğinde 5 adımlı koşum, döngüden kalan ASCII adaptörüyle koştu ve doğru olarak "ASCII"
hatası aldı; satır etiketi yanlıştı, kod değil.

test-reviewer 5 bulgu verdi; dördü sağ kalan mutantla doğrulandı ve testle kapatıldı:

- ASCII testi genel yedek mesajla da geçiyordu: başlık tanınmasa da yeşildi (3 mutant).
- ".log yok" deseni ".step satırı yok"u da eşliyordu (1 mutant).
- Önizleme toleransı 5 mV'tu: LTspice'ın çözümü elle hesaptan en çok 8,0 µV sapıyor, en yakın örneği alan yanlış
  önizleme mV mertebesinde sapıp geçiyordu. Tolerans 0,1 mV yapıldı.
- Darbe testinin gerekçesi yanlıştı: kenar uzaklığı 0,7 µs değil 0,15 µs.

Beşincisi (Tstart > 0 kaydı, "ızgara 0'dan başlar" mutantı) ilk bakışta gerçek LTspice verisinde eşdeğerdi: eksen
Tstart'ta da 0'dan başlıyordu. Altında başka bir hata vardı. Tstart > 0'da `.raw`'ın ekseni 0'dan yazılıyor, kayma
başlıktaki `Offset`'te ve spicelib bunu uygulamıyor (LTSPICE_REFERENCE §2.20); grafik ve `read_trace` ekseni Tstart
kadar kayıktı. Tanık LTspice'ın kendi `.meas WHEN` zamanı: izden bulunan geçiş RC'de 1 ms, diyot kırpıcıda
(`.tran 0 4m 2m`) 2 ms farklıydı; `raw_axis_values` Offset'i ekleyince fark 1e-15 s mertebesinde. Offset diğer analizlerde 0 olduğu için
analiz türüne göre dal yok. Düzeltmeden sonra o mutant da öldü (`ref26_rc_tstart`). test-reviewer'ın bu düzeltme için
iki bulgusu iki sağ kalan mutantla doğrulandı ve kapatıldı:

- Offset'i tembel açılışın ilk çağrısında (eksen henüz yüklenmemişken) unutan kod yolu sınanmıyordu.
- Yalnız ilk adıma Offset ekleyen kod geçiyordu: steplenmiş kayıt `ref26_rc_tstart_step` eklendi, her adımın geçişi
  kendi `.meas`'iyle karşılaştırılıyor.

"ASCII'de ve diğer analizlerde de aynı" iddiası yalnız silinen bir deneme betiğinde ölçülmüştü; `examples/18` T3'e
kalıcı satır olarak alındı.

Açık: verideki adımlardan az `.step` satırlı başka bir koşumun `.log`'unda tembel açılış hâlâ geçiyor. Hata ilk izde
spicelib'in `IndexError`'ı olarak çıkıyor. Açılışta yakalamak verinin adım sınırlarını okumayı gerektirir.
