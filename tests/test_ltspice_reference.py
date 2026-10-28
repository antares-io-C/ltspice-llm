"""
ADI LTspice AI Reference'tan kazanılan davranışların testleri (LTspice gerektirmez).

tests/data/ref26_*.log.txt ve .db dosyaları gerçek LTspice 26.1.1 koşumlarıdır (yollar C:\\ws olarak
temizlendi). Beklenen değerler LTspice'ın kendi .db kayıtlarından alınmıştır. Ayrıntı:
docs/LESSONS_LEARNED_LTSPICE_REFERENCE.md.
"""

import re
import sys
from pathlib import Path

import pytest

from ltspice_llm import diagnostics as diag_mod
from ltspice_llm.adapter import LTSpiceAdapter, SimulationResult
from ltspice_llm.diagnostics import SPICEDiagnostics, SPICEIssueType
from ltspice_llm.parser import LogParser, parse_convergence_report, read_measure_db
from ltspice_llm.reference import ReferenceDocs, find_model_definitions
from ltspice_llm.schematic import (
    SYMBOL_PIN_DEFS,
    AscSchematic,
    decode_asc_bytes,
    encode_asc_text,
    transform_pin,
)
from ltspice_llm.tools import handle_tool_call

DATA = Path(__file__).parent / "data"


def _log(name: str):
    return LogParser.parse_text((DATA / f"ref26_{name}.log.txt").read_text(encoding="utf-8"))


def _diag(name: str):
    parsed = _log(name)
    text = (DATA / f"ref26_{name}.log.txt").read_text(encoding="utf-8")
    return SPICEDiagnostics.diagnose(text, errors_list=parsed.errors)


@pytest.fixture
def no_library(monkeypatch):
    """Kütüphane araması makineye bağlı olmasın."""
    monkeypatch.setattr(diag_mod, "_lookup_library", lambda name: [])


# --------------------------------------------------------------------------- başarısız koşum = hata
def test_dc_operating_point_failure_is_an_error_not_success():
    """'Simulation Failed: Iteration limit reached' eskiden hata sayılmıyordu: koşum success=True görünüyordu."""
    parsed = _log("dcop_fail")
    assert any("Simulation Failed" in e for e in parsed.errors)
    assert _diag("dcop_fail").issue_type == SPICEIssueType.DC_OP_CONVERGENCE


def test_time_step_too_small_with_space_is_an_error():
    parsed = _log("convreport_fail")
    assert any("Time step too small" in e for e in parsed.errors)
    assert any("Simulation Failed" in e for e in parsed.errors)


def test_more_than_one_analysis_is_an_error_and_diagnosed():
    assert _log("two_analyses").errors == ["More than one analysis specified."]
    report = _diag("two_analyses")
    assert report.issue_type == SPICEIssueType.MULTIPLE_ANALYSES
    assert report.suggested_options == []


# --------------------------------------------------------------------------- ölümcül olmayan bildirimler
def test_zero_rise_time_limit_is_a_warning_not_an_error():
    """PULSE(0 1 0 0 0 ...) LTspice'ta rc=0 ile biter; eskiden koşum FAILED/UNKNOWN_ERROR görünüyordu."""
    parsed = _log("zero_rise")
    assert parsed.errors == []
    assert parsed.measurements["vmax"] == pytest.approx(0.99997150898)
    assert any("Limiting rise time to 1e-07" in w for w in parsed.warnings)
    assert sum("Ton/10" in w for w in parsed.warnings) == 1  # açıklama bir kez


def test_removed_duplicate_step_values_is_a_warning_with_source_line():
    parsed = _log("step_partial_dup")
    assert parsed.errors == []
    assert any("Removed duplicate values. | satır: .step param Rx list 1k 1k 2k" in w for w in parsed.warnings)


def test_compiler_errors_carry_the_offending_source_line():
    (err,) = _log("step_single").errors
    assert err.endswith("This results in only one step. | satır: .step param Rx list 1k")


# --------------------------------------------------------------------------- AC ölçüm biçimleri
_AC_EXPECTED = {
    # ad: (bode, polar, cartesian) -- gerçek değerler LTspice .db'sinden
    "m1k": (-3.00607194349, 0.707451061927, 0.707451061927),
    "p1k": (-44.9720966632, -44.9720966632, -44.9720966632),
    "r1k": (0.500487005022, 0.500487005022, 0.500487005022),
    "i1k": (-0.499999762826, -0.499999762826, -0.499999762826),
    "vmax": (-4.33449074446e-06, 0.999999500973, 0.999999500973),
    "fc": (1000.97471156, 1000.97471156, 1000.97471156),
    "fc_rel": (1000.97571081, 1000.97571081, 1000.97571081),
    "ratio": (1.41490212385, 1.41490212385, 1.41490212385),
}


@pytest.mark.parametrize("fmt_index,fmt", [(0, "bode"), (1, "polar"), (2, "cartesian")])
@pytest.mark.parametrize("name", sorted(_AC_EXPECTED))
def test_ac_measurements_decode_in_every_meascplxfmt(fmt_index, fmt, name):
    """Eskiden polar'da ph/im işareti kayboluyor, WHEN ... =1/sqrt(2) sonucu 1.0 okunuyordu."""
    meas = _log(f"ac_{fmt}").measurements
    assert meas[name] == pytest.approx(_AC_EXPECTED[name][fmt_index], rel=1e-6, abs=1e-12)


def test_bare_complex_result_keeps_phase_or_imaginary_part():
    assert _log("ac_bode").measurements["v1k_deg"] == pytest.approx(-44.9720966632)
    assert _log("ac_polar").measurements["v1k"] == pytest.approx(0.707451061927)
    assert _log("ac_polar").measurements["v1k_deg"] == pytest.approx(-44.9720966632)
    cart = _log("ac_cartesian").measurements
    assert (cart["v1k"], cart["v1k_im"]) == (pytest.approx(0.500487005022), pytest.approx(-0.499999762826))


def test_when_with_named_threshold_is_not_dropped():
    """'f_peak: mag(V(out))=vmax AT 1': eşik bir ad; eski desen bu ölçümü hiç üretmiyordu."""
    meas = _log("ac_bode").measurements
    assert meas["f_peak"] == pytest.approx(1.0)
    assert "fc_val" not in meas  # eşik '1/sqrt(2)' sayısal değil: uydurma _val yazılmaz


def test_when_with_numeric_threshold_keeps_val_key():
    meas = LogParser.parse_text("t: V(out)=2.5 AT 0.00069\n").measurements
    assert meas == {"t": 0.00069, "t_val": 2.5}


def test_log_parser_agrees_with_ltspice_measure_database():
    meas = _log("ac_polar").measurements
    for rec in read_measure_db(DATA / "ref26_ac_polar.db"):
        if rec["name"] == "v1k":  # gerçek karmaşık: log genlik/faz, .db Re/Im verir
            assert abs(complex(rec["re"], rec["im"])) == pytest.approx(meas["v1k"], rel=1e-9)
            continue
        assert rec["success"]
        assert rec["value"] == pytest.approx(meas[rec["name"]], rel=1e-9), rec["name"]


def test_read_measure_db_stepped_records_and_failures():
    records = read_measure_db(DATA / "ref26_step_tran_db.db")
    tau = [r for r in records if r["name"] == "tau"]
    assert [r["step_values"] for r in tau] == [{"r": 1000.0}, {"r": 2000.0}, {"r": 3000.0}]
    assert [r["type"] for r in tau] == ["when"] * 3
    assert tau[1]["value"] == pytest.approx(0.0019993433288944948)
    never = [r for r in records if r["name"] == "never"]
    assert len(never) == 3 and not any(r["success"] for r in never)
    vmax = next(r for r in records if r["name"] == "vmax")
    assert vmax["type"] == "range" and vmax["from"] == 0.0


# --------------------------------------------------------------------------- .four ve convreport
def test_fourier_block_is_parsed_with_thd():
    (block,) = _log("four_thd").fourier
    assert block["signal"] == "V(out)"
    assert block["fundamental_hz"] == 1000.0
    assert block["thd_percent"] == pytest.approx(17.342839)
    assert block["partial_thd_percent"] == pytest.approx(17.317829)
    assert block["n_periods"] == 5.0
    assert [h["n"] for h in block["harmonics"]] == [1, 2, 3, 4, 5]
    assert block["harmonics"][2]["normalized"] == pytest.approx(0.1688)
    assert _log("four_thd").measurements == {}  # tablo satırları sahte ölçüm üretmez


def test_convergence_report_scores_are_parsed_and_not_measurements():
    parsed = _log("convreport_fail")
    assert parsed.convergence["device_scores"] == {"B1": pytest.approx(98.601642)}
    assert parsed.convergence["node_scores"] == {"n": pytest.approx(83.438666)}
    assert parsed.convergence["trouble_nodes"] == ["n"]
    assert parsed.convergence["failure_time"] == 2e-13
    assert "B1" not in parsed.measurements and "n" not in parsed.measurements


def test_trouble_with_instance_extracts_the_instance_name():
    """Eskiden 'trouble with instance "B1"' satırından eleman adı olarak 'instance' çıkıyordu."""
    conv = parse_convergence_report(
        'Convergence Failure:  Time step too small; time = 1.0005e-06, timestep = 1.25e-19: trouble with instance "A2"'
    )
    assert conv["trouble_instances"] == ["A2"]
    assert conv["failure_time"] == 1.0005e-06


# --------------------------------------------------------------------------- teşhis
def test_timestep_diagnosis_uses_adi_order_and_convreport_scores():
    report = _diag("convreport_fail")
    assert report.issue_type == SPICEIssueType.TIMESTEP_TOO_SMALL
    assert report.suggested_options[:3] == [".options method=gear", ".options itl4=50", ".options cshunt=1e-15"]
    assert "B1" in report.components_involved and "n" in report.nodes_involved
    assert "Convergence" not in report.nodes_involved and "instance" not in report.components_involved
    assert report.convergence["device_scores"] == {"B1": pytest.approx(98.601642)}
    assert any("convreport" in r for r in report.actionable_recommendations)
    assert any("kararsız" in r for r in report.actionable_recommendations)  # Gear uyarısı


def test_timestep_patch_applies_only_the_conservative_options():
    report = _diag("convreport_fail")
    patched = SPICEDiagnostics.apply_diagnostic_patch("* t\nR1 a 0 1k\n.tran 1m\n.end\n", report)
    assert ".options method=gear" in patched and ".options itl4=50" in patched
    assert "reltol" not in patched and "abstol" not in patched  # doğruluktan veren seçenekler denenecek sıradır


def test_floating_node_on_current_source_is_the_root_cause():
    report = _diag("floating_current_source")
    assert report.issue_type == SPICEIssueType.FLOATING_NODE
    assert report.nodes_involved == ["n"]
    assert "B1" in report.components_involved
    patched = SPICEDiagnostics.apply_diagnostic_patch("* t\nC1 n 0 1p\n.tran 1u\n.end\n", report)
    assert "R_bleed_n n 0 100Meg" in patched


@pytest.mark.parametrize("name,fix", [
    ("step_single", ".param"),
    ("step_no_param_kw", "'param' anahtar sözcüğü zorunlu"),
    ("dc_passive", "bağımsız kaynak"),
])
def test_sweep_directive_errors_get_specific_fixes(name, fix):
    report = _diag(name)
    assert report.issue_type == SPICEIssueType.SWEEP_DIRECTIVE_ERROR
    assert any(fix in r for r in report.actionable_recommendations)
    assert report.suggested_options == []  # .options bu hatayı çözmez


def test_missing_model_suggests_and_applies_library(monkeypatch):
    monkeypatch.setattr(diag_mod, "_lookup_library", lambda name: [
        {"name": "1N4148", "kind": "model", "file": "cmp/standard.dio", "directive": ".lib standard.dio"}
    ] if name.lower() == "1n4148" else [])
    report = _diag("missing_model")
    assert report.issue_type == SPICEIssueType.MISSING_MODEL_OR_SUBCKT
    assert "1n4148" in report.components_involved
    patched = SPICEDiagnostics.apply_diagnostic_patch("* d\nD1 b 0 1N4148\n.op\n.end\n", report)
    assert ".lib standard.dio" in patched.splitlines()
    assert SPICEDiagnostics.apply_diagnostic_patch(patched, report) == patched  # idempotent


def test_missing_subckt_name_comes_from_source_line(no_library):
    report = _diag("missing_subckt")
    assert report.issue_type == SPICEIssueType.MISSING_MODEL_OR_SUBCKT
    assert "FOOAMP" in report.components_involved
    assert any("FOOAMP" in r and "bulunamadı" in r for r in report.actionable_recommendations)


def test_missing_include_file_is_its_own_issue(no_library):
    report = _diag("missing_include")
    assert report.issue_type == SPICEIssueType.MISSING_INCLUDE_FILE
    assert "does_not_exist.lib" in report.root_cause_tr


def test_parallel_inductor_with_rser0_names_both_elements():
    report = _diag("parallel_l_rser0")
    assert report.issue_type == SPICEIssueType.PARALLEL_VOLTAGE_SOURCES
    assert report.components_involved == ["L1", "V1"]
    assert any("thev_induc" in r for r in report.actionable_recommendations)


def test_dc_op_diagnosis_does_not_recommend_uic():
    report = _diag("dcop_fail")
    assert report.suggested_options[0] == ".options solver=alt"  # ADI sırası (aşağıdaki test)
    uic = [r for r in report.actionable_recommendations if "uic" in r]
    assert uic and all("değildir" in r for r in uic)


def test_dc_options_follow_the_adi_table_order():
    """ADI TROUBLESHOOTING-GUIDE.md 'Improve DC Operating Point Convergence': "Try these in order (least to most
    aggressive)"; Simulator Options tablosu solver=alt, abstol, reltol, gmin, gshunt (değerler 'Try' sütunundan). Eski
    sıra bunun tersiydi ve yama en agresif ikisini (gshunt, gmin) birlikte ekliyordu (CHANGELOG 1.4.0 açık konular)."""
    assert _diag("dcop_fail").suggested_options == [
        ".options solver=alt", ".options abstol=1e-10", ".options reltol=0.005", ".options gmin=1e-10",
        ".options gshunt=1e-15",
    ]


def _added_options(log_name, netlist, rounds):
    """Her turda netlist yeniden teşhis edilir ve yama uygulanır; turda eklenen .options satırları."""
    text = (DATA / f"ref26_{log_name}.log.txt").read_text(encoding="utf-8")
    added = []
    for _ in range(rounds):
        report = SPICEDiagnostics.diagnose(text, netlist_code=netlist, errors_list=_log(log_name).errors)
        patched = SPICEDiagnostics.apply_diagnostic_patch(netlist, report)
        added.append([ln for ln in patched.splitlines() if ln.startswith(".options") and ln not in netlist.splitlines()])
        netlist = patched
    return added


def test_dc_patch_tries_the_adi_options_one_at_a_time():
    """'Try these in order': her teşhis netlistte ayarlı olmayan sıradaki tek seçeneği yamaya koyar."""
    net = "* x\nB1 a 0 V=if(V(a)>0.5,0,1)\nR1 a 0 1k\n.op\n.end\n"
    assert _added_options("dcop_fail", net, 3) == [
        [".options solver=alt"], [".options abstol=1e-10"], [".options reltol=0.005"],
    ]


def test_timestep_patch_continues_with_the_next_adi_option():
    """ADI 'Improve Transient Convergence' örneği tablonun ilk üç seçeneğini birlikte uygular ('.options method=gear
    itl4=50 cshunt=1f'); sonraki teşhisler tablonun sırasıyla tek seçenek ekler. Eski hata: ilk üçü ayarlı netlistte
    yama boştu, apply_fix ilerlemiyordu."""
    net = "* t\nR1 a 0 1k\n.tran 1m\n.end\n"
    assert _added_options("convreport_fail", net, 3) == [
        [".options method=gear", ".options itl4=50", ".options cshunt=1e-15"], [".options abstol=1e-10"],
        [".options gshunt=1e-15"],
    ]


def test_timestep_patch_after_a_user_set_gear_continues_one_at_a_time():
    """Kullanıcı ilk üçlüden birini kendisi verdiyse (GEMINI.md salınımda method=gear'ı önerir) ADI örneğinin üçlüsü
    yarımdır: kalanlar tablonun sırasıyla tek tek eklenir (test-reviewer: kısmi durum sınanmıyordu)."""
    net = "* t\nR1 a 0 1k\n.options method=gear\n.tran 1m\n.end\n"
    assert _added_options("convreport_fail", net, 3) == [
        [".options itl4=50"], [".options cshunt=1e-15"], [".options abstol=1e-10"],
    ]


def test_diagnose_tool_progresses_through_the_adi_order(tmp_path):
    """Araç teşhisi netlistle yapar: ikinci apply_fix ADI üçlüsünü tekrarlamaz, sıradakini (abstol) ekler. Log: gerçek
    LTspice 26.1.1 time step hatası (ref26_convreport_fail). Eskiden diagnose netlisti hiç kullanmıyordu."""
    adapter = LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path)
    (tmp_path / "osc.net").write_text("* t\nR1 a 0 1k\n.tran 1m\n.end\n", encoding="utf-8")
    (tmp_path / "osc.log").write_bytes((DATA / "ref26_convreport_fail.log.txt").read_bytes())
    for _ in range(2):
        res = handle_tool_call("diagnose_circuit_issue", {"circuit_name": "osc", "apply_fix": True}, adapter=adapter)
        assert res["fix_applied"] is True
    net = (tmp_path / "osc.net").read_text(encoding="utf-8")
    assert [ln for ln in net.splitlines() if ln.startswith(".options")] == [
        ".options method=gear", ".options itl4=50", ".options cshunt=1e-15", ".options abstol=1e-10",
    ]


def test_simulation_diagnosis_uses_the_netlist_that_failed(tmp_path, monkeypatch):
    """Koşumun teşhisi (SimulationResult.diagnostics) LTspice'a giden netliste bakar: ilk üçlüyü zaten içeren netlistin
    yaması sıradaki seçenektir. Gerçek sınır subprocess.Popen; ref26_convreport_fail logu netlistin yanına yazılır."""
    import ltspice_llm.adapter as adapter_mod

    class _Replay:
        def __init__(self, cmd, **kwargs):
            Path(cmd[-1]).with_suffix(".log").write_bytes((DATA / "ref26_convreport_fail.log.txt").read_bytes())
            self.pid, self.returncode = 999, 0

        def communicate(self, timeout=None):
            return "", ""

    monkeypatch.setattr(adapter_mod.subprocess, "Popen", _Replay)
    adapter = LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path)
    net = "* t\nR1 a 0 1k\n.options method=gear itl4=50 cshunt=1e-15\n.tran 1m\n.end\n"
    res = adapter.run_netlist(net, circuit_name="osc", treat_as_text=True)
    assert res.success is False and res.diagnostics["issue_type"] == "TIMESTEP_TOO_SMALL"
    patch = res.diagnostics["suggested_netlist_patch"].splitlines()
    assert [ln for ln in patch if ln.startswith(".options")] == [".options abstol=1e-10"]


def test_singular_matrix_patch_fixes_the_circuit_not_the_options():
    """ADI TROUBLESHOOTING-GUIDE.md 'Simulation Stops with Errors': 'Singular matrix | Circuit topology problem
    (floating node, voltage source loop)'; çözümü 'Fix the Circuit'. Eskiden yama ADI'de olmayan cshunt=1e-14 ve
    gmin=1e-10 ekliyordu."""
    report = SPICEDiagnostics.diagnose("Singular matrix: check node n_floating")
    assert report.issue_type == SPICEIssueType.SINGULAR_MATRIX
    assert report.suggested_options == []
    patch = report.suggested_netlist_patch.splitlines()
    # ADI çözümü devreyi düzeltmektir: boşta düğümü şaseye bağlayan bir direnç (değeri kaynağa bağlı değil, sınanmaz)
    (bleeder,) = [line.split() for line in patch if line.startswith("R_bleed")]
    assert bleeder[1:3] == ["n_floating", "0"]
    assert not any(line.startswith(".options") for line in patch)


def test_unknown_error_gets_no_blind_gear_patch():
    report = SPICEDiagnostics.diagnose("", errors_list=["Simülasyon log dosyası üretilemedi."])
    assert report.issue_type == SPICEIssueType.UNKNOWN
    assert report.suggested_options == [] and report.suggested_netlist_patch == ""


def test_transient_options_follow_the_adi_table_order():
    """ADI TROUBLESHOOTING-GUIDE.md 'Improve Transient Convergence': tablo 'listed in recommended order to try',
    değerler 'Try' sütunundan; sekiz satırın hepsi. maxstep'in 'Try' değeri 'explicit' (devreye göre): seçenek satırı
    değil, öneri metni olarak gelir (CHANGELOG 1.4.0 açık konusu: maxstep ve solver=alt listede yoktu)."""
    report = _diag("convreport_fail")
    assert report.suggested_options == [
        ".options method=gear", ".options itl4=50", ".options cshunt=1e-15",
        ".options abstol=1e-10", ".options gshunt=1e-15", ".options reltol=0.005", ".options solver=alt",
    ]
    assert sum("maxstep" in r for r in report.actionable_recommendations) == 1


def test_timestep_diagnosis_says_when_and_shows_the_device_score():
    """Gerçek log: 'time = 2e-13' ve 'B1 : 98.601642' (iki skor bloğunun büyüğü)."""
    report = _diag("convreport_fail")
    assert "(t = 2e-13 s)" in report.root_cause_tr
    assert "convreport puanları (cihaz): B1=98.6016." in report.actionable_recommendations


def test_compiler_error_is_counted_once_with_its_source_line():
    """Parser derleyici hatasına kaynak satırı ekler; ham logdaki aynı mesaj ikinci kez eşleşen satır olmaz."""
    report = _diag("step_single")
    assert report.matched_error_lines == [_log("step_single").errors[0]]
    assert report.matched_error_lines[0].endswith("| satır: .step param Rx list 1k")


def test_convergence_summary_keeps_the_five_hardest_devices():
    """DiagnosisReport.convergence: en zor 5 cihaz (LLM bağlamı); satırlar gerçek convreport logunun biçiminde."""
    rows = [("B1", 98.6), ("Q1", 12.0), ("M1", 77.5), ("D1", 3.1), ("R9", 55.0), ("C3", 64.2), ("X1", 0.4),
            ("D2", 30.0)]
    log = ("Device Convergence Difficulty Score:\n" + "".join(f"\t{n}\t : {s}\n" for n, s in rows)
           + "\nConvergence Failure:  Time step too small; time = 1e-06, timestep = 0\n")
    report = SPICEDiagnostics.diagnose(log)
    assert list(report.convergence["device_scores"]) == ["B1", "M1", "C3", "R9", "D2"]
    assert sorted(report.components_involved) == ["B1", "C3", "M1", "R9"]   # ~50'nin üstü sorunlu (ADI), 30 değil


def test_convergence_report_keeps_the_highest_score_whichever_block_comes_first():
    """Gerçek logda iki skor bloğu var, büyüğü (98.601642) önde; yerleri değişince de büyük olan kalır."""
    text = (DATA / "ref26_convreport_fail.log.txt").read_text(encoding="utf-8")
    swapped = text.replace("98.601642", "@").replace("98.584471", "98.601642").replace("@", "98.584471")
    assert swapped != text
    assert LogParser.parse_text(swapped).convergence["device_scores"] == {"B1": pytest.approx(98.601642)}


def test_dc_patch_does_not_repeat_an_option_set_on_a_continuation_line():
    """ADI SPICE-SYNTAX-REFERENCE: satır başındaki '+' önceki satırın devamıdır; orada verilen solver, reltol ve gmin
    kullanıcının. ADI sırasında (solver=alt, abstol, reltol, gmin, gshunt) ayarlı olmayan tek seçenek gshunt kalır.
    Netlistsiz raporun yaması solver=alt'tır; o da devam satırında olduğu için ikinci kez eklenmemeli (test-reviewer:
    eski hali yamanın hiç içermediği seçenekleri sayıyordu)."""
    netlist = ("* t\nB1 a 0 V=if(V(a)>0.5,0,1)\nR1 a 0 1k\n.options abstol=1e-10\n"
               "+ solver=alt reltol=0.002 gmin=1e-12\n.op\n.end\n")
    assert _added_options("dcop_fail", netlist, 1) == [[".options gshunt=1e-15"]]
    patched = SPICEDiagnostics.apply_diagnostic_patch(netlist, _diag("dcop_fail"))  # netlistsiz rapor: solver=alt
    assert patched.count("solver=") == 1


def test_dc_patch_recognises_the_singular_option_directive():
    """LTspice 26.1.1 logu kendisi '.option gminsteps=0' önerir: '.option solver=alt' de kullanıcının ayarıdır."""
    assert '".option gminsteps=0"' in (DATA / "ref26_dcop_fail.log.txt").read_text(encoding="utf-8")
    netlist = "* t\nB1 a 0 V=if(V(a)>0.5,0,1)\nR1 a 0 1k\n.option solver=alt\n.op\n.end\n"
    patched = SPICEDiagnostics.apply_diagnostic_patch(netlist, _diag("dcop_fail"))  # netlistsiz rapor: solver=alt
    assert patched.count("solver=") == 1
    assert _added_options("dcop_fail", netlist, 1) == [[".options abstol=1e-10"]]


def test_every_suggested_reference_is_a_section_of_the_adi_documents(no_library):
    """references 'BELGE.md: Bölüm' gösterir (search_ltspice_reference ile aranır): bölüm ADI'nin yerel belgesinde
    birebir başlık olmalı. Her hata türü için bir rapor, çoğu gerçek LTspice 26.1.1 logundan."""
    docs = ReferenceDocs()
    if not docs.available:
        pytest.skip("ADI LTspice AI Reference belgeleri bu makinede yok (LTspice 26+ ile kurulur)")
    reports = [_diag(name) for name in ("two_analyses", "step_single", "missing_include", "missing_model",
                                        "syntax_bad_line", "floating_current_source", "parallel_l_rser0",
                                        "convreport_fail", "dcop_fail")]
    reports += [SPICEDiagnostics.diagnose("Singular matrix: check node n_floating"),
                SPICEDiagnostics.diagnose("", errors_list=["Simülasyon log dosyası üretilemedi."])]
    assert {r.issue_type for r in reports} == set(SPICEIssueType)
    for ref in {ref for r in reports for ref in r.references}:
        file, _, section = ref.partition(": ")
        text = (docs.directory / file).read_text(encoding="utf-8")
        assert not section or re.search(rf"^#+ {re.escape(section)}\s*$", text, re.M), ref


# --------------------------------------------------------------------------- şema: pinler ve kodlama
def test_current_source_pins_match_ltspice_symbol():
    """lib/sym/current.asy: PIN 0 0 / PIN 0 80 (eskiden 0,16 / 0,96 -> netlistte 'I1 NC_01 NC_02')."""
    assert SYMBOL_PIN_DEFS["current"]["+"] == (0, 0)
    assert SYMBOL_PIN_DEFS["current"]["-"] == (0, 80)
    sch = AscSchematic()
    i1 = sch.add_symbol("current", 400, 96, "R0", "I1", "1m")
    assert sch.get_pin(i1, "+") == (400, 96)
    assert sch.get_pin(i1, "-") == (400, 176)


def test_library_path_symbol_names_match_on_basename_only(monkeypatch):
    # Tabloda olmayan sembol artık ADI kütüphanesinden (.asy) okunuyor (tests/test_symbols.py); bu test yalnızca
    # tablonun ad eşleşmesini sınar, sonucu LTspice'ın kurulu olup olmamasına bağlı kalmasın.
    monkeypatch.setattr("ltspice_llm.symbols.library_symbol_dir", lambda: None)
    sch = AscSchematic()
    assert sch.get_pin(sch.add_symbol("OpAmps\\opamp2", 0, 0), "out") == (32, 64)
    with pytest.raises(KeyError):  # "battery" "e" sembolüyle eşleşmemeli
        sch.get_pin(sch.add_symbol("Misc\\battery", 0, 0), "+")


def test_mirror_transform_matches_adi_rule():
    """ADI: aynalamada önce px negatiflenir, sonra döndürme uygulanır."""
    for rot in ("0", "90", "180", "270"):
        assert transform_pin(100, 200, 16, 48, f"M{rot}") == transform_pin(100, 200, -16, 48, f"R{rot}")


def test_asc_encoding_round_trip_keeps_micro_sign_as_single_byte():
    text = "Version 4\nSHEET 1 880 680\nSYMBOL cap 0 0 R0\nSYMATTR Value 2.2µ\n"
    data = encode_asc_text(text)
    assert b"2.2\xb5" in data and b"\xc2\xb5" not in data  # UTF-8 µ LTspice'ta '2.2Âµ' olur
    assert decode_asc_bytes(data) == (text, "cp1252")


def test_asc_with_characters_outside_cp1252_is_written_as_utf16():
    text = "Version 4\nSHEET 1 880 680\nTEXT 0 0 Left 2 ;Türkçe açıklama: ğışİ\n"
    data = encode_asc_text(text)
    assert data[:2] == b"V\x00"  # BOM'suz UTF-16LE (LTspice'ın kendi biçimi)
    assert decode_asc_bytes(data) == (text, "utf-16-le")


def test_schematic_save_never_writes_utf8(tmp_path):
    sch = AscSchematic()
    sch.add_symbol("cap", 0, 0, "R0", "C1", "1µ")
    assert b"1\xb5" in sch.save(tmp_path / "c.asc").read_bytes()


@pytest.mark.parametrize("encoding", ["cp1252", "utf-16-le"])
def test_adapter_preserves_asc_encoding(tmp_path, encoding):
    """Eskiden UTF-8 + errors='ignore' ile okunuyordu: µ siliniyor (1µ -> 1 F), UTF-16 şema hiç çalışmıyordu."""
    src = tmp_path / "rc.asc"
    text = "Version 4\r\nSHEET 1 880 680\r\nSYMBOL cap 0 0 R0\r\nSYMATTR InstName C1\r\nSYMATTR Value 1µ\r\n"
    src.write_bytes(text.encode(encoding))
    ad = LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path / "ws", cpu_guard=False)
    target, _ = ad._prepare_netlist(src, "rc", parameters={"C1": "2.2µ"})
    written = target.read_bytes()
    assert decode_asc_bytes(written) == (text.replace("1µ", "2.2µ"), encoding)


# --------------------------------------------------------------------------- şema bağlantı denetimi
_NETLIST_WITH_OPEN_PINS = """* nc_test.asc
* Generated by LTspice 26.1.1 for Windows.
V1 N001 0 PULSE(0 1 0 1n 1n 1m 2m)
R1 out N001 1k
C1 out 0 1µ
I1 NC_01 NC_02 1m
R2 out NC_03 1k
R3 N002 out 1k
X§U1 0 N004 NC_04 IN LT8609S ;§pnba GND)SW)INTVcc)VIN
.tran 5m
.backanno
.end
"""


def test_inspect_schematic_reports_unconnected_pins_and_dangling_nets(tmp_path, monkeypatch):
    ad = LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path, cpu_guard=False)
    monkeypatch.setattr(ad, "schematic_to_netlist", lambda src, **kw: _NETLIST_WITH_OPEN_PINS)
    report = ad.inspect_schematic("Version 4\nSHEET 1 1 1\n", treat_as_text=True)
    assert report["status"] == "WARNING"
    assert [(p["element"], p["net"]) for p in report["unconnected_pins"]] == [
        ("I1", "NC_01"), ("I1", "NC_02"), ("R2", "NC_03"), ("X§U1", "NC_04"),
    ]
    assert report["unconnected_pins"][-1]["pin_name"] == "INTVcc"
    assert report["dangling_nets"] == [{"net": "N002", "element": "R3"}, {"net": "N004", "element": "X§U1"}]


# --------------------------------------------------------------------------- referans belgeleri ve kütüphane
def _fake_reference(tmp_path):
    ref = tmp_path / "reference"
    ref.mkdir()
    (ref / "TROUBLESHOOTING-GUIDE.md").write_text(
        "---\ntitle: x\n---\n# Guide\n\n## Table of Contents\n- .step\n\n"
        "## Sweep Directive Errors\n\n### .STEP Rejected Before Simulating\n"
        "Every `.step` must produce at least two distinct steps.\n\n## Waveform Compression\nplotwinsize\n",
        encoding="utf-8",
    )
    (ref / "LTSPICE-KEYBOARD-SHORTCUTS.md").write_text("## Simulator\n.step .step .step\n", encoding="utf-8")
    return ref


def test_reference_search_ranks_heading_matches_and_skips_toc(tmp_path):
    docs = ReferenceDocs(_fake_reference(tmp_path))
    hits = docs.search(".step steps")
    assert hits[0].heading == ".STEP Rejected Before Simulating"
    assert all(h.heading != "Table of Contents" for h in hits)
    assert docs.search("") == [] and docs.search("yokboylebirsey") == []


def test_reference_tool_works_without_ltspice(tmp_path, monkeypatch):
    monkeypatch.setenv("LTSPICE_REFERENCE_DIR", str(_fake_reference(tmp_path)))
    res = handle_tool_call("search_ltspice_reference", {"query": "only one step", "max_results": 1})
    assert res["status"] == "SUCCESS"
    assert res["results"][0]["section"] == ".STEP Rejected Before Simulating"


def test_reference_tool_reports_missing_documents(tmp_path, monkeypatch):
    monkeypatch.setenv("LTSPICE_REFERENCE_DIR", str(tmp_path / "yok"))
    res = handle_tool_call("search_ltspice_reference", {"query": "step"})
    assert res["status"] == "ERROR" and "LTSPICE_REFERENCE_DIR" in res["message"]


def test_find_model_definitions_indexes_utf16_and_encrypted_libraries(tmp_path):
    lib = tmp_path / "lib"
    (lib / "cmp").mkdir(parents=True)
    (lib / "sub").mkdir()
    (lib / "cmp" / "standard.dio").write_bytes("* c\n.model 1N4148 D(Is=2.52n)\n".encode("utf-16-le"))
    (lib / "sub" / "LTC.lib").write_text("* x\n.subckt LT1001 1 2 3 4 5\n.ends\n", encoding="utf-8")
    (lib / "sub" / "LT8610.sub").write_bytes(b"\r\n<Binary File>\r\n\x1a\x12\xd4")
    assert find_model_definitions("1n4148", lib)[0]["directive"] == ".lib standard.dio"
    assert find_model_definitions("LT1001", lib)[0] == {
        "name": "LT1001", "kind": "subckt", "file": "sub/LTC.lib", "directive": ".lib LTC.lib",
    }
    assert find_model_definitions("LT8610", lib)[0]["kind"] == "file"
    assert find_model_definitions("yok", lib) == []


# --------------------------------------------------------------------------- LLM çıktısı
def test_llm_dict_includes_fourier_and_trims_convergence_scores(tmp_path):
    scores = {f"D{i}": float(100 - i) for i in range(20)}
    res = SimulationResult(
        success=True, netlist_path=tmp_path / "x.net",
        fourier=[{"signal": "V(out)", "thd_percent": 1.2}], convergence={"device_scores": scores},
    )
    data = res.to_llm_dict()
    assert data["fourier"][0]["thd_percent"] == 1.2
    assert list(data["convergence"]["device_scores"]) == ["D0", "D1", "D2", "D3", "D4"]


def test_documents_lists_reference_file_names(tmp_path):
    assert ReferenceDocs(_fake_reference(tmp_path)).documents() == [
        "LTSPICE-KEYBOARD-SHORTCUTS.md", "TROUBLESHOOTING-GUIDE.md"]
    assert ReferenceDocs(tmp_path / "yok").documents() == []


def test_library_lookup_failure_counts_as_not_found(monkeypatch, tmp_path):
    """Kütüphane araması patlarsa teşhis çökmez; çalışırsa gerçek indeksten LTspice'ın okuduğu .lib satırını döner
    (find_model_definitions belgesi: 'X1 ... LT1001' + '.lib LTC.lib' LTspice'ta ölçüldü)."""
    from ltspice_llm import reference as ref_mod
    real = ref_mod.find_model_definitions

    def boom(name):
        raise OSError("lib dizini yok")
    monkeypatch.setattr(ref_mod, "find_model_definitions", boom)
    assert diag_mod._lookup_library("LT1001") == []
    monkeypatch.setattr(ref_mod, "find_model_definitions", real)
    (tmp_path / "lib" / "sub").mkdir(parents=True)
    (tmp_path / "lib" / "sub" / "LTC.lib").write_text(".subckt LT1001 1 2 3 4 5\n.ends LT1001\n", encoding="utf-8")
    monkeypatch.setenv(ref_mod.ENV_DATA_DIR, str(tmp_path))   # LTspice veri klasörü: gerçek sınır
    assert diag_mod._lookup_library("LT1001")[0]["directive"] == ".lib LTC.lib"


def test_ltspice_data_dir_prefers_the_environment_variable_then_localappdata(tmp_path, monkeypatch):
    """Belge: sırayla LTSPICE_DATA_DIR, %LOCALAPPDATA%/LTspice; ikisi de yoksa None (Windows)."""
    from ltspice_llm import reference as ref_mod
    local = tmp_path / "local"
    (local / "LTspice").mkdir(parents=True)
    monkeypatch.setattr(ref_mod.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv(ref_mod.ENV_DATA_DIR, str(tmp_path))
    assert ref_mod.ltspice_data_dir() == tmp_path
    monkeypatch.setenv(ref_mod.ENV_DATA_DIR, str(tmp_path / "yok"))
    assert ref_mod.ltspice_data_dir() == local / "LTspice"
    monkeypatch.delenv(ref_mod.ENV_DATA_DIR)
    monkeypatch.delenv("LOCALAPPDATA")
    assert ref_mod.ltspice_data_dir() is None


def test_ltspice_expected_here_message_is_classified_as_syntax_error(no_library):
    """Gerçek LTspice 26.1.1 logu (examples/14): '1k R2 in 0' -> 'Expected device instantiation or directive here.'
    Tarama direktifi hataları (step_no_param_kw) bilerek daha özel SWEEP_DIRECTIVE_ERROR sınıfındadır."""
    assert _diag("syntax_bad_line").issue_type == SPICEIssueType.SYNTAX_ERROR
