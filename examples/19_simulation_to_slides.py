"""
Simülasyondan sunuma ve GIF'e: RC basamak yanıtı (ajan skill'lerinin uçtan uca örneği)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Soru: seri direnç R 1 kΩ'dan 2,2 kΩ'a (E12) çıkınca 1 µF'lık kondansatörün %63,2'ye (1 - 1/e) dolma anı ne olur?
Tek değişken R; ölçülen düğüm hep V(out). Her sayı LTspice ``.meas`` satırından gelir.

Çıktılar (``reports/`` ya da ``LTSPICE_LLM_REPORT_DIR``):
- ``19_simulation_to_slides.html``: SimulationReport (GEMINI.md "Simülasyon raporu").
- ``19_simulation_to_slides.pptx``: narrative-pptx sunumu; görseller ``19_simulation_to_slides_dosyalar/`` altında.
- İki GIF (explainer-gif): R = 1 kΩ taraması ve iki R arasında eğri geçişi.

Skill betikleri paket değildir: klasörleri ``sys.path``'e eklenir (``.agents/skills/<ad>/scripts``).

    python examples/19_simulation_to_slides.py
"""

import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for _skill in ("narrative-pptx", "explainer-gif"):
    sys.path.insert(0, str(ROOT / ".agents" / "skills" / _skill / "scripts"))

from chart_artist import render_annotated_panels  # noqa: E402
from deck_builder import build_deck, spec_warnings  # noqa: E402
from gif_archetypes import render_parameter_morph_gif, render_scope_sweep_gif  # noqa: E402
from validate_deck import audit_pptx  # noqa: E402
from validate_gif import audit_gif  # noqa: E402

import ltspice_llm  # noqa: E402
from ltspice_llm import LTSpiceAdapter  # noqa: E402
from ltspice_llm.report import (  # noqa: E402
    Column,
    Effect,
    LineChart,
    SimulationReport,
    Study,
    Table,
    Trace,
    format_value,
    read_trace,
)
from study_helpers import report_path, save_report  # noqa: E402

NAME = "19_simulation_to_slides"

# --------------------------------------------------------------------------- girdiler (varsayımlar)
V_STEP = 5.0                      # V, darbe genliği
C_F = 1e-6                        # F, ideal kondansatör
R_VALUES = (1e3, 2.2e3)           # Ω, E12; tek değişken
T_ON = 15e-3                      # s, darbe açık süresi: en yavaş R·C'nin 6,8 katı, pencerede oturur
T_STOP = 2 * T_ON
V_63 = (1.0 - math.exp(-1.0)) * V_STEP   # 1 - 1/e tanımı (zaman sabiti anındaki gerilim)


def netlist(r_ohm):
    return f"""* RC basamak yanıtı, R = {r_ohm:g} ohm
V1 in 0 PULSE(0 {V_STEP:g} 0 1u 1u {T_ON:g} {T_STOP:g})
R1 in out {r_ohm:g}
C1 out 0 {C_F:g}
.tran 0 {T_STOP:g} 0 5u
.meas tran t63 WHEN V(out)={V_63:.6g} RISE=1
.meas tran v_on FIND V(out) AT={T_ON:g}
.meas tran v_end FIND V(out) AT={T_STOP:g}
.end
"""


def r_label(r_ohm):
    return f"R = {format_value(r_ohm, 'kΩ', 1e3, 1)}"


def run_all(adapter):
    runs = {}
    for r in R_VALUES:
        res = adapter.run_netlist(netlist(r), circuit_name=f"ex19_rc_{int(r)}")
        if not res.success:
            raise SystemExit(f"{r_label(r)} koşumu başarısız: {res.errors}")
        runs[r] = res
    return runs


def build_gifs(runs, out_dir):
    r0, r1 = R_VALUES
    t, v_in = read_trace(runs[r0], "V(in)")
    _, v_out = read_trace(runs[r0], "V(out)")
    _, i_c = read_trace(runs[r0], "I(C1)")
    t63 = runs[r0].measurements["t63"]
    sweep = render_scope_sweep_gif(
        out_dir / "rc_tarama.gif", t * 1e3,
        [{"title": "Giriş ve çıkış", "ylabel": "V", "traces": [{"y": v_in, "label": "V(in)", "color": "slate"},
                                                               {"y": v_out, "label": "V(out)", "color": "blue"}]},
         {"title": "Kondansatör akımı", "ylabel": "mA", "traces": [{"y": i_c * 1e3, "label": "I(C1)", "color": "amber"}]}],
        title=f"RC basamak yanıtı, {r_label(r0)}", xlabel="Zaman (ms)",
        stages=[{"t": 0.0, "text": "Kaynak açık: kondansatör dolar", "role": "blue"},
                {"t": T_ON * 1e3, "text": "Kaynak kapalı: kondansatör boşalır", "role": "rose"}],
        events=[{"t": t63 * 1e3, "panel": 0, "xy": (t63 * 1e3, float(np.interp(t63, t, v_out))), "offset": (26, -24),
                 "text": f"%63,2: {format_value(t63, 'ms', 1e-3, 3)}", "role": "blue"}],
        poster_time=T_ON * 1e3,
    )

    grid = np.linspace(0.0, T_STOP, 800)   # her koşumun ekseni ayrı (değişken adım): ortak ızgaraya taşınır
    cases = []
    for r, color in zip(R_VALUES, ("blue", "amber")):
        tr, vr = read_trace(runs[r], "V(out)")
        cases.append({"label": r_label(r), "y": np.interp(grid, tr, vr), "color": color,
                      "note": f"%63,2: {format_value(runs[r].measurements['t63'], 'ms', 1e-3, 3)}"})
    morph = render_parameter_morph_gif(out_dir / "rc_r_gecisi.gif", grid * 1e3, cases, title="V(out), R değişirken",
                                       xlabel="Zaman (ms)", ylabel="V", interp_steps=10)
    return sweep, morph


def build_chart(runs, out_dir):
    traces, callouts = [], []
    for r, color, offset in zip(R_VALUES, ("blue", "amber"), ((70, -14), (70, -40))):
        t, v = read_trace(runs[r], "V(out)")
        t63 = runs[r].measurements["t63"]
        traces.append({"x": t * 1e3, "y": v, "label": r_label(r), "color": color})
        callouts.append({"xy": (t63 * 1e3, float(np.interp(t63, t, v))), "offset": offset, "role": color,
                         "text": f"{r_label(r)}: {format_value(t63, 'ms', 1e-3, 3)}"})
    return render_annotated_panels(
        out_dir / "rc_karsilastirma.png",
        [{"title": "V(out): iki direnç", "xlabel": "Zaman (ms)", "ylabel": "V", "traces": traces,
          "callouts": callouts, "hlines": [{"y": V_63, "color": "slate"}]}],
        figsize=(8.15, 2.3),
    )


def build_story(runs, chart, sweep, morph, version):
    r0, r1 = R_VALUES
    m0, m1 = runs[r0].measurements, runs[r1].measurements
    ratio = m1["t63"] / m0["t63"]
    t63 = [format_value(m["t63"], "ms", 1e-3, 3) for m in (m0, m1)]
    v_on = [format_value(m["v_on"], "V", decimals=3) for m in (m0, m1)]
    return {
        "title": "Seri direnç büyüdükçe kondansatör geç dolar",
        "author": "ltspice-llm örnek 19",
        "story_arc": "Tek değişken R; %63,2 dolma anı R ile birlikte uzar.",
        "theme": "midnight_editorial",
        "language": "tr",
        "slides": [
            {"layout": "hero_cover", "accent": "blue", "eyebrow": "SİMÜLASYON ÇALIŞMASI",
             "title": f"R {format_value(r0, 'kΩ', 1e3, 1)}'dan {format_value(r1, 'kΩ', 1e3, 1)}'a çıkınca dolma "
                      f"{format_value(ratio, '', decimals=2)} kat uzuyor",
             "subtitle": "1 µF'lık kondansatör aynı 5 V darbeyle dolup boşalıyor; değişen tek şey seri direnç.",
             "pillars": [{"role": "blue", "tag": "1. BÖLÜM", "title": "Düzenek", "body": "Darbe, R ve C; ölçülen V(out)."},
                         {"role": "amber", "tag": "2. BÖLÜM", "title": "Kanıt", "body": "Tarama GIF'i ve iki R."},
                         {"role": "emerald", "tag": "3. BÖLÜM", "title": "Kural", "body": "Dolma süresi R ile ölçeklenir."}],
             "meta": f"LTspice {version} · simülasyon, donanım ölçümü değil",
             "speaker_notes": "Soru: seri direnci büyütmek kondansatörün dolma anını nasıl değiştirir?"},
            {"layout": "mental_model", "chapter": "Düzenek", "accent": "blue",
             "assertion": "Aynı darbe, aynı kondansatör: iki koşum arasında yalnızca seri direnç değişiyor.",
             "image": chart.name, "scene_title": "Test düzeneği",
             "scene_summary": f"V1 0-5 V darbe, {format_value(T_ON, 'ms', 1e-3, 0)} açık. C = 1 µF. Ölçülen: V(out).",
             "characters": [{"role": "blue", "title": "Direnç", "law": "I = V/R", "body": "Akımı sınırlar; tek değişken.",
                             "trait": " → ".join(r_label(r) for r in R_VALUES)},
                            {"role": "amber", "title": "Kondansatör", "law": "I = C·dV/dt",
                             "body": "Gerilimi ani sıçramaz; akım verildikçe dolar.", "trait": "C = 1 µF"}],
             "narrative_bridge": "Önce tek bir koşumu baştan sona izleyelim.",
             "speaker_notes": "Grafikteki iki eğri aynı düğümün iki R değerindeki yanıtı."},
            {"layout": "evidence_bento", "chapter": "Kanıt", "accent": "blue",
             "assertion": "Kaynak açılınca V(out) üstel yükselir, kaynak kapanınca akım yön değiştirir.",
             "image": sweep.name, "image_caption": f"{r_label(r0)}: tarama; olay balonu .meas t63",
             "metrics": [{"role": "blue", "label": "%63,2 dolma", "value": t63[0], "note": "WHEN V(out) = %63,2"},
                         {"role": "emerald", "label": "darbe sonu", "value": v_on[0], "note": "V(out), t = T_on"}],
             "mechanism": {"role": "blue", "title": "Neden üstel?", "body": "Kondansatör doldukça R üzerindeki gerilim, "
                           "dolayısıyla akım azalır; dolma hızı da azalır.", "takeaway_pill": "Pencere sonunda oturuyor"},
             "narrative_bridge": "Aynı ölçümü R'yi büyüterek tekrarlayalım.",
             "speaker_notes": "Balon %63,2 anını gösteriyor; değer .meas satırından."},
            {"layout": "evidence_bento", "chapter": "Karşılaştırma", "accent": "amber",
             "assertion": f"R büyüyünce %63,2 anı {t63[0]}'dan {t63[1]}'a kayıyor.",
             "image": morph.name, "image_caption": "Ara kareler iki eğrinin karışımıdır, simülasyon değil",
             "metrics": [{"role": "blue", "label": r_label(r0), "value": t63[0], "note": "%63,2 dolma"},
                         {"role": "amber", "label": r_label(r1), "value": t63[1], "note": "%63,2 dolma"}],
             "mechanism": {"role": "amber", "title": "Oran", "body": f"Ölçülen oran {format_value(ratio, '', decimals=3)}; "
                           "zaman sabiti R·C ile orantılı.", "takeaway_pill": "Tek değişken: R"},
             "narrative_bridge": "Sayıları yan yana koyalım.",
             "speaker_notes": "Kayan eğri yalnız iki koşum arasındaki geçişi gösterir."},
            {"layout": "split_duality", "chapter": "Tablo", "accent": "amber",
             "assertion": "Darbe sonunda iki koşum da kaynağa oturuyor; fark yalnızca dolma hızında.",
             "left": {"role": "blue", "title": r_label(r0), "subtitle": "önce",
                      "rows": [{"label": "%63,2 dolma", "value": f"`{t63[0]}`"},
                               {"label": "darbe sonu V(out)", "value": f"`{v_on[0]}`"}], "summary": "Hızlı dolar"},
             "right": {"role": "amber", "title": r_label(r1), "subtitle": "sonra",
                       "rows": [{"label": "%63,2 dolma", "value": f"`{t63[1]}`"},
                                {"label": "darbe sonu V(out)", "value": f"`{v_on[1]}`"}], "summary": "Yavaş dolar"},
             "narrative_bridge": "Buradan çıkan kural ve varsayımlar.",
             "speaker_notes": "Satırlar aynı .meas ölçümleri."},
            {"layout": "synthesis_closing", "chapter": "Sonuç", "accent": "emerald",
             "assertion": "Dolma süresini kısaltmak için seri direnci küçült; kaynağın akım sınırını da hesaba kat.",
             "pillars": [{"role": "blue", "badge": "KURAL", "title": "Süre R ile ölçeklenir",
                          "body": f"Oran {format_value(ratio, '', decimals=2)} (ölçüm).", "rule": "Tek değişken: R"},
                         {"role": "amber", "badge": "BEDEL", "title": "Tepe akım artar",
                          "body": "Küçük R, açılış anında daha büyük akım demek.", "rule": "Kaynağı kontrol et"},
                         {"role": "rose", "badge": "VARSAYIM", "title": "İdeal bileşen",
                          "body": "Kaynak iç direnci, ESR ve kaçak modellenmedi.", "rule": "Simülasyon, ölçüm değil"}],
             "closing_banner": "Sayıların hepsi LTspice .meas satırlarından; donanımda ölçülmedi.",
             "speaker_notes": "Kural, bedel ve varsayımları tekrar et."},
        ],
    }


def build_report(runs, version):
    r0, r1 = R_VALUES
    rep = SimulationReport(NAME, "RC basamak yanıtı: seri direncin dolma anına etkisi",
                           subtitle="Sunum ve GIF üreten ajan skill'lerinin uçtan uca örneği",
                           meta=[("LTspice", version or "?"), ("ltspice_llm", ltspice_llm.__version__)])
    traces = []
    for r, slot in zip(R_VALUES, (1, 2)):
        t, v = read_trace(runs[r], "V(out)")
        traces.append(Trace(r_label(r), t, v, slot=slot))
    rows = [(r_label(r), {"t63": runs[r].measurements["t63"], "v_on": runs[r].measurements["v_on"],
                          "v_end": runs[r].measurements["v_end"]}) for r in R_VALUES]
    rep.add_study(Study(
        "T1", "Seri direnç ve dolma anı",
        question="R 1 kΩ'dan 2,2 kΩ'a çıkınca kondansatörün %63,2'ye dolma anı ne kadar uzar?",
        method="0-5 V darbe, R1 seri, C1 = 1 µF. Tek değişken R1; ölçülen düğüm V(out). t63: WHEN V(out) = %63,2.",
        charts=[LineChart("V(out)", traces)],
        table=Table([Column("t63", "%63,2 dolma", "ms", 1e-3, 3), Column("v_on", "darbe sonu", "V", 1.0, 3),
                     Column("v_end", "pencere sonu", "V", 1.0, 3)], rows),
        effects=[Effect(f"R1: {r_label(r0)} → {r_label(r1)}", "V(out)", "%63,2 dolma anı",
                        runs[r0].measurements["t63"], runs[r1].measurements["t63"], unit="ms", scale=1e-3, decimals=3,
                        better="lower", study="T1")],
    ))
    rep.add_runs("T1", [(r_label(r), runs[r]) for r in R_VALUES])
    for r in R_VALUES:
        settled = abs(runs[r].measurements["v_on"] - V_STEP) / V_STEP
        rep.add_check(f"{r_label(r)}: V(out) darbe sonunda kaynağa oturdu", format_value(settled * 100, "%", decimals=3),
                      "ok" if settled < 0.002 else "warn", "|V(out) - 5 V| / 5 V, t = T_on")
    rep.assumptions = [("Bileşenler", "İdeal R ve C; ESR, kaçak ve kaynak iç direnci yok."),
                       ("Darbe", "0-5 V, 1 µs kenar, 15 ms açık (girdi).")]
    rep.open_items = ["Donanımda aynı düğüm osiloskopla ölçülmeli; kaynak iç direnci dolma anını uzatır."]
    return rep


def main():
    adapter = LTSpiceAdapter()
    version = adapter.ltspice_version() or "?"
    runs = run_all(adapter)

    assets = report_path(f"{NAME}_dosyalar")
    assets.mkdir(parents=True, exist_ok=True)
    sweep, morph = build_gifs(runs, assets)
    chart = build_chart(runs, assets)
    spec = build_story(runs, chart, sweep, morph, version)
    problems = spec_warnings(spec)
    deck = build_deck(spec, report_path(f"{NAME}.pptx"), base_dir=assets)

    checks = [(p.name, audit_gif(p)) for p in (sweep, morph)] + [(deck.name, audit_pptx(deck))]
    for name, rep in checks:
        print(f"{name}: {'PASSED' if rep['passed'] else 'FAILED'} {rep['errors']} {rep['warnings']}")
    save_report(build_report(runs, version), report_path(f"{NAME}.html"))
    print(f"Sunum: {deck}")
    if problems or not all(rep["passed"] and not rep["warnings"] for _, rep in checks):
        raise SystemExit(f"Denetim geçmedi: {problems}")


if __name__ == "__main__":
    main()
