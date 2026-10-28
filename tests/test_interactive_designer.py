"""
interactive_designer.py (CLI) testleri: menü akışı, hazır şablon, hata yolları ve otomasyon modu.
Gerçek LTspice çalıştırılmaz; adaptör, netlisti çalışma alanına yazan sahte bir nesnedir.
"""

import builtins
import sys
from pathlib import Path

import pytest

import interactive_designer as designer
from ltspice_llm import SimulationResult


class _FakeAdapter:
    """LTSpiceAdapter yerine geçer: netlisti çalışma alanına yazar, simülasyon yapmaz."""

    executable_path = Path("fake_ltspice.exe")

    def __init__(self, workspace: Path):
        self.workspace_dir = workspace
        self.runs = []

    def run_netlist(self, netlist_content_or_path, circuit_name, parameters=None, **kw):
        from ltspice_llm.adapter import apply_parameters

        content = (
            Path(netlist_content_or_path).read_text(encoding="utf-8")
            if isinstance(netlist_content_or_path, Path) else str(netlist_content_or_path)
        )
        if parameters:
            content = apply_parameters(content, parameters).content
        (self.workspace_dir / f"{circuit_name}.net").write_text(content, encoding="utf-8")
        self.runs.append((circuit_name, parameters))
        return SimulationResult(
            success=True, netlist_path=self.workspace_dir / f"{circuit_name}.net",
            measurements={"v_max": 1.0}, elapsed_seconds=0.1,
        )


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    ad = _FakeAdapter(tmp_path)
    monkeypatch.setattr(designer, "LTSpiceAdapter", lambda *a, **k: ad)
    return ad


def _script_input(monkeypatch, answers):
    queue = list(answers)

    def fake_input(prompt=""):
        if not queue:
            raise EOFError
        return queue.pop(0)

    monkeypatch.setattr(builtins, "input", fake_input)
    return queue


def test_print_error_if_any(capsys):
    assert designer.print_error_if_any({"status": "SUCCESS"}) is False
    assert designer.print_error_if_any({"status": "ERROR", "message": "kötü"}) is True
    assert designer.print_error_if_any({"status": "FAILED", "errors": ["e1"]}) is True
    out = capsys.readouterr().out
    assert "kötü" in out and "e1" in out


def test_non_interactive_mode_reports_ready_and_exits(adapter, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    designer.main()
    out = capsys.readouterr().out
    assert "Otomasyon Modu" in out and "Toplam Kayıtlı Devre: 0" in out


def test_full_menu_flow(adapter, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    queue = _script_input(monkeypatch, [
        "1",                                    # devreleri listele (boş)
        "3", "3",                               # hazır şablon: RC filtre
        "2", "rc_preset",                       # netlisti göster
        "4", "rc_preset", "R1", "2k",           # eleman değerini güncelle
        "5", "rc_preset", "e",                  # Bode (raw yok -> hata mesajı)
        "5", "rc_preset", "h", "V(out)",        # dalga formu (raw yok -> hata mesajı)
        "6", "mycirc", "* t", "R1 a 0 1k", ".end",  # özel netlist
        "7", "R1=15.915k, C1=1.414n", "E24",    # kuantalama
        "8", "rc_preset", "e",                  # teşhis
        "9",                                    # geçersiz seçim
        "0",
    ])
    designer.main()
    out = capsys.readouterr().out

    assert queue == []  # tüm girdiler tüketildi, akış senkron kaldı
    assert "Toplam 0 devre bulundu" in out
    assert "'rc_preset' simüle ediliyor" in out and "Durum       : SUCCESS" in out
    assert "R1 in out 1k" in out                                  # netlist görüntülendi
    assert ("rc_preset", {"R1": "2k"}) in adapter.runs            # modify_parameters çağrıldı
    assert "R1 in out 2k" in (adapter.workspace_dir / "rc_preset.net").read_text(encoding="utf-8")
    assert out.count("Hata:") >= 2                                # raw olmadığı için iki çizim hatası
    assert ("mycirc", None) in adapter.runs
    assert '"R1": "16k"' in out and "Maksimum Hata" in out        # E24 kuantalama
    assert "Teşhis Raporu" in out and "Hata Var mı" in out
    assert "Geçersiz seçim!" in out and "İyi çalışmalar!" in out


@pytest.mark.parametrize("choice,expected_name", [("1", "sallen_key_preset"), ("2", "rc_preset")])
def test_presets_create_their_circuits(adapter, monkeypatch, capsys, choice, expected_name):
    _script_input(monkeypatch, [choice])
    designer.run_preset(adapter)
    assert adapter.runs == [(expected_name, None)]
    assert (adapter.workspace_dir / f"{expected_name}.net").exists()
    assert "Durum       : SUCCESS" in capsys.readouterr().out


def test_eof_ends_the_session_cleanly(adapter, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    _script_input(monkeypatch, [])  # ilk input() EOFError verir
    designer.main()
    assert "İyi çalışmalar!" in capsys.readouterr().out
