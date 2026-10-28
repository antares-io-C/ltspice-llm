"""
Örnek 16: Gemini'nin süreç, disk, bellek, sembol ve DRC önerilerini ölçer (2026-10-06) ve rapor yazar.

Her iddia ölçümle sınandı; tutanlar uygulandı (CHANGELOG, docs/LESSONS_LEARNED_PERFORMANCE.md §10,
docs/LESSONS_LEARNED_LTSPICE_REFERENCE.md). Belgelerdeki sayılar bu betiğin çıktısıdır:

- T1 Çökme: LTspice hangi devrede çöküyor, çıkış kodu ne, WerFault ne kadar bekletiyor, iş nesnesi bayrağı ne yapıyor?
- T2 Yetim süreç: Python ölünce LTspice yaşıyor mu, çekirdek bütçesi (cpu_guard) ne gösteriyor?
- T3 .save budaması: ikili .raw'da dosya ve süre kazancı (Gemini: 39,2 -> 1,4 MB, 28x; "%70 kısalma").
- T4 .raw belleği: özetin ve çizimin tepe belleği; LTTB ile min/max seyreltmesi uç değerleri koruyor mu?
- T5 Semboller: lib/sym sayımı, biçim, tarama süresi; kütüphane pinleriyle kurulan şema LTspice'ta bağlı mı?
- T6 DRC: önerilen kuralların durumlarını LTspice ve teşhis motoru zaten bildiriyor mu?
- T7 Yan bulgu: dalga formu özetindeki ortalama zaman ağırlıklı mı?

Windows ve psutil gerekir (WerFault süreci, tepe çalışma kümesi, yetim sürecin CPU süresi); psutil paketin bağımlılığı
değildir. Ölçerken başka LTspice çalışmamalı. Rapor reports/ altına yazılır (git'e girmez):

    python examples/16_process_and_memory_hardening.py [--hizli]   # --hizli: 67 MB .raw bellek ölçümü (T4) yok
"""

import importlib
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

import numpy as np  # noqa: E402

import ltspice_llm.adapter as adapter_mod  # noqa: E402
from ltspice_llm import AscSchematic, LTSpiceAdapter  # noqa: E402
from ltspice_llm.cpu_guard import default_pool  # noqa: E402
from ltspice_llm.parser import LogParser, RawParser  # noqa: E402
from ltspice_llm.report import (  # noqa: E402
    BarChart, Column, Effect, Finding, LineChart, SimulationReport, Study, Table, Trace, format_value, read_trace,
)
from ltspice_llm.schematic import decode_asc_bytes  # noqa: E402
from ltspice_llm.symbols import find_symbol, library_symbol_dir, read_symbol  # noqa: E402
from study_helpers import report_path, save_report  # noqa: E402

try:
    import psutil
except ImportError:  # ölçüm psutil'siz koşmaz (main), ama modülün verileri (DRC_CASES, örnek 17) okunabilir
    psutil = None

WS = Path(tempfile.gettempdir()) / "ltspice_llm_ex16"
BUCK = importlib.import_module("10_performance_fast_paths").BUCK  # LESSONS_LEARNED_PERFORMANCE referans devresi
BIG_BUCK = BUCK.replace(".tran 0 2m 0 20n", ".tran 0 20m 0 20n").replace("FROM 1.5m TO 2m", "FROM 19.5m TO 20m")
CREATE_DEFAULT_ERROR_MODE = 0x04000000  # alt süreç ebeveynin hata kipini değil Windows varsayılanını alır

CRASH_CASES = {
    "alt devre kendini çağırıyor": "* r\nV1 in 0 1\nX1 in 0 LOOP\n.subckt LOOP a b\nR1 a b 1k\nX2 a b LOOP\n.ends\n.op\n.end\n",
    "A -> B -> A": ("* m\nV1 in 0 1\nX1 in 0 SUBA\n.subckt SUBA a b\nR1 a b 1k\nX1 a b SUBB\n.ends\n"
                    ".subckt SUBB a b\nR1 a b 1k\nX1 a b SUBA\n.ends\n.op\n.end\n"),
    ".func kendini çağırıyor": "* f\n.func f(x) {f(x)+1}\nV1 in 0 1\nB1 out 0 V={f(V(in))}\nR1 out 0 1k\n.op\n.end\n",
    "tamsayı 1/0 (.param)": "* d\n.param a=0\n.param b={int(1)/int(a)}\nV1 in 0 {b}\nR1 in 0 1k\n.op\n.end\n",
    "B kaynağında 1/0": "* b\nV1 in 0 0\nB1 out 0 V={1/V(in)}\nR1 out 0 1k\n.tran 1m\n.end\n",
}
RECURSIVE = CRASH_CASES["alt devre kendini çağırıyor"]
LONG_RC = "* uzun\nV1 in 0 PULSE(0 1 0 1u 1u 0.5m 1m)\nR1 in out 1k\nC1 out 0 100n\n.save V(out)\n.tran 0 20 0 50n\n.end\n"
DRC_CASES = {
    "İki gerilim kaynağı paralel": "* p\nV1 a 0 5\nV2 a 0 3.3\nR1 a 0 1k\n.op\n.end\n",
    "DC yolu olmayan düğüm (yalnız kondansatör)": "* c\nV1 in 0 1\nC1 in a 1u\nC2 a 0 1u\nR1 in 0 1k\n.op\n.end\n",
    "Tek bağlantılı düğüm (açık uçlu direnç)": "* x\nV1 in 0 1\nR1 in x 1k\nR2 in 0 1k\n.op\n.end\n",
    "Boşta op-amp girişi (LT1001 In+)": ("* o\nV1 vcc 0 5\nV2 vee 0 -5\nXU1 inp out vcc vee out LT1001\nR1 out 0 10k\n"
                                        ".lib LTC.lib\n.op\n.end\n"),
    "Ters paralel diyot (kıskaç)": ("* d\nV1 in 0 SINE(0 2 1k)\nR1 in a 1k\nD1 a 0 1N4148\nD2 0 a 1N4148\n"
                                    ".lib standard.dio\n.tran 2m\n.end\n"),
}


# ============================================================================ alt süreç görevleri
class _Job(adapter_mod._ProcessJob):
    """Bayrakları seçilebilen iş nesnesi (yalnızca karşılaştırma için)."""

    def __init__(self, flags):
        super().__init__()
        self.LIMIT_FLAGS = flags


def _werfault_for(pid):
    found = False
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            if (p.info["name"] or "").lower().startswith("werfault") and str(pid) in " ".join(p.info["cmdline"] or []):
                found = True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return found


def child_crash_timing():
    """Varsayılan hata kipinde (WER açık) çöken LTspice: iş nesnesi yok / KILL / KILL|DIE, her biri 3 kez."""
    import ctypes
    exe = str(LTSpiceAdapter().executable_path)
    net = WS / "crash_timing.net"
    net.write_text(RECURSIVE.replace(".op\n", ".options threads=1\n.op\n"), encoding="utf-8")
    rows = []
    variants = [("iş nesnesi yok", None), ("KILL_ON_JOB_CLOSE", 0x2000), ("KILL_ON_JOB_CLOSE + DIE_ON_UNHANDLED", 0x2400)]
    for rep in range(3):
        for label, flags in variants:
            job = _Job(flags) if flags is not None else None
            t0 = time.perf_counter()
            proc = subprocess.Popen([exe, "-b", str(net)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if job is not None:
                job.assign(proc)
            wer = False
            while proc.poll() is None and time.perf_counter() - t0 < 60:
                wer = wer or _werfault_for(proc.pid)
                time.sleep(0.05)
            proc.communicate()
            rows.append({"variant": label, "rep": rep, "seconds": time.perf_counter() - t0,
                         "rc": proc.returncode & 0xFFFFFFFF, "werfault": wer})
    print(json.dumps({"error_mode": ctypes.windll.kernel32.GetErrorMode(), "rows": rows}))


def child_orphan_parent(use_job):
    if use_job == "0":
        adapter_mod._assign_to_job = lambda process: False  # eski davranış: tek değişken iş nesnesi
    LTSpiceAdapter(workspace_dir=WS, default_timeout=600, stall_timeout=None).run_netlist(
        LONG_RC, circuit_name=f"orphan_{use_job}", treat_as_text=True, summarize_raw=False)


def child_memory(task):
    """Tek görev, ayrı süreçte: tepe çalışma kümesinin (peak_wset) taban üstü artışı."""
    import ltspice_llm.parser as parser_mod
    import ltspice_llm.visualizer as vis_mod
    raw_path = WS / "big_buck.raw"
    proc = psutil.Process()
    base = proc.memory_info().peak_wset
    t0 = time.perf_counter()
    out = {}
    if task == "summ_lazy":  # eski kod: open_raw tembel (tek değişken)
        real = parser_mod.open_raw
        parser_mod.open_raw = lambda p, traces=None: real(p)
        out["summary"] = {k: v.to_dict() for k, v in RawParser.summarize_file(raw_path).items()}
    elif task == "summ_single":
        out["summary"] = {k: v.to_dict() for k, v in RawParser.summarize_file(raw_path).items()}
    elif task in ("plot_full", "plot_minmax"):
        if task == "plot_full":
            vis_mod.PLOT_BUCKETS = 10 ** 9  # seyreltme kapalı (tek değişken)
        vis_mod.WaveformVisualizer.plot_traces(raw_path, ["V(vsw)"], output_png_path=WS / f"{task}.png")
    out["seconds"] = time.perf_counter() - t0
    out["peak_delta_mb"] = (proc.memory_info().peak_wset - base) / 1e6
    print(json.dumps(out))


def _child(*args, flags=0):
    out = subprocess.run([sys.executable, __file__, "--alt", *args], capture_output=True, text=True,
                         encoding="utf-8", creationflags=flags, timeout=900)
    if out.returncode != 0:
        raise SystemExit(f"alt görev {args} başarısız: {out.stderr[-800:]}")
    return json.loads(out.stdout.strip().splitlines()[-1])


# ============================================================================ ölçümler
def ntstatus_messages():
    """Adaptörün çökme adları Windows ileti tablosuyla (ntdll.dll, FormatMessage) aynı mı?"""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetModuleHandleW.restype = wintypes.HMODULE
    k32.FormatMessageW.argtypes = [wintypes.DWORD, wintypes.LPCVOID, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.LPWSTR, wintypes.DWORD, ctypes.c_void_p]
    ntdll = k32.GetModuleHandleW("ntdll.dll")
    rows = []
    for code, label in adapter_mod._NTSTATUS_NAMES.items():
        buf = ctypes.create_unicode_buffer(2048)
        k32.FormatMessageW(0x800 | 0x200, ntdll, code, 0, buf, 2048, None)  # FROM_HMODULE | IGNORE_INSERTS
        rows.append((f"0x{code:08X}", label, " ".join(buf.value.split())))
    return rows


def t1_crash(adapter):
    cases = {}
    exe = str(adapter.executable_path)
    for label, net in CRASH_CASES.items():
        path = WS / f"crash_{len(cases)}.net"
        path.write_text(net.replace(".end\n", ".options threads=1\n.end\n"), encoding="utf-8")
        proc = subprocess.Popen([exe, "-b", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        adapter_mod._assign_to_job(proc)  # WerFault beklenmesin (ölçüm aşağıda ayrıca)
        _, err = proc.communicate(timeout=60)
        res = adapter.run_netlist(net, circuit_name=f"t1_{len(cases)}", treat_as_text=True, summarize_raw=False)
        cases[label] = {"rc": proc.returncode & 0xFFFFFFFF, "stderr": err.decode("utf-8", "replace").strip(),
                        "result": res}
    timing = _child("crash-timing", flags=CREATE_DEFAULT_ERROR_MODE)
    return cases, timing


def t2_orphan():
    out = {}
    for use_job in ("0", "1"):
        parent = subprocess.Popen([sys.executable, __file__, "--alt", "orphan-parent", use_job])
        pp = psutil.Process(parent.pid)
        lt, t0 = None, time.time()
        while lt is None and time.time() - t0 < 60:
            lt = next((c for c in pp.children(recursive=True) if c.name().lower().startswith("ltspice")), None)
            time.sleep(0.1)
        if lt is None:
            parent.kill()
            raise SystemExit("T2: LTspice başlamadı")
        time.sleep(3)
        free_before = default_pool().free_slots()
        cpu0 = sum(lt.cpu_times()[:2])
        pp.kill()  # TerminateProcess: yalnızca ebeveyn
        parent.wait(timeout=10)
        t_kill, samples = time.time(), []
        for wait in (1, 5, 10):
            time.sleep(max(0.0, wait - (time.time() - t_kill)))
            alive = lt.is_running() and lt.status() != psutil.STATUS_ZOMBIE
            samples.append({"t": wait, "alive": alive,
                            "cpu": (sum(lt.cpu_times()[:2]) - cpu0) if alive else None,
                            "free": default_pool().free_slots()})
        if lt.is_running():
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(lt.pid)], capture_output=True, check=False)
        out[use_job] = {"free_before": free_before, "samples": samples}
    return out


def t3_save(adapter):
    rows = {"full": [], "pruned": []}
    for rep in range(3):
        for label, kw in (("full", {}), ("pruned", {"save_signals": ["V(vout)"]})):
            t0 = time.perf_counter()
            r = adapter.run_netlist(BUCK, circuit_name=f"t3_{label}_{rep}", treat_as_text=True, summarize_raw=False,
                                    **kw)
            wall = time.perf_counter() - t0
            solver = float(re.search(r"Total elapsed time:\s*([\d.]+)", LogParser.read_text(r.log_path)).group(1))
            rows[label].append({"wall": wall, "solver": solver, "mb": r.raw_path.stat().st_size / 1e6, "result": r})
    return rows


def _lttb(x, y, n_out):
    """Largest-Triangle-Three-Buckets (Steinarsson 2013): uçlar + kova başına tek nokta."""
    n = len(x)
    every = (n - 2) / (n_out - 2)
    idx, a = [0], 0
    for i in range(n_out - 2):
        start, end = int(i * every) + 1, int((i + 1) * every) + 1
        nxt = slice(end, min(int((i + 2) * every) + 1, n))
        avg_x = x[nxt].mean() if nxt.stop > nxt.start else x[-1]
        avg_y = y[nxt].mean() if nxt.stop > nxt.start else y[-1]
        area = np.abs((x[a] - avg_x) * (y[start:end] - y[a]) - (x[a] - x[start:end]) * (avg_y - y[a]))
        a = start + int(np.argmax(area))
        idx.append(a)
    idx.append(n - 1)
    return np.asarray(idx)


def _lost_extremes(x, y, xs, ys, buckets=1000):
    """Eşit süreli 1000 kovanın kaçında kovanın tepesi / dibi seçilen (xs, ys) noktalarında yok? Kovalar
    report._decimate'inkilerle aynı tanımlanır (searchsorted, sol); iki yöntem aynı kovalarla değerlendirilir."""
    edges_t = np.linspace(x[0], x[-1], buckets + 1)
    full, sel = np.searchsorted(x, edges_t), np.searchsorted(xs, edges_t)
    lost_max = lost_min = 0
    for k in range(buckets):
        if full[k + 1] <= full[k]:
            continue
        seg, chosen = y[full[k]:full[k + 1]], ys[sel[k]:sel[k + 1]]
        lost_max += chosen.size == 0 or not np.isclose(chosen.max(), seg.max())
        lost_min += chosen.size == 0 or not np.isclose(chosen.min(), seg.min())
    return int(lost_max), int(lost_min)


def t4_memory(adapter):
    raw_path = WS / "big_buck.raw"
    big = adapter.run_netlist(BIG_BUCK, circuit_name="big_buck", treat_as_text=True, summarize_raw=False, timeout=300)
    if not big.success:
        raise SystemExit(f"T4: büyük koşum başarısız: {big.errors}")
    runs = {task: [_child("memory", task) for _ in range(2)]
            for task in ("summ_lazy", "summ_single", "plot_full", "plot_minmax")}
    import matplotlib.image as mpimg
    a = mpimg.imread(str(WS / "plot_full.png"))[..., :3]
    b = mpimg.imread(str(WS / "plot_minmax.png"))[..., :3]
    diff = np.abs(a - b).max(axis=2)
    from ltspice_llm.report import _decimate
    x, y = read_trace(big, "V(vsw)")
    keep = _lttb(x, y, 2000)
    chosen = {"LTTB, 2000 nokta": (x[keep], y[keep]), "min/max, 1000 kova": _decimate(x, y, x[0], x[-1], buckets=1000)}
    lost = {k: _lost_extremes(x, y, xs, ys) for k, (xs, ys) in chosen.items()}
    global_kept = {k: (bool(np.isclose(ys.max(), y.max())), bool(np.isclose(ys.min(), y.min())))
                   for k, (xs, ys) in chosen.items()}
    return {"result": big, "raw_mb": raw_path.stat().st_size / 1e6, "points": len(x), "runs": runs,
            "selected": {k: len(xs) for k, (xs, _) in chosen.items()},
            "pixels": (a.shape[1], a.shape[0], float(np.mean(diff > 1 / 255))), "lost": lost, "global": global_kept,
            "same_summary": runs["summ_lazy"][0]["summary"] == runs["summ_single"][0]["summary"],
            "signals": len(runs["summ_single"][0]["summary"])}


def t5_symbols(adapter):
    lib = library_symbol_dir()
    t0 = time.perf_counter()
    paths = list(lib.rglob("*.asy"))
    t_list = time.perf_counter() - t0
    t0 = time.perf_counter()
    enc, missing, full_order = {}, 0, 0
    for p in paths:
        text, e = decode_asc_bytes(p.read_bytes())
        enc[e] = enc.get(e, 0) + 1
        pins = sum(1 for ln in text.splitlines() if ln.startswith("PIN "))
        names = sum(1 for ln in text.splitlines() if ln.startswith("PINATTR PinName"))
        orders = sum(1 for ln in text.splitlines() if ln.startswith("PINATTR SpiceOrder"))
        missing += names < pins
        full_order += pins == orders
    t_parse = time.perf_counter() - t0
    order = read_symbol(find_symbol("LT8610")).spice_order
    sch = AscSchematic()
    for rotation, x in (("R0", 400), ("R90", 1200)):
        u = sch.add_symbol("PowerProducts\\\\LT8610", x, 400, rotation=rotation, inst_name=f"XU_{rotation}")
        for name in order:
            sch.add_net_label(*sch.get_pin(u, name), f"{rotation}_{re.sub(r'[^A-Za-z0-9]', '_', name)}")
    sch.add_directive(0, 800, ".op")
    inspection = adapter.inspect_schematic(sch.render(), treat_as_text=True)
    in_order = all(
        ln.partition(";")[2].strip().removeprefix("§pnba ").split(")") == order
        for ln in inspection["netlist"].splitlines() if ln.startswith("X§XU_"))
    # LT1001 gerilim izleyici: pinler kütüphaneden; ideal V(out) = V(in) = 1,25 V
    f = AscSchematic()
    u1 = f.add_symbol("OpAmps\\\\LT1001", 400, 200, inst_name="U1")
    vin = f.add_symbol("voltage", 160, 240, inst_name="V1", value="1.25")
    vp = f.add_symbol("voltage", 640, 80, inst_name="V2", value="5")
    vn = f.add_symbol("voltage", 640, 320, inst_name="V3", value="5")
    f.connect(f.get_pin(vin, "+"), f.get_pin(u1, "In+"))
    for pin, net in (("OUT", "out"), ("In-", "out"), ("V+", "vcc"), ("V-", "vee")):
        f.add_net_label(*f.get_pin(u1, pin), net)
    f.add_net_label(*f.get_pin(vp, "+"), "vcc")
    f.add_net_label(*f.get_pin(vn, "-"), "vee")
    for src, pin in ((vin, "-"), (vp, "-"), (vn, "+")):
        f.add_ground(*f.get_pin(src, pin))
    f.add_directive(0, 480, ".op")
    f.add_directive(0, 520, ".meas op vout FIND V(out)")
    follower = adapter.run_netlist(f.save(WS / "t5_follower.asc"), circuit_name="t5_follower")
    return {"count": len(paths), "t_list": t_list, "t_parse": t_parse, "enc": enc, "missing_name": missing,
            "all_have_order": full_order == len(paths), "lt8610_pins": len(order),
            "lt8610_unconnected": len(inspection["unconnected_pins"]), "lt8610_in_order": in_order,
            "follower": follower}


def t6_drc(adapter):
    return {label: adapter.run_netlist(net, circuit_name=f"t6_{i}", treat_as_text=True, summarize_raw=False)
            for i, (label, net) in enumerate(DRC_CASES.items())}


# T8: kart üstü bir SPI saat hattı (9 MHz, MEDIUM kenar 6 ns, 33 Ω seri, iz 70 nH, iz + alıcı 16 pF)
SCK = ("* sck 9 MHz\nV1 s 0 PULSE(0 3.3 5n 6n 6n 49.55n 111.1n)\nR1 s a 33\nL1 a r 70n Rser=0\nC1 r 0 16p\n"
       ".save V(r)\n.tran 0 2.3u 0 0.05n\n.meas tran vr_max MAX V(r) FROM 1u TO 2.3u\n"
       ".meas tran vr_min MIN V(r) FROM 1u TO 2.3u\n.end\n")
SCK_PERIOD = 111.1e-9


def t8_jitter(adapter):
    """Deterministik simülasyonda saat geçişlerinin periyot sapması (jitter) ve zaman hatası (TIE)."""
    res = adapter.run_netlist(SCK, circuit_name="t8_sck", treat_as_text=True)
    x, y = read_trace(res, "V(r)")
    level = 1.65
    idx = np.where((y[:-1] < level) & (y[1:] >= level))[0]
    t_cross = x[idx] + (level - y[idx]) * (x[idx + 1] - x[idx]) / (y[idx + 1] - y[idx])  # doğrusal ara değer
    t_cross = t_cross[3:]  # ilk periyotlar oturma
    periods = np.diff(t_cross)
    n = np.arange(len(t_cross))
    tie = t_cross - np.polyval(np.polyfit(n, t_cross, 1), n)
    return {"result": res, "edges": len(t_cross), "period_mean": float(periods.mean()),
            "period_std": float(periods.std()), "tie_pp": float(tie.max() - tie.min())}


def t7_mean(adapter):
    net = BUCK.replace(".end\n", ".meas tran vsw_avg AVG V(vsw)\n.meas tran vout_all AVG V(vout)\n.end\n")
    res = adapter.run_netlist(net, circuit_name="t7_mean", treat_as_text=True)
    rows = []
    for sig, meas in (("V(vsw)", "vsw_avg"), ("V(vout)", "vout_all")):
        x, y = read_trace(res, sig)
        rows.append((sig, {"meas": res.measurements.get(meas), "summary": res.raw_summary[sig].mean_val,
                           "trapz": float(np.trapezoid(y, x) / (x[-1] - x[0]))}))
    dt = np.diff(read_trace(res, "V(vsw)")[0])
    return {"result": res, "rows": rows, "dt_ratio": float(dt.max() / dt[dt > 0].min())}


# ============================================================================ rapor
def med(values):
    return float(np.median(values))


def _first_line(res):
    """Koşumun ilk hata ya da uyarısı, ilk cümlesine kadar ve çalışma alanı yolu olmadan; yoksa 'hata yok'."""
    lines = res.errors or [w for w in res.warnings if "WARNING" in w]
    if not lines:
        return "başarılı, hata ya da LTspice uyarısı yok"
    text = lines[0].split(" | satır:")[0]
    text = re.sub(r"^\S*[\\/]([^\\/\s]+\.net\(\d+\)):", r"\1:", text)  # C:\...\t1_3.net(3): -> t1_3.net(3):
    return text.split("; ")[0].rstrip(".") + "."


def build_report(m, elapsed, fast):
    rep = SimulationReport(
        "Süreç ve bellek sağlamlaştırma", "Gemini önerileri: çökme, yetim süreç, disk, bellek, semboller, DRC",
        "Her öneri bu makinede ölçüldü; tutanlar uygulandı, tutmayanlar gerekçesiyle reddedildi.",
        meta=[("LTspice", LTSpiceAdapter().ltspice_version() or "?"), ("Makine", "i7-1255U, Windows 11"),
              ("Süre", f"{elapsed:.0f} sn")],
        disclaimer=("Ölçümler bu makinedeki süreç ve dosya davranışıdır; devre sayıları LTspice simülasyonudur, "
                    "donanım ölçümü değildir."),
    )
    rep.footer = ("Yeniden üretmek için: `python examples/16_process_and_memory_hardening.py`. Uygulanan değişiklikler: "
                  "`ltspice_llm/adapter.py` (`_ProcessJob`, `_crash_description`, `cleanup_workspace`), `parser.open_raw`, "
                  "`visualizer.PLOT_BUCKETS`, `ltspice_llm/symbols.py`. Testler: `tests/test_robustness.py`, "
                  "`tests/test_symbols.py`.")

    # ---- T1 çökme
    cases, timing = m["t1"]
    rows1 = [(f"{label} (rc 0x{c['rc']:08X})", {"crash": 1.0 if c["rc"] >= 0xC0000000 else 0.0,
                                                 "ok": 1.0 if c["result"].success else 0.0})
             for label, c in cases.items()]
    t_rows = {}
    for row in timing["rows"]:
        t_rows.setdefault(row["variant"], []).append(row)
    trow = [(v, {"min": min(r["seconds"] for r in rs), "max": max(r["seconds"] for r in rs),
                 "wer": float(sum(r["werfault"] for r in rs))}) for v, rs in t_rows.items()]
    none_t = med([r["seconds"] for r in t_rows["iş nesnesi yok"]])
    die_t = med([r["seconds"] for r in t_rows["KILL_ON_JOB_CLOSE + DIE_ON_UNHANDLED"]])
    msg = {label: _first_line(c["result"]) for label, c in cases.items()}
    rep.add_study(Study(
        "T1", "LTspice nasıl çöküyor, çökme ne kadar bekletiyor?",
        question=("Gemini: başsız LTspice çökünce WerFault süreci timeout boyunca asılı tutar; SetErrorMode ile "
                  "bastırılmalı; 0xC0000005/0xC00000FD/0xC0000094 kodları anlamlandırılmalı. Hangi devre çökertiyor, "
                  "adaptör bunu ne olarak raporluyordu, WerFault gerçekte ne kadar bekletiyor?"),
        method=("Beş aday devre doğrudan `LTspice.exe -b` ile (çıkış kodu) ve adaptörle (hata metni) koşuldu. Süre: "
                "çöken devre, alt süreçte Windows'un varsayılan hata kipiyle (CREATE_DEFAULT_ERROR_MODE; Claude "
                f"Code'un miras kipi {timing['error_mode']} değil), üç iş nesnesi yapılandırmasıyla 3'er kez. Tek "
                "değişken iş nesnesinin bayrağı; WerFault süreci psutil ile `-p <pid>` argümanından arandı."),
        table=Table([Column("crash", "çöktü (1/0)", decimals=0), Column("ok", "adaptör başarılı (1/0)", decimals=0)],
                    rows1, caption="Adaptörün ilk hata satırı: "
                    + " · ".join(f"**{k}**: {v}" for k, v in msg.items())),
        charts=[BarChart("Çöken LTspice'ın dönüş süresi (medyan, varsayılan hata kipi)",
                         [(v, med([r["seconds"] for r in rs])) for v, rs in t_rows.items()], unit="sn", decimals=2,
                         highlight="KILL_ON_JOB_CLOSE + DIE_ON_UNHANDLED")],
        effects=[Effect("DIE_ON_UNHANDLED bayrağı", "LTspice'ın iş nesnesi", "çöken koşumun dönüşü (medyan)",
                        none_t, die_t, "sn", decimals=2, note="WerFault; Python'un kendi hata kipine dokunulmaz")],
        notes=["Süre aralığı: " + "; ".join(f"{v}: {format_value(r['min'], 'sn', decimals=2)}–"
                                             f"{format_value(r['max'], 'sn', decimals=2)}, WerFault {r['wer']:.0f}/3"
                                             for v, r in trow),
               "Adaptörün adları Windows ileti tablosuyla (ntdll.dll): "
               + "; ".join(f"{c} {label}: “{text.split('. ')[0].rstrip('.')}.”" for c, label, text in m["ntstatus"])],
    ))

    # ---- T2 yetim
    o = m["t2"]
    last = {k: v["samples"][-1] for k, v in o.items()}
    rep.add_study(Study(
        "T2", "Python ölünce LTspice yaşamaya devam ediyor mu?",
        question=("Gemini: Python aniden kapanınca LTspice.exe CPU yakmayı sürdürür; Job Object KILL_ON_JOB_CLOSE "
                  "eklenmeli. Bu makinede ölçülüyor mu, çekirdek bütçesi (cpu_guard) ne görüyor?"),
        method=("Ebeveyn Python `run_netlist` ile 20 sn'lik bir RC koşumu başlatır; LTspice görününce ebeveyn "
                "TerminateProcess ile (ağaç değil) öldürülür. Tek değişken: adaptörün iş nesnesine bağlama adımı "
                "(kapalı = eski davranış). 1, 5, 10 sn sonra LTspice yaşıyor mu, ne kadar CPU harcadı, bütçede kaç "
                "boş yuva var?"),
        table=Table([Column("alive_old", "yaşıyor (eski)", decimals=0), Column("cpu_old", "CPU (eski)", "sn", decimals=1),
                     Column("alive_new", "yaşıyor (iş nesnesi)", decimals=0),
                     Column("free", f"boş yuva / {default_pool().size}", decimals=0)],
                    [(f"ebeveyn öldükten {a['t']} sn sonra", {"alive_old": float(a["alive"]), "cpu_old": a["cpu"],
                                                               "alive_new": float(b["alive"]), "free": float(a["free"])})
                     for a, b in zip(o["0"]["samples"], o["1"]["samples"])],
                    caption=f"Ebeveyn yaşarken bütçede boş yuva: {o['0']['free_before']}/{default_pool().size}."),
        effects=[Effect("KILL_ON_JOB_CLOSE bayrağı", "LTspice'ın iş nesnesi",
                        "yetimin CPU süresi, 10 sn", last["0"]["cpu"],
                        last["1"]["cpu"] if last["1"]["alive"] else 0.0, "sn", decimals=1,
                        note="iş nesnesiyle LTspice 1 sn içinde ölü")],
    ))

    # ---- T3 .save
    s = m["t3"]
    full, pruned = s["full"], s["pruned"]
    same = all(r["result"].measurements == full[0]["result"].measurements for r in full + pruned)
    t3 = Study(
        "T3", ".save budaması ve keep_raw: ne kazandırıyor?",
        question=("Gemini: .save yoksa ölçülen sinyalleri otomatik .save yap (39,2 -> 1,4 MB, 28x); keep_raw=False ile "
                  ".raw'ı sonradan sil, simülasyon %70 kısalsın. Bugünkü ikili .raw varsayılanıyla ölçüm ne diyor?"),
        method=("examples/10'un 500 kHz buck'ı (2 ms, 20 ns). Tek değişken: `save_signals=['V(vout)']` (yalnızca "
                "ölçülen sinyal). Her biri 3 kez, sırayla; LTspice süresi logdaki `Total elapsed time`."),
        table=Table([Column("mb", ".raw", "MB", decimals=2), Column("wall", "duvar (medyan)", "sn", decimals=3),
                     Column("solver", "LTspice (medyan)", "sn", decimals=3)],
                    [("budamasız", {"mb": full[0]["mb"], "wall": med([r["wall"] for r in full]),
                                    "solver": med([r["solver"] for r in full])}),
                     ("save_signals", {"mb": pruned[0]["mb"], "wall": med([r["wall"] for r in pruned]),
                                       "solver": med([r["solver"] for r in pruned])})],
                    caption=f"Ölçümler 6 koşumda birebir aynı: {'evet' if same else 'HAYIR'}. 39,2 MB eski ASCII "
                            "varsayılanınındı (LESSONS_LEARNED_PERFORMANCE §0)."),
        effects=[Effect("save_signals (.save V(vout))", "buck, .raw", ".raw boyutu", full[0]["mb"], pruned[0]["mb"],
                        "MB", decimals=2),
                 Effect("save_signals (.save V(vout))", "buck", "LTspice süresi (medyan)",
                        med([r["solver"] for r in full]), med([r["solver"] for r in pruned]), "sn", decimals=3),
                 Effect("save_signals (.save V(vout))", "buck", "duvar süresi (medyan)",
                        med([r["wall"] for r in full]), med([r["wall"] for r in pruned]), "sn", decimals=3)],
    )
    x_f, y_f = read_trace(full[-1]["result"], "V(vout)")
    x_p, y_p = read_trace(pruned[-1]["result"], "V(vout)")
    t3.charts.append(LineChart("V(vout): budamasız ve save_signals üst üste",
                               [Trace("budamasız", x_f, y_f, emphasis=False), Trace("save_signals", x_p, y_p)],
                               x_range=(1.5e-3, 2e-3)))
    rep.add_study(t3)
    rep.add_runs("T3", [(f"{k} #{i + 1}", r["result"]) for k, v in s.items() for i, r in enumerate(v)])

    # ---- T4 bellek
    if not fast:
        mm = m["t4"]
        r4 = mm["runs"]
        rows4 = [(label, {"mb": med([x["peak_delta_mb"] for x in r4[k]]), "s": med([x["seconds"] for x in r4[k]])})
                 for label, k in (("özet, tembel (eski)", "summ_lazy"), ("özet, tek geçiş", "summ_single"),
                                  ("çizim, tam çözünürlük (eski)", "plot_full"), ("çizim, min/max 2000 kova",
                                                                                  "plot_minmax"))]
        w, h, frac = mm["pixels"]
        rep.add_study(Study(
            "T4", "Büyük .raw RAM'i şişiriyor mu, LTTB çözüm mü?",
            question=("Gemini: milyon noktalı dalga formları RAM'i şişirir; summarize_file ve visualizer'a LTTB "
                      "eklenmeli (200 MB -> 500 KB), LTTB tepe ve dipleri birebir korur."),
            method=(f"Aynı buck 20 ms: {format_value(mm['raw_mb'], 'MB', decimals=1)} ikili .raw, "
                    f"{mm['points']:,} nokta, {mm['signals']} sinyal. ".replace(",", ".")
                    + "Her görev ayrı süreçte 2 kez; tepe çalışma kümesinin (peak_wset) taban üstü artışı. Tek "
                    "değişkenler: özet için okuma biçimi (tembel / tek geçiş), çizim için seyreltme (kapalı / 2000 "
                    "kova). Uç değer: V(vsw) eşit süreli 1000 kovaya bölünür, seçilen noktalarda (LTTB "
                    f"{mm['selected']['LTTB, 2000 nokta']}, min/max {mm['selected']['min/max, 1000 kova']} nokta) "
                    "kovanın tepesi ve dibi var mı?"),
            table=Table([Column("mb", "tepe bellek artışı", "MB", decimals=0), Column("s", "süre", "sn", decimals=2)],
                        rows4, caption=(f"Özet değerleri iki okumada birebir aynı: "
                                        f"{'evet' if mm['same_summary'] else 'HAYIR'}. Çizim pikselleri ({w}×{h}): "
                                        f"{format_value(frac * 100, '%', decimals=4)} farklı.")),
            charts=[BarChart("1000 kovada kaybolan uç değer (V(vsw))",
                             [(f"{k}: tepe", float(v[0])) for k, v in mm["lost"].items()]
                             + [(f"{k}: dip", float(v[1])) for k, v in mm["lost"].items()],
                             decimals=0, highlight="min/max, 1000 kova: tepe")],
            effects=[Effect("open_raw(traces='*')", "RawParser.summarize_file", "tepe bellek artışı",
                            rows4[0][1]["mb"], rows4[1][1]["mb"], "MB", decimals=0),
                     Effect("min/max seyreltme (PLOT_BUCKETS)", "WaveformVisualizer.plot_traces", "çizim süresi",
                            rows4[2][1]["s"], rows4[3][1]["s"], "sn", decimals=2),
                     Effect("min/max seyreltme (PLOT_BUCKETS)", "WaveformVisualizer.plot_traces",
                            "tepe bellek artışı", rows4[2][1]["mb"], rows4[3][1]["mb"], "MB", decimals=0)],
            notes=["Global uç değer korundu mu (tepe, dip): " + "; ".join(
                f"{k}: {'evet' if a else 'HAYIR'}, {'evet' if b else 'HAYIR'}" for k, (a, b) in mm["global"].items())],
        ))
        rep.add_runs("T4", [("buck 20 ms", mm["result"])])

    # ---- T5 semboller
    sy = m["t5"]
    rep.add_study(Study(
        "T5", "ADI sembollerinin pinleri .asy'den okunabilir mi?",
        question=("Gemini: SYMBOL_PIN_DEFS yalnızca 24 sembol biliyor; lib/sym'deki 4200+ sembol ~0,4 sn'de taranıp JSON "
                  "indeksine yazılmalı, AscSchematic karmaşık entegrelerin pinlerine bağlanabilmeli."),
        method=("lib/sym'deki bütün .asy dosyaları sayıldı ve ayrıştırıldı (süre). LT8610 (13 pin) iki dönüşle "
                "yerleştirildi, her pinine adıyla ağ etiketi kondu; LTspice'ın kendi netlisterı bağlantıyı ve düğüm "
                "sırasını verdi. LT1001 gerilim izleyici kütüphane pinleriyle kurulup .op koşuldu."),
        table=Table([Column("n", "adet", decimals=0)], [
            ("lib/sym'deki sembol", {"n": float(sy["count"])}),
            ("UTF-16LE sembol", {"n": float(sy["enc"].get("utf-16-le", 0))}),
            ("bazı pinlerinin PinName'i olmayan sembol", {"n": float(sy["missing_name"])}),
            (f"LT8610 bağlantısız pin (iki dönüş, {2 * sy['lt8610_pins']} pin)", {"n": float(sy["lt8610_unconnected"])}),
        ], caption=(f"Her PIN'in SpiceOrder'ı var: {'evet' if sy['all_have_order'] else 'HAYIR'}. LT8610 düğüm sırası "
                    f"LTspice'ın `§pnba` sırasıyla aynı: {'evet' if sy['lt8610_in_order'] else 'HAYIR'}. LT1001 izleyici: "
                    f"V(in) = 1,25 V, `.meas` V(out) = "
                    f"{format_value(sy['follower'].measurements.get('vout'), 'V', decimals=6)}.")),
        charts=[BarChart("Kütüphane taraması", [("dosya adlarını listeleme", sy["t_list"]),
                                                ("hepsini okuyup ayrıştırma", sy["t_parse"])], unit="sn", decimals=2,
                         highlight="dosya adlarını listeleme",
                         caption="Sembol gerektiğinde tek dosya okunur; JSON indeksi gereksiz.")],
    ))
    rep.add_runs("T5", [("LT1001 izleyici", sy["follower"])])

    # ---- T6 DRC
    d = m["t6"]
    rows6 = []
    for label, res in d.items():
        rows6.append((label, {"ok": 1.0 if res.success else 0.0, "err": float(len(res.errors)),
                              "warn": float(len([w for w in res.warnings if "WARNING" in w]))}))
    rep.add_study(Study(
        "T6", "Önerilen DRC kuralları LTspice'ın bildirmediği bir şey buluyor mu?",
        question=("Gemini: -netlist çıktısından açık girişler, paralel gerilim kaynakları, zıt diyotlar, ters "
                  "polcap ve saat hattında aşırı kapasitif yük için kural denetçisi."),
        method="Her kural için en küçük devre; LTspice'ın ve adaptörün (teşhis motoru) söylediği.",
        table=Table([Column("ok", "başarılı (1/0)", decimals=0), Column("err", "hata", decimals=0),
                     Column("warn", "LTspice uyarısı", decimals=0)], rows6,
                    caption=" · ".join(f"**{k}**: {_first_line(r)}"
                                       + (f" (teşhis: {r.diagnostics['issue_type']})" if r.diagnostics else "")
                                       for k, r in d.items())),
        notes=["Ters paralel diyot geçerli bir kıskaç devresi: kural hatalı alarm verir. Ters polcap netlistten değil "
               "çalışma noktası geriliminden anlaşılır (V(+) − V(−) < 0); saat hattındaki kapasitif yük de bir kural "
               "değil, kenar/tepe ölçümüdür (`.meas`). Gerçek boşluk tek bağlantılı düğüm: LTspice sessiz, koşum "
               "başarılı; şemalarda `inspect_schematic` bunu buluyor, netlistlerde run_netlist artık uyarıyor "
               "(examples/17 T4)."],
    ))
    # T6 koşumları bilerek hatalı devreler: güvenilirlik denetimine (başarılı koşum oranı) girmez

    # ---- T7 ortalama
    mean = m["t7"]
    rep.add_study(Study(
        "T7", "Yan bulgu: özetteki ortalama zaman ağırlıklı mı?",
        question=("LLM'e giden dalga formu özetindeki `mean_val` LTspice'ın `.meas AVG`'siyle aynı mı? LTspice zaman "
                  "adımı değişkendir."),
        method=("examples/10 buck'ına `.meas tran ... AVG` eklendi (tüm pencere). Özet ortalaması, .raw'dan yamuk "
                "integrali ve LTspice ölçümü karşılaştırıldı."),
        table=Table([Column("meas", ".meas AVG", "V", decimals=4), Column("trapz", ".raw yamuk ortalaması", "V",
                                                                           decimals=4),
                     Column("summary", "özet mean_val", "V", decimals=4)], mean["rows"],
                    caption=f"En büyük / en küçük zaman adımı oranı: {mean['dt_ratio']:.3g}."),
    ))
    rep.add_runs("T7", [("buck + AVG", mean["result"])])

    # ---- T8 jitter
    j = m["t8"]
    t8 = Study(
        "T8", "Göz diyagramı ve jitter bu simülasyonda bir şey ölçer mi?",
        question=("Gemini: SPI/UART/CAN hatları için göz diyagramı, jitter (geçiş zamanı varyansı), gürültü marjı ve "
                  "setup/hold ihlali analizi eklenmeli."),
        method=("Kart üstü bir SPI saat hattı: 9 MHz PULSE, 6 ns kenar, 33 Ω seri, 70 nH iz, 16 pF yük; "
                "2,3 µs, 0,05 ns en büyük adım. V(r)'nin 1,65 V'tan yükselen geçişleri .raw'dan doğrusal ara değerle "
                "bulundu (ilk 3 geçiş oturma); periyot sapması ve doğrusal uyumdan zaman hatası (TIE)."),
        table=Table([Column("v", "değer", "ps", 1e-12, 6)], [
            ("ortalama periyot", {"v": j["period_mean"]}),
            ("periyot standart sapması (jitter)", {"v": j["period_std"]}),
            ("zaman hatası tepeden tepeye (TIE)", {"v": j["tie_pp"]}),
        ], caption=(f"{j['edges']} geçiş; sapma {j['period_std']:.1e} s, TIE {j['tie_pp']:.1e} s (çift duyarlıklı "
                    f"kayan noktanın artığı). Kaynak periyodu {format_value(SCK_PERIOD, 'ns', 1e-9, 1)}. Tepe/dip "
                    f"(`.meas`, 1-2,3 µs): {format_value(j['result'].measurements.get('vr_max'), 'V', decimals=3)} / "
                    f"{format_value(j['result'].measurements.get('vr_min'), 'V', decimals=3)}.")),
        notes=["LTspice transient'i gürültüsüz ve deterministiktir: ideal kaynaklı hatta jitter sayısal artıktır, göz "
               "yalnızca çınlamayla kapanır ve bunu tepe/dip `.meas`'i zaten ölçüyor (ör. SCK tepesi, ESD akımı). Rastgele jitter için kaynağa gürültü modeli, setup/hold için alıcının zamanlama verisi gerekir. "
               "Uygulanmadı."],
    )
    xs, ys = read_trace(j["result"], "V(r)")
    t8.charts.append(LineChart("SCK, alıcıda V(r)", [Trace("V(r)", xs, ys, label_extremes=("max", "min"))],
                               # pencere düşük seviyede başlar: sol kenardaki eşik/VDD etiketleri izle çakışmaz
                               x_range=(1.08e-6, 1.08e-6 + 3 * SCK_PERIOD), references=[(1.65, "eşik"), (3.3, "VDD")]))
    rep.add_study(t8)
    rep.add_runs("T8", [("SCK 9 MHz", j["result"])])

    # ---- özet ve kararlar
    rep.add_tile("Çöken LTspice dönüşü", f"{format_value(none_t, 'sn', decimals=2)} → {format_value(die_t, 'sn', decimals=2)}",
                 "WerFault yerine iş nesnesi bayrağı (T1)")
    rep.add_tile("Yetim LTspice, 10 sn", f"{format_value(last['0']['cpu'], 'CPU-sn', decimals=1)} → ölü", "T2")
    solver_gain = (1 - med([r["solver"] for r in pruned]) / med([r["solver"] for r in full])) * 100
    rep.add_tile(".save budaması", f"{full[0]['mb'] / pruned[0]['mb']:.1f}x küçük",
                 f"LTspice süresi {format_value(solver_gain, '%', decimals=0)} kısa; Gemini: %70 (T3)")
    if not fast:
        rep.add_tile("Özet tepe belleği", f"{rows4[0][1]['mb']:.0f} → {rows4[1][1]['mb']:.0f} MB", "tek geçiş (T4)")
    rep.add_decision("good", "Uygulandı", "İş nesnesi (KILL_ON_JOB_CLOSE + DIE_ON_UNHANDLED_EXCEPTION), çökme kodunun "
                     "raporlanması, .raw'ın tek geçişte okunması, çizimde min/max seyreltme, `_batch_*` temizliği ve "
                     "isteğe bağlı kota (`workspace_quota_mb`), .asy pinleri.")
    rep.add_decision("serious", "Reddedildi", "SetErrorMode (Python'un kendi kipini de değiştirir; iş nesnesi bayrağı "
                     "aynı sonucu yalnızca LTspice'a verir), keep_raw ile '%70 kısalma', varsayılan otomatik .save "
                     "(özet ve grafikler ölçülmeyen sinyalleri kaybeder; 28x değil 5x), LTTB (uç değerleri kaybediyor), "
                     f"JSON sembol indeksi (listeleme {format_value(sy['t_list'], 'sn', decimals=2)}), göz diyagramı/"
                     "jitter (T8) ve DRC kuralları (T6).")
    rep.add_finding(Finding(
        "LTspice kendini çağıran tanımda hata yazmak yerine çöküyor",
        "LTspice.exe 'Infinite subckt recursion.' dizgisini içeriyor ama 26.1.1 kendini çağıran .subckt ve .func'ta "
        "0xC00000FD ile çöküyor; stderr boş, log yalnızca başlık.",
        "Adaptör hata önem dereceli her NTSTATUS'u (ve POSIX sinyalini) çökme olarak yazıyor; 0xC00000FD'ye ölçülen "
        "neden ekleniyor. Eskiden 'dalga formu üretilemedi, sözdizimini kontrol edin' deniyordu.",
        "tests/test_robustness.py (kayıtlı çıkış kodu), tests/test_integration_reference.py (gerçek LTspice).",
        "warn", "LTspice davranışı"))
    rep.add_finding(Finding(
        "Yetim LTspice çekirdek bütçesini atlatıyor",
        "cpu_guard'ın dosya kilitleri ebeveyn ölünce işletim sistemince bırakılıyor; yetim LTspice ise çalışmayı "
        "sürdürüyor.",
        "İş nesnesi yetimi ebeveynle birlikte öldürüyor; bütçe ile gerçek yük yeniden tutarlı.",
        f"Eski davranışta bütçe ebeveyn ölünce {o['0']['samples'][0]['free']}/{default_pool().size} boş görünürken "
        "yetim çalışıyordu (livelock koşulu, LESSONS_LEARNED_PERFORMANCE §4).", "serious", "eşzamanlılık"))
    rep.add_finding(Finding(
        "Özetteki ortalama örnek ortalamasıydı (sonra düzeltildi)",
        "LTspice'ın zaman adımı değişken; `WaveformSummary.mean_val`/`rms_val` örneklerin aritmetik ortalamasıydı.",
        "Bu turda düzeltilmedi; examples/17 T2'de zaman ağırlıklı yapıldı. T7 tablosu artık özetle .meas'i aynı "
        "gösterir (eski sapma: anahtarlama düğümünde %3,0).",
        "T7 tablosu ve examples/17 T2.", "warn", "sonradan düzeltildi"))

    rep.add_check("Ölçümler budamayla değişmedi (T3)", "evet" if same else "HAYIR", "good" if same else "critical")
    if not fast:
        rep.add_check("Özet değerleri tek geçişte aynı (T4)", "evet" if m["t4"]["same_summary"] else "HAYIR",
                      "good" if m["t4"]["same_summary"] else "critical")
    rep.add_check("Çökme süresi ölçümünde hata kipi", str(timing["error_mode"]), "good" if timing["error_mode"] == 0
                  else "warn", "0 = Windows varsayılanı (WER açık); 3 olsaydı WerFault hiç çalışmazdı.")
    rep.add_check("Ölçüm sırasında makinenin CPU yükü (koşum sonu)", f"%{psutil.cpu_percent(interval=1):.0f}", "warn",
                  "Süreler arka plan yüküyle değişir (açık tarayıcı ve editör 1-2 çekirdek kullanıyordu); belgedeki "
                  "süreler beş koşumun aralığıdır. Bellek, dosya boyutu, sayımlar ve çıkış kodları yükten bağımsız.")
    rep.assumptions = [
        ("Tek makine", "i7-1255U, Windows 11 Pro 26200, LTspice 26.1.1; WER varsayılan (DontShowUI tanımsız)."),
        ("Çökme örneği", "Ölçülen tek LTspice çökmesi kendini çağıran tanım; 0xC0000005 ve 0xC0000094 LTspice'ta "
                         "üretilemedi (tamsayı 1/0 hata mesajı verdi, B kaynağında 1/0 başarılı)."),
        ("Bellek", "peak_wset Windows'un tepe çalışma kümesi; taban importlardan sonra alındı."),
    ]
    rep.open_items = [
        "Masaüstü/iş istasyonunda T1-T4 tekrar ölçülmeli.",
        "Tek bağlantılı düğüm (T6) ve özet ortalaması (T7) examples/17'de kapatıldı.",
    ]
    return rep


def main():
    if psutil is None:
        raise SystemExit("Bu ölçüm psutil ister: pip install psutil")
    if len(sys.argv) > 2 and sys.argv[1] == "--alt":
        task = sys.argv[2]
        if task == "crash-timing":
            return child_crash_timing()
        if task == "orphan-parent":
            return child_orphan_parent(sys.argv[3])
        if task == "memory":
            return child_memory(sys.argv[3])
        raise SystemExit(f"bilinmeyen alt görev {task}")
    if sys.platform != "win32":
        raise SystemExit("Bu ölçüm Windows ister (WerFault, iş nesnesi).")
    fast = "--hizli" in sys.argv
    WS.mkdir(parents=True, exist_ok=True)
    adapter = LTSpiceAdapter(workspace_dir=WS, default_timeout=120)
    t0 = time.perf_counter()
    m = {"ntstatus": ntstatus_messages()}
    print("T1 çökme...", flush=True)
    m["t1"] = t1_crash(adapter)
    for label, c in m["t1"][0].items():
        print(f"   {label:32s} rc=0x{c['rc']:08X} adaptör: {(c['result'].errors or ['başarılı'])[0][:100]}")
    for row in m["t1"][1]["rows"]:
        print(f"   {row['variant']:40s} {row['seconds']:.2f} sn WerFault={row['werfault']}")
    print("T2 yetim süreç...", flush=True)
    m["t2"] = t2_orphan()
    for k, v in m["t2"].items():
        print(f"   iş nesnesi={'var' if k == '1' else 'yok'}: önce boş {v['free_before']}, {v['samples']}")
    print("T3 .save...", flush=True)
    m["t3"] = t3_save(adapter)
    for k, rows in m["t3"].items():
        print(f"   {k}: " + ", ".join(f"{r['mb']:.2f} MB {r['wall']:.3f}/{r['solver']:.3f} sn" for r in rows))
    if not fast:
        print("T4 bellek...", flush=True)
        m["t4"] = t4_memory(adapter)
        for k, rows in m["t4"]["runs"].items():
            print(f"   {k}: " + ", ".join(f"{r['peak_delta_mb']:.0f} MB {r['seconds']:.2f} sn" for r in rows))
        print(f"   kayıp uç değer {m['t4']['lost']}, global {m['t4']['global']}, piksel {m['t4']['pixels']}")
    print("T5 semboller...", flush=True)
    m["t5"] = t5_symbols(adapter)
    print(f"   {m['t5']['count']} sembol, liste {m['t5']['t_list']:.3f} sn, ayrıştırma {m['t5']['t_parse']:.2f} sn, "
          f"{m['t5']['enc']}, PinName eksik {m['t5']['missing_name']}, izleyici {m['t5']['follower'].measurements}")
    print("T6 DRC...", flush=True)
    m["t6"] = t6_drc(adapter)
    for k, r in m["t6"].items():
        print(f"   {k}: success={r.success} {(r.errors or r.warnings or ['sessiz'])[0][:90]}")
    print("T7 ortalama...", flush=True)
    m["t7"] = t7_mean(adapter)
    for sig, row in m["t7"]["rows"]:
        print(f"   {sig}: .meas {row['meas']:.5f} yamuk {row['trapz']:.5f} özet {row['summary']:.5f}")
    print("T8 jitter...", flush=True)
    m["t8"] = t8_jitter(adapter)
    j = m["t8"]
    print(f"   {j['edges']} geçiş, periyot {j['period_mean'] * 1e9:.6f} ns, sapma {j['period_std'] * 1e12:.4f} ps, "
          f"TIE p-p {j['tie_pp'] * 1e12:.4f} ps, {j['result'].measurements}")
    elapsed = time.perf_counter() - t0
    rep = build_report(m, elapsed, fast)
    save_report(rep, report_path("16_process_and_memory_hardening.html"))
    removed = adapter.cleanup_workspace(0)  # 67 MB'lık .raw ve ölçüm dosyaları kalmasın
    print(f"{removed} çalışma dosyası silindi, {elapsed:.0f} sn")


if __name__ == "__main__":
    main()
