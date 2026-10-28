"""
Örnek 15: `analyze_hotplug_transient`'in iddialarını LTspice ile ölçer ve rapor yazar.

1. Kapalı form tepe formülü (seri RLC adım yanıtı) LTspice'ın ölçtüğü tepeyle uyuşuyor mu?
2. Aracın paralel sönüm önerisi (C_b = 4·C_in, ideal tepeyi en aza indiren ESR) için öngördüğü tepe LTspice'la aynı mı,
   farklı ölçekte de mi? AN-88 s.4'ün 47 µF / 0.44 Ω örneği ideal modelde ne veriyor (donanımda 25 V / 24 V)?
3. Kritik sönüm için önerilen toplam seri direnç (2·Z0) aşımı gerçekten kaldırıyor mu?

Bobinlere `Rser=0` yazılır: LTspice'ın varsayılan bobin seri direnci 1 mΩ'dur (ADI CIRCUIT-ELEMENTS-REFERENCE: "1m (for
SMPS; set Rser=0 to disable)"); yazılmazsa ideal modelle karşılaştırma %0.1-0.4 kayar (ölçüldü). Rapordaki her ölçüm
.meas'ten gelir; kaynak sayılar (AN-88 tabloları) ayrıca belirtilir. Rapor `reports/` altına yazılır, git'e girmez:

    python examples/15_hotplug_damping_validation.py [--hizli]    # --hizli: Gear çapraz kontrolü yok
"""

import math
import re
import sys
import time
from pathlib import Path

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ltspice_llm import LTSpiceAdapter  # noqa: E402
from ltspice_llm.netlist import insert_before_end  # noqa: E402
from ltspice_llm.parasitics import analyze_hotplug_transient, get_cable_parasitics, hotplug_step_peak  # noqa: E402
from ltspice_llm.parser import LogParser  # noqa: E402
from ltspice_llm.report import (  # noqa: E402
    Column, Effect, Finding, LineChart, SimulationReport, Study, Table, Trace, format_value, read_trace,
)
from study_helpers import report_path, save_report  # noqa: E402

# Varsayım (girdi): AN-88 s.1 toplu ESR'nin yalnızca adaptör çıkış kondansatörü payını verir (10-30 mΩ); kablo ve
# L_OUT direnci belirtilmemiş. Ortadaki 20 mΩ alındı.
R_AN88 = 0.02
L_1M = get_cable_parasitics(1.0).l_cable   # 1 m kablo, SLOD006 üst ucu: 2 * 100 cm * 12 nH/cm
REC_AN88 = analyze_hotplug_transient(24.0, 22e-6, 1e-6, 0.0)
REC_CABLE = analyze_hotplug_transient(12.0, 10e-6, L_1M, 0.0)
Z0_CABLE = REC_CABLE.z0

# ad: (etiket, V, L, C, R, C_b, ESR_b)
CASES = {
    "f_test": ("5 V · 0.5 µH · 1 µF · 0.15 Ω", 5.0, 0.5e-6, 1e-6, 0.15, None, None),
    "f_an88": ("24 V · 1 µH · 22 µF · 20 mΩ (AN-88 düzeni)", 24.0, 1e-6, 22e-6, R_AN88, None, None),
    "f_cable": ("12 V · 1 m kablo · 10 µF · 0 Ω", 12.0, L_1M, 10e-6, 0.0, None, None),
}
DAMP = {
    "an88_none": ("AN-88 düzeni, sönümsüz (R = 0)", 24.0, 1e-6, 22e-6, 0.0, None, None),
    "an88_47u": ("AN-88 düzeni + 47 µF / 0.44 Ω (AN-88 s.4)", 24.0, 1e-6, 22e-6, 0.0, 47e-6, 0.44),
    "an88_rec": ("AN-88 düzeni + araç önerisi", 24.0, 1e-6, 22e-6, 0.0,
                 REC_AN88.recommended_bulk_c, REC_AN88.recommended_bulk_esr),
    "cable_rec": ("12 V · 1 m kablo · 10 µF + araç önerisi", 12.0, L_1M, 10e-6, 0.0,
                  REC_CABLE.recommended_bulk_c, REC_CABLE.recommended_bulk_esr),
}
SERIES = {
    "r0": ("R = 0", 12.0, L_1M, 10e-6, 0.0, None, None),
    "r_z0": ("R = Z0 (zeta 0.5)", 12.0, L_1M, 10e-6, Z0_CABLE, None, None),
    "r_2z0": ("R = 2·Z0 (zeta 1, öneri)", 12.0, L_1M, 10e-6, 2 * Z0_CABLE, None, None),
}


def netlist(v, l_h, c_f, r, cb=None, rb=None, periods=3):
    """1 ns kenarlı basamak -> R -> L (Rser=0) -> C, isteğe bağlı paralel C_b + ESR. PULSE kenarları açık."""
    period = 2 * math.pi * math.sqrt(l_h * (c_f + (cb or 0.0)))
    t_end = periods * period
    lines = ["* hotplug", f"V1 src 0 PULSE(0 {v} 0 1n 1n 1 2)"]
    node = "src"
    if r > 0:
        lines.append(f"R1 src a {r:.6g}")
        node = "a"
    lines += [f"L1 {node} c {l_h:.6g} Rser=0", f"C1 c 0 {c_f:.6g}"]
    if cb:
        lines += [f"C2 c nb {cb:.6g}", f"R2 nb 0 {rb:.6g}"]
    lines += [f".tran 0 {t_end:.6g} 0 {period / 2000:.6g}", ".meas tran vpk MAX V(c)",
              f".meas tran vend FIND V(c) AT {t_end:.6g}", ".end"]
    return "\n".join(lines) + "\n"


# T4: normalize devre (L = C_in = 1 µ, Z0 = 1 Ω, V_in = 1 V, seri R = 0); tek koşumda iç içe .step
RATIOS = (2.136, 4.0, 6.0)
ESR_GRID = tuple(round(0.5 + 0.05 * i, 2) for i in range(15))  # 0.50 ... 1.20 · Z0


def sweep_netlist():
    period = 2 * math.pi * math.sqrt(1e-6 * 1e-6 * (1 + max(RATIOS)))
    return (f"* bulk sweep\n.param n=4 k=1\nV1 src 0 PULSE(0 1 0 1n 1n 1 2)\nL1 src c 1u Rser=0\nC1 c 0 1u\n"
            f"C2 c nb {{n*1u}}\nR2 nb 0 {{k}}\n.tran 0 {10 * period:.6g} 0 {period / 4000:.6g}\n"
            f".step param n list {' '.join(map(str, RATIOS))}\n.step param k list {' '.join(map(str, ESR_GRID))}\n"
            ".meas tran vpk MAX V(c)\n.end\n")


def run_all(adapter, gear):
    jobs = ([("formul", k, netlist(*v[1:])) for k, v in CASES.items()]
            + [("sonum", k, netlist(*v[1:], periods=20)) for k, v in DAMP.items()]
            + [("seri", k, netlist(*v[1:], periods=20)) for k, v in SERIES.items()])
    if gear:
        jobs += [("gear:" + g, k, insert_before_end(n, [".options method=gear"])) for g, k, n in jobs]
    results = adapter.run_batch(
        [{"netlist_content_or_path": n, "circuit_name": f"ex15_{i}", "treat_as_text": True}
         for i, (_, _, n) in enumerate(jobs)],
        max_workers=4,
    )
    out = {(g, k): res for (g, k, _), res in zip(jobs, results)}
    out[("tarama", "bulk")] = adapter.run_netlist(sweep_netlist(), circuit_name="ex15_sweep", treat_as_text=True,
                                                  summarize_raw=False)
    return out


def sweep_rows(res):
    """Her C_b/C_in için ızgaradaki en düşük tepe (LTspice) ve aracın ESR aramasının bulduğu tepe."""
    best = {}
    for row in res.stepped_measurements.get("vpk", []) if res.success else []:
        n, k, vpk = row.get("n"), row.get("k"), row.get("vpk")
        if vpk is not None and (n not in best or vpk < best[n][1]):
            best[n] = (k, vpk)
    rows = []
    for n in RATIOS:
        tool = analyze_hotplug_transient(1.0, 1e-6, 1e-6, 0.0, bulk_ratio=n)
        k, vpk = best.get(n, (None, None))
        rows.append((f"C_b = {n:g}·C_in", {"k_grid": k, "vpk": vpk, "k_tool": tool.recommended_bulk_esr / tool.z0,
                                            "tool": tool.recommended_damped_peak}))
    return rows


def V(x, d=2):
    return format_value(x, "V", decimals=d)


def pct(meas, theory):
    return None if meas is None else (meas - theory) / theory * 100


def build_report(runs, elapsed):
    m = {key: (res.measurements if res.success else {}) for key, res in runs.items()}
    rep = SimulationReport(
        "Hot-plug sönüm doğrulaması", "analyze_hotplug_transient: formül ve sönüm önerisi",
        "Takma anı seri RLC modelinin LTspice ölçümüyle karşılaştırılması",
        meta=[("LTspice", LTSpiceAdapter().ltspice_version() or "?"), ("Koşum", str(len(runs))),
              ("Süre", f"{elapsed:.1f} sn")],
    )
    rep.footer = ("Yeniden üretmek için: `python examples/15_hotplug_damping_validation.py`. Kaynaklar: "
                  "`docs/references/text/adi-an88.txt` (s.1-4), `ti-slod006.txt` (s.362), LTspice "
                  "CIRCUIT-ELEMENTS-REFERENCE (bobin Rser).")

    # ---- T1: kapalı form
    rows1, worst = [], 0.0
    for key, (label, v, l_h, c_f, r, _, _) in CASES.items():
        theory = analyze_hotplug_transient(v, c_f, l_h, r).v_peak
        meas = m[("formul", key)].get("vpk")
        worst = max(worst, abs(pct(meas, theory) or 0.0))
        rows1.append((label, {"theory": theory, "meas": meas, "diff": pct(meas, theory)}))
    t1 = Study(
        "T1", "Kapalı form tepe formülü LTspice'la aynı mı?",
        question="Araç sönümsüz tepeyi seri RLC adım yanıtının kapalı formundan hesaplıyor. Simülasyonla uyuşuyor mu?",
        method=("1 ns kenarlı basamak → R → L (Rser=0) → C. Her satır ayrı devre. Ölçüm: `.meas MAX V(c)`. Zaman "
                "adımı periyodun 1/2000'i."),
        table=Table([Column("theory", "formül", "V", decimals=4), Column("meas", "LTspice", "V", decimals=4),
                     Column("diff", "fark", "%", decimals=5)], rows1),
    )
    if runs[("formul", "f_an88")].success:
        x, y = read_trace(runs[("formul", "f_an88")], "V(c)")
        t1.charts.append(LineChart("AN-88 düzeni, 20 mΩ: C_in gerilimi",
                                   [Trace("V(c)", x, y, label_extremes=("max",))], references=[(24.0, "V_in")]))
    rep.add_study(t1)

    # ---- T2: paralel sönüm
    rows2, worst2 = [], 0.0
    for key, (label, v, l_h, c_f, r, cb, rb) in DAMP.items():
        theory = hotplug_step_peak(v, l_h, c_f, r, cb, rb)
        meas = m[("sonum", key)].get("vpk")
        if key != "an88_47u":
            worst2 = max(worst2, abs(pct(meas, theory) or 0.0))
        rows2.append((label, {"theory": theory, "meas": meas, "diff": pct(meas, theory),
                              "ratio": None if meas is None else meas / v}))
    undamped = {"cable": m[("formul", "f_cable")].get("vpk")}
    t2 = Study(
        "T2", "Sönüm önerisinin öngörülen tepesi LTspice'la aynı mı?",
        question=(f"Araç C_b = {REC_CABLE.recommended_bulk_c / 10e-6:g}·C_in ve tepeyi en aza indiren ESR'yi "
                  f"(burada {REC_CABLE.recommended_bulk_esr / Z0_CABLE:.3f}·Z0) öneriyor ve o devrenin ideal tepesini "
                  "hesaplıyor. Hesap simülasyonla uyuşuyor mu? AN-88'in 47 µF / 0.44 Ω örneği ideal modelde ne veriyor?"),
        method=("Seri R = 0 (yalnız sönüm kolu sönümlesin). Öngörü: `hotplug_step_peak` (3. dereceden transfer "
                "fonksiyonu). Ölçüm: `.meas MAX V(c)`; 20 periyot."),
        table=Table([Column("theory", "öngörü", "V", decimals=4), Column("meas", "LTspice", "V", decimals=4),
                     Column("diff", "fark", "%", decimals=5), Column("ratio", "tepe / V_in", "", decimals=4)], rows2),
        effects=[
            Effect("47 µF / 0.44 Ω (AN-88 s.4)", "C_in paraleli, AN-88 düzeni, R = 0", "C_in tepesi",
                   m[("sonum", "an88_none")].get("vpk"), m[("sonum", "an88_47u")].get("vpk"), "V", decimals=2),
            Effect("araç önerisi: 4·C_in + en iyi ESR", "C_in paraleli, AN-88 düzeni, R = 0", "C_in tepesi",
                   m[("sonum", "an88_none")].get("vpk"), m[("sonum", "an88_rec")].get("vpk"), "V", decimals=2),
            Effect("araç önerisi: 4·C_in + en iyi ESR", "C_in paraleli, 12 V · 1 m kablo", "C_in tepesi",
                   undamped["cable"], m[("sonum", "cable_rec")].get("vpk"), "V", decimals=2),
        ],
    )
    traces = []
    for key, run_key, emph in (("sönümsüz", ("formul", "f_cable"), False), ("öneri", ("sonum", "cable_rec"), True)):
        if runs[run_key].success:
            x, y = read_trace(runs[run_key], "V(c)")
            traces.append(Trace(key, x, y, emphasis=emph, label_extremes=("max",)))
    if traces:
        t2.charts.append(LineChart("12 V · 1 m kablo · 10 µF: öneri var / yok", traces, references=[(12.0, "V_in")],
                                   x_range=(0.0, 3 * 2 * math.pi * math.sqrt(L_1M * 10e-6))))
    rep.add_study(t2)

    # ---- T3: kritik seri direnç
    rows3 = [(label, {"theory": analyze_hotplug_transient(v, c_f, l_h, r).v_peak, "meas": m[("seri", key)].get("vpk")})
             for key, (label, v, l_h, c_f, r, _, _) in SERIES.items()]
    rep.add_study(Study(
        "T3", "2·Z0 seri direnç aşımı kaldırıyor mu?",
        question="Araç kritik sönüm (zeta = 1) için toplam seri direnci 2·Z0 öneriyor. Aşım gerçekten kalkıyor mu?",
        method="Tek değişken: seri R (0, Z0, 2·Z0); 12 V, 1 m kablo, 10 µF. Ölçüm: `.meas MAX V(c)`.",
        table=Table([Column("theory", "formül", "V", decimals=4), Column("meas", "LTspice", "V", decimals=4)], rows3),
    ))

    # ---- T4: bulk oranı taraması
    rows4 = sweep_rows(runs[("tarama", "bulk")])
    rep.add_study(Study(
        "T4", "Paralel sönüm kolu kritik sönüm verebilir mi?",
        question=("Seri direnç yokken yalnız C_b + ESR kolu tepeyi ne kadar düşürür? Aracın ESR araması, LTspice'ın "
                  "taramada bulduğu en iyi noktayla uyuşuyor mu?"),
        method=(f"Normalize devre: L = C_in = 1 µ, Z0 = 1 Ω, V_in = 1 V, seri R = 0. Tek koşumda iç içe `.step`: "
                f"C_b/C_in ∈ {{{', '.join(map(str, RATIOS))}}}, ESR/Z0 = {ESR_GRID[0]}…{ESR_GRID[-1]} (0.05 adım). "
                "Ölçüm: `.meas MAX V(c)`; her oranın en düşük tepesi."),
        table=Table([Column("k_grid", "ESR/Z0 (ızgara)", "", decimals=2), Column("vpk", "LTspice tepe / V_in", "",
                     decimals=4), Column("k_tool", "ESR/Z0 (araç)", "", decimals=3),
                     Column("tool", "araç tepe / V_in", "", decimals=4)], rows4),
        notes=["Hiçbir oran tepeyi V_in'e indirmiyor: kritik sönüm için seri direnç (T3) ya da devrenin diğer "
               "kayıpları gerekir. Aracın tepesi ızgaranın arasındaki gerçek en düşük noktadır, ızgaranınkinden "
               "biraz düşük olabilir."],
    ))
    runs_t4 = [("C_b/C_in × ESR taraması", runs[("tarama", "bulk")])]

    # ---- özet
    by_label = dict(rows2)
    an88_ratio = by_label[DAMP["an88_47u"][0]]["ratio"]
    rec_ratio = by_label[DAMP["cable_rec"][0]]["ratio"]
    rep.add_tile("Kapalı form ile LTspice en büyük fark", format_value(worst, "%", decimals=5), "T1, üç devre")
    rep.add_tile("Sönüm öngörüsü ile LTspice en büyük fark", format_value(worst2, "%", decimals=5), "T2, iki ölçek")
    rep.add_tile("Öneriyle tepe / V_in", format_value(rec_ratio, "", decimals=4), "ideal model")
    rep.add_tile("AN-88 örneği, ideal tepe / V_in", format_value(an88_ratio, "", decimals=4),
                 "donanımda (AN-88 Tablo 3 Ch1): 25 V / 24 V")
    rep.add_decision("good" if worst < 0.1 else "serious", "Formül",
                     f"Kapalı form tepe LTspice ile en çok {format_value(worst, '%', decimals=5)} ayrışıyor.")
    rep.add_decision("good" if worst2 < 0.1 else "serious", "Sönüm öngörüsü",
                     f"Sönümlü tepe öngörüsü LTspice ile en çok {format_value(worst2, '%', decimals=5)} ayrışıyor; iki "
                     "ölçekte tepe / V_in aynı.")
    rep.add_decision("good" if (m[("seri", "r_2z0")].get("vpk") or 99) <= 12.0 * 1.001 else "serious", "Kritik seri R",
                     f"2·Z0 seri dirençle tepe {V(m[('seri', 'r_2z0')].get('vpk'), 4)} (V_in = 12 V).")
    rep.add_finding(Finding(
        "AN-88'in örneği ideal modelde kritik sönüm vermiyor",
        "AN-88 s.4 47 µF / 0.44 Ω elektrolitiğin 22 µF ve 1 µH ile kritik sönüm verdiğini yazıyor; donanımda tepe 25 V "
        "(24 V giriş, Tablo 3 Ch1).",
        "Eski araç bu örneği hiç anmadan '4·C_in, ESR = Z0' öneriyor ve AN-88'e atfediyordu. Yeni araç öneriyi kendi "
        "hesabıyla veriyor ve öngördüğü tepeyi yazıyor; AN-88 donanımı bir referans olarak notta.",
        f"Bu örnek ideal modelde (seri R = 0) tepe / V_in = {format_value(an88_ratio, '', decimals=4)}: donanımdaki "
        "sönümün bir kısmı devrenin diğer kayıplarından (kablo, adaptör, yük).", "warn", "kaynakla fark"))
    rep.add_finding(Finding(
        "İdeal model donanımdaki tepeyi küçük tahmin ediyor",
        "AN-88 Tablo 1: 24 V, 1 µH, 10 µF ile donanımda 57.2 V ölçüldü; doğrusal seri RLC'nin tepesi 2·V_in = 48 V'u "
        "aşamaz.",
        "Aracın sonucuna bu sınır not olarak eklendi; güvenlik kararı kaynaksız 1.2×/1.6× eşikleri yerine parçanın "
        "mutlak maksimum gerilimiyle (max_voltage) verilir.",
        f"Bu raporda AN-88 düzeninin ideal tepesi {V(m[('formul', 'f_an88')].get('vpk'))}; donanımda 22 µF ile 41 V "
        "(Tablo 1).", "warn", "sınır"))

    # ---- güvenilirlik
    for g in ("formul", "sonum", "seri"):
        diffs, gear_ok = [], 0
        for (gg, k), res in runs.items():
            if gg != "gear:" + g or not res.success:
                continue
            gear_ok += bool(re.search(r"^method = Gear", LogParser.read_text(res.log_path), re.MULTILINE))
            trap = m[(g, k)].get("vpk")
            if trap is not None:
                diffs.append(abs(res.measurements["vpk"] - trap) / trap * 100)
        if diffs:
            rep.add_check(f"Gear ile trapez en büyük tepe farkı ({g})", format_value(max(diffs), "%", decimals=4),
                          "good" if max(diffs) < 0.1 else "warn", f"logda `method = Gear`: {gear_ok} koşum")
    settle = [abs(m[(g, k)]["vend"] - v[1]) / v[1] * 100 for g, table in (("sonum", DAMP), ("seri", SERIES))
              for k, v in table.items() if k not in ("r0", "an88_none") and "vend" in m[(g, k)]]
    rep.add_check("Sönümlü devrede son değer pencerede oturdu", format_value(max(settle), "%", decimals=5),
                  "good" if max(settle) < 1 else "warn", "20 periyot sonunda V(c) ile V_in farkı (R = 0 hariç)")
    rep.add_check("DC çalışma noktası", "0 V", "info", "PULSE 0'dan başlar; t = 0'da bütün düğümler 0 V.")
    rep.add_runs("T1", [(CASES[k][0], runs[("formul", k)]) for k in CASES])
    rep.add_runs("T2", [(DAMP[k][0], runs[("sonum", k)]) for k in DAMP])
    rep.add_runs("T3", [(SERIES[k][0], runs[("seri", k)]) for k in SERIES])
    rep.add_runs("T4", runs_t4)

    rep.assumptions = [
        ("İdeal doğrusal elemanlar", "Kapasite gerilimle, ESR kenarla değişmez; endüktans doymaz (AN-88 s.2 aksini "
         "söyler: gerçek tepe daha yüksek olabilir)."),
        ("AN-88 düzeninin seri direnci 20 mΩ", "AN-88 s.1 yalnız adaptör çıkış kondansatörünün ESR'sini verir "
         "(10-30 mΩ); kablo ve L_OUT direnci belirtilmemiş."),
        ("1 m kablo 2.4 µH", "2 × 100 cm × 12 nH/cm; TI SLOD006 s.362 aralığının (6-12 nH/cm) üst ucu, iletkenler "
         "arası kuplaj ihmal (tutucu)."),
        ("C_b = 4·C_in", "Tasarım seçimi (aracın bulk_ratio varsayılanı); büyüdükçe tepe düşer, kart alanı artar."),
        ("Basamak kenarı 1 ns", "Rezonans periyodundan (≥ 4 µs) çok kısa: ideal basamak."),
    ]
    rep.open_items = [
        "Kartın kendi kablosu ve konnektörüyle takma anı tepesini donanımda ölçün (osiloskop, tek atış).",
        "Seramik kondansatörü çalışma gerilimindeki DC-bias kaybıyla (üretici eğrisi) yeniden simüle edin.",
    ]
    return rep


def main():
    args = sys.argv[1:]
    out = report_path("15_hotplug_damping_validation.html")
    adapter = LTSpiceAdapter()
    t0 = time.perf_counter()
    runs = run_all(adapter, gear="--hizli" not in args)
    elapsed = time.perf_counter() - t0
    for (g, k), res in runs.items():
        if not res.success:
            print(f"BAŞARISIZ {g}/{k}: {res.errors}")
        elif not g.startswith(("gear:", "tarama")):
            print(f"{g:7s} {k:10s} vpk = {res.measurements['vpk']:.6g}  vend = {res.measurements['vend']:.6g}")
    for label, row in sweep_rows(runs[("tarama", "bulk")]):
        print(f"T4 {label}: ızgara k={row['k_grid']} tepe={row['vpk']:.5f} | araç k={row['k_tool']:.3f} "
              f"tepe={row['tool']:.5f}")
    rep = build_report(runs, elapsed)
    save_report(rep, out)


if __name__ == "__main__":
    main()
