"""
Örnek 22: Kapasitif yükte op-amp izleyicinin döngü kararlılığı
==============================================================
Soru: Op-amp izleyicinin çıkışındaki kapasitif yük faz payını ne kadar düşürür, çıkışa seri yalıtım direnci (Riso)
ne kadar geri kazandırır? Faz payındaki fark adım yanıtında ne kadar aşma demektir?

- T1: döngü kazancı. Döngü, op-amp çıkışı (y) ile eviren giriş (x) arasına konan seri AC kaynağıyla açılır (Middlebrook
  gerilim enjeksiyonu, ``Vinj x y AC 1``); T = -V(y)/V(x), payları ``raw_loop_margins`` verir. Varyantlar tek
  değişkenle ayrışır: yüksüz → CL → CL + Riso.
- T2: aynı devrelerin küçük işaret adım yanıtı; aşma her varyantta op-amp çıkışında (``.meas``), Riso'lu devrede
  yükün ucunda ayrıca.

Op-amp, kutupları ve çıkış direnci bilinen doğrusal bir makro modeldir. Aynı T(jω) kapalı biçimden de hesaplanır ve
LTspice'ınkiyle karşılaştırılır (güvenilirlik denetimi). Değerler varsayımdır; raporda listelenir.

    python examples/22_loop_stability_capacitive_load.py [--hizli] [--rapor <yol>]

``--hizli`` aşmanın Gear çapraz kontrolünü atlar.
"""

import re
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ltspice_llm import LTSpiceAdapter, raw_loop_margins, stability_margins  # noqa: E402
from ltspice_llm.netlist import insert_before_end  # noqa: E402
from ltspice_llm.parser import ALL_TRACES, LogParser, open_raw, raw_axis_values, raw_wave  # noqa: E402
from ltspice_llm.report import (  # noqa: E402
    Column,
    Effect,
    Finding,
    LineChart,
    SimulationReport,
    Study,
    Table,
    Trace,
    format_value,
    read_trace,
)
from study_helpers import report_option, save_report  # noqa: E402

NAME = "22_loop_stability_capacitive_load"

# Varsayımlar (girdi): doğrusal op-amp makro modeli, yük ve ölçüt
A0 = 1e5          # açık döngü DC kazancı
FP1 = 100.0       # baskın kutup, Hz (GBW = A0·FP1)
FP2 = 30e6        # ikinci kutup, Hz
RO = 50.0         # açık döngü çıkış direnci, Ω
CL = 1e-9         # kapasitif yük, F
RISO = 50.0       # yalıtım direnci, Ω
V_STEP = 0.1      # küçük işaret adımı, V
PM_TARGET = 45.0  # tasarım ölçütü, derece

AC_CMD = ".ac dec 100 1k 1G"
TRAN_CMD = ".tran 0 3u 0 1n"

VARIANTS = ("noload", "cl", "cl_riso")


def tran_nodes(variant):
    """Adım yanıtında ölçülen düğümler: hep op-amp çıkışı y; Riso'lu devrede yükün ucu out da."""
    return ("y", "out") if variant == "cl_riso" else ("y",)


def label(variant):
    cl = format_value(CL, "nF", 1e-9, 0)
    return {"noload": "yüksüz", "cl": f"CL = {cl}",
            "cl_riso": f"CL = {cl} + Riso = {format_value(RISO, 'Ω', decimals=0)}"}[variant]


def netlist(variant, analysis):
    """İzleyici: E1 (A0) → R1C1 (FP1) → E2 → R2C2 (FP2) → E3 → Ro → y; geri besleme y → Vinj → x (eviren giriş)."""
    c1 = 1 / (2 * np.pi * 1e3 * FP1)
    c2 = 1 / (2 * np.pi * 1e3 * FP2)
    load = {"noload": "", "cl": f"CL y 0 {CL:.6g}\n",
            "cl_riso": f"Riso y out {RISO:.6g}\nCL out 0 {CL:.6g}\n"}[variant]
    if analysis == "ac":
        drive = "Vin in 0 0\nVinj x y AC 1\n"
        cmd = f"{AC_CMD}\n.save V(x) V(y)\n"
    else:
        nodes = tran_nodes(variant)
        drive = f"Vin in 0 PULSE(0 {V_STEP:g} 100n 1n 1n 10u 20u)\nVinj x y 0\n"
        cmd = f"{TRAN_CMD}\n.save V(in) " + " ".join(f"V({n})" for n in nodes) + "\n" + "".join(
            f".meas tran v_dc_{n} FIND V({n}) AT 90n\n"
            f".meas tran v_max_{n} MAX V({n}) FROM 100n TO 3u\n"
            f".meas tran v_late_{n} FIND V({n}) AT 2.5u\n"
            f".meas tran v_end_{n} FIND V({n}) AT 3u\n" for n in nodes)
    return (f"* op-amp follower, {variant}, {analysis}\n" + drive
            + f"E1 e1 0 in x {A0:.6g}\nR1 e1 p1 1k\nC1 p1 0 {c1:.6g}\n"
            + f"E2 e2 0 p1 0 1\nR2 e2 p2 1k\nC2 p2 0 {c2:.6g}\n"
            + f"E3 o 0 p2 0 1\nRo o y {RO:.6g}\n" + load + cmd + ".end\n")


def analytic_loop_gain(f, variant):
    """Aynı makro modelin kapalı biçimli T(jω): A(s)·H(s); H, Ro ile yükün bölücüsü."""
    s = 2j * np.pi * f
    t = A0 / ((1 + s / (2 * np.pi * FP1)) * (1 + s / (2 * np.pi * FP2)))
    if variant == "cl":
        t = t / (1 + s * RO * CL)
    elif variant == "cl_riso":
        t = t * (1 + s * RISO * CL) / (1 + s * (RO + RISO) * CL)
    return t


def _run(adapter, text, name):
    res = adapter.run_netlist(text, circuit_name=name, treat_as_text=True)
    if not res.success:
        raise SystemExit(f"{name} koşumu başarısız: {res.errors}")
    return res


def run_all(adapter, gear=True):
    runs = {(v, a): _run(adapter, netlist(v, a), f"ex22_{v}_{a}") for v in VARIANTS for a in ("ac", "tran")}
    if gear:
        runs[("cl", "gear")] = _run(adapter, insert_before_end(netlist("cl", "tran"), [".options method=gear"]),
                                    "ex22_cl_tran_gear")
    return runs


def loop_gain(res):
    raw = open_raw(res.raw_path, traces=ALL_TRACES)
    return np.real(raw_axis_values(raw)), -raw_wave(raw, "V(y)") / raw_wave(raw, "V(x)")


def overshoot(meas, node="y"):
    return (meas[f"v_max_{node}"] - meas[f"v_end_{node}"]) / meas[f"v_end_{node}"] * 100


def build_report(adapter, runs, elapsed):
    margins = {v: raw_loop_margins(runs[(v, "ac")].raw_path, "x", "y")["steps"][0] for v in VARIANTS}
    curves = {v: loop_gain(runs[(v, "ac")]) for v in VARIANTS}
    formula = {v: stability_margins(curves[v][0], analytic_loop_gain(curves[v][0], v)) for v in VARIANTS}
    meas = {v: runs[(v, "tran")].measurements for v in VARIANTS}
    over = {v: overshoot(meas[v]) for v in VARIANTS}       # hep aynı düğümde: op-amp çıkışı y
    over_load = overshoot(meas["cl_riso"], "out")           # Riso'lu devrede yükün ucu
    pm = {v: margins[v]["phase_margin_deg"] for v in VARIANTS}
    fc = {v: margins[v]["crossover_hz"] for v in VARIANTS}

    def deg(value):
        return format_value(value, "°", decimals=1)

    def pct(value):
        return format_value(value, "%", decimals=1)

    def mhz(value):
        return format_value(value, "MHz", 1e6, 2)

    rep = SimulationReport(
        "Op-amp döngü kararlılığı", "Kapasitif yükte op-amp izleyicinin kararlılığı",
        "Middlebrook enjeksiyonuyla döngü kazancı, faz ve kazanç payı; Riso ile düzeltme ve adım yanıtındaki aşma",
        meta=[("LTspice", adapter.ltspice_version()), ("Koşum süresi", format_value(elapsed, "s", decimals=1)),
              ("Analiz", f"{AC_CMD}; {TRAN_CMD}")])
    for v in VARIANTS:
        rep.add_tile(f"Faz payı, {label(v)}", deg(pm[v]), f"kazanç geçişi {mhz(fc[v])}")
    if margins["cl"]["gain_margin_db"] is not None:
        rep.add_tile(f"Kazanç payı, {label('cl')}", format_value(margins["cl"]["gain_margin_db"], "dB", decimals=1),
                     f"faz geçişi {mhz(margins['cl']['phase_crossover_hz'])}")
    good = pm["cl_riso"] >= PM_TARGET
    rep.add_decision("good" if good else "warn", f"Riso = {format_value(RISO, 'Ω', decimals=0)}",
                     f"Faz payı {deg(pm['cl'])} → {deg(pm['cl_riso'])} (ölçüt ≥ {PM_TARGET:g}°); op-amp çıkışında aşma "
                     f"{pct(over['cl'])} → {pct(over['cl_riso'])}, yükün ucunda {pct(over_load)}.")

    log_f = {v: np.log10(curves[v][0]) for v in VARIANTS}
    style = {"noload": {"emphasis": False}, "cl": {"slot": 1}, "cl_riso": {"slot": 2}}
    mag = LineChart("Döngü kazancının genliği |T|",
                    [Trace(label(v), log_f[v], 20 * np.log10(np.abs(curves[v][1])), **style[v]) for v in VARIANTS],
                    x_label="frekans", x_unit="log₁₀ Hz", y_label="|T|", y_unit="dB",
                    references=[(0.0, "0 dB")], value_decimals=1,
                    caption="Kazanç geçişi 0 dB çizgisini kestiği yerdir.")
    phase = LineChart("Döngü kazancının fazı ∠T",
                      [Trace(label(v), log_f[v], np.degrees(np.unwrap(np.angle(curves[v][1]))), **style[v])
                       for v in VARIANTS],
                      x_label="frekans", x_unit="log₁₀ Hz", y_label="∠T", y_unit="°",
                      references=[(-180.0, "−180°")], value_decimals=0,
                      caption="Faz payı, kazanç geçişindeki fazın −180°'den uzaklığıdır.")
    t1 = Study(
        "T1", "Döngü kazancı ve faz payı",
        question="Kapasitif yük faz payını ne kadar düşürüyor, Riso ne kadar geri kazandırıyor?",
        method="Op-amp, iki kutuplu doğrusal makro model (E kaynakları ve RC katları) ve çıkış direnci Ro. Döngü, "
               "op-amp çıkışı y ile eviren giriş x arasına konan `Vinj x y AC 1` ile açıldı; T = −V(y)/V(x), payları "
               "`raw_loop_margins` hesapladı. Varyantlar tek değişkenle ayrışır: yüksüz → y'ye CL → CL ile y arasına "
               "Riso (geri besleme Riso'dan önce, y'den alınır).",
        charts=[mag, phase],
        table=Table(
            [Column("fc", "kazanç geçişi", "MHz", 1e6, 2), Column("pm", "faz payı", "°", 1, 1),
             Column("n", "faz geçişi sayısı", "", 1, 0)],
            [(label(v), {"fc": fc[v], "pm": pm[v], "n": float(len(margins[v]["phase_crossovers"]))})
             for v in VARIANTS],
            caption="Payları LTspice'ın .raw'ından raw_loop_margins hesapladı."),
        effects=[
            Effect(f"CL = {format_value(CL, 'nF', 1e-9, 0)}", "op-amp çıkışı y – toprak", "faz payı",
                   pm["noload"], pm["cl"], unit="°", decimals=1, better="higher"),
            Effect(f"CL = {format_value(CL, 'nF', 1e-9, 0)}", "op-amp çıkışı y – toprak", "kazanç geçişi",
                   fc["noload"], fc["cl"], unit="MHz", scale=1e6, decimals=2, better=None),
            Effect(f"Riso = {format_value(RISO, 'Ω', decimals=0)}", "y ile CL arasında", "faz payı",
                   pm["cl"], pm["cl_riso"], unit="°", decimals=1, better="higher"),
        ],
        notes=[f"{label(v)}: {w}" for v in VARIANTS for w in margins[v]["warnings"]],
    )
    rep.add_study(t1)

    load_label = f"{label('cl_riso')}, yükte (out)"
    traces = [Trace(label(v), *read_trace(runs[(v, "tran")], "V(y)"), **style[v]) for v in VARIANTS]
    traces.append(Trace(load_label, *read_trace(runs[("cl_riso", "tran")], "V(out)"), slot=3))

    def row(m, node):
        return {"vmax": m[f"v_max_{node}"], "vend": m[f"v_end_{node}"], "os": overshoot(m, node)}

    t2 = Study(
        "T2", "Adım yanıtında aşma",
        question="Faz payındaki fark zaman bölgesinde ne kadar aşma demek?",
        method=f"Aynı devreler; Vinj = 0 V (geri besleme kısa devre), girişte "
               f"{format_value(V_STEP, 'mV', 1e-3, 0)} basamak (1 ns kenar), `{TRAN_CMD}`. Aşma her varyantta aynı "
               "düğümde, op-amp çıkışında (y), `.meas MAX` ile son değerden hesaplandı. Riso'lu devrede yükün ucu "
               "(out) ayrıca ölçüldü.",
        charts=[LineChart("Op-amp çıkışının ve yükün gerilimi", traces, y_label="gerilim", y_unit="V",
                          x_range=(0.0, 1.5e-6), value_decimals=3,
                          caption="Gösterilen pencere 0–1,5 µs; ölçümler 3 µs'lik koşumdan.")],
        table=Table(
            [Column("vmax", "tepe", "mV", 1e-3, 2), Column("vend", "son değer", "mV", 1e-3, 3),
             Column("os", "aşma", "%", 1, 1)],
            [(label(v), row(meas[v], "y")) for v in VARIANTS] + [(load_label, row(meas["cl_riso"], "out"))],
            caption="İlk üç satır op-amp çıkışında (y); son satır aynı koşumda yükün ucunda (out)."),
        effects=[
            Effect(f"CL = {format_value(CL, 'nF', 1e-9, 0)}", "op-amp çıkışı y", "aşma", over["noload"], over["cl"],
                   unit="%", decimals=1, better="lower"),
            Effect(f"Riso = {format_value(RISO, 'Ω', decimals=0)}", "op-amp çıkışı y", "aşma", over["cl"],
                   over["cl_riso"], unit="%", decimals=1, better="lower"),
        ],
        notes=[f"Riso'lu devrede aşma op-amp çıkışında (y) {pct(over['cl_riso'])}, yükün ucunda (out) "
               f"{pct(over_load)}. Aradaki fark faz payından değil, Riso·CL alçak geçireninin y'deki aşmayı "
               "süzmesinden gelir: y ile out aynı koşumun iki düğümüdür."],
    )
    rep.add_study(t2)

    rep.add_finding(Finding(
        "Kapasitif yük faz payını yiyor",
        cause="Op-amp'ın çıkış direnci Ro ile CL döngüye bir kutup ekler; kutup kazanç geçişinin altına düşünce "
              "geçişteki faz gecikmesi artar.",
        handling="Yük, makro modelde Ro'nun arkasına ideal kondansatör olarak kondu (tek değişken: CL).",
        result=f"Faz payı {deg(pm['noload'])} → {deg(pm['cl'])}, kazanç geçişi {mhz(fc['noload'])} → "
               f"{mhz(fc['cl'])}; adım yanıtında aşma {pct(over['noload'])} → {pct(over['cl'])}.",
        status="serious" if pm["cl"] < pm["noload"] else "info",
        status_label="kötüleşti" if pm["cl"] < pm["noload"] else "kötüleşmedi"))
    rep.add_finding(Finding(
        "Riso'nun sıfırı yük kutbunu dengeliyor",
        cause="CL ile seri Riso döngüye bir sıfır ekler; yüksek frekansta y'yi CL'den yalıtır.",
        handling="Riso, op-amp çıkışı ile CL arasına kondu; geri besleme Riso'dan önce alındı (tek değişken: Riso).",
        result=f"Faz payı {deg(pm['cl'])} → {deg(pm['cl_riso'])}; op-amp çıkışında aşma {pct(over['cl'])} → "
               f"{pct(over['cl_riso'])}. Yükün ucunda aşma {pct(over_load)} (Riso·CL süzmesiyle).",
        status="good" if good else "warn", status_label="iyileşti" if good else "yetersiz"))

    pm_diff = max(abs(pm[v] - formula[v]["phase_margin_deg"]) for v in VARIANTS)
    fc_diff = max(abs(fc[v] / formula[v]["crossover_hz"] - 1) * 100 for v in VARIANTS)
    rep.add_check("Formül ile LTspice faz payı farkı", format_value(pm_diff, "°", decimals=4),
                  "good" if pm_diff < 0.01 else "warn",
                  "Aynı frekans ızgarasında kapalı biçimli T(jω) ile LTspice'ın −V(y)/V(x)'i; fark küçükse enjeksiyonun "
                  f"yönü ve model doğru kurulmuş. Kazanç geçişi farkı en çok {format_value(fc_diff, '%', decimals=4)}.")
    if ("cl", "gear") in runs:
        gear = runs[("cl", "gear")]
        gear_seen = bool(re.search(r"^method = Gear", LogParser.read_text(gear.log_path), re.MULTILINE))
        gear_diff = abs(overshoot(gear.measurements) - over["cl"])
        rep.add_check("Gear ile aşma farkı (CL)", format_value(gear_diff, "puan", decimals=3),
                      "good" if gear_seen and gear_diff < 0.5 else "warn",
                      f"logda `method = Gear`: {'var' if gear_seen else 'yok'}. Fark küçükse çınlama fiziksel, "
                      "trapez artefaktı değil.")
    dc = max(abs(meas[v][f"v_dc_{n}"]) for v in VARIANTS for n in tran_nodes(v))
    rep.add_check("DC çalışma noktası", format_value(dc, "µV", 1e-6, 3), "good" if dc < 1e-3 else "warn",
                  "Basamaktan önce ölçülen düğümlerin gerilimi (giriş 0 V).")
    settle = max(abs(meas[v][f"v_late_{n}"] - meas[v][f"v_end_{n}"])
                 for v in VARIANTS for n in tran_nodes(v)) / V_STEP * 100
    rep.add_check("Son değer pencerede oturdu", format_value(settle, "%", decimals=4), "good" if settle < 1 else "warn",
                  "2,5 µs ile 3 µs arasındaki değişim, basamağın yüzdesi olarak.")
    rep.add_runs("T1", [(label(v), runs[(v, "ac")]) for v in VARIANTS])
    rep.add_runs("T2", [(label(v), runs[(v, "tran")]) for v in VARIANTS])
    if ("cl", "gear") in runs:
        rep.add_runs("Gear", [(f"{label('cl')}, Gear", runs[("cl", "gear")])])

    rep.assumptions = [
        ("Op-amp", f"Doğrusal makro model: DC kazanç {A0:g}, kutuplar {format_value(FP1, 'Hz', decimals=0)} ve "
         f"{format_value(FP2, 'MHz', 1e6, 0)} (GBW {format_value(A0 * FP1, 'MHz', 1e6, 0)}), çıkış direnci "
         f"{format_value(RO, 'Ω', decimals=0)}. Slew rate, besleme sınırları, giriş kapasitansı ve gürültü yok."),
        ("Yük", f"CL = {format_value(CL, 'nF', 1e-9, 0)} ideal (ESR ve ESL yok); Riso = "
         f"{format_value(RISO, 'Ω', decimals=0)} ideal."),
        ("Enjeksiyon", "Vinj, ideal kaynağın (E3) Ro'lu çıkışı ile sonsuz empedanslı eviren giriş arasında: "
         "Middlebrook ölçümü burada döngü kazancına eşit."),
        ("Ölçüt", f"Faz payı ≥ {PM_TARGET:g}° (yaygın tasarım kuralı)."),
        ("Adım", f"{format_value(V_STEP, 'mV', 1e-3, 0)} basamak, 1 ns kenar; model doğrusal olduğu için aşma "
         "genlikten bağımsız."),
    ]
    rep.open_items = [
        "Gerçek bir op-amp modeliyle (ADI `.lib`) tekrarlayın; datasheet'te kapasitif yük grafiği varsa karşılaştırın.",
        "Donanımda döngü kazancını enjeksiyon trafosu ve ağ analizörüyle ölçün ya da yükün düğümünde küçük işaret adım "
        "yanıtının aşmasını osiloskopla ölçün.",
        "Riso, yük akımı varken DC hatası ve daha yüksek çıkış empedansı getirir; yükün DC akımı varsa bunu ayrıca "
        "değerlendirin.",
    ]
    return rep


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    adapter = LTSpiceAdapter()
    t0 = time.perf_counter()
    runs = run_all(adapter, gear="--hizli" not in args)
    elapsed = time.perf_counter() - t0
    for v in VARIANTS:
        m = raw_loop_margins(runs[(v, "ac")].raw_path, "x", "y")["steps"][0]
        print(f"{v:8s} fc = {m['crossover_hz']:.4g} Hz  PM = {m['phase_margin_deg']:.3f} deg  "
              f"GM = {m['gain_margin_db']} dB  asma = {overshoot(runs[(v, 'tran')].measurements):.2f} %")
    save_report(build_report(adapter, runs, elapsed), report_option(args, f"{NAME}.html"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
