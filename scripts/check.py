"""
Tek kalite kapısı: insan, CI ve Claude Code Stop hook'u aynı komutu çalıştırır.

    python scripts/check.py                  # .asc bütünlüğü + ruff (F, B) + mypy + LTspice'sız testler (~45 sn)
    python scripts/check.py --full           # testler ve örnekler LTspice ile (~8 dk; kurulu değilse atlanır)
    python scripts/check.py --steps lint,tests --cov

``flows`` adımı use case senaryolarının coverage tanığıdır (``scripts/flows.py``; tracer ``tests`` adımında koşar).

Her adımı koşar (tek koşumda bütün sorunlar görünsün), bağlama sığan kısa bir özet basar ve bir adım
kırıksa 1 döner. Batch LTspice çalışırken ``--full`` kullanma (bkz. GEMINI.md "Performans ve eşzamanlılık").
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".hypothesis", ".venv", "venv", "build", "dist"}
MAX_LINES = 40  # kırık adım başına; ajanın bağlamına sığsın
NO_LTSPICE = ROOT / "nonexistent-ltspice" / "LTspice.exe"


def walk(base, suffixes):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        for name in filenames:
            if name.lower().endswith(suffixes):
                yield Path(dirpath) / name


def asc_problem(path):
    """
    LTspice ``.asc``'yi cp1252 ya da UTF-16LE okur. Edit/Write araçları dosyayı UTF-8 sanıp ``µ``'yü U+FFFD
    (EF BF BD) yapar; UTF-8 ile yazılan ``µ`` (C2 B5) LTspice'ta ``Âµ`` = 1 F okunur.
    """
    data = path.read_bytes()
    # UTF-16LE: BOM'lu ya da BOM'suz (encode_asc_text ve LTspice BOM'suz yazar; baştaki NUL baytları ele verir)
    bom = data.startswith(b"\xff\xfe")
    if bom or b"\x00" in data[:256]:
        text = data[2 if bom else 0:].decode("utf-16-le", errors="replace")
        if "�" in text:
            return "UTF-16 içinde U+FFFD"
        return "UTF-8 ile yazılmış µ (Âµ = 1 F)" if "Âµ" in text else None
    if data.startswith(b"\xef\xbb\xbf"):
        return "UTF-8 BOM (LTspice cp1252 bekler)"
    if b"\xef\xbf\xbd" in data:
        return "U+FFFD: µ gibi bir karakter Edit/Write ile bozulmuş"
    if b"\xc2\xb5" in data:
        return "UTF-8 ile yazılmış µ (LTspice Âµ = 1 F okur)"
    return None


def check_asc():
    paths = sorted(walk(ROOT, (".asc",)))
    lines = [f"{p.relative_to(ROOT).as_posix()}: {issue}. `AscSchematic` / `write_asc_file` ile yeniden yaz ya da "
             "yedekten geri al." for p in paths if (issue := asc_problem(p))]
    return (1, "\n".join(lines)) if lines else (0, f"{len(paths)} .asc dosyası temiz")


def run(args, env=None):
    proc = subprocess.run([sys.executable, "-m", *args], cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env, check=False)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def check_lint():
    code, out = run(["ruff", "check", "--output-format", "concise", "--quiet", "."])
    if "No module named ruff" in out:
        out += "\nKur: python -m pip install -e .[dev]"
    return code, out or "ruff: temiz"


def check_types():
    """
    mypy paketi iki platformun gözüyle denetler (Windows, Linux): platforma özgü dallar (msvcrt/fcntl, iş nesnesi)
    ``sys.platform`` denetimiyle ayrılmazsa biri kırık kalır. Paket ``py.typed`` taşıdığı için tip hatası kullanıcıya
    ulaşır (inceleme 2026-10-11: kapıda tip denetimi yoktu, mypy 24 hata veriyordu).
    """
    code, parts = 0, []
    for platform in ("win32", "linux"):
        rc, out = run(["mypy", "ltspice_llm", "--platform", platform])
        if "No module named mypy" in out:
            out += "\nKur: python -m pip install -e .[dev]"
        code = code or rc
        parts.append(f"{platform}: {out.splitlines()[-1] if out else ''}" if rc == 0 else f"{platform}:\n{out}")
    return code, "\n".join(parts)


def check_tests(full=False, cov=False):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    if not full:
        env["LTSPICE_EXE"] = str(NO_LTSPICE)  # LTspice gerektiren testler kendiliğinden atlanır
    args = ["pytest", "tests", "-q", "--tb=short", "-p", "no:cacheprovider"]
    if cov:
        args += ["--cov=ltspice_llm", "--cov-report=term-missing:skip-covered"]
    code, out = run(args, env)
    if code == 5:  # pytest "Exit codes": hiç test toplanmadı (testpaths ya da conftest bozuk); kapı geçmez
        return code, f"{out}\npytest hiç test toplamadı (çıkış kodu 5): testpaths ve conftest'i kontrol edin."
    return code, out


def check_flows(full=False):
    cmd = [sys.executable, str(ROOT / "scripts" / "flows.py")] + (["--full"] if full else [])
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          env=dict(os.environ, PYTHONIOENCODING="utf-8"), check=False)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def tail(text, n):
    lines = text.splitlines()
    if len(lines) <= n:
        return text
    return f"... ({len(lines) - n} satır atlandı)\n" + "\n".join(lines[-n:])


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--steps", default="asc,lint,types,tests,flows", help="virgülle: asc, lint, types, tests, flows")
    parser.add_argument("--full", action="store_true", help="testleri LTspice ile koş")
    parser.add_argument("--cov", action="store_true", help="testlerde branch coverage raporu")
    opts = parser.parse_args(argv)

    steps = {"asc": check_asc, "lint": check_lint, "types": check_types, "tests": lambda: check_tests(opts.full, opts.cov),
             "flows": lambda: check_flows(opts.full)}
    wanted = [s.strip() for s in opts.steps.split(",") if s.strip()]
    unknown = [s for s in wanted if s not in steps]
    if unknown:
        parser.error(f"bilinmeyen adım: {', '.join(unknown)}")

    failed = []
    for name in wanted:
        code, output = steps[name]()
        if code == 0:
            lines = output.splitlines()
            summary = [ln for ln in lines if ln.startswith("TOTAL")] + lines[-1:]  # --cov: toplam satırı
            print(f"[ok]   {name:<6} {' | '.join(summary)}")
        else:
            failed.append(name)
            print(f"[FAIL] {name}")
            print(tail(output, MAX_LINES))
    print("PASSED" if not failed else f"FAILED: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
