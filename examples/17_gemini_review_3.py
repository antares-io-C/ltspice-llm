"""
Örnek 17: Üçüncü Gemini incelemesinin (2026-10-06) beş iddiası, LTspice 26.1.1 ile ölçülerek.

- T1 Kota temizliği elde tutulan sonucun ``_bode.png``'sini siliyor mu? (gerçek .ac koşumu + plot_bode_diagram)
- T2 Dalga formu özetinin ortalaması ve RMS'i ``.meas AVG``/``RMS`` ile aynı mı? (darbe kaynağı, examples/10 buck)
- T3 LTspice kullanıcı klasöründeki (User Files) sembolü nasıl buluyor; find_symbol aynısını buluyor mu?
- T4 Netlistte tek bağlantılı düğüm: examples/16 T6 devreleri ve ADI örnek şemalarının netlistleri (LTspice'ın
  ``NC_xx`` adlandırması bağımsız tanık).

Her testte "eski" kol, düzeltmenin monkeypatch'le geri alınmasıdır (tek değişken). Gerçek ``LTspice.ini``'ye
dokunulmaz: T3 LTspice'ı ``-ini <geçici dosya>`` ile çalıştırır ve dosyanın değişmediğini denetler.

    python examples/17_gemini_review_3.py           # ~3 dk
    python examples/17_gemini_review_3.py --hizli   # T4'te ADI örneklerinin 1/4'ü

Rapor: ``reports/17_gemini_review_3.html``. Ayrıntı: docs/LESSONS_LEARNED_PERFORMANCE.md §11.
"""

import hashlib
import os
import re
import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

import numpy as np

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

import importlib  # noqa: E402

import ltspice_llm.adapter as adapter_mod  # noqa: E402
import ltspice_llm.parser as parser_mod  # noqa: E402
import ltspice_llm.symbols as symbols_mod  # noqa: E402
from ltspice_llm import AutonomousCircuitAgent, LTSpiceAdapter, handle_tool_call  # noqa: E402
from ltspice_llm.netlist import _logical_lines, single_connection_nodes  # noqa: E402
from ltspice_llm.parser import RawParser  # noqa: E402
from ltspice_llm.reference import ltspice_data_dir  # noqa: E402
from ltspice_llm.report import (  # noqa: E402
    BarChart, Column, Effect, Finding, LineChart, SimulationReport, Study, Table, Trace, format_value, read_trace,
)
from ltspice_llm.symbols import find_symbol, library_symbol_dir  # noqa: E402
from study_helpers import report_path, save_report  # noqa: E402

WS = Path(tempfile.gettempdir()) / "ltspice_llm_ex17"
BUCK = importlib.import_module("10_performance_fast_paths").BUCK
MEAN = importlib.import_module("14_record_flow_fixtures").MEAN  # tests/data/ref26_mean_pulse.* ile aynı devre
DRC_CASES = importlib.import_module("16_process_and_memory_hardening").DRC_CASES
AC_RC = "* rc ac\nV1 in 0 AC 1\nR1 in out 1k\nC1 out 0 159n\n.ac dec 20 10 100k\n.end\n"


def _fresh(name):
    folder = WS / name
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    return folder


# ============================================================================ T1 kota
def _old_run_names(file_name):
    return (file_name.split(".")[0].lower(),)  # eski gruplama: ilk noktaya kadarki ad


def t1_quota():
    out = {}
    for arm in ("eski", "yeni"):
        real = adapter_mod._run_names
        if arm == "eski":
            adapter_mod._run_names = _old_run_names
        try:
            ad = LTSpiceAdapter(workspace_dir=_fresh(f"t1_{arm}"))
            old = ad.run_netlist(AC_RC, circuit_name="old", treat_as_text=True)
            plot_old = handle_tool_call("plot_bode_diagram", {"circuit_name": "old"}, adapter=ad)
            del old  # sonuç tutulmuyor
            for p in ad.workspace_dir.glob("old*"):
                os.utime(p, (time.time() - 300, time.time() - 300))
            live = ad.run_netlist(AC_RC, circuit_name="filt", treat_as_text=True)
            plot = handle_tool_call("plot_bode_diagram", {"circuit_name": "filt"}, adapter=ad)
            before = sorted(p.name for p in ad.workspace_dir.iterdir() if p.is_file())
            removed = ad.cleanup_workspace(max_total_mb=0)
            after = sorted(p.name for p in ad.workspace_dir.iterdir() if p.is_file())
            out[arm] = {"before": before, "after": after, "removed": removed, "live": live,
                        "plot_status": (plot_old["status"], plot["status"]),
                        "bode_kept": "filt_bode.png" in after, "live_kept": live.raw_path.exists()}
        finally:
            adapter_mod._run_names = real
    return out


# ============================================================================ T2 ortalama
def _summaries(raw_path, arm):
    real = parser_mod.time_weighted_mean_rms
    if arm == "eski":
        parser_mod.time_weighted_mean_rms = lambda t, y: None  # eski: örneklerin aritmetik ortalaması
    try:
        return RawParser.summarize_file(raw_path)
    finally:
        parser_mod.time_weighted_mean_rms = real


def t2_mean():
    ad = LTSpiceAdapter(workspace_dir=_fresh("t2"), solver_threads=1)
    pulse = ad.run_netlist(MEAN, circuit_name="pulse", treat_as_text=True)
    buck_net = BUCK.replace(".end\n", ".meas tran vsw_avg AVG V(vsw)\n.meas tran vsw_rms RMS V(vsw)\n"
                                      ".meas tran vout_avg_all AVG V(vout)\n.meas tran il_rms RMS I(L1)\n.end\n")
    buck = ad.run_netlist(buck_net, circuit_name="buck", treat_as_text=True)
    rows = []
    old_p, new_p = _summaries(pulse.raw_path, "eski"), _summaries(pulse.raw_path, "yeni")
    for step, ton in ((1, 1e-6), (2, 3e-6)):
        for sig, key in (("V(in)", "in"), ("V(out)", "out")):
            label = f"darbe {sig}, ton {ton * 1e6:.0f} µs"
            k = f"{sig} [step {step}]"
            meas = {name: pulse.stepped_measurements[f"{key}_{name}"][step - 1][f"{key}_{name}"]
                    for name in ("avg", "rms")}
            rows.append((label, "AVG", meas["avg"], old_p[k].mean_val, new_p[k].mean_val,
                         5 * (ton + 10e-9) / 10e-6 if key == "in" else None))
            rows.append((label, "RMS", meas["rms"], old_p[k].rms_val, new_p[k].rms_val,
                         float(np.sqrt(25 * (ton + 2 * 10e-9 / 3) / 10e-6)) if key == "in" else None))
    old_b, new_b = _summaries(buck.raw_path, "eski"), _summaries(buck.raw_path, "yeni")
    for sig, kind, meas in (("V(vsw)", "AVG", "vsw_avg"), ("V(vsw)", "RMS", "vsw_rms"),
                            ("V(vout)", "AVG", "vout_avg_all"), ("I(L1)", "RMS", "il_rms")):
        attr = "mean_val" if kind == "AVG" else "rms_val"
        rows.append((f"buck {sig}", kind, buck.measurements[meas], getattr(old_b[sig], attr),
                     getattr(new_b[sig], attr), None))
    t_buck = read_trace(buck, "V(vsw)")[0]
    dt = np.diff(t_buck)
    # Darbe, adım 1 (ton 1 µs): sapmanın nedeni. Yüksek seviyenin (V(in) > 2,5 V) nokta payı ve süre payı, adımlar
    raw = parser_mod.open_raw(pulse.raw_path, traces=parser_mod.ALL_TRACES)
    t = np.real(parser_mod.raw_axis_values(raw, 0)).astype(float)
    v = np.real(parser_mod.raw_wave(raw, "V(in)", 0)).astype(float)
    step = np.diff(t)
    high_interval = (v[:-1] + v[1:]) / 2 > 2.5
    return {"pulse": pulse, "buck": buck, "rows": rows, "dt_ratio": float(dt.max() / dt[dt > 0].min()),
            "points": len(t_buck), "pulse_points": len(t), "high_points": float(np.mean(v > 2.5)),
            "high_time": float(step[high_interval].sum() / (t[-1] - t[0])),
            "step_min": float(step[step > 0].min()), "step_max": float(step.max())}


# ============================================================================ T3 semboller
SYMBOL_CASES = [  # (sembolün yeri, .asc'deki ad, açıklama)
    ("user/zz_usersym.asy", "zz_usersym", "User Files, yalın ad"),
    ("user/sub/zz_subsym.asy", "zz_subsym", "User Files alt klasörü, yalın ad"),
    ("user/sub/zz_subsym.asy", "sub\\\\zz_subsym", "User Files alt klasörü, göreli yol"),
    ("user/sym/zz_usersym2.asy", "zz_usersym2", "User Files/sym, yalın ad"),
    ("user/lib/sym/zz_userlib.asy", "zz_userlib", "User Files/lib/sym, yalın ad"),
    ("sch/local/zz_schsub.asy", "zz_schsub", "şemanın alt klasörü, yalın ad"),
    ("sch/local/zz_schsub.asy", "local\\\\zz_schsub", "şemanın alt klasörü, göreli yol"),
]


def _ini_hash():
    path = Path(os.environ["APPDATA"]) / "LTspice.ini"
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def t3_symbols():
    from ltspice_llm.schematic import decode_asc_bytes

    base = _fresh("t3")
    res_asy = (library_symbol_dir() / "res.asy").read_bytes()
    for rel, _, _ in SYMBOL_CASES:
        path = base.joinpath(*rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(res_asy)
    real_ini = Path(os.environ["APPDATA"]) / "LTspice.ini"
    data = real_ini.read_bytes()
    text, encoding = decode_asc_bytes(data)
    lines = [f"UserCmpDir={base / 'user'}" if ln.startswith("UserCmpDir=") else ln for ln in text.split("\r\n")]
    appdata = base / "appdata"
    appdata.mkdir()
    bom = data[:2] if data[:2] in (b"\xff\xfe", b"\xfe\xff") else b""
    (appdata / "LTspice.ini").write_bytes(bom + "\r\n".join(lines).encode(encoding))
    ini_before = _ini_hash()

    real_run = adapter_mod.subprocess.run

    def with_ini(cmd, *args, **kwargs):  # LTspice -ini <geçici ayar> -netlist <şema>
        return real_run([cmd[0], "-ini", str(appdata / "LTspice.ini"), *cmd[1:]], *args, **kwargs)

    ad = LTSpiceAdapter(workspace_dir=base / "ws")
    rows = []
    real_appdata, real_user_dir = os.environ["APPDATA"], symbols_mod.user_symbol_dir
    adapter_mod.subprocess.run = with_ini
    try:
        for i, (_, name, label) in enumerate(SYMBOL_CASES):
            asc = base / "sch" / f"t{i}.asc"
            asc.write_bytes(("Version 4\nSHEET 1 880 680\n"
                             f"SYMBOL {name} 96 96 R0\nSYMATTR InstName R1\nSYMATTR Value 1k\n"
                             "FLAG 112 192 0\nTEXT 0 300 Left 2 !.op\n").encode("cp1252"))
            try:
                net = ad.schematic_to_netlist(asc)
                ltspice = any(line.startswith("R1 ") for line in net.splitlines())
            except RuntimeError:
                ltspice = False
            os.environ["APPDATA"] = str(appdata)
            try:
                new = find_symbol(name, [base / "sch"]) is not None
                symbols_mod.user_symbol_dir = lambda: None  # eski: kullanıcı klasörü aranmıyordu
                old = find_symbol(name, [base / "sch"]) is not None
            finally:
                os.environ["APPDATA"] = real_appdata
                symbols_mod.user_symbol_dir = real_user_dir
            rows.append({"label": label, "name": name.replace("\\\\", "\\"), "ltspice": ltspice, "old": old,
                         "new": new})
    finally:
        adapter_mod.subprocess.run = real_run
    return {"rows": rows, "ini_unchanged": _ini_hash() == ini_before, "encoding": encoding, "bom": bool(bom)}


# ============================================================================ T4 tek bağlantılı düğüm
def _nc_nodes(net):
    """LTspice'ın kendi tanığı: üst düzey eleman satırlarında netlisterın bağlantısız pine verdiği NC_xx."""
    depth, found = 0, set()
    for line in _logical_lines(net):
        head = line.split()[0].lower()
        if head == ".subckt":
            depth += 1
        elif head.startswith(".ends"):
            depth -= 1
        elif not depth and not head.startswith("."):
            found.update(t.lower() for t in line.split()[1:] if re.fullmatch(r"nc_\d+", t, re.I))
    return found


def _token_counts(net):
    """
    İkinci, kaba sayım: üst düzey eleman satırlarında (eleman adı ve {ifade} dışında) bir adın kaç kez geçtiği.
    İşaretlenen bir düğüm burada 1'den fazla geçiyorsa kural bir bağlantıyı kaçırmış olabilir. {...} atlanır:
    ``V4 Refin 0 {VREF}``'teki VREF parametredir (AD4081.asc); ifadede V(x) ile okunan düğüm zaten işaretlenmez.
    """
    depth, counts = 0, Counter()
    for line in _logical_lines(net):
        head = line.split()[0].lower()
        if head == ".subckt":
            depth += 1
        elif head.startswith(".ends"):
            depth -= 1
        elif not depth and not head.startswith("."):
            fields = re.sub(r"\{[^}]*\}", " ", " ".join(line.split()[1:]))
            counts.update(t.lower() for t in re.split(r"[\s(),=]+", fields) if t)
    return counts


def t4_floating(fast):
    ad = LTSpiceAdapter(workspace_dir=_fresh("t4"), solver_threads=1)
    drc = {label: ad.run_netlist(net, circuit_name=f"t4_{i}", treat_as_text=True, summarize_raw=False)
           for i, (label, net) in enumerate(DRC_CASES.items())}
    work = _fresh("t4_examples")
    shutil.copytree(ltspice_data_dir() / "examples", work / "examples")  # kurulum klasörüne yazılmaz
    all_asc = sorted((work / "examples").rglob("*.asc"))
    edu = [p for p in all_asc if "Educational" in p.parts]
    app = [p for p in all_asc if "Educational" not in p.parts][:: 80 if fast else 20]
    stats = Counter()
    named, missed_files, errors = [], [], 0
    t0 = time.perf_counter()
    for asc in (edu[::4] if fast else edu) + app:
        try:
            net = ad.schematic_to_netlist(asc, timeout=30)
        except RuntimeError:
            errors += 1
            continue
        # Netlist sırası: küme sırası dizenin hash'ine bağlı, "adlı ağ" notu koşumdan koşuma değişiyordu
        flagged = [d["node"] for d in single_connection_nodes(net)]
        nc = _nc_nodes(net)
        stats["netlist"] += 1
        stats["nc"] += len(nc)
        stats["missed"] += len(nc - set(flagged))
        stats["skipped_include"] += bool(re.search(r"^\.inc(?:lude)?\b", net, re.I | re.M))
        if nc - set(flagged):
            missed_files.append(asc.name)
        counts = _token_counts(net)
        for node in flagged:
            kind = ("nc" if re.fullmatch(r"nc_\d+", node) else "mp" if re.fullmatch(r"mp_\d+", node)
                    else "auto" if re.fullmatch(r"n\d{3,}", node) else "named")
            stats[f"flag_{kind}"] += 1
            stats["recount_not_one"] += counts[node] != 1
            if kind == "named":
                named.append(f"{asc.relative_to(work / 'examples').as_posix()}: {node}")
        stats["with_flags"] += bool(flagged)
    shutil.rmtree(work, ignore_errors=True)
    return {"drc": drc, "stats": dict(stats), "named": named, "missed_files": missed_files, "errors": errors,
            "seconds": time.perf_counter() - t0, "sample": f"{len(edu[::4] if fast else edu)} Educational + "
                                                           f"{len(app)} Applications (her {80 if fast else 20}. şema)"}


# ============================================================================ rapor
def build_report(m, elapsed, fast):
    rep = SimulationReport(
        "Gemini incelemesi 3", "Kota, özet ortalaması, sembol arama, tek bağlantılı düğüm, ajan araçları",
        "Beş iddia LTspice 26.1.1 ile ölçüldü; tutanlar uygulandı. İkisi belgelerimizdeki açık konulardan alınmıştı.",
        meta=[("LTspice", LTSpiceAdapter().ltspice_version() or "?"), ("Makine", "i7-1255U, Windows 11"),
              ("Süre", f"{elapsed:.0f} sn")],
        disclaimer="Sayılar LTspice simülasyonu ve bu makinedeki dosya davranışıdır; donanım ölçümü değildir.",
    )
    rep.footer = ("Yeniden üretmek için: `python examples/17_gemini_review_3.py`. Değişiklikler: `adapter._run_names`, "
                  "`parser.time_weighted_mean_rms`, `reference.ltspice_user_dir` ve `symbols.find_symbol`, "
                  "`netlist.single_connection_nodes` (run_netlist uyarısı), `AutonomousCircuitAgent(include_extended=)`.")

    # ---- T1
    q = m["t1"]
    rep.add_study(Study(
        "T1", "Kota, elde tutulan sonucun Bode grafiğini siliyor mu?",
        question=("Gemini: kota dosyaları ilk noktaya kadarki adla grupluyor; `filt_bode.png`'nin anahtarı `filt_bode`, "
                  "elde tutulan `filt` koşumuyla eşleşmiyor ve grafik silinir."),
        method=("Gerçek .ac koşumu (RC, 1 kHz kutup) ve `plot_bode_diagram`: tutulmayan `old` (300 sn eski) ve "
                "elde tutulan `filt`, ikisinin de `_bode.png`'si var. `cleanup_workspace(max_total_mb=0)` silinebilen "
                "her şeyi siler. Tek değişken: dosyanın koşuma bağlanması (`adapter._run_names`; eski = ilk noktaya "
                "kadarki ad)."),
        table=Table([Column("removed", "silinen dosya", decimals=0), Column("bode", "filt_bode.png kaldı (1/0)",
                                                                             decimals=0),
                     Column("live", "filt.raw kaldı (1/0)", decimals=0)],
                    [(arm, {"removed": float(v["removed"]), "bode": float(v["bode_kept"]),
                            "live": float(v["live_kept"])}) for arm, v in q.items()],
                    caption="Kalan dosyalar: " + " · ".join(f"**{arm}**: {', '.join(v['after'])}"
                                                            for arm, v in q.items())),
        effects=[Effect("_run_names (_bode.png koşumuna bağlanır)", "cleanup_workspace / kota",
                        "elde tutulan sonucun silinen dosyası", float(not q["eski"]["bode_kept"]),
                        float(not q["yeni"]["bode_kept"]), "adet", decimals=0)],
        notes=["`x_bode.png` `x_bode` adlı devrenin dalga formu grafiği de olabilir: iki ad da sahibi sayılır, biri "
               "elde tutuluyorsa silinmez (tests/test_robustness.py)."],
    ))
    rep.add_runs("T1", [("filt (.ac)", q["yeni"]["live"])])

    # ---- T2
    mean = m["t2"]
    rows2 = []
    for label, kind, meas, old, new, _ in mean["rows"]:
        rows2.append((f"{label} {kind}", {"meas": meas, "old": old, "new": new,
                                          "old_err": (old / meas - 1) * 100, "new_err": (new / meas - 1) * 100}))
    worst_old = max(abs(r[1]["old_err"]) for r in rows2)
    worst_new = max(abs(r[1]["new_err"]) for r in rows2)
    hand_rows = [(f"{label} {kind}", new / hand - 1) for label, kind, _, _, new, hand in mean["rows"] if hand]
    t2 = Study(
        "T2", "Özetteki ortalama ve RMS LTspice'ın .meas'iyle aynı mı?",
        question=("Gemini (ve examples/16 T7): LTspice'ın zaman adımı değişken; özet örneklerin aritmetik ortalaması "
                  "olduğu için hızlı kenarlar fazla ağırlık alır. Gemini'nin %3,0'ı bizim T7 ölçümümüzdü; '%3-10' "
                  "aralığının kaynağı yok."),
        method=("İki devre: 10 ns kenarlı darbe (elle hesap var, iki .step adımı) ve examples/10 buck (V(vsw), "
                "V(vout), I(L1)). Aynı .raw'ın özeti iki kez: eski (aritmetik) ve yeni (doğrusal ara değerin zaman "
                "integrali; RMS'te Δt·(a²+ab+b²)/3). Beklenen değer LTspice'ın `.meas AVG`/`RMS`'i (tüm pencere)."),
        table=Table([Column("meas", ".meas", decimals=5), Column("old", "özet (eski)", decimals=5),
                     Column("new", "özet (yeni)", decimals=5), Column("old_err", "eski sapma", "%", decimals=3),
                     Column("new_err", "yeni sapma", "%", decimals=4)], rows2,
                    caption=(f"Buck'ta en büyük/en küçük zaman adımı oranı {mean['dt_ratio']:.3g} "
                             f"({mean['points']} nokta). Darbe V(in) elle hesaba göre yeni özetin sapması: "
                             + ", ".join(f"{k}: {v:.1e}" for k, v in hand_rows) + ".")),
        charts=[BarChart("Özetin .meas'ten mutlak sapması (en kötü)", [("eski (aritmetik)", worst_old),
                                                                       ("yeni (zaman ağırlıklı)", worst_new)],
                         unit="%", decimals=3, highlight="yeni (zaman ağırlıklı)")],
        effects=[Effect("time_weighted_mean_rms", "RawParser.summarize_file (.tran)",
                        "en kötü |özet − .meas| / .meas", worst_old, worst_new, "%", decimals=4)],
    )
    x, y = read_trace(mean["pulse"], "V(in)")
    t2.charts.append(LineChart(
        "Darbe V(in), ilk periyot", [Trace("V(in)", x, y)], x_range=(0, 1.2e-6),
        caption=(f"Adım 1: {mean['pulse_points']} nokta; adım düzlükte "
                 f"{format_value(mean['step_max'], 'ns', 1e-9, 1)}, kenarda "
                 f"{format_value(mean['step_min'], 'ps', 1e-12, 2)}'ye iniyor. Kenar çevresinde sıklaşan noktalar "
                 "çoğunlukla 5 V köşelerinde: yüksek seviye sürenin "
                 f"{format_value(mean['high_time'] * 100, '%', decimals=1)}'i, noktaların "
                 f"{format_value(mean['high_points'] * 100, '%', decimals=1)}'i. Aritmetik ortalamanın fazlası "
                 "buradan geliyor.")))
    rep.add_study(t2)
    rep.add_runs("T2", [("darbe (.step)", mean["pulse"]), ("buck", mean["buck"])])

    # ---- T3
    s = m["t3"]
    rep.add_study(Study(
        "T3", "LTspice kullanıcı klasöründeki sembolü nasıl buluyor?",
        question=("Gemini: ADI SCHEMATIC-REFERENCE 'Default Search Priority' 3. sıradaki User Files klasörünü "
                  "(varsayılan Documents\\LTspice) find_symbol aramıyor."),
        method=("Kullanıcı klasörü LTspice'ın ayar dosyasındaki `UserCmpDir`. Gerçek ayar dosyasına dokunmamak için "
                "kopyası geçici klasörü gösterecek biçimde yazıldı ve LTspice `-ini <kopya> -netlist` ile çalıştırıldı "
                f"(kopya {s['encoding']}{', BOM' if s['bom'] else ''}). Her satırda `res.asy`'nin kopyası başka bir "
                "yerde; LTspice netlisti üretiyorsa sembolü bulmuştur. find_symbol aynı ayar dosyasıyla (APPDATA) "
                "çağrıldı; eski kol kullanıcı klasörünü aramaz."),
        table=Table([Column("lt", "LTspice buldu (1/0)", decimals=0), Column("old", "find_symbol eski", decimals=0),
                     Column("new", "find_symbol yeni", decimals=0)],
                    [(f"{r['label']} ({r['name']})", {"lt": float(r["ltspice"]), "old": float(r["old"]),
                                                      "new": float(r["new"])}) for r in s["rows"]],
                    caption=("Gerçek LTspice.ini ölçüm boyunca değişmedi: "
                             f"{'evet' if s['ini_unchanged'] else 'HAYIR'}. Şemanın alt klasörü satırları "
                             "şema klasörü search_dirs olarak verildiğinde.")),
        notes=["Alt klasörler yalnızca göreli yolun kökü: ADI'nin '(and subfolders)' ifadesi yalın adla aramak "
               "anlamına gelmiyor. find_symbol verilen dizinlerde (search_dirs) yalın adı alt klasörlerde de "
               "arıyor; LTspice şemanın alt klasöründe aramıyor (tabloda son iki satır). Değiştirilmedi: mevcut "
               "testler dizini kütüphane gibi kullanıyor (yalın ad alt klasörde bulunmalı); karar kullanıcıda."],
    ))

    # ---- T4
    f = m["t4"]
    st = f["stats"]
    rows4 = [(label, {"ok": float(r.success),
                      "lt": float(len([w for w in r.warnings if "WARNING" in w])),
                      "new": float(any("tek bir pine" in w for w in r.warnings))}) for label, r in f["drc"].items()]
    named = f["named"]
    rep.add_study(Study(
        "T4", "Netlistteki tek bağlantılı düğüm yakalanıyor mu, yanlış alarm veriyor mu?",
        question=("Gemini: elle yazılmış netlistte bir ucu boşta kalan eleman LTspice'tan başarılı döner. Bu, "
                  "examples/16 T6'da ölçülüp ertelenmişti."),
        method=("Kural `netlist.single_connection_nodes`: eleman başına düğüm sayısı ADI CIRCUIT-ELEMENTS-REFERENCE "
                "sözdiziminden; Q/M'nin 4. alanı, POLY denetimi ve ifadedeki V(x) bağlantı sayılır ama "
                "işaretlenmez; .subckt gövdesi, toprak/GND, .global ve $G_ atlanır; .include varsa denetim yok. "
                f"Yanlış alarm için ADI örnek şemaları ({f['sample']}) LTspice netlisterıyla çevrildi; netlisterın "
                "bağlantısız pine verdiği `NC_xx` bağımsız tanık."),
        table=Table([Column("ok", "LTspice başarılı (1/0)", decimals=0), Column("lt", "LTspice uyarısı", decimals=0),
                     Column("new", "yeni uyarı (1/0)", decimals=0)], rows4,
                    caption="examples/16 T6 devreleri, run_netlist ile."),
        charts=[BarChart("ADI örnekleri: işaretlenen düğümler", [
            ("NC_xx (LTspice: bağlantısız pin)", float(st.get("flag_nc", 0))),
            ("MP_xx (alt devrenin boşta pini)", float(st.get("flag_mp", 0))),
            ("N### (tek pinli adsız ağ)", float(st.get("flag_auto", 0))),
            ("adlı ağ (etiket)", float(st.get("flag_named", 0)))], decimals=0,
            caption=(f"{st.get('netlist', 0)} netlist ({f['errors']} şema çevrilemedi, "
                     f"{st.get('skipped_include', 0)} netlist .include yüzünden atlandı); "
                     f"{st.get('nc', 0)} NC_xx pinin {st.get('missed', 0)}'i kaçtı; "
                     f"{st.get('with_flags', 0)} netlistte en az bir işaret; ikinci sayımda adı birden çok geçen "
                     f"işaret {st.get('recount_not_one', 0)}."))],
        notes=["Adlı ağ işaretleri (ilk 12): " + ("; ".join(named[:12]) if named else "yok") + ".",
               "Kaçan NC_xx olan şemalar: " + (", ".join(f["missed_files"][:12]) or "yok") + ".",
               "İşaretlerin hepsi gerçekten tek pinli; ADI örneklerinde çoğu bilerek boş bırakılmış pin ya da yüksüz "
               "ölçüm çıkışı (trafo sekonderi, E kaynağı çıkışı). Bu yüzden kural hata değil uyarı."],
    ))

    # ---- özet
    tools = m["tools"]
    rep.add_tile("Kota: korunan Bode grafiği", "silindi → kaldı" if not q["eski"]["bode_kept"] and q["yeni"]["bode_kept"]
                 else "değişmedi", "T1")
    rep.add_tile("Özet ortalaması / RMS", f"{format_value(worst_old, '%', decimals=1)} → "
                 f"{format_value(worst_new, '%', decimals=4)}", "en kötü sapma, .meas'e göre (T2)")
    rep.add_tile("Tek bağlantılı düğüm", f"{st.get('missed', 0)}/{st.get('nc', 0)} NC kaçtı",
                 f"{st.get('netlist', 0)} ADI netlisti (T4)")
    rep.add_tile("Ajan araçları", f"{tools['default']} → {tools['extended']}", "include_extended=True")
    rep.add_decision("good", "Uygulandı", "Kota `_bode.png`'yi koşumuna bağlıyor (T1); `.tran` özeti zaman ağırlıklı "
                     "(T2); find_symbol LTspice'ın kullanıcı klasörünü LTspice gibi arıyor (T3); run_netlist tek "
                     "bağlantılı düğümü uyarı olarak bildiriyor (T4); `AutonomousCircuitAgent(include_extended=True)`.")
    rep.add_decision("warn", "Açık", "interactive_designer menüsüne genişletilmiş araç eklenmedi (insan arayüzü, LLM'e "
                     "kapalılıkla ilgisiz).")
    rep.add_finding(Finding(
        "İki bulgu bizim açık konular listemizden",
        "Aritmetik ortalama (PERFORMANCE §9, '4,9848 / 4,8408 V, +%3,0') ve tek bağlantılı düğüm (§2.16) "
        "belgelerimizde ölçümleriyle 'açık' duruyordu; Gemini bunları sayılarıyla aktardı.",
        "Gemini'nin transcript'inde bu sayıları üreten betik yok. Sapma darbede %16,8'e çıkıyor; '%3-10' aralığı "
        "kaynaksız.", "Bu rapor T2, T4.", "warn", "inceleme"))
    rep.add_finding(Finding(
        "LTspice User Files alt klasörlerini yalın adla aramıyor",
        "ADI belgesi şema klasörü için '(and subfolders)' diyor; ölçümde alt klasör yalnızca göreli yolla bulunuyor.",
        "find_symbol kullanıcı klasöründe LTspice'ın davranışını izliyor. `-ini <yol>` 26.1.1'de çalışıyor "
        "(belgelenen `-I<yol>` çalışmıyordu).", "T3 tablosu.", "warn", "LTspice davranışı"))
    rep.add_finding(Finding(
        "Belgeler klasörü OneDrive'a yönlendirilmiş olabilir",
        "Bu makinede `~/Documents` değil OneDrive\\Documents; LTspice kullanıcı klasörünü ayar dosyasındaki "
        "UserCmpDir'den alıyor (BOM'lu UTF-16LE).",
        "ltspice_user_dir UserCmpDir'i okuyor, yol tahmin etmiyor.", "tests/test_symbols.py, tests/data/ref26_LTspice.ini",
        "warn", "ortam"))
    rep.add_check("Gerçek LTspice.ini değişmedi (T3)", "evet" if s["ini_unchanged"] else "HAYIR",
                  "good" if s["ini_unchanged"] else "critical")
    rep.add_check("Kaçan NC_xx (T4)", str(st.get("missed", 0)), "good" if not st.get("missed") else "critical",
                  "LTspice'ın bağlantısız dediği her pin işaretlenmeli.")
    rep.add_check("İkinci sayımla uyuşmayan işaret (T4)", str(st.get("recount_not_one", 0)),
                  "good" if not st.get("recount_not_one") else "warn",
                  "İşaretlenen düğümün adı üst düzey satırlarda tek kez geçmeli.")
    rep.assumptions = [
        ("Ortalama", "LTspice'ın .meas AVG/RMS'i tüm pencere üzerinden; .raw sıkıştırılmış (LTspice varsayılanı)."),
        ("ADI örnekleri", "Örneklem: " + f["sample"] + "; şemalar geçici klasöre kopyalanıp çevrildi."),
        ("Kullanıcı klasörü", "Ayar dosyasında UserCmpDir yoksa (LTspice hiç çalışmamış) arama yapılmaz."),
    ]
    rep.open_items = [
        "find_symbol'un search_dirs'te yalın adı alt klasörlerde araması LTspice'tan geniş (T3).",
        "Özetin `downsampled_points` önizlemesi hâlâ indeks aralıklı: adaptif adımda noktalar kenarlarda toplanır.",
        "Steplenmiş .raw yanında .log yoksa open_raw'ın hatası 'ASCII' diyor; asıl neden eksik .log.",
    ]
    return rep


def main():
    fast = "--hizli" in sys.argv
    WS.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    m = {}
    print("T1 kota...", flush=True)
    m["t1"] = t1_quota()
    for arm, v in m["t1"].items():
        print(f"   {arm}: silinen {v['removed']}, kalan {v['after']}")
    print("T2 ortalama...", flush=True)
    m["t2"] = t2_mean()
    for label, kind, meas, old, new, hand in m["t2"]["rows"]:
        print(f"   {label:28s} {kind}: .meas {meas:.6f} eski {old:.6f} yeni {new:.6f}"
              + (f" elle {hand:.6f}" if hand else ""))
    print("T3 semboller...", flush=True)
    m["t3"] = t3_symbols()
    for r in m["t3"]["rows"]:
        print(f"   {r['label']:36s} LTspice {r['ltspice']} eski {r['old']} yeni {r['new']}")
    print(f"   gerçek ini değişmedi: {m['t3']['ini_unchanged']}")
    print("T4 tek bağlantılı düğüm...", flush=True)
    m["t4"] = t4_floating(fast)
    for label, r in m["t4"]["drc"].items():
        print(f"   {label}: success={r.success} {[w for w in r.warnings if 'tek bir pine' in w]}")
    print(f"   {m['t4']['stats']} ({m['t4']['seconds']:.0f} sn), adlı: {m['t4']['named'][:8]}")
    m["tools"] = {"default": len(AutonomousCircuitAgent(adapter=LTSpiceAdapter(workspace_dir=WS)).tools),
                  "extended": len(AutonomousCircuitAgent(adapter=LTSpiceAdapter(workspace_dir=WS),
                                                         include_extended=True).tools)}
    elapsed = time.perf_counter() - t0
    rep = build_report(m, elapsed, fast)
    save_report(rep, report_path("17_gemini_review_3.html"))
    shutil.rmtree(WS, ignore_errors=True)
    print(f"{elapsed:.0f} sn")


if __name__ == "__main__":
    main()
