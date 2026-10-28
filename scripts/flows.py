"""
Use case senaryolarının ikinci tanığı: gcov ``--gcov-check``'in Python karşılığı (coverage.py dinamik bağlamları).

Tracer (``tests/flowtrace.py``, pytest kancası) çağrı sırasını denetler. Bu betik aynı testleri coverage.py ile
``--cov-context=test`` altında koşar ve ayrı bir mekanizmayla doğrular: her senaryonun ``trace`` eylemlerinin
fonksiyonuna o testte girildi, ``forbidden`` fonksiyonlara girilmedi. ``--uncovered`` bütün paketi koşar ve
hiçbir testin girmediği fonksiyonları, ``--full`` ile yalnızca LTspice'lı testlerin girdiklerini de listeler.

    python scripts/flows.py                  # LTspice'sız senaryolar (LTspice'lılar ATLANDI)
    python scripts/flows.py --full           # LTspice'lı senaryolar da
    python scripts/flows.py --uncovered      # hiçbir testin girmediği fonksiyonlar

Sorun varsa 1 döner. Batch LTspice çalışırken ``--full`` kullanma (bkz. GEMINI.md "Performans ve eşzamanlılık").
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import flowtrace  # noqa: E402

LTSPICE_TESTS = ("tests/test_adapter.py", "tests/test_integration_")
MAX_LIST = 60


def run_with_contexts(targets, full, data_file):
    # ctrace: 3.14'ün varsayılan sys.monitoring çekirdeği bir satırı ilk testten sonra bir daha görmüyor, test başına
    # bağlam eksik kalıyordu (ölçüldü: koşan senaryolar "girilmedi"/"koşmadı" görünüyordu)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", COVERAGE_FILE=str(data_file), COVERAGE_CORE="ctrace")
    if not full:
        env["LTSPICE_EXE"] = str(ROOT / "nonexistent-ltspice" / "LTspice.exe")
    cmd = [sys.executable, "-m", "pytest", *targets, "-q", "-p", "no:cacheprovider", "--tb=line",
           "--cov=ltspice_llm", "--cov-context=test", "--cov-report="]
    return subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=False)


def entered_by(data_file):
    """``{sembol: {test}}``: fonksiyonun ilk gövde satırını çalıştıran testler (parametreler birleşik)."""
    from coverage import CoverageData

    data = CoverageData(basename=str(data_file))
    data.read()
    first_lines = {}
    for name, (file, line) in flowtrace.symbols()[1].items():
        first_lines.setdefault(file, {})[line] = name
    entered = {}
    for measured in data.measured_files():
        lines = first_lines.get(flowtrace._norm(measured))
        if not lines:
            continue
        for line, contexts in (data.contexts_by_lineno(measured) or {}).items():
            if line in lines:
                # yalnızca test gövdesi: atlanan testin fixture kurulumu (|setup) "koştu" sayılmasın
                tests = {c.rsplit("|", 1)[0].split("[", 1)[0] for c in contexts if c.endswith("|run")}
                if tests:
                    entered[lines[line]] = tests
    return entered


def witness(cases, entered):
    """Her senaryo için (durum, test, ayrıntı). Durum: TAMAM, KISMİ ya da ATLANDI (test koşmadı)."""
    ran = set().union(*entered.values()) if entered else set()
    rows = []
    for uc in cases:
        actions = {a["id"]: a for a in uc["actions"]}
        for sc in uc["scenarios"]:
            test = sc["test"]
            if test not in ran:
                rows.append(("ATLANDI", test, "test koşmadı (LTspice gerektiriyorsa --full)"))
                continue
            trace = [actions[a]["symbol"] for a in sc["actions"] if actions[a]["proof"] == "trace"]
            missing = [s for s in trace if s not in sc.get("forbidden", []) and test not in entered.get(s, set())]
            hit = [s for s in sc.get("forbidden", []) if test in entered.get(s, set())]
            issues = [f"girilmedi: {s}" for s in missing] + [f"girilmemeliydi: {s}" for s in hit]
            rows.append(("KISMİ" if issues else "TAMAM", test, "; ".join(issues) or f"{len(trace)} eylem"))
    return rows


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--full", action="store_true", help="LTspice gerektiren testleri de koş")
    parser.add_argument("--uncovered", action="store_true", help="bütün paket: hiçbir testin girmediği fonksiyonlar")
    opts = parser.parse_args(argv)

    cases = flowtrace.load_usecases()
    problems = flowtrace.validate(cases)
    if problems:
        print("[FAIL] manifest:\n- " + "\n- ".join(problems))
        return 1
    targets = ["tests"] if opts.uncovered else sorted({sc["test"] for uc in cases for sc in uc["scenarios"]})
    with tempfile.TemporaryDirectory() as tmp:
        data_file = Path(tmp) / ".coverage"
        proc = run_with_contexts(targets, opts.full, data_file)
        entered = entered_by(data_file) if data_file.exists() else {}
    tail = proc.stdout.strip().splitlines()[-1:] or [proc.stderr.strip()[-300:]]
    failed = proc.returncode not in (0, 5)
    if failed:
        print("[FAIL] pytest (tracer ya da assert):\n" + "\n".join(proc.stdout.strip().splitlines()[-MAX_LIST:]))

    if opts.uncovered:
        never = sorted(s for s in flowtrace.symbols()[1] if s not in entered)
        print(f"Hiçbir testin girmediği fonksiyonlar ({len(never)}/{len(flowtrace.symbols()[1])}):")
        print("\n".join(f"  {s}" for s in never[:MAX_LIST]) + (f"\n  ... (+{len(never) - MAX_LIST})" if len(never) > MAX_LIST else ""))
        if opts.full:
            only = sorted(s for s, tests in entered.items() if all(t.startswith(LTSPICE_TESTS) for t in tests))
            print(f"Yalnızca LTspice'lı testlerin girdiği fonksiyonlar ({len(only)}):")
            print("\n".join(f"  {s}" for s in only[:MAX_LIST]))
        print(tail[0])
        return 1 if failed else 0

    rows = witness(cases, entered)
    for state, test, detail in rows:
        print(f"[{state:<7}] {test}  {detail}")
    partial = sum(1 for r in rows if r[0] == "KISMİ")
    skipped = sum(1 for r in rows if r[0] == "ATLANDI")
    print(f"{len(rows) - partial - skipped} tamam, {partial} kısmi, {skipped} atlandı | pytest: {tail[0]}")
    return 1 if failed or partial else 0


if __name__ == "__main__":
    sys.exit(main())
