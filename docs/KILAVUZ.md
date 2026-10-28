# Kullanım Kılavuzu

ltspice-llm'in ayrıntılı başvuru belgesi: özellikler, API örnekleri, performans ölçümleri, kilitlenme koruması, testler ve bilinen sınırlamalar. Kısa tanıtım için [README](../README.md).

---

## 🚀 Özellikler

1. **Headless Yürütme:**
   - LTspice'ı grafik arayüzsüz (`-b`) arka planda çalıştırır. Bir simülasyon, LTspice süreç başlatma süresi dahil, ölçülen makinede (LTspice 26.1.1) yaklaşık **0,7 sn** sürer.
   - LTspice'ı sırayla `executable_path` argümanı, `LTSPICE_EXE` ortam değişkeni, `PATH` ve bilinen kurulum konumlarında (Windows, macOS) arar.
   - Zaman aşımında yalnızca kendi başlattığı LTspice sürecini (ve çocuklarını / süreç grubunu) sonlandırır; açık olan diğer LTspice pencerelerine dokunmaz.
   - `.raw` biçimi varsayılan olarak **ikili (binary)**'dir (`raw_format="auto"`). Ölçülen 500 kHz senkron buck devresinde ikili biçim ASCII'ye göre simülasyonu **3-4x**, ayrıştırmayı **18x** hızlandırır ve `.raw` dosyasını **6x** küçültür (39,2 MB → 6,8 MB); ölçüm değerleri ~1e-5 bağıl farkla örtüşür. ASCII (`raw_format="ascii"`) yalnızca `.raw`'ı elle incelemek için gerekir ve `.step` ile çalışmaz (spicelib steplenmiş ASCII okuyamaz).

2. **Gelişmiş Çıktı Ayrıştırma (Parsers):**
   - **LogParser:** Çoklu kodlama (UTF-8, UTF-16, CP1252) desteği ile `.meas` ölçümlerini, `.op` çalışma noktalarını ve derleyici/sözdizimi hatalarını yakalar. Hiçbir adımda sonuç üretemeyen ölçümler (`Measurement "x" FAIL'ed`) `failed_measurements` alanında ve `warnings` içinde bildirilir; sessizce kaybolmaz.
   - **RawParser (LLM İçin Optimize):** `.raw` dosyalarındaki veri noktalarını min, max, RMS, peak-to-peak ve 15 noktalı trend önizlemesi olarak sıkıştırır. Steplenmiş dosyalarda ilk 3 adım ayrı ayrı özetlenir (`"V(out) [step 2]"`).

3. **Görselleştirme (Bode & Zaman Alanı & Multimodal):**
   - Matplotlib ile zaman alanı dalga formlarını ve **çift panelli (Kazanç dB + Faz) Bode diyagramlarını** PNG veya **Base64** olarak üretir (`plot_to_base64`, `plot_bode_to_base64`; araçlarda `include_image_base64=true`). Steplenmiş simülasyonlarda adımlar aynı grafiğe bindirilir.
   - Bode yalnızca `.ac`, dalga formu aracı yalnızca `.tran`/tarama analizleri için çalışır; yanlış analiz türünde açıklayıcı hata döner.

4. **LLM Function Calling / Tool Desteği (10 araç):**
   - `create_or_replace_circuit`, `modify_parameters`, `plot_transient_waveforms`, `plot_bode_diagram`, `get_circuit_netlist`, `list_circuits`, `quantize_parameters`, `diagnose_circuit_issue`, `optimize_parameter`, `search_ltspice_reference`.
   - Şemalar `get_ltspice_tool_schemas(format=...)` ile `generic`, `openai`, `anthropic` ve `gemini` biçimlerinde alınır. Biçimler sağlayıcı dokümantasyonuna göre üretilir, canlı API üzerinde otomatik test edilmez.
   - Araç argümanları sözlük veya JSON metni olabilir (OpenAI argümanları metin olarak gönderir).

5. **Standart Komponent Kuantalama (E-Series Snapping):**
   - Teorik değerleri (örn: $15.915\text{ k}\Omega, 1.414\text{ nF}$) **E6, E12, E24, E96** serilerine yuvarlar (nearest, floor, ceil) ve sapma analizini sunar.

6. **SPICE Yakınsama Hataları Teşhis Motoru:**
   - *Singular Matrix*, *Time step too small*, *Parallel Voltage Sources*, *Iteration limit reached* hatalarında log'u ve netlisti inceler, sorunlu düğümleri bulur, `.options` önerileri ve otomatik yama sunar. Yama `.end ; yorum` satırından önce eklenir; kullanıcının zaten ayarladığı bir seçenek ezilmez.
   - Seçenekler ADI TROUBLESHOOTING-GUIDE'ın sırasıyla gelir. Geçici rejimde ilk yama ADI örneğindeki üçlüdür (`method=gear itl4=50 cshunt=1f`); sonraki her teşhis netlistte ayarlı olmayan sıradaki tek seçeneği ekler (`abstol` → `gshunt` → `reltol` → `solver=alt`; `maxstep` devreye göre açıkça verilir). DC çalışma noktasında en az agresiften tek tek: `solver=alt` → `abstol` → `reltol` → `gmin` → `gshunt`. Tekil matriste `.options` eklenmez: ADI'ye göre topoloji sorunudur, yama yalnızca boşta düğüme direnç ekler.
   - Log ayrıştırıcısı ölçüm satırını hata sözcüğü aramadan önce tanır: `gain_error`, `temp_warning` gibi ölçüm adları ve `Circuit:` satırı ile "Files loaded:" listesindeki netlist yolu hata ya da uyarı sayılmaz.

7. **Parametrik & Sıcaklık Taraması (.step):**
   - Gerçek LTspice `.step` tablolarını ayrıştırır; `min`, `max`, `mean`, `std`, `range` ve başarısız adım sayısını hesaplar, her satıra adım parametresini (`rv`, `temp`...) ekler. `measurements` içinde `.step`'e bağlı ölçümler **adımların ortalamasıdır**; adım başına değerler `stepped_measurements` içindedir.

8. **Parametre ve Eleman Değeri Güncelleme:**
   - `run_netlist(parameters=...)` ve `modify_parameters` her ad için sırayla: var olan `.param` tanımını günceller, `{ad}` olarak kullanılıyorsa `.param` ekler, aynı adlı **R/C/L (ve yalın V/I) elemanının değerini** değiştirir. Hiçbirine uymayan ad `modify_parameters`'ta hata döner (etkisiz değişiklik sessizce `SUCCESS` sayılmaz).

9. **Görsel Şema (.asc) Desteği ve LTspice Grafik Arayüzü (UI):**
   - Metin netlistleri yerine gerçek görsel şemaları (`.asc`) simüle eder, parametrelerini günceller ve üretir (`AscSchematic`). `.asc` dosyaları LTspice'ın kodlamasıyla (cp1252 / UTF-16LE) okunur ve yazılır; UTF-8 yazılmaz (LTspice UTF-8 `1µ`'yü `1Âµ` = 1 F okur).
   - `adapter.inspect_schematic(path)` şemayı LTspice'ın kendi netlisterıyla (`-netlist`) çevirir ve bağlantısız pinleri (`NC_xx`) ve açık uçlu ağları raporlar. Koordinat hesabıyla üretilen şemaları bununla doğrulayın. `run_sweep` `.asc` şemayı aynı yolla taranabilir netliste çevirir.
   - Tek bir fonksiyonla (`adapter.open_in_gui(path)`) devreyi LTspice grafik arayüzünde (GUI) açarak bileşen sembollerini, bağlantı hatlarını, etiketleri ve prob ölçümlerini görsel olarak incelemenizi sağlar.

10. **ADI LTspice AI Reference Entegrasyonu:**
    - LTspice 26'nın kurduğu resmî ADI başvuru belgeleri (`%LOCALAPPDATA%\LTspice\reference`) kurulumdan okunur, depoya kopyalanmaz. `ReferenceDocs().search("...")` ve `search_ltspice_reference` aracı ilgili bölümleri döner. Konum `LTSPICE_REFERENCE_DIR` ile değiştirilebilir.
    - `find_model_definitions("LT1001")` bir model/alt devrenin LTspice kütüphanesindeki dosyasını bulur (`.lib LTC.lib`). Eksik model teşhisi bunu kullanarak `.lib` yaması önerir.
    - Belgelerdeki iddialar LTspice 26.1.1'de ölçülerek doğrulandı. Sonuçlar ve bu sırada düzeltilen hatalar (başarısız DC çalışma noktasının SUCCESS görünmesi, `.asc` µ kaybı, `WHEN …=1/sqrt(2)` sonucunun 1.0 okunması vb.) `docs/LESSONS_LEARNED_LTSPICE_REFERENCE.md` belgesinde. Yeniden üretmek için: `python examples/12_ltspice_reference_checks.py` (~15 sn).
    - `.four` sonuçları (`fourier`: THD %, harmonikler), `convreport` zorluk puanları (`convergence`) ve LTspice'ın `.meas` SQLite veritabanı (`db_path`, `read_measure_db`) sonuçlara eklendi.

11. **Simülasyon Raporu (`SimulationReport`):**
    - Simülasyon sonuçlarından tek dosyalık HTML rapor üretir. Rapor her testin sorusunu ve yöntemini, dalga formlarını (satır içi SVG, imleçle değer okuma), ölçüm tablolarını, bileşen etkilerini (önce → sonra, tek değişkenli varyant çiftlerinden), yeni etkileri, koşum güvenilirliğini (logdaki iş parçacığı ve yöntem, uyarılar, Gear çapraz kontrolü) ve varsayımları içerir. Rapordaki her sayı `.meas`/`.raw`'dan gelir; açık ve koyu temada okunur, LTspice gerektirmez.
    - Örnek: `python examples/15_hotplug_damping_validation.py` → `reports/15_hotplug_damping_validation.html`. Proje kuralı gereği her simülasyon süreci böyle bir raporla biter (GEMINI.md).

---

## 📁 Proje Yapısı

```text
ltspice-llm/
├── ltspice_llm/                  # Çekirdek Python Paketi
│   ├── __init__.py               # Dışa aktarılan modüller ve __version__
│   ├── adapter.py                # LTspice yürütme motoru, .asc/.net desteği, open_in_gui
│   ├── cpu_guard.py              # Eşzamanlı LTspice örnekleri için makine geneli iş parçacığı bütçesi
│   ├── schematic.py              # LTspice .asc şema oluşturucu (AscSchematic) ve pin dönüşümleri
│   ├── symbols.py                # .asy sembol pinleri (ADI kütüphanesindeki parçalar için get_pin)
│   ├── netlist.py                # LTspice gerektirmeyen netlist metni yardımcıları (.end, .step, analiz regex'leri)
│   ├── parser.py                 # .log (.step tabloları dahil) ve .raw ayrıştırıcıları
│   ├── visualizer.py             # Matplotlib dalga formu & Bode çizici (PNG / Base64)
│   ├── tools.py                  # LLM Tool / Function Calling arayüzleri ve şema biçimleri
│   ├── quantizer.py              # E-serisi komponent kuantalama motoru
│   ├── parasitics.py             # PCB izi, MLCC DC-bias, kablo endüktansı ve takma anı analizi (kaynaklı sabitler)
│   ├── diagnostics.py            # SPICE hata ve yakınsama teşhis danışmanı (ADI sorun giderme sırası)
│   ├── reference.py              # ADI LTspice AI Reference araması ve model kütüphanesi indeksi
│   ├── report.py                 # Simülasyon raporu (HTML): sorular, dalga formları, bileşen etkileri, güvenilirlik
│   ├── agent.py                  # Kapalı devre optimizasyon (CircuitOptimizer)
│   ├── circuit_agent.py          # LLM araç döngüsü, konuşma geçmişi ve araç günlüğü
│   └── system_prompts.py         # AI Donanım Mühendisi sistem promptu
├── examples/                     # Örnek Senaryolar (01-18)
│   ├── 01_basic_simulation.py             # temel simülasyon ve .meas ölçümleri
│   ├── 02_waveform_plotting.py            # dalga formu grafiği (PNG / multimodal LLM görseli)
│   ├── 03_llm_function_calling_demo.py    # LLM araç çağrısı (tool / function calling)
│   ├── 04_closed_loop_tuning.py           # kapalı döngü parametre optimizasyonu
│   ├── 05_filter_design_walkthrough.py    # araç çağrılarıyla filtre tasarımı (çağrılar betikte sabit)
│   ├── 07_quantization_sweep_and_diagnosis.py  # E serisi kuantalama, sıcaklık taraması, teşhis
│   ├── 08_agent_loop_with_scripted_llm.py # sağlayıcıdan bağımsız araç çağırma döngüsü
│   ├── 10_performance_fast_paths.py       # sinyal budama, run_sweep, run_batch ölçümleri
│   ├── 12_ltspice_reference_checks.py     # ADI referans iddialarının ve düzeltmelerin ölçümle doğrulanması
│   ├── 14_record_flow_fixtures.py         # use case testlerinin yeniden oynattığı gerçek LTspice kayıtları
│   ├── 15_hotplug_damping_validation.py   # takma anı aracının iddiaları LTspice ile ölçülerek + rapor
│   ├── 16_process_and_memory_hardening.py # Çökme, yetim süreç, disk, .raw belleği, .asy pinleri: ölçüm + rapor
│   ├── 17_gemini_review_3.py              # üçüncü Gemini incelemesinin iddiaları, ölçülerek + rapor
│   ├── 18_gemini_review_4.py              # dördüncü Gemini incelemesinin iddiaları, ölçülerek + rapor
│   └── study_helpers.py                   # rapor yolu ve kaydı
├── docs/
│   ├── KILAVUZ.md                         # bu belge: ayrıntılı kullanım kılavuzu
│   ├── LESSONS_LEARNED_PERFORMANCE.md     # performans/kilitlenme çalışmasının dersleri ve kontrol listesi
│   ├── LESSONS_LEARNED_LTSPICE_REFERENCE.md  # ADI LTspice AI Reference: doğrulanan/çürüyen iddialar, bulunan hatalar
│   ├── MISSION_BRIEF_TEMPLATE.md # Tasarım/doğrulama işi şablonu: .meas eşikleri, özerklik zarfı, kanıt
│   └── references/               # App note / datasheet kataloğu ve indirici (PDF'ler git'e girmez)
├── scripts/
│   ├── check.py                  # Tek kalite kapısı (insan, CI, Claude Code Stop hook'u)
│   ├── flows.py                  # Use case senaryolarının coverage tanığı
│   ├── isolation.py              # Her testi tek başına koşup birbirine dayanan testleri bulur
│   └── mutants.py                # Elle seçilmiş mutantlarla test gücü ölçümü
├── tests/                        # Testler, Hypothesis property testleri, gerçek LTspice log örnekleri (tests/data/)
├── .github/workflows/tests.yml   # CI: scripts/check.py (LTspice gerektirmeyen testler)
├── interactive_designer.py       # İnteraktif terminal arayüzü (CLI, [G] ile GUI açma)
├── pyproject.toml                # Paket konfigürasyonu (sürüm: ltspice_llm.__version__)
├── CHANGELOG.md
└── README.md
```

---

## 🛠️ Hızlı Başlangıç

### 1. Kurulum

```bash
pip install -e ".[dev]"
```

Düzenlenebilir kurulum, **bulunduğunuz klasörü** gösterir; projeyi taşırsanız yeniden çalıştırın (aksi halde paket başka klasörden `import` edilemez). LTspice farklı bir yerdeyse `LTSPICE_EXE` ortam değişkenini veya `LTSpiceAdapter(executable_path=...)` argümanını kullanın.

### 2. İnteraktif Tasarımcıyı Başlatma (CLI)

```bash
python interactive_designer.py     # veya kurulumdan sonra: ltspice-designer
```

### 3. Temel Simülasyon Örneği

```python
from ltspice_llm import LTSpiceAdapter

adapter = LTSpiceAdapter()

netlist = """* RC Alcak Geciren Filtre
V1 in 0 PULSE(0 5 0 1n 1n 1m 2m)
R1 in out 1k
C1 out 0 1u
.tran 5m
.meas tran v_max MAX V(out)
.meas tran v_tau FIND V(out) WHEN time=1m
.end
"""

result = adapter.run_netlist(netlist, circuit_name="rc_filter")

print(f"Simülasyon Başarılı: {result.success}")
print("Ölçümler (.meas):", result.measurements)
# Çıktı: {'v_max': 3.646, 'v_tau': 3.1606, 'v_tau_at': 0.001}

# LLM'e doğrudan verilebilecek JSON:
print(result.to_llm_dict())
```

`status` değerleri: `SUCCESS` (işlem başarılı), `FAILED` (simülasyon çalıştı ama başarısız oldu; ayrıntı `errors` ve `diagnostics` içinde), `NOT_CONVERGED` (optimizasyon hedefe ulaşamadı), `ERROR` (araç çağrısı yürütülemedi: eksik argüman, bulunamayan devre vb.). LLM'e giden yanıtta `warnings`, `failed_measurements` ve `parameter_changes` alanlarını da okuması gerekir.

---

## 🤖 LLM Ajanı Entegrasyonu (Function Calling)

```python
from ltspice_llm import get_ltspice_tool_schemas, handle_tool_call

tools = get_ltspice_tool_schemas("openai")      # "generic" | "openai" | "anthropic" | "gemini"

# LLM bir tool çağrısı ürettiğinde adaptöre iletin (argümanlar sözlük veya JSON metni olabilir):
response = handle_tool_call(
    "create_or_replace_circuit",
    {"circuit_name": "voltage_divider", "netlist_code": "...", "parameters": {"R1_VAL": "10k"}},
)
# Geri dönen JSON doğrudan LLM'e ToolMessage olarak iletilir.
```

### Sağlayıcıdan bağımsız araç döngüsü

`AutonomousCircuitAgent` hiçbir LLM sağlayıcısına kendisi bağlanmaz ve API anahtarı okumaz. Model çağrısını siz bir fonksiyon olarak verirsiniz; ajan araç çağrılarını yürütür, sonuçları geçmişe ekler ve model araç çağırmayı bırakana kadar döner:

```python
from ltspice_llm import AutonomousCircuitAgent

def llm(messages, tools):
    ...  # kendi istemcinizi çağırın; {"content": str|None, "tool_calls": [{"id", "name", "arguments"}]} döndürün

agent = AutonomousCircuitAgent()
outcome = agent.run("10 kHz alçak geçiren filtre tasarla", llm, max_steps=10)
# {"finished": True, "steps": 4, "final_message": "..."}
agent.tool_log          # yürütülen her aracın ad, argüman, durum ve süresi
agent.get_history()     # sağlayıcıdan bağımsız mesaj geçmişi (add_user_message / add_assistant_message / add_tool_result)
```

Çalışan bir örnek için bkz. [examples/08_agent_loop_with_scripted_llm.py](../examples/08_agent_loop_with_scripted_llm.py).

Geçmişi sağlayıcı biçimine çevirmek için `ltspice_llm.providers`: `to_anthropic` (`system`, `messages`), `to_openai` (`messages`), `to_gemini` (`system_instruction`, `contents`); yanıtı ajan biçimine `from_anthropic`, `from_openai`, `from_gemini` (sözlük ya da SDK nesnesi). Grafik araçlarının `image_base64`'ü geçmişe metin olarak girmez, mesajın `images` alanında durur ve dönüştürücüler onu sağlayıcının görsel bloğuna çevirir (Anthropic `image`, OpenAI araç mesajlarından sonra `image_url`, Gemini `inlineData`). Biçimler sağlayıcı belgelerinden; canlı API'yle otomatik test edilmez.

```python
from ltspice_llm.providers import from_anthropic, to_anthropic

def llm(history, _tools):
    system, messages = to_anthropic(history)
    response = client.messages.create(model=MODEL, max_tokens=4096, system=system, messages=messages,
                                      tools=get_ltspice_tool_schemas("anthropic", include_extended=True))
    return from_anthropic(response)
```

Genişletilmiş araçlar (`include_extended=True`): şema çizimi, iz parazitikleri, takma anı, SOA/derating, Monte Carlo ve birden çok parametreyi birden çok `.meas` hedefine Nelder-Mead ile ayarlayan `optimize_parameters`.

---

## 📈 Otonom Kapalı Devre Optimizasyon

```python
from ltspice_llm import CircuitOptimizer

res = CircuitOptimizer().optimize_single_parameter(
    base_netlist=netlist,
    circuit_name="rc_opt",
    param_name="R_VAL",          # .param adı veya R/C/L eleman adı
    target_meas_name="v_target",
    target_value=3.50,
    min_param=100.0,
    max_param=5000.0,
    tolerance=0.005,             # %0.5 hata toleransı
)

print(res.converged, res.best_parameters, res.best_value)
print(res.message)               # yakınsamadıysa nedeni (uçlar aynı değer, hedef aralık dışında, simülasyon başarısız...)
# Örnek 04: R_VAL = 827.34 Ω, Ölçülen: 3.5071 V
```

Aynı işlev LLM'e `optimize_parameter` aracı olarak sunulur. Ölçümün parametreyle **monoton** değiştiği varsayılır; tek parametre optimize edilir. Adım başına üretilen geçici dosyalar (`.net/.log/.raw/.db`) varsayılan olarak iş bitince silinir (`cleanup_intermediate=False` ile korunur) ve her çalıştırma benzersiz adlar kullanır.

---

## 🧹 Çalışma Alanı ve Eşzamanlılık

Dosyalar varsayılan olarak `%TEMP%/ltspice_llm_runs` altına `circuit_name`'den türetilen sabit adlarla yazılır (LTspice 26 devre başına bir `.db` önbellek dosyası da bırakır). Temizlemek için:

```python
adapter.remove_circuit("rc_filter")            # tek devrenin tüm dosyaları
adapter.cleanup_workspace(max_age_seconds=86400)   # 1 günden eski çıktılar (0: hepsi), run_batch'in _batch_* dizinleri dahil
adapter.cleanup_workspace(max_total_mb=500)        # en eski koşumlardan başlayarak 500 MB'a in
LTSpiceAdapter(workspace_quota_mb=500)             # her koşumdan sonra otomatik (varsayılan kapalı)
```

Kota, elde tutulan `SimulationResult`'ların dosyalarını silmez. Grafik araçları `.raw`'ı sonradan `circuit_name` ile
okuduğundan varsayılan kapalıdır. Ölçüm: kotasız varsayılan çalışma alanı 9 günde 3,16 GB'a çıktı (%99'u `.raw`).

Windows'ta LTspice süreçleri Python sürecine bağlı bir iş nesnesindedir: Python nasıl biterse bitsin LTspice de
sonlanır, çöken LTspice Windows Hata Bildirimi'ni beklemez ve çökme kodu hata olarak raporlanır
(bkz. [docs/LESSONS_LEARNED_PERFORMANCE.md §10](LESSONS_LEARNED_PERFORMANCE.md)).

Aynı `workspace_dir` içinde aynı `circuit_name`'i eşzamanlı kullanan iki çağrı birbirinin dosyalarını ezer; paralel iş akışlarında her biri için ayrı `workspace_dir` verin. `adapter.run_batch(...)` bunu kendisi yapar: her işe ayrı bir alt dizin açar.

LTspice'ı aynı anda birden fazla kez çalıştırmak (iş parçacıklarından, `run_batch` ile, birden fazla ajan ya da ayrı Python süreçlerinden) **güvenlidir**: kilitlenme koruması varsayılan olarak açıktır ve tüm koşumların çözücü iş parçacıklarını makine genelinde çekirdek sayısıyla sınırlar (bkz. [Performans → Kilitlenme koruması](#-performans)).

---

## ⚡ Performans

> Bu çalışmada öğrenilenler, kök neden analizi ve gelecekteki performans işleri için kontrol listesi:
> [docs/LESSONS_LEARNED_PERFORMANCE.md](LESSONS_LEARNED_PERFORMANCE.md).

Aşağıdaki sayıların tamamı bu depodaki kodla, LTspice 26.1.1 ve Windows 11 üzerinde **ölçülmüştür**.
Makine: Intel Core i7-1255U (2 P + 8 E çekirdek, 12 mantıksal işlemci, 15 W), "Balanced" güç planı, AC güç.
Referans devre: 500 kHz senkron buck dönüştürücü, 2 ms `.tran`, 20 ns maksimum adım. Paralellik
kazançları işlemciye çok bağlıdır; masaüstü/iş istasyonu işlemcilerinde daha yüksek olması beklenir.

### Ne gerçekten hızlandırıyor

| Müdahale | Ölçülen kazanç | Durum |
| --- | --- | --- |
| **İkili (binary) `.raw`** (varsayılan) | simülasyon **3,2x**, ayrıştırma **18x**, dosya **5,8x** küçük | Otomatik |
| **`.save` ile sinyal budama** | `.raw` **2,5x** küçük, ayrıştırma **2,4x** | `save_signals=[...]` |
| **`run_sweep`** (LTspice `.step`, tek süreç) | 8 noktalı taramada **1,4-1,9x** | `adapter.run_sweep(...)` |
| **`run_batch`** (çözücü iş parçacıkları otomatik sınırlı) | 12 bağımsız devrede **2,0-2,4x** | `adapter.run_batch(...)` |

Uçtan uca: 9 iterasyonluk gerçek bir optimizasyon döngüsü **20,62 sn / 176,9 MB**'tan
**7,76 sn / 6,2 MB**'a indi (**2,66x hızlı, 28,8x az disk**), bulunan optimum parametre aynı kaldı.

```python
# Parametre taraması: LTspice'in kendi .step'i tek süreçte koşar
res = adapter.run_sweep(netlist, parameter="Lval", values=["10u", "22u", "47u"],
                        save_signals=["V(vout)"])
print(res.step_statistics["vout_pp"])   # min / max / mean / std / range

# Birbirinden bağımsız devreler: eşzamanlı örnekler, çözücü iş parçacıkları otomatik sınırlı
results = adapter.run_batch([
    dict(netlist_content_or_path=net_a, circuit_name="a", treat_as_text=True),
    dict(netlist_content_or_path=net_b, circuit_name="b", treat_as_text=True),
])
```

### Paralel LTspice neden kilitleniyordu?

**Kök neden:** LTspice 26'nın çözücüsü (`Multi-threaded, self-authoring solver`) varsayılan olarak **tüm
mantıksal çekirdekler kadar** iş parçacığı açar (log: `Maximum thread count: 12`). Bu sayı süreç
affinity'sini ve makinedeki diğer yükü yok sayar. Birkaç LTspice örneği aynı anda çalışıp bu iş
parçacıkları aynı anda çekirdek bulamayınca çözücü **CPU yakarak ilerlemeden döner** (livelock): 3 çekirdeğe
sabitlenmiş bir örnek 60 sn'de 176 CPU-saniyesi harcayıp logunu bir satır bile ilerletmedi.

**Düzeltme:** belgelenmemiş `.options threads=N` direktifi çözücünün iş parçacığı sayısını sınırlar
(`.options numthreads` / `nthreads` ise sözdizimi hatasıdır; `OMP_NUM_THREADS` etkisizdir). `run_batch` her
işe `.options threads=çekirdek//örnek` ekler. Tek fark bu direktif olan nedensellik testi:

| Yapılandırma (iş başına normalde ~1 sn) | `threads` varsayılan (12) | `threads` sınırlı |
| --- | --- | --- |
| 8 serbest örnek | **1/24 başarılı**, 23'ü 40 sn'de öldürüldü | **24/24**, 8 iş ~3,6 sn (`threads=1`) |
| 4 örnek, her biri ayrı 3 çekirdeğe sabit | **0/12 başarılı** | **12/12** (`threads=3`) |
| `run_batch` 12 iş, 8 örnek | 5/12 başarılı, 0,27x | **36/36**, 2,1x |

**Kazancın tavanı donanımdır.** i7-1255U hibrit bir işlemcidir: aynı çözücü işi E-çekirdekte P-çekirdeğe
göre **1,95x** yavaş koşuyor ve tüm çekirdekler ortak 15 W bütçeyi paylaşıyor (10 çekirdek yüklüyken P-çekirdek
çözücü süresi 0,53 → 0,73-0,85 sn). Her işi ayrı bir fiziksel çekirdeğe sabitlemek bile teorik tavanı ~3,3x'e
çıkarıyor; `run_batch`'in 2,0-2,4x'i buna yakın.

| 12 işlik yük | 4 örnek × 3 thread | 6 × 2 | 10 × 1 | 12 × 1 |
| --- | --- | --- | --- | --- |
| Hızlanma (en kötü tekrar) | **2,39x** | 2,38x | 1,79x | 1,93x |

> Tek bir devrenin parametre taraması için `run_batch` değil **`run_sweep`** kullanın: LTspice'ın kendi
> `.step`'i tek süreçte koşar.

### Kilitlenme koruması

Bu kilitlenme yalnızca `run_batch`'te değil, LTspice aynı anda birden fazla kez çalıştığı **her durumda**
oluşur: kendi iş parçacıklarınızdan `run_netlist` çağırmak, birden fazla ajan, ayrı Python süreçleri. Bu
yüzden koruma adaptörün kendisinde ve varsayılan olarak açıktır (`cpu_guard=True`):

1. **Makine geneli çekirdek bütçesi.** Her koşum başlamadan önce bütçeden çekirdek kiralar ve LTspice'a
   `.options threads=<kiralanan>` verilir. Bütçe, her çekirdek için bir işletim sistemi dosya kilidiyle tutulur;
   dolayısıyla sınır farklı Python süreçleri arasında da geçerlidir. Süreç çökse ya da öldürülse bile kilitler
   işletim sistemi tarafından bırakılır, bütçe asılı kalmaz.
2. **Makineye göre boyut.** Bütçe sürecin gerçekten kullanabildiği çekirdek sayısıdır (affinity maskesi
   okunur; LTspice bunu kendisi yapmaz). `LTSPICE_LLM_CPU_BUDGET=6` gibi bir değerle bu yazılımın LTspice'a
   ayıracağı toplam çekirdek kısılabilir.
3. **Adil paylaşım.** Tek başına çalışan koşum tüm çekirdekleri alır (LTspice varsayılanıyla aynı hız). Sırasını
   beklemek zorunda kalan koşum makineyi kapmak yerine `çekirdek/4` pay ister; böylece çekişmede koşumlar
   güvenli biçimde paralel ilerler. Bütçe doluysa yeni koşum aşırı abone etmek yerine bekler
   (`slot_wait_timeout`, varsayılan `max(60, 2 × timeout)` sn; süre dolarsa açık bir hatayla döner).
4. **Takılma bekçisi.** Korumanın göremediği yükler için (LTspice arayüzünde açık bir simülasyon, başka
   araçlar): çözücünün `.log`/`.raw` dosyaları `stall_timeout` (varsayılan 20 sn) boyunca hiç ilerlemezse koşum
   durdurulur ve `threads=1` ile bir kez yeniden çalıştırılır. Normal koşumlarda bu dosyalar saniyenin altında
   aralıklarla büyür; livelock'ta ise hiç değişmez.

Kullanıcı netlistte `.options threads=N` verdiyse bu değer ezilmez; koşum N çekirdek (en çok sürecin kullanabildiği
kadar) boşalana kadar bekler. LTspice N'yi makinenin mantıksal çekirdek sayısına kırpar; `SimulationResult.solver_threads`
LTspice'ın kullandığı sayıyı bildirir.
Adaptörün eklediği satır `; ltspice_llm:auto-threads` ile işaretlenir, sonraki koşumda güncellenir ve
`get_circuit_netlist` çıktısında gösterilmez.

Gerçek LTspice ile ölçüm (2 ms buck, iş başına normalde ~1 sn, zaman aşımı 40 sn):

| Senaryo | Koruma kapalı | Koruma açık (varsayılan) |
| --- | --- | --- |
| 8 iş parçacığı, her biri ayrı adaptörle `run_netlist` | **1/8 başarılı**, 43 sn | **8/8**, 3,9-6,1 sn (sıralı: 6,7 sn) |
| 4 ayrı Python süreci × 3 eşzamanlı koşum | **0/12 başarılı**, 45 sn | **12/12**, ~10 sn |
| Yalnızca bekçi (koruma kapalı, `stall_timeout=5`) | — | **8/8**; 8 takılma yakalanıp `threads=1` ile kurtarıldı |
| Tek koşum ek maliyeti | 1,101 sn | 1,080 sn |

```python
LTSpiceAdapter()                                  # koruma + bekçi açık (önerilen)
LTSpiceAdapter(stall_timeout=120)                 # çalışma noktası uzun süren çok büyük devreler
LTSpiceAdapter(cpu_guard=False, stall_timeout=None)   # LTspice varsayılan davranışı (önerilmez)
```

### İşe yaramayan `.options` ayarları

Yaygın olarak önerilen bazı SPICE "hızlandırma" seçenekleri bu devrede ölçüldüğünde kazanç vermedi:

| Seçenek | İddia | Ölçülen |
| --- | --- | --- |
| `.options method=gear` | 2-3x hız | **1,07x** (gürültü seviyesi); kaba adımda rezonans genliğini hafif sönümler |
| `.options plotwinsize=0` | Sıkıştırmayı kapatıp hızlandırır | **1,02x**, `.raw` boyutu değişmedi |
| `.options reltol=0.005 trtol=7` | "Doğruluğu bozmadan" 2x | **1,10x**, üstelik ölçüm değerlerini değiştirir |

`method=gear` yine de **yakınsama** sorunlarında değerlidir (teşhis motoru bu amaçla önerir) — ama bir hız
ayarı değildir.

### Optimizasyon döngüsü neden paralelleştirilemiyor?

`CircuitOptimizer.optimize_single_parameter` **ikili arama** yapar: her adımın hangi noktayı deneyeceği bir
önceki adımın sonucuna bağlıdır. Bu yüzden iterasyonlar süreç havuzuna dağıtılamaz. Döngü bunun yerine
her iterasyonda `.meas` sinyalleri dışındaki her şeyi `.save` ile budar (`prune_signals=True`); bağımsız
noktaları aynı anda denemek gerekiyorsa `run_sweep` ile kaba bir tarama yapıp aralığı daraltın.

---

## 🧪 Testleri Çalıştırma

```bash
python -m pip install -e ".[dev]"          # pytest, hypothesis, pytest-timeout, ruff
python scripts/check.py                    # kalite kapısı: .asc bütünlüğü + ruff + LTspice'sız testler (CI de bunu koşar)
python scripts/check.py --full             # aynı kapı, testler LTspice ile (örnekler dahil)
python -m pytest                           # tüm testler; LTspice kurulu değilse ona bağlı olanlar atlanır
python -m pytest -m "not examples"         # LTspice'lı testler, örnekleri uçtan uca koşmadan
python scripts/mutants.py                  # test gücü: elle seçilmiş mutantların hepsi ölmeli
python scripts/flows.py [--full]           # use case senaryoları: coverage tanığı (gcov --gcov-check karşılığı)
python scripts/flows.py --uncovered        # hiçbir testin girmediği fonksiyonlar
python scripts/isolation.py [hedef ...]    # her test tek başına: önceki testin durumuna dayanan testleri bulur
```

Uyarılar hata sayılır ve tek test 120 sn'de kesilir (`pyproject.toml`). `tests/test_properties.py` Hypothesis
property testleridir; `HYPOTHESIS_PROFILE=ci` ile 1000 örnek dener.

| Dosya | LTspice gerekir mi? |
| --- | --- |
| `test_integration_ltspice.py`, `test_integration_reference.py`, `test_adapter.py` | Evet (kurulu değilse atlanır) |
| `test_examples.py` | Kısmen: içe aktarma ve rapor yolu LTspice'sız; örneklerin uçtan uca koşumu (`examples` işareti) LTspice ister |
| Diğerleri (`test_real_logs.py`, `test_parameters.py`, `test_tools_and_agent.py`, `test_visualizer_steps.py`, `test_adapter_platform.py`, `test_interactive_designer.py`, `test_regressions.py`, ...) | Hayır |

**Use case akışı.** `tests/usecases/*.json` netlist simülasyonu, tarama/toplu koşum ve ajan döngüsünün eylemlerini
belgeden (bu kılavuz, GEMINI.md) çıkarır ve her senaryoyu bir teste bağlar. Test koşarken `tests/flowtrace.py`
(`sys.setprofile`, gdb'nin durmayan breakpoint'lerinin karşılığı) `ltspice_llm` çağrılarını kaydeder ve diziyi,
yasak çağrıları ve çağrı sayılarını denetler. Hata ayıklama için gdb yerine `pytest --pdb`, `--trace` ya da
`breakpoint()`.

`tests/data/` altındaki loglar gerçek LTspice 26.1.1 çıktılarıdır; ayrıştırıcıyı sentetik biçimlere değil gerçek çıktıya karşı sınar.

---

## ⚠️ Bilinen Sınırlamalar

- `optimize_single_parameter` tek parametre ve monoton ölçüm içindir; ikili arama sırasal olduğu için iterasyonları paralelleştirilemez. Birden çok parametre ve hedef için `optimize_multi_parameters` (Nelder-Mead) kullanın.
- Paralellik ölçümleri tek bir makinede (i7-1255U, hibrit 2P+8E) yapılmıştır; kazanç işlemciye çok bağlıdır. `.options threads=N` LTspice'ta belgelenmemiş bir direktiftir; LTspice 26.1.1'de doğrulanmıştır, başka sürümlerde davranışı değişebilir.
- Kilitlenme koruması yalnızca bu kütüphane üzerinden başlatılan koşumları (aynı kullanıcı, aynı `LTSPICE_LLM_SLOT_DIR`) koordine eder. LTspice arayüzünde elle başlatılan simülasyonlar ya da başka araçların LTspice koşumları bütçeye dahil değildir; bunlara karşı yalnızca takılma bekçisi devrededir. `open_in_gui` ile açılan LTspice penceresinin iş parçacığı sayısı da kontrol edilemez.
- Takılma bekçisi ilerlemeyi dosya büyümesinden anlar. Çalışma noktası hesabı `stall_timeout`'tan uzun süren çok büyük bir devre yanlışlıkla takılmış sayılıp `threads=1` ile yeniden koşturulabilir (sonuç yine doğrudur, yalnızca yavaşlar); böyle devrelerde `stall_timeout` değerini artırın.
- `save_signals` ile budama yapıldığında `.meas` satırlarında geçen sinyaller otomatik korunur, ancak `.meas` dışında kalan bir sinyali sonradan çizdirmek isterseniz simülasyonu yeniden koşturmanız gerekir.
- AC `.meas` sonuçlarının birimi `.options meascplxfmt`'e bağlıdır: varsayılan (bode) biçimde `mag()` dB, polar/cartesian'da doğrusal döner. Biçimden bağımsız kesin değer için `read_measure_db(result.db_path)` kullanın.
- `inspect_schematic`/`schematic_to_netlist` yerel sembolleri bulabilmek için şemanın geçici bir kopyasını kendi klasörüne yazar ve hemen siler (LTspice 26.1.1'de `-I<yol>` anahtarı çalışmıyor). Klasör yazılamıyorsa kopya çalışma alanında çevrilir ve yalnızca standart semboller çözülür.
- AC `.raw` özeti yalnızca genlik (magnitude) verir. Steplenmiş `.raw` özeti ilk 3 adımla, grafikler ilk 8 adımla sınırlıdır.
- macOS (`LTspice.app`) ve `LTSPICE_EXE` ile Linux/Wine desteği koda eklenmiştir ancak bu ortamlarda denenmemiştir; testler yalnızca Windows + LTspice 26.1.1 üzerinde çalıştırılmıştır.
- Araç katmanı LLM'in netlistini simülasyondan önce `netlist.file_access_problems` ile denetler (ADI belgelerindeki dosya erişimleri): dosya yazan `.wave`, `.savebias`, `.savestate` ve `.tran ... savestate` reddedilir; `.include`/`.lib`/`.loadstate`, `file=` ve `wavefile=` yalnızca mutlak yol, `~`, `..` ya da ortam değişkeni (`%VAR%`; LTspice 26.1.1 `.include` yolunda açıyor, `examples/12` S3) gösteriyorsa reddedilir. Parametre değerleri uygulandıktan sonraki metin denetlenir. Girdi güvenilirse `handle_tool_call(..., allow_file_access=True)` ya da `AutonomousCircuitAgent(allow_file_access=True)`. `LTSpiceAdapter.run_netlist`'i doğrudan çağıran kod denetlenmez.
- Araç şemalarının OpenAI/Anthropic/Gemini biçimleri sağlayıcı dokümantasyonuna göre üretilir; canlı API'lerle otomatik test edilmez.
