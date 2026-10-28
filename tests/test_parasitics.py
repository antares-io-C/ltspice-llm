"""
Parazitik ve Derating Motoru Birim Testleri
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Beklenen değerler yerel kaynaklardan (docs/references/text, sayfa numarasıyla) ya da elle hesaptan gelir:
ADI MT-094 (IPC-2141 microstrip), ADI Basic Linear Design Bl.12 (bakır), TI SLOD006 (tel endüktansı),
AN-88 (takma anı), MT-101 (NP0/C0G). LTspice karşılaştırması: tests/test_integration_ltspice.py, examples/15.
"""

import math

import pytest

from ltspice_llm.parasitics import (
    analyze_hotplug_transient,
    calculate_trace_parasitics,
    get_cable_parasitics,
    hotplug_step_peak,
    inject_trace_parasitics_into_netlist,
    mlcc_dc_bias_derating,
)

MIL = 0.0254  # mm


def test_calculate_trace_parasitics_standard():
    # 50 mm uzunluğunda, 0.254 mm (10 mil) genişliğinde, 1.6 mm FR-4 iz, 1 oz (T = 1.4 mil). Elle (MT-094 Denk. 3-4,
    # Bl.12 s.7): Z0 = 87/sqrt(5.81) * ln(9.568/0.23876) = 133.21 Ohm; C = 0.67*5.81/ln(.) pF/in * 50/25.4 = 2.0762 pF;
    # L = Z0^2 C = 36.843 nH; R = 1.724e-8 * 0.05 / (0.254e-3 * 0.03556e-3) = 0.095436 Ohm
    res = calculate_trace_parasitics(length_mm=50.0, width_mm=0.254, height_mm=1.6, er=4.4)
    assert res.length_mm == 50.0
    assert res.r_dc == pytest.approx(0.095436, rel=1e-4)
    assert res.z0 == pytest.approx(133.212, rel=1e-4)
    assert res.l_trace == pytest.approx(36.843e-9, rel=1e-4)
    assert res.c_trace == pytest.approx(2.0762e-12, rel=1e-4)

    # to_dict ve to_spice_subckt
    d = res.to_dict()
    assert d["r_dc_ohm"] == res.r_dc and d["z0_ohm"] == res.z0
    assert d["l_trace_nh"] == pytest.approx(36.843, rel=1e-4) and d["c_trace_pf"] == pytest.approx(2.0762, rel=1e-4)
    # MT-094 Denk. 5: 1.017 * sqrt(0.475 * 4.4 + 0.67) ns/ft * 50 / 304.8 = 277.160 ps
    assert d["tpd_ps"] == pytest.approx(277.160, rel=1e-5)
    sub = res.to_spice_subckt("MY_TRACE")
    assert ".subckt MY_TRACE" in sub
    assert ".ends MY_TRACE" in sub


def test_microstrip_matches_the_mt094_example():
    """MT-094 s.3-4: W = 20 mil, 1 oz (T = 1.4 mil), H = 10 mil, er = 4.0 -> "Eq. 3 predicts Zo to be about 46 Ω";
    "136 ps/in". Elle (Denk. 3-5): Z0 = 87/sqrt(5.41) * ln(59.8/17.4) = 46.18 Ohm; tpd = 1.017*sqrt(2.57) ns/ft / 12 =
    135.87 ps/in (Denk. 6'nın 85 ps/in katsayısı 1.017/12 = 84.75'in yuvarlanmışı: 136.27); C0 = 0.67*5.41/ln(59.8/17.4)
    = 2.936 pF/in."""
    res = calculate_trace_parasitics(length_mm=25.4, width_mm=20 * MIL, height_mm=10 * MIL, copper_oz=1.0, er=4.0)
    assert res.z0 == pytest.approx(46.18, rel=1e-3)
    assert res.z0 == pytest.approx(46.0, rel=0.01)       # belgenin yuvarlanmış değeri
    assert res.tpd * 1e12 == pytest.approx(135.865, rel=1e-4)
    assert res.tpd * 1e12 == pytest.approx(136.0, rel=0.005)  # belgenin yuvarlanmış değeri
    assert res.c_trace * 1e12 == pytest.approx(2.936, rel=1e-3)
    assert res.l_trace == pytest.approx(res.z0 ** 2 * res.c_trace, rel=1e-12)  # kayıpsız hat: Z0 = sqrt(L/C)
    assert "50-100" in res.accuracy_note                  # MT-094 s.4: denklem 50-100 Ohm arasında en doğru


def test_microstrip_inside_the_accurate_range_has_no_note():
    """MT-094 s.3: aynı laminatta 75 Ohm için W ~ 8.3 mil; Z0 50-100 Ohm aralığında, not yok."""
    res = calculate_trace_parasitics(length_mm=10.0, width_mm=8.3 * MIL, height_mm=10 * MIL, copper_oz=1.0, er=4.0)
    assert 50.0 <= res.z0 <= 100.0
    assert res.accuracy_note == ""


def test_trace_resistance_matches_basic_linear_design_example():
    """ADI Basic Linear Design Bl.12 s.7-8: rho = 1.724e-6 Ohm*cm (25 °C); 0.25 mm 1 oz iz "about 19 mΩ/cm", 5 cm'si
    "nearly 0.1 Ω". Elle: 1.724e-8 * 0.05 / (0.25e-3 * 1.4 * 25.4e-6) = 0.09696 Ohm."""
    res = calculate_trace_parasitics(length_mm=50.0, width_mm=0.25, copper_oz=1.0, temp_c=25.0)
    assert res.r_dc == pytest.approx(0.09696, rel=1e-3)
    assert res.r_per_meter * 1e3 / 100 == pytest.approx(19.0, rel=0.03)  # mOhm/cm


def test_trace_subcircuit_writes_the_model_values_exactly():
    """Netliste giden değerler: R/2 = 0.0969629 / 2 = 48.4814 mOhm (elle, yukarıdaki iz) 6 haneyle yazılır (eski 3 hane
    48.5 mOhm yazıyordu: LTspice'ta R_dc %0.04 kaydı) ve bobinlere Rser=0 (LTspice varsayılanı 1 mΩ, ADI
    CIRCUIT-ELEMENTS-REFERENCE; yazılmazsa R_dc ~%2 büyür). LTspice karşılaştırması tests/test_integration_ltspice.py."""
    sub = calculate_trace_parasitics(length_mm=50.0, width_mm=0.25).to_spice_subckt().splitlines()
    assert "R1 in n1 48.4814mR" in sub
    inductors = [line for line in sub if line.startswith("L")]
    assert len(inductors) == 2 and all(line.endswith(" Rser=0") for line in inductors)
    cable = get_cable_parasitics(1.0, resistance_ohm=0.1).to_spice_subckt().splitlines()
    assert [line for line in cable if line.startswith("L")] == ["L_p m_p outp 1.2uH Rser=0", "L_n m_n outn 1.2uH Rser=0"]


def test_trace_resistance_follows_the_copper_tempco():
    """Bl.12 s.8: TC "about 0.4%/°C" -> 125 °C'de 25 °C'ye göre 1 + 0.004 * 100 = 1.4 kat."""
    cold = calculate_trace_parasitics(length_mm=50.0, width_mm=0.25, temp_c=25.0)
    hot = calculate_trace_parasitics(length_mm=50.0, width_mm=0.25, temp_c=125.0)
    assert hot.r_dc / cold.r_dc == pytest.approx(1.4, rel=1e-9)


def test_microstrip_rejects_a_geometry_outside_the_equation():
    """5.98·H <= 0.8·W + T iken ln(.) <= 0: Denk. 3 sıfır ya da negatif Z0 verir. Eski kod 5 Ohm'a kırpıyordu."""
    with pytest.raises(ValueError, match="geçersiz"):
        calculate_trace_parasitics(length_mm=10.0, width_mm=10.0, height_mm=0.1)


def test_calculate_trace_parasitics_invalid():
    with pytest.raises(ValueError):
        calculate_trace_parasitics(length_mm=-10)
    with pytest.raises(ValueError):
        calculate_trace_parasitics(length_mm=10, er=0.5)


def test_mlcc_dc_bias_derating_c0g():
    # C0G/NP0 gerilimden etkilenmez (%0 kayıp)
    r = mlcc_dc_bias_derating("10n", applied_voltage=25.0, rated_voltage=25.0, dielectric="C0G")
    assert r.loss_percent == 0.0
    assert r.effective_capacitance == 10e-9
    assert "MT-101" in r.source  # MT-101 s.6: NP0'ın düşük gerilim katsayısı; ihmal bir varsayım olarak yazılır


def test_mlcc_class2_needs_the_manufacturer_curve():
    """Eski hata: X7R/X5R/Y5V kaybı kaynaksız bir kılıf tablosundan uyduruluyordu. DC-bias kaybı parçaya özgüdür."""
    with pytest.raises(ValueError, match="dc_bias_curve"):
        mlcc_dc_bias_derating("10u", applied_voltage=16.0, rated_voltage=16.0, dielectric="X7R", package="0805")


def test_mlcc_curve_reproduces_the_an88_y5v_example_and_interpolates():
    """AN-88 s.2: "Y5V ceramic capacitors will loose 80% of the initial capacitance under rated input voltage".
    Eğri [(0 V, 1.0), (16 V, 0.2)]: 16 V'ta %80 kayıp (2 µF); 8 V'ta doğrusal enterpolasyon 0.6 -> 6 µF (elle)."""
    curve = [(0.0, 1.0), (16.0, 0.2)]
    rated = mlcc_dc_bias_derating("10u", 16.0, 16.0, dielectric="Y5V", dc_bias_curve=curve)
    assert rated.loss_percent == pytest.approx(80.0)
    assert rated.effective_capacitance == pytest.approx(2e-6)
    half = mlcc_dc_bias_derating("10u", 8.0, 16.0, dielectric="Y5V", dc_bias_curve=curve)
    assert half.effective_capacitance == pytest.approx(6e-6)
    assert mlcc_dc_bias_derating("10u", 0.0, 16.0, dielectric="Y5V", dc_bias_curve=curve).loss_percent == 0.0


def test_mlcc_rejects_voltages_outside_the_rating_or_the_curve():
    with pytest.raises(ValueError, match="anma"):
        mlcc_dc_bias_derating("10u", 20.0, 16.0, dielectric="C0G")
    with pytest.raises(ValueError, match="aralığı dışında"):
        mlcc_dc_bias_derating("10u", 12.0, 16.0, dc_bias_curve=[(0.0, 1.0), (10.0, 0.5)])


def test_mlcc_curve_order_does_not_matter_and_srf_uses_the_effective_capacitance():
    """Sırasız üç nokta [(16, 0.2), (0, 1.0), (8, 0.5)]: 4 V (0, 1.0)-(8, 0.5) arasında -> 1 - 0.5 * 4/8 = 0.75 (elle),
    10 µF -> 7.5 µF. Öz rezonans etkin kapasiteyle: f = 1 / (2 pi sqrt(1n * 7.5µ))."""
    r = mlcc_dc_bias_derating("10u", 4.0, 16.0, dielectric="X5R",
                              dc_bias_curve=[(16.0, 0.2), (0.0, 1.0), (8.0, 0.5)], esl_h=1e-9)
    assert r.effective_capacitance == pytest.approx(7.5e-6)
    assert r.srf_hz == pytest.approx(1 / (2 * math.pi * math.sqrt(1e-9 * 7.5e-6)), rel=1e-12)


def test_mlcc_parasitics_are_inputs_and_set_the_self_resonance():
    """ESR/ESL girdidir (eski kılıf tablosu kaynaksızdı). Elle: f = 1 / (2 pi sqrt(0.85n * 10n)) = 54.59 MHz."""
    r = mlcc_dc_bias_derating("10n", 5.0, 50.0, dielectric="C0G", esr_ohm=0.015, esl_h=0.85e-9)
    assert r.to_spice_string() == "10n Rser=15m Lser=850p"
    assert r.srf_hz == pytest.approx(1 / (2 * math.pi * math.sqrt(0.85e-9 * 10e-9)), rel=1e-12)
    bare = mlcc_dc_bias_derating("10n", 5.0, 50.0, dielectric="C0G")
    assert bare.to_spice_string() == "10n" and bare.srf_hz is None


def test_cable_inductance_uses_the_slod006_range():
    """TI SLOD006 s.362: "Common wires and PCB traces vary between 6 nH and 12 nH per centimeter". Gidiş + dönüş:
    1 m -> 2 * 100 cm * 12 nH/cm = 2.4 µH (varsayılan, üst uç: tutucu); 6 nH/cm ile 1.2 µH."""
    assert get_cable_parasitics(1.0).l_cable == pytest.approx(2.4e-6)
    assert get_cable_parasitics(1.0, nh_per_cm=6.0).l_cable == pytest.approx(1.2e-6)
    assert get_cable_parasitics(1.0).r_cable == 0.0  # verilmezse sönümsüz kötü durum
    sub = get_cable_parasitics(1.0).to_spice_subckt()
    assert ".subckt CABLE" in sub and "R_p" not in sub  # 0 Ohm direnç LTspice'a yazılmaz
    assert "R_p" in get_cable_parasitics(1.0, resistance_ohm=0.1).to_spice_subckt()


def test_hotplug_closed_form_by_hand():
    """5 V, 1 µF, 0.5 µH, 0.15 Ohm. Elle: Z0 = sqrt(0.5µ/1µ) = 0.70711 Ohm; zeta = 0.15/2 * sqrt(1µ/0.5µ) = 0.10607;
    V_peak = 5 (1 + exp(-pi zeta / sqrt(1 - zeta^2))) = 8.5763 V (LTspice 26.1.1: 8.5763 V, examples/15)."""
    res = analyze_hotplug_transient(v_in=5.0, c_in="1u", l_cable="0.5u", r_cable=0.15)
    assert res.z0 == pytest.approx(0.70711, rel=1e-5)
    assert res.damping_ratio == pytest.approx(0.10607, rel=1e-4)
    assert res.is_underdamped is True
    assert res.v_peak == pytest.approx(8.5763, rel=1e-4)
    assert res.recommended_damping_r == pytest.approx(2 * 0.70711 - 0.15, rel=1e-4)  # zeta = 1 için eklenecek R
    assert res.safe_margin_rating == "NO_RATING"
    d_hp = res.to_dict()
    assert "theoretical_peak_voltage" in d_hp and "assessment" in d_hp
    assert "57.2" in res.note  # AN-88 Tablo 1: donanım tepesi ideal 2·V_in sınırını aştı


def test_hotplug_without_overshoot_when_damped_beyond_critical():
    """Elle: R = 2.5·Z0 -> zeta = 1.25 >= 1: adım yanıtında aşım yok, V_peak = V_in; 2·Z0'dan büyük R'ye eklenecek
    seri direnç 0. 12 V, 10 µF, 2.4 µH."""
    z0 = math.sqrt(2.4e-6 / 10e-6)
    res = analyze_hotplug_transient(12.0, 10e-6, 2.4e-6, 2.5 * z0)
    assert res.damping_ratio == pytest.approx(1.25)
    assert res.is_underdamped is False and res.overshoot_percent == 0.0 and res.v_peak == 12.0
    assert res.recommended_damping_r == 0.0


def test_hotplug_step_peak_matches_the_ltspice_measurements():
    """LTspice 26.1.1 (examples/15 T2, Rser=0, .meas MAX V(c)): AN-88 düzeni + 47 µF / 0.44 Ω -> 37.9221 V; araç önerisi
    (4·C_in, en iyi ESR) -> 32.9387 V. Bağımsız tanık: LTspice; değerler 6 haneyle yazıldığından rel 5e-6."""
    assert hotplug_step_peak(24.0, 1e-6, 22e-6, 0.0, 47e-6, 0.44) == pytest.approx(37.9221, rel=5e-6)
    assert analyze_hotplug_transient(24.0, 22e-6, 1e-6).recommended_damped_peak == pytest.approx(32.9387, rel=5e-6)


def test_hotplug_at_exactly_critical_damping_from_the_tools_own_recommendation():
    """Aracın önerdiği toplam seri direnç 2·Z0 float'ta tam zeta = 1 verir. Elle: kritik sönümde aşım yok, V_peak = V_in
    (LTspice 26.1.1: 12.0000 V, examples/15 T3). Aşımlı dala girilseydi sqrt(1 - zeta^2) = 0'a bölünürdü."""
    for v, c_f, l_h in ((12.0, 10e-6, 2.4e-6), (24.0, 22e-6, 1e-6)):
        crit = analyze_hotplug_transient(v, c_f, l_h, analyze_hotplug_transient(v, c_f, l_h).recommended_damping_r)
        assert crit.damping_ratio == 1.0
        assert crit.is_underdamped is False and crit.v_peak == v


def test_hotplug_step_peak_matches_an_independent_ode_solution():
    """Bağımsız tanık: devrenin durum denklemleri (L di/dt = V - R i - v_c; C dv_c/dt = i - i_b; C_b dv_b/dt = i_b,
    i_b = (v_c - v_b)/ESR) scipy.integrate.solve_ivp ile çözülür. 1 V, L = C = 1 µ, seri R = 0.3, C_b = 4 µ, ESR = 0.8:
    transfer fonksiyonundaki r·(1 + C_b/C) ve R·C·Rb·Cb terimleri bu vakada görünür."""
    import numpy as np
    from scipy.integrate import solve_ivp

    l_h, c_f, r, cb, rb = 1e-6, 1e-6, 0.3, 4e-6, 0.8

    def deriv(t, y):
        i, vc, vb = y
        ib = (vc - vb) / rb
        return [(1.0 - r * i - vc) / l_h, (i - ib) / c_f, ib / cb]

    t_end = 10 * 2 * math.pi * math.sqrt(l_h * (c_f + cb))
    sol = solve_ivp(deriv, (0.0, t_end), [0.0, 0.0, 0.0], method="DOP853", rtol=1e-11, atol=1e-14, dense_output=True)
    ode_peak = float(sol.sol(np.linspace(0.0, t_end, 200001))[1].max())
    assert hotplug_step_peak(1.0, l_h, c_f, r, cb, rb) == pytest.approx(ode_peak, rel=1e-7)


def test_hotplug_recommendation_accounts_for_the_series_resistance():
    """12 V, 10 µF, 1 µH, seri R = 50 mΩ: bildirilen tepe, önerilen C_b + ESR'nin seri R dahil tepesidir; komşu ESR'ler
    daha yüksek tepe verir (ESR araması seri R'yi hesaba katar)."""
    res = analyze_hotplug_transient(12.0, 10e-6, 1e-6, 0.05)

    def peak(esr):
        return hotplug_step_peak(12.0, 1e-6, 10e-6, 0.05, res.recommended_bulk_c, esr)

    assert res.recommended_damped_peak == pytest.approx(peak(res.recommended_bulk_esr), rel=1e-9)
    for k in (0.8, 1.2):
        assert peak(k * res.recommended_bulk_esr) > res.recommended_damped_peak
    # Seri R'siz devrenin en iyi ESR'si (0.873·Z0) bu devrede en iyi değil (0.831·Z0): o ESR kesin daha yüksek tepe verir
    assert peak(analyze_hotplug_transient(12.0, 10e-6, 1e-6).recommended_bulk_esr) > res.recommended_damped_peak


def test_hotplug_recommendation_uses_the_given_bulk_ratio():
    """LTspice 26.1.1 (examples/15 T4; normalize devre, seri R = 0, ESR ızgarası 0.05·Z0 adımlı): C_b/C_in = 6 için en
    düşük tepe 1.29324·V_in, 2.136 için 1.50995·V_in. Araç ızgaranın arasını da aradığından en çok ızgara kadar ve
    %0.03 içinde."""
    for ratio, grid in ((6.0, 1.29324), (2.136, 1.50995)):
        peak = analyze_hotplug_transient(1.0, 1e-6, 1e-6, bulk_ratio=ratio).recommended_damped_peak
        assert peak <= grid * (1 + 1e-5)
        assert peak == pytest.approx(grid, rel=3e-4)


def test_hotplug_step_peak_limits_reduce_to_the_closed_form():
    """Elle sınırlar (L = C = 1 µ, Z0 = 1 Ohm, seri R = 0.3·Z0, C_b = 4·C): ESR -> sonsuz iken kol açılır ve seri R'li
    kapalı forma iner; ESR = 0 iken C_b, C_in'e paraleldir (C = 5 µ). ESR = 0 eskiden scipy BadCoefficients veriyordu."""
    def closed(c_f):
        zeta = 0.3 / 2 * math.sqrt(c_f / 1e-6)
        return 1 + math.exp(-math.pi * zeta / math.sqrt(1 - zeta * zeta))

    assert hotplug_step_peak(1.0, 1e-6, 1e-6, 0.3, 4e-6, 1e4) == pytest.approx(closed(1e-6), rel=1e-3)
    assert hotplug_step_peak(1.0, 1e-6, 1e-6, 0.3, 4e-6, 0.0) == pytest.approx(closed(5e-6), rel=1e-9)


def test_hotplug_rating_compares_the_peak_with_the_given_maximum():
    """Eski hata: kaynaksız 1.2× / 1.6× eşikleri. Karar parçanın mutlak maksimum gerilimiyle verilir (8.5763 V)."""
    assert analyze_hotplug_transient(5.0, 1e-6, 0.5e-6, 0.15, max_voltage=8.0).safe_margin_rating == "EXCEEDS_RATING"
    assert analyze_hotplug_transient(5.0, 1e-6, 0.5e-6, 0.15, max_voltage=10.0).safe_margin_rating == "WITHIN_RATING"


def test_hotplug_step_peak_reduces_to_the_closed_form_and_is_scale_free():
    """Sönüm kolu yokken adım yanıtı kapalı forma eşittir; normalize yanıt yalnızca oranlara bağlı olduğundan aynı
    oranlarla 24 V · 1 µH · 22 µF ve 12 V · 2.4 µH · 10 µF aynı tepe / V_in'i verir."""
    assert hotplug_step_peak(5.0, 0.5e-6, 1e-6, 0.15) == pytest.approx(8.576309, rel=1e-6)
    a = analyze_hotplug_transient(24.0, 22e-6, 1e-6)
    b = analyze_hotplug_transient(12.0, 10e-6, 2.4e-6)
    assert a.recommended_bulk_c == pytest.approx(4 * 22e-6)
    assert a.recommended_damped_peak / 24.0 == pytest.approx(b.recommended_damped_peak / 12.0, rel=1e-6)
    assert a.recommended_bulk_esr / a.z0 == pytest.approx(b.recommended_bulk_esr / b.z0, rel=1e-3)
    # Bildirilen tepe, önerilen C_b ve ESR'nin kendi tepesidir
    assert b.recommended_damped_peak == pytest.approx(
        hotplug_step_peak(12.0, 2.4e-6, 10e-6, 0.0, b.recommended_bulk_c, b.recommended_bulk_esr), rel=1e-9)
    # Önerilen ESR tepeyi gerçekten en aza indiriyor: komşu ESR'ler daha yüksek tepe verir
    for k in (0.8, 1.2):
        assert hotplug_step_peak(12.0, 2.4e-6, 10e-6, 0.0, b.recommended_bulk_c, k * b.recommended_bulk_esr) > \
            b.recommended_damped_peak


def test_inject_trace_parasitics_into_netlist():
    net = "* test\nV1 in 0 5\nR1 out 0 1k\n.tran 1m\n.end\n"
    tr = calculate_trace_parasitics(length_mm=25.0)
    injected = inject_trace_parasitics_into_netlist(net, "in", "out", tr)
    assert "X_TR1 in out 0 PCB_TRACE_TR1" in injected
    assert ".subckt PCB_TRACE_TR1" in injected
    assert injected.strip().endswith(".end")


def test_inject_trace_parasitics_stays_outside_an_existing_subcircuit():
    """Eski hata: ``startswith(".end")`` ``.ends``'i de yakalıyordu; iz LOAD alt devresinin içine girdi ve LTspice 26.1.1
    ana devrede in-out bağlantısı görmedi (ölçüldü: vout_max 0.505 V yerine 0 V, koşum 'başarılı')."""
    net = "* t\nV1 in 0 1\nX1 out 0 LOAD\n.subckt LOAD p n\nRl p n 50\n.ends LOAD\n.tran 1u\n.end\n"
    lines = inject_trace_parasitics_into_netlist(net, "in", "out", calculate_trace_parasitics(length_mm=25.0)).splitlines()
    ends_load = lines.index(".ends LOAD")
    assert lines.index("X_TR1 in out 0 PCB_TRACE_TR1") > ends_load
    assert lines.index(".subckt PCB_TRACE_TR1 in out gnd") > ends_load
    assert lines[-1] == ".end"


def test_injected_trace_subcircuit_does_not_short_on_user_node_names():
    """Eski hata: alt devre portları kullanıcı düğümlerinin adını alıyordu; ``n1`` adlı düğümde iç düğüm ``n1`` ile
    çakışıp ``R1 n1 n1`` (kısa devre) yazıldı."""
    injected = inject_trace_parasitics_into_netlist("* t\nV1 n1 0 1\nRl n2 0 50\n.op\n.end\n", "n1", "n2",
                                                    calculate_trace_parasitics(length_mm=25.0))
    body = injected.split(".subckt PCB_TRACE_TR1", 1)[1].split(".ends", 1)[0].splitlines()[1:]
    for line in body:
        _, node_a, node_b = line.split()[:3]  # ad, düğüm, düğüm, değer [Rser=0]
        assert node_a != node_b, line
