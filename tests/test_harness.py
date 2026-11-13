"""
Ajan koşum düzeninin (harness) testleri: tek kalite kapısı (``scripts/check.py``) ve Claude Code hook'ları
(``.claude/hooks/``). Hook'lar Claude Code'un verdiği JSON'u stdin'den okur; burada aynı JSON ile çağrılır.
"""

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _load("check_gate", ROOT / "scripts" / "check.py")
guard = _load("guard_tests", ROOT / ".claude" / "hooks" / "guard_tests.py")
sys.path.insert(0, str(ROOT / ".claude" / "hooks"))  # pre_ltspice_guard, ltspice_procs'u içe aktarır
ltspice_guard = _load("pre_ltspice_guard", ROOT / ".claude" / "hooks" / "pre_ltspice_guard.py")


@pytest.mark.parametrize("command,runs_ltspice_tests", [
    ("python -m pytest tests -q", True),
    ("python scripts/check.py --full", True),
    ("python scripts/check.py --steps tests --full --cov", True),
    ("python scripts/check.py", False),          # LTSPICE_EXE'yi kendisi kapatır
    ("python scripts/check.py --steps lint", False),
    ("python scripts/flows.py --full", True),     # coverage tanığı LTspice'lı senaryoları da koşar
    ("python scripts/flows.py", False),           # LTSPICE_EXE'yi kendisi kapatır
    ("python examples/12_ltspice_reference_checks.py", False),  # pytest değil; USES_LTSPICE ile ayrıca bakılır
    ("python scripts/isolation.py --full", True),  # her testi ayrı pytest sürecinde, LTspice'lı testler dahil
    ("python scripts/isolation.py tests/test_report.py", False),
    ("pytest tests -q", True),
    ("py -3 -X utf8 -m pytest tests", True),      # python seçenekleri -m'den önce
    ("cd tests && python -m pytest test_report.py", True),
    ("git log --grep pytest && python -m pytest tests", True),
    # pytest sözcüğü komutta ama çalışan program pytest değil (eskiden hepsi engelleniyordu)
    ("python -m pip install pytest", False),
    ("python -c \"print('pytest')\"", False),
    ("python scripts/mutants.py pytest", False),
    ("python .scratch/ozet.py --cikti pytest.txt", False),
    ("git commit -m \"hook; pytest sözcüğü artık engellemiyor\"", False),   # tırnak içindeki ; komutu bölmez
    # LTspice'sız alt küme: LTSPICE_EXE var olmayan bir dosyayı gösterir
    ("LTSPICE_EXE=C:/yok/x.exe python -m pytest tests -q", False),
    ("export LTSPICE_EXE=/yok/x.exe && python -m pytest tests", False),
    ("python -m pytest tests -k LTSPICE_EXE", True),        # sözcüğün geçmesi atama değil
    ("LTSPICE_EXE=/yok/x.exe; python -m pytest tests", True),  # export'suz atama alt sürece geçmez (bash)
    ("LTSPICE_EXE=C:/yok/x.exe python -m pytest a; python -m pytest b", True),  # atama yalnız ilk komutta
    (f'LTSPICE_EXE="{sys.executable}" python -m pytest tests', True),  # var olan dosya: LTspice'lı koşum
])
def test_ltspice_guard_treats_full_gate_like_pytest(command, runs_ltspice_tests):
    assert ltspice_guard.runs_ltspice_tests(command) is runs_ltspice_tests


@pytest.mark.parametrize("shell,command,runs_ltspice_tests", [
    # atama kabuğun kuralıyla: başka kabuğun sözdizimi atama değildir, pytest LTspice'la koşar (test-reviewer ölçtü)
    ("PowerShell", "$env:LTSPICE_EXE='C:\\yok\\x.exe'; python -m pytest tests", False),
    ("PowerShell", "$env:LTSPICE_EXE = 'C:\\yok\\x.exe'; python -m pytest tests", False),
    ("Bash", "$env:LTSPICE_EXE='C:\\yok\\x.exe'; python -m pytest tests", True),   # bash'te $env boş genişler
    ("Bash", "set LTSPICE_EXE=C:/yok/x.exe && python -m pytest tests", True),      # set ortam değişkeni değil
    ("PowerShell", "set LTSPICE_EXE=C:/yok/x.exe; python -m pytest tests", True),
    ("PowerShell", "export LTSPICE_EXE=/yok/x.exe; python -m pytest tests", True),  # PowerShell'de export yok
    # eski sözcük araması bunları yakalıyordu; yeni ayrıştırma da yakalamalı
    ("Bash", "env python -m pytest tests", True),
    ("Bash", "env LTSPICE_EXE=/yok/x.exe python -m pytest tests", False),
    ("Bash", "timeout 600 python -m pytest tests -q", True),
    ("Bash", "bash -c 'python -m pytest tests'", True),
    ("Bash", "LTSPICE_EXE=/yok/x.exe bash -c 'python -m pytest tests'", False),
    ("PowerShell", "pwsh -Command \"python -m pytest tests\"", True),
    ("Bash", "python -m coverage run -m pytest tests", True),
    ("Bash", "python -mpytest tests", True),
    ("Bash", "C:/Python314/python.exe -m pytest tests", True),
    ("Bash", "cd tests\npython -m pytest test_report.py", True),
])
def test_ltspice_guard_reads_commands_like_the_shell_does(shell, command, runs_ltspice_tests):
    assert ltspice_guard.runs_ltspice_tests(command, shell) is runs_ltspice_tests


@pytest.mark.parametrize("command,batch,expected", [
    ("python -m pytest tests -q", "", None),                                  # batch LTspice yok
    ("python -m pytest tests -q", "other", "deny"),                           # bütçe paylaşılmaz -> livelock
    ("LTSPICE_EXE=C:/yok/x.exe python -m pytest tests -q", "other", None),    # LTspice'sız alt küme
    ("python scripts/check.py --full", "other", "deny"),
    ("python scripts/check.py", "other", None),                               # LTSPICE_EXE'yi kendisi kapatır
    ("python examples/12_ltspice_reference_checks.py", "other", None),       # cpu_guard'lı betikler birbirini bekler
    ("python examples/12_ltspice_reference_checks.py", "pytest", "deny"),    # pytest'in bütçesini görmez
    ("LTSPICE_LLM_GUARD=off python -m pytest tests", "other", None),          # kullanıcı onaylı kaçış
    ("python -m pip install pytest", "other", None),                          # pytest sözcüğü komut değil
], ids=["bos", "pytest", "pytest-ltspicesiz", "kapi-full", "kapi", "ornek", "ornek-pytest-varken", "kacis",
        "pip-pytest-sozcugu"])
def test_ltspice_guard_decision(monkeypatch, capsys, command, batch, expected):
    procs = {"": [], "other": ["batch"], "pytest": ["pytest-batch"]}[batch]
    monkeypatch.setattr(ltspice_guard, "batch_processes", lambda: procs)
    monkeypatch.setattr(ltspice_guard, "owned_by_pytest", lambda p: p == "pytest-batch")
    monkeypatch.setattr(ltspice_guard, "describe", lambda ps: ", ".join(ps))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_input": {"command": command}})))
    assert ltspice_guard.main() == 0
    out = capsys.readouterr().out
    decision = json.loads(out)["hookSpecificOutput"]["permissionDecision"] if out.strip() else None
    assert decision == expected


# --------------------------------------------------------------------------- .asc bütünlüğü
@pytest.mark.parametrize("data,problem", [
    ("Version 4\r\nSYMATTR Value 1µ\r\n".encode("cp1252"), None),
    ("Version 4\r\nSYMATTR Value 1µ\r\n".encode("utf-16-le"), None),
    (b"\xff\xfe" + "Version 4\r\nSYMATTR Value 1µ\r\n".encode("utf-16-le"), None),
    ("Version 4\r\nSYMATTR Value 1µ\r\n".encode("utf-8"), "UTF-8 ile yazılmış µ"),
    (b"\xef\xbb\xbfVersion 4\r\n", "UTF-8 BOM"),
    ("Version 4\r\nSYMATTR Value 1\ufffd\r\n".encode("utf-8"), "U+FFFD"),
    (b"\xff\xfe" + "Version 4\r\nSYMATTR Value 1Âµ\r\n".encode("utf-16-le"), "Âµ"),
    (b"\xff\xfe" + "Version 4\r\n1\ufffd\r\n".encode("utf-16-le"), "U+FFFD"),
    # encode_asc_text UTF-16'y\u0131 BOM'suz yazar (LTspice'\u0131n kendi bi\u00e7imi); bozulma orada da yakalanmal\u0131
    ("Version 4\r\nSYMATTR Value 1\ufffd\r\n".encode("utf-16-le"), "U+FFFD"),
    ("Version 4\r\nSYMATTR Value 1\u00c2\u00b5\r\n".encode("utf-16-le"), "\u00c2\u00b5"),
], ids=["cp1252", "utf16-bomsuz", "utf16-bomlu", "utf8-mu", "utf8-bom", "edit-araci-fffd", "utf16-mojibake",
        "utf16-fffd", "utf16-bomsuz-fffd", "utf16-bomsuz-mojibake"])
def test_asc_problem(tmp_path, data, problem):
    path = tmp_path / "x.asc"
    path.write_bytes(data)
    found = check.asc_problem(path)
    assert (found is None) if problem is None else (problem in found)


def test_gate_fails_on_corrupt_asc_and_passes_on_clean(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(check, "ROOT", tmp_path)
    (tmp_path / "ok.asc").write_bytes("Version 4\r\nSYMATTR Value 1µ\r\n".encode("cp1252"))
    assert check.main(["--steps", "asc"]) == 0
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "bad.asc").write_bytes("Version 4\r\nSYMATTR Value 1µ\r\n".encode("utf-8"))
    assert check.main(["--steps", "asc"]) == 1
    out = capsys.readouterr().out
    assert "sub/bad.asc" in out and "FAILED: asc" in out


def test_gate_rejects_unknown_step():
    with pytest.raises(SystemExit):
        check.main(["--steps", "asc,typo"])


def test_type_step_checks_both_platforms_and_fails_on_either(monkeypatch, capsys):
    """Paket py.typed taşır; platforma özgü dallar (msvcrt/fcntl, iş nesnesi) yalnız bir platformda denetlenirse öbürü
    kırık kalır. Eskiden kapıda tip denetimi yoktu, mypy 24 hata veriyordu (inceleme 2026-10-11)."""
    calls = []

    def fake_run(args, env=None):
        calls.append(args)
        broken = args[-1] == "linux"
        return (1, "x.py:1: error: Module has no attribute \"flock\"") if broken else (0, "Success: no issues found")

    monkeypatch.setattr(check, "run", fake_run)
    assert check.main(["--steps", "types"]) == 1
    assert [c[0] for c in calls] == ["mypy", "mypy"] and {c[-1] for c in calls} == {"win32", "linux"}
    out = capsys.readouterr().out
    assert out.count("linux:") == 1 and "flock" in out and "FAILED: types" in out


def test_gate_fails_when_pytest_collects_no_tests(monkeypatch):
    """pytest belgesi 'Exit codes': 5 = "No tests were collected". Eski kapı 5'i başarı sayıyordu: testpaths ya da
    conftest bozulunca kapı sıfır testle PASSED derdi (inceleme 2026-10-11)."""
    monkeypatch.setattr(check, "run", lambda args, env=None: (5, "no tests ran in 0.01s"))
    code, out = check.check_tests()
    assert code == 5 and "hiç test toplamadı" in out


# --------------------------------------------------------------------------- test koruması
TESTS = '''
def test_a():
    assert f(1) == 2
    assert f(2) == 3


def test_b():
    with pytest.raises(ValueError):
        f(-1)
'''


@pytest.mark.parametrize("after,finding", [
    (TESTS.replace("def test_b", "def test_c"), "test_b"),
    (TESTS.replace("    assert f(2) == 3\n", ""), "3'dan 2'a"),
    (TESTS.replace("def test_a", "@pytest.mark.skip\ndef test_a"), "skip/xfail"),
    (TESTS.replace("assert f(1)", "pytest.xfail('x')\n    assert f(1)"), "skip/xfail"),
    (TESTS.replace("with pytest.raises(ValueError):\n        f(-1)", "f(-1)"), "3'dan 2'a"),
], ids=["silinen-test", "azalan-assert", "skip-marker", "xfail-cagrisi", "kaldirilan-raises"])
def test_guard_flags_weakening(after, finding):
    assert any(finding in f for f in guard.weakening(TESTS, after))


@pytest.mark.parametrize("after", [
    TESTS,
    TESTS + "\n\ndef test_new():\n    assert f(3) == 4\n",
    TESTS.replace("assert f(1) == 2", "assert f(1) == pytest.approx(2, rel=1e-12)"),
], ids=["degisiklik-yok", "yeni-test", "assert-degisti-sayi-ayni"])
def test_guard_allows_additions(after):
    assert guard.weakening(TESTS, after) == []


def _run_guard(event, project):
    proc = subprocess.run([sys.executable, str(ROOT / ".claude" / "hooks" / "guard_tests.py")],
                          input=json.dumps(event).encode("utf-8"), capture_output=True,
                          env={**os.environ, "CLAUDE_PROJECT_DIR": str(project)},
                          timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def test_guard_hook_asks_before_an_edit_removes_an_assert(tmp_path):
    test_file = tmp_path / "tests" / "test_x.py"
    test_file.parent.mkdir()
    test_file.write_text(TESTS, encoding="utf-8")
    event = {"tool_name": "Edit", "tool_input": {
        "file_path": str(test_file), "old_string": "    assert f(2) == 3\n", "new_string": ""}}
    out = _run_guard(event, tmp_path)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask"
    assert "test_x.py" in out["permissionDecisionReason"]
    assert "kodu düzelt" in out["additionalContext"]


def test_guard_hook_is_silent_for_source_files_and_new_tests(tmp_path):
    src = tmp_path / "pkg" / "mod.py"
    src.parent.mkdir()
    src.write_text("assert True\n", encoding="utf-8")
    edit_src = {"tool_name": "Edit", "tool_input": {"file_path": str(src), "old_string": "assert True\n",
                                                    "new_string": ""}}
    new_test = {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "tests" / "test_new.py"),
                                                     "content": ""}}
    assert _run_guard(edit_src, tmp_path) is None
    assert _run_guard(new_test, tmp_path) is None


def test_guard_write_that_drops_a_test_is_flagged(tmp_path):
    test_file = tmp_path / "test_y.py"
    test_file.write_text(TESTS, encoding="utf-8")
    event = {"tool_name": "Write", "tool_input": {"file_path": str(test_file),
                                                  "content": TESTS.split("def test_b")[0]}}
    assert _run_guard(event, tmp_path)["hookSpecificOutput"]["permissionDecision"] == "ask"


# ------------------------------------------------------------------- use case manifesti koruması
MANIFEST = {
    "use_case": "u", "source": ["README.md"],
    "actions": [{"id": "A1", "proof": "trace", "symbol": "s1"}, {"id": "A2", "proof": "boundary"}],
    "scenarios": [{"test": "tests/test_x.py::test_a", "flow": "Basic", "actions": ["A1", "A2"],
                   "sequence": ["s1", "s2"], "mode": "strict", "per_thread": True, "forbidden": ["s3"],
                   "counts": {"s1": 2}, "evidence": {"A2": "assert x"}}],
}


def _changed(**scenario):
    data = json.loads(json.dumps(MANIFEST))
    for key, value in scenario.items():
        if value is None:
            data["scenarios"][0].pop(key)
        else:
            data["scenarios"][0][key] = value
    return json.dumps(data)


@pytest.mark.parametrize("after, finding", [
    (json.dumps({**MANIFEST, "scenarios": []}), "senaryo(ları) siliyor"),
    (json.dumps({**MANIFEST, "actions": MANIFEST["actions"][:1]}), "eylem(ler)i siliyor: A2"),
    (json.dumps({**MANIFEST, "actions": [MANIFEST["actions"][0], {"id": "A2", "proof": "none"}]}), "A2 kanıtını 'none'"),
    (_changed(sequence=["s1"]), "sequence kısalıyor"),
    (_changed(mode="subsequence"), "strict'ten çıkıyor"),
    (_changed(mode=None), "strict'ten çıkıyor"),
    (_changed(per_thread=None), "per_thread kaldırılıyor"),
    (_changed(forbidden=[]), "forbidden azalıyor"),
    (_changed(counts={}), "counts azalıyor"),
    (_changed(counts={"s1": 1}), "counts değişiyor"),
    (_changed(evidence={}), "evidence azalıyor"),
    (_changed(actions=["A1"]), "actions azalıyor"),
])
def test_guard_flags_manifest_weakening(after, finding):
    assert any(finding in f for f in guard.manifest_weakening(json.dumps(MANIFEST), after))


@pytest.mark.parametrize("after", [
    json.dumps(MANIFEST),
    _changed(sequence=["s1", "s2", "s4"], forbidden=["s3", "s5"]),
    "{bozuk",
], ids=["degisiklik-yok", "ekleme", "bozuk-json"])
def test_guard_allows_manifest_additions(after):
    assert guard.manifest_weakening(json.dumps(MANIFEST), after) == []


def test_guard_hook_asks_before_a_manifest_scenario_is_dropped(tmp_path):
    manifest = tmp_path / "tests" / "usecases" / "u.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps(MANIFEST), encoding="utf-8")
    event = {"tool_name": "Write", "tool_input": {"file_path": str(manifest),
                                                  "content": json.dumps({**MANIFEST, "scenarios": []})}}
    out = _run_guard(event, tmp_path)["hookSpecificOutput"]
    assert out["permissionDecision"] == "ask" and "senaryo" in out["permissionDecisionReason"]


# ------------------------------------------- Antigravity (.agents/hooks.json, .agents/agents/) ve ortak korumalar
# Antigravity girdi ve çıktıları Antigravity'nin hooks belgesinden yazıldı, gerçek bir Antigravity oturumundan kaydedilmedi;
# belgenin yerel kopyası depoda yok (2026-11-11). Gerçek yükler kaydedilince tests/data'ya konmalı (ref26_* kayıtları gibi).
session_start = _load("session_start", ROOT / ".claude" / "hooks" / "session_start.py")
stop_gate = _load("stop_gate", ROOT / ".claude" / "hooks" / "stop_gate.py")


def test_antigravity_hooks_json_wires_all_four_hooks_from_agents_working_dir():
    """Kaynak: Antigravity hooks belgesi ('The working directory is set to the directory containing hooks.json').
    .agents/hooks.json içindeki her komut .agents/ klasöründen bakıldığında var olan .claude/hooks/<betik>.py'yi göstermeli."""
    hooks_json = ROOT / ".agents" / "hooks.json"
    assert hooks_json.is_file()
    data = json.loads(hooks_json.read_text(encoding="utf-8"))
    spec = data["ltspice-harness"]
    commands = [h["command"] for h in spec["SessionStart"] + spec["Stop"]]
    commands += [h["command"] for group in spec["PreToolUse"] for h in group["hooks"]]
    scripts = set()
    for cmd in commands:
        assert cmd.startswith("python ")
        rel = cmd.split(" ", 1)[1].strip('"')
        target = (hooks_json.parent / rel).resolve()
        assert target.is_file(), (cmd, target)
        scripts.add(target.name)
    assert scripts == {"session_start.py", "pre_ltspice_guard.py", "guard_tests.py", "stop_gate.py"}
    matchers = {g["matcher"] for g in spec["PreToolUse"]}
    assert matchers == {"run_command", "edit_file|write_file"}


@pytest.mark.parametrize("command,batch,expected", [
    ("python -m pytest tests -q", "", None),
    ("python -m pytest tests -q", "other", "deny"),
    ("$env:LTSPICE_EXE='C:\\yok\\x.exe'; python -m pytest tests -q", "other", None),
    ("export LTSPICE_EXE=/yok/x.exe; python -m pytest tests -q", "other", "deny"),  # Windows PowerShell'de export yok
    ("python examples/12_ltspice_reference_checks.py", "pytest", "deny"),
    ("git clean -fd", "", "deny"),
    ("git -C sub clean -fdx", "", "deny"),
    ("git reset --hard HEAD~1", "", "deny"),
    ("cmd /c \"git reset --hard\"", "", "deny"),
    ("git reset HEAD a.py", "", None),
    ("git commit -m \"git clean ve git reset --hard engellendi\"", "", None),
], ids=["bos", "pytest-deny", "powershell-ltspicesiz", "yanlis-kabuk-export", "ornek-pytest-varken",
        "git-clean", "git-c-clean", "git-reset-hard", "cmd-c-reset-hard", "git-reset-soft", "commit-mesaji"])
def test_antigravity_pre_ltspice_guard_and_git_deny(monkeypatch, capsys, command, batch, expected):
    """Kaynak: Antigravity hooks belgesi PreToolUse sözleşmesi (toolCall.name='run_command', args.CommandLine;
    çıktı: {'decision': 'deny', 'reason': ...}) ve .claude/settings.json permissions.deny (git clean, git reset --hard).
    Sorun yoksa çıktı boştur: 'allow' aracı kullanıcı onayı olmadan otomatik çalıştırırdı (aynı belge)."""
    procs = {"": [], "other": ["batch"], "pytest": ["pytest-batch"]}[batch]
    monkeypatch.setattr(ltspice_guard, "batch_processes", lambda: procs)
    monkeypatch.setattr(ltspice_guard, "owned_by_pytest", lambda p: p == "pytest-batch")
    monkeypatch.setattr(ltspice_guard, "describe", lambda ps: ", ".join(ps))
    monkeypatch.setattr(sys, "platform", "win32")
    payload = {"conversationId": "c1", "workspacePaths": [str(ROOT)],
               "toolCall": {"name": "run_command", "args": {"CommandLine": command}}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert ltspice_guard.main() == 0
    raw = capsys.readouterr().out
    if expected is None:
        assert raw == ""
        return
    out = json.loads(raw)
    assert out["decision"] == expected
    assert out.get("reason")


def test_guard_tests_handles_antigravity_edit_and_write_from_agents_cwd(tmp_path):
    """Kaynak: Antigravity hooks belgesi PreToolUse sözleşmesi ve edit_file/write_file araç şeması.
    hooks.json çalışma dizini .agents/ iken CLAUDE_PROJECT_DIR olmadan workspacePaths ile proje kökünü bulmalı."""
    agents_dir = tmp_path / ".agents"
    agents_dir.mkdir()
    test_file = tmp_path / "tests" / "test_x.py"
    test_file.parent.mkdir()
    test_file.write_text(TESTS, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}

    def run_agy(tool_name, **args):
        event = {"conversationId": "c1", "workspacePaths": [str(tmp_path)],
                 "toolCall": {"name": tool_name, "args": args}}
        proc = subprocess.run([sys.executable, str(ROOT / ".claude" / "hooks" / "guard_tests.py")],
                              input=json.dumps(event).encode("utf-8"), capture_output=True,
                              cwd=agents_dir, env=env, timeout=30)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout) if proc.stdout.strip() else None

    # edit_file ile assert silme -> ask
    res = run_agy("edit_file", Path=str(test_file), Target="    assert f(2) == 3\n", Replacement="")
    assert res["decision"] == "ask" and "test_x.py" in res["reason"] and "kodu düzelt" in res["reason"]

    # edit_file tüm eşleşmeleri değiştirir (araç kuralı #3): iki assert tek Target ile silinirse 3 -> 1 düşüş yakalanır
    dup_file = tmp_path / "tests" / "test_dup.py"
    dup_file.write_text("def test_a():\n    assert f(1) == 2\n    assert f(1) == 2\n    assert f(2) == 3\n", encoding="utf-8")
    res_dup = run_agy("edit_file", Path=str(dup_file), Target="    assert f(1) == 2\n", Replacement="")
    assert res_dup["decision"] == "ask" and "3'dan 1'a" in res_dup["reason"]

    # write_file Append=True ile yeni test ekleme -> çıktı yok (karar kullanıcının izin akışına kalır)
    res_append = run_agy("write_file", Path=str(test_file), Content="\ndef test_c():\n    assert f(3) == 4\n", Append=True)
    assert res_append is None

    # write_file Append=False ile mevcut testi ezme -> ask
    res_overwrite = run_agy("write_file", Path=str(test_file), Content="\ndef test_c():\n    assert f(3) == 4\n", Overwrite=True)
    assert res_overwrite["decision"] == "ask" and "test_a" in res_overwrite["reason"]


def test_guard_tests_denies_asc_edit_and_write_in_both_agents(tmp_path):
    """Kaynak: .claude/settings.json permissions.deny ('Edit(**/*.asc)', 'Write(**/*.asc)') ve GEMINI.md ('UTF-8 1µ -> 1 F').
    Var olmayan yeni bir .asc dosyası oluşturulurken de, var olan düzenlenirken de reddedilmelidir."""
    new_asc = tmp_path / "devre.asc"
    claude_write = {"tool_name": "Write", "tool_input": {"file_path": str(new_asc), "content": "Version 4\n"}}
    out_claude = _run_guard(claude_write, tmp_path)["hookSpecificOutput"]
    assert out_claude["permissionDecision"] == "deny" and "AscSchematic" in out_claude["permissionDecisionReason"]

    agy_edit = {"conversationId": "c1", "workspacePaths": [str(tmp_path)],
                "toolCall": {"name": "edit_file", "args": {"Path": str(new_asc), "Target": "a", "Replacement": "b"}}}
    out_agy = _run_guard(agy_edit, tmp_path)
    assert out_agy["decision"] == "deny" and "AscSchematic" in out_agy["reason"]


def test_session_start_supports_antigravity_and_claude_contracts(tmp_path, monkeypatch, capsys):
    """Kaynak: Antigravity hooks belgesi SessionStart sözleşmesi (injectSteps -> systemMessage)."""
    monkeypatch.setattr(session_start, "STATE_DIR", tmp_path)
    monkeypatch.setattr(session_start, "STATE", tmp_path / "ltspice.json")
    fake_exe = tmp_path / "LTspice.exe"
    fake_exe.write_bytes(b"exe")
    monkeypatch.setattr(session_start, "find_ltspice_exe", lambda: fake_exe)
    monkeypatch.setattr(session_start, "file_version", lambda p: "26.1.1")
    monkeypatch.setattr(session_start, "batch_processes", lambda: [])

    # İlk çalışmada sorun yok -> Antigravity'de {}, Claude'da boş çıktı
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"conversationId": "c1", "workspacePaths": [str(tmp_path)]})))
    assert session_start.main() == 0
    assert json.loads(capsys.readouterr().out) == {}

    # Sürüm değiştiğinde ve batch LTspice varken -> Antigravity injectSteps döner
    monkeypatch.setattr(session_start, "file_version", lambda p: "26.2.0")
    monkeypatch.setattr(session_start, "batch_processes", lambda: ["p1"])
    monkeypatch.setattr(session_start, "describe", lambda ps: "PID 42 (pytest)")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"conversationId": "c1", "workspacePaths": [str(tmp_path)]})))
    assert session_start.main() == 0
    out = json.loads(capsys.readouterr().out)
    msg = out["injectSteps"][0]["systemMessage"]
    assert "26.1.1 -> 26.2.0" in msg["systemMessage"] and "PID 42" in msg["systemMessage"]
    assert "26.1.1 -> 26.2.0" in msg["metadata"]["user_markdown"]
    # sürüm metni iki iletide de var: kullanıcıya giden ileti kısa nottur, modelin talimatı (örnek 12'yi çalıştır) değil
    assert msg["metadata"]["user_markdown"].count("yeniden çalıştırılması gerekiyor") == 1
    assert msg["metadata"]["user_markdown"].count("12_ltspice_reference_checks.py") == 0


def test_stop_gate_supports_antigravity_idle_and_continue_contract(tmp_path, monkeypatch, capsys):
    """Kaynak: Antigravity hooks belgesi Stop sözleşmesi (fullyIdle, terminationReason, decision='continue'|'stop')."""
    monkeypatch.setattr(stop_gate, "ROOT", tmp_path)
    monkeypatch.setattr(stop_gate, "STATE_DIR", tmp_path / ".state")
    monkeypatch.setattr(stop_gate, "STATE", tmp_path / ".state" / "stop_gate.json")
    monkeypatch.setattr(stop_gate, "batch_processes", lambda: [])
    gate_calls = []

    def fake_gate():
        gate_calls.append(1)
        return subprocess.CompletedProcess(["check.py"], 1, stdout="[FAIL] tests\n1 failed\n", stderr="")

    monkeypatch.setattr(stop_gate, "run_gate", fake_gate)
    (tmp_path / "ltspice_llm").mkdir()
    (tmp_path / "ltspice_llm" / "a.py").write_text("x = 1\n", encoding="utf-8")

    # Arka plan görevi çalışırken (fullyIdle=False) kapı koşulmaz, durmaya izin verilir
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "conversationId": "c1", "terminationReason": "NO_TOOL_CALL", "fullyIdle": False})))
    assert stop_gate.main() == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "stop"
    assert gate_calls == []

    # Kullanıcı iptalinde (USER_CANCELED) kapı koşulmaz
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "conversationId": "c1", "terminationReason": "USER_CANCELED", "fullyIdle": True})))
    assert stop_gate.main() == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "stop"
    assert gate_calls == []

    # Normal tur bitişinde (fullyIdle=True, NO_TOOL_CALL) kırık kapı bir kez 'continue' döner, aynı parmak izinde ikinci kez engellemez
    payload = json.dumps({"conversationId": "c1", "terminationReason": "NO_TOOL_CALL", "fullyIdle": True})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert stop_gate.main() == 0
    first = json.loads(capsys.readouterr().out)
    assert first["decision"] == "continue" and "Kalite kapısı kırık" in first["reason"]
    assert len(gate_calls) == 1

    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    assert stop_gate.main() == 0
    second = json.loads(capsys.readouterr().out)
    assert second["decision"] == "stop"


def test_test_reviewer_agent_is_synced_between_agents_and_claude():
    """Kaynak: Antigravity alt ajan ayarları belgesi ('agents/<name>.md': name, description, subagent: true, mainAgent: false,
    tools: [read_file, run_command, manage_task]; gövde Claude Code alt ajanıyla birebir aynı)."""
    import yaml

    claude_path = ROOT / ".claude" / "agents" / "test-reviewer.md"
    agents_path = ROOT / ".agents" / "agents" / "test-reviewer.md"
    assert claude_path.is_file() and agents_path.is_file()

    def split_md(p):
        text = p.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        end = text.index("\n---\n", 4)
        return yaml.safe_load(text[4:end]), text[end + 5:]

    c_meta, c_body = split_md(claude_path)
    a_meta, a_body = split_md(agents_path)
    assert a_meta["name"] == c_meta["name"] == "test-reviewer"
    assert a_meta["description"] == c_meta["description"]
    assert a_meta["subagent"] is True and a_meta["mainAgent"] is False
    assert a_meta["tools"] == ["read_file", "run_command", "manage_task"]
    assert a_body == c_body


def test_both_agents_wire_each_event_to_the_same_hook_script():
    """.agents/hooks.json ile .claude/settings.json aynı olaya aynı betiği bağlar: oturum başı session_start, komut öncesi
    pre_ltspice_guard, dosya düzenlemesi öncesi guard_tests, tur sonu stop_gate. Betik adlarının kümesi (yukarıdaki test)
    yer değiştirmiş iki betiği görmez."""
    agy = json.loads((ROOT / ".agents" / "hooks.json").read_text(encoding="utf-8"))["ltspice-harness"]
    claude = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))["hooks"]

    def scripts(hooks):
        return {Path(hook["command"].split()[-1].strip('"')).name for hook in hooks}

    agy_map = {"SessionStart": scripts(agy["SessionStart"]), "Stop": scripts(agy["Stop"])}
    agy_map.update({group["matcher"]: scripts(group["hooks"]) for group in agy["PreToolUse"]})
    claude_map = {event: scripts(hook for group in claude[event] for hook in group["hooks"])
                  for event in ("SessionStart", "Stop")}
    claude_map.update({group["matcher"]: scripts(group["hooks"]) for group in claude["PreToolUse"]})
    events = {"SessionStart": {"session_start.py"}, "Stop": {"stop_gate.py"}}
    assert agy_map == {**events, "run_command": {"pre_ltspice_guard.py"}, "edit_file|write_file": {"guard_tests.py"}}
    assert claude_map == {**events, "Bash|PowerShell": {"pre_ltspice_guard.py"}, "Edit|Write": {"guard_tests.py"}}


@pytest.mark.parametrize("platform, command, expected", [
    ("win32", "$env:LTSPICE_EXE='C:\\yok\\x.exe'; python -m pytest tests -q", None),
    ("win32", "export LTSPICE_EXE=/yok/x.exe; python -m pytest tests -q", "deny"),
    ("linux", "export LTSPICE_EXE=/yok/x.exe; python -m pytest tests -q", None),
    ("linux", "$env:LTSPICE_EXE='C:\\yok\\x.exe'; python -m pytest tests -q", "deny"),
], ids=["windows-powershell", "windows-export", "linux-export", "linux-powershell"])
def test_antigravity_command_is_read_with_the_platform_shell(monkeypatch, capsys, platform, command, expected):
    """Antigravity girdisinde komut yalnız metindir (toolCall.args.CommandLine); kod kabuğu platformdan çıkarır: Windows'ta
    PowerShell, öbür platformlarda Bash (pre_ltspice_guard.main). Atama o kabuğun kuralıyla okunur
    (test_ltspice_guard_reads_commands_like_the_shell_does)."""
    monkeypatch.setattr(ltspice_guard, "batch_processes", lambda: ["batch"])
    monkeypatch.setattr(ltspice_guard, "owned_by_pytest", lambda p: False)
    monkeypatch.setattr(ltspice_guard, "describe", lambda ps: ", ".join(ps))
    monkeypatch.setattr(sys, "platform", platform)
    payload = {"conversationId": "c1", "toolCall": {"name": "run_command", "args": {"CommandLine": command}}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert ltspice_guard.main() == 0
    raw = capsys.readouterr().out
    assert (json.loads(raw)["decision"] if raw else None) == expected


def test_guard_tests_denies_editing_an_existing_asc_in_both_agents(tmp_path):
    """Var olan bir şema (LTspice'ın cp1252 biçimi, µ'lü; GEMINI.md "LTspice referans kuralları"): Edit ve edit_file de
    reddedilir, dosyaya dokunulmaz. Yukarıdaki test yalnız henüz olmayan bir .asc'yi sınıyor."""
    asc = tmp_path / "devre.asc"
    data = "Version 4\r\nSYMATTR Value 1µ\r\n".encode("cp1252")
    asc.write_bytes(data)
    claude_edit = {"tool_name": "Edit", "tool_input": {"file_path": str(asc), "old_string": "1µ", "new_string": "2µ"}}
    out = _run_guard(claude_edit, tmp_path)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny" and out["permissionDecisionReason"].count("AscSchematic") == 1
    agy_edit = {"conversationId": "c1", "workspacePaths": [str(tmp_path)],
                "toolCall": {"name": "edit_file", "args": {"Path": str(asc), "Target": "1µ", "Replacement": "2µ"}}}
    out_agy = _run_guard(agy_edit, tmp_path)
    assert out_agy["decision"] == "deny" and out_agy["reason"].count("AscSchematic") == 1
    assert asc.read_bytes() == data


# Claude Code hook girdileri (Claude Code hooks belgesi, "Hook Input"): her olayda session_id, transcript_path, cwd,
# permission_mode ve hook_event_name; PreToolUse'ta tool_name ve tool_input, Stop'ta stop_hook_active, SessionStart'ta
# source. Antigravity'nin alanları (conversationId, workspacePaths, toolCall, terminationReason) yok.
CLAUDE_EVENT = {"session_id": "s1", "transcript_path": "C:/proje/s1.jsonl", "cwd": "C:/proje",
                "permission_mode": "default"}


@pytest.mark.parametrize("event, expected", [
    ({**CLAUDE_EVENT, "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "python -V"}},
     False),
    ({**CLAUDE_EVENT, "hook_event_name": "Stop", "stop_hook_active": False}, False),
    ({**CLAUDE_EVENT, "hook_event_name": "SessionStart", "source": "startup"}, False),
    ({"conversationId": "c1", "toolCall": {"name": "run_command", "args": {"CommandLine": "python -V"}}}, True),
    ({"conversationId": "c1", "terminationReason": "NO_TOOL_CALL", "fullyIdle": True}, True),
    ({"conversationId": "c1", "workspacePaths": ["C:/proje"]}, True),
], ids=["claude-pretooluse", "claude-stop", "claude-sessionstart", "agy-pretooluse", "agy-stop", "agy-sessionstart"])
def test_hook_input_is_recognised_by_its_own_fields(event, expected):
    """Claude Code olayı Antigravity sanılırsa pre_ltspice_guard boş CommandLine okur ve LTspice koruması sessizce kapanır."""
    assert ltspice_guard.is_antigravity(event) is expected


@pytest.mark.parametrize("command, batch", [
    ("python -m pytest tests -q", ["batch"]),           # batch LTspice çalışırken LTspice'lı pytest
    ("python scripts/x.py && git reset --hard", []),    # python ile başlayan komut hook'a gelir (settings.json "if")
], ids=["pytest", "git-reset-hard"])
def test_claude_pre_tool_use_gets_the_claude_deny_format(monkeypatch, capsys, command, batch):
    """Claude Code hooks belgesi ("PreToolUse Decision Control"): karar hookSpecificOutput.permissionDecision'dadır;
    Antigravity'nin üst düzey "decision" alanı Claude Code'da karar değildir."""
    monkeypatch.setattr(ltspice_guard, "batch_processes", lambda: batch)
    monkeypatch.setattr(ltspice_guard, "owned_by_pytest", lambda p: False)
    monkeypatch.setattr(ltspice_guard, "describe", lambda ps: ", ".join(ps))
    event = {**CLAUDE_EVENT, "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
    assert ltspice_guard.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert list(out) == ["hookSpecificOutput"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_stop_gate_blocks_claude_once_and_never_while_the_stop_hook_is_active(tmp_path, monkeypatch, capsys):
    """Claude Code hooks belgesi ("Stop Decision Control"): "decision": "block" ve reason Claude'u sürdürür.
    stop_hook_active doğruysa Claude zaten bir Stop hook'u yüzünden sürüyordur; yeniden engellemek döngü olur, sorun
    kullanıcıya systemMessage ile gösterilir."""
    monkeypatch.setattr(stop_gate, "ROOT", tmp_path)
    monkeypatch.setattr(stop_gate, "STATE_DIR", tmp_path / ".state")
    monkeypatch.setattr(stop_gate, "STATE", tmp_path / ".state" / "stop_gate.json")
    monkeypatch.setattr(stop_gate, "batch_processes", lambda: [])
    monkeypatch.setattr(stop_gate, "run_gate", lambda: subprocess.CompletedProcess(
        ["check.py"], 1, stdout="[FAIL] tests\n1 failed\n", stderr=""))
    source = tmp_path / "ltspice_llm" / "a.py"
    source.parent.mkdir()
    source.write_text("x = 1\n", encoding="utf-8")

    def stop(active):
        event = {**CLAUDE_EVENT, "hook_event_name": "Stop", "stop_hook_active": active}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))
        assert stop_gate.main() == 0
        return json.loads(capsys.readouterr().out)

    first = stop(False)
    assert first["decision"] == "block" and first["reason"].count("Kalite kapısı kırık") == 1
    source.write_text("x = 22\n", encoding="utf-8")  # yeni dosya durumu: kapı yeniden koşar, yine kırık
    second = stop(True)
    assert "decision" not in second and second["systemMessage"].count("Kalite kapısı kırık") == 1


def test_session_start_tells_claude_and_the_user_different_things(tmp_path, monkeypatch, capsys):
    """Claude Code hooks belgesi ("SessionStart"): hookSpecificOutput.additionalContext modelin bağlamına, üst düzey
    systemMessage kullanıcıya gider. Kullanıcıya kısa not gider, modelin talimatı (örnek 12'yi çalıştır) yalnız bağlamda
    durur (session_start belgesi: not hem Claude'a hem kullanıcıya bir kez gösterilir)."""
    monkeypatch.setattr(session_start, "STATE_DIR", tmp_path)
    monkeypatch.setattr(session_start, "STATE", tmp_path / "ltspice.json")
    fake_exe = tmp_path / "LTspice.exe"
    fake_exe.write_bytes(b"exe")
    monkeypatch.setattr(session_start, "find_ltspice_exe", lambda: fake_exe)
    monkeypatch.setattr(session_start, "batch_processes", lambda: [])
    event = json.dumps({**CLAUDE_EVENT, "hook_event_name": "SessionStart", "source": "startup"})

    monkeypatch.setattr(session_start, "file_version", lambda p: "26.1.1")
    monkeypatch.setattr(sys, "stdin", io.StringIO(event))
    assert session_start.main() == 0
    assert capsys.readouterr().out == ""  # söylenecek bir şey yoksa Claude Code'a hiçbir şey yazılmaz

    monkeypatch.setattr(session_start, "file_version", lambda p: "26.2.0")
    monkeypatch.setattr(sys, "stdin", io.StringIO(event))
    assert session_start.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"hookSpecificOutput", "systemMessage"}
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    context, user = out["hookSpecificOutput"]["additionalContext"], out["systemMessage"]
    assert context.count("12_ltspice_reference_checks.py") == 1 and user.count("12_ltspice_reference_checks.py") == 0
    assert user.count("26.1.1 -> 26.2.0") == 1 and user.count("yeniden çalıştırılması gerekiyor") == 1



def test_guard_tests_answers_a_full_claude_event_in_claude_format(tmp_path):
    """Gerçek Claude Code PreToolUse olayı (CLAUDE_EVENT alanlarıyla; Claude Code hooks belgesi "Hook Input") Antigravity
    sanılmamalı: guard_tests'in kendi anahtar listesi bu alanlardan birini içerse karar Claude Code'un okumadığı biçime
    gider ve koruma sessizce kapanır. Karar hookSpecificOutput'ta, hookEventName ile ("PreToolUse Decision Control");
    .asc reddinde de (test-reviewer 2026-11-11)."""
    test_file = tmp_path / "tests" / "test_x.py"
    test_file.parent.mkdir()
    test_file.write_text(TESTS, encoding="utf-8")
    base = {**CLAUDE_EVENT, "cwd": str(tmp_path), "hook_event_name": "PreToolUse"}
    asc_write = {**base, "tool_name": "Write",
                 "tool_input": {"file_path": str(tmp_path / "devre.asc"), "content": "Version 4\n"}}
    weakening = {**base, "tool_name": "Edit", "tool_input": {
        "file_path": str(test_file), "old_string": "    assert f(2) == 3\n", "new_string": ""}}
    for event, decision in ((asc_write, "deny"), (weakening, "ask")):
        out = _run_guard(event, tmp_path)
        assert list(out) == ["hookSpecificOutput"]
        assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert out["hookSpecificOutput"]["permissionDecision"] == decision
