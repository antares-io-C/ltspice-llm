"""
Örnek betiklerin (``examples/``) testleri. Örnekler yalnızca elle koşuluyordu: paketin bir API değişikliği bir örneği
sessizce kırabiliyordu.

- `test_example_imports_without_running_main`: her örnek ``main`` koşmadan içe aktarılır. Kaldırılan ya da adı
  değişen bir API burada kırılır.
- LTspice kuruluysa örnekler ayrı süreçte gerçekten koşar (``--full``), raporlarını geçici klasöre yazar.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ltspice_llm.adapter import LTSpiceAdapter

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
ALL_EXAMPLES = sorted(p.stem for p in EXAMPLES.glob("[0-9][0-9]_*.py"))


@pytest.fixture
def load_example(monkeypatch):
    """Örneği ``main``'i koşmadan, her seferinde yeni bir modül olarak yükler; sys.path değişiklikleri geri alınır."""
    monkeypatch.setattr(sys, "path", [str(EXAMPLES), *sys.path])

    def load(stem):
        spec = importlib.util.spec_from_file_location(f"example_{stem}", EXAMPLES / f"{stem}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    return load


def test_every_numbered_example_is_listed():
    assert len(ALL_EXAMPLES) == 14 and ALL_EXAMPLES[0].startswith("01_") and ALL_EXAMPLES[-1].startswith("18_")


@pytest.mark.parametrize("stem", ALL_EXAMPLES)
def test_example_imports_without_running_main(stem, load_example):
    module = load_example(stem)
    assert callable(module.main)


def test_reports_go_where_the_environment_says(load_example, tmp_path, monkeypatch):
    """Testler örnek raporlarını geçici klasöre yazdırır; kullanıcının reports/ klasöründeki raporlar ezilmez."""
    load_example("12_ltspice_reference_checks")   # examples/ klasörünü sys.path'e koyar
    import study_helpers

    monkeypatch.setenv(study_helpers.ENV_REPORT_DIR, str(tmp_path))
    assert study_helpers.report_path("17_gemini_review_3.html") == tmp_path / "17_gemini_review_3.html"
    assert study_helpers.report_option(["--rapor", "a/b.html"], "x.html") == Path("a/b.html")
    monkeypatch.delenv(study_helpers.ENV_REPORT_DIR)
    assert study_helpers.report_path("x.html") == ROOT / "reports" / "x.html"


# ------------------------------------------------------------------- LTspice ile uçtan uca (check.py --full)
#: Hızlı kip argümanları: --hizli Gear çapraz kontrolünü ya da ağır ölçümü atlar.
_FAST_ARGS = {
    "15_hotplug_damping_validation": ["--hizli"],
    "16_process_and_memory_hardening": ["--hizli"],
    "17_gemini_review_3": ["--hizli"],
}
#: Raporunu LTSPICE_LLM_REPORT_DIR'e yazan örnekler
_OUTPUTS = {stem: [f"{stem}.html", f"{stem}.parca.html"] for stem in (
    "12_ltspice_reference_checks", "15_hotplug_damping_validation", "16_process_and_memory_hardening",
    "17_gemini_review_3", "18_gemini_review_4")}
#: 14, testlerin yeniden oynattığı gerçek LTspice kayıtlarını (tests/data) yeniden yazar: test paketinde koşulmaz.
RUNNABLE = [stem for stem in ALL_EXAMPLES if not stem.startswith("14_")]


@pytest.fixture(scope="module")
def ltspice():
    try:
        return LTSpiceAdapter()
    except FileNotFoundError:
        pytest.skip("LTspice bu makinede kurulu değil")


@pytest.mark.examples
@pytest.mark.timeout(900)
@pytest.mark.parametrize("stem", RUNNABLE)
def test_example_runs_end_to_end_with_ltspice(stem, ltspice, tmp_path):
    """Örnek kullanıcının koştuğu gibi kendi sürecinde koşar ve raporunu geçici klasöre yazar (reports/ ezilmez)."""
    if stem.startswith("16_") and importlib.util.find_spec("psutil") is None:
        pytest.skip("örnek 16 psutil ister (paketin bağımlılığı değil)")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", MPLBACKEND="Agg", LTSPICE_LLM_REPORT_DIR=str(tmp_path))
    proc = subprocess.run([sys.executable, str(EXAMPLES / f"{stem}.py"), *_FAST_ARGS.get(stem, [])], cwd=tmp_path,
                          env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=840, check=False)
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-3000:]
    assert "Traceback" not in proc.stderr, proc.stderr[-3000:]
    # araç çağıran örnekler (03, 05, 08) hata yanıtını yutup 0 ile çıkabilir: API kırılması bu iletilerle görünür
    assert "Bilinmeyen araç" not in proc.stdout and "Araç yürütme hatası" not in proc.stdout, proc.stdout[-3000:]
    assert sorted(p.name for p in tmp_path.iterdir() if p.suffix in (".html", ".png")) == _OUTPUTS.get(stem, [])
