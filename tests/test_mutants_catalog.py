"""
scripts/mutants.py: LTspice verisine bağlı mutantların atlanması.

Haftalık iş LTspice'sız koşar. 2026-11-02 koşumunda (Actions 41431058489) 319 mutantın üçü sağ kaldı: öldüren testleri sembol
kütüphanesi ya da ADI belgeleri yokken kendiliğinden atlanıyordu. Aynı üçü bu veriler olan makinede öldü (2026-11-07'de
ölçüldü). Atlama yalnız veri gerçekten yokken olmalı: kurulu makinede bu mutantlar koşulmaya devam eder.

Makinenin durumu ``ltspice_data_dir``'in baktığı ortam değişkenleriyle kurulur (``ltspice_llm/reference.py``), test LTspice'ın
kurulu olup olmamasına bağlı değildir.
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
#: Actions 41431058489 kaydındaki sağ kalanlar, etiketleri katalogdaki gibi
WEEKLY_SURVIVORS = {
    "eski hata: LTspice'ta olmayan 'inductor' sembolü",
    "reviewer: diyotta anode/cathode yer değiştirir",
    "teşhis: ADI belgesinde olmayan bölüm adı",
}
SYMBOL_LABELS = WEEKLY_SURVIVORS - {"teşhis: ADI belgesinde olmayan bölüm adı"}


@pytest.fixture(scope="module")
def mutants():
    spec = importlib.util.spec_from_file_location("mutants_catalog", ROOT / "scripts" / "mutants.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ltspice_home(monkeypatch, tmp_path):
    """Boş ``%LOCALAPPDATA%``: LTspice'ın kurulu verisi yok. Testler içine ``LTspice/...`` koyarak veriyi ekler."""
    monkeypatch.delenv("LTSPICE_DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    return tmp_path / "LTspice"


def _add_symbols(home):
    (home / "lib" / "sym").mkdir(parents=True)


def _add_reference(home):
    (home / "reference").mkdir(parents=True)
    (home / "reference" / "TROUBLESHOOTING-GUIDE.md").write_text("# Baslik\n", encoding="utf-8")


def test_the_weekly_survivors_are_the_data_dependent_mutants(mutants):
    assert set(mutants.NEEDS_LTSPICE_DATA) == WEEKLY_SURVIVORS


def test_every_data_dependent_label_exists_once_in_the_catalog(mutants):
    """Etiket katalogda değişirse koruma sessizce düşer ve mutant yine sağ kalmış görünür."""
    labels = [m[3] for m in mutants.MUTANTS]
    assert {label: labels.count(label) for label in WEEKLY_SURVIVORS} == dict.fromkeys(WEEKLY_SURVIVORS, 1)


def test_missing_data_follows_what_the_tests_look_for(mutants, ltspice_home):
    """tests/test_integration_reference.py ``symbol_dir``: ``lib/sym`` klasörü; test_ltspice_reference.py: ``ReferenceDocs.available``."""
    assert mutants.missing_ltspice_data() == {mutants.SYMBOLS, mutants.REFERENCE}
    _add_symbols(ltspice_home)
    assert mutants.missing_ltspice_data() == {mutants.REFERENCE}
    _add_reference(ltspice_home)
    assert mutants.missing_ltspice_data() == set()


def test_symbols_are_missing_when_only_the_reference_is_installed(mutants, ltspice_home):
    """Veri klasörünün varlığı yetmez: testler ``lib/sym``'e bakar."""
    _add_reference(ltspice_home)
    assert mutants.missing_ltspice_data() == {mutants.SYMBOLS}


def test_reference_folder_without_documents_counts_as_missing(mutants, ltspice_home):
    """``ReferenceDocs.available`` en az bir ``.md`` ister; boş klasörde test atlanır, mutant sağ kalmış görünürdü."""
    (ltspice_home / "reference").mkdir(parents=True)
    assert mutants.REFERENCE in mutants.missing_ltspice_data()


def test_only_mutants_needing_the_missing_data_are_skipped(mutants):
    run, skipped = mutants.split_by_ltspice_data(mutants.MUTANTS, {mutants.SYMBOLS})
    assert {m[3] for m in skipped} == SYMBOL_LABELS
    assert len(run) + len(skipped) == len(mutants.MUTANTS)
    assert not any(m[3] in SYMBOL_LABELS for m in run)


def test_nothing_is_skipped_where_the_data_exists(mutants):
    run, skipped = mutants.split_by_ltspice_data(mutants.MUTANTS, set())
    assert skipped == [] and run == list(mutants.MUTANTS)


def test_main_reports_skipped_mutants_and_does_not_fail(mutants, ltspice_home, capsys):
    """Yalnız atlanan seçilince kopya açılmaz, test koşulmaz; sonuç başarılıdır ve atlanan gizlenmez."""
    assert mutants.main(["belgesinde olmayan bölüm"]) == 0
    out = capsys.readouterr().out
    assert out.count("ATLANDI") == 1
    assert out.count(mutants.REFERENCE) == 1
    assert out.splitlines()[-1] == "0/0 mutant öldü, 1 atlandı"
