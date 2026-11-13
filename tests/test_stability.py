"""
stability_margins (ltspice_llm/stability.py) testleri. LTspice gerektirmez.

Beklenen değerlerin kaynağı elle hesaptır, kodun çıktısı değil:

- Üç eş kutup, T = K/(1 + jx)³, x = f/f0: ∠T = -3·atan(x), |T| = K/(1 + x²)^(3/2).
  Faz geçişi: 3·atan(x) = 180° → x = tan 60° = √3, |T| = K/8 → GM = 20·log10(8/K).
  Kazanç geçişi: (1 + x²)^(3/2) = K → x = √(K^(2/3) - 1), PM = 180° - 3·atan(x).
- Routh-Hurwitz, kapalı biçimden bağımsız tanık: 1 + T = 0 → s³ + 3s² + 3s + 1 + K = 0 (ω0 = 1); kararlılık
  koşulu 3·3 > 1 + K, yani K < 8. K = 8'de kökler ±j√3: iki geçiş de √3·f0'da, GM = 0 dB, PM = 0°.
- İntegratör ve kutup, T = (fp/jf)/(1 + jf/fp), x = f/fp: |T| = 1 → x²(1 + x²) = 1 → x² = (√5 - 1)/2;
  PM = 90° - atan(x). ∠T -180°'ye ulaşmaz.
- Dekad aralıklı yapay noktalar: dB ve faz log f'e göre doğrusal; geçişler testin yanında elle bulunur.
- Rastgele döngü: üretilen dB ve açılmış fazdan bağımsız oracle (test_random_loop_matches_unwrapped_oracle).

Toleranslar: dekad başına 100 nokta, h = 0,01 dekad. Doğrusal aradeğerleme hatası ≤ h²/8·max|y''|; üç kutupta
|dB''| ≤ 30·ln10 ≈ 69 dB/dekad² ve |∠''| ≤ 3·ln²10/4 ≈ 4 rad/dekad². K ∈ [2, 64]'te bu, PM'de ≤ 0,011°, GM'de
≤ 0,002 dB, frekansta ≤ 1e-4 bağıl hata verir; toleranslar bunun iki katı ya da fazlası.
"""

import cmath
import math
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from ltspice_llm import LTSpiceAdapter, stability_margins
from ltspice_llm.tools import handle_tool_call

DATA = Path(__file__).parent / "data"

PM_TOL_DEG = 0.025
GM_TOL_DB = 0.005
FREQ_REL = 2e-4


def _decades(f0, offset=0.0):
    """f0'ın üç dekad altından üç dekad üstüne, dekad başına 100 nokta; ızgara ``offset`` dekad kaydırılır."""
    return f0 * 10.0 ** (offset + np.arange(-300, 301) / 100)


def _three_pole(k, f0, offset=0.0):
    f = _decades(f0, offset)
    return f, k / (1 + 1j * f / f0) ** 3


def _three_pole_expected(k):
    """(kazanç geçişindeki x, PM derece, GM dB): modül docstring'indeki kapalı biçim."""
    x = math.sqrt(k ** (2 / 3) - 1)
    return x, 180 - 3 * math.degrees(math.atan(x)), 20 * math.log10(8 / k)


def _polar(mag, deg):
    return mag * cmath.exp(1j * math.radians(deg))


def _freqs(rows):
    return [row["freq_hz"] for row in rows]


@pytest.mark.parametrize("k", [4.0, 16.0])
def test_three_pole_margins_match_closed_form(k):
    f0 = 1e3
    r = stability_margins(*_three_pole(k, f0))
    x, pm, gm = _three_pole_expected(k)
    assert r["crossover_hz"] == pytest.approx(x * f0, rel=FREQ_REL)
    assert r["phase_margin_deg"] == pytest.approx(pm, abs=PM_TOL_DEG)
    assert r["phase_crossover_hz"] == pytest.approx(math.sqrt(3) * f0, rel=FREQ_REL)
    assert r["gain_margin_db"] == pytest.approx(gm, abs=GM_TOL_DB)
    # Routh: K < 8 kararlı, K > 8 kararsız; tek geçişli minimum fazlı döngüde iki pay da bu işareti taşır.
    stable = k < 8
    assert (r["phase_margin_deg"] > 0) is stable
    assert (r["gain_margin_db"] > 0) is stable
    assert r["warnings"] == []


def test_routh_critical_gain_puts_both_crossings_at_sqrt3():
    f0 = 1e3
    r = stability_margins(*_three_pole(8.0, f0))
    assert r["crossover_hz"] == pytest.approx(math.sqrt(3) * f0, rel=FREQ_REL)
    assert r["phase_crossover_hz"] == pytest.approx(math.sqrt(3) * f0, rel=FREQ_REL)
    assert r["phase_margin_deg"] == pytest.approx(0, abs=PM_TOL_DEG)
    assert r["gain_margin_db"] == pytest.approx(0, abs=GM_TOL_DB)


@given(
    k=st.floats(min_value=2, max_value=64),
    exponent=st.floats(min_value=0, max_value=6),
    offset=st.floats(min_value=0, max_value=0.01, exclude_max=True),
)
def test_three_pole_property(k, exponent, offset):
    """K, f0 ve örneklerin geçişlere göre yeri rastgele; sonuç kapalı biçimden toleranslar içinde."""
    f0 = 10.0**exponent
    r = stability_margins(*_three_pole(k, f0, offset))
    x, pm, gm = _three_pole_expected(k)
    assert len(r["gain_crossovers"]) == 1 and len(r["phase_crossovers"]) == 1
    assert r["crossover_hz"] == pytest.approx(x * f0, rel=FREQ_REL)
    assert r["phase_margin_deg"] == pytest.approx(pm, abs=PM_TOL_DEG)
    assert r["phase_crossover_hz"] == pytest.approx(math.sqrt(3) * f0, rel=FREQ_REL)
    assert r["gain_margin_db"] == pytest.approx(gm, abs=GM_TOL_DB)


# Rastgele döngü: başlangıç fazı ve nokta başına (dB, faz adımı). Adım ±170° ile sınırlı: fonksiyon, ardışık
# noktalarda fazın 180°'den az değişmesini şart koşuyor; 170° tam 180° sınırındaki yuvarlama belirsizliğini dışlar.
_LOOP = st.tuples(
    st.floats(min_value=-180, max_value=180),
    st.lists(
        st.tuples(st.floats(min_value=-60, max_value=60), st.floats(min_value=-170, max_value=170)),
        min_size=2,
        max_size=40,
    ),
)


@given(loop=_LOOP, n=st.integers(min_value=-10, max_value=10), c=st.floats(min_value=1e-3, max_value=1e3))
def test_scaling_property(loop, n, c):
    """Frekansı c ile ölçeklemek geçiş frekanslarını c ile çarpar, payları değiştirmez. T'yi 2ⁿ ile çarpmak ∠T'yi
    korur: faz geçişleri yerinde kalır, kazanç payı tam 20·n·log10(2) dB azalır."""
    start, rows = loop
    phases = start + np.cumsum([step for _, step in rows])
    f = 10.0 ** (np.arange(len(rows)) / 10)
    t = np.array([_polar(10 ** (db / 20), ph) for (db, _), ph in zip(rows, phases)])
    base = stability_margins(f, t)

    stretched = stability_margins(f * c, t)
    for key, margin in (("gain_crossovers", "phase_margin_deg"), ("phase_crossovers", "gain_margin_db")):
        assert _freqs(stretched[key]) == pytest.approx([x * c for x in _freqs(base[key])], rel=1e-9)
        assert [row[margin] for row in stretched[key]] == [row[margin] for row in base[key]]

    louder = stability_margins(f, t * 2.0**n)
    assert _freqs(louder["phase_crossovers"]) == pytest.approx(_freqs(base["phase_crossovers"]), rel=1e-12)
    assert [row["gain_margin_db"] for row in louder["phase_crossovers"]] == pytest.approx(
        [row["gain_margin_db"] - 20 * n * math.log10(2) for row in base["phase_crossovers"]], abs=1e-9
    )


def _wrap(deg):
    """Açıyı [-180°, 180°) aralığına indirger (yalnız iki açının farkını karşılaştırmak için)."""
    return (deg + 180.0) % 360.0 - 180.0


@given(loop=_LOOP)
def test_random_loop_matches_unwrapped_oracle(loop):
    """Bağımsız oracle: üretilen dB ile açılmış faz (başlangıç + adımların toplamı; np.angle kullanılmaz), log f'te
    doğrusal aradeğerleme. Kazanç geçişi dB'nin işaret değiştirdiği aralıkta; faz geçişi açılmış fazın 180° + 360°·k'yi
    kestiği aralıkta (adım < 180° olduğundan aralıkta en çok bir tane). Tam 0 dB'deki ya da tam eksendeki örnek ayrı
    vakalarda sınanıyor; burada 0,01 itilir (adım en çok 170,01°)."""
    start, rows = loop
    db = [d if abs(d) > 1e-3 else 0.01 for d, _ in rows]
    ph = [p if abs(_wrap(p - 180.0)) > 1e-3 else p + 0.01 for p in start + np.cumsum([step for _, step in rows])]
    f = 10.0 ** (np.arange(len(rows)) / 10)
    r = stability_margins(f, [_polar(10 ** (d / 20), p) for d, p in zip(db, ph)])
    gain, phase = [], []
    for i in range(len(rows) - 1):
        if db[i] * db[i + 1] < 0:
            frac = db[i] / (db[i] - db[i + 1])
            gain.append((f[i] * 10 ** (frac / 10), 180.0 + ph[i] + frac * (ph[i + 1] - ph[i])))
        lo, hi = sorted((ph[i], ph[i + 1]))
        odd = 360.0 * math.ceil((lo - 180.0) / 360.0) + 180.0   # lo'dan büyük ilk 180° + 360°·k
        if lo < odd < hi:
            frac = (odd - ph[i]) / (ph[i + 1] - ph[i])
            phase.append((f[i] * 10 ** (frac / 10), -(db[i] + frac * (db[i + 1] - db[i]))))
    got_gain, got_phase = r["gain_crossovers"], r["phase_crossovers"]
    assert _freqs(got_gain) == pytest.approx([g[0] for g in gain], rel=1e-6)
    assert [_wrap(row["phase_margin_deg"] - g[1]) for row, g in zip(got_gain, gain)] == pytest.approx(
        [0.0] * len(gain), abs=1e-6)
    assert _freqs(got_phase) == pytest.approx([p[0] for p in phase], rel=1e-6)
    assert [row["gain_margin_db"] for row in got_phase] == pytest.approx([p[1] for p in phase], abs=1e-6)


def test_integrator_and_pole_has_no_phase_crossover():
    fp = 1e3
    f = _decades(fp)
    r = stability_margins(f, (fp / (1j * f)) / (1 + 1j * f / fp))
    x = math.sqrt((math.sqrt(5) - 1) / 2)
    assert r["crossover_hz"] == pytest.approx(x * fp, rel=FREQ_REL)
    assert r["phase_margin_deg"] == pytest.approx(90 - math.degrees(math.atan(x)), abs=PM_TOL_DEG)
    assert r["phase_crossovers"] == []
    assert r["phase_crossover_hz"] is None and r["gain_margin_db"] is None
    assert len(r["warnings"]) == 1 and r["warnings"][0].startswith("Faz geçişi yok")


def test_headline_phase_margin_is_the_smallest_signed():
    # dB +6, -6, +6 → geçişler log f'te orta noktalarda: √10 ve 10^1,5. Faz -150 → -170 → -310 (adım -20°, -140°);
    # orta noktalarda -160° ve -240° → PM 20° ve -60°. Mutlak değerce küçük 20° değil, işaretli en küçük -60° seçilir.
    r = stability_margins([1.0, 10.0, 100.0], [_polar(2, -150), _polar(0.5, -170), _polar(2, -310)])
    rows = r["gain_crossovers"]
    assert _freqs(rows) == pytest.approx([10**0.5, 10**1.5], rel=1e-9)
    assert [row["phase_margin_deg"] for row in rows] == pytest.approx([20, -60], abs=1e-9)
    assert r["crossover_hz"] == pytest.approx(10**1.5, rel=1e-9)
    assert r["phase_margin_deg"] == pytest.approx(-60, abs=1e-9)
    assert sum(w.startswith("2 kazanç geçişi var") for w in r["warnings"]) == 1


def test_headline_gain_margin_is_closest_to_0db():
    # ∠T -170 → -190 → -170 → -190: T negatif gerçel ekseni orta noktalarda keser (√10, 10^1,5, 10^2,5).
    # dB -10, -10, +4, +4 → kesişmelerde -10, -3, +4 → GM +10, +3, -4 dB. 0 dB'ye en yakın +3 dB seçilir, işaretli en
    # küçük -4 dB değil. |T| doğrusal aradeğerlenseydi ortadaki GM 3 dB değil ~0,44 dB çıkardı.
    points = [(-10, -170), (-10, -190), (4, -170), (4, -190)]
    r = stability_margins([1.0, 10.0, 100.0, 1000.0], [_polar(10 ** (db / 20), ph) for db, ph in points])
    rows = r["phase_crossovers"]
    assert _freqs(rows) == pytest.approx([10**0.5, 10**1.5, 10**2.5], rel=1e-9)
    assert [row["gain_margin_db"] for row in rows] == pytest.approx([10, 3, -4], abs=1e-9)
    assert r["phase_crossover_hz"] == pytest.approx(10**1.5, rel=1e-9)
    assert r["gain_margin_db"] == pytest.approx(3, abs=1e-9)


@pytest.mark.parametrize("mags", [[2.0, 1.0, 0.5], [0.5, 1.0, 2.0], [2.0, 1.0, 2.0], [0.5, 1.0, 0.5]])
def test_sample_exactly_at_0db_counts_once(mags):
    # T = -j·m: |T| tam m, ∠T = -90° → PM = 90°. Geçerek ya da dokunarak, tam 0 dB'deki örnek tek geçiştir.
    r = stability_margins([1.0, 10.0, 100.0], [-1j * m for m in mags])
    assert _freqs(r["gain_crossovers"]) == [10.0]
    assert r["phase_margin_deg"] == pytest.approx(90, abs=1e-12)


@pytest.mark.parametrize("last_deg", [-190, -170])
def test_sample_exactly_on_negative_real_axis_counts_once(last_deg):
    # Ortadaki T = -0,5 tam negatif gerçel eksende: geçerek (-190°) ya da dokunarak (-170°), tek faz geçişi; GM = 20·log10 2.
    r = stability_margins([1.0, 10.0, 100.0], [_polar(0.5, -170), -0.5 + 0j, _polar(0.5, last_deg)])
    assert _freqs(r["phase_crossovers"]) == [10.0]
    assert r["gain_margin_db"] == pytest.approx(20 * math.log10(2), abs=1e-12)


def test_headline_phase_margin_is_not_simply_the_last_crossing():
    # dB +6, -6, +6, -6 → üç geçiş orta noktalarda (√10, 10^1,5, 10^2,5). Faz -150 → -170 → -310 → -150 (adımlar -20°,
    # -140°, +160°); orta noktalarda -160°, -240°, -230° → PM 20°, -60°, -50°. En küçük (-60°) ortadaki geçişte.
    t = [_polar(2, -150), _polar(0.5, -170), _polar(2, -310), _polar(0.5, -150)]
    r = stability_margins([1.0, 10.0, 100.0, 1000.0], t)
    assert [row["phase_margin_deg"] for row in r["gain_crossovers"]] == pytest.approx([20, -60, -50], abs=1e-9)
    assert r["crossover_hz"] == pytest.approx(10**1.5, rel=1e-9)
    assert r["phase_margin_deg"] == pytest.approx(-60, abs=1e-9)


def test_large_phase_step_through_minus_180_is_a_phase_crossover():
    # ∠T -100° → -270° (adım -170°, belgedeki < 180° sınırının içinde; seyrek taramada olur): ∠(-T) 80° → -90°,
    # kesir 80/170 → log f'te 10^(80/170). |T| sabit 0,5 → GM = 20·log10 2.
    r = stability_margins([1.0, 10.0], [_polar(0.5, -100), _polar(0.5, -270)])
    assert _freqs(r["phase_crossovers"]) == pytest.approx([10 ** (80 / 170)], rel=1e-9)
    assert r["gain_margin_db"] == pytest.approx(20 * math.log10(2), abs=1e-9)


@pytest.mark.parametrize("mags, expected_hz", [([2.0, 1.5, 1.0], 100.0), ([1.0, 0.5, 0.25], 1.0)])
def test_sample_exactly_at_0db_at_either_end(mags, expected_hz):
    r = stability_margins([1.0, 10.0, 100.0], [-1j * m for m in mags])
    assert _freqs(r["gain_crossovers"]) == [expected_hz]
    assert r["phase_margin_deg"] == pytest.approx(90, abs=1e-12)


def test_last_sample_exactly_on_negative_real_axis():
    # ∠(-T) 10° → 5° → 0 (son örnek tam -0,5): tek faz geçişi son örnekte; GM = 20·log10 2.
    r = stability_margins([1.0, 10.0, 100.0], [_polar(0.5, -170), _polar(0.5, -175), -0.5 + 0j])
    assert _freqs(r["phase_crossovers"]) == [100.0]
    assert r["gain_margin_db"] == pytest.approx(20 * math.log10(2), abs=1e-12)


def test_phase_margin_at_zero_phase_is_plus_180():
    # ∠T = 0° (T pozitif gerçel) -1'den en uzak nokta: PM aralığı (-180°, 180°], yani +180°; -180° değil.
    assert stability_margins([1.0, 10.0, 100.0], [2.0, 1.0, 0.5])["phase_margin_deg"] == 180.0


def test_interpolation_uses_each_interval_on_a_nonuniform_grid():
    # Izgara düzgün değil (1, 10, 1000 Hz; `.ac lin`/`.ac list` böyledir). Geçiş ikinci aralıkta: dB +6 → -6, kesir 0,5
    # → √(10·1000) = 100 Hz.
    r = stability_margins([1.0, 10.0, 1000.0], [-2j, -2j, -0.5j])
    assert _freqs(r["gain_crossovers"]) == pytest.approx([100.0], rel=1e-9)


def test_phase_step_of_179_degrees_through_minus_180_is_a_crossover():
    # ∠T -91° → -270° (adım -179°, sınır 180°): ∠(-T) 89° → -90°, kesir 89/179 → 10^(89/179). GM = 20·log10 2.
    r = stability_margins([1.0, 10.0], [_polar(0.5, -91), _polar(0.5, -270)])
    assert _freqs(r["phase_crossovers"]) == pytest.approx([10 ** (89 / 179)], rel=1e-9)
    assert r["gain_margin_db"] == pytest.approx(20 * math.log10(2), abs=1e-9)


def test_phase_step_of_179_degrees_through_zero_is_not_a_crossover():
    # ∠T +89,5° → -89,5° (adım -179°): T pozitif gerçel ekseni keser; ∠(-T) -90,5° → +90,5° (fark 181°) sarmadır.
    r = stability_margins([1.0, 10.0], [_polar(0.5, 89.5), _polar(0.5, -89.5)])
    assert r["phase_crossovers"] == []


def test_near_zero_db_touch_is_not_a_crossing():
    # +0,0005 dB'ye inip dönen eğri 0 dB'yi kesmez: yalnız tam 0 dB'deki örnek geçiş sayılır.
    r = stability_margins([1.0, 10.0, 100.0], [-2j, -1j * 10 ** (0.0005 / 20), -2j])
    assert r["gain_crossovers"] == []


def test_headline_gain_margin_can_be_negative():
    # T = -0,316 (eksende, GM +10 dB), -j, -1,41 (eksende, GM -3 dB). 0 dB'ye en yakın pay -3 dB: kazancı 3 dB azaltmak
    # -1'e götürür (koşullu kararlı döngü). +10 dB'yi seçmek iyimser olurdu.
    r = stability_margins([1.0, 10.0, 100.0], [-(10 ** (-10 / 20)), -1j, -(10 ** (3 / 20))])
    assert [row["gain_margin_db"] for row in r["phase_crossovers"]] == pytest.approx([10, -3], abs=1e-9)
    assert r["phase_crossover_hz"] == 100.0
    assert r["gain_margin_db"] == pytest.approx(-3, abs=1e-9)


def test_positive_real_axis_is_not_a_phase_crossover():
    # ∠T +10° → -10°: T pozitif gerçel ekseni keser; -T'nin açısı ±180°'de sarar, bu faz geçişi değildir.
    r = stability_margins([1.0, 10.0], [_polar(0.5, 10), _polar(0.5, -10)])
    assert r["phase_crossovers"] == []
    assert r["gain_margin_db"] is None


@pytest.mark.parametrize("k, side", [(0.5, "altında"), (2e9, "üstünde")])
def test_missing_gain_crossover_is_reported(k, side):
    # K = 0,5: |T| ≤ 0,5 her yerde. K = 2e9: en yüksek frekansta bile |T| = 2e9/(1 + 1e6)^1,5 ≈ 2.
    r = stability_margins(*_three_pole(k, 1e3))
    assert r["gain_crossovers"] == []
    assert r["crossover_hz"] is None and r["phase_margin_deg"] is None
    missing = [w for w in r["warnings"] if w.startswith("Kazanç geçişi yok")]
    assert len(missing) == 1 and f"0 dB'nin {side}" in missing[0]


@pytest.mark.parametrize(
    "freq, gain, message",
    [
        ([1.0, 2.0], [1j], "aynı uzunlukta"),
        ([[1.0, 2.0]], [[1j, 1j]], "tek boyutlu"),
        ([1.0], [1j], "en az iki"),
        ([1.0, float("nan")], [1j, 1j], "sonlu"),
        ([1.0, 2.0], [1j, complex(float("inf"), 0.0)], "sonlu"),
        ([0.0, 1.0], [1j, 1j], "pozitif"),
        ([2.0, 1.0], [1j, 1j], "kesin artan"),
        ([1.0, 1.0], [1j, 1j], "kesin artan"),
        ([1.0, 2.0], [1j, 0j], "sıfır olamaz"),
    ],
)
def test_rejects_invalid_input(freq, gain, message):
    with pytest.raises(ValueError, match=message):
        stability_margins(freq, gain)


# ------------------------------------------------------------------ analyze_loop_stability (gerçek LTspice verisi)
# tests/data/ref26_ac_loop3.* (examples/14 "loop3", LTspice 26.1.1): üç tamponlu RC katı ve ters çeviren E3, T =
# -V(y)/V(x) = kloop/(1 + jf/fc)³, fc = 1/(2π·1k·159.155n); `.step param kloop list 4 16`, `.ac dec 100 10 100k`
# (h = 0,01 dekad: yukarıdaki toleranslar geçerli).
LOOP3_FC = 1 / (2 * math.pi * 1e3 * 159.155e-9)


@pytest.fixture
def loop_adapter(tmp_path):
    """LTspice çalıştırmayan adaptör; kayıtlı koşum çalışma alanında 'loop' adıyla (steplenmiş .raw .log'uyla okunur)."""
    adapter = LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path / "ws")
    adapter.workspace_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(DATA / "ref26_ac_loop3.raw", adapter.workspace_dir / "loop.raw")
    shutil.copy(DATA / "ref26_ac_loop3.log.txt", adapter.workspace_dir / "loop.log")
    return adapter


def test_loop_tool_matches_closed_form_on_ltspice_data(loop_adapter):
    # Boşluklu düğüm adı (' x ') ve büyük/küçük harfi farklı iz ('v(Y)') kabul edilir; ifade gerçek iz adlarıyla döner.
    res = handle_tool_call("analyze_loop_stability",
                           {"circuit_name": "loop", "loop_input": " x ", "loop_output": "v(Y)"}, adapter=loop_adapter)
    assert res["status"] == "SUCCESS" and res["loop_gain"] == "-V(y)/V(x)"
    assert [step["step"] for step in res["steps"]] == [1, 2]
    # ref26_ac_loop3.net `.step param kloop list 4 16`; log: ".step kloop=4", ".step kloop=16"
    assert [step["params"] for step in res["steps"]] == [{"kloop": 4.0}, {"kloop": 16.0}]
    for step in res["steps"]:
        x, pm, gm = _three_pole_expected(step["params"]["kloop"])
        assert step["crossover_hz"] == pytest.approx(x * LOOP3_FC, rel=FREQ_REL)
        assert step["phase_margin_deg"] == pytest.approx(pm, abs=PM_TOL_DEG)
        assert step["phase_crossover_hz"] == pytest.approx(math.sqrt(3) * LOOP3_FC, rel=FREQ_REL)
        assert step["gain_margin_db"] == pytest.approx(gm, abs=GM_TOL_DB)
        assert step["warnings"] == []
    # Routh: kloop = 4 kararlı, 16 kararsız
    assert res["steps"][0]["phase_margin_deg"] > 0 > res["steps"][1]["phase_margin_deg"]


@pytest.mark.parametrize(
    "args, fragment",
    [
        ({"circuit_name": "loop", "loop_output": "y"}, "'loop_input' gereklidir"),
        ({"circuit_name": "loop", "loop_input": "  ", "loop_output": "y"}, "'loop_input' gereklidir"),
        ({"circuit_name": "loop", "loop_input": "x"}, "'loop_output' gereklidir"),
        ({"circuit_name": "loop", "loop_input": "x", "loop_output": "V(z)"}, "'V(z)' izi yok"),
        ({"circuit_name": "yok", "loop_input": "x", "loop_output": "y"}, ".raw dosyası bulunamadı"),
    ],
)
def test_loop_tool_errors(loop_adapter, args, fragment):
    res = handle_tool_call("analyze_loop_stability", args, adapter=loop_adapter)
    assert res["status"] == "ERROR" and res["message"].count(fragment) == 1


def test_loop_tool_refuses_a_transient_run(loop_adapter):
    # ref26_flow_rc: `.tran 3m` koşumu (examples/14 "flow")
    shutil.copy(DATA / "ref26_flow_rc.raw", loop_adapter.workspace_dir / "rc.raw")
    res = handle_tool_call("analyze_loop_stability", {"circuit_name": "rc", "loop_input": "in", "loop_output": "out"},
                           adapter=loop_adapter)
    assert res["status"] == "ERROR" and res["message"].count(".ac koşumu ister; bu .raw 'tran'") == 1
