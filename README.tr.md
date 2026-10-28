<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">
    <img src="docs/assets/logo-light.svg" alt="ltspice-llm" height="72">
  </picture>
</h1>

<p align="center"><b>Türkçe</b> · <a href="README.md">English</a></p>

<p align="center"><b>Dil modeliniz LTspice'ı çalıştırsın.</b></p>

[LTspice](https://www.analog.com/en/resources/design-tools-and-calculators/ltspice-simulator.html), Analog Devices'ın
ücretsiz devre simülatörüdür; grafik arayüz için tasarlanmıştır ve `.log`/`.raw` çıktıları bir dil modelinin
okuyamayacağı kadar büyük ve hamdır. **ltspice-llm** onu arka planda çalıştırır; `.meas` ölçümlerini, çalışma noktasını
ve hataları okur, her dalga formunu kısa bir JSON özete çevirir (min, max, ortalama, RMS ve 15 noktalı trend). Hazır
araç şemalarıyla bir model devre kurar, simüle eder, sayıları okur ve değerleri değiştirir; kütüphane hiçbir model
sağlayıcısına kendisi bağlanmaz.

```text
LLM ya da Python ──(devre)──► ltspice-llm ──► LTspice (arka planda)
        ▲                                         │
        └──────── kısa özet (JSON) ◄──────────────┘
```

## Neler yapabilirsiniz?

- **Netlisti simüle edip sayıları alın.** LTspice arayüzsüz çalışır; `.meas` sonuçları, çalışma noktası ve hatalar
  Python sözlüğü olarak döner. [examples/01_basic_simulation.py](examples/01_basic_simulation.py)
- **Dalga formu çizin.** `.tran` izlerini PNG olarak kaydedin; çizim araçları görüntüyü çok modlu modeller için Base64
  olarak da döndürebilir. [examples/02_waveform_plotting.py](examples/02_waveform_plotting.py)
- **LTspice'ı modele araç olarak verin.** OpenAI, Anthropic, Gemini ya da genel biçimde 10 araç şeması;
  `handle_tool_call` modelin araç çağrılarını çalıştırır. [examples/03_llm_function_calling_demo.py](examples/03_llm_function_calling_demo.py)
- **Araç döngüsünü yazmadan çalıştırın.** `AutonomousCircuitAgent` model fonksiyonunuzu çağırır, istediği araçları
  çalıştırır ve model araç çağırmayı bırakınca durur. Örnekte model bir betikle taklit edilir, API anahtarı gerekmez.
  [examples/08_agent_loop_with_scripted_llm.py](examples/08_agent_loop_with_scripted_llm.py)
- **Bir değeri hedefe göre ayarlayın.** Tek parametrede ikili arama; `.meas` sonucu hedefe tolerans içinde yaklaşana
  kadar. [examples/04_closed_loop_tuning.py](examples/04_closed_loop_tuning.py)
- **Bir filtre tasarımını araçlarla izleyin.** 10 kHz Sallen-Key alçak geçiren: AC analizi ve −3 dB noktası, Bode
  grafiği, basamak yanıtı. Araç çağrıları betikte sabittir.
  [examples/05_filter_design_walkthrough.py](examples/05_filter_design_walkthrough.py)
- **Standart değere yuvarlayın, sıcaklık taraması yapın, hatayı teşhis edin.** E24 yuvarlama, her ölçüm için
  min/max/ortalama/std veren `.step temp` taraması ve bilerek bozulmuş bir devrenin teşhisi.
  [examples/07_quantization_sweep_and_diagnosis.py](examples/07_quantization_sweep_and_diagnosis.py)
- **Tarama ve paralel koşum.** `run_sweep` parametre taramasını tek bir LTspice `.step` koşumunda yapar; `run_batch`
  bağımsız devreleri aynı anda koşar ve örnekler kilitlenmesin diye çözücü iş parçacıklarını sınırlar.
  [examples/10_performance_fast_paths.py](examples/10_performance_fast_paths.py)
- **Bir formülü LTspice ile sınayıp rapor yazın.** Dalga formları, ölçüm tabloları ve güvenilirlik denetimleriyle tek
  dosyalık HTML rapor. [examples/15_hotplug_damping_validation.py](examples/15_hotplug_damping_validation.py)

Ayrıca: `.asc` şemalarını okuma, değiştirme, üretme ve bağlantısız pinleri bulma (bkz. [docs/KILAVUZ.md](docs/KILAVUZ.md)).

## Hızlı başlangıç

Gerekenler: Windows, Python 3.10 ya da üstü ve ayrıca kurulmuş bir LTspice
([Analog Devices'tan indirin](https://www.analog.com/en/resources/design-tools-and-calculators/ltspice-simulator.html)).

```bash
git clone https://github.com/antares-io-C/ltspice-llm.git
cd ltspice-llm
pip install -e .
```

Bir RC alçak geçiren filtreyi simüle edip ölçümlerini okuyun:

```python
from ltspice_llm import LTSpiceAdapter

netlist = """* RC Alcak Geciren Filtre
V1 in 0 PULSE(0 5 0 1n 1n 1m 2m)
R1 in out 1k
C1 out 0 1u
.tran 5m
.meas tran v_max MAX V(out)
.meas tran v_tau FIND V(out) WHEN time=1m
.end
"""

result = LTSpiceAdapter().run_netlist(netlist, circuit_name="rc_filter")
print(result.success)        # True
print(result.measurements)   # {'v_max': 3.64623451233, 'v_tau': 3.16060549511, 'v_tau_at': 0.001}
print(result.to_llm_dict())  # dil modeline verilecek kısa özet
```

`v_tau`, basamaktan bir zaman sabiti (RC = 1 ms) sonraki çıkıştır: 5 V × (1 − 1/e) ≈ 3,16 V.

Bir modelin araçları nasıl kullandığını görmek için ajan döngüsünü betikli bir modelle çalıştırın (API anahtarı
gerekmez):

```bash
python examples/08_agent_loop_with_scripted_llm.py
```

LTspice bilinen kurulum konumlarında kendiliğinden bulunur; başka bir yerdeyse yolunu `LTSPICE_EXE` ortam değişkenine
yazın.

## Yapay zekâ ile kullanım

```python
from ltspice_llm import get_ltspice_tool_schemas, handle_tool_call

tools = get_ltspice_tool_schemas("anthropic")   # "openai" | "anthropic" | "gemini" | "generic"

# Modele tools'u verin. Model bir araç çağırdığında adını ve argümanlarını iletin:
result = handle_tool_call("create_or_replace_circuit",
                          {"circuit_name": "rc_filter", "netlist_code": netlist})
# result bir sözlüktür; JSON'a çevirip modele araç sonucu olarak geri verin.
```

Döngüyü kendiniz yazmak istemezseniz `AutonomousCircuitAgent` modelinizi çağırır, istediği araçları çalıştırır ve
model bitene kadar devam eder. Hiçbir sağlayıcıya kendisi bağlanmaz, API anahtarı okumaz; modeli siz bir fonksiyon
olarak verirsiniz. Bkz. [examples/08_agent_loop_with_scripted_llm.py](examples/08_agent_loop_with_scripted_llm.py).

## Örnekler

Çalıştırılabilir 14 örneğin hepsi [examples/](examples/) klasöründe. Etkileşimli bir terminal arayüzü de var:
`python interactive_designer.py`.

## Proje yapısı

```text
ltspice_llm/   kütüphane: LTspice adaptörü, çıktı ayrıştırıcıları, LLM araçları, teşhis, rapor
examples/      çalıştırılabilir örnekler
docs/          ayrıntılı kullanım kılavuzu ve ölçümle doğrulanmış dersler
tests/         testler (çoğu LTspice olmadan koşar)
scripts/       kalite kapısı ve test araçları
```

## Testler

```bash
pip install -e ".[dev]"
python scripts/check.py          # lint + LTspice gerektirmeyen testler
python scripts/check.py --full   # testler ve örnekler gerçek LTspice ile
```

## Sınırlamalar

- Simülasyonlar yalnızca Windows'ta LTspice 26.1.1 ile koşuldu. Kod macOS'ta da LTspice'ı arar ve Linux'ta
  `LTSPICE_EXE` ile bir Wine sarmalayıcısını kabul eder, ama ikisi de denenmedi. CI yalnızca LTspice gerektirmeyen
  testleri koşar (Ubuntu ve Windows; Python 3.10, 3.12 ve 3.14).
- Araç katmanı (`handle_tool_call`, `AutonomousCircuitAgent`) dil modelinin netlistinde dosya yazan direktifleri
  (`.wave`, `.savebias`, `savestate`) ve çalışma alanı dışını okuyan yolları (mutlak yol, `..`, `%USERPROFILE%` gibi
  ortam değişkenleri) reddeder; kütüphane adları (`.lib standard.dio`) serbesttir. Girdi güvenilirse
  `allow_file_access=True`. Doğrudan `LTSpiceAdapter.run_netlist` çağıran kod denetlenmez.
- Sağlayıcı şema biçimleri belgelerine göre üretilir; canlı API'lerle otomatik test edilmez.

## Daha fazlası

- [docs/KILAVUZ.md](docs/KILAVUZ.md): tüm özellikler, API ayrıntıları, performans ölçümleri, kilitlenme koruması ve
  bilinen sınırlamaların tamamı
- [docs/](docs/): LTspice ve performans üzerine ölçümle doğrulanmış dersler
- [CHANGELOG.md](CHANGELOG.md): sürüm geçmişi
- [CONTRIBUTING.md](CONTRIBUTING.md): katkı ve kalite kapısı

## Lisans

GPL-3.0-or-later; metni [LICENSE](LICENSE) dosyasında. Telif hakkı © 2026 ltspice-llm contributors.

LTspice, Analog Devices, Inc.'in tescilli markasıdır. Bu proje Analog Devices ile bağlantılı değildir ve onun
tarafından desteklenmez. LTspice'ı ayrıca kurmanız gerekir; bu depo LTspice'ı dağıtmaz.
