"""
Örnek 4: Otonom Kapalı Devre Parametre Optimizasyonu (Closed-Loop Tuning)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
LLM veya otonom ajanın belirli bir hedefe (.meas değerine) ulaşmak için
devre parametrelerini iteratif olarak simüle edip optimize ettiği senaryo.
"""

import sys
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from ltspice_llm import CircuitOptimizer, LTSpiceAdapter


def main():
    print("=========================================================")
    print("  Otonom Devre Optimizasyonu (Closed-Loop Tuning) Demo  ")
    print("=========================================================\n")

    adapter = LTSpiceAdapter()
    optimizer = CircuitOptimizer(adapter=adapter)

    # Parametrik RC Devresi
    base_netlist = """* Parametrik RC Gecikme Devresi
.param R_VAL=2k

V1 in 0 PULSE(0 5 0 1n 1n 2m 4m)
R1 in out {R_VAL}
C1 out 0 1u

.tran 2m
.meas tran v_target FIND V(out) WHEN time=1m
.end
"""

    target_voltage = 3.50  # 1 milisaniyede çıkışın tam 3.50V olmasını istiyoruz
    print(f"Hedef: t = 1ms anında V(out) geriliminin {target_voltage:.2f}V olması.")
    print("Optimizasyon başlatılıyor (R_VAL: 100Ω ile 5kΩ arasında aranıyor)...\n")

    res = optimizer.optimize_single_parameter(
        base_netlist=base_netlist,
        circuit_name="rc_tuning_demo",
        param_name="R_VAL",
        target_meas_name="v_target",
        target_value=target_voltage,
        min_param=100.0,
        max_param=5000.0,
        tolerance=0.005,  # %0.5 tolerans
        max_iterations=12,
    )

    print(f"{'İterasyon':<10} {'R_VAL (Ω)':<15} {'Ölçülen (V)':<15} {'Hata (V)':<15}")
    print("-" * 55)
    for step in res.history:
        r_val = step.parameters.get("R_VAL", 0)
        meas_val = step.measurements.get("v_target", 0)
        print(f"{step.iteration:<10} {r_val:<15.2f} {meas_val:<15.4f} {step.error:<15.4f}")

    print("\n--- Optimizasyon Özeti ---")
    print(f"Hedefe Ulaşıldı mı? : {'EVET (Yakınsadı)' if res.converged else 'HAYIR'}")
    print(f"Toplam İterasyon    : {res.total_iterations}")
    print(f"En İyi Parametre    : R_VAL = {res.best_parameters.get('R_VAL', 0):.2f} Ω")
    print(f"Elde Edilen Değer   : {res.best_value:.4f} V (Hedef: {res.target_value} V)")
    print(f"Son Mutlak Hata     : {res.final_error:.6f} V")


if __name__ == "__main__":
    main()
