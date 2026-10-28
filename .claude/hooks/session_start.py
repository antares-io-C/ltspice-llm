"""
SessionStart (startup/resume): yalnızca söylenecek bir şey varsa bağlama not ekler, yoksa sessizdir.

- LTspice bulunamadı: LTspice gerektiren testler atlanır, simülasyonlar çalışmaz.
- LTspice.exe değişti (sürüm/boyut/tarih): GEMINI.md kuralı gereği referans kontrolleri ve ``.options threads``
  doğrulaması yeniden yapılmalı. Not hem Claude'a hem kullanıcıya bir kez gösterilir.
- Oturum başında çalışan batch LTspice: önceki bir koşumdan kalmış ve livelock'ta olabilir.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ltspice_procs import batch_processes, describe, file_version, find_ltspice_exe  # noqa: E402

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
STATE_DIR = ROOT / ".claude" / "hooks" / ".state"
STATE = STATE_DIR / "ltspice.json"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    context, user = [], []

    exe = find_ltspice_exe()
    if exe is None:
        context.append("LTspice bulunamadı (LTSPICE_EXE ya da bilinen kurulum yolları): LTspice gerektiren testler "
                       "atlanır, simülasyon çalışmaz.")
    else:
        st = exe.stat()
        current = {"exe": str(exe), "version": file_version(exe), "size": st.st_size, "mtime": int(st.st_mtime)}
        try:
            previous = json.loads(STATE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = None
        if previous != current:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            STATE.write_text(json.dumps(current, indent=1), encoding="utf-8")
        if previous and previous != current:
            change = f"{previous.get('version')} -> {current['version']}"
            context.append(f"LTspice.exe değişti ({change}, {exe}). Ölçülmüş davranışlar bozulmuş olabilir: "
                           "`python examples/12_ltspice_reference_checks.py` çalıştır ve `.options threads` "
                           "davranışını docs/LESSONS_LEARNED_PERFORMANCE.md'ye göre yeniden doğrula; farkları ilgili "
                           "ders belgesine yaz.")
            user.append(f"LTspice değişti ({change}): referans ve thread kontrollerinin yeniden çalıştırılması gerekiyor.")

    batch = batch_processes()
    if batch:
        msg = (f"Oturum başında çalışan batch LTspice var: {describe(batch)}. Önceki bir koşumdan kalmış ve "
               "livelock'ta olabilir; yeni LTspice işi başlatmadan önce kullanıcıya sor.")
        context.append(msg)
        user.append(msg)

    if not context:
        return 0
    out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n".join(context)}}
    if user:
        out["systemMessage"] = "\n".join(user)
    json.dump(out, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
