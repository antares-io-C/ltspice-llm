"""
Parametrik ve Stepped Simülasyon (.step) Log Ayrıştırma Testleri
"""

import pytest
from ltspice_llm.parser import LogParser


def test_parse_stepped_measurement_table():
    log_text = """
    Circuit: * Parametric Sweep Circuit
    .step r1 10000 30000 10000

    Measurement: v_max
      step	r1	v_max
         1	10000	3.123
         2	20000	3.845
         3	30000	4.210

    Measurement: t_rise
      step	r1	t_rise
         1	10000	1.2e-06
         2	20000	2.4e-06
         3	30000	3.6e-06

    Total elapsed time: 0.125 seconds.
    """
    parsed = LogParser.parse_text(log_text)

    # Ölçümler listesi
    assert "v_max" in parsed.stepped_measurements
    assert len(parsed.stepped_measurements["v_max"]) == 3
    assert parsed.stepped_measurements["v_max"][0]["step"] == 1
    assert parsed.stepped_measurements["v_max"][0]["r1"] == 10000.0
    assert parsed.stepped_measurements["v_max"][0]["v_max"] == 3.123

    # İstatistikler
    assert "v_max" in parsed.step_statistics
    stats = parsed.step_statistics["v_max"]
    assert stats["min"] == 3.123
    assert stats["max"] == 4.210
    assert stats["count"] == 3
    assert stats["mean"] == pytest.approx((3.123 + 3.845 + 4.210) / 3.0)

    # Tekil ölçüm olarak ortalama aktarılmalı
    assert parsed.measurements["v_max"] == stats["mean"]


def test_parse_temperature_stepped_table():
    log_text = """
    Circuit: * Temperature Sweep
    .step temp -40 85 25

    Measurement: f_3db
      step	temp	f_3db
         1	-40	9500
         2	-15	9750
         3	10	10000
         4	35	10200
         5	60	10350
         6	85	10450
    """
    parsed = LogParser.parse_text(log_text)

    assert "f_3db" in parsed.stepped_measurements
    assert len(parsed.stepped_measurements["f_3db"]) == 6
    assert parsed.step_statistics["f_3db"]["min"] == 9500.0
    assert parsed.step_statistics["f_3db"]["max"] == 10450.0
    assert parsed.step_statistics["f_3db"]["range"] == 950.0
