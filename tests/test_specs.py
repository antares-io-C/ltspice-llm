"""
check_specs (ltspice_llm/specs.py) ve verify_specs aracı. LTspice gerektirmez.

Beklenen değerler elle hesaptır (pay = en yakın sınıra uzaklık; sınır içinde >= 0) ya da LTspice 26.1.1'in kayıtlı
loglarından gelir (tests/data/ref26_*.log.txt; satırlar testlerin yanında).
"""

import shutil
import sys
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ltspice_llm import LTSpiceAdapter, check_specs
from ltspice_llm.adapter import strip_auto_directives
from ltspice_llm.specs import numerical_spread
from ltspice_llm.tools import handle_tool_call

DATA = Path(__file__).parent / "data"


def test_bounds_are_inclusive_and_a_violation_has_a_negative_margin():
    # v_out 3,4 üst sınırda: dahil, pay 0. i_q 2 mA, üst sınır 1 mA: pay 1 mA - 2 mA = -1 mA.
    r = check_specs([{"v_out": 3.4, "i_q": 2e-3}], {"v_out": {"min": 3.2, "max": 3.4}, "i_q": {"max": 1e-3}})
    v_out, i_q = r["results"]
    assert (v_out["status"], v_out["margin"], v_out["failed_steps"]) == ("PASS", 0.0, [])
    assert i_q["status"] == "FAIL" and i_q["margin"] == pytest.approx(-1e-3) and i_q["failed_steps"] == [1]
    assert (r["verdict"], r["passed"], r["failed"], r["unverified"], r["steps"]) == ("FAIL", 1, 1, 0, 1)


@pytest.mark.parametrize("spec, status, margin", [
    ({"min": 3.2, "max": 3.4}, "PASS", 0.05),   # 3,25 V: alt sınıra 0,05, üst sınıra 0,15
    ({"min": 3.0}, "PASS", 0.25),
    ({"max": 3.3}, "PASS", 0.05),
    ({"max": 3.0}, "FAIL", -0.25),              # 0,25 V aşım
])
def test_margin_is_the_distance_to_the_nearest_bound(spec, status, margin):
    (row,) = check_specs([{"v": 3.25}], {"v": spec})["results"]
    assert row["status"] == status and row["margin"] == pytest.approx(margin)


def test_every_step_is_checked_and_the_worst_step_is_reported():
    # Adımların ortalaması 3,3 sınır içinde; adım 3 (3,6) üst sınırı 0,2 aşıyor: ortalamaya bakan karar bunu gizlerdi.
    steps = [{"v_out": 3.1}, {"v_out": 3.2}, {"v_out": 3.6}]
    (row,) = check_specs(steps, {"v_out": {"min": 3.0, "max": 3.4}})["results"]
    assert (row["status"], row["worst_step"], row["worst_value"], row["failed_steps"]) == ("FAIL", 3, 3.6, [3])
    assert row["margin"] == pytest.approx(-0.2)


def test_the_worst_step_can_be_in_the_middle():
    # Paylar: adım 1 min(0,2; 0,2) = 0,2; adım 2 min(0,05; 0,35) = 0,05; adım 3 min(0,3; 0,1) = 0,1.
    steps = [{"v": 3.2}, {"v": 3.05}, {"v": 3.3}]
    (row,) = check_specs(steps, {"v": {"min": 3.0, "max": 3.4}})["results"]
    assert (row["status"], row["worst_step"], row["worst_value"]) == ("PASS", 2, 3.05)
    assert row["margin"] == pytest.approx(0.05)


def test_missing_or_uncomputed_measurements_are_never_a_pass():
    # t_rise logda yok; v_never LTspice'ın FAIL'ed dediği ölçüm. Hiçbir spec kalmadı ama karar PASS olamaz.
    r = check_specs([{"v_out": 3.3}], {"v_out": {"min": 3.2}, "t_rise": {"max": 1e-6}, "V_NEVER": {"max": 1.0}},
                    failed=["v_never"])
    rows = {row["name"]: row for row in r["results"]}
    assert rows["v_out"]["status"] == "PASS"
    assert rows["t_rise"]["status"] == "UNVERIFIED" and rows["t_rise"]["reason"].count(".meas ekleyin") == 1
    assert rows["V_NEVER"]["status"] == "UNVERIFIED" and rows["V_NEVER"]["reason"].count("FAIL'ed") == 1
    assert (r["verdict"], r["passed"], r["failed"], r["unverified"]) == ("UNVERIFIED", 1, 0, 2)


def test_a_failing_spec_outranks_an_unverified_one():
    assert check_specs([{"v_out": 3.5}], {"v_out": {"max": 3.4}, "t_rise": {"max": 1e-6}})["verdict"] == "FAIL"


def test_a_measurement_missing_in_some_steps_is_unverified():
    # Adım 2'de hesaplanamayan ölçüm: kalan adımlar sınır içinde olsa da spec doğrulanmış sayılmaz.
    (row,) = check_specs([{"v": 1.0}, {}, {"v": 1.0}], {"v": {"max": 2.0}})["results"]
    assert row["status"] == "UNVERIFIED" and row["reason"].count("adım [2]") == 1


def test_names_are_case_insensitive_and_bounds_may_use_spice_suffixes():
    # LTspice ölçüm adlarını küçük harfe çevirir (tools._yield_analysis belgesi); "1u" = 1e-6 (quantizer).
    (row,) = check_specs([{"t_rise": 0.5e-6}], {"T_RISE": {"max": "1u"}})["results"]
    assert (row["status"], row["max"]) == ("PASS", 1e-6) and row["margin"] == pytest.approx(0.5e-6)
    # check_specs herkese açık: adım tablosundaki ad da harfe duyarsız (docstring), yalnız spec adı değil
    (row,) = check_specs([{"T_RISE": 0.5e-6}], {"t_rise": {"max": "1u"}})["results"]
    assert (row["status"], row["worst_value"]) == ("PASS", 0.5e-6)


@pytest.mark.parametrize("specs, message", [
    ({}, "en az bir spec"),
    ({"v": 3.3}, "biçiminde olmalı"),
    ({"v": {}}, "en az bir sınır"),
    ({"v": {"limit": 1}}, "bilinmeyen alan"),
    ({"v": {"min": 2, "max": 1}}, "max'tan"),
    ({"v": {"max": float("nan")}}, "sonlu bir sayı"),
    ({"v": {"max": True}}, "sonlu bir sayı"),
])
def test_invalid_specs_are_rejected(specs, message):
    with pytest.raises(ValueError, match=message):
        check_specs([{"v": 1.0}], specs)


_BOUND = st.one_of(st.none(), st.floats(min_value=-1e3, max_value=1e3))


@given(values=st.lists(st.floats(min_value=-1e3, max_value=1e3), min_size=1, max_size=8), low=_BOUND, high=_BOUND)
def test_status_matches_a_direct_comparison(values, low, high):
    """Bağımsız oracle: her adımda doğrudan karşılaştırma (pay hesabı kullanılmadan)."""
    if low is None and high is None:
        high = 0.0
    if low is not None and high is not None and low > high:
        low, high = high, low
    (row,) = check_specs([{"v": v} for v in values], {"v": {"min": low, "max": high}})["results"]
    outside = [i + 1 for i, v in enumerate(values) if (low is not None and v < low) or (high is not None and v > high)]
    assert row["failed_steps"] == outside
    assert row["status"] == ("FAIL" if outside else "PASS")
    assert (row["margin"] < 0) == bool(outside)


# ------------------------------------------------------------------ verify_specs aracı (gerçek LTspice logları)
@pytest.fixture
def adapter(tmp_path):
    """LTspice çalıştırmayan adaptör; araç yalnız çalışma alanındaki .log'u okur."""
    adapter = LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path / "ws")
    adapter.workspace_dir.mkdir(parents=True, exist_ok=True)
    return adapter


def _log(adapter, circuit, fixture):
    shutil.copy(DATA / f"{fixture}.log.txt", adapter.workspace_dir / f"{circuit}.log")


def test_tool_judges_a_real_run(adapter):
    """ref26_flow_rc logu (LTspice 26.1.1): "v_max: MAX(V(out))=3.58834481239", "vr_max: MAX(V(in,out))=4.99999749785".
    Elle: v_max'ın payı min(3.58834481239 - 3.5, 3.6 - 3.58834481239) = 0.01165518761; vr_max 4.9 üst sınırını
    0.09999749785 aşar."""
    _log(adapter, "rc", "ref26_flow_rc")
    specs = {"v_max": {"min": 3.5, "max": 3.6}, "vr_max": {"max": 4.9}}
    res = handle_tool_call("verify_specs", {"circuit_name": "rc", "specs": specs}, adapter=adapter)
    assert res["status"] == "SUCCESS" and res["verdict"] == "FAIL"
    v_max, vr_max = res["results"]
    assert v_max["status"] == "PASS" and v_max["margin"] == pytest.approx(0.01165518761, abs=1e-11)
    assert vr_max["status"] == "FAIL" and vr_max["margin"] == pytest.approx(-0.09999749785, abs=1e-11)
    assert res["message"].count("Karar: FAIL") == 1


def test_tool_accepts_specs_as_json_text(adapter):
    # Model sözlüğü bazen JSON metni olarak gönderir (tools._coerce_dict).
    _log(adapter, "rc", "ref26_flow_rc")
    res = handle_tool_call("verify_specs", {"circuit_name": "rc", "specs": '{"v_max": {"max": 4}}'}, adapter=adapter)
    assert (res["status"], res["verdict"]) == ("SUCCESS", "PASS")


def test_tool_checks_each_step_of_a_stepped_run(adapter):
    """ref26_rc_tstart_step logu (LTspice 26.1.1, `.step param r list 1k 2k`): v_max adım 1 = 3.58834481239, adım 2 =
    2.69109368324. Ortalama (3.1397) 3.0 alt sınırını geçerdi; adım 2 sınırın 0.30890631676 altında kalır."""
    _log(adapter, "swept", "ref26_rc_tstart_step")
    res = handle_tool_call("verify_specs", {"circuit_name": "swept", "specs": {"v_max": {"min": 3.0}}}, adapter=adapter)
    (row,) = res["results"]
    assert (res["verdict"], res["steps"], row["worst_step"], row["failed_steps"]) == ("FAIL", 2, 2, [2])
    assert row["margin"] == pytest.approx(2.69109368324 - 3.0, abs=1e-11)


def test_tool_marks_a_measurement_ltspice_could_not_compute(adapter):
    """ref26_cmp_meas_fail logu (LTspice 26.1.1): 'Measurement "v_never" FAIL'ed' ve v_max = 3.58834481239."""
    _log(adapter, "probe", "ref26_cmp_meas_fail")
    specs = {"v_max": {"max": 4.0}, "v_never": {"max": 1.0}}
    res = handle_tool_call("verify_specs", {"circuit_name": "probe", "specs": specs}, adapter=adapter)
    v_max, v_never = res["results"]
    assert (res["verdict"], v_max["status"], v_never["status"]) == ("UNVERIFIED", "PASS", "UNVERIFIED")
    assert v_never["reason"].count("FAIL'ed") == 1


@pytest.mark.parametrize("args, fragment", [
    ({"circuit_name": "yok", "specs": {"v": {"max": 1}}}, "logu bulunamadı"),
    ({"circuit_name": "rc", "specs": {"v_max": {"min": 2, "max": 1}}}, "max'tan"),
    ({"circuit_name": "rc"}, "en az bir spec"),
])
def test_tool_errors(adapter, args, fragment):
    _log(adapter, "rc", "ref26_flow_rc")
    res = handle_tool_call("verify_specs", args, adapter=adapter)
    assert res["status"] == "ERROR" and res["message"].count(fragment) == 1


# ------------------------------------------------------------------ belirsizlik, kaynak, sayısal kontrol
# JCGM 106:2012 (docs/references jcgm-106-2012): s.18 3.3.11 koruma bandı, s.33 8.3.2 korumalı kabul, s.34 8.3.3 korumalı
# ret. Değerler 2'nin kuvvetleri: farklar kayan noktada tam (4.0 - 3.5 = 0.5 = U).
@pytest.mark.parametrize("value, status", [
    (3.5, "PASS"),         # pay 0,5 = U: kabul sınırı dahil
    (3.75, "UNVERIFIED"),  # pay 0,25 < U: sınır içinde ama koruma bandında (korumalı kabul)
    (4.25, "UNVERIFIED"),  # pay -0,25 > -U: sınır dışında ama bandın içinde (korumalı ret)
    (4.5, "UNVERIFIED"),   # pay -0,5 = -U: TU + U kabul sınırı, s.18 3.3.8 Not "acceptance limits belong to the
                           # acceptance interval": korumalı rette kabul edilir, yani ret değil
    (4.75, "FAIL"),        # pay -0,75 < -U
])
def test_uncertainty_is_a_guard_band_on_both_sides(value, status):
    (row,) = check_specs([{"v": value}], {"v": {"max": 4.0, "uncertainty": 0.5, "source": "test"}})["results"]
    assert (row["status"], row["uncertainty"]) == (status, 0.5)
    assert row.get("reason", "").count("belirsizlik bandında") == (1 if status == "UNVERIFIED" else 0)


def test_numerical_uncertainty_adds_to_the_guard_band():
    # Pay 0,25. Yalnız spec belirsizliğiyle (0,125) kabul; sayısal fark 0,25 eklenince U = 0,375 > pay: bantta.
    spec = {"v": {"max": 4.0, "uncertainty": 0.125, "source": "test"}}
    assert check_specs([{"v": 3.75}], spec)["results"][0]["status"] == "PASS"
    (row,) = check_specs([{"v": 3.75}], spec, numerical={"V": 0.25})["results"]
    assert (row["status"], row["uncertainty"], row["numerical_uncertainty"]) == ("UNVERIFIED", 0.375, 0.25)
    # Ret tarafı: pay -0,25. Yalnız 0,125'lik bantla ret; sayısal fark eklenince -0,25 >= -0,375: bantta.
    assert check_specs([{"v": 4.25}], spec)["results"][0]["status"] == "FAIL"
    (row,) = check_specs([{"v": 4.25}], spec, numerical={"v": 0.25})["results"]
    assert (row["status"], row["uncertainty"]) == ("UNVERIFIED", 0.375)


_GRID = st.integers(min_value=-256, max_value=256).map(lambda n: n / 16)
_WIDTH = st.integers(min_value=0, max_value=64).map(lambda n: n / 16)


@given(values=st.lists(_GRID, min_size=1, max_size=8), low=st.one_of(st.none(), _GRID),
       high=st.one_of(st.none(), _GRID), uncertainty=_WIDTH, numerical=_WIDTH)
def test_guarded_status_matches_interval_membership(values, low, high, uncertainty, numerical):
    """Bağımsız oracle (JCGM 106:2012 8.3.2, 8.3.3; s.18 3.3.8 Not: kabul sınırları kabul aralığındadır): bütün adımlar
    [min + U, max - U] içindeyse PASS, bir adım [min - U, max + U] dışındaysa FAIL, yoksa UNVERIFIED; U = uncertainty +
    sayısal fark. Değerler 1/16'nın katları: toplam ve fark kayan noktada tam, iki formül yuvarlamayla ayrışamaz. Genel
    kayan nokta alanını U = 0 için test_status_matches_a_direct_comparison sınar."""
    if low is None and high is None:
        high = 0.0
    if low is not None and high is not None and low > high:
        low, high = high, low
    spec = {"v": {"min": low, "max": high, "uncertainty": uncertainty, "source": "test"}}
    (row,) = check_specs([{"v": v} for v in values], spec, numerical={"v": numerical})["results"]
    band = uncertainty + numerical

    def within(v, widen):
        return (low is None or v >= low - widen) and (high is None or v <= high + widen)

    expected = ("PASS" if all(within(v, -band) for v in values)
                else "FAIL" if not all(within(v, band) for v in values) else "UNVERIFIED")
    assert row["status"] == expected


def test_a_measurement_the_numerical_check_could_not_reproduce_is_unverified():
    (row,) = check_specs([{"v": 1.0}], {"v": {"max": 2.0, "source": "test"}}, numerical={"v": None})["results"]
    assert row["status"] == "UNVERIFIED" and row["reason"].count("sayısal kontrol") == 1


def test_a_spec_without_a_source_is_flagged():
    r = check_specs([{"v": 1.0, "w": 1.0}], {"v": {"max": 2.0}, "w": {"max": 2.0, "source": "gereksinim R-12"}})
    assert len(r["warnings"]) == 1 and r["warnings"][0].count("'v'") == 1
    assert "source" not in r["results"][0] and r["results"][1]["source"] == "gereksinim R-12"


@pytest.mark.parametrize("spec, message", [
    ({"max": 1, "uncertainty": -0.1}, "negatif"),
    ({"max": 1, "source": 3}, "metin olmalı"),
])
def test_invalid_uncertainty_or_source_is_rejected(spec, message):
    with pytest.raises(ValueError, match=message):
        check_specs([{"v": 1.0}], {"v": spec})


def test_numerical_spread_is_the_largest_change_over_reruns_and_steps():
    base = [{"a": 1.0}, {"a": 2.0}]
    # Farklar: 1. koşum 0,25 ve 0,5; 2. koşum -1 ve 0,125. En büyüğü (|-1|) ne ilk ne son (iki gezinme sırasında da),
    # işareti eksi: ilk, son, sıfırdan farklı ilk ya da işaretli fark 1'i vermez.
    good = [[{"A": 1.25}, {"a": 2.5}], [{"a": 0.0}, {"a": 2.125}]]
    assert numerical_spread(base, good, ["a"]) == {"a": 1.0}
    assert numerical_spread(base, [good[0], None], ["a"]) == {"a": None}       # yeniden koşum başarısız
    assert numerical_spread(base, [[{"a": 1.0}]], ["a"]) == {"a": None}         # adım sayısı tutmuyor
    assert numerical_spread(base, [[{"a": 1.0}, {}]], ["a"]) == {"a": None}     # ölçüm bir adımda yok


def test_tool_numerical_check_needs_the_netlist(adapter):
    # Model bayrağı metin gönderebilir: "false" kapalı, "true" açık (bool("false") doğru olurdu).
    _log(adapter, "rc", "ref26_flow_rc")
    args = {"circuit_name": "rc", "specs": {"v_max": {"max": 4, "source": "test"}}}
    off = handle_tool_call("verify_specs", {**args, "numerical_check": "false"}, adapter=adapter)
    assert off["status"] == "SUCCESS" and "numerical_check" not in off
    on = handle_tool_call("verify_specs", {**args, "numerical_check": "true"}, adapter=adapter)
    assert on["status"] == "ERROR" and on["message"].count("netlisti bulunamadı") == 1


@pytest.fixture
def replay(monkeypatch):
    """Gerçek sınır ``subprocess.Popen``: LTspice'a giden netlisti saklar, devre adına göre gerçek LTspice çıktılarını
    (examples/14 ``numerical``) netlistin yanına yazar."""
    import ltspice_llm.adapter as adapter_mod

    class _Replay:
        outputs = {}    # {devre adı: {".log": kayıt, ".raw": kayıt}}
        netlists = {}

        def __init__(self, cmd, **kwargs):
            netlist = Path(cmd[-1])
            type(self).netlists[netlist.stem] = netlist.read_text(encoding="utf-8")
            for suffix, source in type(self).outputs.get(netlist.stem, {}).items():
                netlist.with_suffix(suffix).write_bytes((DATA / source).read_bytes())
            self.pid, self.returncode = 999, 0

        def communicate(self, timeout=None):
            return "", ""

    monkeypatch.setattr(adapter_mod.subprocess, "Popen", _Replay)
    return _Replay


_RERUN = {suffix: {".log": f"ref26_flow_rc_{suffix}.log.txt", ".raw": f"ref26_flow_rc_{suffix}.raw"}
          for suffix in ("reltol", "gear")}


def _numerical_base(adapter):
    _log(adapter, "rc", "ref26_flow_rc")
    shutil.copy(DATA / "ref26_flow_rc.net", adapter.workspace_dir / "rc.net")


def test_tool_numerical_check_replays_real_reruns(adapter, replay):
    """LTspice 26.1.1 kayıtları (examples/14 ``flow`` ve ``numerical``):
    - taban ref26_flow_rc (method = trap): v_max 3.58834481239, vr_max 4.99999749785;
    - ref26_flow_rc_reltol (method = trap): v_max 3.58834481239, vr_max 4.99999749786;
    - ref26_flow_rc_gear (method = Gear): v_max 3.58834791183, vr_max 4.99999749785.
    Elle: v_max yayılımı 3.58834791183 - 3.58834481239 = 3.09944e-6 (Gear koşumundan), vr_max yayılımı 1e-11 (reltol
    koşumundan). v_max'ın 3.588346 sınırına payı 1.18761e-6: tek koşumda PASS, yayılım eklenince bantta. Tolerans
    kayan nokta: iki ondalık sayının ayrıştırma hatası 2 ulp'den küçük."""
    _numerical_base(adapter)
    replay.outputs = {"rc_reltol": _RERUN["reltol"], "rc_gear": _RERUN["gear"]}
    specs = {"v_max": {"max": 3.588346, "source": "test"}, "vr_max": {"max": 5.0, "source": "test"}}
    single = handle_tool_call("verify_specs", {"circuit_name": "rc", "specs": specs}, adapter=adapter)
    assert single["verdict"] == "PASS"
    res = handle_tool_call("verify_specs", {"circuit_name": "rc", "specs": specs, "numerical_check": True},
                           adapter=adapter)
    assert (res["status"], res["verdict"]) == ("SUCCESS", "UNVERIFIED"), res.get("message")
    v_max, vr_max = res["results"]
    assert v_max["status"] == "UNVERIFIED" and v_max["reason"].count("belirsizlik bandında") == 1
    assert v_max["numerical_uncertainty"] == pytest.approx(3.09944e-6, abs=2e-15)
    assert vr_max["status"] == "PASS" and vr_max["numerical_uncertainty"] == pytest.approx(1e-11, abs=2e-15)
    runs = res["numerical_check"]["runs"]
    assert [(run["circuit"], run["success"]) for run in runs] == [("rc_reltol", True), ("rc_gear", True)]
    assert runs[1]["gear_in_log"] is True
    # Oynatılan kayıt aracın gönderdiği netlistin kaydı olmalı: seçenek değişirse kayıt eskir (examples/14 numerical)
    for suffix in ("reltol", "gear"):
        sent = strip_auto_directives(replay.netlists[f"rc_{suffix}"])
        assert sent == (DATA / f"ref26_flow_rc_{suffix}.net").read_text(encoding="utf-8")


@pytest.mark.parametrize("outputs, run, field", [
    # Gear uygulanmadı: Gear koşumunda method = trap olan reltol kaydı döner
    ({"rc_reltol": _RERUN["reltol"], "rc_gear": _RERUN["reltol"]}, 1, "gear_in_log"),
    # .raw yazılamadı: log ölçümleri taşısa da adaptör koşumu başarısız sayar (run_netlist: analiz var, .raw yok)
    ({"rc_reltol": {".log": _RERUN["reltol"][".log"]}, "rc_gear": _RERUN["gear"]}, 0, "success"),
])
def test_tool_numerical_check_does_not_use_an_unusable_rerun(adapter, replay, outputs, run, field):
    # İki yeniden koşum da kullanılsaydı v_max'ın 4.0'a payı (0.41) yayılımdan (en çok 3.1e-6) büyük, karar PASS olurdu.
    _numerical_base(adapter)
    replay.outputs = outputs
    args = {"circuit_name": "rc", "specs": {"v_max": {"max": 4.0, "source": "test"}}, "numerical_check": True}
    res = handle_tool_call("verify_specs", args, adapter=adapter)
    (row,) = res["results"]
    assert res["numerical_check"]["runs"][run][field] is False
    assert (res["status"], res["verdict"], row["status"]) == ("SUCCESS", "UNVERIFIED", "UNVERIFIED")
    assert row["reason"].count("sayısal kontrol") == 1
