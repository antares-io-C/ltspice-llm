"""
SPICE Hata ve Yakınsama Teşhis Motoru Testleri
"""

import sys

import pytest
from ltspice_llm.diagnostics import SPICEDiagnostics, SPICEIssueType
from ltspice_llm.tools import handle_tool_call
from ltspice_llm.adapter import LTSpiceAdapter


def test_diagnostics_timestep_too_small():
    log = """
    Circuit: * High Frequency Oscillator
    Direct Newton iteration for .op point succeeded.
    Starting transient analysis
    Analysis: Time step too small; initial timepoint: trouble with node "n_feedback"
    Fatal Error: Analysis failed.
    """
    report = SPICEDiagnostics.diagnose(log)
    assert report.has_error is True
    assert report.issue_type == SPICEIssueType.TIMESTEP_TOO_SMALL
    assert any(".options method=gear" in opt for opt in report.suggested_options)
    assert any(".options cshunt=" in opt for opt in report.suggested_options)
    assert len(report.actionable_recommendations) > 0


def test_diagnostics_singular_matrix():
    log = """
    Circuit: * LC Filter without ground return
    Singular matrix: check node n_floating
    Iteration No. 1
    Fatal Error: Singular matrix: check node n_floating
    """
    report = SPICEDiagnostics.diagnose(log)
    assert report.has_error is True
    assert report.issue_type == SPICEIssueType.SINGULAR_MATRIX
    assert "n_floating" in report.nodes_involved
    assert any("R_bleed_n_floating" in line for line in report.suggested_netlist_patch.splitlines())


def test_diagnostics_dc_op_convergence():
    log = """
    Circuit: * Non-linear BJT latch
    Iteration limit reached in DC operating point calculation.
    Could not find DC operating point.
    """
    report = SPICEDiagnostics.diagnose(log)
    assert report.has_error is True
    assert report.issue_type == SPICEIssueType.DC_OP_CONVERGENCE
    # ADI TROUBLESHOOTING-GUIDE.md "Improve DC Operating Point Convergence", "least to most aggressive":
    # solver=alt, abstol, reltol, gmin, gshunt (itl1 orada önerilmiyor). Eskiden sıra tersti (CHANGELOG 1.4.0).
    assert report.suggested_options[0] == ".options solver=alt"
    assert report.suggested_options[-1] == ".options gshunt=1e-15"
    assert any(".options gmin=" in opt for opt in report.suggested_options)
    # 'uic' DC çalışma noktası sorunu için çözüm olarak önerilmez; aksine uyarılır
    assert not any("uic" in r and "değildir" not in r for r in report.actionable_recommendations)


def test_apply_diagnostic_patch():
    netlist = """* Test Circuit
R1 in out 1k
C1 out 0 10n
.tran 1m
.end
"""
    log = "Analysis: Time step too small; trouble with node out"
    report = SPICEDiagnostics.diagnose(log)
    patched = SPICEDiagnostics.apply_diagnostic_patch(netlist, report)

    assert ".options method=gear" in patched
    assert ".options cshunt=1e-15" in patched
    # .end en sonda kalmalı
    assert patched.strip().endswith(".end")


def _timestep_report():
    return SPICEDiagnostics.diagnose("Analysis: Time step too small; trouble with node out")


@pytest.mark.parametrize("end_line", [".end", ".END", "  .end  ", ".end ; devre sonu", ".end  ;end of netlist"])
def test_patch_is_inserted_before_end_even_with_trailing_comment(end_line):
    """'.end ; yorum' satırı tanınmazsa yama .end'den sonraya düşer ve LTspice onu hiç okumaz."""
    netlist = f"* t\nR1 in out 1k\n.tran 1m\n{end_line}\n"
    lines = SPICEDiagnostics.apply_diagnostic_patch(netlist, _timestep_report()).splitlines()
    end_idx = next(i for i, l in enumerate(lines) if l.strip().lower().startswith(".end"))
    assert end_idx == len(lines) - 1  # .end son satır
    assert ".options method=gear" in lines[:end_idx]


def test_patch_preserves_trailing_newline_and_content():
    netlist = "* t\nR1 in out 1k\n.tran 1m\n.end\n"
    patched = SPICEDiagnostics.apply_diagnostic_patch(netlist, _timestep_report())
    assert patched.endswith(".end\n")
    assert patched.startswith("* t\nR1 in out 1k\n.tran 1m\n")


def test_patch_does_not_override_options_the_user_already_set():
    netlist = "* t\nR1 in out 1k\n.options gmin=1e-12 reltol=0.002\n.tran 1m\n.end\n"
    patched = SPICEDiagnostics.apply_diagnostic_patch(netlist, _timestep_report())
    assert patched.count("gmin=") == 1 and "gmin=1e-12" in patched  # çelişen ikinci gmin eklenmez
    assert patched.count("reltol=") == 1
    assert ".options method=gear" in patched


def test_patch_is_idempotent():
    netlist = "* t\nR1 in out 1k\n.tran 1m\n.end\n"
    once = SPICEDiagnostics.apply_diagnostic_patch(netlist, _timestep_report())
    assert SPICEDiagnostics.apply_diagnostic_patch(once, _timestep_report()) == once


def test_patch_handles_netlist_without_end():
    patched = SPICEDiagnostics.apply_diagnostic_patch("* t\nR1 in out 1k\n.tran 1m", _timestep_report())
    assert patched.endswith(".end\n") and ".options method=gear" in patched


def test_bleed_resistors_are_not_duplicated():
    log = "Singular matrix: check node n_floating"
    netlist = "* t\nC1 n_floating 0 1n\n.tran 1m\n.end\n"
    report = SPICEDiagnostics.diagnose(log)
    once = SPICEDiagnostics.apply_diagnostic_patch(netlist, report)
    assert once.count("R_bleed_n_floating") == 1
    assert SPICEDiagnostics.apply_diagnostic_patch(once, report).count("R_bleed_n_floating") == 1


def test_diagnose_tool_call(tmp_path):
    adapter = LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path)
    net_path = tmp_path / "osc.net"
    net_path.write_text("* Osc\nR1 a 0 1k\n.end\n", encoding="utf-8")
    log_path = tmp_path / "osc.log"
    log_path.write_text("Analysis: Time step too small\n", encoding="utf-8")

    res = handle_tool_call(
        "diagnose_circuit_issue",
        {"circuit_name": "osc", "apply_fix": True},
        adapter=adapter,
    )
    assert res["has_error"] is True
    assert res["issue_type"] == "TIMESTEP_TOO_SMALL"
    assert res["fix_applied"] is True

    # Netlist dosyasına yama eklenmiş olmalı
    updated_net = net_path.read_text(encoding="utf-8")
    assert ".options method=gear" in updated_net
