"""
Test izolasyonu (atomiklik) denetimi: ent-dch-sws-app'teki integration_isolation_check.py'nin karşılığı.

Bir test, aynı oturumda önceki testlerin bıraktığı durum (modül/sınıf düzeyi değişken, önbellek, kayıt) sayesinde
geçiyorsa geçerli değildir: dosya bölünür, sıra değişir ya da bir test silinirse kırılır.

1. Taban: seçilen testler birlikte, gate'in koştuğu gibi koşulur; her testin sonucu kaydedilir.
2. İzolasyon: her test kendi pytest sürecinde **tek başına** koşulur (gerçekten tek test koştuğu doğrulanır).
3. Karşılaştırma: sonucu değişen test atomik değildir (başka testin durumuna dayanıyor ya da hatası örtülüyor).

    python scripts/isolation.py                       # use case senaryolarına bağlı testler (tests/usecases)
    python scripts/isolation.py tests/test_cpu_guard.py tests/test_x.py::test_y
    python scripts/isolation.py --full                # LTspice'lı testler de (batch LTspice çalışırken kullanma)

Test başına bir süreç açıldığı için (~1-2 sn) bütün paket yerine seçili dosyalarda kullan. Atomik olmayan test
varsa 1 döner.
"""

import argparse
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import flowtrace  # noqa: E402


def junit_outcomes(xml_text):
    """``{düğüm kimliği: PASS|FAIL|SKIP}`` (pytest ``-o junit_family=xunit1`` çıktısından)."""
    outcomes = {}
    for case in ET.fromstring(xml_text).iter("testcase"):
        file = case.get("file", "").replace("\\", "/")
        module = file[:-3].replace("/", ".") if file.endswith(".py") else ""
        classname = case.get("classname", "")
        inner = classname[len(module) + 1:] if module and classname.startswith(module + ".") else ""
        nodeid = "::".join([file] + ([*inner.split(".")] if inner else []) + [case.get("name", "")])
        if case.find("failure") is not None or case.find("error") is not None:
            outcomes[nodeid] = "FAIL"
        elif case.find("skipped") is not None:
            outcomes[nodeid] = "SKIP"
        else:
            outcomes[nodeid] = "PASS"
    return outcomes


def compare(baseline, isolated):
    """Sonucu değişen testler: ``[(test, taban, tek başına)]``."""
    return [(t, baseline[t], isolated.get(t, "KOŞMADI")) for t in sorted(baseline) if isolated.get(t) != baseline[t]]


def run(targets, full, xml_path):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    if not full:
        env["LTSPICE_EXE"] = str(ROOT / "nonexistent-ltspice" / "LTspice.exe")
    cmd = [sys.executable, "-m", "pytest", *targets, "-q", "-p", "no:cacheprovider", "-p", "no:randomly",
           "-o", "junit_family=xunit1", f"--junitxml={xml_path}"]
    subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                   check=False)
    return junit_outcomes(Path(xml_path).read_text(encoding="utf-8")) if Path(xml_path).exists() else {}


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("targets", nargs="*", help="test dosyaları ya da düğüm kimlikleri")
    parser.add_argument("--full", action="store_true", help="LTspice gerektiren testleri de koş")
    opts = parser.parse_args(argv)
    targets = opts.targets or sorted({sc["test"] for uc in flowtrace.load_usecases() for sc in uc["scenarios"]})

    with tempfile.TemporaryDirectory() as tmp:
        baseline = run(targets, opts.full, Path(tmp) / "base.xml")
        if not baseline:
            print("[FAIL] taban koşumunda hiç test bulunamadı")
            return 1
        isolated, errors = {}, []
        for i, test in enumerate(sorted(baseline)):
            alone = run([test], opts.full, Path(tmp) / f"t{i}.xml")
            if len(alone) != 1:  # parametre/ad eşleşmesi başka testleri de sürüklediyse kontrol geçersiz
                errors.append(f"{test}: tek başına koşumda {len(alone)} test koştu")
            isolated.update({k: v for k, v in alone.items() if k == test})
    changed = compare(baseline, isolated)
    for test, before, after in changed:
        print(f"[ATOMİK DEĞİL] {test}: birlikte {before}, tek başına {after}")
    for line in errors:
        print(f"[HATA] {line}")
    counts = {s: sum(1 for v in baseline.values() if v == s) for s in ("PASS", "FAIL", "SKIP")}
    print(f"{len(baseline)} test ({counts}), {len(changed)} atomik değil, {len(errors)} denetlenemedi")
    return 1 if changed or errors else 0


if __name__ == "__main__":
    sys.exit(main())
