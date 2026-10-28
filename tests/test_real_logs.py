"""
Gerçek LTspice 26.1.1 loglarıyla (tests/data/*.log.txt) parser ve teşhis testleri.

Bu loglar gerçek simülasyonlardan alınmıştır; sentetik biçimler gerçek çıktıyla uyuşmadığında
(ör. .step tablosu başlıkları) testlerin yanlış güven vermesini önler. LTspice gerektirmez.
"""

import math
import typing
from pathlib import Path

import pytest

from ltspice_llm.diagnostics import SPICEDiagnostics, SPICEIssueType
from ltspice_llm.parser import LogParser

DATA = Path(__file__).parent / "data"


def _log(name: str):
    return LogParser.parse_text((DATA / f"{name}.log.txt").read_text(encoding="utf-8"))


def test_log_fixtures_keep_ltspice_line_endings():
    """LTspice 26.1.1 logu başlıkta LF, .meas satırlarında CRLF yazar; CRCRLF hiç yazmaz. Windows'ta metin modunda
    yazılan kayıt (``write_text`` ``newline=""`` olmadan) CRLF'yi CRCRLF yapıyordu ve araya giren boş satır .step
    ölçüm tablosunu ilk satırda kesiyordu (ölçüldü: ref26_step_tran_db'de stepped_measurements boştu)."""
    corrupted = [p.name for p in sorted(DATA.glob("*.log.txt")) if b"\r\r\n" in p.read_bytes()]
    assert corrupted == []
    # LF olan satırlar metin modunda CRLF'ye döner ve iz bırakmaz; ilk satır (başlık ya da derleyici mesajı) LTspice'ta
    # her zaman LF (yeniden kayıt, 2026-10-05: ref26_syntax_bad_line ve ref26_four_thd baştan sona LF). 13 log
    # tümüyle CRLF'ydi, yani metin modunda yazılmıştı; CRLF -> LF ile geri çevrildi.
    crlf_first_line = [p.name for p in sorted(DATA.glob("*.log.txt")) if p.read_bytes().split(b"\n", 1)[0].endswith(b"\r")]
    assert crlf_first_line == []


def test_restored_stepped_log_matches_ltspice_measure_database():
    """ref26_step_tran_db: satır sonları düzelince .step tablosu logdan okunur. Bağımsız tanık: aynı koşumun LTspice
    ölçüm veritabanı (ref26_step_tran_db.db, LTspice'ın kendi kaydı); log 12 hane yazar."""
    from ltspice_llm.parser import read_measure_db

    stepped = _log("ref26_step_tran_db").stepped_measurements
    db = read_measure_db(DATA / "ref26_step_tran_db.db")
    for name in ("tau", "vmax", "vend"):
        logged = [row[name] for row in stepped[name]]
        recorded = [r["value"] for r in db if r["name"] == name]
        assert len(logged) == len(recorded) == 3, name
        assert logged == pytest.approx(recorded, rel=1e-9), name


# ------------------------------------------------------------------- .step tabloları
def test_step_table_uses_value_column_not_last_column():
    """Gerçek başlık 'step MAX(V(out)) FROM TO': istatistik FROM/TO değil ölçüm değeri üzerinden hesaplanır."""
    parsed = _log("step_param_multi")
    stats = parsed.step_statistics["v_max"]
    assert stats["min"] == pytest.approx(1.776817, abs=1e-6)
    assert stats["max"] == pytest.approx(3.588345, abs=1e-6)
    assert stats["count"] == 4
    assert stats["std"] > 0.5  # eski hata: FROM/TO sütunu -> std=0
    assert parsed.measurements["v_max"] == pytest.approx(stats["mean"])


def test_step_rows_carry_step_parameters_from_step_lines():
    parsed = _log("step_param_multi")
    assert parsed.step_parameters == [
        {"rv": 1000.0, "cv": 1e-06},
        {"rv": 2000.0, "cv": 1e-06},
        {"rv": 1000.0, "cv": 2e-06},
        {"rv": 2000.0, "cv": 2e-06},
    ]
    rows = parsed.stepped_measurements["v_max"]
    assert [(r["step"], r["rv"], r["cv"]) for r in rows] == [
        (1, 1000.0, 1e-06), (2, 2000.0, 1e-06), (3, 1000.0, 2e-06), (4, 2000.0, 2e-06),
    ]
    assert rows[0]["v_max"] == pytest.approx(3.58834481239)
    assert rows[0]["to"] == 0.003  # FROM/TO sütunları korunur ama değer sütunu değildir


def test_step_table_header_variants_are_all_resolved():
    """Başlık bazen ifade (PP(V(out))), bazen 'V(out)' + 'at', bazen ölçüm adı (v_tr), bazen 'V(out)=2' olur."""
    parsed = _log("step_param_multi")
    assert parsed.stepped_measurements["v_at"][0]["v_at"] == pytest.approx(3.16060311305)
    assert parsed.stepped_measurements["v_at"][0]["at"] == 0.001
    assert parsed.stepped_measurements["v_pp"][0]["v_pp"] == pytest.approx(3.58834481239)
    assert parsed.stepped_measurements["v_avg"][0]["v_avg"] == pytest.approx(2.13721926316)
    assert parsed.stepped_measurements["v_tr"][0]["v_tr"] == pytest.approx(0.000405465305986)
    assert parsed.stepped_measurements["t_x"][0]["t_x"] == pytest.approx(0.00051082681004)


def test_failed_step_becomes_none_and_is_excluded_from_statistics():
    parsed = _log("step_param_multi")
    rows = parsed.stepped_measurements["t_x"]
    assert rows[3]["t_x"] is None  # LTspice 4. adımda 'failed' yazmış
    stats = parsed.step_statistics["t_x"]
    assert stats["count"] == 3
    assert stats["failed_steps"] == 1
    assert any("t_x" in w and "başarısız" in w for w in parsed.warnings)


def test_step_ac_complex_cells_are_decoded_like_single_measurements():
    parsed = _log("step_temp_ac")
    assert parsed.step_parameters == [{"temp": -40.0}, {"temp": 22.5}, {"temp": 85.0}]
    assert parsed.stepped_measurements["g_at"][0]["g_at"] == pytest.approx(-1.44507011621)  # mag(): dB
    assert parsed.stepped_measurements["f3"][0]["f3"] == pytest.approx(159.174892572)
    # ph(): LTspice kutupsal (dB,açı) yazar; gerçel değer tekil .meas ile aynı kuralla çözülür.
    # RC (1k, 1u), 100 Hz: faz = -atan(100 / 159.155) = -32.14 derece
    assert parsed.stepped_measurements["ph_at"][0]["ph_at"] == pytest.approx(-32.14, abs=0.05)


def test_step_notice_warning_is_added_when_measurements_are_averaged():
    parsed = _log("step_param_multi")
    assert any("adımların ortalamasıdır" in w for w in parsed.warnings)


# --------------------------------------------------------------------- FAIL'ed ölçümler
def test_failed_measurement_is_reported_not_silently_dropped():
    parsed = _log("meas_fail")
    assert parsed.failed_measurements == ["v_never", "v_never2"]
    assert "v_never" not in parsed.measurements
    assert parsed.measurements["v_ok"] == pytest.approx(3.58834471825)
    assert parsed.errors == []
    assert len([w for w in parsed.warnings if "FAIL'ed" in w]) == 2


def test_failed_measurement_in_stepped_run():
    parsed = _log("step_meas_fail")
    assert parsed.failed_measurements == ["v_never"]
    assert parsed.step_statistics["v_max"]["count"] == 2


# ------------------------------------------------- adı hata/uyarı sözcüğüyle biten ölçüm
def test_measurement_named_like_an_error_or_warning_is_a_measurement():
    """ref26_meas_names (examples/14 ``measnames``, LTspice 26.1.1): ref26_flow_rc devresine aynı ifadeli ölçümler
    eklendi. Eski hata: satır ölçüm olarak tanınmadan önce "error:" / "warning:" alt dizesi arandı; 'gain_error'
    hata sayılıp koşum FAILED gösterdi, ölçüm kayboldu, 'temp_warning' uyarı oldu. Bağımsız tanık: aynı koşumda aynı
    ifadeyi ölçen v_max ve vr_max (LTspice'ın kendi sonucu). LTspice adı küçük harfe çevirir (netlistte Offset_Error)."""
    parsed = _log("ref26_meas_names")
    assert parsed.errors == []
    assert parsed.warnings == []
    m = parsed.measurements
    assert m["gain_error"] == m["v_max"] == pytest.approx(3.58834481239)
    assert m["temp_warning"] == m["vr_max"] == pytest.approx(4.99999749785)
    assert m["offset_error"] == 0.0  # elle: C boşken başlar, V(out) en düşük 0 V
    # Saf WHEN, eşik bir ifade ('t_error: V(out)=v_max/2 AT t'; test-reviewer): satırda sayısal sonuç yok, değer AT'den
    # sonra. Elle: 0-1 ms dolarken V(out) = 5(1 - e^(-(t - Tr/2)/τ)), τ = 1 ms; 1 ns'lik rampa (Tr) τ ≫ Tr iken birinci
    # mertebede Tr/2 gecikmedir -> t = 0.5 ns - 1 ms·ln(1 - v_max/10).
    expected = 0.5e-9 - 1e-3 * math.log(1 - m["v_max"] / 10)
    assert m["t_error"] == m["t_warning"] == m["t_half"] == pytest.approx(expected, rel=1e-6)
    assert set(m) == {"v_max", "gain_error", "offset_error", "temp_warning", "vr_max", "t_half", "t_error", "t_warning"}


def test_ltspice_writes_measurement_names_in_lowercase():
    """Ayrıştırıcının ölçüm satırını tanıma ölçütünün dayanağı: netlistte 'Offset_Error' yazılan ad logda küçük harfle
    geçer (ref26_meas_names, LTspice 26.1.1). LTspice kendi iletilerini büyük harfle başlatır ('ERROR:', 'Circuit:')."""
    text = (DATA / "ref26_meas_names.log.txt").read_text(encoding="utf-8")
    assert "Offset_Error" in (DATA / "ref26_meas_names.net").read_text(encoding="utf-8")
    assert "Offset_Error" not in text and text.count("\noffset_error: MIN(V(out))=") == 1


def test_netlist_path_in_the_log_is_not_scanned_for_error_words():
    """ref26_path_keyword (examples/14 ``measnames``): çalışma klasörünün adı 'Simulation Failed cases'. LTspice
    netlistin yolunu 'Circuit:' satırına ve 'Files loaded:' listesine yazar. Eski hata: "simulation failed" alt dizesi
    bu satırlarda da arandı, başarılı koşum FAILED göründü. Tanık: aynı devrenin ref26_flow_rc kaydı (aynı ölçümler)."""
    parsed = _log("ref26_path_keyword")
    assert parsed.errors == []
    assert parsed.circuit_title == r"C:\ws\Simulation Failed cases\ref26_path_keyword.net"
    assert parsed.measurements == _log("ref26_flow_rc").measurements


def test_ltspice_error_line_is_still_an_error():
    """Kayıtlardaki tek LTspice 'ERROR:' satırı (ref26_floating_current_source) ölçüm biçimine uymaz (sayısal sonuç yok)
    ve hata kalır; ölçüm satırını taramadan ayırmak onu etkilemez."""
    parsed = _log("ref26_floating_current_source")
    assert parsed.errors.count("ERROR: Node n is floating and connected to current source B1") == 1
    assert parsed.measurements == {}


# ------------------------------------------------------------------------- teşhis
def test_parallel_voltage_sources_log_is_diagnosed():
    parsed = _log("parallel_sources")
    assert parsed.errors  # 'over-defined' satırları hata olarak yakalanır
    report = SPICEDiagnostics.diagnose("", errors_list=parsed.errors)
    assert report.issue_type == SPICEIssueType.PARALLEL_VOLTAGE_SOURCES


# ------------------------------------------------------------------ tip ipuçları
def test_parser_type_hints_resolve():
    """`Tuple` import'u eksikken typing.get_type_hints NameError veriyordu."""
    typing.get_type_hints(LogParser._parse_stepped_measurements)
    typing.get_type_hints(LogParser.parse_text)


def test_utf16_log_roundtrip_through_read_text(tmp_path):
    p = tmp_path / "x.log"
    p.write_bytes('Measurement "m" FAIL\'ed\n'.encode("utf-16-le"))  # LTspice XVII biçimi (BOM'suz)
    assert LogParser.read_text(p).strip() == 'Measurement "m" FAIL\'ed'
    assert LogParser.parse_file(p).failed_measurements == ["m"]
