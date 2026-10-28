"""
PreToolUse (Edit|Write): mevcut bir testi zayıflatan düzenlemede kullanıcıya sorar.

Baskı altındaki ajanlar kırık testi kodu değil testi değiştirerek "düzeltir" (ImpossibleBench, ICLR 2026; katı
yönerge ve testi değiştirmenin önüne geçmek oranı belirgin düşürdü). Üç yaygın biçim yakalanır: bir test
fonksiyonu kaybolur, skip/xfail eklenir ya da assert sayısı azalır. Engellemez, "ask" döner: kullanıcı onaylar
(otomatik kipte de sorulur). Yeni test dosyaları ve yalnızca ekleme yapan düzenlemeler sessizce geçer.

Use case manifestleri (``tests/usecases/*.json``) de korunur: senaryo/eylem silme, kanıtı ``none`` yapma, dizi
kısaltma, ``strict``'ten çıkma, ``per_thread``/``forbidden``/``counts``/``evidence`` kaldırma testi zayıflatmakla
aynıdır (bkz. GEMINI.md "Use case akışı").

LTspice gerektiren testlerdeki ``pytest.skip("LTspice bu makinede kurulu değil")`` meşrudur; yeni bir tane
eklenirse de sorulur, kullanıcı onaylar.
"""

import json
import os
import re
import sys
from pathlib import Path

TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)", re.MULTILINE)
SKIP = re.compile(r"\bpytest\.(?:skip|xfail|importorskip)\b|\bmark\.(?:skip|skipif|xfail)\b")
CHECK = re.compile(r"\bassert\b|\bpytest\.raises\b|\bpytest\.warns\b")


def is_test_file(path, project):
    if path.suffix != ".py" or not path.is_relative_to(project):
        return False
    return path.name.startswith("test_") or "tests" in path.relative_to(project).parts


def is_manifest(path, project):
    if path.suffix != ".json" or not path.is_relative_to(project):
        return False
    return path.relative_to(project).parts[:2] == ("tests", "usecases")


def _manifest(text):
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def manifest_weakening(before, after):
    old, new = _manifest(before), _manifest(after)
    if old is None or new is None:
        return []  # bozuk JSON'u manifest doğrulama testi yakalar
    findings = []
    old_actions = {a.get("id"): a for a in old.get("actions", [])}
    new_actions = {a.get("id"): a for a in new.get("actions", [])}
    gone = sorted(map(str, set(old_actions) - set(new_actions)))
    if gone:
        findings.append(f"eylem(ler)i siliyor: {', '.join(gone)}")
    findings += [f"{aid} kanıtını 'none' yapıyor" for aid in sorted(map(str, set(old_actions) & set(new_actions)))
                 if old_actions[aid].get("proof") != "none" and new_actions[aid].get("proof") == "none"]
    old_sc = {s.get("test"): s for s in old.get("scenarios", [])}
    new_sc = {s.get("test"): s for s in new.get("scenarios", [])}
    gone = sorted(map(str, set(old_sc) - set(new_sc)))
    if gone:
        findings.append(f"senaryo(ları) siliyor: {', '.join(gone)}")
    for test in sorted(map(str, set(old_sc) & set(new_sc))):
        o, n, name = old_sc[test], new_sc[test], test.rsplit("::", 1)[-1]
        if len(n.get("sequence") or []) < len(o.get("sequence") or []):
            findings.append(f"{name}: sequence kısalıyor")
        if o.get("mode") == "strict" and n.get("mode") != "strict":
            findings.append(f"{name}: strict'ten çıkıyor")
        if o.get("per_thread") and not n.get("per_thread"):
            findings.append(f"{name}: per_thread kaldırılıyor")
        for key in ("forbidden", "counts", "evidence", "actions"):
            lost = [k for k in (o.get(key) or []) if k not in (n.get(key) or [])]
            if lost:
                findings.append(f"{name}: {key} azalıyor ({', '.join(map(str, lost))})")
        changed = [k for k, v in (o.get("counts") or {}).items() if k in (n.get("counts") or {}) and n["counts"][k] != v]
        if changed:
            findings.append(f"{name}: counts değişiyor ({', '.join(changed)})")
    return findings


def proposed_content(event, current):
    tool_input = event.get("tool_input") or {}
    if event.get("tool_name") == "Write":
        return tool_input.get("content", "")
    old, new = tool_input.get("old_string", ""), tool_input.get("new_string", "")
    if not old:
        return current
    return current.replace(old, new) if tool_input.get("replace_all") else current.replace(old, new, 1)


def weakening(before, after):
    findings = []
    removed = sorted(set(TEST_DEF.findall(before)) - set(TEST_DEF.findall(after)))
    if removed:
        findings.append(f"test(ler)i siliyor ya da yeniden adlandırıyor: {', '.join(removed)}")
    if len(SKIP.findall(after)) > len(SKIP.findall(before)):
        findings.append("skip/xfail ekliyor")
    checks_before, checks_after = len(CHECK.findall(before)), len(CHECK.findall(after))
    if checks_after < checks_before:
        findings.append(f"assert sayısını {checks_before}'dan {checks_after}'a düşürüyor")
    return findings


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        event = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except ValueError:
        return 0
    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or ".").resolve()
    path = Path((event.get("tool_input") or {}).get("file_path", "")).resolve()
    if not path.is_file():
        return 0
    if is_manifest(path, project):
        check = manifest_weakening
    elif is_test_file(path, project):
        check = weakening
    else:
        return 0

    before = path.read_text(encoding="utf-8", errors="replace")
    findings = check(before, proposed_content(event, before))
    if not findings:
        return 0

    summary = "; ".join(findings)
    json.dump({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "ask",
        "permissionDecisionReason": f"Test koruması: {path.name} düzenlemesi {summary}. Yalnızca bilerek yapıldıysa onayla.",
        "additionalContext": (
            f"Test koruması bu düzenlemeyi ({summary}) kullanıcı onayına sundu. Kırık bir testi geçirmeye "
            "çalışıyorsan testi değil kodu düzelt; test yanlışsa nedenini kaynağıyla (datasheet, ADI referansı, "
            "LTspice ölçümü) kullanıcıya açıkla ve dur."
        ),
    }}, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
