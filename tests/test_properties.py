"""
Saf fonksiyonlar için property testleri (Hypothesis).

Örnek tabanlı testler %100 branch coverage'da bile iki hatayı kaçırıyordu: serideki bir değerin floor/ceil
kipinde komşusuna atlaması (330n E6 ceil -> 470n) ve LTspice'ın okuduğu ".1u" / "4k7" yazımlarında ValueError.
Buradaki beklentiler koddan değil kaynaktan gelir: ADI SPICE-SYNTAX-REFERENCE çarpan tablosu, LTspice 26.1.1
ölçümleri (examples/12_ltspice_reference_checks.py) ve IEC 60063'ün geometrik seri tanımı.

Daha çok örnek: ``HYPOTHESIS_PROFILE=ci python -m pytest tests/test_properties.py``
"""

import math
import re

import pytest
from hypothesis import assume, example, given
from hypothesis import strategies as st

from ltspice_llm.adapter import _set_element_value
from ltspice_llm.netlist import insert_before_end
from ltspice_llm.quantizer import SERIES_MAP, format_spice_value, parse_spice_value, snap_to_e_series
from ltspice_llm.schematic import decode_asc_bytes, encode_asc_text

# ADI SPICE-SYNTAX-REFERENCE "Engineering Multipliers" tablosu (büyük/küçük harf duyarsız; M = mili).
ADI_MULTIPLIERS = {"T": 1e12, "G": 1e9, "Meg": 1e6, "K": 1e3, "mil": 25.4e-6, "m": 1e-3, "u": 1e-6,
                   "n": 1e-9, "p": 1e-12, "f": 1e-15}

digits = st.text("0123456789", min_size=1, max_size=4)


@st.composite
def plain_numbers(draw):
    """SPICE sayısı: "12", "12.", "12.5", ".5", isteğe bağlı işaret ve üs."""
    whole, frac = draw(digits), draw(digits)
    body = draw(st.sampled_from([whole, f"{whole}.", f"{whole}.{frac}", f".{frac}"]))
    sign = draw(st.sampled_from(["", "+", "-"]))
    exponent = draw(st.sampled_from(["", "e3", "E-2", "e+1"]))
    return f"{sign}{body}{exponent}"


@st.composite
def mixed_case(draw, word):
    """"Meg" -> "MEG", "meg", "mEg"... ("m" tek başına mili kalır, Meg her yazımda mega)."""
    return "".join(c.upper() if draw(st.booleans()) else c.lower() for c in word)


suffixes = st.sampled_from(sorted(ADI_MULTIPLIERS)).flatmap(
    lambda key: (st.just(key) if key == "m" else mixed_case(key)).map(lambda text: (text, ADI_MULTIPLIERS[key]))
)


@given(plain_numbers(), suffixes)
def test_parse_follows_adi_multiplier_table(number, suffix):
    text, multiplier = suffix
    assert parse_spice_value(number + text) == pytest.approx(float(number) * multiplier, rel=1e-12)


@given(st.sampled_from(["", "+", "-"]), digits, suffixes, digits)
def test_parse_embedded_decimal(sign, whole, suffix, frac):
    # ADI "Embedded decimal: 6K34 means 6.34K"; LTspice 26.1.1: 4k7 = 4700, 1M5 = 1.5m ve kesir işaretten
    # bağımsız eklenir: -4k7 = (-4 + 0.7)k = -3300 (examples/12)
    text, multiplier = suffix
    expected = (float(sign + whole) + float(f"0.{frac}")) * multiplier
    assert parse_spice_value(f"{sign}{whole}{text}{frac}") == pytest.approx(expected, rel=1e-12, abs=1e-30)


# format_spice_value çıktısı: SI önekli sayı ya da önekin dışında bilimsel gösterim, ikisi birden değil
FORMATTED = re.compile(r"-?\d+(?:\.\d+)?(?:T|G|MEG|k|m|u|n|p|f)?|-?\d+(?:\.\d+)?e[+-]\d+")
mantissas = st.floats(min_value=1.0, max_value=10.0, exclude_max=True)
values = st.builds(lambda m, e, s: s * m * 10.0 ** e, mantissas, st.integers(-18, 17), st.sampled_from([1, -1]))


@given(values)
def test_format_then_parse_round_trips(x):
    text = format_spice_value(x)
    assert FORMATTED.fullmatch(text), text
    assert parse_spice_value(text) == pytest.approx(x, rel=5e-3)  # 3 anlamlı basamak


# --------------------------------------------------------------------------- E serisi (IEC 60063)
def in_series(value, series):
    mantissa = value / 10.0 ** math.floor(math.log10(value) + 1e-12)
    return any(math.isclose(mantissa, b, rel_tol=1e-9) for b in (*SERIES_MAP[series], 10.0))


positive = st.builds(lambda m, e: m * 10.0 ** e, mantissas, st.integers(-15, 12))
series_names = st.sampled_from(sorted(SERIES_MAP))


@given(positive, series_names)
def test_snap_brackets_value_with_series_members(x, series):
    lo, near, hi = (snap_to_e_series(x, series, mode)["standard_value"] for mode in ("floor", "nearest", "ceil"))
    assert all(in_series(v, series) for v in (lo, near, hi)), (lo, near, hi)
    assert lo <= x * (1 + 1e-9) and hi >= x * (1 - 1e-9)
    assert near in (lo, hi)
    # En yakın değer göreli (logaritmik) uzaklıkla seçilir; seri geometrik olduğu için IEC'nin ölçütü budur
    assert abs(math.log(near / x)) <= min(abs(math.log(lo / x)), abs(math.log(hi / x))) + 1e-12


@given(positive, positive, series_names, st.sampled_from(["nearest", "floor", "ceil"]))
def test_snap_is_monotonic(a, b, series, mode):
    a, b = sorted((a, b))
    assert snap_to_e_series(a, series, mode)["standard_value"] <= snap_to_e_series(b, series, mode)["standard_value"]


@given(positive, series_names)
def test_snap_error_pct_is_relative_error(x, series):
    res = snap_to_e_series(x, series)
    assert res["error_pct"] == pytest.approx((res["standard_value"] - x) / x * 100, abs=1e-4)


# --------------------------------------------------------------------------- .asc kodlaması
# .asc bir metin dosyasıdır ve her zaman "Version 4" ile başlar; denetim karakteri (NUL...) içermez.
asc_text = st.text(st.characters(exclude_categories=("Cs", "Cc")) | st.sampled_from("\r\n\t")).map(
    lambda body: "Version 4\r\n" + body
)


@given(asc_text)
def test_asc_encoding_round_trips(text):
    data = encode_asc_text(text)
    assert decode_asc_bytes(data)[0] == text
    assert not data.startswith(b"\xef\xbb\xbf")  # LTspice UTF-8 okumaz


@given(st.text("abcR1 0kµΩ", max_size=20))
def test_asc_micro_sign_is_never_utf8(value):
    # µ cp1252'de B5, UTF-16LE'de B5 00; UTF-8'deki C2 B5'i LTspice "Âµ" = 1 F okur (ölçüm, GEMINI.md)
    data = encode_asc_text(f"Version 4\r\nSYMATTR Value {value}µ\r\n")
    assert data.count(b"\xb5") == value.count("µ") + 1
    assert b"\xc2\xb5" not in data


# --------------------------------------------------------------------------- netlist metni
def ltspice_reads(text):
    """
    LTspice'ın komut olarak okuduğu satırlar. ADI SPICE-SYNTAX-REFERENCE "Netlist Structure": ilk satır her zaman
    başlıktır; ADI ".END": "All lines after .end are ignored". Dönüş: (komut satırları, .end bulundu mu).
    """
    out = []
    for line in text.splitlines()[1:]:
        if re.fullmatch(r"\s*\.end\s*(;.*)?", line, re.IGNORECASE):
            return out, True
        out.append(line)
    return out, False


netlist_lines = st.sampled_from(["R1 a 0 1k", "* yorum", ".subckt X a b", ".ends X", ".endl", ".tran 1m", "",
                                 ".end", ".END ; son", "  .end"])
new_directives = st.lists(st.sampled_from([".param K=3", ".options threads=2", ".meas tran x AVG V(a)"]),
                          min_size=1, max_size=3)


@given(netlist_lines, st.lists(netlist_lines, max_size=8), new_directives, st.sampled_from(["\n", "\r\n"]),
       st.booleans())
def test_insert_before_end_lines_are_read_by_ltspice(title, lines, new, eol, trailing_eol):
    # Başlık satırı da netlist satırlarından seçilir: ".end" başlığı LTspice için yalnızca başlıktır
    content = eol.join([title, *lines]) + (eol if trailing_eol else "")
    assume(content)  # boş metin netlist değildir (korunacak başlık yok)
    out = insert_before_end(content, new)
    before, had_end = ltspice_reads(content)
    after, has_end = ltspice_reads(out)
    assert out.splitlines()[0] == title
    assert after == before + new
    assert has_end
    if had_end:  # ilk .end'den sonrası olduğu gibi kalır
        tail = content.splitlines()[1 + len(before):]
        assert out.splitlines()[1 + len(after):] == tail


# --------------------------------------------------------------------------- dalga formu zaman ortalaması
# LTspice dalga formu: artan zaman (kenarda 1e-12 s, düzlükte 1e-3 s adım; aynı zamanda iki nokta da olur) ve değer.
time_steps = st.lists(st.one_of(st.just(0.0), st.floats(1e-12, 1e-3)), min_size=1, max_size=40)
levels = st.floats(-1e3, 1e3)


@st.composite
def waveforms(draw):
    steps = draw(time_steps)
    t = [0.0]
    for dt in steps:
        t.append(t[-1] + dt)
    y = draw(st.lists(levels, min_size=len(t), max_size=len(t)))
    assume(t[-1] > 0)
    return t, y


def _close(a, b, scale):
    return a == pytest.approx(b, rel=1e-9, abs=1e-9 * scale)


@given(waveforms(), st.lists(st.floats(0.0, 1.0), min_size=1, max_size=5))
def test_time_weighted_mean_rms_ignore_points_inserted_on_the_line(wave, fractions):
    """Bağımsız oracle: noktalar doğrusal birleştirilir (LTspice'ın çizdiği dalga formu); bir doğru parçasının üstüne
    nokta eklemek dalga formunu, dolayısıyla ortalamayı ve RMS'i değiştirmez. Aritmetik ortalama ve y²'nin yamuk
    integrali bu özelliği sağlamaz."""
    import numpy as np

    from ltspice_llm.parser import time_weighted_mean_rms

    t, y = wave
    t_fine, y_fine = [t[0]], [y[0]]
    for i in range(1, len(t)):
        for f in sorted(fractions):
            t_fine.append(t[i - 1] + f * (t[i] - t[i - 1]))
            y_fine.append(y[i - 1] + f * (y[i] - y[i - 1]))
        t_fine.append(t[i])
        y_fine.append(y[i])
    scale = max(1.0, max(abs(v) for v in y))
    mean, rms = time_weighted_mean_rms(np.array(t), np.array(y))
    mean_fine, rms_fine = time_weighted_mean_rms(np.array(t_fine), np.array(y_fine))
    assert _close(mean_fine, mean, scale) and _close(rms_fine, rms, scale)


@given(waveforms(), st.floats(1e-3, 1e3), st.floats(-1.0, 1.0))
def test_time_weighted_mean_rms_bounds_and_time_scale(wave, alpha, beta):
    """min ≤ ortalama ≤ max; |ortalama| ≤ RMS ≤ max|y| (Cauchy-Schwarz; doğrunun karesi uçlarının büyüğünü aşmaz).
    Zaman ekseni ölçeklenip kaydırılınca (t -> αt + β·T) ortalama ve RMS değişmez. Kayma süreyle (T) orantılı: 1 s
    kayma 1e-12 s'lik adımı float duyarlığında %0,01 bozardı (testin yapay hatası)."""
    import numpy as np

    from ltspice_llm.parser import time_weighted_mean_rms

    t, y = np.array(wave[0]), np.array(wave[1])
    scale = max(1.0, float(np.max(np.abs(y))))
    mean, rms = time_weighted_mean_rms(t, y)
    tol = 1e-9 * scale
    assert y.min() - tol <= mean <= y.max() + tol
    assert abs(mean) - tol <= rms <= np.max(np.abs(y)) + tol
    mean_s, rms_s = time_weighted_mean_rms(alpha * t + beta * t[-1], y)
    assert _close(mean_s, mean, scale) and _close(rms_s, rms, scale)


def test_time_weighted_mean_rms_rejects_an_axis_that_goes_back():
    """Geri dönen eksen (iç içe .dc taraması) ya da sıfır süre zaman ortalaması değildir: None (çağıran aritmetiğe düşer)."""
    import numpy as np

    from ltspice_llm.parser import time_weighted_mean_rms

    assert time_weighted_mean_rms(np.array([0.0, 1.0, 0.5]), np.array([1.0, 2.0, 3.0])) is None
    assert time_weighted_mean_rms(np.array([1.0, 1.0]), np.array([1.0, 2.0])) is None
    assert time_weighted_mean_rms(np.array([1.0]), np.array([1.0])) is None


# --------------------------------------------------------------------------- rapor grafiği ekseni
@given(st.floats(-1e3, 1e3), st.floats(1e-3, 2e3), st.integers(2, 10), st.integers(-12, 3))
# 0.25 adımı (1 / 4): etiket "0.25" olmalı. Rastgele örnekler bu adımı her koşumda üretmiyor; mutant kataloğunda
# "0.25 adımlı etiketler '0.2'/'0.3' yazılır" bir koşumda sağ kaldı (2026-10-11).
@example(0.0, 1.0, 4, 0)
@example(0.0, 1.0, 4, -12)
def test_axis_ticks_are_nice_complete_and_labelled_exactly(lo, span, count, exponent):
    """
    _line_chart işaretleri ölçeklenmemiş değerlerle (saniye, amper: 1e-12 ile 1e3 arası) üretir, etiketi SI çarpanına
    böler. Adım "güzel" sayıdır (1, 2, 2.5, 5 × 10^k); işaretler aralığı eksiksiz, adım arayla kaplar; etiket işaretin
    değerini tam yazar (0.25 adımında "0.2" değil "0.25"; 2.5 ps adımında 2 ps değil).
    """
    from ltspice_llm.report import _nice_step, _tick_decimals, _ticks

    scale = 10.0 ** exponent
    step = _nice_step(span, count) * scale
    mantissa = step / 10 ** math.floor(math.log10(step))
    assert any(math.isclose(mantissa, m, rel_tol=1e-9) for m in (1, 2, 2.5, 5))
    lo, hi = lo * scale, (lo + span) * scale
    ticks = _ticks(lo, hi, step)
    assert ticks and 1 <= len(ticks) <= count + 1
    assert ticks[0] >= lo - 1e-9 * step and ticks[0] - step < lo and ticks[-1] + step > hi
    assert all(math.isclose(b - a, step, rel_tol=1e-6) for a, b in zip(ticks, ticks[1:]))
    decimals = _tick_decimals(step / scale)
    assert all(abs(float(f"{t / scale:.{decimals}f}") - t / scale) <= 1e-6 * step / scale for t in ticks)


@pytest.mark.parametrize("lo, hi, step, expected", [
    (0.0, 15e-12, 2.5e-12, [0.0, 2.5e-12, 5e-12, 7.5e-12, 10e-12, 12.5e-12, 15e-12]),   # ps ekseni (test-reviewer)
    (0.0, 1e-12, 2e-13, [0.0, 2e-13, 4e-13, 6e-13, 8e-13, 1e-12]),                      # pA ekseni
])
def test_axis_ticks_keep_their_values_at_pico_scale(lo, hi, step, expected):
    """Elle: adımın aralıktaki katları. Eskiden round(v, 12) 2.5 ps'yi 2 ps'ye, 0.2 pA'i 0'a yuvarlıyordu."""
    from ltspice_llm.report import _ticks

    assert _ticks(lo, hi, step) == pytest.approx(expected, rel=1e-9, abs=1e-24)


@given(st.floats(-1e3, 1e3), st.floats(-1e3, 1e3), st.integers(0, 3), st.sampled_from([1.0, 1e-3, 1e-6]))
def test_effect_says_unchanged_exactly_when_the_shown_change_is_zero(before, after, decimals, scale):
    """Etki tablosu: karar ("değişmedi") yanındaki gösterilen değişimle çelişmemeli (25.0 → 25.5 °C '0 °C' yazar)."""
    from ltspice_llm.report import Effect, format_value

    shown = format_value(after - before, "V", scale, decimals, signed=True).split()[0].replace("−", "-")
    unchanged = Effect("x", "y", "z", before, after, "V", scale, decimals).verdict()[1] == "değişmedi"
    assert unchanged == (float(shown) == 0)


# ------------------------------------------------------- şema ile netlisti aynı eleman değerini değiştirir
# LTspice netlisterı 'SYMATTR Value' metnini olduğu gibi netlistin değer alanına yazar (tests/data/ref26_sch_connected:
# 'SYMATTR Value 5' -> 'V1 N001 0 5'). Bu yüzden apply_parameters bir şemaya, LTspice'ın o şemadan üreteceği netliste
# yaptığını yapmalı. Öznitelik sırası sabit değil: LTspice örneği LT1963-1.5.asc'de C1'in Value'su InstName'den önce.
element_values = st.one_of(
    plain_numbers(),
    st.builds(lambda n, s: n + s, plain_numbers(), st.sampled_from(["k", "Meg", "u", "n", "p", "µ"])),
    st.sampled_from(["{R_VAL}", "{2*Rb}", "PULSE(0 5 0 1n 1n 1m 2m)", "SINE(0 1 1k)", "PWL(0 0 1m 5)", "1N4148",
                     "R=2k", "DC 5", "DC 1 AC 1", "AC 1", "1k tc=1e-3", "10µ Rser=50m"]),
)


@given(st.sampled_from("RCLVI"), element_values, st.booleans(), plain_numbers(), st.booleans())
@example("V", "PULSE(0 5 0 1n 1n 1m 2m)", False, "3.3", False)  # eski hata: şemada '3.3 5 0 1n 1n 1m 2m)' oluyordu
@example("C", "10µ", True, "22µ", False)                        # eski hata: Value önce gelince eleman bulunamıyordu
@example("C", "10µ", False, "22µ", True)                        # SPICE adı büyük/küçük harf duyarsız (test-reviewer)
def test_schematic_and_its_netlist_agree_on_element_values(kind, value, value_first, new, lower_request):
    name = f"{kind}1"
    requested = name.lower() if lower_request else name
    attrs = [f"SYMATTR InstName {name}", f"SYMATTR Value {value}"]
    if value_first:
        attrs.reverse()
    asc = "Version 4\nSHEET 1 880 680\nSYMBOL x 0 0 R0\nWINDOW 0 0 0 Left 2\n" + "\n".join(attrs) + "\nTEXT 0 0 Left 2 !.op\n"
    net = f"* t\n{name} a 0 {value}\n.op\n.end\n"
    from_asc, from_net = _set_element_value(asc, requested, new), _set_element_value(net, requested, new)
    assert (from_asc is None) == (from_net is None)
    if from_net is not None:
        assert from_asc[1] == from_net[1]
        (value_line,) = [ln for ln in from_asc[0].splitlines() if ln.startswith("SYMATTR Value ")]
        (element_line,) = [ln for ln in from_net[0].splitlines() if ln.startswith(f"{name} ")]
        assert value_line[len("SYMATTR Value "):] == element_line[len(f"{name} a 0 "):]
