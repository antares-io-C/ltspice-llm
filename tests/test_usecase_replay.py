"""
Netlist simülasyonunun başarı ve log-hata yolları, gerçek LTspice çıktısını yeniden oynatan sahte süreçle
(LTspice gerektirmez; akış: tests/usecases/run_netlist.json).

Sahte olan yalnızca gerçek sınırdır (``subprocess.Popen``): başlatılınca gerçek LTspice 26.1.1 çıktılarını
(``tests/data/ref26_flow_rc.*``, examples/14; ``ref26_dcop_fail.log.txt``) netlistin yanına yazar ve rc=0 döner.
"""

import errno
import os
import re
import sys
import threading
from pathlib import Path

import pytest

import ltspice_llm.adapter as adapter_mod
import ltspice_llm.cpu_guard as guard
from ltspice_llm import LTSpiceAdapter
from ltspice_llm.cpu_guard import CpuSlotPool
from ltspice_llm.diagnostics import SPICEIssueType

DATA = Path(__file__).parent / "data"
RC = (DATA / "ref26_flow_rc.net").read_text(encoding="utf-8")


class _ReplayPopen:
    outputs = {}
    netlists = []
    launched = []

    def __init__(self, cmd, **kwargs):
        netlist = Path(cmd[-1])
        type(self).launched.append(netlist)
        type(self).netlists.append(netlist.read_text(encoding="utf-8"))
        for suffix, source in type(self).outputs.items():
            netlist.with_suffix(suffix).write_bytes(source.read_bytes())
        self.pid, self.returncode = 999, 0

    def communicate(self, timeout=None):
        return "", ""


@pytest.fixture
def replay(monkeypatch):
    monkeypatch.setattr(adapter_mod.subprocess, "Popen", _ReplayPopen)
    _ReplayPopen.netlists = []
    _ReplayPopen.launched = []
    return _ReplayPopen


def _adapter(tmp_path):
    return LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path / "ws", solver_threads=1)


def test_successful_run_reads_measurements_from_the_ltspice_log(tmp_path, replay):
    replay.outputs = {".log": DATA / "ref26_flow_rc.log.txt", ".raw": DATA / "ref26_flow_rc.raw"}
    res = _adapter(tmp_path).run_netlist(RC, circuit_name="rc", treat_as_text=True)
    assert res.success and res.errors == []
    assert res.measurements["v_max"] == pytest.approx(3.58834481239)  # LTspice 26.1.1 logu (examples/14)
    assert any(name.lower() == "v(out)" for name in res.raw_summary)
    assert ".options threads=1" in replay.netlists[0]


def test_failed_operating_point_with_exit_code_zero_is_not_success(tmp_path, replay):
    """GEMINI.md: başarıyı çıkış kodundan değil logdan belirle (Simulation Failed olan koşum rc=0 dönebiliyor)."""
    replay.outputs = {".log": DATA / "ref26_dcop_fail.log.txt"}
    res = _adapter(tmp_path).run_netlist(RC, circuit_name="fail", treat_as_text=True)
    assert not res.success
    assert SPICEIssueType.DC_OP_CONVERGENCE.value in str(res.diagnostics)


# --------------------------------------------------------------------------- dosya yolu: devre kendi klasöründe koşar
# ADI SIMULATION-COMMANDS-REFERENCE: göreli .include "the directory containing the directive"den, .lib önce "Directory of
# calling netlist"ten çözülür; SCHEMATIC-REFERENCE "Symbol and Library Search Paths": semboller önce "Folder of currently
# active schematic"te aranır. Kopya çalışma alanında koşulunca tasarımcının model dosyası "File not found" veriyordu,
# şemanın yanındaki sembol netlistten uyarısız düşüyordu (LTspice 26.1.1; examples/12).
RC_LOG = {".log": DATA / "ref26_flow_rc.log.txt", ".raw": DATA / "ref26_flow_rc.raw"}
# LTspice .tran koşumunda çalışma noktasını <ad>.op.raw'a, .meas sonuçlarını <ad>.db'ye de yazar (26.1.1 çalışma alanı;
# kayıtlar: ref26_flow_op.raw examples/14'ün .op koşumu, ref26_ac_polar.db bir .meas veritabanı)
RC_RUN = {**RC_LOG, ".op.raw": DATA / "ref26_flow_op.raw", ".db": DATA / "ref26_ac_polar.db"}
ASC_TEXT = "Version 4\nSHEET 1 880 680\nSYMBOL res 0 0 R0\nSYMATTR InstName R1\nSYMATTR Value 1k\nTEXT 0 200 Left 2 !.op\n"


def _project(tmp_path, name, content):
    folder = tmp_path / "proj"
    folder.mkdir()
    (folder / "models.lib").write_text("* designer's model file\n", encoding="ascii")
    src = folder / name
    src.write_bytes(content.encode("cp1252"))
    return src


@pytest.mark.parametrize("as_arg", [Path, str], ids=["Path", "str"])  # README: run_netlist(r"C:\...\buck.asc")
@pytest.mark.parametrize("name,content,outputs", [
    ("rc.net", RC, RC_RUN),
    # LTspice şemanın yanına üretilen netlisti de yazar (ref26_sch_connected: examples/14'ün .asc kaydı)
    ("rc.asc", ASC_TEXT, {**RC_RUN, ".net": DATA / "ref26_sch_connected.net"}),
])
def test_circuit_file_runs_in_its_own_folder_and_results_move_to_the_workspace(tmp_path, replay, name, content,
                                                                              outputs, as_arg):
    replay.outputs = outputs
    src = _project(tmp_path, name, content)
    before = src.read_bytes()
    ad = _adapter(tmp_path)
    res = ad.run_netlist(as_arg(src), circuit_name="rc")
    assert [p.parent for p in replay.launched] == [src.parent.resolve()]
    assert replay.launched[0].suffix == src.suffix
    assert res.success and res.measurements["v_max"] == pytest.approx(3.58834481239)  # LTspice 26.1.1 logu (examples/14)
    assert (res.netlist_path, res.log_path, res.raw_path, res.db_path) == (
        ad.workspace_dir / name, ad.workspace_dir / "rc.log", ad.workspace_dir / "rc.raw", ad.workspace_dir / "rc.db")
    assert {s: (ad.workspace_dir / f"rc{s}").read_bytes() == path.read_bytes() for s, path in outputs.items()} == {
        s: True for s in outputs}
    assert re.findall(r"\.options threads=(\d+)\b", res.netlist_path.read_text(encoding="cp1252")) == ["1"]
    assert sorted(p.name for p in src.parent.iterdir()) == sorted(["models.lib", name])
    assert src.read_bytes() == before


def test_netlist_text_and_workspace_files_run_in_the_workspace(tmp_path, replay):
    """Araç katmanının yolları: modelin metni (treat_as_text=True) ve çalışma alanındaki dosya (modify_parameters)
    çalışma alanında koşar; dosya erişimi politikası (netlist.file_access_problems) göreli yolları bu klasöre göre
    serbest bırakır."""
    replay.outputs = RC_LOG
    ad = _adapter(tmp_path)
    ad.run_netlist(RC, circuit_name="rc", treat_as_text=True)
    ad.run_netlist(ad.workspace_dir / "rc.net", circuit_name="rc")
    assert replay.launched == [ad.workspace_dir / "rc.net"] * 2


def test_unwritable_source_folder_runs_in_the_workspace_with_a_warning(tmp_path, replay):
    """Kopya kaynağın klasörüne yazılamazsa (burada kopyanın yerinde bir klasör var) koşum çalışma alanında yapılır ve
    uyarılır: göreli yollar ve şemanın yanındaki semboller orada bulunamayabilir."""
    replay.outputs = RC_LOG
    src = _project(tmp_path, "rc.net", RC)
    (src.parent / f"{adapter_mod._staging_stem('rc')}.net").mkdir()
    ad = _adapter(tmp_path)
    res = ad.run_netlist(src, circuit_name="rc")
    assert replay.launched == [ad.workspace_dir / "rc.net"]
    assert res.success
    assert sum("kendi klasöründe koşulamadı" in w for w in res.warnings) == 1


def test_sweep_of_a_netlist_file_runs_in_its_own_folder(tmp_path, replay):
    replay.outputs = RC_LOG
    src = _project(tmp_path, "rc.net", RC)
    _adapter(tmp_path).run_sweep(src, "R1", ["1k", "2k"], circuit_name="sw")
    assert [p.parent for p in replay.launched] == [src.parent.resolve()]
    assert replay.netlists[0].count(".step param R1 list 1k 2k") == 1
    assert sorted(p.name for p in src.parent.iterdir()) == ["models.lib", "rc.net"]


def test_two_runs_of_the_same_file_at_once_use_separate_copies(tmp_path, replay, monkeypatch):
    """run_batch: aynı dosya, aynı ad, farklı R1 (her işin kendi çalışma alanı var, ad çakışması meşru). İki LTspice da
    başlamadan hiçbiri bitmez (engel); iki kopya aynı anda klasördedir ve adları iş parçacığına özgü olduğundan birbirini
    ezmez. Makine 2 çekirdekli sahtelenir: tek çekirdekte işler sıraya girer ve engel hiç dolmaz."""
    monkeypatch.setattr(adapter_mod, "available_cpus", lambda: 2)
    monkeypatch.setattr(guard.os, "cpu_count", lambda: 2)  # LTspice'ın sınırı (cpu_guard.ltspice_thread_limit)
    both_started = threading.Barrier(2, timeout=10)

    class Concurrent(_ReplayPopen):
        def __init__(self, cmd, **kwargs):
            both_started.wait()
            super().__init__(cmd, **kwargs)

    monkeypatch.setattr(adapter_mod.subprocess, "Popen", Concurrent)
    replay.outputs = RC_LOG
    src = _project(tmp_path, "rc.net", RC)
    ad = LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path / "ws", solver_threads=1,
                        cpu_guard=CpuSlotPool(size=2, directory=tmp_path / "slots"))
    jobs = [dict(netlist_content_or_path=src, circuit_name="rc", parameters={"R1": value}) for value in ("1k", "2k")]
    results = ad.run_batch(jobs, max_workers=2)
    assert [r.success for r in results] == [True, True]
    assert len(set(replay.launched)) == 2 and {p.parent for p in replay.launched} == {src.parent.resolve()}
    assert sorted(re.findall(r"^R1 in out (\S+)$", text, re.M)[0] for text in replay.netlists) == ["1k", "2k"]
    assert [re.findall(r"^R1 in out (\S+)$", r.netlist_path.read_text(encoding="utf-8"), re.M) for r in results] == [
        ["1k"], ["2k"]]
    assert sorted(p.name for p in src.parent.iterdir()) == ["models.lib", "rc.net"]


def test_outputs_reach_the_workspace_from_another_drive(tmp_path, replay, monkeypatch):
    """Tasarım başka bir sürücüde (D:, ağ paylaşımı), çalışma alanı %TEMP%'te: os.replace sürücüler arasında taşıyamaz
    (POSIX EXDEV, Windows ERROR_NOT_SAME_DEVICE); dosya kopyalanıp silinir. İşletim sistemi sınırı sahtelenir: tek geçici
    birimde üretilemez."""
    replay.outputs = RC_LOG
    src = _project(tmp_path, "rc.net", RC)
    real_replace = os.replace

    def across_drives(source, target):
        if Path(source).parent == src.parent.resolve():
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_replace(source, target)

    monkeypatch.setattr(adapter_mod.os, "replace", across_drives)
    ad = _adapter(tmp_path)
    res = ad.run_netlist(src, circuit_name="rc")
    assert res.success and (res.log_path, res.raw_path) == (ad.workspace_dir / "rc.log", ad.workspace_dir / "rc.raw")
    assert sorted(p.name for p in src.parent.iterdir()) == ["models.lib", "rc.net"]
