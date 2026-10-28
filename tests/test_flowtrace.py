"""
Use case akış doğrulamasının (tests/flowtrace.py) kendisinin testleri ve manifest tutarlılığı (LTspice gerektirmez).

Eşleştirme anlamı ent-dch-sws-app'teki use_case_flow_tracer.py ile aynı: varsayılan sıralı alt dizi (araya başka
çağrı girebilir), ``strict`` birebir dizi (yalnızca dizide adı geçen semboller sayılır).
"""

import ast
import json
import threading

import pytest

import flowtrace
from ltspice_llm import netlist

A, B, C = "netlist.insert_before_end", "adapter.LTSpiceAdapter._execute", "cpu_guard.CpuLease.release"


def _events(*names, thread=1):
    return [(thread, n) for n in names]


def test_manifests_are_consistent():
    assert flowtrace.validate(flowtrace.load_usecases()) == []


def test_every_manifest_has_a_scenario_bound_to_a_test():
    cases = flowtrace.load_usecases()
    assert len(cases) >= 3 and all(case["scenarios"] for case in cases)


@pytest.mark.parametrize("recorded, ok", [
    ([A, "x", B, C], True),        # araya giren çağrı alt diziyi bozmaz
    ([B, A, C], False),            # sıra yanlış
    ([A, C], False),               # adım eksik
])
def test_subsequence_mode(recorded, ok):
    problems = flowtrace.check({"sequence": [A, B, C]}, _events(*recorded))
    assert (problems == []) is ok


def test_strict_mode_rejects_an_extra_call_that_subsequence_accepts():
    recorded = _events(A, B, B, C)
    assert flowtrace.check({"sequence": [A, B, C]}, recorded) == []
    assert flowtrace.check({"sequence": [A, B, C], "mode": "strict"}, recorded) != []


def test_forbidden_and_counts():
    recorded = _events(A, B, B)
    assert flowtrace.check({"forbidden": [C]}, recorded) == []
    assert flowtrace.check({"forbidden": [B]}, recorded) == [f"{B} çağrılmamalıydı, 2 kez çağrıldı"]
    assert flowtrace.check({"counts": {B: 2}}, recorded) == []
    assert flowtrace.check({"counts": {B: 1}}, recorded) == [f"{B}: 1 çağrı bekleniyordu, 2 oldu"]


def test_per_thread_checks_each_thread_that_starts_the_sequence():
    good = _events(A, B, thread=1) + _events(A, B, thread=2)
    bad = _events(A, B, thread=1) + _events(A, thread=2)
    scenario = {"sequence": [A, B], "per_thread": True}
    assert flowtrace.check(scenario, good) == []
    assert len(flowtrace.check(scenario, bad)) == 1
    assert flowtrace.check(scenario, _events(B)) == [f"{A} hiçbir iş parçacığında çağrılmadı"]


def test_tracer_records_package_calls_in_order_including_other_threads():
    with flowtrace.CallTracer() as tracer:
        netlist.ltspice_visible(".end\n")
        worker = threading.Thread(target=netlist.insert_before_end, args=("* t\n.end\n", [".op"]))
        worker.start()
        worker.join()
    names = [n for _, n in tracer.events]
    assert names[0] == "netlist.ltspice_visible"
    assert "netlist.insert_before_end" in names
    threads = {ident for ident, n in tracer.events if n == "netlist.insert_before_end"}
    assert threads == {worker.ident}


def test_tracer_ignores_code_outside_the_package():
    with flowtrace.CallTracer() as tracer:
        json.dumps({"a": 1})
    assert tracer.events == []


def _write(tmp_path, case):
    (tmp_path / "uc.json").write_text(json.dumps(case), encoding="utf-8")
    return flowtrace.load_usecases(tmp_path)


def _case(**scenario):
    return {
        "use_case": "u", "source": ["README.md"],
        "actions": [{"id": "A1", "message": "m", "proof": "trace", "symbol": A, "source": "README.md"},
                    {"id": "A2", "message": "m", "proof": "boundary", "source": "README.md"}],
        "scenarios": [{"test": "tests/test_flowtrace.py::test_forbidden_and_counts", "flow": "Basic",
                       "actions": ["A1", "A2"], "sequence": [A],
                       "evidence": {"A2": "assert flowtrace.check({\"forbidden\": [C]}, recorded) == []"},
                       **scenario}],
    }


def test_validate_accepts_a_correct_manifest(tmp_path):
    assert flowtrace.validate(_write(tmp_path, _case())) == []


@pytest.mark.parametrize("change, fragment", [
    ({"sequence": ["netlist.yazim_hatasi"]}, "ltspice_llm'de yok"),
    ({"sequence": [B]}, "senaryoda denetlenmiyor"),
    ({"evidence": {"A2": "assert hiç_yazılmamış()"}}, "evidence testte yok"),
    ({"evidence": {}}, "evidence (testteki assert alıntısı) yok"),
    ({"actions": ["A1"], "evidence": {}}, "A2 hiçbir senaryoda kanıtlanmıyor"),
    ({"test": "tests/test_flowtrace.py::test_yok"}, "test fonksiyonu bulunamadı"),
    ({"flow": "Happy"}, "flow 'Happy' geçersiz"),
    ({"mode": "loose"}, "mode 'loose' geçersiz"),
])
def test_validate_reports_each_kind_of_drift(tmp_path, change, fragment):
    problems = flowtrace.validate(_write(tmp_path, _case(**change)))
    assert any(fragment in p for p in problems), problems


@pytest.mark.parametrize("source, name, line", [
    ('def f():\n    "doc"\n    global g\n    g = 1\n', "f", 4),
    ("def h():\n    x = 0\n    def i():\n        nonlocal x\n        x += 1\n", "i", 5),
])
def test_first_body_line_skips_statements_without_bytecode(source, name, line):
    """Docstring, global ve nonlocal bayt kodu üretmez; coverage.py o satırı hiç çalışmış görmez. Antigravity
    incelemesi (2026-10-02): agent.py eval_point'in ilk satırı nonlocal, --uncovered onu "girilmedi" sayıyordu."""
    node = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef) and n.name == name)
    assert flowtrace._first_body_line(node) == line


def test_optimizer_eval_point_first_line_is_executable():
    file, line = flowtrace.symbols()[1]["agent.CircuitOptimizer.optimize_single_parameter.<locals>.eval_point"]
    text = (flowtrace.PACKAGE / "agent.py").read_text(encoding="utf-8").splitlines()[line - 1]
    assert not text.strip().startswith(("nonlocal", "global"))


def test_first_body_line_matches_the_compiled_code_for_every_package_function():
    """Bağımsız oracle: derlenmiş kod nesnesinde def satırından sonraki ilk satır (co_lines)."""
    mismatches = []
    for path in sorted(flowtrace.PACKAGE.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        codes, stack = {}, [compile(source, str(path), "exec")]
        while stack:
            code = stack.pop()
            codes[(code.co_firstlineno, code.co_name)] = code
            stack += [c for c in code.co_consts if hasattr(c, "co_lines")]
        for name, node in flowtrace._walk(ast.parse(source).body, path.stem):
            code = codes[(min([node.lineno] + [d.lineno for d in node.decorator_list]), node.name)]
            lines = sorted({ln for _, _, ln in code.co_lines() if ln is not None and ln > node.lineno})
            if lines and lines[0] != flowtrace._first_body_line(node):
                mismatches.append((name, lines[0], flowtrace._first_body_line(node)))
    assert mismatches == []
