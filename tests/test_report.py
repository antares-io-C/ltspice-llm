"""`ltspice_llm.report` testleri: LTspice gerektirmez, sonuçlar sahte `SimulationResult` benzeri nesnelerdir."""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from ltspice_llm.report import (
    BarChart, Column, Effect, LineChart, SimulationReport, Study, Table, Trace, _decimate, format_value, log_facts,
)
from ltspice_llm.adapter import SimulationResult
from ltspice_llm.report import Finding, read_trace


@pytest.mark.parametrize("value, args, expected", [
    (0.0657, ("mW", 1e-3, 0), "66 mW"),
    (-0.73, ("V", 1.0, 2), "−0.73 V"),
    (0.31, ("V", 1.0, 2, True), "+0.31 V"),
    (-0.00004, ("V", 1.0, 3, True), "0.000 V"),   # sıfıra yuvarlanan değer işaretsiz
    (None, ("V",), "—"),
    (float("nan"), ("V",), "—"),
])
def test_format_value(value, args, expected):
    assert format_value(value, *args) == expected


def test_effect_direction_and_change_text():
    better = Effect("100 µF", "5 V girişi", "tepe", 7.22, 5.45)
    assert better.verdict() == ("good", "iyileşti")
    assert better.change_text() == "−1.77 V (−25 %)"
    assert Effect("diyot", "giriş", "5 V", 4.98, 4.67, better="higher").verdict() == ("serious", "kötüleşti")
    same = Effect("USB 4.5 V", "giriş", "3.3 V", 3.2851, 3.2849, decimals=3, better="higher")
    assert same.verdict() == ("info", "değişmedi")
    assert same.change_text() == "0.000 V (0 %)"
    assert Effect("x", "y", "z", None, 1.0).verdict() == ("warn", "ölçülemedi")


def test_decimate_keeps_peaks_inside_range():
    x = np.linspace(0, 1e-3, 200_001)
    y = np.sin(2 * np.pi * 5e3 * x)
    y[123_457] = 9.0                                   # tek örneklik tepe
    xd, yd = _decimate(x, y, 0, 1e-3, buckets=300)
    assert len(xd) <= 2 * 300 + 2
    assert yd.max() == 9.0 and np.all(np.diff(xd) >= 0)


def _run(tmp_path, name, threads=3, method="trap", warnings=()):
    log = tmp_path / f"{name}.log"
    log.write_text(f"LTspice 26.1.1\nMaximum thread count: {threads}\nmethod = {method}\n"
                   "Total elapsed time: 0.250 seconds.\n", encoding="utf-8")
    return SimpleNamespace(success=True, warnings=list(warnings), errors=[], log_path=log,
                           netlist_path=tmp_path / f"{name}.net", elapsed_seconds=0.4)


def _report(tmp_path):
    rep = SimulationReport("Deneme Kartı", "Deneme <kartı>", "alt başlık", meta=[("LTspice", "26.1.1")])
    rep.add_tile("Kayıp", "66 mW", "not")
    rep.add_decision("good", "Uygula", "100 µF koy.")
    t = np.linspace(0, 1e-4, 2001)
    rep.add_study(Study(
        "T1", "Takma anı", question="Neden `V(v5)`?", method="Nasıl **tek değişken**?",
        charts=[LineChart("5 V barası", [Trace("<script>alert(1)</script>", t, 5 + np.exp(-t / 2e-5), slot=2,
                                               label_extremes=("max",)),
                                         Trace("eski", t, 5 + 0 * t, emphasis=False)],
                          references=[(6.0, "6 V")]),
                BarChart("Kayıp", [("a", 0.066), ("b", 0.265)], unit="mW", scale=1e-3, highlight="a")],
        table=Table([Column("v5_pk", "Tepe", "V", decimals=2)], [("seramik", {"v5_pk": 7.22})]),
        effects=[Effect("C_bulk", "5 V", "tepe", 7.22, 5.45)],
    ))
    rep.add_runs("T1", [("seramik", _run(tmp_path, "a")), ("elko", _run(tmp_path, "b", warnings=["uyarı"]))])
    rep.add_check("Gear farkı", "42 µV", "good", "ayrıntı")
    rep.assumptions = [("LDO", "davranışsal")]
    rep.open_items = ["arka ışık akımı"]
    return rep


def test_report_html_contains_every_section_and_escapes_labels(tmp_path):
    page = _report(tmp_path).to_html()
    assert page.startswith("<!doctype html>") and "<title>Deneme Kartı</title>" in page
    for anchor in ("ozet", "etkiler", "test-t1", "guvenilirlik", "varsayimlar"):
        assert f'id="{anchor}"' in page
    assert "<script>alert(1)</script>" not in page          # seri adı güvenilmeyen veri
    assert "Deneme &lt;kartı&gt;" in page
    assert "<code>V(v5)</code>" in page and "<strong>tek değişken</strong>" in page
    assert "−1.77 V (−25 %)" in page and "1 / 2" not in page
    assert "2 / 2" in page                                   # başarılı koşum denetimi
    assert not re.search(r"\b(None|nan|inf)\b", page)


def test_report_chart_payload_is_valid_json(tmp_path):
    page = _report(tmp_path).to_html()
    blobs = re.findall(r'<script type="application/json" class="chart-data">(.*?)</script>', page, re.S)
    assert len(blobs) == 1
    data = json.loads(blobs[0])
    assert [s["name"] for s in data["series"]] == ["<script>alert(1)</script>", "eski"]
    assert data["series"][0]["color"] == "var(--s2)" and data["series"][1]["color"] == "var(--s0)"
    assert data["xu"] == "µs"


def test_report_fragment_and_save(tmp_path):
    rep = _report(tmp_path)
    fragment = rep.to_html(standalone=False)
    assert "<!doctype" not in fragment and "<body>" not in fragment and fragment.startswith("<title>")
    path = rep.save(tmp_path / "out" / "r.html")
    assert path.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_report_loads_nothing_from_the_network(tmp_path):
    """
    SECURITY.md "HTML reports": rapor çevrimdışı açılır, açanın tarayıcısını üçüncü bir tarafa bağlamaz; eskiden
    Google Fonts'a üç bağlantı vardı. Etiket/öznitelik yasak listesi fetch(), srcset ve SVG <image> gibi biçimleri
    kaçırıyordu (test-reviewer 2026-10-28). Değişmez sınanır: sayfada ne ağ adresi şeması ne protokolsüz "//" var;
    "\\\\sunucu" ve "/\\sunucu" da yok (file:// ile açılan sayfada UNC yolu olur, Windows SMB isteği gönderir).
    """
    rep = _report(tmp_path)
    for page in (rep.to_html(), rep.to_html(standalone=False)):
        assert "//" not in page and not re.search(r"(?i)\b(?:https?|wss?|ftp):", page)
        assert "\\\\" not in page and "/\\" not in page


def test_log_facts_reads_thread_count_and_method(tmp_path):
    facts = log_facts(_run(tmp_path, "g", threads=4, method="Gear"))
    assert facts == {"threads": 4, "method": "Gear", "solver_seconds": 0.25}
    assert log_facts(SimpleNamespace(log_path=None))["threads"] is None


# ------------------------------------------------------------------- read_trace, yeni etkiler
FLOW_RAW = Path(__file__).parent / "data" / "ref26_flow_rc.raw"   # LTspice 26.1.1, examples/14 (.tran 3m)


def test_read_trace_reads_the_fixture_case_insensitively_from_a_path_or_a_result():
    x, y = read_trace(FLOW_RAW, "V(out)")
    x2, y2 = read_trace(SimulationResult(success=True, netlist_path=FLOW_RAW.with_suffix(".net"), raw_path=FLOW_RAW),
                        "v(OUT)")
    assert np.array_equal(x, x2) and np.array_equal(y, y2)
    assert x[0] == 0 and x[-1] == pytest.approx(3e-3) and np.all(np.diff(x) >= 0)   # .tran 3m, monoton zaman ekseni
    assert y.max() == pytest.approx(3.58834481239, rel=1e-6)   # aynı koşumun .meas MAX V(out) değeri (log)


def test_read_trace_difference_of_a_node_with_itself_is_zero_and_unknown_trace_raises():
    _, d = read_trace(FLOW_RAW, "V(out, out)")
    assert np.all(d == 0)
    with pytest.raises(KeyError, match=r"V\(nope\)"):
        read_trace(FLOW_RAW, "V(nope)")


def test_findings_section_shows_cause_handling_and_result():
    rep = SimulationReport("t", "t", "s")
    assert 'id="yeni-etkiler"' not in rep.to_html(standalone=False)
    rep.add_finding(Finding("Takma anı", "neden <x>", "nasıl", "sonuç", "warn", "dikkat"))
    page = rep.to_html(standalone=False)
    assert 'id="yeni-etkiler"' in page and "neden &lt;x&gt;" in page
    assert "<dt>Nasıl ele alındı</dt><dd>nasıl</dd>" in page and "<dd>sonuç</dd>" in page
    assert "<h3>Takma anı</h3>" in page and "dikkat" in page
    assert "<dt>Neden ortaya çıktı</dt><dd>neden &lt;x&gt;</dd>" in page
    assert "<dt>Simülasyon sonucu</dt><dd>sonuç</dd>" in page


def test_read_trace_difference_matches_ltspice_meas_and_op_raw_has_no_axis():
    _, d = read_trace(FLOW_RAW, "V(in,out)")
    assert d.max() == pytest.approx(4.99999749785, rel=1e-6)   # aynı koşumun .meas MAX V(in,out) değeri (log)
    with pytest.raises(ValueError, match="eksen yok"):
        read_trace(FLOW_RAW.with_name("ref26_flow_op.raw"), "V(out)")   # .op koşumu (examples/14)


# ------------------------------------------------------------------- etkiler: karar gösterilen sayıyla tutarlı
def test_effect_verdict_agrees_with_the_displayed_change():
    """'değişmedi' yalnızca gösterilen değişim sıfıra yuvarlanınca: 0.7 mV üç ondalıkta +0.001 V görünür."""
    up = Effect("x", "y", "3.3 V", 3.2850, 3.2857, decimals=3, better="higher")
    assert up.change_text() == "+0.001 V (0 %)" and up.verdict() == ("good", "iyileşti")
    flat = Effect("x", "y", "3.3 V", 3.2850, 3.2854, decimals=3, better="higher")
    assert flat.change_text() == "0.000 V (0 %)" and flat.verdict() == ("info", "değişmedi")
    # better=None: yön yargısı yok (ör. arka ışık akımı, R1 köprüden dirence)
    assert Effect("R1", "y", "akım", 0.038, 0.030, "mA", 1e-3, 1, better=None).verdict() == ("info", "değişti")
    # yarım değer: gösterim çifte yuvarlar (round(0.5) = 0), karar da gösterilen sıfırla tutarlı (test-reviewer)
    half = Effect("Isınma", "y", "z", 25.0, 25.5, "°C", decimals=0)
    assert half.change_text() == "0 °C (+2 %)" and half.verdict() == ("info", "değişmedi")
    assert Effect("x", "y", "z", 1.0, 1.5, decimals=0).verdict() == ("info", "değişmedi")


def test_effect_percent_is_relative_to_the_magnitude_of_before():
    """Elle: −2.00 V → −1.00 V, değişim +1.00 V = |−2.00|'nin %50'si."""
    assert Effect("x", "y", "SCK dip", -2.0, -1.0, better="higher").change_text() == "+1.00 V (+50 %)"


# ------------------------------------------------------------------- güvenilirlik bölümü
def test_auto_checks_flag_a_failed_run_and_count_runs_with_warnings(tmp_path):
    """GEMINI.md rapor tuzakları: başarısız koşum 'başarılı koşum' denetimini hata gösterir. Uyarılar koşum
    kaydında görünür ve uyarı veren koşumlar (hatalı olanlardan ayrı) sayılır."""
    failed = _run(tmp_path, "d")
    failed.success, failed.errors = False, ["Simulation Failed: Iteration limit reached"]
    rep = SimulationReport("t", "t")
    # uyarı metinleri raporun kendi açıklamalarında geçmeyen biçimde: koşum kaydında görünmeleri sınanıyor
    rep.add_runs("T1", [("a", _run(tmp_path, "a")),
                        ("b", _run(tmp_path, "b", warnings=["WARNING: Less than two connections to node vout",
                                                       "ikinci uyarı: koşum yine bir kez sayılır"])),
                        ("c", _run(tmp_path, "c", warnings=["Tek bir pine bağlı düğüm: nc1 (R9)"])), ("d", failed)])
    checks = {c.name: (c.value, c.status) for c in rep._auto_checks()}
    assert checks["Başarılı koşum (logdan)"] == ("3 / 4", "critical")
    assert checks["Uyarı veren koşum"] == ("2", "warn")
    page = rep.to_html(standalone=False)
    for issue in ("WARNING: Less than two connections to node vout", "Tek bir pine bağlı düğüm: nc1 (R9)",
                  "Simulation Failed: Iteration limit reached"):
        assert page.count(issue) == 1, issue


def test_report_says_simulation_not_measurement_unless_told_otherwise():
    """GEMINI.md: donanımda ölçülmediyse 'simülasyon' de. Varsayılan rapor bunu başta söyler."""
    assert "Buradaki sayılar simülasyondur, donanım ölçümü değildir." in SimulationReport("t", "t").to_html()
    assert 'class="disclaimer"' not in SimulationReport("t", "t", disclaimer="").to_html()


def test_every_internal_link_points_to_a_section_of_the_page(tmp_path):
    """İçindekiler ve etki tablosundaki test bağlantıları sayfadaki bir bölüme gider (boş raporda da)."""
    bare = SimulationReport("t", "t")
    bare.add_study(Study("T 2", "etkisiz test", "q", "m"))
    for page in (_report(tmp_path).to_html(), bare.to_html()):
        links = re.findall(r'href="#([^"]+)"', page)
        assert links and set(links) <= set(re.findall(r'id="([^"]+)"', page))


def test_measurement_table_highlights_only_the_chosen_variant():
    rep = SimulationReport("t", "t")
    rep.add_study(Study("T1", "s", "q", "m", table=Table([Column("v", "Tepe", "V")],
                                                         [("a", {"v": 1.0}), ("b", {"v": 2.0}), ("c", {"v": 3.0})],
                                                         highlight="b")))
    rows = re.findall(r'<tr( class="hi")?><th scope="row">(\w)</th>', rep.to_html(standalone=False))
    assert rows == [("", "a"), (' class="hi"', "b"), ("", "c")]


def test_bar_widths_are_proportional_and_a_negative_or_missing_value_draws_no_bar():
    """Elle: 0.066 / 0.264 = %25; çubuk sıfırdan başlar, negatif ve ölçülemeyen değerin genişliği %0 (CSS yine
    2 px'lik işaret çizer)."""
    rep = SimulationReport("t", "t")
    rep.add_study(Study("T1", "s", "q", "m", charts=[BarChart(
        "Kayıp", [("a", 0.066), ("b", 0.264), ("c", -0.01), ("d", None)], unit="mW", scale=1e-3)]))
    widths = re.findall(r'class="bar" style="width:([^%]+)%"', rep.to_html(standalone=False))
    assert widths == ["25.0", "100.0", "0.0", "0.0"]


# ------------------------------------------------------------------- çizgi grafik
def _chart_page(chart):
    rep = SimulationReport("t", "t")
    rep.add_study(Study("T1", "s", "q", "m", charts=[chart]))
    page = rep.to_html(standalone=False)
    data = json.loads(re.search(r'class="chart-data">(.*?)</script>', page, re.S).group(1))
    return page, data


def test_line_chart_keeps_references_in_frame_and_labels_the_visible_extreme():
    """x_range penceresi çizilir; referans çizgisi çizim alanında kalır; uç değer etiketi görünür aralığın tepesini
    yazar. Genel tepe (9 V) pencerenin hemen dışında: çizgi sürekli olsun diye seyreltme o noktayı tutar, etiket
    onu göstermemeli."""
    t = np.linspace(0, 1e-3, 1001)
    y = np.where(t < 0.5e-3, 5.0 + t * 1e3, 1.0)   # 0.5 ms'ye kadar 5 → 5.5 V rampa, sonra 1 V
    y[601] = 9.0                                    # 0.601 ms: pencerenin (0.6005 ms) ilk dış noktası
    page, data = _chart_page(LineChart("c", [Trace("v", t, y, label_extremes=("max",))], x_range=(0, 0.6005e-3),
                                       references=[(8.0, "8 V")]))
    assert (data["frame"]["x0"], data["frame"]["x1"]) == (0, 0.6005e-3)
    assert data["frame"]["y0"] <= 1.0 and data["frame"]["y1"] >= 8.0
    assert re.findall(r'class="dlabel"[^>]*>([^<]+)<', page) == ["5.50 V"]   # t = 0.499 ms: 5.499 V


def test_grey_context_series_is_drawn_under_the_emphasised_one():
    """Trace(emphasis=False) eski/temel durumdur: gri ve altta. SVG'de sonra çizilen üstte görünür."""
    t = np.linspace(0, 1e-3, 101)
    page, _ = _chart_page(LineChart("c", [Trace("A", t, 1 + t), Trace("eski", t, 2 + t, emphasis=False),
                                          Trace("B", t, 3 + t)]))
    assert re.findall(r'<polyline [^>]*style="stroke:([^"]+)"', page) == ["var(--s0)", "var(--s1)", "var(--s2)"]


def test_decimate_keeps_dips_as_well_as_peaks():
    """GEMINI.md: uzun iz kova başına min/max ile seyreltilir; LTTB tepe ve dipleri kaybediyordu."""
    x = np.linspace(0, 1e-3, 200_001)
    y = np.sin(2 * np.pi * 5e3 * x)
    y[54_321], y[123_457] = -9.0, 9.0                  # tek örneklik dip ve tepe
    _, yd = _decimate(x, y, 0, 1e-3, buckets=300)
    assert (yd.min(), yd.max()) == (-9.0, 9.0)
