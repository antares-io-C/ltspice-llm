"""
Örnek 14: Use case testlerinin yeniden oynattığı gerçek LTspice çıktılarını kaydeder.

``tests/test_usecase_replay.py`` sahte bir LTspice süreci kullanır: süreç başlatılınca bu betiğin gerçek LTspice ile
ürettiği ``.log``/``.raw`` dosyalarını netlistin yanına yazar. Böylece başarı yolu (log ayrıştırma, raw özeti)
LTspice'sız CI'da da gerçek çıktıya karşı sınanır (GEMINI.md: LTspice davranışını taklit eden fixture gerçek
bir log olmalı). LTspice güncellendiğinde yeniden çalıştırın:

    python examples/14_record_flow_fixtures.py          # hepsi
    python examples/14_record_flow_fixtures.py mc       # yalnızca adı verilen kayıtlar

Kayıt adları: flow, mc, safety, schematic, crash, mean, stepraw, tstart, ini, measnames.

Kayıtlarda çalışma alanının yolu ``C:\\ws`` olur (log, netlist, ``.raw`` başlığı, ``LTspice.ini``): depo public, gerçek
yol kullanıcı adını ve klasör düzenini gösterir (``tests/test_fixture_privacy.py``).
"""

import json
import re
import subprocess
import sys
from pathlib import Path

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ltspice_llm import AscSchematic, LTSpiceAdapter, handle_tool_call  # noqa: E402
from ltspice_llm.parser import LogParser  # noqa: E402

DATA = ROOT / "tests" / "data"
RC = """* rc flow fixture
V1 in 0 PULSE(0 5 0 1n 1n 1m 2m)
R1 in out 1k
C1 out 0 1u
.tran 3m
.save V(in) V(out)
.meas tran v_max MAX V(out)
.meas tran vr_max MAX V(in,out)
.end
"""
# .op koşumunun raw dosyasında eksen yoktur (read_trace bunu ValueError ile bildirir)
DIVIDER = "* op divider\nV1 in 0 10\nR1 in out 1k\nR2 out 0 1k\n.op\n.end\n"
# Tarama dışı sözdizimi hatası: LTspice 'Expected device instantiation or directive here.' yazar
SYNTAX = "* syntax\nV1 in 0 1\nR1 in 0 1k\n1k R2 in 0\n.tran 1m\n.end\n"
# simulate_monte_carlo verim hesabı (tests/test_tools_and_agent.py) bu bölücünün 5 adımlık logunu yeniden oynatır.
# Başarısız adım spec'i sağlamamış sayılmalı: t_hi yalnızca V(out) > 5.1 V olan adımlarda üretilir (V(r) 1 ms'de
# 0 -> 10 V rampa, V(r)*V(out)/10 en çok V(out)'a çıkar); v_never hiçbir adımda üretilmez (V(out) <= 5.25 V < 20 V).
MC_DIVIDER = ("* mc divider\nV1 in 0 10\nR1 in out 10k\nR2 out 0 10k\nV2 r 0 PWL(0 0 1m 10)\n.tran 1m\n"
              ".save V(out) V(r)\n.meas tran v_out AVG V(out)\n.meas tran t_hi WHEN V(r)*V(out)/10=5.1\n"
              ".meas tran v_never FIND V(out) WHEN V(out)=20\n.end\n")
# check_design_safety ölçüm adları (tests/test_tools_and_agent.py). Elle: v_c10 = 30 V, vhalf_c10 = V(b) = 15 V
# (aynı bileşene ikinci, küçük ölçüm: en kötüsü seçilmeli); R1 üzerinde 15 V -> pavg_r1 = 15^2 / 1k = 0.225 W;
# ters kutuplu D1 ve kesimdeki Q1 (C-E) üzerinde 30 V (yarı iletken derating katsayısı); v_c11 = -30 V (mutlak).
# C10 ile C1'in, AVG'deki V ile gerilimin karışmadığı sınanır.
SAFETY = """* safety fixture
V1 a 0 30
C10 a 0 1u
R1 a b 1k
R2 b 0 1k
D1 0 a DX
Q1 a 0 0 QX
V2 c 0 -30
C11 c 0 1u
.model DX D
.model QX NPN
.tran 1m
.meas tran v_c10 MAX V(a)
.meas tran vhalf_c10 MAX V(b)
.meas tran pavg_r1 AVG V(a,b)*I(R1)
.meas tran v_d1 MAX V(a)
.meas tran v_q1 MAX V(a)
.meas tran v_c11 MIN V(c)
.end
"""


# Kayıt makinesinin yolu: sürücülü (C:\...) ya da UNC (\\sunucu\...); temizlenmiş hâli C:\ws
_MACHINE_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'<>|*?]*|(?<![\\\w])\\\\[^\\\s]+\\")


def scrub_raw(data, workspace):
    """
    ``.raw`` başlığındaki çalışma alanı yolunu logdaki gibi ``C:\\ws`` yapar (``Title:`` netlistin mutlak yoludur);
    ``Binary:``/``Values:`` satırından sonraki veriye dokunmaz. Başlık ikili ``.raw``'da UTF-16LE, ASCII ``.raw``'da tek
    baytlık metin; ASCII dışı harfli yol UTF-8 sayılır. Yol sessizce kalmaz: başlık çözülemezse ya da temizlendikten
    sonra ``C:\\ws`` dışında bir yol taşıyorsa (çalışma alanı başlıkta başka yazılmışsa) hata verir.
    """
    enc = "utf-16-le" if data[1:2] == b"\x00" else "utf-8"
    for marker in ("Binary:", "Values:"):
        end = data.find(marker.encode(enc))
        if end != -1:
            break
    else:
        raise ValueError("'.raw' başlığı bulunamadı (Binary:/Values: satırı yok)")
    header = data[:end].decode(enc).replace(str(workspace), r"C:\ws")
    left = [p for p in _MACHINE_PATH.findall(header) if not re.match(r"C:\\ws(?:\\|$)", p)]
    if left:
        raise ValueError(f"'.raw' başlığında kayıt yolu kaldı: {left}")
    return header.encode(enc) + data[end:]


def copy_raw(src, dst, workspace):
    """``src``'yi ``dst``'ye yazar; başlıktaki çalışma alanı yolu ``C:\\ws`` olur (``scrub_raw``)."""
    Path(dst).write_bytes(scrub_raw(Path(src).read_bytes(), workspace))


def record_flow(workspace):
    adapter = LTSpiceAdapter(workspace_dir=workspace, solver_threads=1)
    res = adapter.run_netlist(RC, circuit_name="ref26_flow_rc", treat_as_text=True)
    if not res.success:
        raise SystemExit(f"Kayıt başarısız: {res.errors}")
    log = LogParser.read_text(res.log_path).replace(str(workspace), r"C:\ws")
    (DATA / "ref26_flow_rc.log.txt").write_text(log, encoding="utf-8", newline="")
    copy_raw(res.raw_path, DATA / "ref26_flow_rc.raw", workspace)
    (DATA / "ref26_flow_rc.net").write_text(RC, encoding="utf-8")
    op = adapter.run_netlist(DIVIDER, circuit_name="ref26_flow_op", treat_as_text=True)
    if not op.success:
        raise SystemExit(f"Kayıt başarısız: {op.errors}")
    copy_raw(op.raw_path, DATA / "ref26_flow_op.raw", workspace)
    print(f"vr_max = {res.measurements['vr_max']}, op raw {op.raw_path.stat().st_size} bayt")
    bad = adapter.run_netlist(SYNTAX, circuit_name="syntax_bad_line", treat_as_text=True)
    if bad.success or bad.log_path is None:
        raise SystemExit("Sözdizimi hatası beklenirdi")
    text = LogParser.read_text(bad.log_path).replace(str(workspace), r"C:\ws")
    (DATA / "ref26_syntax_bad_line.log.txt").write_text(text, encoding="utf-8", newline="")
    print(f"LTspice {adapter.ltspice_version()}: v_max = {res.measurements['v_max']}, "
          f"raw {res.raw_path.stat().st_size} bayt -> tests/data/ref26_flow_rc.*")


def record_monte_carlo(workspace):
    """Aracın kendi ürettiği Monte Carlo netlistinin (R1, R2 ±%5, 5 adım) logu."""
    adapter = LTSpiceAdapter(workspace_dir=workspace, solver_threads=1)
    handle_tool_call("create_or_replace_circuit", {"circuit_name": "ref26_mc_divider", "netlist_code": MC_DIVIDER},
                     adapter=adapter)
    res = handle_tool_call(
        "simulate_monte_carlo",
        {"circuit_name": "ref26_mc_divider", "tolerances": {"R1": 0.05, "R2": 0.05}, "runs": 5},
        adapter=adapter,
    )
    if res["status"] != "SUCCESS":
        raise SystemExit(f"Kayıt başarısız: {res.get('errors') or res.get('message')}")
    log = LogParser.read_text(workspace / "ref26_mc_divider_mc.log").replace(str(workspace), r"C:\ws")
    # LTspice 26.1.1 logu LF ve CRLF karışık; newline="" olmadan Windows CRLF'yi CRCRLF yapar ve araya giren boş
    # satır .step ölçüm tablosunu ilk satırda keser (ölçüldü: stepped_measurements boş kaldı).
    (DATA / "ref26_mc_divider.log.txt").write_text(log, encoding="utf-8", newline="")
    copy_raw(workspace / "ref26_mc_divider_mc.raw", DATA / "ref26_mc_divider.raw", workspace)
    (DATA / "ref26_mc_divider.net").write_text(MC_DIVIDER, encoding="utf-8", newline="")  # testin girdisi
    print(f"LTspice {adapter.ltspice_version()}: v_out adımları {res['step_statistics']['v_out']} "
          f"-> tests/data/ref26_mc_divider.*")


def record_safety(workspace):
    adapter = LTSpiceAdapter(workspace_dir=workspace, solver_threads=1)
    res = adapter.run_netlist(SAFETY, circuit_name="ref26_safety", treat_as_text=True)
    if not res.success:
        raise SystemExit(f"Kayıt başarısız: {res.errors}")
    log = LogParser.read_text(res.log_path).replace(str(workspace), r"C:\ws")
    (DATA / "ref26_safety.log.txt").write_text(log, encoding="utf-8", newline="")
    print(f"LTspice {adapter.ltspice_version()}: {res.measurements} -> tests/data/ref26_safety.log.txt")


def v_r_schematic(ground_r1):
    """create_or_replace_schematic (tests/test_tools_and_agent.py): V1 (5 V) + -> R1; R1'in 2. pini topraklı/boşta."""
    return {
        "symbols": [
            {"name": "voltage", "x": 100, "y": 200, "inst_name": "V1", "value": "5"},
            {"name": "res", "x": 300, "y": 200, "inst_name": "R1", "value": "1k"},
        ],
        "connections": [{"from_inst": "V1", "from_pin": "+", "to_inst": "R1", "to_pin": "1"}],
        "grounds": [[100, 296], [316, 296]] if ground_r1 else [[100, 296]],
        "directives": [".op", ".meas op ir FIND I(R1)"],
    }


def record_schematic(workspace):
    """
    İki şemanın girdisi (``.json``, testler buradan okur), LTspice ``-netlist``'in ham çıktısı (``.net``) ve bağlı
    şemanın simülasyonu (``.log.txt``/``.raw``); sahte netlister ve sahte simülasyon süreci bunları yeniden oynatır.
    """
    import ltspice_llm.adapter as adapter_mod

    adapter = LTSpiceAdapter(workspace_dir=workspace, solver_threads=1)
    real_run, raw = adapter_mod.subprocess.run, {}

    def keep_raw_netlist(cmd, **kwargs):  # schematic_to_netlist geçici .net'i siler: baytları silinmeden al
        proc = real_run(cmd, **kwargs)
        out = Path(cmd[-1]).with_suffix(".net")
        if out.exists():
            raw["net"] = out.read_bytes()
        return proc

    for label, grounded in (("connected", True), ("open_pin", False)):
        data = v_r_schematic(grounded)
        (DATA / f"ref26_sch_{label}.json").write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
        adapter_mod.subprocess.run = keep_raw_netlist
        try:
            net = adapter.schematic_to_netlist(AscSchematic.from_dict(data).render(),
                                               circuit_name=f"ref26_sch_{label}", treat_as_text=True)
        finally:
            adapter_mod.subprocess.run = real_run
        # Ham baytlar; yalnızca 1. satırdaki (yorum) geçici yol diğer kayıtlardaki gibi C:\ws olur
        (DATA / f"ref26_sch_{label}.net").write_bytes(raw.pop("net").replace(str(workspace).encode(), rb"C:\ws"))
        print(f"LTspice {adapter.ltspice_version()}: tests/data/ref26_sch_{label}.*\n{net}")

    res = handle_tool_call("create_or_replace_schematic",
                           {"circuit_name": "ref26_sch_connected", "schematic_data": v_r_schematic(True)}, adapter=adapter)
    if res["status"] != "SUCCESS":
        raise SystemExit(f"Kayıt başarısız: {res.get('message') or res.get('errors')}")
    log = LogParser.read_text(workspace / "ref26_sch_connected.log").replace(str(workspace), r"C:\ws")
    (DATA / "ref26_sch_connected.log.txt").write_text(log, encoding="utf-8", newline="")
    copy_raw(workspace / "ref26_sch_connected.raw", DATA / "ref26_sch_connected.raw", workspace)
    print(f"simülasyon: {res['measurements']} -> tests/data/ref26_sch_connected.log.txt/.raw")


# Kendini çağıran alt devre: LTspice 26.1.1 "Infinite subckt recursion." yazmak yerine yığın taşmasıyla çöküyor
# (0xC00000FD, stderr boş, log yalnızca başlık). Kendini çağıran .func de aynı kodu veriyor (examples/16).
CRASH = "* recursive subckt\nV1 in 0 1\nX1 in 0 LOOP\n.subckt LOOP a b\nR1 a b 1k\nX2 a b LOOP\n.ends\n.op\n.end\n"


def record_crash(workspace):
    """Çöken koşumun logu ve çıkış kodu; adaptörü atlayarak doğrudan LTspice ile (çıkış kodu kaybolmasın)."""
    adapter = LTSpiceAdapter(workspace_dir=workspace, solver_threads=1)
    workspace.mkdir(parents=True, exist_ok=True)
    net = workspace / "ref26_crash_recursive.net"
    net.write_text(CRASH.replace(".op\n", ".options threads=1\n.op\n"), encoding="utf-8")
    net.with_suffix(".log").unlink(missing_ok=True)
    proc = subprocess.run([str(adapter.executable_path), "-b", str(net)], capture_output=True, timeout=60)
    log = LogParser.read_text(net.with_suffix(".log")).replace(str(workspace), r"C:\ws")
    (DATA / "ref26_crash_recursive.log.txt").write_text(log, encoding="utf-8", newline="")
    (DATA / "ref26_crash_recursive.net").write_text(CRASH, encoding="utf-8", newline="")  # testin girdisi
    record = {"returncode": proc.returncode, "stderr": proc.stderr.decode("utf-8", "replace"),
              "ltspice": adapter.ltspice_version()}
    (DATA / "ref26_crash_recursive.json").write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    print(f"LTspice {record['ltspice']}: rc = 0x{proc.returncode & 0xFFFFFFFF:08X} -> tests/data/ref26_crash_recursive.*")


# Özetin ortalama ve RMS'i (tests/test_robustness.py): LTspice zaman adımı düzlükte 48,8 ns (Tstop/1024), kenarlarda
# 0,49 ps'ye iniyor (oran 1e5).
# Elle (V(in), ton + iki 10 ns kenar, periyot 10 µs): AVG = 5·(ton + 10n)/10u, RMS = √(25·(ton + 2·10n/3)/10u).
MEAN = """* time-weighted mean fixture
.param ton=1u
.step param ton list 1u 3u
V1 in 0 PULSE(0 5 0 10n 10n {ton} 10u)
R1 in out 100
C1 out 0 1n
.tran 50u
.save V(in) V(out)
.meas tran in_avg AVG V(in)
.meas tran in_rms RMS V(in)
.meas tran out_avg AVG V(out)
.meas tran out_rms RMS V(out)
.end
"""


def record_mean(workspace):
    adapter = LTSpiceAdapter(workspace_dir=workspace, solver_threads=1)
    res = adapter.run_netlist(MEAN, circuit_name="ref26_mean_pulse", treat_as_text=True)
    if not res.success:
        raise SystemExit(f"Kayıt başarısız: {res.errors}")
    log = LogParser.read_text(res.log_path).replace(str(workspace), r"C:\ws")
    (DATA / "ref26_mean_pulse.log.txt").write_text(log, encoding="utf-8", newline="")
    copy_raw(res.raw_path, DATA / "ref26_mean_pulse.raw", workspace)
    (DATA / "ref26_mean_pulse.net").write_text(MEAN, encoding="utf-8", newline="")
    table = {k: [row[k] for row in rows] for k, rows in res.stepped_measurements.items()}
    print(f"LTspice {adapter.ltspice_version()}: {table}, raw {res.raw_path.stat().st_size} bayt "
          "-> tests/data/ref26_mean_pulse.*")
    # .ac özeti zaman ağırlıklı değil, noktaların aritmetik ortalaması kalır (tests/test_robustness.py). Elle:
    # |H(f)| = 1/√(1 + (f/fc)²), fc = 1/(2π·1k·159n)
    ac = adapter.run_netlist(AC_RC, circuit_name="ref26_ac_rc", treat_as_text=True)
    if not ac.success:
        raise SystemExit(f"Kayıt başarısız: {ac.errors}")
    copy_raw(ac.raw_path, DATA / "ref26_ac_rc.raw", workspace)
    (DATA / "ref26_ac_rc.net").write_text(AC_RC, encoding="utf-8", newline="")
    print(f"ac raw {ac.raw_path.stat().st_size} bayt -> tests/data/ref26_ac_rc.*")


AC_RC = "* rc ac fixture\nV1 in 0 AC 1\nR1 in out 1k\nC1 out 0 159n\n.ac dec 10 10 100k\n.save V(in) V(out)\n.end\n"

# Steplenmiş .raw'ın okunamadığı iki durum (tests/test_robustness.py, open_raw): spicelib 1.6.3 steplenmiş ASCII .raw'ı
# hiç okuyamıyor; ikili .raw'ın adımlarını yanındaki .log'dan okuyor, .log yoksa iz okurken IndexError. Aynı devre iki
# biçimde. Elle (bölücü): V(out) = V1·1k/(r + 1k); r = 1k'da 0, 0,5, 1 V, r = 3k'da 0, 0,25, 0,5 V.
STEP_DC = ("* stepped dc divider\nV1 in 0 1\nR1 in out {r}\nR2 out 0 1k\n.step param r list 1k 3k\n.dc V1 0 2 1\n"
           ".save V(out)\n.end\n")


def record_step_raw(workspace):
    for fmt in ("binary", "ascii"):
        adapter = LTSpiceAdapter(workspace_dir=workspace, raw_format=fmt, solver_threads=1)
        res = adapter.run_netlist(STEP_DC, circuit_name=f"ref26_step_dc_{fmt}", treat_as_text=True,
                                  summarize_raw=False)
        if not res.success:
            raise SystemExit(f"Kayıt başarısız: {res.errors}")
        log = LogParser.read_text(res.log_path).replace(str(workspace), r"C:\ws")
        (DATA / f"ref26_step_dc_{fmt}.log.txt").write_text(log, encoding="utf-8", newline="")
        copy_raw(res.raw_path, DATA / f"ref26_step_dc_{fmt}.raw", workspace)
        print(f"LTspice {adapter.ltspice_version()}: {fmt} raw {res.raw_path.stat().st_size} bayt "
              f"-> tests/data/ref26_step_dc_{fmt}.*")
    (DATA / "ref26_step_dc.net").write_text(STEP_DC, encoding="utf-8", newline="")


# Tstart > 0'da zaman ekseni (tests/test_robustness.py). ADI SIMULATION-COMMANDS-REFERENCE .tran: "Tstart | Start time
# for saving data (data before this discarded)". LTspice 26.1.1 .raw eksenini yine 0'dan yazıyor, kaymayı başlığın
# Offset alanına koyuyor; .meas zamanları mutlak (examples/18 T3). Elle (RC τ = 1 ms, 5 V darbe 0-1 ms ve 2-3 ms):
# t_fall = 1m + τ·ln(V1/3), t_rise = 2m + τ·ln((5 − V2)/2), V1 = 5(1 − e^(−1)), V2 = V1·e^(−1).
RC_TSTART = RC.replace(".tran 3m", ".tran 0 3m 1m").replace(
    ".end\n", ".meas tran t_fall WHEN V(out)=3 FALL=1\n.meas tran t_rise WHEN V(out)=3 RISE=1\n"
              ".meas tran v_avg AVG V(out)\n.end\n")
# Steplenmiş hali (r = 1k, 2k): her adımın ekseni yine 0'dan, Offset tek. 2,5 V'u iki adım da yükselerek geçer. Elle:
# t = 2m + τ·ln((5 − V2)/2,5), τ = r·1µ; V2 = V(2m) = V(1m)·e^(−1m/τ), V(1m) = 5(1 − e^(−1m/τ)).
RC_TSTART_STEP = (RC.replace("R1 in out 1k", "R1 in out {r}")
                  .replace(".tran 3m", ".step param r list 1k 2k\n.tran 0 3m 1m")
                  .replace(".end\n", ".meas tran t_x WHEN V(out)=2.5 RISE=1\n.end\n"))


def record_tstart(workspace):
    adapter = LTSpiceAdapter(workspace_dir=workspace, solver_threads=1)
    for name, net in (("ref26_rc_tstart", RC_TSTART), ("ref26_rc_tstart_step", RC_TSTART_STEP)):
        res = adapter.run_netlist(net, circuit_name=name, treat_as_text=True)
        if not res.success:
            raise SystemExit(f"Kayıt başarısız: {res.errors}")
        log = LogParser.read_text(res.log_path).replace(str(workspace), r"C:\ws")
        (DATA / f"{name}.log.txt").write_text(log, encoding="utf-8", newline="")
        copy_raw(res.raw_path, DATA / f"{name}.raw", workspace)
        (DATA / f"{name}.net").write_text(net, encoding="utf-8", newline="")
        print(f"LTspice {adapter.ltspice_version()}: {res.measurements or res.stepped_measurements}, raw "
              f"{res.raw_path.stat().st_size} bayt -> tests/data/{name}.*")


def record_ini(workspace):
    """
    LTspice'ın ayar dosyası (``%APPDATA%\\LTspice.ini``): kullanıcı klasörü ``UserCmpDir`` (symbols.user_symbol_dir).
    Yalnızca ``[Options]``'ın ilk satırları alınır; kişisel yollar ``C:\\ws\\user``, UUID ``0`` olur. Kodlama ve satır
    sonu olduğu gibi kalır (26.1.1: BOM'lu UTF-16LE, CRLF).
    """
    import os

    from ltspice_llm.schematic import decode_asc_bytes

    data = (Path(os.environ["APPDATA"]) / "LTspice.ini").read_bytes()
    text, encoding = decode_asc_bytes(data)
    kept = []
    for line in text.split("\r\n"):
        key = line.split("=", 1)[0]
        if key in ("UserCmpDir", "WorkingDirectory"):
            line = f"{key}=C:\\ws\\user"
        elif key == "UUID":
            line = "UUID=0"
        kept.append(line)
        if key == "LibrarySearchPathDisabled":
            break
    bom = data[:2] if data[:2] in (b"\xff\xfe", b"\xfe\xff") else b""
    (DATA / "ref26_LTspice.ini").write_bytes(bom + ("\r\n".join(kept) + "\r\n").encode(encoding))
    print(f"{encoding}{' (BOM)' if bom else ''}, {len(kept)} satır -> tests/data/ref26_LTspice.ini")


# Adı LTspice'ın hata/uyarı sözcüğüyle biten ölçümler (tests/test_real_logs.py). Eski hata: log satırı ölçüm olarak
# tanınmadan önce "error:" / "warning:" alt dizeleri aranıyordu; 'gain_error' koşumu FAILED, 'temp_warning' uyarı oldu.
# Devre ref26_flow_rc'nin aynısı, yalnızca ölçüm eklendi: gain_error ile v_max aynı ifade (MAX V(out)), temp_warning ile
# vr_max aynı ifade (MAX V(in,out)). Offset_Error büyük harfle yazıldı: LTspice adı loga nasıl yazıyor? t_half, t_error
# ve t_warning aynı saf WHEN ölçümü, eşiği bir ifade (v_max/2): sonuç satırı "ad: v(out)=v_max/2 AT t" sayısal
# değer içermez (test-reviewer). Elle: 0-1 ms dolarken V(out) = 5(1 - e^(-t/1ms)), t_half = -1ms·ln(1 - v_max/10).
MEAS_NAMES = RC.replace(
    ".meas tran v_max MAX V(out)\n",
    ".meas tran v_max MAX V(out)\n.meas tran gain_error MAX V(out)\n.meas tran Offset_Error MIN V(out)\n"
    ".meas tran temp_warning MAX V(in,out)\n.meas tran t_half WHEN V(out)=v_max/2\n"
    ".meas tran t_error WHEN V(out)=v_max/2\n.meas tran t_warning WHEN V(out)=v_max/2\n",
)


def record_meas_names(workspace):
    adapter = LTSpiceAdapter(workspace_dir=workspace, solver_threads=1)
    res = adapter.run_netlist(MEAS_NAMES, circuit_name="ref26_meas_names", treat_as_text=True)
    # Kayıt sınanan ayrıştırıcıya güvenmez (success ondan gelir): LTspice logu ve .raw'ı yazdı mı?
    if res.log_path is None or res.raw_path is None:
        raise SystemExit(f"Kayıt başarısız: {res.errors}")
    log = LogParser.read_text(res.log_path).replace(str(workspace), r"C:\ws")
    (DATA / "ref26_meas_names.log.txt").write_text(log, encoding="utf-8", newline="")
    copy_raw(res.raw_path, DATA / "ref26_meas_names.raw", workspace)
    (DATA / "ref26_meas_names.net").write_text(MEAS_NAMES, encoding="utf-8", newline="")
    shown = [ln for ln in log.splitlines() if "error" in ln.lower() or "warning" in ln.lower() or ln.startswith("v")]
    print(f"LTspice {adapter.ltspice_version()}: " + " | ".join(shown) + " -> tests/data/ref26_meas_names.*")
    # Aynı sorunun başka yüzü: logun "Circuit:" satırı ve "Files loaded:" listesi netlistin tam yolunu yazar. Çalışma
    # klasörünün adında bir hata sözcüğü ("Simulation Failed") geçerse eski ayrıştırıcı her koşumu FAILED gösterirdi.
    folder = workspace / "Simulation Failed cases"
    adapter = LTSpiceAdapter(workspace_dir=folder, solver_threads=1)
    res = adapter.run_netlist(RC, circuit_name="ref26_path_keyword", treat_as_text=True)
    if res.log_path is None or res.raw_path is None:
        raise SystemExit(f"Kayıt başarısız: {res.errors}")
    log = LogParser.read_text(res.log_path).replace(str(folder), r"C:\ws\Simulation Failed cases")
    (DATA / "ref26_path_keyword.log.txt").write_text(log, encoding="utf-8", newline="")
    shown = [ln for ln in log.splitlines() if "failed" in ln.lower()]
    print(f"LTspice {adapter.ltspice_version()}: " + " | ".join(shown) + " -> tests/data/ref26_path_keyword.log.txt")


RECORDERS = {"flow": record_flow, "mc": record_monte_carlo, "safety": record_safety, "schematic": record_schematic,
             "crash": record_crash, "mean": record_mean, "stepraw": record_step_raw, "tstart": record_tstart,
             "ini": record_ini, "measnames": record_meas_names}


def main(argv=None):
    names = (sys.argv[1:] if argv is None else argv) or list(RECORDERS)
    unknown = [n for n in names if n not in RECORDERS]
    if unknown:
        raise SystemExit(f"Bilinmeyen kayıt: {unknown}; seçenekler: {list(RECORDERS)}")
    workspace = ROOT / ".scratch" / "flow_fixtures"
    for name in names:
        RECORDERS[name](workspace)


if __name__ == "__main__":
    main()
