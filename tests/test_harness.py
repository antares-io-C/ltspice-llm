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
