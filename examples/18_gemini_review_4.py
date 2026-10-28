"""
Örnek 18: Dördüncü Gemini incelemesinin (2026-10-06) iki iddiası, LTspice 26.1.1 ve spicelib 1.6.3 ile ölçülerek.

- T1 Steplenmiş .raw okunamadığında ``open_raw`` doğru nedeni söylüyor mu? Gemini: yanındaki .log silinmiş ikili
  .raw'da hata "steplenmiş ASCII" diyor. Aynı devre ikili ve ASCII; .log var / yok / başka koşumun logu / log değil;
  tembel açılış (read_trace, Bode) ve tek geçiş (özet, grafik).
- T2 Özetin 15 noktalı önizlemesi indeks aralıklı; LTspice'ın adımı değişken. Gemini: noktaların %80'i bir kenara
  yığılabilir, zamanda eşit aralıklı np.interp daha dengeli olur. Okuyan (LLM) önizlemeyi zamansız alır ve eşit
  aralıklı sanır; ölçüt, bu varsayımla kurulan eğrinin gerçek dalga formundan sapması.
- T3 (yeni bulgu) Tstart > 0 olan .tran'da LTspice ekseni 0'dan yazıyor, kayma başlığın Offset alanında; spicelib
  uygulamıyor. Tanık LTspice'ın kendi .meas WHEN zamanı; RC ve diyot kırpıcı.

Her testte "eski" kol, düzeltmenin monkeypatch'le geri alınmasıdır (tek değişken).

    python examples/18_gemini_review_4.py      # ~15 sn

Rapor: ``reports/18_gemini_review_4.html``. Ayrıntı: docs/LESSONS_LEARNED_PERFORMANCE.md §12.
"""

import importlib
import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

import logging  # noqa: E402

import ltspice_llm.parser as parser_mod  # noqa: E402
from ltspice_llm import LTSpiceAdapter  # noqa: E402
from ltspice_llm.parser import ALL_TRACES, RawParser, open_raw, raw_steps, raw_wave  # noqa: E402
from ltspice_llm.report import (  # noqa: E402
    Column, Effect, Finding, LineChart, SimulationReport, Study, Table, Trace, format_value, log_facts, read_trace,
)
from study_helpers import report_path, save_report  # noqa: E402

logging.getLogger("spicelib").setLevel(logging.ERROR)  # .log yok uyarıları T1'in konusu, tabloya giriyor

WS = Path(tempfile.gettempdir()) / "ltspice_llm_ex18"
FIX = importlib.import_module("14_record_flow_fixtures")
BUCK = importlib.import_module("10_performance_fast_paths").BUCK
STEP_DC, MEAN, RC = FIX.STEP_DC, FIX.MEAN, FIX.RC
# Takma anı çınlaması: 12 V basamak, 50 mΩ, 1 µH, 10 µF (f0 = 1/(2π√(LC)) ≈ 50 kHz, Q = √(L/C)/R ≈ 6,3). .tran'da azami
# adım yok (LLM'in tipik yazdığı biçim); olay pencerenin başında ve ortasında.
HOTPLUG = ("* hotplug ringdown\nV1 src 0 PULSE(0 12 {td} 1n 1n 1 2)\nR1 src a 50m\nL1 a c 1u Rser=0\nC1 c 0 10u\n"
           ".tran 5m\n.save V(c) I(L1)\n.meas tran vend FIND V(c) AT 5m\n.end\n")
N_PREVIEW = 15


def _fresh(name):
    folder = WS / name
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    return folder


# ============================================================================ T1 steplenmiş .raw
OLD_MESSAGE = ("'{name}' okunamadı (IndexError: {ex}). Steplenmiş (.step) ASCII .raw dosyaları spicelib ile "
               "okunamıyor; simülasyonu ikili (binary) raw biçiminde çalıştırın (LTSpiceAdapter(raw_format='auto') bunu "
               ".step için otomatik yapar).")
STEP5 = STEP_DC.replace("list 1k 3k", "list 1k 2k 3k 4k 5k")
# (etiket, kayıt, biçim, .log, beklenen). .log: "kendi", None (yok), "metin" ya da logu alınan koşum (kayıt, biçim).
# Beklenen: "oku" (2 adım, elle hesap) ya da hatada geçmesi gereken metin.
T1_CASES = [
    ("ikili, .log var", "dc", "binary", "kendi", "oku"),
    ("ikili, .log yok", "dc", "binary", None, "'x.log' yok"),
    ("ikili, steplenmemiş koşumun .log'u", "dc", "binary", ("rc", "binary"), ".step satırı yok"),
    ("ikili, LTspice logu olmayan .log", "dc", "binary", "metin", ".step satırı yok"),
    ("ikili 5 adım, 2 adımlı koşumun .log'u", "dc5", "binary", ("dc", "binary"), "2 .step satırı"),
    ("ikili 2 adım, 5 adımlı koşumun .log'u", "dc", "binary", ("dc5", "binary"), "Expecting 5 got 2"),
    ("ASCII, .log var", "dc", "ascii", "kendi", "ASCII"),
    ("ASCII, .log yok", "dc", "ascii", None, "ASCII"),
    ("ikili .tran (darbe), .log var", "pulse", "binary", "kendi", "oku"),
    ("ikili .tran (darbe), .log yok", "pulse", "binary", None, "'x.log' yok"),
]


def _try_read(path, traces):
    """open_raw + her adımın eksenini ve izlerini okur: (sonuç metni, okunan adım, adım başına V(out)/V(in))."""
    try:
        raw = open_raw(path, traces=traces)
        steps = raw_steps(raw)
        name = "V(out)" if "V(out)" in raw.get_trace_names() else raw.get_trace_names()[1]
        waves = [np.asarray(raw_wave(raw, name, s), dtype=float) for s in steps]
        return f"okundu, {len(steps)} adım, adım 1'de {len(waves[0])} nokta", len(steps), waves
    except Exception as ex:
        text = str(ex)
        return f"{type(ex).__name__}: {text[:150]}{'…' if len(text) > 150 else ''}", 0, None


def _correct(expect, outcome, waves, fmt_kind):
    if expect == "oku":
        if waves is None or len(waves) != 2:
            return False
        if fmt_kind == "dc":  # elle (bölücü): V(out) = V1·1k/(r + 1k), r = 1k ve 3k
            return np.allclose(waves[0], [0, 0.5, 1]) and np.allclose(waves[1], [0, 0.25, 0.5])
        return True
    return not outcome.startswith("okundu") and expect in outcome and (expect != "ASCII" or "x.log" not in outcome)


def t1_stepped(ad_runs):
    rows = []
    for arm in ("eski", "yeni"):
        real_flags, real_msg = parser_mod._raw_flags, parser_mod._unreadable_raw_message
        if arm == "eski":
            parser_mod._raw_flags = lambda raw: ""  # adım sayısı denetimi yok
            parser_mod._unreadable_raw_message = lambda path, ex: OLD_MESSAGE.format(name=path.name, ex=ex)
        try:
            for i, (label, kind, fmt, log, expect) in enumerate(T1_CASES):
                res = ad_runs[(kind, fmt)]
                for traces in (None, ALL_TRACES):
                    folder = _fresh(f"t1_{arm}_{i}_{'tek' if traces else 'tembel'}")
                    shutil.copyfile(res.raw_path, folder / "x.raw")
                    if log == "kendi":
                        shutil.copyfile(res.log_path, folder / "x.log")
                    elif isinstance(log, tuple):
                        shutil.copyfile(ad_runs[log].log_path, folder / "x.log")
                    elif log == "metin":
                        (folder / "x.log").write_text("başka bir dosya\n", encoding="utf-8")
                    outcome, n_steps, waves = _try_read(folder / "x.raw", traces)
                    rows.append({"arm": arm, "label": label, "mode": "tek geçiş" if traces else "tembel",
                                 "outcome": outcome, "steps": n_steps, "ok": _correct(expect, outcome, waves, kind),
                                 "merged": waves is not None and len(waves) == 1 and kind == "dc"})
        finally:
            parser_mod._raw_flags, parser_mod._unreadable_raw_message = real_flags, real_msg
    return rows


# ============================================================================ T2 önizleme
def _previews(raw_path, arm):
    real = parser_mod.time_weighted_mean_rms
    if arm == "eski":
        parser_mod.time_weighted_mean_rms = lambda t, y: None  # zaman ekseni kullanılmaz: indeks aralıklı önizleme
    try:
        return {k: v.downsampled_points for k, v in RawParser.summarize_file(raw_path).items()}
    finally:
        parser_mod.time_weighted_mean_rms = real


def _reader_curve(t, preview):
    """Okuyanın eğrisi: önizleme değerleri zamanda eşit aralıklı sanılır, doğrusal birleştirilir."""
    return np.interp(t, np.linspace(t[0], t[-1], len(preview)), preview)


def _reader_error(t, y, preview):
    err = _reader_curve(t, preview) - y
    pp = float(y.max() - y.min()) or 1.0
    return parser_mod.time_weighted_mean_rms(t, err)[1] / pp, float(np.max(np.abs(err))) / pp


def _rc_hand(t):
    """examples/14 RC: τ = 1 ms, 5 V darbe 0-1 ms ve 2-3 ms (kenarlar 1 ns, ihmal)."""
    tau, v1 = 1e-3, 5 * (1 - np.exp(-1))
    v2 = v1 * np.exp(-1)
    return np.where(t <= 1e-3, 5 * (1 - np.exp(-t / tau)),
                    np.where(t <= 2e-3, v1 * np.exp(-(t - 1e-3) / tau), 5 - (5 - v2) * np.exp(-(t - 2e-3) / tau)))


def t2_preview(ad, rc):
    runs = {"RC (examples/14)": rc,
            "buck (examples/10)": ad.run_netlist(BUCK, circuit_name="t2_buck", treat_as_text=True),
            "takma anı, olay 0 ms": ad.run_netlist(HOTPLUG.replace("{td}", "0"), circuit_name="t2_hp0",
                                                   treat_as_text=True),
            "takma anı, olay 2,5 ms": ad.run_netlist(HOTPLUG.replace("{td}", "2.5m"), circuit_name="t2_hpmid",
                                                     treat_as_text=True)}
    gear = ad.run_netlist(HOTPLUG.replace("{td}", "2.5m").replace(".end", ".options method=gear\n.end"),
                          circuit_name="t2_hpmid_gear", treat_as_text=True)
    rows = []
    for label, res in list(runs.items()) + [("takma anı, olay 2,5 ms, Gear", gear)]:
        old, new = _previews(res.raw_path, "eski"), _previews(res.raw_path, "yeni")
        for sig in [s for s in new if s in ("V(out)", "V(vout)", "I(L1)", "V(c)")]:
            t, y = read_trace(res, sig)
            span = t[-1] - t[0]
            densest = np.max(np.searchsorted(t, t + 0.1 * span) - np.arange(len(t))) / len(t)
            idx_t = t[np.linspace(0, len(t) - 1, N_PREVIEW, dtype=int)]
            gaps = np.diff(idx_t) / (span / (N_PREVIEW - 1))
            e_old, m_old = _reader_error(t, y, old[sig])
            e_new, m_new = _reader_error(t, y, new[sig])
            row = {"label": f"{label} {sig}", "points": len(t), "densest": densest * 100, "gap_min": gaps.min(),
                   "gap_max": gaps.max(), "rms_old": e_old * 100, "rms_new": e_new * 100, "max_old": m_old * 100,
                   "max_new": m_new * 100, "early_old": int(np.sum(idx_t - t[0] < 0.5 * span))}
            if label.startswith("RC") and sig == "V(out)":
                hand = _rc_hand(np.linspace(0, 3e-3, N_PREVIEW))
                row["hand_old"] = float(np.max(np.abs(np.array(old[sig]) - hand)))
                row["hand_new"] = float(np.max(np.abs(np.array(new[sig]) - hand)))
            rows.append(row)
    t, y = read_trace(runs["takma anı, olay 2,5 ms"], "V(c)")
    mid = {"t": t, "y": y, "old": _previews(runs["takma anı, olay 2,5 ms"].raw_path, "eski")["V(c)"],
           "new": _previews(runs["takma anı, olay 2,5 ms"].raw_path, "yeni")["V(c)"]}
    # 6 V'un (yarı yol) ilk geçildiği an: gerçek iz ve okuyanın iki eğrisi
    mid["cross"] = {k: float(t[np.argmax(v >= 6.0)]) for k, v in
                    (("gerçek", y), ("eski", _reader_curve(t, mid["old"])), ("yeni", _reader_curve(t, mid["new"])))}
    return {"runs": runs, "gear": gear, "rows": rows, "mid": mid}


# ============================================================================ T3 Tstart (yeni bulgu)
# Test-reviewer'ın "önizleme ızgarası 0'dan başlarsa" mutantını öldürecek kayıt ararken bulundu. ADI
# SIMULATION-COMMANDS-REFERENCE .tran: "Tstart | Start time for saving data (data before this discarded)".
# Tanık LTspice'ın kendi .meas WHEN zamanı (mutlak). İkinci tanık doğrusal olmayan bir devre: diyot kırpıcı
# (.tran 0 4m 2m 1u); 1 kHz sinüsün kırpılmış çıkışı 0 V'u 2,5 ms civarında düşerek geçer. 1N4148 LTspice'ın standart
# kütüphanesinden (elle yazılan netlistte `.lib standard.dio` gerekir; GEMINI.md).
CLIPPER = """* t3 diyot kırpıcı
V1 in 0 SINE(0 2 1k)
R1 in vout 1k
D1 vout 0 1N4148
D2 0 vout 1N4148
.lib standard.dio
.tran 0 4m 2m 1u
.meas tran t_zc WHEN V(vout)=0 FALL=1
.end
"""
OTHER_ANALYSES = {
    ".ac": "* ac\nV1 in 0 AC 1\nR1 in out 1k\nC1 out 0 1u\n.ac dec 5 10 1k\n.end\n",
    ".dc": "* dc\nV1 in 0 1\nR1 in out 1k\nR2 out 0 1k\n.dc V1 1 2 0.5\n.end\n",
    ".op": "* op\nV1 in 0 1\nR1 in out 1k\nR2 out 0 1k\n.op\n.end\n",
    ".noise": "* noise\nV1 in 0 0 AC 1\nR1 in out 1k\nC1 out 0 1u\n.noise V(out) V1 dec 5 10 1k\n.end\n",
    ".tf": "* tf\nV1 in 0 1\nR1 in out 1k\nR2 out 0 1k\n.tf V(out) V1\n.end\n",
    ".tran (Tstart 0)": "* tran\nV1 in 0 PULSE(0 1 0 1n 1n 1m 2m)\nR1 in out 1k\nC1 out 0 1u\n.tran 0 2m 0\n.end\n",
}


def _crossings(x, y, level, falling):
    i = np.flatnonzero((y[:-1] > level) & (y[1:] <= level) if falling else (y[:-1] < level) & (y[1:] >= level))
    return x[i] + (level - y[i]) * (x[i + 1] - x[i]) / (y[i + 1] - y[i])


def _axis_arms(res, sig, step=0):
    """Eski (Offset uygulanmaz) ve yeni eksenle (x, y): `raw_axis_values` ile, adım ``step``."""
    out = {}
    for arm in ("eski", "yeni"):
        real = parser_mod._raw_offset
        if arm == "eski":
            parser_mod._raw_offset = lambda raw: 0.0
        try:
            raw = open_raw(res.raw_path, traces=ALL_TRACES)
            out[arm] = (np.real(parser_mod.raw_axis_values(raw, step)).astype(float),
                        np.real(raw_wave(raw, sig, step)).astype(float))
        finally:
            parser_mod._raw_offset = real
    return out


def t3_tstart(ad):
    rc = ad.run_netlist(FIX.RC_TSTART, circuit_name="t3_rc", treat_as_text=True)
    rc_ascii = LTSpiceAdapter(workspace_dir=_fresh("t3_ascii"), raw_format="ascii", solver_threads=1).run_netlist(
        FIX.RC_TSTART, circuit_name="t3_rc_ascii", treat_as_text=True)
    rc_step = ad.run_netlist(FIX.RC_TSTART_STEP, circuit_name="t3_rc_step", treat_as_text=True)
    clip = ad.run_netlist(CLIPPER, circuit_name="t3_clip", treat_as_text=True)
    offset = float(open_raw(rc.raw_path).get_raw_property("Offset"))
    step2 = rc_step.stepped_measurements["t_x"][1]["t_x"]
    rows = {}
    for label, res, sig, level, falling, meas, step in (
            ("RC .tran 0 3m 1m, V(out) 3 V düşüş", rc, "V(out)", 3.0, True, rc.measurements["t_fall"], 0),
            ("RC .tran 0 3m 1m, V(out) 3 V yükseliş", rc, "V(out)", 3.0, False, rc.measurements["t_rise"], 0),
            ("RC, ASCII .raw, V(out) 3 V düşüş", rc_ascii, "V(out)", 3.0, True, rc_ascii.measurements["t_fall"], 0),
            ("RC .step r 1k/2k, adım 2, V(out) 2,5 V yükseliş", rc_step, "V(out)", 2.5, False, step2, 1),
            ("Kırpıcı .tran 0 4m 2m, V(vout) 0 V düşüş", clip, "V(vout)", 0.0, True, clip.measurements["t_zc"], 0)):
        row = {"meas": meas}
        for arm, (x, y) in _axis_arms(res, sig, step).items():
            row[f"x0_{arm}"], row[f"x1_{arm}"] = float(x[0]), float(x[-1])
            row[f"err_{arm}"] = abs(float(_crossings(x, y, level, falling)[0]) - meas)
        rows[label] = row
    # Offset diğer analizlerde: analiz türüne göre dal gerekmediğinin kanıtı
    others = {kind: open_raw(ad.run_netlist(net, circuit_name=f"t3_{kind}", treat_as_text=True,
                                            summarize_raw=False).raw_path).get_raw_property("Offset")
              for kind, net in OTHER_ANALYSES.items()}
    x_new, y_new = _axis_arms(rc, "V(out)")["yeni"]
    return {"rc": rc, "rc_ascii": rc_ascii, "rc_step": rc_step, "clip": clip, "offset": offset, "rows": rows,
            "clip_arms": _axis_arms(clip, "V(vout)"), "others": {k: float(v) for k, v in others.items()},
            "rc_hand_err": float(np.max(np.abs(y_new - _rc_hand(x_new))))}


# ============================================================================ rapor
def build_report(m, elapsed):
    rep = SimulationReport(
        "Gemini incelemesi 4", "Steplenmiş .raw'ın okunamama nedeni, özet önizlemesi ve Tstart'lı zaman ekseni",
        "İki iddia ölçüldü; ikisi de belgelerimizin açık konularındandı. Tutanlar uygulandı. Ölçümde iki yeni sessiz "
        "hata çıktı ve düzeltildi: adımların tek adımda birleşmesi ve Tstart > 0'da kayık zaman ekseni.",
        meta=[("LTspice", LTSpiceAdapter().ltspice_version() or "?"), ("spicelib", m["spicelib"]),
              ("Makine", "i7-1255U, Windows 11"), ("Süre", f"{elapsed:.0f} sn")],
        disclaimer="Sayılar LTspice simülasyonu ve spicelib'in bu makinedeki davranışıdır; donanım ölçümü değildir.",
    )
    rep.footer = ("Yeniden üretmek için: `python examples/18_gemini_review_4.py`. Değişiklikler: `parser.open_raw` "
                  "(adım sayısı denetimi, başlıktan neden), `RawParser.summarize_file` (`.tran` önizlemesi zamanda eşit "
                  "aralıklı). Kayıtlar: `tests/data/ref26_step_dc_*` (examples/14 `stepraw`).")

    # ---- T1
    rows1 = m["t1"]
    by = {(r["label"], r["mode"]): {} for r in rows1}
    for r in rows1:
        by[(r["label"], r["mode"])][r["arm"]] = r
    table_rows = [(f"{label} ({mode})", {"old_ok": float(v["eski"]["ok"]), "new_ok": float(v["yeni"]["ok"]),
                                         "old_steps": float(v["eski"]["steps"]), "new_steps": float(v["yeni"]["steps"])})
                  for (label, mode), v in by.items()]
    old_ok = sum(r["ok"] for r in rows1 if r["arm"] == "eski")
    new_ok = sum(r["ok"] for r in rows1 if r["arm"] == "yeni")
    n_cases = len(rows1) // 2
    merged = [f"{r['label']} ({r['mode']})" for r in rows1 if r["arm"] == "eski" and r["merged"]]
    notes = [f"**{label} ({mode})** — eski: {v['eski']['outcome']} · yeni: {v['yeni']['outcome']}"
             for (label, mode), v in by.items() if v["eski"]["outcome"] != v["yeni"]["outcome"]]
    rep.add_study(Study(
        "T1", "Steplenmiş .raw okunamadığında open_raw doğru nedeni söylüyor mu?",
        question=("Gemini: steplenmiş ikili .raw'ın yanındaki .log silinmiş ya da taşınmışsa spicelib IndexError "
                  "veriyor, open_raw bunu 'Steplenmiş ASCII .raw okunamıyor' diye bildiriyor. Bu, examples/17 raporunun "
                  "açık konularında ölçülmemiş bir not olarak duruyordu."),
        method=("Aynı küçük devre (.step r 1k/3k, .dc, elle hesap: V(out) = V1·1k/(r + 1k)) LTspice'ta ikili ve "
                "`-ascii` biçimde, ayrıca 10 ns kenarlı darbe (.step ton, .tran) ikili koşturuldu. .raw geçici "
                "klasöre `x.raw` olarak kopyalandı; yanına kendi logu, hiç log, steplenmemiş bir koşumun (RC) logu, "
                "LTspice logu olmayan bir metin ya da adım sayısı farklı (2 ↔ 5 adım) bir koşumun logu kondu. Her durum tembel açılışla (read_trace, Bode) ve tek geçişle "
                "(özet, grafik) açılıp bütün adımların izi okundu. Tek değişken: düzeltme (eski kol: adım sayısı "
                "denetimi kapalı, eski hata metni)."),
        table=Table([Column("old_ok", "eski: doğru (1/0)", decimals=0), Column("new_ok", "yeni: doğru (1/0)", decimals=0),
                     Column("old_steps", "eski: okunan adım", decimals=0),
                     Column("new_steps", "yeni: okunan adım", decimals=0)], table_rows,
                    caption=("Doğru: okunabilen dosyada 2 adım ve elle hesaptaki değerler; okunamayanda hatanın asıl "
                             "nedeni söylemesi (.log yok, .log'da .step satırı yok, ASCII). Okunan adım 0: istisna.")),
        effects=[Effect("open_raw: adım sayısı denetimi, nedeni başlıktan", "parser.open_raw",
                        "nedeni doğru bildirilen ya da doğru okunan durum", float(old_ok), float(new_ok), "adet",
                        decimals=0, better="higher")],
        notes=notes,
    ))
    rep.add_runs("T1", [("bölücü, ikili", m["runs"][("dc", "binary")]), ("bölücü, ASCII", m["runs"][("dc", "ascii")]),
                        ("bölücü 5 adım, ikili", m["runs"][("dc5", "binary")]),
                        ("darbe, ikili", m["runs"][("pulse", "binary")])])

    # ---- T2
    rows2 = m["t2"]["rows"]
    rc = next(r for r in rows2 if "hand_old" in r)
    mid = next(r for r in rows2 if r["label"] == "takma anı, olay 2,5 ms V(c)")
    gear = next(r for r in rows2 if r["label"] == "takma anı, olay 2,5 ms, Gear V(c)")
    worst_density = max(r["densest"] for r in rows2)
    t2 = Study(
        "T2", "Özetin önizlemesi zaman eğrisini doğru veriyor mu?",
        question=("Gemini: 15 noktalık önizleme indeks aralıklı; değişken adımlı .tran'da noktaların %80'i bir kenara "
                  "yığılabilir, zamanda eşit aralıklı np.interp daha dengeli olur. Bu, PERFORMANCE §9 açık konularında "
                  "duruyordu."),
        method=("Önizleme zamansız verilir; okuyan (LLM) değerleri zamanda eşit aralıklı sanar. Ölçüt: bu varsayımla "
                "kurulan eğrinin (15 değer eşit aralığa yerleştirilip doğrusal birleştirilmiş) gerçek .raw izinden "
                "sapması, tepeden tepeye genliğe göre (RMS zaman ağırlıklı). Devreler: examples/14 RC (elle hesap var), "
                "examples/10 buck (azami adım 20 ns), takma anı çınlaması (12 V, 1 µH, 10 µF, 50 mΩ; `.tran 5m`, azami "
                "adım yok) olay 0 ms'de ve 2,5 ms'de. Tek değişken: önizlemenin zaman eksenini kullanması (eski kol: "
                "`time_weighted_mean_rms` devre dışı, önizleme indeks aralıklı)."),
        table=Table([Column("points", "nokta", decimals=0), Column("densest", "yoğun %10", "%", decimals=1),
                     Column("gap_min", "aralık min", "×", decimals=2), Column("gap_max", "aralık max", "×", decimals=2),
                     Column("rms_old", "hata eski", "%", decimals=2), Column("rms_new", "hata yeni", "%", decimals=2)],
                    [(r["label"], r) for r in rows2],
                    caption=("Yoğun %10: zaman penceresinin onda birine düşen en büyük nokta payı. Aralık: eski "
                             "önizlemenin ardışık iki noktası arasındaki süre, eşit aralığın katı. Hata: okuyanın "
                             "eğrisinin RMS sapması, tepeden tepeye genliğin yüzdesi. RC V(out) önizlemesinin elle "
                             f"hesaptan en büyük sapması: eski {format_value(rc['hand_old'], 'mV', 1e-3, 1)}, yeni "
                             f"{format_value(rc['hand_new'], 'mV', 1e-3, 3)}.")),
        effects=[Effect("önizleme zamanda eşit aralıklı", "RawParser.summarize_file (.tran)",
                        "RC V(out) önizlemesi, elle hesaptan en büyük sapma", rc["hand_old"], rc["hand_new"], "mV",
                        scale=1e-3, decimals=3),
                 Effect("önizleme zamanda eşit aralıklı", "RawParser.summarize_file (.tran)",
                        "takma anı (olay 2,5 ms) V(c), okuyanın eğrisinin RMS hatası / pp", mid["rms_old"],
                        mid["rms_new"], "%", decimals=2)],
        notes=[f"Gemini'nin %80'i görülmedi: en yoğun %10'luk sürede noktaların en çok "
               f"{format_value(worst_density, '%', decimals=1)}'i var. LTspice düzlükte adımı süre/1024 civarında "
               "tutuyor; kenarlar birkaç düzine nokta ekliyor.",
               (f"Yeni önizlemenin okuyanın eğrisini kötüleştirdiği iz: "
                f"{', '.join(r['label'] for r in rows2 if r['rms_new'] > r['rms_old']) or 'yok'} "
                f"({len(rows2)} iz). Darbe dizisi gibi 15 noktanın çözemediği dalga formlarında ikisi de kaba kalıyor; "
                "uç değerler zaten min/max alanlarında."),
               f"Gear ile aynı devre: yeni önizlemenin RMS hatası {format_value(gear['rms_new'], '%', decimals=2)} "
               f"(trapez {format_value(mid['rms_new'], '%', decimals=2)}), yönteme bağlı değil. Eskininki "
               f"{format_value(gear['rms_old'], '%', decimals=2)} (trapez {format_value(mid['rms_old'], '%', decimals=2)}): "
               "indeks aralıklı önizleme çözücünün nokta dağılımıyla değişiyor."],
    )
    d = m["t2"]["mid"]
    t2.charts.append(LineChart(
        "Takma anı (olay 2,5 ms) V(c): gerçek iz ve okuyanın önizlemeden kurduğu eğri",
        [Trace("gerçek (.raw)", d["t"], d["y"], emphasis=False),
         Trace("eski önizleme (indeks aralıklı)", d["t"], _reader_curve(d["t"], d["old"]), slot=2),
         Trace("yeni önizleme (zamanda eşit aralıklı)", d["t"], _reader_curve(d["t"], d["new"]), slot=1)],
        caption=(f"Eski önizlemenin {mid['early_old']}/15 noktası pencerenin ilk yarısında, gerisi olaydan sonraki "
                 "çınlamada. 6 V'un ilk geçildiği an: "
                 + ", ".join(f"{k} {format_value(v * 1e3, 'ms', decimals=3)}" for k, v in d["cross"].items()) + ".")))
    rep.add_study(t2)
    rep.add_runs("T2", list(m["t2"]["runs"].items()) + [("takma anı, olay 2,5 ms, Gear", m["t2"]["gear"])])

    # ---- T3
    ts = m["t3"]
    rows3 = ts["rows"]
    worst_old = max(r["err_eski"] for r in rows3.values())
    worst_new = max(r["err_yeni"] for r in rows3.values())
    t3 = Study(
        "T3", "Tstart > 0 olan .tran'da zaman ekseni doğru mu? (yeni bulgu)",
        question=("Test-reviewer önizleme ızgarasının kaydın ilk noktasından başladığını sınayan bir Tstart > 0 kaydı "
                  "istedi. Kayıt alınınca eksenin Tstart'tan değil 0'dan başladığı görüldü."),
        method=("LTspice Tstart > 0'da ekseni 0'dan yazıp kaymayı başlığın `Offset` alanına koyuyor; spicelib 1.6.3 "
                "uygulamıyor. Tanık LTspice'ın kendi `.meas WHEN` zamanı (mutlak). Devreler: examples/14 RC'si "
                "`.tran 0 3m 1m` ile (elle hesap var; ikili, `-ascii` ve `.step r 1k/2k`) ve diyot kırpıcı (1 kHz "
                "sinüs, 1N4148, `.tran 0 4m 2m 1u`, `.meas WHEN V(vout)=0 FALL=1`). Geçiş zamanı "
                "`raw_axis_values` ekseni ve izden doğrusal ara değerle. Tek değişken: `raw_axis_values`'ın Offset'i "
                "eklemesi (eski kol: Offset 0)."),
        table=Table([Column("meas", ".meas WHEN", "ms", 1e-3, 6),
                     Column("x0_eski", "eksen başı eski", "ms", 1e-3, 3),
                     Column("x0_yeni", "eksen başı yeni", "ms", 1e-3, 3),
                     Column("err_eski", "geçiş − .meas eski", "ms", 1e-3, 6),
                     Column("err_yeni", "geçiş − .meas yeni", "ns", 1e-9, 6)],
                    list(rows3.items()),
                    caption=(f"RC başlığında `Offset: {ts['offset']:g}`. Yeni eksende RC izinin elle hesaptan en büyük "
                             f"sapması {format_value(ts['rc_hand_err'], 'µV', 1e-6, 1)}.")),
        effects=[Effect("raw_axis_values: eksen + Offset", "grafik, read_trace (.tran, Tstart > 0)",
                        "izden bulunan geçiş zamanının LTspice .meas'inden farkı (en kötü)", worst_old, worst_new, "ms",
                        scale=1e-3, decimals=6)],
        notes=["Özetin ortalaması, RMS'i ve önizleme değerleri etkilenmiyor (yalnız süre farkları kullanılıyor); "
               "değişen grafik ve `read_trace` ekseni. ASCII ve steplenmiş .raw'da da aynı Offset, her adım 0'dan "
               "(tablo). Diğer analizlerde başlıktaki Offset: "
               + ", ".join(f"{k} {v:g}" for k, v in ts["others"].items())
               + "; bu yüzden analiz türüne göre dal yok."],
    )
    arms = ts["clip_arms"]
    t3.charts.append(LineChart(
        "Diyot kırpıcı V(vout): eski ve yeni zaman ekseni",
        [Trace("eski eksen (Offset yok)", arms["eski"][0], arms["eski"][1], emphasis=False),
         Trace("yeni eksen (eksen + Offset)", arms["yeni"][0], arms["yeni"][1], slot=1)],
        caption=("Aynı iz; eski eksende 0-2 ms, yenide simülasyonun kaydettiği 2-4 ms (`.tran 0 4m 2m`). LTspice'ın "
                 "0 V düşüş zamanı " + format_value(rows3["Kırpıcı .tran 0 4m 2m, V(vout) 0 V düşüş"]["meas"], "ms", 1e-3, 4)
                 + ".")))
    rep.add_study(t3)
    rep.add_runs("T3", [("RC, .tran 0 3m 1m", ts["rc"]), ("RC, ASCII", ts["rc_ascii"]),
                        ("RC, .step r 1k/2k", ts["rc_step"]), ("diyot kırpıcı", ts["clip"])])

    # ---- özet
    rep.add_tile("Steplenmiş .raw: doğru neden", f"{old_ok}/{n_cases} → {new_ok}/{n_cases}", "durum × açılış (T1)")
    rep.add_tile("Sessiz adım birleşmesi", f"{len(merged)} → 0", "steplenmemiş koşumun .log'u (T1)")
    rep.add_tile("Önizleme, RC elle hesaptan sapma", f"{format_value(rc['hand_old'], 'mV', 1e-3, 0)} → "
                 f"{format_value(rc['hand_new'], 'mV', 1e-3, 2)}", "T2")
    rep.add_tile("En yoğun %10 süredeki nokta payı", format_value(worst_density, "%", decimals=1),
                 "Gemini: %80'e kadar (T2)")
    rep.add_decision("good", "Uygulandı", "open_raw steplenmiş .raw'da adımları .log'dan okuyamazsa açılışta, nedeni "
                     "söyleyerek hata veriyor: .log yok, .log'da .step satırı yok ya da ASCII (T1). `.tran` özetinin "
                     "önizlemesi zamanda eşit aralıklı (T2).")
    rep.add_decision("info", "Değişmedi", "`.ac` ve `.dc` önizlemesi indeks aralıklı kalıyor: `.ac dec` noktaları zaten "
                     "logaritmik eşit aralıklı, `.dc` taraması doğrusal.")
    rep.add_decision("good", "Uygulandı (yeni bulgu)", "`raw_axis_values` başlıktaki Offset'i ekliyor: Tstart > 0 "
                     "olan `.tran`'da grafik ve `read_trace` ekseni LTspice'ın `.meas` zamanlarıyla aynı (T3).")
    rep.add_finding(Finding(
        "Steplenmemiş bir koşumun .log'u adımları sessizce birleştiriyor",
        "spicelib 1.6.3 adımları yanındaki .log'un `.step` satırlarından alıyor. .log okunabilir ama `.step` satırı "
        "içermiyorsa hata vermeden bütün adımları tek adım sayıyor: bölücüde V(out) = 0, 0,5, 1, 0, 0,25, 0,5 tek dizi.",
        "LTspice tek değerli `.step`'i reddettiği için steplenmiş .raw en az iki adımdır; open_raw iki adımdan azını "
        "reddediyor.", "Eski kolda birleşen durumlar: " + (", ".join(merged) or "yok") + ".", "serious",
        "sessiz yanlış sonuç"))
    clip_row = ts["rows"]["Kırpıcı .tran 0 4m 2m, V(vout) 0 V düşüş"]
    rep.add_finding(Finding(
        "Tstart > 0'da .raw'ın zaman ekseni 0'dan başlıyor",
        "LTspice kaydı Tstart'tan başlatıyor ama ekseni 0'dan yazıp kaymayı başlığın `Offset` alanına koyuyor; ADI "
        "belgesi bu alanı anlatmıyor, spicelib 1.6.3 uygulamıyor. Grafik ve `read_trace` ekseni Tstart kadar kayıktı.",
        "`raw_axis_values` eksen + Offset döndürüyor (bütün eksen okumaları oradan geçiyor). Offset diğer analizlerde 0.",
        f"Kırpıcıda izden bulunan geçiş .meas'ten {format_value(clip_row['err_eski'], 'ms', 1e-3, 3)} → "
        f"{format_value(clip_row['err_yeni'], 'ns', 1e-9, 6)} (T3).", "serious", "sessiz yanlış sonuç"))
    rep.add_finding(Finding(
        "İki bulgu da bizim açık konularımızdan",
        "Önizleme PERFORMANCE §9'da, .log/ASCII notu examples/17 raporunun açık konularında duruyordu.",
        "İkincisi orada ölçülmemiş bir tahmindi; ölçünce tembel açılışta hatanın hiç açıklama taşımadığı ve sessiz "
        "birleşme çıktı.", "Bu rapor T1, T2.", "warn", "inceleme"))
    ends = [r for k, r in m["t2"]["runs"].items() if k.startswith("takma")]
    worst_end = max(abs(r.measurements["vend"] - 12.0) for r in ends)
    rep.add_check("Takma anı V(c) pencere sonunda 12 V'a oturdu", format_value(worst_end, "µV", 1e-6, 2),
                  "good" if worst_end < 1e-3 else "warn", "`.meas FIND V(c) AT 5m`, iki olay zamanında.")
    gear_method = log_facts(m["t2"]["gear"])["method"]
    rep.add_check("Gear ile önizleme sonucu", f"{format_value(gear['rms_new'], '%', decimals=2)} / "
                  f"{format_value(mid['rms_new'], '%', decimals=2)}",
                  "good" if abs(gear["rms_new"] - mid["rms_new"]) < 1 and gear_method == "Gear" else "warn",
                  f"Yeni önizlemenin RMS hatası Gear / trapez. Logda `method = {gear_method}`.")
    rep.assumptions = [
        ("Okuyan", "Önizlemeyi zaman bilgisi olmadan alır ve değerleri zamanda eşit aralıklı sanar (to_dict alanı "
                   "`sampled_trend_preview`, zaman yok)."),
        ("Takma anı devresi", "Kablo ve kondansatör idealleştirilmiş (Rser yok); amaç değişken adımlı bir iz, takma "
                              "anı tasarımı değil (o examples/15)."),
        ("spicelib", "Davranış 1.6.3'te ölçüldü; sürüm değişince T1 yeniden koşulmalı."),
    ]
    rep.open_items = [
        "Verideki adımlardan az .step satırlı başka bir koşumun .log'u (T1, 5 adım + 2 satır): tek geçişte hata "
        "uyuşmazlığı söylüyor; tembel açılışta açılış geçiyor ve spicelib'in IndexError'ı ilk izde açıklamasız "
        "çıkıyor. Açılışta yakalamak verinin adım sınırlarını okumayı gerektirir (tembel açılışın amacına ters).",
    ]
    return rep


def main():
    import importlib.metadata as md

    WS.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    m = {"spicelib": md.version("spicelib")}
    runs = {}
    for fmt in ("binary", "ascii"):
        ad = LTSpiceAdapter(workspace_dir=_fresh(f"runs_{fmt}"), raw_format=fmt, solver_threads=1)
        runs[("dc", fmt)] = ad.run_netlist(STEP_DC, circuit_name=f"stepdc_{fmt}", treat_as_text=True,
                                           summarize_raw=False)
    ad = LTSpiceAdapter(workspace_dir=_fresh("runs"), solver_threads=1)  # ikili (varsayılan)
    runs[("dc5", "binary")] = ad.run_netlist(STEP5, circuit_name="stepdc5", treat_as_text=True, summarize_raw=False)
    runs[("pulse", "binary")] = ad.run_netlist(MEAN, circuit_name="pulse", treat_as_text=True)
    runs[("rc", "binary")] = ad.run_netlist(RC, circuit_name="rc", treat_as_text=True)
    m["runs"] = runs
    print("T1 steplenmiş .raw...", flush=True)
    m["t1"] = t1_stepped(runs)
    for r in m["t1"]:
        print(f"   {r['arm']:4s} {r['label']:38s} {r['mode']:9s} doğru={int(r['ok'])} {r['outcome'][:90]}")
    print("T2 önizleme...", flush=True)
    m["t2"] = t2_preview(ad, runs[("rc", "binary")])
    for r in m["t2"]["rows"]:
        print(f"   {r['label']:42s} n={r['points']:6d} yoğun%10={r['densest']:5.1f}% aralık {r['gap_min']:.2f}-"
              f"{r['gap_max']:.2f} RMS {r['rms_old']:.2f}→{r['rms_new']:.2f} %pp"
              + (f" elle {r['hand_old']:.4f}→{r['hand_new']:.5f} V" if "hand_old" in r else ""))
    print("T3 Tstart...", flush=True)
    m["t3"] = t3_tstart(ad)
    for label, r in m["t3"]["rows"].items():
        print(f"   {label:42s} .meas {r['meas']:.9g} eksen başı {r['x0_eski']:.4g}→{r['x0_yeni']:.4g} "
              f"|geçiş − .meas| {r['err_eski']:.3g}→{r['err_yeni']:.3g} s")
    elapsed = time.perf_counter() - t0
    rep = build_report(m, elapsed)
    save_report(rep, report_path("18_gemini_review_4.html"))
    shutil.rmtree(WS, ignore_errors=True)
    print(f"{elapsed:.0f} sn")


if __name__ == "__main__":
    main()
