"""
LTspice - LLM Adapter Unit Tests
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Adaptörün tüm temel yeteneklerini (simülasyon, log parser, raw özeti,
tool handlers, visualizer ve optimizer) doğrulayan test paketi.
"""

import pytest
from ltspice_llm import (
    LTSpiceAdapter,
    CircuitOptimizer,
    AutonomousCircuitAgent,
    WaveformVisualizer,
    handle_tool_call,
)


@pytest.fixture(scope="session")
def adapter():
    """Gerçek LTspice gerektirir; kurulu değilse bu dosyadaki testler atlanır."""
    try:
        return LTSpiceAdapter()
    except FileNotFoundError:
        pytest.skip("LTspice bu makinede kurulu değil")


def test_adapter_initialization(adapter):
    """LTspice executable dosyasının başarıyla bulunduğunu test eder."""
    assert adapter.executable_path.exists()
    assert adapter.executable_path.is_file()
    assert adapter.workspace_dir.exists()


def test_rc_simulation_run(adapter):
    """Temel bir RC devresinin koşulmasını ve .meas değerlerinin ayrıştırılmasını test eder."""
    netlist = """* RC Test
V1 in 0 PULSE(0 5 0 1n 1n 1m 2m)
R1 in out 1k
C1 out 0 1u
.tran 3m
.meas tran v_max MAX V(out)
.meas tran v_tau FIND V(out) WHEN time=1m
.end
"""
    res = adapter.run_netlist(netlist, circuit_name="test_rc_pytest")
    assert res.success is True
    assert "v_max" in res.measurements
    assert "v_tau" in res.measurements
    assert 3.5 <= res.measurements["v_max"] <= 4.0
    assert 3.0 <= res.measurements["v_tau"] <= 3.3


def test_error_detection(adapter):
    """Hatalı SPICE sözdiziminin doğru şekilde yakalandığını test eder."""
    bad_netlist = """* Broken Circuit
V1 in 0 5
R1 in out
.tran 1m
.end
"""
    res = adapter.run_netlist(bad_netlist, circuit_name="test_bad_circuit")
    assert res.success is False
    assert len(res.errors) > 0


def test_tool_calling_simulate_and_tune(adapter):
    """LLM Tool handler fonksiyonlarının uçtan uca çalıştığını test eder."""
    # 1. create_or_replace_circuit çağrısı
    sim_call = {
        "circuit_name": "pytest_voltage_div",
        "netlist_code": """* Test Div
.param R1_V=10k
.param R2_V=10k
V1 in 0 10V
R1 in out {R1_V}
R2 out 0 {R2_V}
.op
.meas vout FIND V(out)
.end
""",
    }
    resp1 = handle_tool_call("create_or_replace_circuit", sim_call, adapter=adapter)
    assert resp1["status"] == "SUCCESS"
    assert resp1["measurements"].get("vout") == pytest.approx(5.0)

    # 2. modify_parameters çağrısı
    tune_call = {
        "circuit_name": "pytest_voltage_div",
        "parameters": {"R2_V": "30k"},
    }
    resp2 = handle_tool_call("modify_parameters", tune_call, adapter=adapter)
    assert resp2["status"] == "SUCCESS"
    assert resp2["measurements"].get("vout") == pytest.approx(7.5)


def test_list_circuits_and_get_netlist(adapter):
    """list_circuits ve get_circuit_netlist araçlarını test eder (başka testin bıraktığı dosyaya bağımlı değil)."""
    create = handle_tool_call(
        "create_or_replace_circuit",
        {
            "circuit_name": "pytest_list_probe",
            "netlist_code": """* Probe
V1 in 0 10V
R1 in 0 1k
.op
.end
""",
        },
        adapter=adapter,
    )
    assert create["status"] == "SUCCESS"

    list_res = handle_tool_call("list_circuits", {}, adapter=adapter)
    assert list_res["status"] == "SUCCESS"
    assert "pytest_list_probe" in [c["name"] for c in list_res["circuits"]]

    netlist_res = handle_tool_call("get_circuit_netlist", {"circuit_name": "pytest_list_probe"}, adapter=adapter)
    assert netlist_res["status"] == "SUCCESS"
    assert "V1 in 0 10V" in netlist_res["netlist"]


def test_tool_error_handling_graceful(adapter):
    """Araçlara geçersiz argümanlar veya bilinmeyen araç adı verildiğinde güvenli hata dönüldüğünü test eder."""
    # Bilinmeyen araç
    res1 = handle_tool_call("non_existent_tool", {}, adapter=adapter)
    assert res1["status"] == "ERROR"

    # Boş kod
    res2 = handle_tool_call("create_or_replace_circuit", {"netlist_code": ""}, adapter=adapter)
    assert res2["status"] == "ERROR"

    # Var olmayan devre dosyası için parametre değiştirme
    res3 = handle_tool_call("modify_parameters", {"circuit_name": "definitely_not_existing_12345"}, adapter=adapter)
    assert res3["status"] == "ERROR"


def test_bode_plot_generation(adapter):
    """AC analizinden Bode diyagramı çizimini test eder."""
    ac_netlist = """* RC AC Filter
V1 in 0 AC 1 0
R1 in out 10k
C1 out 0 15.915n
.ac dec 15 10 100k
.meas ac f_3db WHEN mag(V(out))=0.7071
.end
"""
    res = adapter.run_netlist(ac_netlist, circuit_name="pytest_ac_test")
    assert res.success is True
    assert res.raw_path is not None

    bode_path = WaveformVisualizer.plot_bode(
        raw_path=res.raw_path,
        out_node="V(out)",
        in_node="V(in)",
    )
    assert bode_path.exists()
    assert bode_path.stat().st_size > 1000

    b64 = WaveformVisualizer.plot_to_base64(raw_path=res.raw_path, signals=["V(out)"])
    assert isinstance(b64, str)
    assert len(b64) > 100


def test_autonomous_circuit_agent(adapter):
    """AutonomousCircuitAgent sınıfının fonksiyonlarını test eder."""
    agent = AutonomousCircuitAgent(adapter=adapter)
    assert len(agent.tools) >= 5
    assert len(agent.get_history()) == 1

    circuits = agent.list_circuits()
    assert circuits["status"] == "SUCCESS"

    agent.reset()
    assert len(agent.get_history()) == 1


def test_optimizer_convergence(adapter):
    """Kapalı devre optimizasyonun hedefe yakınsadığını test eder."""
    optimizer = CircuitOptimizer(adapter=adapter)
    netlist = """* Op RC
.param R_VAL=1k
V1 in 0 PULSE(0 5 0 1n 1n 2m 4m)
R1 in out {R_VAL}
C1 out 0 1u
.tran 2m
.meas tran v_meas FIND V(out) WHEN time=1m
.end
"""
    target = 3.0
    res = optimizer.optimize_single_parameter(
        base_netlist=netlist,
        circuit_name="pytest_opt",
        param_name="R_VAL",
        target_meas_name="v_meas",
        target_value=target,
        min_param=200.0,
        max_param=3000.0,
        tolerance=0.01,
        max_iterations=10,
    )
    assert res.converged is True
    assert abs(res.best_value - target) <= 0.05
