"""
Örnek 2: Dalga Formu Grafiği Çizme (PNG / Multimodal LLM Görseli)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Simülasyon sonucunda oluşan .raw dosyasındaki V(in) ve V(out) gerilimlerini
matplotlib ile yüksek çözünürlüklü PNG grafiğine dönüştürür.
"""

import sys
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from pathlib import Path
from ltspice_llm import LTSpiceAdapter, WaveformVisualizer


def main():
    adapter = LTSpiceAdapter()

    netlist = """* RC Alcak Geciren Filtre Grafigi
V1 in 0 PULSE(0 5 0 10u 10u 1m 2m)
R1 in out 2.2k
C1 out 0 470n
.tran 4m
.end
"""

    print("Devre koşturuluyor...")
    result = adapter.run_netlist(netlist, circuit_name="rc_plot_demo")

    if not result.success or not result.raw_path:
        print("Hata: Simülasyon tamamlanamadı!")
        return

    output_png = Path(adapter.workspace_dir) / "rc_plot_demo.png"
    print(f"Grafik çiziliyor: {output_png}")

    saved_path = WaveformVisualizer.plot_traces(
        raw_path=result.raw_path,
        signals=["V(in)", "V(out)"],
        title="RC Filtre Giriş ve Çıkış Yanıtı (2.2kΩ / 470nF)",
        output_png_path=output_png,
    )

    print(f"Grafik kaydedildi: {saved_path}")
    print(f"Dosya boyutu: {saved_path.stat().st_size} bayt")


if __name__ == "__main__":
    main()
