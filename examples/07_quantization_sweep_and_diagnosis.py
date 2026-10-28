"""
Örnek 7: Standart E Serisi Kuantalama, Sıcaklık Taraması ve Otomatik Teşhis
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Bu örnekte:
1. LLM'in teorik olarak hesapladığı değerler (15.915k, 1.414n) standart E24 serisine yuvarlanır.
2. Sıcaklık taraması (.step temp -40 85 62.5) koşturularak filtrenin termal kararlılığı test edilir.
3. Stepped ölçüm tablosu ayrıştırılarak istatistiki metrikler (min, max, std, mean) hesaplanır.
4. Kasıtlı olarak yakınsama hatası (floating node / singular matrix) olan bir devre teşhis edilir
   ve otomatik stabilizasyon yaması uygulanır.
"""

import sys
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from ltspice_llm import (
    LTSpiceAdapter,
    quantize_circuit_parameters,
)


def main():
    print("=================================================================")
    print("  Gelişmiş EDA: Kuantalama, Sıcaklık Taraması ve Hata Teşhisi   ")
    print("=================================================================\n")

    adapter = LTSpiceAdapter()

    # 1. Aşama: Teorik Değerleri Standart E24 Serisine Kuantalama
    print("1. AŞAMA: E24 Komponent Kuantalama (Snapping)")
    print("-------------------------------------------------------------")
    theoretical_params = {
        "R1": "15.915k",
        "R2": "15.915k",
        "C1": "1.414n",
        "C2": "0.707n",
    }
    print(f"Teorik Değerler   : {theoretical_params}")
    quantized_res = quantize_circuit_parameters(theoretical_params, series="E24")
    print(f"E24 Standart BOM  : {quantized_res['quantized_parameters']}")
    print(f"Maksimum Hata     : %{quantized_res['max_abs_error_pct']:.2f}\n")

    # 2. Aşama: Sıcaklık Taraması (.step temp)
    print("2. AŞAMA: Sıcaklık Taraması (.step temp -40°C .. +85°C)")
    print("-------------------------------------------------------------")
    thermal_netlist = f"""* Sallen-Key Lowpass Filter with Temperature Sweep
.param R1={quantized_res['quantized_parameters']['R1']}
.param R2={quantized_res['quantized_parameters']['R2']}
.param C1={quantized_res['quantized_parameters']['C1']}
.param C2={quantized_res['quantized_parameters']['C2']}

V1 in 0 AC 1 0
* tc: direncin sicaklik katsayisi (1/°C). Bilesenlerde tc tanimli olmazsa sicaklik taramasi sonucu degistirmez.
R1 in n1 {{R1}} tc=3e-3
R2 n1 inp {{R2}} tc=3e-3
C1 n1 out {{C1}}
C2 inp 0 {{C2}}
E1 out 0 inp out 100000

.ac dec 25 1k 100k
.step temp -40 85 62.5
.meas ac f_3db WHEN mag(V(out))=0.7071
.meas ac max_gain MAX mag(V(out))
.end
"""
    res = adapter.run_netlist(thermal_netlist, circuit_name="sallen_key_thermal", treat_as_text=True)
    print(f"Simülasyon Durumu : {'BAŞARILI' if res.success else 'BAŞARISIZ'}")
    if res.warnings:
        print(f"Uyarılar          : {res.warnings}")
    if res.step_statistics:
        print("Sıcaklık İstatistikleri (.step statistics):")
        for meas, stats in res.step_statistics.items():
            print(f"  • {meas}: Min={stats['min']}, Max={stats['max']}, Ort={stats['mean']}, Std={stats['std']}")
    if res.stepped_measurements:
        print("\nAdım Detayları (her adımın sıcaklığı 'temp' sütunundadır):")
        for meas, rows in res.stepped_measurements.items():
            for row in rows:
                print(f"  [{meas}] adım {row['step']}: T={row.get('temp')}°C, değer={row[meas]}")

    # 3. Aşama: Hata Teşhisi ve Otomatik Yama
    print("\n3. AŞAMA: SPICE Yakınsama Hataları Teşhis Motoru")
    print("-------------------------------------------------------------")
    broken_netlist = """* Intentionally Broken Circuit (Parallel Voltage Sources Loop)
V1 n1 0 5V
V2 n1 0 10V
R1 n1 0 1k
.op
.end
"""
    broken_res = adapter.run_netlist(broken_netlist, circuit_name="broken_circuit", treat_as_text=True)
    print(f"Beklenen Simülasyon Hatası: {'Hata Tespit Edildi' if not broken_res.success else 'Başarılı'}")
    if broken_res.diagnostics:
        print(f"Teşhis Edilen Sorun       : {broken_res.diagnostics['issue_type']}")
        print(f"Kök Neden Açıklaması      : {broken_res.diagnostics['root_cause']}")
        print(f"Önerilen SPICE Seçenekleri: {broken_res.diagnostics['suggested_options']}")

    print("\nİşlemler başarıyla tamamlandı!")


if __name__ == "__main__":
    main()
