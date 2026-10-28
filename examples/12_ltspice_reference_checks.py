"""
12 - ADI LTspice AI Reference: İddiaların ve Kazanılan Davranışların Doğrulaması
================================================================================
LTspice 26 kurulumuyla gelen resmî ADI belgelerindeki (``%LOCALAPPDATA%\\LTspice\\reference``) kuralları ve
bu belgeler sayesinde düzeltilen adaptör davranışlarını gerçek LTspice ile yeniden üretir.
``docs/LESSONS_LEARNED_LTSPICE_REFERENCE.md`` belgesindeki sayılar, ``tests/test_quantizer.py`` içindeki LTspice
sayı yazımı beklentileri ve ``tests/test_netlist_policy.py``'deki ``%VAR%`` kuralı (S3) bu betikten gelir (~25 sn).
Rapor: ``reports/12_ltspice_reference_checks.html``.

Çalıştırma:
    python examples/12_ltspice_reference_checks.py
"""

import math
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from ltspice_llm import (  # noqa: E402
    AscSchematic,
    LTSpiceAdapter,
    ReferenceDocs,
    SPICEDiagnostics,
    find_model_definitions,
    format_spice_value,
    parse_spice_value,
    read_measure_db,
)
from study_helpers import report_path, save_report  # noqa: E402
from ltspice_llm.netlist import file_access_problems  # noqa: E402
from ltspice_llm.parser import LogParser  # noqa: E402
from ltspice_llm.report import (  # noqa: E402
    Column,
    Effect,
    SimulationReport,
    Study,
    Table,
    log_facts,
)

# LTspice'ın okuduğu sayı yazımları (ADI SPICE-SYNTAX-REFERENCE "Engineering Multipliers", "Important Rules").
# Beklenen değer yazılmaz: LTspice'ın okuduğu değer ölçülür ve parse_spice_value ile karşılaştırılır.
NUMBER_FORMS = [".5k", "-.5k", "1.k", "5.", "4k7", "+4k7", "-4k7", "-1.5k3", "6K34", "2Meg2", "1M5", "1u5", "2f2",
                "4R7", "1.5k3", "4V7", "4x7", "10Volts", "100Hz", "1a", "10Fa", "1mil", "1e-3k", "47E-1", "1Meg", "1M"]
# LTspice'ın hata verdiği yazımlar; parse_spice_value da reddetmeli
BAD_FORMS = ["4k7ohm", "k", ".", "1..2", "12 3", "1.5.3"]
TWO_END_TAIL = "* ilk .end'den sonrası\n.end\n"

RC_ASC = (
    "Version 4\r\nSHEET 1 880 680\r\nWIRE 112 96 32 96\r\nWIRE 208 96 192 96\r\nWIRE 224 96 208 96\r\n"
    "WIRE 32 112 32 96\r\nWIRE 224 112 224 96\r\nFLAG 32 192 0\r\nFLAG 224 176 0\r\nFLAG 208 96 out\r\n"
    "SYMBOL voltage 32 96 R0\r\nSYMATTR InstName V1\r\nSYMATTR Value PULSE(0 1 0 1n 1n 1 2)\r\n"
    "SYMBOL cap 208 112 R0\r\nSYMATTR InstName C1\r\nSYMATTR Value 1µ\r\n"
    "SYMBOL res 208 80 R90\r\nWINDOW 0 0 56 VBottom 2\r\nWINDOW 3 32 56 VTop 2\r\nSYMATTR InstName R1\r\n"
    "SYMATTR Value 1k\r\nTEXT 0 240 Left 2 !.tran 0 8m 0 5u\r\nTEXT 0 272 Left 2 !.meas tran tau WHEN V(out)=0.632\r\n"
)

PROBE_VAR = "LTSPICE_LLM_PROBE"
PROBE_VAR_PAREN = "LTSPICE_LLM_PROBE(X)"  # Windows'ta ad parantez içerebilir (%ProgramFiles(x86)%)

results = []


def file_access_probes(outside):
    """
    (etiket, kontrol mü, satırlar): çalışma alanının (``outside/../ws``) dışındaki ``outside`` klasörünün dosyasını
    okutmaya çalışan yol yazımları. Kontroller mutlak yol ve ``..``: LTspice'ın okuduğunu, politikanın zaten
    reddettiğini bildiğimiz yazımlar. Yapının dosyayı okuyabildiğini gösterirler; kontrol okumazsa ölçüm geçersizdir.
    ``.include`` dosyası ``.param probe=4.2``, ``.lib`` dosyası 4.2 V'luk bir alt devre, PWL dosyası sabit 4.2 V.
    """
    lib, sub, pwl = outside / "probe.lib", outside / "probe.sub", outside / "probe_pwl.txt"
    return [
        ("mutlak yol", True, f'.include "{lib}"\nV1 a 0 {{probe}}'),
        ("..", True, ".include ..\\outside\\probe.lib\nV1 a 0 {probe}"),
        (".lib mutlak yol", True, f'.lib "{sub}"\nX1 a PROBE'),
        ("PWL file=mutlak yol", True, f'V1 a 0 PWL file="{pwl}"'),
        ("%VAR%", False, f".include %{PROBE_VAR}%\\probe.lib\nV1 a 0 {{probe}}"),
        ('"%VAR%"', False, f'.include "%{PROBE_VAR}%\\probe.lib"\nV1 a 0 {{probe}}'),
        ("%var% (küçük harf)", False, f".include %{PROBE_VAR.lower()}%\\probe.lib\nV1 a 0 {{probe}}"),
        ("%VAR(X)%", False, f".include %{PROBE_VAR_PAREN}%\\probe.lib\nV1 a 0 {{probe}}"),
        (".lib %VAR%", False, f".lib %{PROBE_VAR}%\\probe.sub\nX1 a PROBE"),
        ("PWL file=%VAR%", False, f"V1 a 0 PWL file=%{PROBE_VAR}%\\probe_pwl.txt"),
        ("$VAR", False, f".include ${PROBE_VAR}\\probe.lib\nV1 a 0 {{probe}}"),
        ("${VAR}", False, f".include ${{{PROBE_VAR}}}\\probe.lib\nV1 a 0 {{probe}}"),
    ]


def check(title, ok, detail):
    results.append((title, ok, detail))
    print(f"[{'OK ' if ok else 'HATA'}] {title}\n       {detail}")


def threads_reach_ltspice(early, capped):
    """
    İki ``.end``'li netlistte sınır LTspice'a ulaşıyor mu: ilk ``.end``'den önceki ``threads=2`` uygulanıyor ve LTspice
    adaptörün kiraladığı sayıyı kullanıyor. Koruma 2'yi sürecin kullanabildiği çekirdeğe kırpar (tek çekirdekte 1),
    bu yüzden beklenen sabit 2 değil kiralanan sayı. Kira alınamadıysa (bütçe dolu) koşum yoktur: geçmez.
    """
    t_cap = log_facts(capped)["threads"]
    return (capped.success and t_cap is not None and t_cap == capped.solver_threads
            and log_facts(early)["threads"] == 2)


def main():
    work = Path(tempfile.mkdtemp(prefix="ltspice_ref_checks_"))
    ad = LTSpiceAdapter(workspace_dir=work / "ws", default_timeout=60)
    print(f"LTspice {ad.ltspice_version()}  ({ad.executable_path})")
    docs = ReferenceDocs()
    print(f"ADI referansı: {docs.directory} ({len(docs.documents())} belge, {len(docs.sections())} bölüm)\n")

    def run(net, name):
        return ad.run_netlist(net, circuit_name=name, treat_as_text=True)

    # 1. Çarpan tuzakları (SPICE-SYNTAX-REFERENCE: Engineering Multipliers)
    i_1m = run("* m\nV1 a 0 1\nR1 a 0 1M\n.tran 1u\n.meas tran ir AVG I(R1)\n.end\n", "m1").measurements["ir"]
    i_1meg = run("* m\nV1 a 0 1\nR1 a 0 1Meg\n.tran 1u\n.meas tran ir AVG I(R1)\n.end\n", "m2").measurements["ir"]
    check("1M = 1 mili, 1Meg = 1 mega", abs(i_1m - 1000) < 1e-6 and abs(i_1meg - 1e-6) < 1e-9,
          f"1 V / 1M -> {i_1m:g} A, 1 V / 1Meg -> {i_1meg:g} A")
    ic = run("* f\nV1 a 0 AC 1\nC1 a 0 4Farads\n.ac list 1k\n.options meascplxfmt=polar\n"
             ".meas ac ic FIND mag(I(C1)) AT 1k\n.end\n", "m3").measurements["ic"]
    check("'4Farads' = 4 fF (F femto okunur, 'arads' yok sayılır)", abs(ic / (2 * 3.141592653589793e3 * 4e-15) - 1) < 1e-6,
          f"|I(C1)| @1 kHz = {ic:g} A  (2*pi*f*4f = 2.513e-11)")

    # 2. .asc kodlaması (SCHEMATIC-REFERENCE: File Encoding — Critical)
    for enc in ("cp1252", "utf-16-le"):
        src = work / f"rc_{enc}.asc"
        src.write_bytes(RC_ASC.encode(enc))
        tau = ad.run_netlist(src, circuit_name=f"rc_{enc.replace('-', '')}").measurements.get("tau")
        check(f".asc {enc} içindeki '1µ' doğru okunuyor", tau is not None and abs(tau / 1e-3 - 1) < 1e-3,
              f"tau = {tau} s (beklenen R*C = 1 ms; eskiden µ silinip C = 1 F oluyordu)")
    utf8 = work / "rc_utf8.asc"
    utf8.write_bytes(RC_ASC.encode("utf-8"))
    # Adaptör .asc'yi cp1252 okuyup aynı baytlarla geri yazar: LTspice UTF-8 baytlarını görür. Eskiden LTspice burada
    # adaptörsüz başlatılıyordu; threads sınırı ve takılma bekçisi yoktu, tek çekirdekte 60 sn'de bitmedi (2026-10-11).
    u = ad.run_netlist(utf8, circuit_name="rc_utf8")
    log = LogParser.read_text(u.log_path) if u.log_path else ""
    check("UTF-8 .asc'yi LTspice yanlış okur (referans doğru)", "FAIL" in log,
          f"adaptörle, baytlar değişmeden: 'tau' ölçümü {'FAIL' if 'FAIL' in log else 'üretildi'} ('1Âµ' = 1 F)")

    # 3. Zaman adımı / yakınsama: ölümcül olmayan bildirimler ve gerçek hatalar
    z = run("* z\nV1 in 0 PULSE(0 1 0 0 0 50n 100n)\nR1 in out 1k\nC1 out 0 1p\n.tran 1u\n"
            ".meas tran vavg AVG V(in)\n.end\n", "zero_rise")
    e = run("* z\nV1 in 0 PULSE(0 1 0 1n 1n 50n 100n)\nR1 in out 1k\nC1 out 0 1p\n.tran 1u\n"
            ".meas tran vavg AVG V(in)\n.end\n", "explicit_rise")
    limit = next((w.split(": ", 1)[-1].split(" |")[0] for w in z.warnings if "Limiting rise" in w), "?")
    check("Trise=0 koşumu başarılı + uyarı (eskiden FAILED)", z.success and not z.errors,
          f"LTspice: '{limit}' (Ton/10); AVG V(in) = {z.measurements['vavg']:.3f} (Trise=1n ile {e.measurements['vavg']:.3f})")
    d = run("* x\nB1 a 0 V=if(V(a)>0.5,0,1)\nR1 a 0 1k\n.op\n.end\n", "dcop_fail")
    check("DC çalışma noktası bulunamayınca FAILED (eskiden success=True)", not d.success,
          f"teşhis: {(d.diagnostics or {}).get('issue_type')}")
    two = run("* two\nV1 in 0 SINE(0 1 1k) AC 1\nR1 in out 1k\nC1 out 0 159n\n.tran 2m\n.ac dec 10 1 1Meg\n.end\n", "two")
    check("İki analiz komutu açıkça teşhis ediliyor", (two.diagnostics or {}).get("issue_type") == "MULTIPLE_ANALYSES",
          f"hata: {two.errors[:1]}")
    try:
        ad.run_sweep("* s\nV1 a 0 1\nR1 a b 1k\nR2 b 0 1k\n.op\n.end\n", "R1", ["1k"], circuit_name="sweep1")
        rejected = False
    except ValueError:
        rejected = True
    check("Tek değerli run_sweep LTspice'a gitmeden reddediliyor", rejected,
          "LTspice: 'This results in only one step.' (koşum hiç başlamaz, .raw yazılmaz)")
    conv = run("* c\nB1 0 n I=if(V(n)>0.5,-1m,1m)\nC1 n 0 1p\nR1 n 0 1Meg\n.tran 0 10u 0 1n convreport\n.end\n", "conv")
    check("convreport puanları teşhise giriyor", "B1" in ((conv.diagnostics or {}).get("components_involved") or []),
          f"puanlar: {conv.convergence.get('device_scores')} / {conv.convergence.get('node_scores')}")

    # 4. AC ölçümleri (MEAS-REFERENCE) ve .db (MEASURE-DATABASE-REFERENCE)
    ac = run("* ac\nV1 in 0 AC 1\nR1 in out 1k\nC1 out 0 159n\n.ac dec 100 1 1Meg\n.options meascplxfmt=polar\n"
             ".meas AC g0 FIND mag(V(out)) AT 1\n.meas AC fc WHEN mag(V(out))=g0/sqrt(2)\n"
             ".meas AC p1k FIND ph(V(out)) AT 1k\n.end\n", "ac")
    db = {r["name"]: r["value"] for r in read_measure_db(ac.db_path)}
    # Log 12 anlamlı basamak yazar (.options measdgt), .db tam double tutar: göreli 1e-9 aynı değerdir
    check("WHEN ...=g0/sqrt(2) ve polar faz .db ile birebir",
          math.isclose(ac.measurements["fc"], db["fc"], rel_tol=1e-9)
          and math.isclose(ac.measurements["p1k"], db["p1k"], rel_tol=1e-9),
          f"fc = {ac.measurements['fc']:.3f} Hz (eskiden 1.0), ph @1k = {ac.measurements['p1k']:.3f}° (eskiden +44.97)")

    # 5. Model kütüphanesi (DEVICE-MODELS-GUIDE)
    net = "* d\nV1 a 0 1\nR1 a b 1k\nD1 b 0 1N4148\n.op\n.meas op vb FIND V(b)\n.end\n"
    miss = run(net, "d_missing")
    fixed = run(SPICEDiagnostics.apply_diagnostic_patch(net, SPICEDiagnostics.diagnose("", errors_list=miss.errors)), "d_fixed")
    ideal = run(net.replace(".op", ".model 1N4148 D\n.op"), "d_ideal")
    check("Eksik 1N4148 -> '.lib standard.dio' önerisi ve yaması", fixed.success,
          f"gerçek model V(b) = {fixed.measurements.get('vb', float('nan')):.3f} V; '.model 1N4148 D' (varsayılan) "
          f"V(b) = {ideal.measurements.get('vb', float('nan')):.3f} V")
    lt = find_model_definitions("LT1001")
    check("LT1001 kütüphanede bulunuyor", bool(lt), f"{lt[:1]}")

    # 6. Şema bağlantısı (-netlist) ve .four
    s = AscSchematic()
    i1 = s.add_symbol("current", 400, 96, "R0", "I1", "1m")
    r1 = s.add_symbol("res", 512, 80, "R0", "R1", "1k")
    s.connect(s.get_pin(i1, "+"), s.get_pin(r1, "A"))
    s.add_ground(*s.get_pin(i1, "-"))
    s.add_ground(*s.get_pin(r1, "B"))
    s.add_directive(0, 300, ".op")
    report = ad.inspect_schematic(s.render(), treat_as_text=True)
    i_line = next(ln for ln in report["netlist"].splitlines() if ln.startswith("I1"))
    check("Akım kaynağı sembolü bağlı (pin tablosu düzeltmesi)", report["status"] == "SUCCESS",
          f"netlist: '{i_line}' (eski tabloyla 'I1 NC_01 NC_02 1m')")
    four = run("* four\nV1 in 0 SINE(0 1 1k)\nB1 out 0 V=tanh(2*V(in))\nR1 out 0 1k\n.tran 0 5m 0 1u\n"
               ".four 1k 5 -1 V(out)\n.end\n", "four")
    thd = four.fourier[0]["thd_percent"] if four.fourier else None
    check(".four THD ayrıştırılıyor", thd is not None, f"tanh(2·sin) THD = {thd}%")

    # 7. Sayı yazımı: LTspice'ın okuduğu değer (R = 1 V / I(R1)) ile parse_spice_value aynı mı?
    numbers = []
    for i, form in enumerate(NUMBER_FORMS):
        res = run(f"* v\nV1 a 0 1\nR1 a 0 {form}\n.op\n.meas op ir FIND I(R1)\n.end\n", f"num{i}")
        lt = 1.0 / res.measurements["ir"]
        try:
            py = parse_spice_value(form)
        except ValueError:
            py = None
        numbers.append((form, res, lt, py))
    same = [n for n in numbers if n[3] is not None and math.isclose(n[2], n[3], rel_tol=1e-6)]
    differ = [f"{f}: LTspice {format_spice_value(lt, 4)}, parse {py}" for f, _, lt, py in numbers
              if (f, _, lt, py) not in same]
    check("parse_spice_value LTspice'ın okuduğu değeri veriyor", not differ,
          f"{len(same)}/{len(numbers)} yazım aynı" + (f"; farklı: {'; '.join(differ)}" if differ else ""))
    bad_rows = []
    for i, form in enumerate(BAD_FORMS):
        bad = run(f"* v\nV1 a 0 1\nR1 a 0 {form}\n.op\n.end\n", f"num_bad{i}")
        try:
            parse_spice_value(form)
            rejected = False
        except ValueError:
            rejected = True
        message = bad.errors[0].split("): ", 1)[-1].split(" | ")[0] if bad.errors else "hata yok"  # yol rapora girmesin
        bad_rows.append((form, not bad.success, rejected, message))
    check("LTspice'ın reddettiği yazımları parse_spice_value da reddediyor",
          all(lt_err and py_rej for _, lt_err, py_rej, _ in bad_rows),
          "; ".join(f"'{f}': LTspice {m}, parse {'reddetti' if r else 'KABUL ETTİ'}" for f, _, r, m in bad_rows))
    rc47 = work / "rc47.asc"
    rc47.write_bytes(RC_ASC.replace("SYMATTR Value 1µ", "SYMATTR Value .47µ").encode("cp1252"))
    tau47 = ad.run_netlist(rc47, circuit_name="rc47").measurements.get("tau")
    check("'.47µ' (.asc, cp1252) = 0.47 µF ve parse_spice_value aynı", tau47 is not None
          and math.isclose(tau47, 1e3 * parse_spice_value(".47µ"), rel_tol=1e-3),
          f"tau = {tau47} s (R = 1k; parse: {parse_spice_value('.47µ'):g} F)")

    # 8. .end (SIMULATION-COMMANDS-REFERENCE ".END": "All lines after .end are ignored") ve iş parçacığı sınırı
    two = run("* e\nV1 a 0 1\nR1 a 0 1k\n.op\n.meas op i1 FIND I(R1)\n.end\n.meas op i2 FIND I(R1)\n.end\n", "two_end")
    check("İlk .end'den sonraki .meas okunmuyor", "i1" in two.measurements and "i2" not in two.measurements,
          f"ölçümler: {sorted(two.measurements)}")
    # SPICE-SYNTAX-REFERENCE "Netlist Structure": ilk satır her zaman başlık. Başlığı ".end" olan netlist: adaptör
    # satırlarını başlığın önüne yazarsa sınır satırı başlık olur ve LTspice hiçbir şey koşmaz.
    titled = run(".end\nV1 a 0 1\nR1 a 0 1k\n.op\n.meas op i FIND I(R1)\n.end\n", "title_end")
    untitled = run("V1 a 0 1\nR1 a 0 1k\nR2 a 0 1k\n.op\n.meas op i FIND I(R2)\n.end\n", "no_title")
    check("İlk satır başlık: '.end' başlığı netlisti bitirmiyor, başlıksız netlistte ilk eleman kayboluyor",
          titled.success and titled.measurements.get("i") is not None and untitled.measurements.get("i") == 0,
          f"başlık '.end': I(R1) = {titled.measurements.get('i')} A; başlıksız (V1 ilk satırda): "
          f"I(R2) = {untitled.measurements.get('i')} A")
    # Tek değişken: ".options threads=2" satırının ilk .end'e göre konumu. Koruma bu iki koşumda kapalı, çünkü
    # açıkken adaptör kendi sınırını ilk .end'in önüne ekler (düzeltilen davranış) ve fark görünmez. Koşumlar sıralı.
    raw_ad = LTSpiceAdapter(workspace_dir=work / "ws_noguard", default_timeout=60, cpu_guard=False)
    base = "* t\nV1 a 0 1\nR1 a 0 1k\n.tran 1m\n"
    late = raw_ad.run_netlist(base + ".end\n.options threads=2\n.end\n", circuit_name="threads_after_end",
                              treat_as_text=True)
    early = raw_ad.run_netlist(base + ".options threads=2\n.end\n" + TWO_END_TAIL, circuit_name="threads_before_end",
                               treat_as_text=True)
    capped = LTSpiceAdapter(workspace_dir=work / "ws_cap", default_timeout=60, solver_threads=2).run_netlist(
        base + ".end\n" + TWO_END_TAIL, circuit_name="adapter_two_end", treat_as_text=True)
    t_late, t_early, t_cap = (log_facts(r)["threads"] for r in (late, early, capped))
    check("İki .end'li netlistte adaptörün sınırı LTspice'a ulaşıyor", threads_reach_ltspice(early, capped),
          f"Maximum thread count: .end sonrası {t_late}, .end öncesi {t_early}, "
          f"adaptör (solver_threads=2, kiralanan {capped.solver_threads}) {t_cap}")
    # Netlistin kendi threads değeri mantıksal çekirdek sayısından büyükse LTspice ne kullanır? Adaptör bu değere
    # koruma açıkken de dokunmaz (koşum bütçeye girer); SimulationResult.solver_threads LTspice'ın kullandığını
    # bildirmeli (test-reviewer, 2026-10-11).
    cpus = os.cpu_count() or 1
    above = ad.run_netlist(base + f".options threads={cpus + 2}\n.end\n", circuit_name="threads_above_cpus",
                           treat_as_text=True)
    t_above = log_facts(above)["threads"]
    check("LTspice threads'i mantıksal çekirdek sayısına kırpar",
          above.success and t_above == cpus and above.solver_threads == t_above,
          f".options threads={cpus + 2} -> Maximum thread count: {t_above} (os.cpu_count() = {cpus}); "
          f"adaptörün bildirdiği {above.solver_threads}")

    # 9. Dosya erişimi (netlist.file_access_problems, tests/test_netlist_policy.py): LTspice .include ve PWL file=
    # yolundaki ortam değişkenini açıyor mu? ADI SCHEMATIC-REFERENCE "Symbol and Library Search Paths": "Environment
    # variables supported in all paths" yalnızca Settings > Search Paths için yazılı, netlist için belge yok. Dosya
    # çalışma alanının dışında; LTspice'ın okuyabildiği her yazımı politika reddetmeli.
    outside = work / "outside"
    outside.mkdir()
    (outside / "probe.lib").write_text(".param probe=4.2\n", encoding="utf-8")
    (outside / "probe.sub").write_text(".subckt PROBE a\nVP a 0 4.2\n.ends PROBE\n", encoding="utf-8")
    (outside / "probe_pwl.txt").write_text("0 4.2\n1 4.2\n", encoding="utf-8")
    os.environ[PROBE_VAR] = os.environ[PROBE_VAR_PAREN] = str(outside)
    access = []
    try:
        for i, (label, control, lines) in enumerate(file_access_probes(outside)):
            net = f"* dosya erişimi\n{lines}\nR1 a 0 1k\n.tran 1m\n.meas tran va FIND V(a) AT 0.5m\n.end\n"
            va = run(net, f"file_access{i}").measurements.get("va")
            # .tran dalga formu tek duyarlıkta: 4.2 -> 4.19999980927 (ölçüldü; .op'ta tam 4.2)
            reads = va is not None and math.isclose(va, 4.2, rel_tol=1e-6)
            access.append((label, control, reads, bool(file_access_problems(net))))
    finally:
        os.environ.pop(PROBE_VAR, None)
        os.environ.pop(PROBE_VAR_PAREN, None)
    check("LTspice'ın çalışma alanı dışından okuduğu her yazımı file_access_problems reddediyor",
          all(reads for _, control, reads, _ in access if control)
          and all(rejected for _, _, reads, rejected in access if reads),
          "; ".join(f"{label}{' (kontrol)' if control else ''}: LTspice {'okudu' if reads else 'okumadı'}, "
                    f"politika {'reddetti' if rej else 'geçirdi'}" for label, control, reads, rej in access))

    report = build_report(ad, docs, numbers, same, (late, early, capped, above), two, access)
    save_report(report, report_path("12_ltspice_reference_checks.html"))

    shutil.rmtree(work, ignore_errors=True)
    passed = sum(ok for _, ok, _ in results)
    print(f"{passed}/{len(results)} kontrol geçti.")
    return 0 if passed == len(results) else 1


def build_report(ad, docs, numbers, same, thread_runs, two, access):
    late, early, capped, above = thread_runs
    t_late, t_early, t_cap, t_above = (log_facts(r)["threads"] for r in thread_runs)
    passed = sum(ok for _, ok, _ in results)
    rep = SimulationReport(
        "LTspice referans kontrolleri", "ADI LTspice referansı: iddialar ve kazanılan davranışlar",
        subtitle="Resmî ADI belgelerindeki kuralları ve bu belgeler sayesinde düzeltilen adaptör davranışlarını "
                 "gerçek LTspice ile yeniden üretir.",
        meta=[("LTspice", ad.ltspice_version() or "?"), ("ADI referansı", f"{len(docs.documents())} belge"),
              ("Betik", "examples/12_ltspice_reference_checks.py")],
        disclaimer="Bu rapor LTspice'ın sözdizimi ve çalışma davranışını ölçer; bir devre tasarımı ya da donanım "
                   "ölçümü değildir.")
    rep.add_tile("Kontrol", f"{passed} / {len(results)}", "geçen / toplam")
    rep.add_tile("Sayı yazımı", f"{len(same)} / {len(numbers)}", "parse_spice_value = LTspice")
    rep.add_tile("İş parçacığı", f"{t_late} → {t_cap}", "iki .end'li netlist, eski → yeni ekleme yeri")
    rep.add_decision("good" if len(same) == len(numbers) else "serious", "Sayı yazımı",
                     "`.5k`, `4k7`, `1.5k3`, `4R7` gibi yazımlar LTspice'ta geçerli; `parse_spice_value` "
                     "aynı değeri vermeli (E serisi kuantalaması ve `run_sweep` tekrar ayıklaması buna dayanır).")
    rep.add_decision("good" if threads_reach_ltspice(early, capped) else "critical", ".end",
                     "Adaptörün eklediği satırlar ilk `.end`'in önüne yazılır; sonrası LTspice'a ulaşmaz.")
    leaks = [label for label, _, reads, rejected in access if reads and not rejected]
    rep.add_decision("good" if not leaks else "critical", "Dosya erişimi",
                     "LTspice'ın çalışma alanı dışından okuduğu her yol yazımını `file_access_problems` reddetmeli"
                     + (f"; reddedilmeyen: {', '.join(f'`{x}`' for x in leaks)}." if leaks else "."))

    rows = [(form, {"ratio": (py / lt) if py is not None else None}) for form, _, lt, py in numbers]
    rep.add_study(Study(
        "S1", "Sayı yazımı",
        question="parse_spice_value LTspice'ın okuduğu her sayı yazımını aynı değere çeviriyor mu?",
        method="Her yazım için ayrı devre: 1 V kaynak ve R1 = <yazım>; `.meas op ir FIND I(R1)`, R = 1 V / I. "
               "Aynı metin parse_spice_value'ya verilir. Beklenen değer elle yazılmaz, LTspice'tan ölçülür.",
        table=Table([Column("ratio", "parse ÷ LTspice", decimals=6)], rows,
                    caption="1.000000: aynı değer; boş: parse_spice_value reddetti."),
        notes=["Ölçülen değerler: " + ", ".join(f"`{f}` = {format_spice_value(lt, 4)}" for f, _, lt, _ in numbers)
               + ". Bilinmeyen harften sonraki rakam yok sayılır (`4V7` = 4). Gömülü kesir işaretten bağımsız "
               "eklenir: `-4k7` = (−4 + 0.7)k. LTspice'ın hata verdiği yazımlar: "
               + ", ".join(f"`{f}`" for f in BAD_FORMS) + "."]))
    rep.add_runs("S1", [(form, res) for form, res, _, _ in numbers])

    rep.add_study(Study(
        "S2", ".end ve iş parçacığı sınırı",
        question="LTspice ilk .end'den sonrasını okuyor mu; adaptörün eklediği `.options threads` LTspice'a ulaşıyor mu?",
        method="Aynı RC devresi; tek değişken `.options threads=2` satırının ilk `.end`'e göre konumu (koruma kapalı, "
               "adaptör netliste dokunmaz). Üçüncü koşum: koruma açık, solver_threads=2, iki `.end`'li netlist; "
               "beklenen, korumanın kiraladığı sayı (sürecin çekirdeğine kırpılır). Dördüncü koşum: netlistin kendi "
               "`threads` değeri mantıksal çekirdek sayısının 2 fazlası (koruma açık, adaptör satıra dokunmaz). "
               "Ölçüm: logdaki `Maximum thread count`.",
        table=Table([Column("threads", "Maximum thread count", decimals=0)],
                    [("threads=2 ilk .end'den sonra (eski ekleme yeri)", {"threads": t_late}),
                     ("threads=2 ilk .end'den önce", {"threads": t_early}),
                     ("adaptör, solver_threads=2, iki .end", {"threads": t_cap}),
                     (f"threads={(os.cpu_count() or 1) + 2} (mantıksal çekirdek + 2)", {"threads": t_above})],
                    highlight="adaptör, solver_threads=2, iki .end"),
        effects=[Effect(".options threads=2", "iki .end'li netlist: ilk .end'in sonrası → öncesi",
                        "Maximum thread count", t_late, t_early, unit="", decimals=0, better="lower",
                        note="Sonrasına yazılan sınır okunmaz; çözücü bütün çekirdekleri açar (livelock riski).")],
        notes=[f"Aynı netlistte ikinci `.end`'den önceki `.meas i2` üretilmedi: ölçümler {sorted(two.measurements)}."]))
    rep.add_runs("S2", [("threads .end sonrası", late), ("threads .end öncesi", early), ("adaptör", capped),
                        ("threads çekirdek + 2", above)])

    rep.add_study(Study(
        "S3", "Dosya erişimi politikası",
        question="LTspice çalışma alanının dışındaki bir dosyayı hangi yol yazımlarıyla okuyor; `file_access_problems` "
                 "bunların hepsini reddediyor mu?",
        method="Çalışma alanının dışındaki bir klasöre üç dosya konur: `.param probe=4.2` (`.include` için), 4.2 V'luk "
               "bir alt devre (`.lib` için) ve sabit 4.2 V'luk PWL verisi. Klasör iki ortam değişkenine yazılır, "
               f"`{PROBE_VAR}` ve parantezli `{PROBE_VAR_PAREN}`. Tek değişken yolun yazımı. LTspice dosyayı okuduysa "
               "`.meas tran va FIND V(a) AT 0.5m` 4.2 V verir. Aynı netlist `file_access_problems`'e verilir. Kontrol "
               "satırları (mutlak yol, `..`) yapının dosyayı okuyabildiğini gösterir.",
        table=Table([Column("reads", "LTspice okudu (1 = evet)", decimals=0),
                     Column("rejected", "politika reddetti (1 = evet)", decimals=0)],
                    [(f"{label} (kontrol)" if control else label, {"reads": int(reads), "rejected": int(rejected)})
                     for label, control, reads, rejected in access]),
        notes=["Okunan ama reddedilmeyen yazım, dil modelinin yazdığı bir netlistin çalışma alanı dışındaki bir dosyayı "
               "okutabileceği anlamına gelir. LTspice'ın okumadığı yazımlar bilerek başarısız koşumlardır; "
               "koşum listesine konmadı."]))

    for title, ok, detail in results:
        rep.add_check(title, "geçti" if ok else "HATA", "good" if ok else "critical", detail)
    rep.assumptions = [("Ölçüm düzeni", f"Tek LTspice ({ad.ltspice_version()}); koşumlar sıralı. Değerler bu "
                                         "sürümün davranışıdır; LTspice güncellemesinden sonra betiği yeniden koş.")]
    rep.open_items = ["LTspice güncellemesinden sonra bu betiği yeniden çalıştır (SessionStart hook'u hatırlatır)."]
    rep.footer = "Yeniden üretmek için: `python examples/12_ltspice_reference_checks.py`."
    return rep


if __name__ == "__main__":
    sys.exit(main())
