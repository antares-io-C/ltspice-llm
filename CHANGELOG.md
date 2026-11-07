# Değişiklik Günlüğü

## Yayımlanmadı

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
