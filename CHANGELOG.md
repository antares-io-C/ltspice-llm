# Değişiklik Günlüğü

## Yayımlanmadı

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
