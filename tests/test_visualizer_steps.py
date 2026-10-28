"""
Steplenmiş (.step) raw dosyaları, analiz türü doğrulaması ve Base64 çıktıları için testler.
Gerçek .raw yerine spicelib'in kullanılan arayüzünü taklit eden sahte bir nesne kullanılır.
"""

import base64
from pathlib import Path

import numpy as np
import pytest

from ltspice_llm.parser import RawParser, open_raw, raw_analysis_kind
from ltspice_llm.visualizer import MAX_PLOTTED_STEPS, WaveformVisualizer


class _Named:
    def __init__(self, name):
        self.name = name


class _Trace:
    def __init__(self, waves):
        self._waves = waves

    def get_wave(self, step=0):
        return np.asarray(self._waves[step])


class _FakeRaw:
    """spicelib.RawRead'in bu projede kullanılan alt kümesi."""

    def __init__(self, axis_name, axis_waves, traces):
        self.has_axis = axis_name is not None
        self.axis = _Named(axis_name) if axis_name else None
        self._axis_waves = axis_waves
        self._traces = traces

    def get_steps(self):
        return range(len(self._axis_waves))

    def get_trace_names(self):
        names = list(self._traces)
        return ([self.axis.name] + names) if self.has_axis else names

    def get_trace(self, name):
        if self.has_axis and name == self.axis.name:
            return _Trace(self._axis_waves)
        return _Trace(self._traces[name])

    def get_axis(self, step=0):
        return np.asarray(self._axis_waves[step])


def _tran(n_steps):
    t = [np.linspace(0, 1, 50)] * n_steps
    out = [np.linspace(0, float(k + 1), 50) for k in range(n_steps)]
    return _FakeRaw("time", t, {"V(out)": out, "V(in)": [np.ones(50)] * n_steps})


def _ac(n_steps):
    f = np.logspace(1, 5, 40)
    h = [1.0 / (1.0 + 1j * f / (100.0 * (k + 1))) for k in range(n_steps)]
    return _FakeRaw("frequency", [f.astype(complex)] * n_steps, {"V(out)": h, "V(in)": [np.ones(40, complex)] * n_steps})


@pytest.fixture
def raw_file(tmp_path, monkeypatch):
    """Sahte raw'ı döndüren yardımcı: `use(raw)` dosya yolunu döner ve open_raw'ı yönlendirir."""
    path = tmp_path / "c.raw"
    path.write_text("x")

    def use(fake):
        monkeypatch.setattr("ltspice_llm.parser.open_raw", lambda p, traces=None: fake)
        monkeypatch.setattr("ltspice_llm.visualizer.open_raw", lambda p, traces=None: fake)
        return path

    return use


# ----------------------------------------------------------------------------- özet
def test_unstepped_summary_keeps_plain_keys(raw_file):
    summaries = RawParser.summarize_file(raw_file(_tran(1)))
    assert set(summaries) == {"V(out)", "V(in)"}
    assert "step" not in summaries["V(out)"].to_dict()


def test_stepped_summary_is_reported_per_step(raw_file):
    summaries = RawParser.summarize_file(raw_file(_tran(3)))
    assert set(summaries) == {f"{s} [step {k}]" for s in ("V(out)", "V(in)") for k in (1, 2, 3)}
    assert summaries["V(out) [step 1]"].max_val == pytest.approx(1.0)
    assert summaries["V(out) [step 3]"].max_val == pytest.approx(3.0)  # adımlar tek özette karışmaz
    d = summaries["V(out) [step 2]"].to_dict()
    assert d["step"] == 2 and d["total_steps"] == 3


def test_stepped_summary_is_limited_to_max_steps(raw_file):
    summaries = RawParser.summarize_file(raw_file(_tran(5)), max_steps=2)
    assert {s.step for s in summaries.values()} == {1, 2}
    assert all(s.total_steps == 5 for s in summaries.values())


def test_ac_summary_uses_magnitude(raw_file):
    summaries = RawParser.summarize_file(raw_file(_ac(1)))
    assert summaries["V(out)"].is_complex is True
    assert summaries["V(out)"].max_val == pytest.approx(1.0, abs=0.01)


def test_unreadable_trace_is_skipped_with_a_log_message(raw_file, caplog):
    fake = _tran(1)
    fake._traces["V(bad)"] = [np.array([])]  # boş sinyal atlanır
    fake._traces["V(err)"] = None  # okunurken istisna fırlatır
    with caplog.at_level("WARNING", logger="ltspice_llm.parser"):
        summaries = RawParser.summarize_file(raw_file(fake))
    assert "V(out)" in summaries and "V(err)" not in summaries and "V(bad)" not in summaries
    assert any("V(err)" in r.getMessage() for r in caplog.records)


# ----------------------------------------------------------------------- analiz türü
@pytest.mark.parametrize("axis,kind", [("time", "tran"), ("frequency", "ac"), ("V1", "dc"), (None, "op")])
def test_analysis_kind_from_axis(axis, kind):
    fake = _FakeRaw(axis, [np.arange(3.0)], {"V(out)": [np.arange(3.0)]})
    assert raw_analysis_kind(fake) == kind


def test_raw_analysis_kind_via_rawparser(raw_file):
    assert RawParser.analysis_kind(raw_file(_ac(2))) == "ac"


# ------------------------------------------------------------------------- dalga formu
def test_trace_figure_overlays_every_step(raw_file):
    fig = WaveformVisualizer._build_trace_figure(raw_file(_tran(3)), ["V(out)"], "T")
    labels = [line.get_label() for line in fig.axes[0].get_lines()]
    assert labels == ["V(out) [step 1]", "V(out) [step 2]", "V(out) [step 3]"]


def test_trace_figure_caps_steps_and_says_so_in_the_title(raw_file):
    n = MAX_PLOTTED_STEPS + 4
    fig = WaveformVisualizer._build_trace_figure(raw_file(_tran(n)), ["V(out)"], "T")
    assert len(fig.axes[0].get_lines()) == MAX_PLOTTED_STEPS
    assert f"ilk {MAX_PLOTTED_STEPS}/{n} adım" in fig.axes[0].get_title()


def test_trace_figure_unstepped_labels_are_plain(raw_file):
    fig = WaveformVisualizer._build_trace_figure(raw_file(_tran(1)), ["v(OUT)"], "T")  # büyük/küçük harf duyarsız
    assert [l.get_label() for l in fig.axes[0].get_lines()] == ["V(out)"]


def test_trace_figure_unknown_signal_lists_available_ones(raw_file):
    with pytest.raises(ValueError, match="Mevcut"):
        WaveformVisualizer._build_trace_figure(raw_file(_tran(1)), ["V(nope)"], "T")


def test_missing_signal_is_logged_when_others_exist(raw_file, caplog, capsys):
    with caplog.at_level("WARNING", logger="ltspice_llm.visualizer"):
        WaveformVisualizer._build_trace_figure(raw_file(_tran(1)), ["V(out)", "V(nope)"], "T")
    assert capsys.readouterr().out == ""  # print yerine logging
    assert any("V(nope)" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------- Bode
def test_bode_overlays_steps_with_unwrapped_phase(raw_file):
    fig = WaveformVisualizer._build_bode_figure(raw_file(_ac(3)), "V(out)", "V(in)")
    mag_ax, phase_ax = fig.axes
    assert len(mag_ax.get_lines()) == 3 and len(phase_ax.get_lines()) == 3
    assert mag_ax.get_lines()[0].get_ydata()[0] == pytest.approx(0.0, abs=0.1)  # DC'de 0 dB
    assert phase_ax.get_lines()[0].get_ydata()[-1] < -80  # tek kutup: -90 ddereceye yaklaşır


def test_bode_refuses_non_ac_raw(raw_file):
    with pytest.raises(ValueError, match=r"\.ac analizi gerekir.*'time'.*'tran'"):
        WaveformVisualizer._build_bode_figure(raw_file(_tran(1)), "V(out)", "V(in)")
    with pytest.raises(ValueError, match="eksensiz"):
        WaveformVisualizer._build_bode_figure(
            raw_file(_FakeRaw(None, [np.arange(1.0)], {"V(out)": [np.arange(1.0)]})), "V(out)", None)


def test_bode_warns_when_input_node_is_missing(raw_file, caplog):
    with caplog.at_level("WARNING", logger="ltspice_llm.visualizer"):
        WaveformVisualizer._build_bode_figure(raw_file(_ac(1)), "V(out)", "V(yok)")
    assert any("V(yok)" in r.getMessage() for r in caplog.records)


def test_bode_unknown_output_node_raises(raw_file):
    with pytest.raises(ValueError, match="bulunamadı"):
        WaveformVisualizer._build_bode_figure(raw_file(_ac(1)), "V(yok)", None)


def test_plot_bode_writes_png_next_to_raw(raw_file):
    path = raw_file(_ac(1))
    png = WaveformVisualizer.plot_bode(path)
    assert png.name == "c_bode.png" and png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_bode_and_trace_base64_are_valid_pngs(raw_file):
    path = raw_file(_ac(2))
    for b64 in (
        WaveformVisualizer.plot_bode_to_base64(path, "V(out)", "V(in)"),
        WaveformVisualizer.plot_to_base64(path, ["V(out)"]),
    ):
        assert base64.b64decode(b64)[:8] == b"\x89PNG\r\n\x1a\n"


# --------------------------------------------------------------------------- open_raw
def test_open_raw_explains_stepped_ascii_failure(monkeypatch, tmp_path):
    def boom(path):
        raise IndexError("list assignment index out of range")

    monkeypatch.setattr("spicelib.RawRead", boom)
    with pytest.raises(ValueError, match="binary"):
        open_raw(tmp_path / "s.raw")


def test_plot_traces_renders_a_png_from_a_real_raw_without_ltspice(tmp_path):
    raw = Path(__file__).parent / "data" / "ref26_flow_rc.raw"   # LTspice 26.1.1, examples/14
    png = WaveformVisualizer.plot_traces(raw, ["V(out)"], output_png_path=tmp_path / "rc.png")
    assert Path(png).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_plot_traces_figure_holds_the_real_waveform():
    raw = Path(__file__).parent / "data" / "ref26_flow_rc.raw"   # LTspice 26.1.1, examples/14
    fig = WaveformVisualizer._build_trace_figure(raw, ["V(out)"], "rc")
    line = fig.axes[0].lines[0]
    assert max(line.get_ydata()) == pytest.approx(3.58834481239, rel=1e-6)   # aynı koşumun .meas MAX V(out) (log)
    assert line.get_xdata()[-1] == pytest.approx(3e-3)                         # .tran 3m
