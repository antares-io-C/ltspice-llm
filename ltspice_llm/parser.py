"""
LTspice Log and Raw Output Parsers
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
LTspice'ın ürettiği .log ve .raw dosyalarını okuyan,
ölçümleri (.meas), çalışma noktalarını (.op), hataları ve dalga formlarını
LLM için anlamlı özetlere dönüştüren modül.
"""

from __future__ import annotations

import cmath
import codecs
import logging
import math
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)


#: `open_raw(..., traces=ALL_TRACES)`: bütün izler tek geçişte okunur
ALL_TRACES = "*"


def open_raw(raw_path: Union[str, Path, None], traces: Optional[str] = None) -> Any:
    """
    `.raw` dosyasını açar. Güncel `spicelib` tercih edilir, yoksa eski `PyLTSpice` kullanılır.

    ``traces=None``: yalnızca başlık okunur, izler istendikçe (tembel). ``traces=ALL_TRACES``: bütün izler tek
    geçişte okunur. Birden çok iz okunacaksa tek geçiş kullanın: spicelib 1.6.3 ikili .raw'da (LTspice varsayılanı,
    "Normal access") tembel okunan her iz için bütün veri bloğunu yeniden okuyor ve iz ona bakan bir görünüm olarak
    önbellekte kaldığı için blok bırakılmıyor. Ölçüm (examples/16): 67 MB .raw, 13 sinyal özeti +939 MB tepe bellek,
    tek geçişte +132 MB; değerler birebir aynı.

    Steplenmiş (.step) dosyanın adımları okunamıyorsa ValueError (ölçüm examples/18 T1, spicelib 1.6.3): ASCII .raw hiç
    okunamıyor; ikili .raw'ın adımları yanındaki .log'un ``.step`` satırlarından geliyor, .log yoksa iz okurken
    IndexError, .step satırı olmayan bir .log'da adımlar sessizce tek adımda birleşiyor.
    """
    if raw_path is None:
        # SimulationResult.raw_path None: koşum .raw üretmedi ya da sonuç sonraki bir koşumla geçersiz kılındı
        raise ValueError(
            ".raw yolu yok: koşum dalga formu üretmedi ya da bu sonuç, aynı circuit_name ile yapılan sonraki bir "
            "koşumla geçersiz kılındı (SimulationResult.superseded). Farklı devreler için ayrı circuit_name kullanın."
        )
    try:
        from spicelib import RawRead
    except ImportError:
        from PyLTSpice import RawRead  # type: ignore[no-redef]
    try:
        raw = RawRead(str(raw_path)) if traces is None else RawRead(str(raw_path), traces_to_read=traces)
    except IndexError as ex:
        raise ValueError(_unreadable_raw_message(Path(raw_path), ex)) from ex
    # LTspice tek değerli .step'i reddeder (ref26_step_single): steplenmiş .raw en az iki adımdır. Daha azı adımların
    # .log'dan okunamadığı demek (tembel açılışta hata ancak ilk iz okunurken çıkardı).
    if "stepped" in _raw_flags(raw) and len(list(raw.get_steps())) < 2:
        raise ValueError(_step_log_message(Path(raw_path)))
    return raw


def _raw_flags(raw: Any) -> str:
    """`.raw` başlığındaki ``Flags`` ("real forward stepped"...), küçük harfle; okunamazsa boş."""
    try:
        return str(raw.get_raw_property("Flags") or "").lower()
    except Exception:
        return ""


def _raw_header(raw_path: Path, limit: int = 1 << 20) -> Dict[str, str]:
    """
    `.raw` başlığının alanları ("Flags", "Plotname"...) ve veri bölümünün türü (``"data"``: ``"Binary"`` ya da
    ``"Values"`` = ASCII). LTspice 26.1.1 ikili .raw'ın başlığını UTF-16LE, ASCII .raw'ı 8 bit yazıyor
    (tests/data/ref26_step_dc_*). Okunamazsa boş sözlük.
    """
    data = b""
    match = None
    try:
        with open(raw_path, "rb") as f:
            while match is None and len(data) < limit:
                chunk = f.read(4096)
                if not chunk:
                    break
                data += chunk
                text = data.decode("utf-16-le" if data[1:2] == b"\x00" else "latin-1", errors="replace")
                match = re.search(r"^(Binary|Values):", text, re.MULTILINE)
    except OSError:
        return {}
    if match is None:
        return {}
    fields = {"data": match.group(1)}
    for line in text[: match.start()].splitlines():
        key, sep, value = line.partition(":")
        if sep and not line[:1].isspace():
            fields[key.strip()] = value.strip()
    return fields


def _step_log_message(raw_path: Path) -> str:
    log = raw_path.with_suffix(".log")
    try:
        steps = sum(line.startswith(".step") for line in LogParser.read_text(log).splitlines())  # spicelib gibi
    except OSError:
        steps = None
    if steps is None:
        why = f"'{log.name}' yok (silinmiş ya da taşınmış)"
    elif not steps:
        why = f"'{log.name}' içinde .step satırı yok (başka bir koşumun logu ya da okunamıyor)"
    else:
        why = f"'{log.name}' içindeki {steps} .step satırı .raw'daki adımlarla uyuşmuyor (başka bir koşumun logu)"
    return (f"'{raw_path.name}' steplenmiş (.step) bir .raw; spicelib adımları yanındaki .log'un .step satırlarından "
            f"okur ve {why}. Bu koşumun .log'unu .raw ile aynı klasöre aynı adla koyun ya da simülasyonu yeniden "
            "çalıştırın.")


def _unreadable_raw_message(raw_path: Path, ex: IndexError) -> str:
    header = _raw_header(raw_path)
    if "stepped" in header.get("Flags", "").lower():
        if header["data"] == "Values":
            return (f"'{raw_path.name}' steplenmiş (.step) bir ASCII .raw; spicelib steplenmiş ASCII dosyaları "
                    "okuyamıyor. Simülasyonu ikili (binary) raw biçiminde çalıştırın (LTSpiceAdapter(raw_format='auto') "
                    "ikili seçer).")
        return _step_log_message(raw_path)
    return (f"'{raw_path.name}' okunamadı (IndexError: {ex}). Steplenmiş (.step) dosyalarda iki bilinen neden var: "
            "ASCII .raw spicelib ile okunamıyor (ikili/binary biçimde çalıştırın); ikili .raw'ın adımları yanındaki "
            ".log'dan okunuyor (.log .raw ile aynı klasörde aynı adla olmalı).")


def raw_steps(raw: Any) -> List[int]:
    """`.raw` dosyasındaki adım (step) indeksleri; steplenmemiş dosyada `[0]`."""
    try:
        steps = list(raw.get_steps())
    except Exception:
        steps = []
    return steps or [0]


def raw_wave(raw: Any, name: str, step: int = 0) -> np.ndarray:
    """Bir sinyalin belirtilen adımdaki değerleri."""
    return np.asarray(raw.get_trace(name).get_wave(step))


def raw_axis_values(raw: Any, step: int = 0) -> Optional[np.ndarray]:
    """
    Belirtilen adımın X ekseni (zaman/frekans/tarama); eksen yoksa `None`.

    Eksen + başlıktaki ``Offset``: LTspice Tstart > 0 olan ``.tran``'da ekseni Tstart'tan değil 0'dan yazar ve kaymayı
    ``Offset``'e koyar; spicelib 1.6.3 bunu uygulamaz. Ölçüm (examples/18 T3, LTspice 26.1.1): ``.tran 0 3m 1m``'de
    eksen 0-2 ms, ``Offset`` 1 ms; eksen + Offset'teki geçişler LTspice'ın mutlak ``.meas WHEN`` zamanlarıyla aynı.
    ASCII ve steplenmiş .raw'da da aynı (her adım 0'dan); ``.ac``/``.dc``/``.op``/``.noise``/``.tf``'te Offset 0.
    """
    if not getattr(raw, "has_axis", False):
        return None
    try:
        axis = np.asarray(raw.get_axis(step))
    except RuntimeError:
        # spicelib (1.6) ikili dosyada ekseni ilk iz okunana kadar yüklemez ve o zamana kadar get_axis
        # "eksen yok" der. Eksen her zaman ilk izdir: onu okut, get_axis'i (negatif zaman düzeltmesiyle) yeniden çağır.
        names = raw.get_trace_names()
        if not names:
            return None
        raw.get_trace(names[0])
        axis = np.asarray(raw.get_axis(step))
    offset = _raw_offset(raw)
    return axis + offset if offset else axis


def _raw_offset(raw: Any) -> float:
    """`.raw` başlığındaki ``Offset`` (Tstart); yoksa ya da okunamazsa 0."""
    try:
        return float(raw.get_raw_property("Offset"))
    except Exception:
        return 0.0


def raw_axis_name(raw: Any) -> str:
    """
    X ekseninin adı ("time", "frequency", kaynak adı...); eksen yoksa boş metin.

    spicelib ikili (binary) dosyalarda `raw.axis` nesnesini `None` bırakır (ASCII'de doludur); bu
    durumda eksen her zaman ilk iz olduğundan onun adı kullanılır.
    """
    if not getattr(raw, "has_axis", False):
        return ""
    axis = getattr(raw, "axis", None)
    if axis is not None and getattr(axis, "name", None):
        return str(axis.name)
    names = raw.get_trace_names()
    return str(names[0]) if names else ""


def raw_analysis_kind(raw: Any) -> str:
    """
    Analiz türü: "tran", "ac", "noise", "dc" (tarama) veya "op" (eksensiz).

    Önce .raw başlığındaki `Plotname` ("Transient Analysis", "AC Analysis"...) kullanılır; okunamazsa
    eksen adına bakılır.
    """
    try:
        plot = str(raw.get_plot_name() or "").lower()
    except Exception:
        plot = ""
    if "transient" in plot:
        return "tran"
    if "noise" in plot:
        return "noise"
    if "ac analysis" in plot:
        return "ac"
    if "operating point" in plot:
        return "op"
    if plot.startswith("dc"):
        return "dc"

    axis = raw_axis_name(raw).lower()
    if axis == "time":
        return "tran"
    if axis == "frequency":
        return "ac"
    return "dc" if axis else "op"


@dataclass
class ParsedLog:
    """Ayrıştırılmış log verisi."""
    measurements: Dict[str, Any] = field(default_factory=dict)
    operating_points: Dict[str, float] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    circuit_title: Optional[str] = None
    elapsed_time_log: Optional[float] = None
    stepped_measurements: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    step_statistics: Dict[str, Dict[str, float]] = field(default_factory=dict)
    failed_measurements: List[str] = field(default_factory=list)
    step_parameters: List[Dict[str, float]] = field(default_factory=list)
    #: ``.four`` sonuçları (bkz. `parse_fourier_blocks`)
    fourier: List[Dict[str, Any]] = field(default_factory=list)
    #: ``convreport``/``.options debugtran`` puanları ve yakınsama hatası ayrıntısı (bkz. `parse_convergence_report`)
    convergence: Dict[str, Any] = field(default_factory=dict)


_NUM = r"[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"
# "Device Convergence Difficulty Score:" / "Node Convergence Difficulty Score:" ve altındaki "B1	 : 98.601642"
_SCORE_HEADER_RE = re.compile(r"^(?P<kind>Device|Node) Convergence Difficulty Score:\s*$", re.IGNORECASE)
_SCORE_LINE_RE = re.compile(rf"^(?P<name>\S+)\s*:\s*(?P<score>{_NUM})\s*$")
# "Convergence Failure:  Time step too small; time = 2e-13, timestep = 0: trouble with instance "B1""
_CONV_FAILURE_RE = re.compile(
    rf"time\s*step\s*too\s*small;\s*time\s*=\s*(?P<time>{_NUM})(?:,\s*timestep\s*=\s*(?P<step>{_NUM}))?",
    re.IGNORECASE,
)
_TROUBLE_WITH_RE = re.compile(
    r"trouble with (?:(?P<kind>node|instance)|\w+-instance)\s+\"?(?P<name>[^\"\s]+)\"?", re.IGNORECASE
)
# Çok satırlı biçim: "convergence trouble with nodes :" ardından her satırda bir ad
_TROUBLE_LIST_RE = re.compile(r"^convergence trouble with (?P<kind>nodes|instances)\s*:\s*$", re.IGNORECASE)

#: ADI LTspice Troubleshooting Guide: 1'in altındaki puanlar ihmal edilir, ~50'nin üstü sorunludur
CONVERGENCE_SCORE_THRESHOLD = 50.0


def parse_convergence_report(log_content: str) -> Dict[str, Any]:
    """
    ``.tran ... convreport`` ya da ``.options debugtran`` ile yazılan zorluk puanlarını ve yakınsama hatasının
    zamanını/sorunlu eleman ya da düğümlerini çıkarır.

    Dönüş (yalnızca bulunan anahtarlar): ``device_scores`` / ``node_scores`` (ad -> en yüksek puan, büyükten
    küçüğe), ``trouble_instances`` / ``trouble_nodes``, ``failure_time`` / ``failure_timestep``.
    """
    devices: Dict[str, float] = {}
    nodes: Dict[str, float] = {}
    trouble: Dict[str, List[str]] = {"instance": [], "node": []}
    result: Dict[str, Any] = {}
    mode: Optional[str] = None
    for raw in log_content.splitlines():
        line = raw.strip()
        header = _SCORE_HEADER_RE.match(line)
        if header:
            mode = header.group("kind").lower()
            continue
        trouble_list = _TROUBLE_LIST_RE.match(line)
        if trouble_list:
            mode = "trouble_" + ("node" if trouble_list.group("kind").lower() == "nodes" else "instance")
            continue
        if mode in ("device", "node"):
            score = _SCORE_LINE_RE.match(line)
            if score:
                table = devices if mode == "device" else nodes
                name, value = score.group("name"), float(score.group("score"))
                table[name] = max(value, table.get(name, value))
                continue
        if mode and mode.startswith("trouble_") and line and " " not in line and ":" not in line:
            target = trouble[mode[len("trouble_"):]]
            if line not in target:
                target.append(line)
            continue
        mode = None
        failure = _CONV_FAILURE_RE.search(line)
        if failure:
            result["failure_time"] = float(failure.group("time"))
            if failure.group("step") is not None:
                result["failure_timestep"] = float(failure.group("step"))
        for m in _TROUBLE_WITH_RE.finditer(line):
            kind = "node" if (m.group("kind") or "").lower() == "node" else "instance"
            if m.group("name") not in trouble[kind]:
                trouble[kind].append(m.group("name"))
    if devices:
        result["device_scores"] = dict(sorted(devices.items(), key=lambda kv: -kv[1]))
    if nodes:
        result["node_scores"] = dict(sorted(nodes.items(), key=lambda kv: -kv[1]))
    if trouble["instance"]:
        result["trouble_instances"] = trouble["instance"]
    if trouble["node"]:
        result["trouble_nodes"] = trouble["node"]
    return result


_FOURIER_HEADER_RE = re.compile(r"^Fourier components of (?P<signal>.+?)\s*$", re.IGNORECASE)
_FOURIER_DC_RE = re.compile(rf"^DC component:\s*(?P<v>{_NUM})", re.IGNORECASE)
_FOURIER_NPER_RE = re.compile(rf"^N-Period\s*=\s*(?P<v>{_NUM})", re.IGNORECASE)
_FOURIER_THD_RE = re.compile(rf"^(?P<kind>Partial|Total) Harmonic Distortion:\s*(?P<v>{_NUM})\s*%", re.IGNORECASE)


def parse_fourier_blocks(log_content: str) -> List[Dict[str, Any]]:
    """
    ``.four`` çıktısını (LTspice 26.1.1 biçimi) ayrıştırır. Her blok için::

        {"signal": "V(out)", "fundamental_hz": 1000.0, "dc": ..., "n_periods": 5.0,
         "thd_percent": 17.34, "partial_thd_percent": 17.32, "step": 2,   # yalnızca .step varsa
         "harmonics": [{"n": 1, "freq": 1000.0, "magnitude": 1.118, "normalized": 1.0,
                        "phase_deg": 90.0, "normalized_phase_deg": 0.0}, ...]}

    ADI: ``.four`` ve FFT için sıkıştırmayı kapatın (``.options plotwinsize=0``); LTspice 26'da varsayılan zaten kapalı.
    """
    blocks: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    steps_seen = 0
    for raw in log_content.splitlines():
        line = raw.strip()
        if LogParser._STEP_INFO_RE.match(line) and "=" in line:
            steps_seen += 1
            continue
        header = _FOURIER_HEADER_RE.match(line)
        if header:
            current = {"signal": header.group("signal"), "harmonics": []}
            if steps_seen:
                current["step"] = steps_seen
            blocks.append(current)
            continue
        if current is None:
            continue
        for regex, key in ((_FOURIER_DC_RE, "dc"), (_FOURIER_NPER_RE, "n_periods")):
            m = regex.match(line)
            if m:
                current[key] = float(m.group("v"))
                break
        else:
            thd = _FOURIER_THD_RE.match(line)
            if thd:
                key = "thd_percent" if thd.group("kind").lower() == "total" else "partial_thd_percent"
                current[key] = float(thd.group("v"))
                if key == "thd_percent":
                    current = None
                continue
            tokens = line.replace("°", " ").split()
            if len(tokens) >= 6 and tokens[0].isdigit():
                try:
                    n, freq, mag, norm, phase, norm_phase = (float(t) for t in tokens[:6])
                except ValueError:
                    continue
                current["harmonics"].append({
                    "n": int(n), "freq": freq, "magnitude": mag, "normalized": norm,
                    "phase_deg": phase, "normalized_phase_deg": norm_phase,
                })
                if int(n) == 1:
                    current["fundamental_hz"] = freq
    return blocks


@dataclass
class WaveformSummary:
    """LLM bağlam penceresini şişirmeyen sıkıştırılmış dalga formu özeti."""
    signal_name: str
    min_val: float
    max_val: float
    mean_val: float
    rms_val: float
    peak_to_peak: float
    sample_count: int
    is_complex: bool = False
    downsampled_points: List[float] = field(default_factory=list)
    step: Optional[int] = None  # steplenmiş simülasyonda 1 tabanlı adım numarası
    total_steps: int = 1

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "min": round(self.min_val, 5),
            "max": round(self.max_val, 5),
            "mean": round(self.mean_val, 5),
            "rms": round(self.rms_val, 5),
            "peak_to_peak": round(self.peak_to_peak, 5),
            "sample_count": self.sample_count,
            "is_complex": self.is_complex,
            "sampled_trend_preview": [round(x, 4) for x in self.downsampled_points],
        }
        if self.total_steps > 1:
            data["step"] = self.step
            data["total_steps"] = self.total_steps
        return data


class LogParser:
    """LTspice .log dosyası ayrıştırıcısı."""

    # Örnekler:
    # v_max: MAX(V(out))=3.6462 FROM 0 TO 0.005
    # f_cutoff: mag(V(out))=0.7071 AT 1000.05
    # max_gain: MAX(mag(V(out)))=(-8.69e-05dB,0°) FROM 100 TO 1000000
    # v_tau: V(out)=3.1606 at 0.001
    # t_debounce_fall=0.0009119 FROM 0.001 TO 0.0019
    #
    # LTspice, saf WHEN sorgusunun sonucunu büyük harf "AT" ile ("t: V(out)=2.5 AT 0.00069"),
    # FIND ... AT/WHEN sonucunu küçük harf "at" ile ("g: mag(V(out))=(-3dB,0°) at 1000") yazar.
    # Başlık satırları ("tnom = 27", "temp = 27") "=" öncesinde boşluk içerdiği için eşleşmez.
    MEAS_PATTERN = re.compile(
        r"^\s*([a-zA-Z0-9_]+)(?:\s*:|=)\s*(?:.*?[=:])?\s*\(?([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)(?:dB)?(?:,[^\)]*)?\)?(?:\s+(at|AT)\s+([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?))?"
    )
    # "MAX(...)=x AT y" gibi toplama fonksiyonlarında ana değer fonksiyonun sonucudur.
    _AGGREGATE_RE = re.compile(r"^\s*[:=]\s*(?:max|min|avg|rms|pp|integ)\s*\(", re.IGNORECASE)
    # Ölçüm olmayan ama "ad: ... sayı" biçimine uyan log satırları. LTspice 26 başlığa
    # "Options:method=gear plotwinsize=0 threads=12" yazar; eskiden her sonuca "Options" adlı sahte bir
    # ölçüm (0.0 ya da 12.0) olarak giriyordu.
    _NON_MEAS_NAMES = {"circuit", "date", "options"}
    # LTspice ölçüm sonucunu "<ad>: <ifade>=<değer> ..." ya da "<ad>=<değer> ..." biçiminde, adı küçük harfle yazar
    # (ref26_meas_names: netlistteki Offset_Error logda offset_error). Kendi iletilerini büyük harfle başlatır
    # ("ERROR:", "Circuit:", "Options:", "Measurement:"; tests/data'daki kayıtlar). Ölçüm satırında hata/uyarı sözcüğü
    # aranmaz: eskiden 'gain_error: MAX(V(out))=...' satırı "error:" içerdiği için hata sayılıyor, koşum FAILED oluyordu.
    _MEAS_RESULT_HEAD_RE = re.compile(r"^(?P<name>[a-z0-9_]+)(?::\s|=)")
    # "Files loaded:" listesindeki satır: sürücülü (C:\...) ya da kökten (\\sunucu, /...) bir dosya yolu
    _PATH_LINE_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/])")

    # Saf WHEN sonucu: "fc: mag(V(out))=1/sqrt(2) AT 1000.97". Eşik bir sayı, ad ya da ifade olabilir
    # (ADI'nin önerdiği "dc_gain/sqrt(2)" gibi). Eski desen eşiğin ilk sayısını (1/sqrt(2) -> 1.0) değer sanıyor,
    # eşik bir ad olduğunda ("=Vout_max AT 3162") ölçümü tamamen düşürüyordu.
    _WHEN_RESULT_RE = re.compile(
        r"^\s*(?P<name>[a-zA-Z0-9_]+):\s*(?P<body>.*\S)\s+AT\s+"
        r"(?P<at>[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*$"
    )

    # .ac ölçümleri karmaşık yazılır; biçim ".options meascplxfmt" ile seçilir (LTspice 26.1.1 çıktısı):
    #   bode (varsayılan): "(-3.006dB,-44.97°)"   polar: "(0.7075,-44.97°)"   cartesian: "(0.5005,-0.4999)"
    # Örn. ph(V(out)) = -45 -> bode "(33.06dB,180°)", polar "(44.97,180°)", cartesian "(-44.97,0)".
    _COMPLEX_RESULT_RE = re.compile(
        r"=\(\s*(?P<a>[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*(?P<db>dB)?\s*,\s*"
        r"(?P<b>[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)(?P<unit>[^)]*)\)"
    )
    # Toplama sarmalayıcısı atıldıktan sonra ifadenin en dış fonksiyonu
    _OUTER_FUNC_RE = re.compile(
        r"^\s*(?:(?:max|min|avg|rms|pp|integ)\s*\(\s*)?(?P<func>[a-z_]\w*)\s*\(", re.IGNORECASE
    )

    @classmethod
    def _decode_complex_meas(cls, name: str, rest_of_line: str) -> Optional[Dict[str, float]]:
        """
        Karmaşık .ac ölçüm sonucunu (bode/polar/cartesian biçimi) gerçek değerine çevirir.

        - mag()/abs(): bode biçiminde LTspice arayüzündeki gibi dB (kazanç için beklenen birim), polar ve
          cartesian biçimlerde doğrusal genlik döner.
        - db(): LTspice karmaşık logaritma verir; gerçel kısmı genliğin dB değeridir.
        - Sanal kısmı sıfır olan sonuçlar (ph, re, im, PARAM...): işaretli gerçel değer. Polar "(44.97,180°)"
          -44.97 demektir; eskiden işaret kayboluyordu.
        - Gerçekten karmaşık sonuçlar (örn. çıplak V(out)): bode'da dB genlik, polar'da doğrusal genlik +
          "<ad>_deg" faz açısı; cartesian'da gerçel kısım + "<ad>_im" sanal kısım.
        """
        m = cls._COMPLEX_RESULT_RE.search(rest_of_line)
        if not m:
            return None
        a = float(m.group("a"))
        b = float(m.group("b"))
        if m.group("db"):
            fmt = "bode"
            value = cmath.rect(10.0 ** (a / 20.0), math.radians(b))
            is_real = abs(math.sin(math.radians(b))) < 1e-9
        elif "°" in m.group("unit"):
            fmt = "polar"
            value = cmath.rect(a, math.radians(b))
            is_real = abs(math.sin(math.radians(b))) < 1e-9
        else:
            fmt = "cartesian"
            value = complex(a, b)
            is_real = b == 0.0

        expr = rest_of_line[: m.start()].lstrip(" :=")
        func_match = cls._OUTER_FUNC_RE.match(expr)
        func = func_match.group("func").lower() if func_match else ""

        if func in ("mag", "abs"):
            return {name: a if fmt == "bode" else abs(value)}
        if func == "db":
            return {name: value.real}
        if is_real:
            return {name: value.real}
        if fmt == "cartesian":
            return {name: value.real, f"{name}_im": value.imag}
        return {name: a, f"{name}_deg": b}

    OP_PATTERN = re.compile(
        r"^\s*([VvIi][xX]?\([a-zA-Z0-9_\.:#]+\))\s*:\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
    )

    # LTspice 26.1.1 yakınsama hatasını "Convergence Failure:  Time step too small; ..." ve
    # "Simulation Failed: Iteration limit reached" diye yazar. Eski listede yalnızca bitişik "timestep" vardı:
    # DC çalışma noktası bulunamayan koşum (rc=1) hatasız ve success=True görünüyordu.
    ERROR_KEYWORDS = [
        "fatal error",
        "error:",
        "singular matrix",
        "timestep too small",
        "time step too small",
        "convergence failure",
        "simulation failed",
        "more than one analysis",
        "trouble with node",
        "unknown subcircuit",
        "analysis failed",
        "cannot find",
        "syntax error",
        "already defined elsewhere",
        "over-defined",
        "need to correct",
    ]

    WARNING_KEYWORDS = [
        "warning:",
        "heightened tolerance",
        "pseudo transient",
        "questionable use of",
        "trouble finding operating point",
    ]

    # Derleyici hata formatı: path/file.net(12): Message
    COMPILER_MSG_PATTERN = re.compile(r"^.+?\.(?:net|cir|sp|asc)\(\d+\):\s*(.+)$", re.IGNORECASE)
    # Aynı "dosya(satır): mesaj" biçiminde yazılan ama koşumu durdurmayan bildirimler (LTspice 26.1.1, rc=0,
    # .raw üretiliyor). Eskiden hata sayılıyor, Trise=0 olan her PULSE kaynağı koşumu FAILED gösteriyordu.
    NONFATAL_COMPILER_RE = re.compile(
        r"limiting (?:rise|fall) time|removed duplicate values|limiting trapdamp", re.IGNORECASE
    )
    # Derleyici mesajının altında LTspice sorunlu satırı ve "^^^^" işaretini yazar
    _CARET_LINE_RE = re.compile(r"^\s*\^+\s*$")
    _ZERO_EDGE_NOTE = (
        "LTspice sıfır verilen yükselme/düşme süresini kendisi sınırladı (ölçümde Ton/10): dalga biçimi ve "
        "ortalaması değişir. PULSE(...) içinde Trise/Tfall'u açıkça yazın (örn. 1n)."
    )

    @classmethod
    def _is_measurement_line(cls, line: str) -> bool:
        """
        Satır bir ``.meas`` sonucu mu: küçük harfli ad, ardından ``:`` ya da ``=`` ve sayısal sonuç, ya da eşiği bir
        ifade olan saf WHEN sonucu (``t_error: V(out)=v_max/2 AT 0.00044``; satırda sayı yalnızca AT'den sonra,
        ref26_meas_names). Küçük harf ölçütü savunmadır: kayıtlarda onunla sınıf değiştiren bir LTspice iletisi yok.
        """
        if not cls._MEAS_RESULT_HEAD_RE.match(line):
            return False
        when = cls._WHEN_RESULT_RE.match(line)
        return bool(cls.MEAS_PATTERN.match(line) or (when and "=" in when.group("body")))

    @classmethod
    def parse_file(cls, log_path: Union[str, Path]) -> ParsedLog:
        """Belirtilen log dosyasını çoklu kodlama (encoding) desteğiyle okur ve sonuçları döner."""
        path = Path(log_path)
        if not path.exists():
            return ParsedLog(errors=[f"Log dosyası bulunamadı: {path}"])

        return cls.parse_text(cls.read_text(path))

    @classmethod
    def read_text(cls, log_path: Union[str, Path]) -> str:
        """Log dosyasını doğru kodlamayla (UTF-16/UTF-8/CP1252) çözüp metin olarak döner."""
        return cls._decode_log_bytes(Path(log_path).read_bytes())

    @staticmethod
    def _decode_log_bytes(data: bytes) -> str:
        """
        Log baytlarını çözer. LTspice XVII logları BOM'suz UTF-16LE yazar; bu baytlar geçerli
        UTF-8 de olduğundan (NUL karakterleriyle) önce UTF-16 işaretlerine bakılır.
        """
        if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            return data.decode("utf-16", errors="replace")
        if b"\x00" in data[:256]:
            return data.decode("utf-16-le", errors="replace")
        for enc in ("utf-8-sig", "cp1252"):
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("latin-1")

    # Hiçbir adımda sonuç üretemeyen ölçüm: 'Measurement "v_target" FAIL'ed'
    _MEAS_FAILED_RE = re.compile(r"^Measurement\s+\"(?P<name>[^\"]+)\"\s+FAIL'?ed", re.IGNORECASE)
    # Her adımın başında LTspice şunu yazar: ".step rv=1000 cv=1e-06" / ".step temp=-40°"
    _STEP_INFO_RE = re.compile(r"^\.step\s+(?P<body>.*)$", re.IGNORECASE)
    _STEP_PAIR_RE = re.compile(
        r"(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<val>[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
    )

    @classmethod
    def parse_text(cls, log_content: str) -> ParsedLog:
        """Log metnini satır satır inceler."""
        measurements: Dict[str, Any] = {}
        operating_points: Dict[str, float] = {}
        errors: List[str] = []
        warnings: List[str] = []
        failed_measurements: List[str] = []
        step_parameters: List[Dict[str, float]] = []
        circuit_title = None
        elapsed_time = None

        lines = log_content.splitlines()
        convergence = parse_convergence_report(log_content)
        fourier = parse_fourier_blocks(log_content)
        skip_until = -1
        score_block = False
        in_files = False  # "Files loaded:" altındaki dosya yolu listesi
        for idx, raw_line in enumerate(lines):
            if idx <= skip_until:
                continue
            line = raw_line.strip()
            if not line:
                score_block = in_files = False
                continue

            lower_line = line.lower()

            # LTspice netlistin tam yolunu başlıktaki "Circuit:" satırına ve "Files loaded:" listesine yazar. Bunlar ileti
            # değildir ve yolda bir hata sözcüğü geçebilir (ref26_path_keyword: "...\Simulation Failed cases\x.net");
            # eskiden alt dize araması bu satırlarda da yapılıyor, başarılı koşum FAILED görünüyordu.
            if lower_line.startswith("circuit:"):
                circuit_title = line.split(":", 1)[1].strip()
                continue
            if lower_line == "files loaded:":
                in_files = True
                continue
            if in_files and cls._PATH_LINE_RE.match(line):
                continue
            in_files = False

            # ".options debugtran"/"convreport" puan tabloları ("B1	 : 98.6") ölçüm sanılmamalı
            if _SCORE_HEADER_RE.match(line):
                score_block = True
                continue
            if score_block and _SCORE_LINE_RE.match(line):
                continue
            score_block = False

            # Başarısız ölçüm: LTspice hata satırı yazmaz, yalnızca bu satırı basar
            failed_match = cls._MEAS_FAILED_RE.match(line)
            if failed_match:
                failed_name = failed_match.group("name")
                failed_measurements.append(failed_name)
                warnings.append(
                    f"Ölçüm '{failed_name}' başarısız (FAIL'ed): koşul/tetikleyici hiç oluşmadı. "
                    ".meas ifadesini, eşik değerini veya simülasyon süresini kontrol edin."
                )
                continue

            # .step ilerleme satırı: adım numarası ile parametre değerlerinin eşlemesi
            step_match = cls._STEP_INFO_RE.match(line)
            if step_match:
                pairs = {
                    p.group("name").lower(): float(p.group("val"))
                    for p in cls._STEP_PAIR_RE.finditer(step_match.group("body"))
                }
                if pairs:
                    step_parameters.append(pairs)
                continue

            # Derleyici / Sözdizimi hatası tespiti: file(line): message. Altındaki kaynak satır ("^^^" işaretli)
            # mesaja eklenir; böylece LLM hangi direktifin reddedildiğini görür.
            compiler_match = cls.COMPILER_MSG_PATTERN.match(line)
            if compiler_match:
                message = line
                if (
                    idx + 2 < len(lines)
                    and lines[idx + 1].strip()
                    and cls._CARET_LINE_RE.match(lines[idx + 2])
                ):
                    message = f"{line} | satır: {lines[idx + 1].strip()}"
                    skip_until = idx + 2
                if cls.NONFATAL_COMPILER_RE.search(compiler_match.group(1)):
                    warnings.append(message)
                    if (
                        re.search(r"limiting (?:rise|fall) time", compiler_match.group(1), re.IGNORECASE)
                        and cls._ZERO_EDGE_NOTE not in warnings
                    ):
                        warnings.append(cls._ZERO_EDGE_NOTE)
                else:
                    errors.append(message)
                continue

            # Anahtar kelime tabanlı hata ve uyarı tespiti; ölçüm satırı taranmaz (bkz. _MEAS_RESULT_HEAD_RE)
            is_measurement = cls._is_measurement_line(line)
            if not is_measurement and any(k in lower_line for k in cls.ERROR_KEYWORDS):
                errors.append(line)
                continue
            if not is_measurement and any(k in lower_line for k in cls.WARNING_KEYWORDS):
                warnings.append(line)

            # Zaman tespiti
            if "total elapsed time:" in lower_line:
                match = re.search(r"total elapsed time:\s*([0-9\.]+)", lower_line)
                if match:
                    try:
                        elapsed_time = float(match.group(1))
                    except ValueError:
                        pass

            # Saf WHEN sonucu: aranan değer büyük harf "AT" arkasındaki zaman/frekanstır (eşik ifade olabilir)
            when_match = cls._WHEN_RESULT_RE.match(line)
            if (
                when_match
                and when_match.group("name").lower() not in cls._NON_MEAS_NAMES
                and not cls._AGGREGATE_RE.match(":" + when_match.group("body"))
                and "=" in when_match.group("body")
            ):
                name = when_match.group("name")
                measurements[name] = float(when_match.group("at"))
                threshold = when_match.group("body").rsplit("=", 1)[1].strip()
                try:
                    measurements[f"{name}_val"] = float(threshold)
                except ValueError:
                    pass  # eşik bir ifade/ad (ör. "1/sqrt(2)", "Vout_max"): sayısal karşılığı logda yok
                continue

            # .MEAS direktifleri
            meas_match = cls.MEAS_PATTERN.match(line)
            if meas_match and meas_match.group(1).lower() not in cls._NON_MEAS_NAMES:
                name = meas_match.group(1)
                val_str = meas_match.group(2)
                at_kw = meas_match.group(3)
                at_str = meas_match.group(4)

                val = float(val_str)
                at_val = float(at_str) if at_str else None

                # Saf WHEN sorgusunda (büyük harf "AT") aranan değer AT arkasındaki zaman/frekanstır;
                # FIND ... AT/WHEN (küçük harf "at") ve toplama fonksiyonlarında ana değer "=" sonrasıdır.
                rest_of_line = line[meas_match.end(1):]
                is_when_result = (
                    at_val is not None and at_kw == "AT" and not cls._AGGREGATE_RE.match(rest_of_line)
                )
                if is_when_result:
                    measurements[name] = at_val
                    measurements[f"{name}_val"] = val
                else:
                    complex_vals = cls._decode_complex_meas(name, rest_of_line)
                    if complex_vals:
                        measurements.update(complex_vals)
                    else:
                        measurements[name] = val
                    if at_val is not None:
                        measurements[f"{name}_at"] = at_val
                continue

            # .OP Çalışma noktaları
            op_match = cls.OP_PATTERN.match(line)
            if op_match:
                node = op_match.group(1)
                val_str = op_match.group(2)
                try:
                    operating_points[node] = float(val_str)
                except ValueError:
                    pass

        stepped_measurements, step_statistics = cls._parse_stepped_measurements(log_content, step_parameters)
        for m_name, stats in step_statistics.items():
            if m_name not in measurements and "mean" in stats:
                measurements[m_name] = stats["mean"]

        if stepped_measurements:
            warnings.append(
                "'.step' kullanıldı: 'measurements' içindeki adıma bağlı ölçümler adımların ortalamasıdır; "
                "adım başına değerler için 'stepped_measurements', özet için 'step_statistics' alanlarına bakın."
            )
            for m_name, rows in stepped_measurements.items():
                failed_steps = [r.get("step") for r in rows if r.get(m_name) is None]
                if failed_steps:
                    warnings.append(
                        f"Ölçüm '{m_name}' şu adımlarda başarısız oldu (failed): {failed_steps}; "
                        "istatistikler yalnızca başarılı adımlardan hesaplandı."
                    )

        return ParsedLog(
            measurements=measurements,
            operating_points=operating_points,
            errors=errors,
            warnings=warnings,
            circuit_title=circuit_title,
            elapsed_time_log=elapsed_time,
            stepped_measurements=stepped_measurements,
            step_statistics=step_statistics,
            failed_measurements=failed_measurements,
            step_parameters=step_parameters,
            fourier=fourier,
            convergence=convergence,
        )

    _STEP_MEAS_START_RE = re.compile(r"^Measurement:\s*([a-zA-Z0-9_]+)", re.IGNORECASE)
    _STEP_NUMBER_CLEAN_RE = re.compile(r"^[(\[]|dB|[)\]°]$")

    @staticmethod
    def _split_columns(line: str) -> List[str]:
        """LTspice sütunları sekmeyle ayırır; ifade başlıkları boşluk içerebilir, bu yüzden önce sekme denenir."""
        parts = re.split(r"\t+", line.strip()) if "\t" in line.strip() else re.split(r"\s+", line.strip())
        return [p.strip() for p in parts if p.strip()]

    @classmethod
    def _parse_step_cell(cls, meas_name: str, header: str, token: str) -> Tuple[Any, Optional[float]]:
        """
        Tablo hücresini değere çevirir. `failed` -> None. Kutupsal karmaşık hücre "(<dB>dB,<açı>°)"
        tekil `.meas` ile aynı kurallarla çözülür. Dönüş: (değer, varsa faz açısı).
        """
        if token.lower() == "failed":
            return None, None
        if token.startswith("("):
            decoded = cls._decode_complex_meas(meas_name, f"{header}={token}")
            if decoded and meas_name in decoded:
                return decoded[meas_name], decoded.get(f"{meas_name}_deg")
        clean = cls._STEP_NUMBER_CLEAN_RE.sub("", token).split(",")[0].strip()
        try:
            return float(clean), None
        except ValueError:
            return token, None

    @classmethod
    def _parse_stepped_measurements(
        cls, log_content: str, step_parameters: Optional[List[Dict[str, float]]] = None
    ) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Dict[str, float]]]:
        """
        LTspice .step direktifleri sonucu üretilen ölçüm tablolarını ayrıştırır.

        Gerçek LTspice tablosu ``step <ifade> [FROM TO | at]`` biçimindedir; değer sütunu her zaman
        ``step``'ten sonraki ilk sütundur (başlık ölçüm adı değil, `.meas` ifadesidir). Başlığı ölçüm
        adına eşit olan bir sütun varsa o tercih edilir. Değer sütunu satırlarda ölçüm adıyla anahtarlanır,
        başarısız (`failed`) adımlar `None` olur ve istatistiklere girmez. `step_parameters` verilirse
        (``.step rv=1000`` satırlarından), adım parametreleri her satıra eklenir.
        """
        stepped_measurements: Dict[str, List[Dict[str, Any]]] = {}
        step_statistics: Dict[str, Dict[str, float]] = {}
        step_parameters = step_parameters or []

        lines = log_content.splitlines()
        i = 0
        n = len(lines)

        while i < n:
            line = lines[i].strip()
            match = cls._STEP_MEAS_START_RE.match(line)
            if match:
                meas_name = match.group(1)
                i += 1
                while i < n and not lines[i].strip():
                    i += 1
                if i >= n:
                    break

                raw_headers = cls._split_columns(lines[i])
                headers = [h.lower() for h in raw_headers]
                if len(headers) < 2 or headers[0] != "step":
                    continue

                if meas_name.lower() in headers[1:]:
                    value_idx = headers.index(meas_name.lower(), 1)
                else:
                    value_idx = 1

                i += 1
                rows: List[Dict[str, Any]] = []
                while i < n:
                    data_line = lines[i].strip()
                    if not data_line or cls._STEP_MEAS_START_RE.match(data_line) or data_line.lower().startswith("total elapsed"):
                        break

                    tokens = cls._split_columns(data_line)
                    if not tokens or not tokens[0].isdigit():
                        break

                    step_no = int(tokens[0])
                    row_dict: Dict[str, Any] = {"step": step_no}
                    if 0 < step_no <= len(step_parameters):
                        row_dict.update(step_parameters[step_no - 1])

                    for col_idx in range(1, min(len(headers), len(tokens))):
                        if col_idx == value_idx:
                            value, phase = cls._parse_step_cell(meas_name, raw_headers[col_idx], tokens[col_idx])
                            row_dict[meas_name] = value
                            if phase is not None:
                                row_dict[f"{meas_name}_deg"] = phase
                        else:
                            other, _ = cls._parse_step_cell(meas_name, raw_headers[col_idx], tokens[col_idx])
                            row_dict[headers[col_idx]] = other

                    rows.append(row_dict)
                    i += 1

                if rows:
                    stepped_measurements[meas_name] = rows
                    numeric_vals = [
                        float(r[meas_name])
                        for r in rows
                        if isinstance(r.get(meas_name), (int, float)) and math.isfinite(float(r[meas_name]))
                    ]
                    if numeric_vals:
                        arr = np.array(numeric_vals, dtype=float)
                        stats: Dict[str, float] = {
                            "min": round(float(np.min(arr)), 6),
                            "max": round(float(np.max(arr)), 6),
                            "mean": round(float(np.mean(arr)), 6),
                            "std": round(float(np.std(arr)), 6),
                            "count": len(numeric_vals),
                            "range": round(float(np.max(arr) - np.min(arr)), 6),
                        }
                        if len(numeric_vals) < len(rows):
                            stats["failed_steps"] = len(rows) - len(numeric_vals)
                        step_statistics[meas_name] = stats
                continue
            i += 1

        return stepped_measurements, step_statistics


#: LTspice ``.db`` Measurements.Type değerleri (ADI MEASURE-DATABASE-REFERENCE)
MEAS_DB_TYPES = {0: "param", 1: "find", 2: "range", 3: "when"}


def read_measure_db(db_path: Union[str, Path]) -> List[Dict[str, Any]]:
    """
    LTspice'ın (24.1.9+) ``.meas`` sonuçlarını yazdığı SQLite ``.db`` dosyasını okur.

    Log metninden farklı olarak değerler ``.options meascplxfmt`` biçiminden bağımsızdır ve tam duyarlıklıdır
    (Re/Im her zaman doğrusal karmaşık sayının bileşenleridir; ``mag()`` için doğrusal genlik, dB değil).
    Her satır bir (ölçüm, adım) çiftidir::

        {"name": "fc", "expression": "mag(V(out))=1/sqrt(2)", "type": "when", "step": 1,
         "value": 1000.97, "re": None, "im": None, "from": None, "to": None, "at": 1000.97,
         "success": True, "complex": False, "step_values": {"r": 1000.0}}

    ``value``: WHEN ölçümünde ``at`` (yatay eksen), diğerlerinde ``re``. Adlar LTspice'ta küçük harfe çevrilir.
    Steplenmemiş koşumda Steps tablosu boştur ve ``step_values`` ``{}`` olur.
    """
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f".db dosyası bulunamadı: {path}")
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        params = dict(cur.execute("SELECT Key, Name FROM Parameters"))
        step_values: Dict[int, Dict[str, float]] = {}
        for step, key, value in cur.execute("SELECT Step, Key, Value FROM Steps"):
            step_values.setdefault(int(step), {})[str(params.get(key, key))] = value
        meas = {
            row[0]: row[1:]
            for row in cur.execute("SELECT ID, Name, Expression, Type, Complex FROM Measurements")
        }
        records: List[Dict[str, Any]] = []
        for mid, step, re_, im, frm, to, at, success in cur.execute(
            'SELECT ID, Step, Re, Im, "FROM", "TO", AT, Success FROM Results ORDER BY ID, Step'
        ):
            name, expression, mtype, is_complex = meas.get(mid, (str(mid), "", None, 0))
            kind = MEAS_DB_TYPES.get(mtype, str(mtype))
            records.append({
                "name": name,
                "expression": expression,
                "type": kind,
                "step": int(step) if step is not None else None,
                "value": at if kind == "when" else re_,
                "re": re_,
                "im": im,
                "from": frm,
                "to": to,
                "at": at,
                "success": bool(success),
                "complex": bool(is_complex),
                "step_values": step_values.get(int(step), {}) if step is not None else {},
            })
        return records
    finally:
        con.close()


def time_weighted_mean_rms(t: np.ndarray, y: np.ndarray) -> Optional[Tuple[float, float]]:
    """
    Noktaları doğrusal birleştirilmiş dalga formunun zaman ortalaması ve RMS'i; zaman ekseni artmıyorsa ya da süre
    sıfırsa None.

    LTspice'ın zaman adımı değişkendir; aritmetik ortalamada her nokta eşit sayılır ve kenar çevresinde sıklaşan
    noktalar fazla ağırlık alır. Ortalama yamuk integralidir; RMS'te her aralıkta doğrusal ara değerin karesi tam
    integrallenir, Δt·(a² + ab + b²)/3 (y²'nin yamuk integrali kenarda fazla verir). Ölçüm (tests/data
    ref26_mean_pulse, LTspice 26.1.1): adım düzlükte 48,8 ns (Tstop/1024), kenarda 0,49 ps'ye iniyor; kenar noktaları
    çoğunlukla 5 V köşelerinde, yüksek seviye sürenin %10,0'ı ama noktaların %11,7'si. Ortalama ``.meas AVG``'den
    1e-12, RMS ``.meas RMS``'ten en çok 7,5e-6 oranında farklı; aritmetik ortalama %16,8 fazla.
    """
    if len(t) < 2 or len(t) != len(y):
        return None
    dt = np.diff(t)
    span = float(t[-1] - t[0])
    if span <= 0 or np.any(dt < 0):
        return None
    a, b = y[:-1], y[1:]
    mean = float(np.sum(dt * (a + b)) / (2.0 * span))
    mean_square = float(np.sum(dt * (a * a + a * b + b * b)) / (3.0 * span))
    return mean, float(np.sqrt(max(mean_square, 0.0)))


class RawParser:
    """LTspice .raw dalga formu dosyası ayrıştırıcısı."""

    @classmethod
    def analysis_kind(cls, raw_path: Union[str, Path]) -> str:
        """`.raw` dosyasının analiz türü: "tran", "ac", "dc" (tarama) veya "op"."""
        return raw_analysis_kind(open_raw(raw_path))

    @classmethod
    def summarize_file(
        cls, raw_path: Union[str, Path], preview_points: int = 15, max_steps: int = 3
    ) -> Dict[str, WaveformSummary]:
        """
        .raw dosyasındaki sinyalleri okur ve LLM için özet metrikler üretir.
        AC (karmaşık sayı) ve Transient (gerçel sayı) analizlerini otomatik tanır.

        Steplenmiş (.step) dosyalarda ilk `max_steps` adım ayrı ayrı özetlenir ve anahtarlar
        ``"V(out) [step 2]"`` biçimini alır; böylece adımlar tek bir özette karışmaz.

        Bütün izler tek geçişte okunur (bkz. `open_raw`): 67 MB .raw'da tepe bellek +939 MB -> +132 MB.

        ``.tran``'da ortalama ve RMS zamana göredir (`time_weighted_mean_rms`), LTspice'ın ``.meas AVG``/``RMS``'i
        gibi; diğer analizlerde noktaların aritmetik ortalamasıdır.

        Önizleme (`downsampled_points`) zamansız verilir ve okuyan onu eşit aralıklı sanır: ``.tran``'da zamanda eşit
        aralıklı noktalardaki doğrusal ara değerdir (LTspice'ın adımı değişken; ölçüm examples/18 T2), diğer
        analizlerde eşit indeks aralıklı noktalardır (``.ac dec`` zaten logaritmik eşit aralıklı).
        """
        raw = open_raw(raw_path, traces=ALL_TRACES)
        summaries: Dict[str, WaveformSummary] = {}

        axis_name = raw_axis_name(raw)
        steps = raw_steps(raw)
        total_steps = len(steps)
        trace_names = raw.get_trace_names()
        transient = raw_analysis_kind(raw) == "tran"

        for step_pos, step in enumerate(steps[: max(1, max_steps)]):
            time = raw_axis_values(raw, step) if transient else None
            if time is not None:
                time = np.real(time).astype(float)
            for name in trace_names:
                if name == axis_name:
                    continue

                try:
                    raw_arr = raw_wave(raw, name, step)
                    is_complex = np.iscomplexobj(raw_arr)

                    # AC analizinde veriler karmaşık (complex) sayıdır: genlik (magnitude) analiz edilir
                    arr = np.abs(raw_arr).astype(float) if is_complex else raw_arr.astype(float)
                    if arr.size == 0:
                        continue

                    valid_mask = np.isfinite(arr)
                    if not np.any(valid_mask):
                        continue
                    valid_arr = arr[valid_mask]

                    min_v = float(np.min(valid_arr))
                    max_v = float(np.max(valid_arr))
                    weighted = None
                    if time is not None and len(time) == len(arr):
                        weighted = time_weighted_mean_rms(time[valid_mask], valid_arr)
                    if weighted is not None:
                        mean_v, rms_v = weighted
                    else:
                        mean_v = float(np.mean(valid_arr))
                        rms_v = float(np.sqrt(np.mean(valid_arr ** 2)))

                    if len(valid_arr) <= preview_points:
                        downsampled = [float(x) for x in valid_arr]
                    elif weighted is not None and time is not None:  # zaman ekseni geçerli (artan, süre > 0)
                        t_valid = time[valid_mask]
                        grid = np.linspace(t_valid[0], t_valid[-1], preview_points)
                        downsampled = [float(x) for x in np.interp(grid, t_valid, valid_arr)]
                    else:
                        indices = np.linspace(0, len(valid_arr) - 1, preview_points, dtype=int)
                        downsampled = [float(valid_arr[i]) for i in indices]

                    key = name if total_steps == 1 else f"{name} [step {step_pos + 1}]"
                    summaries[key] = WaveformSummary(
                        signal_name=name,
                        min_val=min_v,
                        max_val=max_v,
                        mean_val=mean_v,
                        rms_val=rms_v,
                        peak_to_peak=max_v - min_v,
                        sample_count=len(valid_arr),
                        is_complex=is_complex,
                        downsampled_points=downsampled,
                        step=step_pos + 1 if total_steps > 1 else None,
                        total_steps=total_steps,
                    )
                except Exception as ex:
                    logger.warning("'%s' sinyali özetlenemedi ve atlandı: %s", name, ex)

        return summaries
