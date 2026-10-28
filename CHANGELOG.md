# Değişiklik Günlüğü

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
