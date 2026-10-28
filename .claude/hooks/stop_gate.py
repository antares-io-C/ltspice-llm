"""
Stop: Claude turu bitirmeden önce iki kontrol.

1. ``.asc`` bütünlüğü (``scripts/check.py`` ile aynı denetim). LTspice ``.asc``'yi cp1252 ya da UTF-16LE okur;
   Edit/Write araçları dosyayı UTF-8 sanıp ``µ``'yü U+FFFD yapar, UTF-8 ``µ`` LTspice'ta ``Âµ`` = 1 F okunur.
2. Python dosyaları son yeşil koşumdan beri değiştiyse kalite kapısı: ``python scripts/check.py --steps lint,tests``
   (ruff F/B + LTspice'sız testler, ~15 sn). İnsan ve CI aynı komutu çalıştırır. Arka planda batch LTspice
   çalışıyorsa atlanır: hem ölçümü bozmasın hem de çekirdek bütçesi aşılmasın.

Aynı bulgu (aynı dosya durumu) için en fazla bir kez engeller; durum ``.claude/hooks/.state/stop_gate.json``'da.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ltspice_procs import batch_processes, describe  # noqa: E402

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
sys.path.insert(0, str(ROOT / "scripts"))
from check import asc_problem, walk  # noqa: E402

STATE_DIR = ROOT / ".claude" / "hooks" / ".state"
STATE = STATE_DIR / "stop_gate.json"
PY_ROOTS = ["ltspice_llm", "tests", "examples", "scripts", ".claude/hooks"]
PY_FILES = ["interactive_designer.py", "pyproject.toml"]
GATE_TIMEOUT = 150
MAX_REASON = 6000


def py_fingerprint():
    h = hashlib.sha1()
    files = [p for r in PY_ROOTS for p in walk(ROOT / r, (".py", ".txt", ".db"))]
    files += [ROOT / f for f in PY_FILES if (ROOT / f).is_file()]
    for p in sorted(files):
        st = p.stat()
        h.update(f"{p.relative_to(ROOT).as_posix()}|{st.st_size}|{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")


def run_gate():
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    cmd = [sys.executable, str(ROOT / "scripts" / "check.py"), "--steps", "lint,tests"]
    return subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=GATE_TIMEOUT)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        data = json.load(sys.stdin)
    except ValueError:
        data = {}
    state = load_state()
    problems, notes = [], []

    reported = state.setdefault("asc_reported", {})
    for path in walk(ROOT, (".asc",)):
        rel = path.relative_to(ROOT).as_posix()
        issue = asc_problem(path)
        key = str(path.stat().st_mtime_ns)
        if issue and reported.get(rel) != key:
            reported[rel] = key
            problems.append(f"`{rel}`: {issue}. Dosyayı `AscSchematic` / `write_asc_file` ile yeniden yaz "
                            f"ya da yedekten geri al; değerleri `adapter.inspect_schematic()` ile kontrol et.")
        elif not issue:
            reported.pop(rel, None)

    fp = py_fingerprint()
    if fp != state.get("green"):
        batch = batch_processes()
        if batch:
            notes.append(f"Stop kapısı testleri atladı: arka planda batch LTspice çalışıyor ({describe(batch)}).")
        else:
            try:
                res = run_gate()
            except subprocess.TimeoutExpired:
                notes.append(f"Stop kapısı: `python scripts/check.py` {GATE_TIMEOUT} sn içinde bitmedi.")
            else:
                if res.returncode == 0:
                    state["green"] = fp
                    state.pop("failed", None)
                elif state.get("failed") == fp:
                    notes.append("Stop kapısı: kalite kapısı hâlâ kırık (aynı durum için bir kez uyarıldı).")
                else:
                    state["failed"] = fp
                    out = (res.stdout + res.stderr).strip()
                    problems.append("Kalite kapısı kırık (`python scripts/check.py --steps lint,tests`). Testi "
                                    "zayıflatma; düzeltme bir karar gerektiriyorsa açıkla ve dur:\n"
                                    + out[-MAX_REASON:])
    save_state(state)

    result = {}
    if problems and not data.get("stop_hook_active"):
        result["decision"] = "block"
        result["reason"] = "Bitirmeden önce düzelt:\n- " + "\n- ".join(problems)
        notes.insert(0, f"Stop kapısı {len(problems)} sorun buldu; Claude düzeltmeye devam ediyor.")
    elif problems:
        notes.extend(problems)
    if notes:
        result["systemMessage"] = "\n".join(n[:1500] for n in notes)
    if result:
        json.dump(result, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
