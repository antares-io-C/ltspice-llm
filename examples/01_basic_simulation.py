"""
Örnek 1: Temel Simülasyon ve Ölçüm Alma (.meas)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Bir RC Alçak Geçiren Filtre devresini netlist olarak tanımlar,
LTspice'ta arka planda koşturur ve .meas sonuçlarını ekrana basar.
"""

import sys
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

import json
from ltspice_llm import LTSpiceAdapter


def main():
    print("--- 1. LTspice Adaptörü Başlatılıyor ---")
    adapter = LTSpiceAdapter()
    print(f"Tespit Edilen LTspice: {adapter.executable_path}")
    print(f"Çalışma Dizini: {adapter.workspace_dir}\n")

    # SPICE Netlist Tanımı
    rc_netlist = """* RC Alcak Geciren Filtre Testi
.param R_VAL=1k
.param C_VAL=1u

* Giris Kaynagi: 0V'dan 5V'a 1ms darbe
V1 in 0 PULSE(0 5 0 1n 1n 1m 2m)
R1 in out {R_VAL}
C1 out 0 {C_VAL}

* 5 milisaniyelik gecici rejim analizi
.tran 5m

* Olcum Direktifleri (.MEAS)
.meas tran v_max MAX V(out)
.meas tran v_tau FIND V(out) WHEN time=1m
.end
"""

    print("--- 2. Simülasyon Çalıştırılıyor ---")
    result = adapter.run_netlist(
        netlist_content_or_path=rc_netlist,
        circuit_name="rc_filter_demo",
    )

    print(f"Başarılı mı? : {result.success}")
    print(f"Süre         : {result.elapsed_seconds:.4f} saniye")
    print("\n--- 3. Ölçüm Sonuçları (.MEAS) ---")
    for k, v in result.measurements.items():
        print(f"  {k} = {v}")

    print("\n--- 4. LLM İçin Hazırlanan JSON Çıktısı ---")
    print(json.dumps(result.to_llm_dict(include_traces_summary=False), indent=2))


if __name__ == "__main__":
    main()
