# Değişiklik Günlüğü

## Yayımlanmadı

Yeni (2026-11-13):

- KiCad kart okuyucu (`ltspice_llm.board`): iz uzunluğu elle girilmez, `.kicad_pcb` dosyasından okunur.
  `read_kicad_board` dosyayı yalnız okur (KiCad 6 ve sonrası; KiCad kurulu olmasa da çalışır),
  `KicadBoard.trace_path` iki pad arasındaki en kısa iz yolunu bulur, `BoardPath.parasitics` her parçayı kartın katman
  yapısındaki dielektrik kalınlığı, bağıl geçirgenlik ve bakır kalınlığıyla `calculate_trace_parasitics`'e verir,
  `inject_board_path_into_netlist` yolu netliste ekler. Bağlantı yalnız bakır dökümden geçiyorsa, yol iç katmandaysa
  (stripline) ya da yönlendirilmemişse `BoardPathError` verilir: uzunluk uydurulmaz. İzin altındaki dönüş düzleminin
  örtme oranı, paralel yol ve döküm uyarı olarak döner. Via'lar sayılır; değerleri girdidir.
- `read_board_trace` aracı (genişletilmiş küme; varsayılan 10 araç değişmedi). Kart yolu çalışma alanında olmalı:
  `allow_file_access` olmadan mutlak yol, `..`, `~`, `%VAR%` ve dışarıyı gösteren bağlantı reddedilir. Yalnız
  `.kicad_pcb` okunur; okunamayan dosyanın içeriği hata iletisine yansıtılmaz.
- Uçtan uca örnek `examples/21_board_trace_to_simulation.py`: karttan okunan yol bir besleme devresine eklenir.
  LTspice'ta DC düşüm I × R ile, yük basamağındaki çökme ideal telle karşılaştırılır; Gear denetimiyle rapor yazılır.
  Kendi kart için `--kart <dosya> --yol <REF.PAD> <REF.PAD>`. LTspice entegrasyon testi: eklenen yolun DC direnci
  elle hesapla aynı.
- Doğrulama: okuyucu KiCad 10.0.3'ün kendi `pcbnew` hesabıyla karşılaştırıldı (`examples/20_kicad_board_checks.py`;
  KiCad tarafı `examples/kicad_oracle.py`). İki sentetik test kartı ve KiCad'in demo kartlarından 13'ünün 12'si aynı
  çıktı: iz uzunlukları, pad konumları, bağlı pad kümeleri, dökümün örttüğü noktalar. Kalan demo bakır katmana çizilmiş
  netli bir şekil içeriyor; okuyucu çizimi bakır saymaz, uyarı verir. Sentetik kartlar ve KiCad tanıkları
  `tests/data/kicad10_*`, testler `tests/test_board.py`, use case `tests/usecases/board_trace.json`; ölçümler ve KiCad'in
  Python'unun tuzakları (kartın yanına proje dosyası yazması, ekranda yanıt bekleyen uyarı pencereleri)
  `docs/LESSONS_LEARNED_KICAD.md`.

## 1.5.0 — 2026-11-11

Yeni (2026-11-11):

- `compare_circuits` aracı ve `compare_netlists` / `compare_measurements`: simüle edilmiş iki devrenin netlist farkı
  (değişen eleman, `.param`, `.options` seçeneği, analiz komutu) ve ortak `.meas` sonuçlarının farkı. Tam olarak bir
  değişken değiştiyse `single_variable` doğrudur; `.meas` tanımı iki devrede farklı olan ölçüm `same_definition: false`
  ile işaretlenir. Steplenmiş koşum karşılaştırılmaz.
- `export_waveforms_csv` aracı ve fonksiyonu: `.raw` izlerini CSV'ye yazar. Değerler yuvarlanmaz, Tstart kayması zamana
  eklenir, `.ac` izleri `re(...)` / `im(...)` sütunlarına, steplenmiş koşum `step` sütunuyla yazılır; sütun ve ondalık
  ayırıcı seçilebilir. `remove_circuit` ve `cleanup_workspace` çalışma alanındaki `.csv` dosyasını da siler.
- İki araç da genişletilmiş kümededir (`include_extended=True`); varsayılan 10 araç değişmedi.
- `docs/GUIDE.md`: `docs/KILAVUZ.md`'nin İngilizce çevirisi; README artık onu gösterir. `tests/test_docs.py` iki
  belgenin başlık, tablo ve kod bloğu yapısının aynı kaldığını denetler. Kılavuzun proje ağacına `providers.py` eklendi.
- Antigravity hook'ları (`.agents/hooks.json`): `.claude/hooks/` altındaki dört hook (oturum başı, komut ve düzenleme
  öncesi, tur sonu) Antigravity'de de çalışır. Betikler girdinin hangi ajandan geldiğini tanır ve o ajanın çıktı
  biçimini yazar; sorun yoksa hiçbir şey basmaz, çünkü Antigravity'de `"allow"` kullanıcının onay adımını atlar.
  İki ajanda da `guard_tests.py` `.asc` dosyasına Edit/Write'ı, `pre_ltspice_guard.py` `git clean` ve
  `git reset --hard`'ı (`git -C` ve `cmd /c` içinde de) reddeder. Tur sonu kapısı Antigravity'de yalnız tur
  kendiliğinden bitince koşar; arka plan görevi sürerken ya da tur başka nedenle (ör. kullanıcı iptali) bitince
  koşmaz. `test-reviewer` alt ajanının Antigravity kopyası
  `.agents/agents/test-reviewer.md`; `tests/test_harness.py` iki kopyanın gövdesinin aynı kaldığını denetler.
- Testler: bağımsız inceleme bu maddelerin testlerinde, bugünkü kodun doğru davrandığı girdilerle öldürülebilen 30 sağ
  kalan mutant buldu (kod hatası değil, test boşluğu). Eklenen testler:
  - Hook'larda Claude Code çıktıları (PreToolUse reddi, Stop'ta `block` ve `stop_hook_active`, SessionStart), iki
    ajanın olay-betik eşlemesi, var olan `.asc`'nin reddi ve komutun platformun kabuğuyla okunması.
  - `compare_circuits` uyarıları ve ölçüm satırının tanımına bağlanması; `.model`, alt devre kapsamı, `.options`
    değerinin sayı olarak karşılaştırılması, `.option`, `.backanno`.
  - Adım başına zaman ekseni ve iki kılavuzdaki sayıların aynılığı.
  - ADI çarpan tablosundan kurulan bağımsız oracle'lı bir Hypothesis testi.

  Yeni gerçek kayıtlar `ref26_cmp_meas_fail`, `ref26_cmp_step_c` (`examples/14` `compare`); kataloğa 28 mutant
  ("inceleme 11-11"). Açık kalan biri: `.raw`'da yalnız harf büyüklüğüyle ayrışan iki iz. LTspice'ta adlar büyük/küçük
  harfe duyarsız olduğundan (ADI "Node Naming") böyle bir kayıt beklenmiyor.

Bağımsız inceleme (2026-11-08) düzeltmeleri:

- Güvenlik, `simulate_monte_carlo`: toleranslı `.param` değeri `re.sub`'ın yerine koyma şablonuna gömülüyordu. Şablon
  değerdeki `\n` kaçışını satır sonuna çevirir: oluşturulurken tek `.param` satırı olan metin Monte Carlo netlistinde
  `.wave` gibi bir direktif olup denetlenmeden LTspice'a gidebiliyordu. Değer artık dilimle konur ve Monte Carlo
  netlisti LTspice'tan önce yeniden denetlenir.
- Güvenlik, `file_access_problems(content, kind)`: şema mı netlist mi olduğu içerikten tahmin edilmez. Araçlar yazdıkları
  dosyanın türünü verir (`"netlist"`, `"asc"`), varsayılan `"auto"` iki biçimde de tarar. Eskiden BOM'lu, sekmeli, küçük
  harfli ya da `SHEET`'siz şemanın direktifleri, başlığı `Version` olan netlistin satırları denetimden geçmiyordu.
- `check_design_safety` steplenmiş koşumda her ölçümün en kötü adımına bakar; bir adımda başarısız ölçüm bileşeni
  UNCHECKED yapar. Eskiden adımların ortalamasını kullanıyordu: sınırı aşan bir köşe varken PASS ya da MARGINAL verebilirdi.
- Steplenmiş `.meas` istatistikleri (ve `measurements` özeti) yuvarlanmaz; dalga özeti ve E serisi farkı anlamlı
  basamağa yuvarlanır. Ondalığa yuvarlama ns, µA ve pF düzeyindeki değerleri 0 gösteriyordu (`tau` = 0.000999668 s
  istatistikte 0.001 oluyordu).
- `.ferret` ayrıca denetlenmez: LTspice 26.1.1 "This directive is no longer supported" der, ağa ve diske dokunmaz (ölçüldü).
- `cleanup_workspace(max_age_seconds=0)` belgedeki gibi hepsini siler. Windows'ta Python 3.12'nin saati 15,6 ms adımlı;
  az önce yazılan dosyanın zaman damgası saatin ilerisinde kalınca dosya atlanıyordu (400 dosyanın 45'inde, ölçüldü).
  Haftalık işte `test_files_that_cannot_be_deleted_are_reported_not_raised` bu yüzden rastgele düşüyordu.

- Ajan skill'leri (`.agents/skills/`, Agent Skills biçimi): `narrative-pptx` (JSON şemadan düzenlenebilir `.pptx`,
  yedi düzen, örnek şema `assets/example_spec.json`, geometri ve görsel denetim) ve `explainer-gif` (genel tarama,
  koyu ışımalı tarama ve eğri ailesi geçişi arketipleri, HTML/WebGL sahnesi yakalama, GIF doğrulayıcı). Claude Code
  için `.claude/skills/` altında aynı önbilgiyi taşıyan yönlendirmeler. Bağımlılıklar `pip install -e ".[media]"`
  (python-pptx, Pillow); `dev` bunu ve SKILL.md önbilgisini denetleyen PyYAML'ı içerir. Skill'lerin testleri ve
  mutantları: `tests/test_skills.py`, `scripts/mutants.py` ("skill 11-03", "reviewer 11-07").
- explainer-gif okuma süresi alt sınırı iki yerde deliniyordu: kelime payının üst sınırı (2700 ms) daha büyük bir
  `min_dwell_ms`'yi eziyordu; eksik süre en yakın 10 ms'ye yuvarlanınca metin alt sınırın 10 ms'ye kadar altında
  kalabiliyordu (komşulara bölmede `round(2.5) = 2`). HTML/WebGL yakalayıcısında sanal saat hiç kurulmuyordu
  (`add_init_script` yalnızca gezinmede çalışır, sahne `set_content` ile yükleniyordu).
- explainer-gif `adaptive_event_samples`: birbirine ya da uçlara yakın `snap_points` kayboluyordu (iki an aynı örneğe
  düşüyor, uç sıfırlaması ilk ve son örneğe yakın anı siliyordu), GIF'te olay anı tam kareye düşmüyordu. Her an artık
  ayrı bir iç örneğe düşer; iç örneklere sığmayan anlar `ValueError` verir.
- HTML/WebGL yakalayıcısı sahneyi ağ kapalı çalıştırır (`offline=True`): sahne belgeye göre kendi kendine yeter, ama
  kodda engel yoktu; dış adreslere script, fetch ve WebSocket isteği atabiliyordu.
- narrative-pptx `validate_deck` PowerShell'i tam yoluyla başlatır: yalın ad, sistem klasöründen önce çalışma klasöründeki
  `powershell.exe`'yi çalıştırırdı.
- `scripts/mutants.py`: öldüren testi LTspice'ın kurulu verisine bakan üç mutant, veri yokken koşulmaz ve `ATLANDI` diye
  yazılır. Haftalık iş bu yüzden kırmızıydı (316/319) ve sonraki izolasyon adımına hiç geçemiyordu.
- `examples/19_simulation_to_slides.py`: RC basamak yanıtından (tek değişken R) HTML rapor, iki GIF ve `.pptx`;
  bütün sayılar `.meas`'ten, çıktılar denetimden geçer.
- narrative-pptx: `spec_warnings` sığmayan kartları ve kalan `⟨...⟩` yer tutucularını bildirir; varsayılan
  etiketler sunumun diline göre (İngilizce sunumda "MECHANISM", "▶ LIVE GIF").
- `verify-review` skill'i `.agents/skills/`'e taşındı (Antigravity de görür); `.claude/skills/` yönlendirmedir.
- Türkçe sunumda (`"language": "tr"`) büyük harfe çevrilen çip ve rozetlerde i -> İ ("GEÇİŞ", eskiden "GEÇIŞ").

- `run_netlist` / `run_sweep`: dosya yoluyla verilen devre kendi klasöründe koşuyor, kopya ve çıktıları çalışma alanına
  taşınıyor. Eskiden kopya çalışma alanında koşuluyordu: şemanın yanındaki model dosyası (göreli `.include`/`.lib`)
  "File not found" verdi, yanındaki sembol netlistten uyarısız düştü ve koşum yanlış devreyle başarılı göründü
  (LTspice 26.1.1; `examples/12` §10).
- `providers.from_anthropic` / `to_anthropic`: Claude'un düşünme blokları (ve `fallback` blokları) değiştirilmeden geri
  gidiyor ("preserved thinking"); `AutonomousCircuitAgent` yanıtın sağlayıcıya özgü alanlarını geçmişte tutuyor.
  Eskiden düşünme blokları her turda atılıyordu. Akışlı yanıtta çıktının ortasında başka modele geçilirse son
  `fallback` bloğundan önceki thinking ve tool_use blokları geri gönderilmiyor, sınırdan önceki araç çağrısı
  yürütülmüyor (Anthropic belgeleri, "Echoing fallback turns back").
- `AutonomousCircuitAgent(system_prompt=...)` ve İngilizce sistem istemi `CIRCUIT_DESIGNER_SYSTEM_PROMPT_EN`.
- README: Claude ile eksiksiz döngü, kendi şemasını koşma, LTspice'ın kendi MCP sunucusundan farkı, modelin gördüğü
  metinlerin dili ve araç katmanına model dosyası verme.
- Testler: iş nesnesi testi Windows venv'inde kırılıyordu (venv başlatıcısı ikinci süreç açar).
- Geliştirme: mypy (`<2.5`) ve ruff (`<0.18`) için üst sürüm sınırı; ilk commit'in CI'ı yeni mypy sürümüyle kırılmıştı.
- Örnek 05'in gerekçe metni simüle edilen değerleri yazıyor; `interactive_designer` başlığı dil modeli
  kullanmadığını söylüyor.

## 1.4.0 — 2026-10-24

İlk açık kaynak yayın (GPL-3.0-or-later). Depo kütüphanenin bu sürümdeki hâliyle başlar; önceki geliştirme geçmişi
taşınmadı.

- `ltspice_llm`: LTspice'ı arayüzsüz çalıştıran adaptör (`run_netlist`, `run_sweep`, `run_batch`; eşzamanlı
  koşumlarda kilitlenme koruması), `.log`/`.raw` ayrıştırıcıları, dil modeli araçları ve sağlayıcı dönüştürücüleri
  (OpenAI, Anthropic, Gemini: araç şeması, istek ve yanıt biçimi), yakınsama teşhisi, E serisi kuantalama, `.asc` şema okuma/yazma, PCB parazitikleri ve HTML simülasyon
  raporu.
- `examples/`: 14 örnek. 12 ve 15-18, kütüphanedeki düzeltmelerin LTspice 26.1.1 ölçümleri ve raporları; belgelerdeki
  sayılar bu betiklerden gelir.
- `tests/`: çoğu LTspice'sız koşan testler, gerçek LTspice kayıtları (`tests/data`), use case akış denetimi
  (`scripts/flows.py`), test izolasyonu (`scripts/isolation.py`) ve mutant kataloğu (`scripts/mutants.py`).
- Güvenlik: `SECURITY.md` (özel bildirim kanalı ve kapsam). Dil modelinin netlistindeki dosya erişimi denetimi ortam
  değişkenli yolları da reddediyor (LTspice 26.1.1 `.include %VAR%\...` yolunu açıyor, `examples/12` S3). HTML raporu
  ağdan yazı tipi yüklemiyor. `tests/data`'daki kayıtlar kayıt makinesinin yolunu taşımıyor.
