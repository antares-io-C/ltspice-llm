"""
LTspice Waveform and Bode Plot Visualizer
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
LTspice simülasyon çıktılarından (.raw) grafikler üreten,
çok modlu (multimodal) LLM'ler veya mühendisler için PNG/Base64 görseller oluşturan modül.

Figürler `pyplot` üzerinden değil doğrudan `matplotlib.figure.Figure` ile oluşturulur:
global backend seçimi (`matplotlib.use`) yapılmaz ve figür kayıt defterinde birikme olmaz.
"""

from __future__ import annotations

import base64
import io
import logging
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union

import numpy as np
from matplotlib.figure import Figure

from .parser import ALL_TRACES, open_raw, raw_analysis_kind, raw_axis_name, raw_axis_values, raw_steps, raw_wave
from .report import _decimate

logger = logging.getLogger(__name__)

#: Steplenmiş (.step) simülasyonlarda grafiğe bindirilen en fazla adım sayısı
MAX_PLOTTED_STEPS = 8

#: Uzun izler çizilmeden önce kova başına min/max ile seyreltilir (`report._decimate`): her kovanın tepesi ve dibi
#: korunur. 1500 piksel genişliğindeki PNG için 2000 kova. Ölçüm (examples/16, 1,12 M noktalı anahtarlama düğümü):
#: 2,47 sn / +659 MB -> 0,78 sn / +212 MB, piksellerin %0,0066'sı farklı. Gemini'nin önerdiği LTTB kova başına tek
#: nokta seçtiği için kovaların %40'ında tepeyi, %63'ünde dibi ve global minimumu kaybetti.
PLOT_BUCKETS = 2000


def _figure_to_base64(fig: Figure) -> str:
    """Figürü bellekte PNG'ye dönüştürüp Base64 metni olarak döner."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


class WaveformVisualizer:
    """Simülasyon dalga formlarını ve Bode diyagramlarını görselleştiren yardımcı sınıf."""

    @staticmethod
    def _find_trace(raw: Any, trace_names: List[str], name: str) -> Tuple[Optional[str], Any]:
        """Sinyali önce tam, sonra büyük/küçük harf duyarsız adla arar; (gerçek ad, veri) döner."""
        if name in trace_names:
            return name, raw.get_trace(name)
        for t in trace_names:
            if t.lower() == name.lower():
                return t, raw.get_trace(t)
        return None, None

    @classmethod
    def _build_trace_figure(
        cls,
        raw_path: Union[str, Path],
        signals: Optional[List[str]],
        title: str,
        figsize: Tuple[float, float] = (10, 5),
        grid: bool = True,
    ) -> Figure:
        """Zaman alanı / DC sweep sinyallerini çizen figürü oluşturur (steplenmişse adımlar bindirilir)."""
        raw_file = Path(raw_path)
        if not raw_file.exists():
            raise FileNotFoundError(f".raw dosyası bulunamadı: {raw_file}")

        raw = open_raw(raw_file, traces=ALL_TRACES)  # birden çok sinyal: tembel okuma her biri için dosyayı okur
        axis_name = raw_axis_name(raw) or "X"
        all_steps = raw_steps(raw)
        steps = all_steps[:MAX_PLOTTED_STEPS]

        available_traces = [t for t in raw.get_trace_names() if t != axis_name]
        if isinstance(signals, str):
            signals = [signals]
        if not signals:
            signals = available_traces[:3]

        fig = Figure(figsize=figsize)
        ax = fig.subplots()
        plotted = 0
        for sig in signals:
            sig_name, trace = cls._find_trace(raw, available_traces, sig)
            if sig_name is None or trace is None:
                logger.warning("'%s' sinyali bulunamadı. Mevcutlar: %s", sig, available_traces)
                continue

            plotted += 1
            for pos, step in enumerate(steps):
                data_arr = raw_wave(raw, sig_name, step)
                if np.iscomplexobj(data_arr):
                    data_arr = np.abs(data_arr)
                data_arr = np.nan_to_num(data_arr, nan=0.0, posinf=0.0, neginf=0.0)
                x = raw_axis_values(raw, step)
                if x is not None and np.iscomplexobj(x):
                    x = np.real(x)  # axis değerleri complex ise reel kısmını al
                if x is None:
                    x = np.arange(len(data_arr))
                x = np.asarray(x, dtype=float)
                if len(x) > 2 * PLOT_BUCKETS + 2 and np.all(np.diff(x) >= 0):  # geri dönen tarama seyreltilmez
                    x, data_arr = _decimate(x, np.asarray(data_arr, dtype=float), x[0], x[-1], buckets=PLOT_BUCKETS)
                label = sig_name if len(all_steps) == 1 else f"{sig_name} [step {pos + 1}]"
                ax.plot(x, data_arr, label=label, linewidth=1.5)

        if plotted == 0:
            # Boş grafiği "başarılı" diye döndürmek yerine çağırana (LLM'e) mevcut sinyalleri bildir.
            raise ValueError(f"İstenen sinyallerin hiçbiri bulunamadı: {list(signals)}. Mevcut: {available_traces}")

        if len(all_steps) > len(steps):
            title = f"{title} (ilk {len(steps)}/{len(all_steps)} adım)"
        ax.set_title(title, fontsize=12, fontweight="bold")
        unit_str = cls._guess_unit(axis_name)
        ax.set_xlabel(f"{axis_name} ({unit_str})", fontsize=10)
        ax.set_ylabel("Genlik", fontsize=10)
        if grid:
            ax.grid(True, linestyle="--", alpha=0.6)
        ax.legend(loc="best")
        fig.tight_layout()
        return fig

    @classmethod
    def plot_traces(
        cls,
        raw_path: Union[str, Path],
        signals: Optional[List[str]] = None,
        title: str = "LTspice Simülasyon Dalga Formu",
        output_png_path: Optional[Union[str, Path]] = None,
        figsize: tuple = (10, 5),
        grid: bool = True,
    ) -> Path:
        """
        Zaman alanı (Transient) veya DC sweep sinyallerini çizer ve PNG olarak kaydeder.
        """
        fig = cls._build_trace_figure(raw_path, signals, title, figsize=figsize, grid=grid)
        target_file = (
            Path(output_png_path)
            if output_png_path
            else Path(raw_path).with_suffix(".png")
        )
        target_file.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(str(target_file), dpi=150)
        return target_file

    @staticmethod
    def _phase_degrees(h: np.ndarray) -> np.ndarray:
        """Karmaşık transfer fonksiyonunun fazını derece cinsinden, ±180° atlamaları açılmış (unwrap) olarak verir."""
        phase = np.nan_to_num(np.angle(h), nan=0.0)
        return np.degrees(np.unwrap(phase))

    @classmethod
    def _build_bode_figure(
        cls,
        raw_path: Union[str, Path],
        out_node: str = "V(out)",
        in_node: Optional[str] = "V(in)",
        title: str = "Bode Frekans ve Faz Diyagramı",
        figsize: tuple = (10, 7),
    ) -> Figure:
        """Kazanç (dB) ve faz panellerini içeren Bode figürünü oluşturur (steplenmişse adımlar bindirilir)."""
        raw_file = Path(raw_path)
        if not raw_file.exists():
            raise FileNotFoundError(f".raw dosyası bulunamadı: {raw_file}")

        raw = open_raw(raw_file)
        kind = raw_analysis_kind(raw)
        if kind != "ac":
            axis = raw_axis_name(raw) or "eksensiz"
            raise ValueError(
                f"Bode diyagramı için .ac analizi gerekir; bu .raw dosyası '{axis}' eksenli bir "
                f"'{kind}' analizinden geliyor."
            )

        traces = raw.get_trace_names()
        out_name, _ = cls._find_trace(raw, traces, out_node)
        if out_name is None:
            raise ValueError(f"Çıkış sinyali '{out_node}' .raw dosyasında bulunamadı. Mevcut: {traces}")

        in_name: Optional[str] = None
        if in_node:
            in_name, _ = cls._find_trace(raw, traces, in_node)
            if in_name is None:
                logger.warning("Giriş sinyali '%s' bulunamadı; %s doğrudan transfer fonksiyonu sayıldı.", in_node, out_name)

        all_steps = raw_steps(raw)
        steps = all_steps[:MAX_PLOTTED_STEPS]

        fig = Figure(figsize=figsize)
        ax_mag, ax_phase = fig.subplots(2, 1, sharex=True)
        single = len(all_steps) == 1

        for pos, step in enumerate(steps):
            freq = np.abs(np.asarray(raw_axis_values(raw, step), dtype=complex))
            v_out = raw_wave(raw, out_name, step).astype(complex)
            if in_name is not None:
                h = v_out / (raw_wave(raw, in_name, step).astype(complex) + 1e-18)
            else:
                h = v_out

            mag_db = 20 * np.log10(np.maximum(np.abs(h), 1e-12))
            mag_db = np.nan_to_num(mag_db, nan=-120.0, posinf=100.0, neginf=-120.0)
            phase_deg = cls._phase_degrees(h)

            mag_kwargs = {"color": "#1f77b4"} if single else {}
            phase_kwargs = {"color": "#d62728"} if single else {}
            suffix = "" if single else f" [step {pos + 1}]"
            ax_mag.semilogx(freq, mag_db, linewidth=2, label=f"Kazanç |{out_name}|{suffix}", **mag_kwargs)
            ax_phase.semilogx(freq, phase_deg, linewidth=2, label=f"Faz ∠{out_name}{suffix}", **phase_kwargs)

        if len(all_steps) > len(steps):
            title = f"{title} (ilk {len(steps)}/{len(all_steps)} adım)"
        ax_mag.set_title(title, fontsize=12, fontweight="bold")
        ax_mag.set_ylabel("Kazanç (dB)", fontsize=10)
        ax_mag.grid(True, which="both", linestyle="--", alpha=0.6)
        ax_mag.legend(loc="best")

        ax_phase.set_xlabel("Frekans (Hz)", fontsize=10)
        ax_phase.set_ylabel("Faz (Derece °)", fontsize=10)
        ax_phase.grid(True, which="both", linestyle="--", alpha=0.6)
        ax_phase.legend(loc="best")

        fig.tight_layout()
        return fig

    @classmethod
    def plot_bode(
        cls,
        raw_path: Union[str, Path],
        out_node: str = "V(out)",
        in_node: Optional[str] = "V(in)",
        title: str = "Bode Frekans ve Faz Diyagramı",
        output_png_path: Optional[Union[str, Path]] = None,
        figsize: tuple = (10, 7),
    ) -> Path:
        """
        AC simülasyonundan profesyonel 2 panelli (Kazanç dB ve Faz Derece) Bode diyagramı çizer.
        `.ac` dışındaki bir analizin .raw dosyası verilirse `ValueError` fırlatır.
        """
        fig = cls._build_bode_figure(raw_path, out_node, in_node, title, figsize)
        raw_file = Path(raw_path)
        target_file = (
            Path(output_png_path)
            if output_png_path
            else raw_file.with_name(f"{raw_file.stem}_bode.png")
        )
        target_file.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(str(target_file), dpi=150)
        return target_file

    @classmethod
    def plot_bode_to_base64(
        cls,
        raw_path: Union[str, Path],
        out_node: str = "V(out)",
        in_node: Optional[str] = "V(in)",
        title: str = "Bode Frekans ve Faz Diyagramı",
    ) -> str:
        """Bode diyagramını bellekte doğrudan PNG'ye dönüştürüp Base64 formatında döndürür."""
        return _figure_to_base64(cls._build_bode_figure(raw_path, out_node, in_node, title))

    @classmethod
    def plot_to_base64(
        cls,
        raw_path: Union[str, Path],
        signals: Optional[List[str]] = None,
        title: str = "LTspice Simülasyon Çıktısı",
    ) -> str:
        """Dalga formunu bellekte doğrudan PNG'ye dönüştürüp Base64 formatında döndürür."""
        return _figure_to_base64(cls._build_trace_figure(raw_path, signals, title))

    @staticmethod
    def _guess_unit(axis_name: str) -> str:
        axis_lower = axis_name.lower()
        if "time" in axis_lower:
            return "saniye"
        if "freq" in axis_lower:
            return "Hz"
        return "birim"
