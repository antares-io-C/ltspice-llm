"""
Örnek 10: Hızlı Yollar — Sinyal Budama, Yerel `.step` Taraması ve Sınırlı Eşzamanlılık
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Bu örnek, `docs/KILAVUZ.md`'nin *Performans* bölümündeki ölçümleri kendi makinenizde tekrarlar:

1. `save_signals` ile `.raw` dosyasını yalnızca ilgilenilen sinyallere budamak.
2. Bir parametre taramasını `run_sweep` ile LTspice'ın kendi `.step` direktifiyle **tek süreçte**
   koşturmak (sıralı N çağrıya göre daha hızlı).
3. Birbirinden bağımsız devreleri `run_batch` ile eşzamanlı koşturmak. `run_batch` her işe
   `.options threads=çekirdek//örnek` ekler: LTspice çözücüsü varsayılan olarak tüm çekirdekler
   kadar iş parçacığı açtığı için, bu sınır olmadan eşzamanlı örnekler birbirini kilitler.

Not: Tek bir devrenin parametre taraması için `run_batch` değil `run_sweep` kullanın.
"""

import sys
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

import time

from ltspice_llm import LTSpiceAdapter

BUCK = """* Senkron Buck 12V -> 5V, 500kHz
.param Lval=22u
V1 vin 0 12
V2 vg 0 PULSE(0 10 0 5n 5n 833n 2u)
S1 vin vsw vg 0 SWMOD
D1 0 vsw DFWD
L1 vsw vout {Lval}
C1 vout 0 100u Rser=10m
R1 vout 0 2.5
R2 vsw 0 1Meg
.model SWMOD SW(Ron=10m Roff=1Meg Vt=5 Vh=0.1)
.model DFWD D(Ron=20m Roff=1Meg Vfwd=0.4)
.tran 0 2m 0 20n
.meas tran vout_avg AVG V(vout) FROM 1.5m TO 2m
.meas tran vout_pp PP V(vout) FROM 1.5m TO 2m
.end
"""

L_VALUES = ["10u", "15u", "22u", "33u", "47u", "68u", "100u", "150u"]


def _mb(result):
    return result.raw_path.stat().st_size / 1e6 if result.raw_path and result.raw_path.exists() else 0.0


def main():
    adapter = LTSpiceAdapter()
    print("=================================================================")
    print("  Hızlı Yollar: budama, yerel .step taraması, sınırlı eşzamanlılık")
    print("=================================================================\n")

    # 1. AŞAMA -------------------------------------------------------------- sinyal budama
    print("1. AŞAMA: .save ile sinyal budama")
    print("-------------------------------------------------------------")
    t0 = time.perf_counter()
    full = adapter.run_netlist(BUCK, circuit_name="perf_full", treat_as_text=True, summarize_raw=False)
    t_full = time.perf_counter() - t0

    t0 = time.perf_counter()
    # .meas satırlarındaki sinyaller (V(vout)) otomatik eklenir; ölçümler bozulmaz.
    pruned = adapter.run_netlist(BUCK, circuit_name="perf_pruned", treat_as_text=True,
                                 summarize_raw=False, save_signals=["V(vsw)"])
    t_pruned = time.perf_counter() - t0

    print(f"Budamasız         : {t_full:5.2f} sn, .raw {_mb(full):6.2f} MB")
    print(f"save_signals ile  : {t_pruned:5.2f} sn, .raw {_mb(pruned):6.2f} MB")
    print(f"Dosya boyutu      : {_mb(full) / max(_mb(pruned), 1e-9):.1f}x küçük")
    print(f"Ölçümler korundu  : {full.measurements == pruned.measurements}\n")

    # 2. AŞAMA ------------------------------------------------- yerel .step vs sıralı çağrı
    print(f"2. AŞAMA: {len(L_VALUES)} noktalı Lval taraması")
    print("-------------------------------------------------------------")
    t0 = time.perf_counter()
    sequential = []
    for i, value in enumerate(L_VALUES):
        res = adapter.run_netlist(BUCK, circuit_name=f"perf_seq_{i}", parameters={"Lval": value},
                                  treat_as_text=True, summarize_raw=False, save_signals=["V(vout)"])
        sequential.append(res.measurements.get("vout_pp"))
    t_seq = time.perf_counter() - t0

    t0 = time.perf_counter()
    swept = adapter.run_sweep(BUCK, parameter="Lval", values=L_VALUES, circuit_name="perf_sweep",
                              treat_as_text=True, save_signals=["V(vout)"])
    t_sweep = time.perf_counter() - t0
    stepped = [row.get("vout_pp") for row in swept.stepped_measurements.get("vout_pp", [])]

    print(f"Sıralı {len(L_VALUES)} çağrı   : {t_seq:5.2f} sn")
    print(f"run_sweep (1 süreç): {t_sweep:5.2f} sn  ->  {t_seq / max(t_sweep, 1e-9):.2f}x hızlı")
    print(f"Aynı sonuçlar mı   : {[f'{float(v):.5g}' for v in sequential] == [f'{float(v):.5g}' for v in stepped]}")
    stats = swept.step_statistics.get("vout_pp", {})
    if stats:
        print(f"Ripple istatistiği : min={stats['min']:.4g} max={stats['max']:.4g} ort={stats['mean']:.4g}\n")

    # 3. AŞAMA ------------------------------------------------------- sınırlı eşzamanlılık
    print("3. AŞAMA: Bağımsız devreleri run_batch ile koşturma")
    print("-------------------------------------------------------------")
    loads = ["1", "2.5", "5", "10"]
    jobs = [dict(netlist_content_or_path=BUCK.replace("R1 vout 0 2.5", f"R1 vout 0 {load}"),
                 circuit_name=f"perf_load_{i}", treat_as_text=True,
                 summarize_raw=False, save_signals=["V(vout)"])
            for i, load in enumerate(loads)]

    t0 = time.perf_counter()
    for i, job in enumerate(jobs):
        adapter.run_netlist(job["netlist_content_or_path"], circuit_name=f"perf_bseq_{i}",
                            treat_as_text=True, summarize_raw=False, save_signals=["V(vout)"])
    t_bseq = time.perf_counter() - t0

    t0 = time.perf_counter()
    batched = adapter.run_batch(jobs)
    t_batch = time.perf_counter() - t0

    workers = adapter.default_workers()
    print(f"Eşzamanlı örnek    : {workers} (örnek başına .options threads={adapter.threads_per_worker(workers)})")
    print(f"Sıralı {len(loads)} devre    : {t_bseq:5.2f} sn")
    print(f"run_batch          : {t_batch:5.2f} sn  ->  {t_bseq / max(t_batch, 1e-9):.2f}x hızlı")
    print(f"Hepsi başarılı mı  : {all(r.success for r in batched)}")
    for load, res in zip(loads, batched):
        print(f"  • R1={load:>4} Ω  ->  ripple {res.measurements.get('vout_pp')}")

    # Temizlik
    for name in ["perf_full", "perf_pruned", "perf_sweep"] + [f"perf_seq_{i}" for i in range(len(L_VALUES))] \
            + [f"perf_bseq_{i}" for i in range(len(loads))]:
        adapter.remove_circuit(name)


if __name__ == "__main__":
    main()
