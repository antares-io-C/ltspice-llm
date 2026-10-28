"""
Netlist simülasyonunun başarı ve log-hata yolları, gerçek LTspice çıktısını yeniden oynatan sahte süreçle
(LTspice gerektirmez; akış: tests/usecases/run_netlist.json).

Sahte olan yalnızca gerçek sınırdır (``subprocess.Popen``): başlatılınca gerçek LTspice 26.1.1 çıktılarını
(``tests/data/ref26_flow_rc.*``, examples/14; ``ref26_dcop_fail.log.txt``) netlistin yanına yazar ve rc=0 döner.
"""

import sys
from pathlib import Path

import pytest

import ltspice_llm.adapter as adapter_mod
from ltspice_llm import LTSpiceAdapter
from ltspice_llm.diagnostics import SPICEIssueType

DATA = Path(__file__).parent / "data"
RC = (DATA / "ref26_flow_rc.net").read_text(encoding="utf-8")


class _ReplayPopen:
    outputs = {}
    netlists = []

    def __init__(self, cmd, **kwargs):
        netlist = Path(cmd[-1])
        type(self).netlists.append(netlist.read_text(encoding="utf-8"))
        for suffix, source in type(self).outputs.items():
            netlist.with_suffix(suffix).write_bytes(source.read_bytes())
        self.pid, self.returncode = 999, 0

    def communicate(self, timeout=None):
        return "", ""


@pytest.fixture
def replay(monkeypatch):
    monkeypatch.setattr(adapter_mod.subprocess, "Popen", _ReplayPopen)
    _ReplayPopen.netlists = []
    return _ReplayPopen


def _adapter(tmp_path):
    return LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path / "ws", solver_threads=1)


def test_successful_run_reads_measurements_from_the_ltspice_log(tmp_path, replay):
    replay.outputs = {".log": DATA / "ref26_flow_rc.log.txt", ".raw": DATA / "ref26_flow_rc.raw"}
    res = _adapter(tmp_path).run_netlist(RC, circuit_name="rc", treat_as_text=True)
    assert res.success and res.errors == []
    assert res.measurements["v_max"] == pytest.approx(3.58834481239)  # LTspice 26.1.1 logu (examples/14)
    assert any(name.lower() == "v(out)" for name in res.raw_summary)
    assert ".options threads=1" in replay.netlists[0]


def test_failed_operating_point_with_exit_code_zero_is_not_success(tmp_path, replay):
    """GEMINI.md: başarıyı çıkış kodundan değil logdan belirle (Simulation Failed olan koşum rc=0 dönebiliyor)."""
    replay.outputs = {".log": DATA / "ref26_dcop_fail.log.txt"}
    res = _adapter(tmp_path).run_netlist(RC, circuit_name="fail", treat_as_text=True)
    assert not res.success
    assert SPICEIssueType.DC_OP_CONVERGENCE.value in str(res.diagnostics)
