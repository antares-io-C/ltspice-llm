"""
Adaptör yapılandırması, raw biçimi seçimi, platform desteği ve çalışma alanı temizliği testleri.
Gerçek LTspice çalıştırılmaz: süreç başlatma sahte bir Popen ile izlenir.
"""

import os
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import ltspice_llm.adapter as adapter_mod
from ltspice_llm import LTSpiceAdapter

PLAIN = "* t\nV1 in 0 10\nR1 in 0 1k\n.op\n.end\n"
STEPPED = "* t\n.param RV=1k\nV1 in 0 10\nR1 in 0 {RV}\n.step param RV list 1k 2k\n.op\n.end\n"


class _FakePopen:
    last_cmd = None
    last_kwargs = None

    def __init__(self, cmd, **kwargs):
        type(self).last_cmd = cmd
        type(self).last_kwargs = kwargs
        self.returncode = 0
        self.pid = 4321

    def communicate(self, timeout=None):
        return "", ""


@pytest.fixture
def fake_popen(monkeypatch):
    monkeypatch.setattr(adapter_mod.subprocess, "Popen", _FakePopen)
    _FakePopen.last_cmd = _FakePopen.last_kwargs = None
    return _FakePopen


def _adapter(tmp_path, **kw):
    return LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path / "ws", **kw)


# --------------------------------------------------------------------------- raw biçimi
def test_auto_raw_format_is_always_binary(tmp_path, fake_popen):
    """'auto' ikili biçimi seçer: ölçümde ~3x hızlı simülasyon, ~18x hızlı ayrıştırma, ~6x küçük dosya."""
    ad = _adapter(tmp_path)
    for name, text in (("a", PLAIN), ("b", STEPPED)):
        ad.run_netlist(text, circuit_name=name, treat_as_text=True)
        assert "-ascii" not in fake_popen.last_cmd
        assert "-b" in fake_popen.last_cmd


@pytest.mark.parametrize("fmt,text,expect_ascii", [
    ("ascii", STEPPED, True), ("binary", PLAIN, False), ("ascii", PLAIN, True), ("binary", STEPPED, False),
])
def test_explicit_raw_format_overrides_auto(tmp_path, fake_popen, fmt, text, expect_ascii):
    _adapter(tmp_path, raw_format=fmt).run_netlist(text, circuit_name="x", treat_as_text=True)
    assert ("-ascii" in fake_popen.last_cmd) is expect_ascii


def test_invalid_raw_format_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="raw_format"):
        _adapter(tmp_path, raw_format="xml")


def test_ascii_stays_opt_in(tmp_path, fake_popen):
    """ASCII yalnizca acikca istendiginde kullanilir (yorum satirindaki .step secimi etkilemez)."""
    commented = "* t\n* .step param RV list 1k 2k\nR1 a 0 1k\n.op\n.end\n"
    _adapter(tmp_path).run_netlist(commented, circuit_name="c", treat_as_text=True)
    assert "-ascii" not in fake_popen.last_cmd
    _adapter(tmp_path, raw_format="ascii").run_netlist(commented, circuit_name="c", treat_as_text=True)
    assert "-ascii" in fake_popen.last_cmd


# ------------------------------------------------------------------------ yürütülebilir
def test_executable_can_be_set_with_environment_variable(tmp_path, monkeypatch):
    monkeypatch.setenv("LTSPICE_EXE", sys.executable)
    ad = LTSpiceAdapter(workspace_dir=tmp_path)
    assert ad.executable_path == Path(sys.executable).resolve()


def test_invalid_environment_variable_is_reported_not_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("LTSPICE_EXE", str(tmp_path / "yok.exe"))
    with pytest.raises(FileNotFoundError, match="LTSPICE_EXE"):
        LTSpiceAdapter(workspace_dir=tmp_path)


def test_explicit_path_wins_over_environment_variable(tmp_path, monkeypatch):
    monkeypatch.setenv("LTSPICE_EXE", str(tmp_path / "yok.exe"))
    ad = LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path)
    assert ad.executable_path == Path(sys.executable).resolve()


def test_macos_install_location_is_searched():
    assert Path("/Applications/LTspice.app/Contents/MacOS/LTspice") in LTSpiceAdapter.KNOWN_MAC_PATHS


# ------------------------------------------------------------------- POSIX süreç grubu
def test_posix_starts_new_session_and_kills_process_group(tmp_path, fake_popen, monkeypatch):
    monkeypatch.setattr(adapter_mod, "_IS_WINDOWS", False)
    _adapter(tmp_path).run_netlist(PLAIN, circuit_name="p", treat_as_text=True)
    assert fake_popen.last_kwargs["start_new_session"] is True

    calls = []
    monkeypatch.setattr(os, "getpgid", lambda pid: 4242, raising=False)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append((pgid, sig)), raising=False)
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)

    class Proc:
        pid = 111
        killed = False

        def kill(self):
            self.killed = True

        def communicate(self, timeout=None):
            return "", ""

    proc = Proc()
    LTSpiceAdapter._kill_process_tree(proc)
    assert calls == [(4242, 9)]  # yalnızca kendi süreç grubu
    assert proc.killed


def test_windows_does_not_pass_start_new_session(tmp_path, fake_popen, monkeypatch):
    monkeypatch.setattr(adapter_mod, "_IS_WINDOWS", True)
    _adapter(tmp_path).run_netlist(PLAIN, circuit_name="w", treat_as_text=True)
    assert "start_new_session" not in fake_popen.last_kwargs


# ------------------------------------------------------------------ çalışma alanı temizliği
def _touch(path: Path, age_seconds: float = 0.0):
    path.write_text("x")
    t = time.time() - age_seconds
    os.utime(path, (t, t))


def test_remove_circuit_deletes_only_that_circuits_files(tmp_path):
    ad = _adapter(tmp_path)
    for name in ("c.net", "c.log", "c.raw", "c.op.raw", "c.db", "c.png", "c_bode.png", "other.net", "c_2.net"):
        _touch(ad.workspace_dir / name)
    removed = ad.remove_circuit("c")
    assert sorted(removed) == ["c.db", "c.log", "c.net", "c.op.raw", "c.png", "c.raw", "c_bode.png"]
    assert (ad.workspace_dir / "other.net").exists() and (ad.workspace_dir / "c_2.net").exists()


def test_remove_circuit_sanitizes_the_name_and_cannot_escape_the_workspace(tmp_path):
    ad = _adapter(tmp_path)
    outside = tmp_path / "secret.net"
    _touch(outside)
    assert ad.remove_circuit("../secret") == []
    assert outside.exists()


def test_cleanup_workspace_removes_only_old_artifacts(tmp_path):
    ad = _adapter(tmp_path)
    _touch(ad.workspace_dir / "old.net", age_seconds=3600)
    _touch(ad.workspace_dir / "old.raw", age_seconds=3600)
    _touch(ad.workspace_dir / "new.net", age_seconds=1)
    _touch(ad.workspace_dir / "keep.txt", age_seconds=3600)  # tanınmayan uzantıya dokunulmaz
    (ad.workspace_dir / "sub").mkdir()
    assert ad.cleanup_workspace(max_age_seconds=600) == 2
    assert {p.name for p in ad.workspace_dir.iterdir()} == {"new.net", "keep.txt", "sub"}
    assert ad.cleanup_workspace(max_age_seconds=0) == 1  # 0: hepsi
    assert {p.name for p in ad.workspace_dir.iterdir()} == {"keep.txt", "sub"}


def test_ltspice_version_asks_once_per_executable_and_is_none_on_error(tmp_path, monkeypatch):
    """Belge: ``LTspice.exe -version`` ilk satırı, süreç başına önbellek; okunamazsa None."""
    calls = []
    monkeypatch.setattr(LTSpiceAdapter, "_version_cache", {})
    monkeypatch.setattr(adapter_mod.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(stdout="26.1.1\nek satır\n"))
    ad = LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path)
    assert ad.ltspice_version() == "26.1.1" and ad.ltspice_version() == "26.1.1"
    assert calls == [[str(ad.executable_path), "-version"]]

    def broken(cmd, **kw):
        raise OSError("başlatılamadı")
    monkeypatch.setattr(LTSpiceAdapter, "_version_cache", {})
    monkeypatch.setattr(adapter_mod.subprocess, "run", broken)
    assert ad.ltspice_version() is None


ASC = "Version 4\nSHEET 1 880 680\n"


def _netlister(monkeypatch, behaviour):
    """``LTspice.exe -netlist`` yerine geçen sahte süreç (gerçek sınır); verdiği şema kopyalarını kaydeder."""
    seen = []

    def fake_run(cmd, **kw):
        asc = Path(cmd[-1])
        seen.append(asc)
        if behaviour == "timeout":
            raise adapter_mod.subprocess.TimeoutExpired(cmd, 1)
        if behaviour == "silent":   # sembol bulunamadığında LTspice .net yazmadan döner
            return SimpleNamespace(returncode=1, stdout="")
        asc.with_suffix(".net").write_text(f"* {asc}\nR1 a b 1k\n.end\n", encoding="utf-8")
        asc.with_suffix(".log").write_text("netlist\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(adapter_mod.subprocess, "run", fake_run)
    return seen


def test_schematic_to_netlist_rejects_text_that_is_not_a_schematic(tmp_path):
    with pytest.raises(ValueError, match="asc"):
        LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path).schematic_to_netlist(
            "* netlist\n.end\n", treat_as_text=True)


def test_schematic_to_netlist_names_the_schematic_and_removes_temporary_files(tmp_path, monkeypatch):
    """Belge: kopya geçici adla çevrilir ve hemen silinir; ilk satır geçici yol yerine şemanın adı olur."""
    seen = _netlister(monkeypatch, "ok")
    net = LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path).schematic_to_netlist(
        ASC, circuit_name="rc", treat_as_text=True)
    assert net.splitlines()[0] == "* rc.asc"
    assert list(seen[0].parent.iterdir()) == []


def test_schematic_to_netlist_timeout_is_an_error_and_still_cleans_up(tmp_path, monkeypatch):
    seen = _netlister(monkeypatch, "timeout")
    with pytest.raises(RuntimeError, match="bitmedi"):
        LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path).schematic_to_netlist(
            ASC, circuit_name="rc", treat_as_text=True)
    assert list(seen[0].parent.iterdir()) == []


def test_files_that_cannot_be_deleted_are_reported_not_raised(tmp_path, monkeypatch, caplog):
    ad = LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path)
    (tmp_path / "rc.net").write_text("* rc\n", encoding="utf-8")

    unlink = Path.unlink

    def locked(self, *a, **k):   # yalnızca bu dosya kilitli: başka bir silme yamadan etkilenmesin
        if self.name == "rc.net":
            raise PermissionError("dosya kilitli")   # Windows'ta açık dosya (işletim sistemi sınırı)
        return unlink(self, *a, **k)
    monkeypatch.setattr(Path, "unlink", locked)
    with caplog.at_level("WARNING"):
        assert ad.remove_circuit("rc") == []
        assert ad.cleanup_workspace(0) == 0
    assert caplog.text.count("silinemedi") == 2


@pytest.mark.parametrize("value", ["abc", "inf", "1e999", "nan", ""])
def test_unreadable_user_threads_value_counts_as_set_but_unknown(value):
    """Belge: (var_mı, sayı ya da çözülemediyse None); kullanıcı ayarı varsa adaptör kendi sınırını yazmaz.
    test-reviewer (2026-10-02): 'inf' ve '1e999' OverflowError ile run_netlist'i düşürüyordu."""
    assert adapter_mod._user_threads(f"* t\n.options threads={value}\n.end\n") == (True, None)


def test_schematic_to_netlist_without_output_is_an_error_and_cleans_up(tmp_path, monkeypatch):
    seen = _netlister(monkeypatch, "silent")
    with pytest.raises(RuntimeError, match="netlist üretmedi"):
        LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path).schematic_to_netlist(
            ASC, circuit_name="rc", treat_as_text=True)
    assert list(seen[0].parent.iterdir()) == []


def test_schematic_file_is_converted_next_to_itself_and_the_copy_is_removed(tmp_path, monkeypatch):
    """Belge: dosya yolu verilirse kopya aynı klasörde çevrilir (yerel .asy ve göreli .include çözülsün), sonra silinir."""
    seen = _netlister(monkeypatch, "ok")
    src = tmp_path / "proj" / "rc.asc"
    src.parent.mkdir()
    src.write_bytes(ASC.encode("cp1252"))
    LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path / "ws").schematic_to_netlist(src)
    assert seen[0].parent == src.parent
    assert sorted(p.name for p in src.parent.iterdir()) == ["rc.asc"]


@pytest.mark.parametrize("outcome", [OSError("yok"), adapter_mod.subprocess.TimeoutExpired("ltspice", 30), ""])
def test_ltspice_version_is_none_when_it_cannot_be_read(tmp_path, monkeypatch, outcome):
    """Belge: okunamazsa None (başlatılamadı, zaman aşımı, boş çıktı)."""
    def run(cmd, **kw):
        if isinstance(outcome, BaseException):
            raise outcome
        return SimpleNamespace(stdout=outcome)
    monkeypatch.setattr(LTSpiceAdapter, "_version_cache", {})
    monkeypatch.setattr(adapter_mod.subprocess, "run", run)
    assert LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path).ltspice_version() is None


def test_ltspice_version_cache_is_per_executable(tmp_path, monkeypatch):
    other = tmp_path / "other.exe"
    other.write_bytes(b"")
    calls = []
    monkeypatch.setattr(LTSpiceAdapter, "_version_cache", {})
    monkeypatch.setattr(adapter_mod.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd[0]) or SimpleNamespace(stdout="26.1.1\n"))
    for exe in (sys.executable, other, sys.executable):
        LTSpiceAdapter(executable_path=exe, workspace_dir=tmp_path).ltspice_version()
    assert calls == [str(Path(sys.executable).resolve()), str(other.resolve())]
