"""
Karttan okunan iz yolu LTspice'ta: KiCad kart okuyucunun uçtan uca örneği
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Kaynak ile yük arasındaki bakır yolun etkisi. Yol ``.kicad_pcb``'den okunur (uzunluk, genişlik, katman, via, katman
yapısı), elle girilmez (GEMINI.md "Simülasyon doğrulama kuralları"). Kart dosyası yalnızca okunur.

Varsayılan kart ``tests/data/kicad10_paths.kicad_pcb``: KiCad 10.0.3'ün kaydettiği sentetik test kartı
(``examples/kicad_oracle.py``); yol J1.1 -> TP1.1 (/CHAIN). Kendi kartınız için ``--kart <dosya> --yol U3.2 C5.1``.
Devrenin değerleri (kaynak, yük akımı, dekuplaj) aşağıdaki girdilerdir: kartınızın netine göre değiştirin.

Testler (rapordaki her ölçüm LTspice ``.meas``'ten):

- T0: karttan okunan yol ve modeli (girdi).
- T1: yük akımında yolun DC düşümü, I x (karttan hesaplanan R) ile aynı mı?
- T2: hızlı yük basamağında yük ucundaki çökme. Tek değişken: kaynak ile yük arasında karttan okunan yol / ideal tel.

Güvenilirlik: T2'nin yollu koşumu Gear ile de koşulur (``--hizli`` atlar).

    python examples/21_board_trace_to_simulation.py [--hizli] [--kart <dosya.kicad_pcb> --yol <REF.PAD> <REF.PAD>]
"""

import math
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ltspice_llm import BoardPathError, LTSpiceAdapter, inject_board_path_into_netlist, read_kicad_board  # noqa: E402
from ltspice_llm.netlist import insert_before_end  # noqa: E402
from ltspice_llm.parser import LogParser  # noqa: E402
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

NAME = "21_board_trace_to_simulation"
DEFAULT_BOARD = ROOT / "tests" / "data" / "kicad10_paths.kicad_pcb"
DEFAULT_PADS = ("J1.1", "TP1.1")

# --------------------------------------------------------------------------- girdiler (varsayımlar)
V_SRC = 3.3            # V, ideal kaynak: regülatörün çıkış empedansı yok sayılır
I_BASE = 0.5           # A, yükün basamaktan önceki akımı
I_STEP = 0.5           # A, basamak: 0.5 A -> 1.0 A
T_STEP = 1e-6          # s, basamak anı
T_EDGE = 10e-9         # s, basamak kenarı
C_DEC = 100e-9         # F, yük ucundaki dekuplaj kondansatörü (MLCC)
ESR_DEC = 10e-3        # Ohm, kondansatörün ESR'si; ESL yok sayılır


def window(model):
    """Simülasyon penceresi ve en büyük adım: yolun L'si ile C_DEC'in salınımı en az 15 sönüm sabiti izlenir."""
    l_h = max(model.l_trace, 1e-12)
    period = 2 * math.pi * math.sqrt(l_h * C_DEC)
    tau = 2 * l_h / (model.r_dc + ESR_DEC)
    return T_STEP + max(10e-6, 15 * tau), min(1e-9, period / 200)


def netlist(model, with_path, gear=False):
    """Kaynak -> (karttan okunan yol | ideal tel) -> yük ucu: dekuplaj kondansatörü ve akım basamağı."""
    t_stop, max_step = window(model)
    t_end = t_stop - 50e-9
    lines = [
        f"* {NAME}: {'karttan okunan iz yolu' if with_path else 'ideal tel'}",
        f"V1 src 0 {V_SRC:g}",
        f"C1 load 0 {C_DEC:g} Rser={ESR_DEC:g}",
        f"I1 load 0 PWL(0 {I_BASE:g} {T_STEP:g} {I_BASE:g} {T_STEP + T_EDGE:g} {I_BASE + I_STEP:g})",
        f".tran 0 {t_stop:.6g} 0 {max_step:.6g}",
        f".meas tran v_before FIND V(load) AT={0.9 * T_STEP:g}",
        f".meas tran i_before FIND I(I1) AT={0.9 * T_STEP:g}",
        f".meas tran v_min MIN V(load) FROM={T_STEP:g} TO={t_stop:.6g}",
        f".meas tran v_end FIND V(load) AT={t_end:.6g}",
        f".meas tran i_end FIND I(I1) AT={t_end:.6g}",
    ]
    if not with_path:
        lines.insert(2, "Vw src load 0")   # ideal tel (0 V kaynak)
    text = "\n".join(lines) + "\n.end\n"
    if with_path:
        text = inject_board_path_into_netlist(text, "src", "load", model)
    if gear:
        text = insert_before_end(text, [".options method=gear"])
    return text


def run_all(adapter, model, gear):
    variants = {"tel": (False, False), "yol": (True, False)}
    if gear:
        variants["yol_gear"] = (True, True)
    runs = {}
    for key, (with_path, use_gear) in variants.items():
        res = adapter.run_netlist(netlist(model, with_path, use_gear), circuit_name=f"ex21_{key}", treat_as_text=True)
        runs[key] = res
        if not res.success:
            raise SystemExit(f"{key} koşumu başarısız: {res.errors}")
    return runs


def mv(x, d=3):
    return format_value(x, "mV", 1e-3, d)


def mohm(x, d=3):
    return format_value(x, "mΩ", 1e-3, d)


# --------------------------------------------------------------------------- rapor
def build_report(board, path, model, runs, elapsed):
    m = {k: r.measurements for k, r in runs.items()}
    r_path = model.r_dc
    rep = SimulationReport(
        NAME, "Karttan okunan iz yolu LTspice'ta",
        subtitle=f"{board.path.name}: {path.start} → {path.end} ({path.net}) besleme yolu, yük basamağında",
        meta=[("LTspice", LTSpiceAdapter().ltspice_version() or "?"), ("Kart", board.path.name),
              ("SHA-256", board.sha256[:12]), ("Revizyon", board.revision or "yok"),
              ("Koşum", str(len(runs))), ("Süre", f"{elapsed:.1f} sn")],
    )

    # ---- T0: yol (girdi)
    pieces = path.pieces
    rows0 = []
    for i, (piece, trace) in enumerate(zip(pieces, model.traces), start=1):
        row = {"len": piece.length_mm, "w": piece.width_mm, "r": trace.r_dc, "l": trace.l_trace, "c": trace.c_trace}
        if piece.plane_coverage is not None:
            row["cov"] = piece.plane_coverage * 100
        rows0.append((f"{i}: {piece.layer}", row))
    totals = (f"Toplam {format_value(path.length_mm, 'mm', decimals=3)}, R {mohm(r_path)}, "
              f"L {format_value(model.l_trace, 'nH', 1e-9, 2)}, C {format_value(model.c_trace, 'pF', 1e-12, 3)}; "
              f"{path.via_count} katman geçişi (via ideal kısa devre).")
    stack =[entry for entry in board.stackup if entry.kind != "copper" and entry.epsilon_r]
    stack_text = "; ".join(f"{e.name}: {format_value(e.thickness_mm, 'mm', decimals=3)}, er {e.epsilon_r:g}"
                           for e in stack)
    rep.add_study(Study(
        "T0", "Karttan okunan yol (girdi)",
        question="Kaynak ile yük arasındaki bakır yol hangisi; simülasyona hangi değerler karttan girdi?",
        method=(f"`read_kicad_board` kart dosyasını yalnızca okur; `trace_path(\"{path.start}\", \"{path.end}\")` iki pad "
                "arasındaki en kısa iz yolunu bulur; `parasitics()` her parçayı kartın katman yapısıyla IPC-2141 "
                "microstrip modeline (ADI MT-094) çevirir ve her parça bir T hücresi olarak netliste eklenir. Bu "
                f"tablodaki sayılar ölçüm değil girdidir. Katman yapısı: {stack_text or 'dosyada yok'}."),
        table=Table([Column("len", "uzunluk", "mm", decimals=3), Column("w", "genişlik", "mm", decimals=3),
                     Column("cov", "altında döküm", "%", decimals=0), Column("r", "R", "mΩ", 1e-3, 3),
                     Column("l", "L", "nH", 1e-9, 2), Column("c", "C", "pF", 1e-12, 3)], rows0, caption=totals),
        notes=list(model.notes),
    ))

    # ---- T1: DC düşüm
    expected_base = m["yol"]["i_before"] * r_path
    expected_end = m["yol"]["i_end"] * r_path
    drop_base = V_SRC - m["yol"]["v_before"]
    drop_end = V_SRC - m["yol"]["v_end"]
    diff_base = (drop_base / expected_base - 1) * 100
    diff_end = (drop_end / expected_end - 1) * 100
    rows1 = [
        (f"I = {format_value(m['yol']['i_before'], 'A', decimals=2)}",
         {"i": m["yol"]["i_before"], "drop": drop_base, "model": expected_base, "diff": diff_base}),
        (f"I = {format_value(m['yol']['i_end'], 'A', decimals=2)} (basamaktan sonra, oturmuş)",
         {"i": m["yol"]["i_end"], "drop": drop_end, "model": expected_end, "diff": diff_end}),
    ]
    rep.add_study(Study(
        "T1", "Yolun DC düşümü, karttan hesaplanan dirençle aynı mı?",
        question=("Yük akımı yolun direncinden geçerken yük ucunda gerilim düşer. LTspice'ın ölçtüğü düşüm, yük akımı "
                  "ile karttan hesaplanan direncin çarpımına eşit mi?"),
        method=("Ölçüm: `V(src) − V(load)`, basamaktan önce (`.meas FIND ... AT`, DC çalışma noktası) ve basamaktan "
                "sonra salınım söndükten sonra; akım `I(I1)`. Model: I × R (R, T0'daki toplam)."),
        table=Table([Column("i", "yük akımı", "A", decimals=3), Column("drop", "LTspice düşüm", "mV", 1e-3, 4),
                     Column("model", "I × R", "mV", 1e-3, 4), Column("diff", "fark", "%", decimals=5)], rows1),
    ))

    # ---- T2: yük basamağı
    dip_path = m["yol"]["v_before"] - m["yol"]["v_min"]
    dip_wire = m["tel"]["v_before"] - m["tel"]["v_min"]
    t2 = Study(
        "T2", "Yük basamağında yük ucu ne kadar çöküyor?",
        question=(f"Yük akımı {format_value(T_EDGE, 'ns', 1e-9, 0)} içinde {format_value(I_STEP, 'A', decimals=2)} "
                  "artınca yük ucundaki gerilim ne kadar düşer? İdeal tel ile kurulan simülasyon bunu gösterir mi?"),
        method=("Tek değişken: kaynak ile yük ucu arasında karttan okunan yol ya da ideal tel (0 V kaynak). Yük ucunda "
                f"{format_value(C_DEC, 'nF', 1e-9, 0)} dekuplaj kondansatörü ({format_value(ESR_DEC, 'mΩ', 1e-3, 0)} "
                "ESR). Ölçüm: `.meas MIN V(load)` basamaktan sonra; çökme = basamaktan önceki gerilim − en düşük."),
        table=Table([Column("before", "basamaktan önce", "V", decimals=4), Column("vmin", "en düşük", "V", decimals=4),
                     Column("dip", "çökme", "mV", 1e-3, 2), Column("end", "oturmuş", "V", decimals=4)],
                    [("ideal tel", {"before": m["tel"]["v_before"], "vmin": m["tel"]["v_min"], "dip": dip_wire,
                                    "end": m["tel"]["v_end"]}),
                     ("karttan okunan yol", {"before": m["yol"]["v_before"], "vmin": m["yol"]["v_min"],
                                             "dip": dip_path, "end": m["yol"]["v_end"]})],
                    highlight="karttan okunan yol"),
        effects=[
            Effect(f"karttan okunan yol ({path.start} → {path.end})", "kaynak ile yük ucu arası",
                   "yük ucundaki en düşük gerilim", m["tel"]["v_min"], m["yol"]["v_min"], "V", decimals=3,
                   better="higher"),
            Effect(f"karttan okunan yol ({path.start} → {path.end})", "kaynak ile yük ucu arası",
                   "basamaktan sonraki oturmuş gerilim", m["tel"]["v_end"], m["yol"]["v_end"], "V", decimals=4,
                   better="higher"),
        ],
    )
    traces = []
    for key, label, emphasis in (("tel", "ideal tel", False), ("yol", "karttan okunan yol", True)):
        x, y = read_trace(runs[key], "V(load)")
        traces.append(Trace(label, x, y, emphasis=emphasis, label_extremes=("min",) if emphasis else ()))
    t2.charts.append(LineChart("Yük ucu gerilimi, basamak çevresinde", traces, references=[(V_SRC, "V_src")],
                               x_range=(T_STEP - 0.3e-6, T_STEP + 3e-6), value_decimals=3))
    rep.add_study(t2)

    # ---- özet
    rep.add_tile("İz yolu (karttan)", format_value(path.length_mm, "mm", decimals=2),
                 f"{len(pieces)} parça, {path.via_count} via")
    rep.add_tile("Yolun direnci (model)", mohm(r_path), "IPC-2141, 25 °C")
    rep.add_tile("Yolun endüktansı (model)", format_value(model.l_trace, "nH", 1e-9, 2), "kayıpsız hat, L = Z0² C")
    rep.add_tile("Basamakta çökme (LTspice)", mv(dip_path, 1), f"ideal telde {mv(dip_wire, 1)}")
    worst = max(abs(diff_base), abs(diff_end))
    rep.add_decision("good" if worst < 0.01 else "serious", "DC düşüm",
                     f"LTspice'ın ölçtüğü düşüm I × R ile en çok {format_value(worst, '%', decimals=5)} ayrışıyor: yolun "
                     "direnci netliste karttan hesaplandığı gibi girdi.")
    rep.add_decision("info", "Yük basamağı",
                     f"Karttan okunan yolla yük ucu {mv(dip_path, 1)} çöküyor; ideal tel {mv(dip_wire, 1)} gösteriyor.")
    rep.add_finding(Finding(
        "İdeal tel varsayımı çökmeyi göstermiyor",
        "Simülasyonda parçalar ideal tellerle bağlanınca kaynak ile yük ucu arasında ne direnç ne endüktans kalır.",
        "Yol karttan okunup parça parça T hücresi olarak netliste eklendi (`inject_board_path_into_netlist`).",
        f"Aynı yük basamağında çökme ideal telde {mv(dip_wire, 1)}, karttan okunan yolla {mv(dip_path, 1)}.",
        "warn", "yeni etki"))
    for warning in path.warnings:
        rep.add_finding(Finding("Kart okuyucunun uyarısı", warning, "Uyarı rapora taşındı; model değiştirilmedi.",
                                "Bu yol için modelin varsayımı; donanımda doğrulanmalı.", "warn", "varsayım"))

    # ---- güvenilirlik
    if "yol_gear" in runs:
        gear_log = LogParser.read_text(runs["yol_gear"].log_path)
        gear_seen = bool(re.search(r"^method = Gear", gear_log, re.MULTILINE))
        gear_dip = m["yol_gear"]["v_before"] - m["yol_gear"]["v_min"]
        gear_diff = abs(gear_dip - dip_path) / dip_path * 100 if dip_path else 0.0
        rep.add_check("Gear ile trapez çökme farkı", format_value(gear_diff, "%", decimals=3),
                      "good" if gear_seen and gear_diff < 1 else "warn",
                      f"logda `method = Gear`: {'var' if gear_seen else 'yok'}. Fark küçükse salınım fiziksel, "
                      "trapez artefaktı değil.")
    settle = abs(m["yol"]["v_end"] - (V_SRC - expected_end)) / dip_path * 100 if dip_path else 0.0
    rep.add_check("Son değer pencerede oturdu", format_value(settle, "%", decimals=3), "good" if settle < 1 else "warn",
                  "Oturmuş gerilimin V_src − I × R'den farkı, çökmenin yüzdesi olarak.")
    rep.add_check("DC çalışma noktası", format_value(m["yol"]["v_before"], "V", decimals=4), "good" if abs(diff_base) < 0.01
                  else "warn", "Basamaktan önceki yük ucu gerilimi V_src − I × R'ye eşit (T1).")
    rep.add_runs("T1/T2", [("ideal tel", runs["tel"]), ("karttan okunan yol", runs["yol"])])
    if "yol_gear" in runs:
        rep.add_runs("Gear", [("karttan okunan yol, Gear", runs["yol_gear"])])

    rep.assumptions = [
        ("Kart ve yol", f"{board.path.name} (SHA-256 {board.sha256[:12]}): uzunluk, genişlik, katman ve katman yapısı "
         "dosyadan okundu; yol iki pad arasındaki en kısa iz yolu."),
        ("İz modeli", "IPC-2141 microstrip (ADI MT-094): izin altında kesintisiz dönüş düzlemi; dönüş yolu ideal (alt "
         "devrenin ref portu 0 düğümüne bağlı). Bakır 25 °C."),
        ("Via'lar", "Sayıldı, modellenmedi (ideal kısa devre); değerleri `parasitics(via_inductance_h=...)` ile girilebilir."),
        ("Kaynak", f"İdeal {format_value(V_SRC, 'V', decimals=1)} kaynak; regülatörün çıkış empedansı ve kendi "
         "kondansatörleri yok sayıldı."),
        ("Yük", f"İdeal akım basamağı {format_value(I_BASE, 'A', decimals=1)} → "
         f"{format_value(I_BASE + I_STEP, 'A', decimals=1)}, kenar {format_value(T_EDGE, 'ns', 1e-9, 0)}."),
        ("Dekuplaj", f"{format_value(C_DEC, 'nF', 1e-9, 0)}, ESR {format_value(ESR_DEC, 'mΩ', 1e-3, 0)}; ESL ve DC-bias "
         "kaybı yok sayıldı."),
    ]
    rep.open_items = [
        "Kendi kartınızda: `--kart <dosya.kicad_pcb> --yol <REF.PAD> <REF.PAD>`; kaynak, yük ve dekuplaj değerlerini o "
        "netin gerçek değerleriyle girin.",
        "Donanımda yük basamağında yük ucundaki gerilimi osiloskopla ölçün; regülatörün yanıtı burada yok.",
    ]
    return rep


def parse_args(args):
    board, pads = DEFAULT_BOARD, DEFAULT_PADS
    if "--kart" in args:
        board = Path(args[args.index("--kart") + 1])
    if "--yol" in args:
        i = args.index("--yol")
        pads = (args[i + 1], args[i + 2])
    return board, pads


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    board_file, (start, end) = parse_args(args)
    board = read_kicad_board(board_file)
    try:
        path = board.trace_path(start, end)
        model = path.parasitics()
    except BoardPathError as exc:
        print(f"Yol modellenemedi: {exc}")
        return 2
    print(f"Kart: {board.path.name} (sha256 {board.sha256[:12]}), yol {path.start} -> {path.end} ({path.net}): "
          f"{path.length_mm:.4g} mm, {path.via_count} via; R = {model.r_dc * 1e3:.4g} mOhm, L = {model.l_trace * 1e9:.4g} nH")
    adapter = LTSpiceAdapter()
    t0 = time.perf_counter()
    runs = run_all(adapter, model, gear="--hizli" not in args)
    elapsed = time.perf_counter() - t0
    for key, res in runs.items():
        meas = res.measurements
        print(f"{key:9s} v_before = {meas['v_before']:.6g}  v_min = {meas['v_min']:.6g}  v_end = {meas['v_end']:.6g}")
    rep = build_report(board, path, model, runs, elapsed)
    save_report(rep, report_option(args, f"{NAME}.html"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
