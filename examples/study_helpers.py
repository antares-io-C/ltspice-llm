"""
Rapor yazan örneklerin (12, 15-18) ortak rapor yolu ve kaydı (GEMINI.md "Simülasyon raporu").

- `report_path`, `report_option`, `save_report`: rapor yolu, ``--rapor <yol>`` seçeneği ve kayıt (``.html`` ile
  iskeletsiz ``.parca.html``). ``LTSPICE_LLM_REPORT_DIR`` raporları başka klasöre yazdırır (testler geçici klasör verir).
"""

import os
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_REPORT_DIR = "LTSPICE_LLM_REPORT_DIR"


# --------------------------------------------------------------------------- rapor yolu ve kayıt
def report_path(name):
    """``reports/<name>``; ``LTSPICE_LLM_REPORT_DIR`` verilmişse o klasörde."""
    return Path(os.environ.get(ENV_REPORT_DIR) or ROOT / "reports") / name


def report_option(args, name):
    """``--rapor <yol>`` verilmişse o yol, yoksa `report_path`."""
    return Path(args[args.index("--rapor") + 1]) if "--rapor" in args else report_path(name)


def save_report(report, out, open_browser=False):
    """Raporu ve yanına iskeletsiz kopyasını (yayımlama/gömme için) yazar; yolu basar."""
    path = report.save(out)
    report.save(out.with_suffix(".parca.html"), standalone=False)
    print(f"\nRapor: {path}")
    if open_browser:
        webbrowser.open(path.as_uri())
    return path
