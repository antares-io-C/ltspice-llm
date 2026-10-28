"""
Simulation Report
~~~~~~~~~~~~~~~~~
Simülasyon sonuçlarından tek dosyalık HTML rapor üretir. Rapor şunları anlatır: her testin hangi soruya cevap
aradığı ve nasıl modellendiği, hangi bileşenin hangi düğümü ne kadar etkilediği (önce → sonra), yeni etkilerin
nasıl ele alındığı, dalga formları ve koşumların güvenilirlik kanıtı (logdaki iş parçacığı sayısı, integrasyon
yöntemi, uyarılar, Gear çapraz kontrolü).

Kural: rapordaki her sayı bir ``.meas`` ölçümünden ya da ``.raw`` dalga formundan gelir. `Effect`, `Table` ve
`Tile` değerleri çağıran betik `SimulationResult.measurements`'tan verir; elle yazılan tek sayı varsayımlardır
(simülasyonun girdisi). Etkiyi tek değişkenli bir varyant çiftinden hesaplayın (önce/sonra aynı düğüm).

Dosya kendi içinde tamamdır: grafikler satır içi SVG, betik ve stil gömülü; ağdan hiçbir şey yüklemez (yazı tipi
de: kuruluysa Archivo/IBM Plex, değilse sistem yazı tipi). LTspice gerektirmez (dalga formu okumak için
``read_trace`` `.raw` dosyasını açar).
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

import numpy as np

from .parser import LogParser, open_raw, raw_axis_values

__all__ = [
    "SimulationReport", "Study", "Effect", "Table", "Column", "LineChart", "BarChart", "Trace", "Tile",
    "Decision", "Finding", "Check", "read_trace", "log_facts", "format_value",
]

#: Durum anahtarları: (simge, CSS sınıfı). Renk hiçbir zaman tek başına anlam taşımaz; simge ve etiketle gelir.
_STATUS = {
    "good": ("✓", "st-good"),
    "warn": ("!", "st-warn"),
    "serious": ("▲", "st-serious"),
    "critical": ("✕", "st-critical"),
    "info": ("i", "st-info"),
}

_SI = [(1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p")]


# --------------------------------------------------------------------------- biçimlendirme
def format_value(value: Optional[float], unit: str = "", scale: float = 1.0, decimals: int = 3,
                 signed: bool = False) -> str:
    """``value/scale`` değerini sabit ondalıkla ve birimle yazar (ör. ``0.066, "mW", 1e-3, 0`` -> ``66 mW``)."""
    if value is None or not math.isfinite(float(value)):
        return "—"
    v = float(value) / scale
    if round(v, decimals) == 0:
        v = 0.0  # "-0.00" ve "+0.00" yazılmasın
    text = f"{v:+.{decimals}f}" if signed and v else f"{v:.{decimals}f}"
    text = text.replace("-", "−")
    return f"{text} {unit}".strip()


def _si_scale(max_abs: float) -> Tuple[float, str]:
    """Eksen için SI çarpanı: değerler 1-1000 aralığına düşsün (``2e-5`` -> ``(1e-6, "µ")``)."""
    if not max_abs or not math.isfinite(max_abs):
        return 1.0, ""
    for factor, prefix in _SI:
        if max_abs >= factor * 0.999:
            return factor, prefix
    return _SI[-1]


def _nice_step(span: float, count: int) -> float:
    raw = span / max(count, 1)
    if raw <= 0 or not math.isfinite(raw):
        return 1.0
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag * 1.0000001:
            return m * mag
    return 10 * mag


def _ticks(lo: float, hi: float, step: float) -> List[float]:
    """``step``'in [lo, hi] içindeki katları, her biri k·step (eskiden round(v, 12): 2.5 ps adımı 2 ps oluyordu)."""
    first = math.ceil(lo / step - 1e-9)
    return [k * step for k in range(first, math.floor(hi / step + 1e-6) + 1)]


def _tick_decimals(step: float) -> int:
    if step >= 1:
        return 0 if float(step).is_integer() else 1
    d = -math.floor(math.log10(step))
    return d + (0 if round(step * 10 ** d, 6).is_integer() else 1)


def _inline(text: str) -> str:
    """Kaçışlı metin; `kod` ve **kalın** işaretlerini destekler."""
    out = html.escape(text)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)


def _paras(text: str) -> str:
    return "".join(f"<p>{_inline(p.strip())}</p>" for p in re.split(r"\n\s*\n", text.strip()) if p.strip())


def _status(status: str, label: str) -> str:
    glyph, cls = _STATUS.get(status, _STATUS["info"])
    return (f'<span class="status {cls}"><span class="ico" aria-hidden="true">{glyph}</span>'
            f"{html.escape(label)}</span>")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "b"


# --------------------------------------------------------------------------- .raw ve log okuma
def read_trace(result_or_path: Any, expr: str) -> Tuple[np.ndarray, np.ndarray]:
    """
    Bir koşumun ``.raw`` dosyasından (x, y) dizisi okur. ``expr``: ``V(n)``, ``I(R1)`` ya da fark gerilimi
    ``V(a,b)`` (= V(a) − V(b)). Ad büyük/küçük harf duyarsızdır. Argüman `SimulationResult` ya da `.raw` yoludur.
    """
    path = getattr(result_or_path, "raw_path", result_or_path)
    raw = open_raw(path)
    names = {n.lower(): n for n in raw.get_trace_names()}

    def wave(name: str) -> np.ndarray:
        real = names.get(name.lower())
        if real is None:
            raise KeyError(f"'{name}' izi yok; mevcut izler: {', '.join(sorted(names.values()))}")
        return np.asarray(raw.get_trace(real).get_wave(0), dtype=float)

    m = re.fullmatch(r"\s*V\(\s*([^,()]+?)\s*,\s*([^,()]+?)\s*\)\s*", expr, re.IGNORECASE)
    y = wave(f"V({m.group(1)})") - wave(f"V({m.group(2)})") if m else wave(expr.strip())
    x = raw_axis_values(raw)  # ikili .raw'daki negatif zaman işaretleri burada düzeltilir
    if x is None:
        raise ValueError(f"'{path}' dosyasında eksen yok (.op koşumu dalga formu içermez).")
    return np.asarray(x, dtype=float), y


def log_facts(result: Any) -> Dict[str, Any]:
    """Logdan güvenilirlik bilgisi: çözücü iş parçacığı, integrasyon yöntemi, çözücü süresi."""
    facts: Dict[str, Any] = {"threads": None, "method": None, "solver_seconds": None}
    path = getattr(result, "log_path", None)
    if not path:
        return facts
    text = LogParser.read_text(path)
    if m := re.search(r"Maximum thread count:\s*(\d+)", text):
        facts["threads"] = int(m.group(1))
    if m := re.search(r"^method\s*=\s*(\w+)", text, re.MULTILINE):
        facts["method"] = m.group(1)
    if m := re.search(r"Total elapsed time:\s*([\d.]+)", text):
        facts["solver_seconds"] = float(m.group(1))
    return facts


def _decimate(x: np.ndarray, y: np.ndarray, x0: float, x1: float, buckets: int = 600) -> Tuple[np.ndarray, np.ndarray]:
    """Görünür aralıktaki izi seyreltir; her piksel kovasında min ve max korunur (tepeler kaybolmaz)."""
    order = np.argsort(x, kind="stable")
    x, y = x[order], y[order]
    start = max(int(np.searchsorted(x, x0, "left")) - 1, 0)
    stop = min(int(np.searchsorted(x, x1, "right")) + 1, len(x))
    x, y = x[start:stop], y[start:stop]
    if len(x) <= 2 * buckets:
        return x, y
    edges = np.searchsorted(x, np.linspace(x[0], x[-1], buckets + 1))
    keep = {0, len(x) - 1}
    for a, b in zip(edges[:-1], edges[1:]):
        if b > a:
            seg = y[a:b]
            keep.add(a + int(np.argmin(seg)))
            keep.add(a + int(np.argmax(seg)))
    idx = np.array(sorted(keep))
    return x[idx], y[idx]


# --------------------------------------------------------------------------- içerik nesneleri
@dataclass
class Trace:
    """Grafikteki bir seri. ``emphasis=False`` nötr gri çizilir (eski/temel durum)."""
    name: str
    x: Sequence[float]
    y: Sequence[float]
    emphasis: bool = True
    label_extremes: Tuple[str, ...] = ()  # ("max",) / ("min",) / ("max", "min"): uç değeri grafikte yaz
    slot: Optional[int] = None            # 1-3: kategorik renk yuvası; aynı varlık her grafikte aynı renk


@dataclass
class LineChart:
    title: str
    traces: List[Trace]
    x_label: str = "zaman"
    x_unit: str = "s"
    y_label: str = "gerilim"
    y_unit: str = "V"
    x_range: Optional[Tuple[float, float]] = None
    references: List[Tuple[float, str]] = field(default_factory=list)  # yatay referans çizgileri (değer, etiket)
    value_decimals: int = 2
    caption: str = ""


@dataclass
class BarChart:
    """Yatay çubuklar; ``highlight`` çubuğu vurgu renginde, diğerleri gri (tek seri: lejant yok)."""
    title: str
    bars: List[Tuple[str, Optional[float]]]
    unit: str = ""
    scale: float = 1.0
    decimals: int = 0
    highlight: Optional[str] = None
    caption: str = ""


@dataclass
class Column:
    key: str
    header: str
    unit: str = ""
    scale: float = 1.0
    decimals: int = 3


@dataclass
class Table:
    """Ölçüm tablosu: her satır bir varyant, her sütun bir ``.meas`` değeri (grafiklerin tablo karşılığı)."""
    columns: List[Column]
    rows: List[Tuple[str, Mapping[str, Optional[float]]]]
    highlight: Optional[str] = None
    caption: str = ""


@dataclass
class Effect:
    """
    Bir bileşen ya da ayar değişikliğinin bir ölçüme etkisi. ``before``/``after`` aynı düğümün, yalnızca bu
    değişiklikle ayrışan iki koşumdaki değeridir. ``better``: "lower", "higher" ya da None (yön yargısı yok).
    """
    component: str
    location: str
    measure: str
    before: Optional[float]
    after: Optional[float]
    unit: str = "V"
    scale: float = 1.0
    decimals: int = 2
    better: Optional[str] = "lower"
    note: str = ""
    study: str = ""

    def verdict(self) -> Tuple[str, str]:
        if self.before is None or self.after is None:
            return "warn", "ölçülemedi"
        delta = self.after - self.before
        if round(delta / self.scale, self.decimals) == 0:  # gösterilen değişim sıfır (format_value gibi yuvarlar)
            return "info", "değişmedi"
        if self.better is None:
            return "info", "değişti"
        improved = delta < 0 if self.better == "lower" else delta > 0
        return ("good", "iyileşti") if improved else ("serious", "kötüleşti")

    def change_text(self) -> str:
        if self.before is None or self.after is None:
            return "—"
        delta = format_value(self.after - self.before, self.unit, self.scale, self.decimals, signed=True)
        if self.before:
            pct = (self.after - self.before) / abs(self.before) * 100
            pct_text = "0 %" if round(pct) == 0 else f"{pct:+.0f} %".replace("-", "−")
            return f"{delta} ({pct_text})"
        return delta


@dataclass
class Tile:
    label: str
    value: str
    note: str = ""


@dataclass
class Decision:
    status: str
    label: str
    text: str


@dataclass
class Finding:
    """Yeni ortaya çıkan ya da yeni ele alınan bir etki: neden ortaya çıktı, nasıl modellendi, ne ölçüldü."""
    title: str
    cause: str
    handling: str
    result: str
    status: str = "info"
    status_label: str = "bilgi"


@dataclass
class Check:
    """Simülasyonun güvenilirlik denetimi (Gear farkı, DC çalışma noktası, pencerede oturma...)."""
    name: str
    value: str
    status: str
    detail: str = ""


@dataclass
class Study:
    """Bir test: soru (neden), yöntem (nasıl, hangi tek değişken), grafikler, ölçüm tablosu, etkiler, karar."""
    id: str
    title: str
    question: str
    method: str
    charts: List[Union[LineChart, BarChart]] = field(default_factory=list)
    table: Optional[Table] = None
    effects: List[Effect] = field(default_factory=list)
    decision: Optional[Decision] = None
    notes: List[str] = field(default_factory=list)


@dataclass
class _Run:
    study: str
    variant: str
    result: Any


# --------------------------------------------------------------------------- rapor
class SimulationReport:
    """
    Simülasyon raporu. İçerik eklendikten sonra `save` ile tek bir HTML dosyası yazılır.

    >>> rep = SimulationReport("Takma anı", "5 V girişin takma anı aşımı", meta=[("LTspice", "26.1.1")])
    >>> rep.add_study(Study("T1", "Takma anı", question="...", method="..."))
    >>> rep.add_runs("T1", [("yalnız seramik", result)])
    >>> rep.save("reports/takma_ani.html")
    """

    DISCLAIMER = ("Buradaki sayılar simülasyondur, donanım ölçümü değildir. Varsayımlar aşağıda listelenmiştir; "
                  "donanımda doğrulanması gerekenler “Açık konular” bölümündedir.")

    def __init__(self, name: str, title: str, subtitle: str = "", meta: Sequence[Tuple[str, str]] = (),
                 disclaimer: Optional[str] = None):
        self.name = name
        self.title = title
        self.subtitle = subtitle
        self.meta = list(meta)
        self.disclaimer = self.DISCLAIMER if disclaimer is None else disclaimer
        self.generated = _dt.datetime.now()
        self.tiles: List[Tile] = []
        self.decisions: List[Decision] = []
        self.studies: List[Study] = []
        self.findings: List[Finding] = []
        self.checks: List[Check] = []
        self.runs: List[_Run] = []
        self.assumptions: List[Tuple[str, str]] = []
        self.open_items: List[str] = []
        self.footer: str = ""
        self._chart_index = 0

    # ---- içerik
    def add_tile(self, label: str, value: str, note: str = "") -> None:
        self.tiles.append(Tile(label, value, note))

    def add_decision(self, status: str, label: str, text: str) -> None:
        self.decisions.append(Decision(status, label, text))

    def add_study(self, study: Study) -> Study:
        for effect in study.effects:
            effect.study = effect.study or study.id
        self.studies.append(study)
        return study

    def add_finding(self, finding: Finding) -> None:
        self.findings.append(finding)

    def add_check(self, name: str, value: str, status: str, detail: str = "") -> None:
        self.checks.append(Check(name, value, status, detail))

    def add_runs(self, study: str, runs: Sequence[Tuple[str, Any]]) -> None:
        self.runs.extend(_Run(study, variant, res) for variant, res in runs)

    def _auto_checks(self) -> List[Check]:
        """Koşum kayıtlarından otomatik denetimler: başarı, uyarılar, logdaki iş parçacığı sayısı."""
        if not self.runs:
            return []
        n = len(self.runs)
        ok = sum(1 for r in self.runs if r.result.success)
        warned = sum(1 for r in self.runs if r.result.warnings)
        known = [t for t in (log_facts(r.result)["threads"] for r in self.runs) if t is not None]
        checks = [
            Check("Başarılı koşum (logdan)", f"{ok} / {n}", "good" if ok == n else "critical",
                  "Başarı LTspice çıkış kodundan değil logdan belirlenir (`Simulation Failed` rc=0 dönebilir)."),
            Check("Uyarı veren koşum", str(warned), "good" if not warned else "warn",
                  "Ölümcül olmayan LTspice bildirimleri (`Limiting rise time` vb.) ve adaptör notları."),
        ]
        if known:
            checks.append(Check(
                "Logdaki en yüksek çözücü iş parçacığı", str(max(known)), "info",
                f"`Maximum thread count` satırı {len(known)} logda okundu. Eşzamanlı örneklerin toplamı çekirdek "
                "sayısını aşmamalı (`cpu_guard`)."))
        return checks

    # ---- HTML
    def to_html(self, standalone: bool = True) -> str:
        """``standalone=False``: doctype/head/body olmadan (yayımlama platformu iskeleti kendisi ekler)."""
        body = "".join([
            self._header(), self._toc(), self._summary(), self._effect_matrix(),
            "".join(self._study(s) for s in self.studies),
            self._findings(), self._quality(), self._assumptions(), self._footer(),
        ])
        page = (f"<title>{html.escape(self.name)}</title>\n<style>{_CSS}</style>\n"
                f'<div class="page">{body}</div>\n<script>{_JS}</script>\n')
        if not standalone:
            return page
        return ('<!doctype html>\n<html lang="tr">\n<head>\n<meta charset="utf-8">\n'
                '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
                f"</head>\n<body>\n{page}</body>\n</html>\n")

    def save(self, path: Union[str, Path], standalone: bool = True) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.to_html(standalone), encoding="utf-8")
        return p

    # ---- bölümler
    def _header(self) -> str:
        meta = "".join(f"<div><dt>{html.escape(k)}</dt><dd>{_inline(v)}</dd></div>" for k, v in self.meta)
        meta += f"<div><dt>Rapor</dt><dd>{self.generated:%Y-%m-%d %H:%M}</dd></div>"
        sub = f'<p class="lede">{_inline(self.subtitle)}</p>' if self.subtitle else ""
        note = (f'<p class="disclaimer">{_status("warn", "Simülasyon")}<span>{_inline(self.disclaimer)}</span></p>'
                if self.disclaimer else "")
        return (f'<header class="masthead"><div class="mast-text"><p class="eyebrow">Simülasyon raporu · LTspice</p>'
                f"<h1>{_inline(self.title)}</h1>{sub}</div><dl class=\"meta\">{meta}</dl></header>{note}")

    def _toc(self) -> str:
        items = [("ozet", "Özet")]
        if any(s.effects for s in self.studies):
            items.append(("etkiler", "Bileşen etkileri"))
        items += [(f"test-{_slug(s.id)}", f"{s.id} · {s.title}") for s in self.studies]
        if self.findings:
            items.append(("yeni-etkiler", "Yeni etkiler"))
        if self.checks or self.runs:
            items.append(("guvenilirlik", "Güvenilirlik"))
        if self.assumptions or self.open_items:
            items.append(("varsayimlar", "Varsayımlar"))
        links = "".join(f'<a href="#{a}">{html.escape(t)}</a>' for a, t in items)
        return f'<nav class="toc" aria-label="İçindekiler">{links}</nav>'

    def _summary(self) -> str:
        tiles = "".join(
            f'<div class="tile"><p class="tile-label">{_inline(t.label)}</p><p class="tile-value">{_inline(t.value)}</p>'
            + (f'<p class="tile-note">{_inline(t.note)}</p>' if t.note else "") + "</div>"
            for t in self.tiles)
        decisions = "".join(
            f'<li>{_status(d.status, d.label)}<span>{_inline(d.text)}</span></li>' for d in self.decisions)
        return ('<section id="ozet" class="block"><h2>Özet</h2>'
                + (f'<div class="tiles">{tiles}</div>' if tiles else "")
                + (f'<h3>Kararlar</h3><ul class="decisions">{decisions}</ul>' if decisions else "")
                + "</section>")

    @staticmethod
    def _effect_rows(effects: Sequence[Effect], with_study: bool) -> str:
        rows = []
        for e in effects:
            status, label = e.verdict()
            study = (f'<td class="mono"><a href="#test-{_slug(e.study)}">{html.escape(e.study)}</a></td>'
                     if with_study else "")
            note = f'<span class="cell-note">{_inline(e.note)}</span>' if e.note else ""
            rows.append(
                f"<tr>{study}<td><strong>{_inline(e.component)}</strong>{note}</td><td>{_inline(e.location)}</td>"
                f"<td>{_inline(e.measure)}</td>"
                f'<td class="num">{format_value(e.before, e.unit, e.scale, e.decimals)}</td>'
                f'<td class="num arrow" aria-hidden="true">→</td>'
                f'<td class="num"><strong>{format_value(e.after, e.unit, e.scale, e.decimals)}</strong></td>'
                f'<td class="num">{e.change_text()}</td><td>{_status(status, label)}</td></tr>')
        head = ("<th>Test</th>" if with_study else "") + (
            "<th>Bileşen / değişiklik</th><th>Nerede</th><th>Etkilenen ölçüm</th>"
            '<th class="num">Önce</th><th></th><th class="num">Sonra</th><th class="num">Değişim</th><th>Sonuç</th>')
        return (f'<div class="scroll"><table class="effects"><thead><tr>{head}</tr></thead>'
                f"<tbody>{''.join(rows)}</tbody></table></div>")

    def _effect_matrix(self) -> str:
        effects = [e for s in self.studies for e in s.effects]
        if not effects:
            return ""
        return ('<section id="etkiler" class="block"><h2>Hangi bileşen nereyi etkiledi</h2>'
                '<p class="section-lede">'
                + _inline("Her satır yalnızca o değişiklikle ayrışan iki koşumu karşılaştırır; önce ve sonra aynı "
                          "düğümde ölçüldü. Değerler `.meas` sonuçlarıdır.") + "</p>"
                f"{self._effect_rows(effects, True)}</section>")

    def _study(self, s: Study) -> str:
        charts = "".join(self._chart(c) for c in s.charts)
        table = self._table(s.table) if s.table else ""
        effects = (f"<h3>Bileşen etkileri</h3>{self._effect_rows(s.effects, False)}" if s.effects else "")
        notes = ("<ul class=\"notes\">" + "".join(f"<li>{_inline(n)}</li>" for n in s.notes) + "</ul>"
                 if s.notes else "")
        decision = (f'<div class="verdict">{_status(s.decision.status, s.decision.label)}'
                    f"<div>{_paras(s.decision.text)}</div></div>" if s.decision else "")
        return (f'<section id="test-{_slug(s.id)}" class="block study">'
                f'<h2><span class="sid">{html.escape(s.id)}</span>{_inline(s.title)}</h2>'
                f'<div class="qm"><div><h3>Neden: soru</h3>{_paras(s.question)}</div>'
                f"<div><h3>Nasıl: yöntem</h3>{_paras(s.method)}</div></div>"
                f"{charts}{table}{effects}{notes}{decision}</section>")

    def _table(self, t: Table) -> str:
        head = "<th>Varyant</th>" + "".join(
            f'<th class="num">{html.escape(c.header)}</th>' for c in t.columns)
        rows = []
        for name, values in t.rows:
            cls = ' class="hi"' if name == t.highlight else ""
            cells = "".join(
                f'<td class="num">{format_value(values.get(c.key), c.unit, c.scale, c.decimals)}</td>'
                for c in t.columns)
            rows.append(f"<tr{cls}><th scope=\"row\">{html.escape(name)}</th>{cells}</tr>")
        cap = f"<caption>{_inline(t.caption)}</caption>" if t.caption else ""
        return (f'<h3>Ölçümler</h3><div class="scroll"><table class="measure">{cap}<thead><tr>{head}</tr></thead>'
                f"<tbody>{''.join(rows)}</tbody></table></div>")

    def _chart(self, chart: Union[LineChart, BarChart]) -> str:
        self._chart_index += 1
        if isinstance(chart, BarChart):
            return self._bar_chart(chart)
        return self._line_chart(chart, self._chart_index)

    def _bar_chart(self, c: BarChart) -> str:
        values = [v for _, v in c.bars if v is not None]
        top = max(values) if values else 1.0
        rows = []
        for label, value in c.bars:
            pct = 0.0 if value is None or top <= 0 else max(value, 0) / top * 100
            cls = " is-hi" if label == c.highlight else ""
            rows.append(
                f'<div class="bar-row{cls}"><span class="bar-label">{html.escape(label)}</span>'
                f'<span class="bar-track"><span class="bar" style="width:{pct:.1f}%"></span></span>'
                f'<span class="bar-val">{format_value(value, c.unit, c.scale, c.decimals)}</span></div>')
        cap = f"<figcaption>{_inline(c.caption)}</figcaption>" if c.caption else ""
        return (f'<figure class="figure"><p class="fig-title">{_inline(c.title)}</p>'
                f'<div class="bars">{"".join(rows)}</div>{cap}</figure>')

    def _line_chart(self, c: LineChart, index: int) -> str:
        W, H, ml, mr, mt, mb = 760, 300, 62, 20, 18, 42
        px0, px1, py0, py1 = ml, W - mr, H - mb, mt  # py0: alt kenar
        prepared = [(t, np.asarray(t.x, float), np.asarray(t.y, float)) for t in c.traces]
        if c.x_range:
            x0, x1 = c.x_range
        else:
            x0 = min(float(x.min()) for _, x, _ in prepared)
            x1 = max(float(x.max()) for _, x, _ in prepared)
        series = [(t, *_decimate(x, y, x0, x1)) for t, x, y in prepared]
        visible = [y[(x >= x0) & (x <= x1)] for _, x, y in series]
        ys = np.concatenate([v for v in visible if len(v)] + [np.array([r for r, _ in c.references])])
        ylo, yhi = float(ys.min()), float(ys.max())
        if yhi - ylo < 1e-12:
            ylo, yhi = ylo - 1, yhi + 1
        pad = (yhi - ylo) * 0.06
        yf, yp = _si_scale(max(abs(ylo), abs(yhi)))
        ystep = _nice_step((yhi - ylo + 2 * pad) / yf, 5) * yf
        y0 = math.floor((ylo - pad) / ystep) * ystep
        y1 = math.ceil((yhi + pad) / ystep) * ystep
        xf, xp = _si_scale(max(abs(x0), abs(x1)))
        xstep = _nice_step((x1 - x0) / xf, 7) * xf

        def sx(v: float) -> float:
            return px0 + (v - x0) / (x1 - x0) * (px1 - px0)

        def sy(v: float) -> float:
            return py0 + (v - y0) / (y1 - y0) * (py1 - py0)

        colors, slot = [], 0
        for t, _, _ in series:
            if not t.emphasis:
                colors.append("var(--s0)")
            elif t.slot:
                colors.append(f"var(--s{max(1, min(t.slot, 3))})")
            else:
                slot += 1
                colors.append(f"var(--s{min(slot, 3)})")

        parts = [f'<rect x="{px0}" y="{py1}" width="{px1 - px0}" height="{py0 - py1}" class="plot-bg"/>']
        ydec, xdec = _tick_decimals(ystep / yf), _tick_decimals(xstep / xf)
        for v in _ticks(y0, y1, ystep):
            yy, label = sy(v), f"{v / yf:.{ydec}f}".replace("-", "−")
            parts.append(f'<line x1="{px0}" x2="{px1}" y1="{yy:.1f}" y2="{yy:.1f}" class="grid"/>')
            parts.append(f'<text x="{px0 - 8}" y="{yy + 4:.1f}" class="tick" text-anchor="end">{label}</text>')
        for v in _ticks(x0, x1, xstep):
            xx, label = sx(v), f"{v / xf:.{xdec}f}".replace("-", "−")
            parts.append(f'<line x1="{xx:.1f}" x2="{xx:.1f}" y1="{py0}" y2="{py0 + 5}" class="axis"/>')
            parts.append(f'<text x="{xx:.1f}" y="{py0 + 19}" class="tick" text-anchor="middle">{label}</text>')
        parts.append(f'<line x1="{px0}" x2="{px1}" y1="{py0}" y2="{py0}" class="axis"/>')
        parts.append(f'<text x="{px1}" y="{H - 6}" class="axis-title" text-anchor="end">'
                     f"{html.escape(c.x_label)} ({xp}{html.escape(c.x_unit)})</text>")
        parts.append(f'<text x="{px0 - 8}" y="{py1 - 6}" class="axis-title" text-anchor="end">'
                     f"{yp}{html.escape(c.y_unit)}</text>")
        for value, label in c.references:  # etiket sol uçta: geçici rejim analizlerinde başlangıç sakin bölgedir
            yy = sy(value)
            parts.append(f'<line x1="{px0}" x2="{px1}" y1="{yy:.1f}" y2="{yy:.1f}" class="ref"/>')
            parts.append(f'<text x="{px0 + 6}" y="{yy - 5:.1f}" class="ref-label" text-anchor="start">'
                         f"{html.escape(label)}</text>")
        order = sorted(range(len(series)), key=lambda i: series[i][0].emphasis)  # gri seriler altta
        for i in order:
            _, x, y = series[i]
            pts = " ".join(f"{sx(a):.1f},{sy(b):.1f}" for a, b in zip(x, y))
            parts.append(f'<polyline points="{pts}" class="line" style="stroke:{colors[i]}"/>')
        for i, (t, x, y) in enumerate(series):
            mask = (x >= x0) & (x <= x1)
            for kind in t.label_extremes:
                if not mask.any():
                    continue
                k = int(np.argmax(np.where(mask, y, -np.inf)) if kind == "max" else np.argmin(np.where(mask, y, np.inf)))
                cx, cy = sx(x[k]), sy(y[k])
                right = cx < px0 + (px1 - px0) * 0.75
                ty = cy - 9 if kind == "max" else cy + 17
                ty = min(max(ty, py1 + 12), py0 - 4)  # etiket çizim alanının dışına taşmasın
                text = format_value(y[k], c.y_unit, 1.0, c.value_decimals)
                parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4" class="dot" style="fill:{colors[i]}"/>')
                parts.append(f'<text x="{cx + (8 if right else -8):.1f}" y="{ty:.1f}" class="dlabel" '
                             f'text-anchor="{"start" if right else "end"}">{html.escape(text)}</text>')
        parts.append(f'<line class="cross" x1="0" x2="0" y1="{py1}" y2="{py0}" visibility="hidden"/>')
        parts += [f'<circle class="hover-dot" r="4" style="fill:{col}" visibility="hidden"/>' for col in colors]

        data = {
            "frame": {"px0": px0, "px1": px1, "py0": py0, "py1": py1, "x0": x0, "x1": x1, "y0": y0, "y1": y1},
            "xf": xf, "xu": f"{xp}{c.x_unit}", "xd": xdec + 1, "yd": c.value_decimals + 1, "yu": c.y_unit,
            "series": [{"name": t.name, "color": colors[i],
                        "x": [float(f"{v:.6g}") for v in x], "y": [float(f"{v:.6g}") for v in y]}
                       for i, (t, x, y) in enumerate(series)],
        }
        legend = ""
        if len(series) > 1:
            legend = '<ul class="legend">' + "".join(
                f'<li><span class="key" style="background:{colors[i]}"></span>{html.escape(t.name)}</li>'
                for i, (t, _, _) in enumerate(series)) + "</ul>"
        cap = f"<figcaption>{_inline(c.caption)}</figcaption>" if c.caption else ""
        names = ", ".join(t.name for t, _, _ in series)
        label = f"{c.title}: {names}. Değerler tabloda ve imleçle okunur."
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
        return (f'<figure class="figure"><p class="fig-title">{_inline(c.title)}</p>{legend}'
                f'<div class="chart" tabindex="0" role="img" aria-label="{html.escape(label)}" id="chart-{index}">'
                f'<svg viewBox="0 0 {W} {H}" preserveAspectRatio="xMidYMid meet">{"".join(parts)}</svg>'
                f'<div class="tip" hidden></div>'
                f'<script type="application/json" class="chart-data">{payload}</script></div>{cap}</figure>')

    def _findings(self) -> str:
        if not self.findings:
            return ""
        cards = "".join(
            f'<article class="finding"><header>{_status(f.status, f.status_label)}<h3>{_inline(f.title)}</h3></header>'
            f"<dl><div><dt>Neden ortaya çıktı</dt><dd>{_inline(f.cause)}</dd></div>"
            f"<div><dt>Nasıl ele alındı</dt><dd>{_inline(f.handling)}</dd></div>"
            f"<div><dt>Simülasyon sonucu</dt><dd>{_inline(f.result)}</dd></div></dl></article>"
            for f in self.findings)
        return (f'<section id="yeni-etkiler" class="block"><h2>Yeni etkiler ve nasıl ele alındı</h2>'
                f'<div class="findings">{cards}</div></section>')

    def _quality(self) -> str:
        if not (self.checks or self.runs):
            return ""
        labels = {"good": "tamam", "warn": "dikkat", "serious": "dikkat", "critical": "hata", "info": "bilgi"}
        checks = "".join(
            f'<li><span class="chk-name">{_inline(k.name)}</span><span class="chk-val">{_inline(k.value)}</span>'
            f"{_status(k.status, labels.get(k.status, 'bilgi'))}"
            + (f'<span class="chk-detail">{_inline(k.detail)}</span>' if k.detail else "") + "</li>"
            for k in self._auto_checks() + self.checks)
        rows = []
        for r in self.runs:
            facts, res = log_facts(r.result), r.result
            state = _status("good", "başarılı") if res.success else _status("critical", "başarısız")
            issues = "; ".join(list(res.errors) + list(res.warnings)) or "—"
            name = getattr(getattr(res, "netlist_path", None), "stem", "")
            rows.append(
                f'<tr><td class="mono"><a href="#test-{_slug(r.study)}">{html.escape(r.study)}</a></td>'
                f"<td>{html.escape(r.variant)}</td><td class=\"mono\">{html.escape(name)}</td><td>{state}</td>"
                f'<td class="num">{facts["threads"] if facts["threads"] is not None else "—"}</td>'
                f'<td class="mono">{html.escape(facts["method"] or "—")}</td>'
                f'<td class="num">{format_value(facts["solver_seconds"], "s", 1.0, 2)}</td>'
                f'<td class="num">{format_value(getattr(res, "elapsed_seconds", None), "s", 1.0, 2)}</td>'
                f"<td>{html.escape(issues)}</td></tr>")
        table = ""
        if rows:
            table = ('<h3>Koşum kaydı</h3><div class="scroll"><table class="runs"><thead><tr><th>Test</th>'
                     "<th>Varyant</th><th>Devre adı</th><th>Durum</th><th class=\"num\">İş parçacığı (log)</th>"
                     "<th>Yöntem (log)</th><th class=\"num\">Çözücü</th><th class=\"num\">Duvar saati</th>"
                     f"<th>Hata / uyarı</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>")
        return (f'<section id="guvenilirlik" class="block"><h2>Simülasyon güvenilirliği</h2>'
                f'<ul class="checks">{checks}</ul>{table}</section>')

    def _assumptions(self) -> str:
        if not (self.assumptions or self.open_items):
            return ""
        rows = "".join(f"<tr><th scope=\"row\">{_inline(k)}</th><td>{_inline(v)}</td></tr>"
                       for k, v in self.assumptions)
        table = (f'<h3>Model varsayımları</h3><div class="scroll"><table class="assume"><tbody>{rows}</tbody>'
                 "</table></div>" if rows else "")
        items = "".join(f"<li>{_inline(i)}</li>" for i in self.open_items)
        todo = f'<h3>Açık konular, donanımda ölçülecekler</h3><ul class="todo">{items}</ul>' if items else ""
        return f'<section id="varsayimlar" class="block"><h2>Varsayımlar ve açık konular</h2>{table}{todo}</section>'

    def _footer(self) -> str:
        from . import __version__
        extra = f" {_inline(self.footer)}" if self.footer else ""
        return (f'<footer class="foot">ltspice_llm {html.escape(__version__)} ile üretildi, '
                f"{self.generated:%Y-%m-%d %H:%M}.{extra}</footer>")


_CSS = r"""
/* Düzen: test föyü. Başlık bloğu + meta listesi, özet, etki matrisi; ardından her test aynı sırayla
   (soru · yöntem · grafik · ölçüm · etki · karar). Tek okuma sütunu, en çok 1120 px.
   Yazı tipleri ağdan yüklenmez (SECURITY.md): kuruluysa Archivo/IBM Plex, değilse sistem yazı tipi. */
:root{
  --paper:#f2f4f5;--sheet:#fcfcfb;--ink:#11161b;--ink-2:#49515a;--ink-3:#7a828b;
  --rule:#dde1e4;--axis:#c0c6cb;--accent:#1c5cab;--wash:#e4edf8;
  --s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s0:#9aa0a7;
  --good:#0ca30c;--warn:#fab219;--serious:#ec835a;--critical:#d03b3b;--info:#7a828b;
  --f-display:"Archivo","Arial Narrow",system-ui,sans-serif;
  --f-body:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
  --f-mono:"IBM Plex Mono",ui-monospace,"Cascadia Mono",Consolas,monospace;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --paper:#0d1013;--sheet:#171a1e;--ink:#eef1f3;--ink-2:#c0c6cc;--ink-3:#8b939b;
  --rule:#2a2f35;--axis:#3b4249;--accent:#6da7ec;--wash:#14243a;
  --s1:#3987e5;--s2:#d95926;--s3:#199e70;--s0:#6b7279;--info:#8b939b;color-scheme:dark}}
:root[data-theme="dark"]{
  --paper:#0d1013;--sheet:#171a1e;--ink:#eef1f3;--ink-2:#c0c6cc;--ink-3:#8b939b;
  --rule:#2a2f35;--axis:#3b4249;--accent:#6da7ec;--wash:#14243a;
  --s1:#3987e5;--s2:#d95926;--s3:#199e70;--s0:#6b7279;--info:#8b939b;color-scheme:dark}
*{box-sizing:border-box}
[hidden]{display:none!important}
body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.6 var(--f-body);-webkit-font-smoothing:antialiased}
.page{max-width:1120px;margin:0 auto;padding-inline:clamp(16px,4vw,40px);padding-block:32px 56px;display:grid;gap:28px}
h1,h2,h3{font-family:var(--f-display);font-stretch:85%;text-wrap:balance;margin:0;line-height:1.15}
h1{font-size:clamp(28px,4.4vw,44px);font-weight:750;letter-spacing:-.01em}
h2{font-size:24px;font-weight:700;display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
h3{font-size:13px;font-weight:650;text-transform:uppercase;letter-spacing:.07em;color:var(--ink-2);margin-top:6px}
p{margin:0}
.block>p+p,.qm p+p,.verdict p+p{margin-top:8px}
code,.mono{font-family:var(--f-mono);font-size:.9em}
code{background:var(--wash);border-radius:4px;padding:1px 5px;white-space:nowrap}
a{color:var(--accent);text-underline-offset:2px}
a:focus-visible,.chart:focus-visible{outline:2px solid var(--accent);outline-offset:3px;border-radius:4px}
.masthead{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(0,1fr);gap:28px;align-items:end;
  border-bottom:2px solid var(--ink);padding-bottom:22px}
.eyebrow{font:500 12px/1 var(--f-mono);letter-spacing:.12em;text-transform:uppercase;color:var(--ink-3);margin-bottom:14px}
.lede{color:var(--ink-2);font-size:17px;max-width:62ch;margin-top:12px}
.meta{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px 18px;margin:0;font-size:13px}
.meta div{border-top:1px solid var(--rule);padding-top:6px;min-width:0}
.meta dt{color:var(--ink-3);font:500 11px/1.3 var(--f-mono);text-transform:uppercase;letter-spacing:.08em}
.meta dd{margin:2px 0 0;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.disclaimer{display:flex;gap:12px;align-items:flex-start;background:var(--sheet);border:1px solid var(--rule);
  border-radius:8px;padding:12px 14px;font-size:14px;color:var(--ink-2)}
.toc{display:flex;flex-wrap:wrap;gap:6px 8px;font-size:13px}
.toc a{border:1px solid var(--rule);border-radius:999px;padding:4px 11px;text-decoration:none;color:var(--ink-2);background:var(--sheet)}
.toc a:hover{border-color:var(--accent);color:var(--accent)}
.block{display:grid;gap:14px;min-width:0}
.study{background:var(--sheet);border:1px solid var(--rule);border-radius:10px;padding:clamp(16px,3vw,28px)}
.sid{font:600 13px/1 var(--f-mono);color:var(--sheet);background:var(--ink);border-radius:5px;padding:5px 8px;letter-spacing:.04em}
.section-lede{color:var(--ink-2);max-width:72ch}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.tile{background:var(--sheet);border:1px solid var(--rule);border-radius:8px;padding:14px 16px;display:grid;gap:4px;align-content:start}
.tile-label{font-size:13px;color:var(--ink-2)}
.tile-value{font-family:var(--f-display);font-stretch:85%;font-size:30px;font-weight:700;line-height:1.1}
.tile-note{font-size:12.5px;color:var(--ink-3)}
.decisions,.notes,.todo,.checks{list-style:none;margin:0;padding:0;display:grid;gap:8px}
.decisions li{display:grid;grid-template-columns:auto minmax(0,1fr);gap:12px;align-items:start;
  padding:10px 0;border-top:1px solid var(--rule)}
.notes li,.todo li{padding-left:16px;position:relative;color:var(--ink-2)}
.notes li::before,.todo li::before{content:"";position:absolute;left:2px;top:.72em;width:6px;height:1px;background:var(--ink-3)}
.qm{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:24px}
.qm>div{display:grid;gap:6px;align-content:start;max-width:62ch}
.status{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;font-weight:600;white-space:nowrap;
  border-radius:999px;padding:2px 9px 2px 3px;background:color-mix(in srgb,var(--st) 14%,transparent);color:var(--ink)}
.status .ico{display:inline-grid;place-items:center;width:17px;height:17px;border-radius:50%;background:var(--st);
  color:var(--on-st,#fff);font-size:10.5px;font-weight:700;line-height:1}
.st-good{--st:var(--good)}.st-warn{--st:var(--warn);--on-st:#11161b}.st-serious{--st:var(--serious);--on-st:#11161b}
.st-critical{--st:var(--critical)}.st-info{--st:var(--info)}
.scroll{overflow-x:auto;min-width:0;border:1px solid var(--rule);border-radius:8px;background:var(--sheet)}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{text-align:left;padding:8px 12px;border-bottom:1px solid var(--rule);vertical-align:top}
thead th{font:500 11.5px/1.3 var(--f-mono);text-transform:uppercase;letter-spacing:.06em;color:var(--ink-3);background:var(--paper)}
tbody tr:last-child>*{border-bottom:0}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.arrow{color:var(--ink-3);padding-inline:0}
td .cell-note{display:block;color:var(--ink-3);font-size:12.5px;margin-top:2px}
.measure tbody th{font-weight:500;min-width:15em}
.measure tr.hi>*{background:var(--wash)}
caption{caption-side:bottom;text-align:left;padding:8px 12px;color:var(--ink-3);font-size:12.5px}
.figure{margin:0;display:grid;gap:8px;min-width:0}
.fig-title{font-weight:600}
figcaption{font-size:13px;color:var(--ink-3);max-width:80ch}
.legend{list-style:none;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:4px 18px;font-size:13px;color:var(--ink-2)}
.legend li{display:flex;align-items:center;gap:7px}
.legend .key{width:18px;height:2px;border-radius:2px}
.chart{position:relative;overflow-x:auto;background:var(--sheet);border:1px solid var(--rule);border-radius:8px}
.chart svg{display:block;width:100%;min-width:600px;height:auto}
.plot-bg{fill:var(--sheet)}
.grid{stroke:var(--rule);stroke-width:1}
.axis{stroke:var(--axis);stroke-width:1}
.tick{fill:var(--ink-3);font:11.5px var(--f-mono);font-variant-numeric:tabular-nums}
.axis-title{fill:var(--ink-3);font:11.5px var(--f-body)}
.ref{stroke:var(--ink-3);stroke-width:1;stroke-dasharray:4 4}
.ref-label{fill:var(--ink-2);font:11.5px var(--f-body);paint-order:stroke;stroke:var(--sheet);stroke-width:4px}
.line{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.dot,.hover-dot{stroke:var(--sheet);stroke-width:2}
.dlabel{fill:var(--ink);font:600 12px var(--f-mono);paint-order:stroke;stroke:var(--sheet);stroke-width:4px}
.cross{stroke:var(--ink-3);stroke-width:1}
.tip{position:absolute;top:12px;pointer-events:none;background:var(--sheet);border:1px solid var(--rule);border-radius:6px;
  box-shadow:0 4px 14px rgba(0,0,0,.12);padding:7px 10px;font-size:12.5px;display:grid;gap:3px;min-width:150px;z-index:2}
.tip .tx{color:var(--ink-3);font-family:var(--f-mono)}
.tip .row{display:grid;grid-template-columns:14px auto minmax(0,1fr);gap:8px;align-items:center}
.tip .k{height:2px;border-radius:2px}
.tip .v{font:600 12.5px var(--f-mono);font-variant-numeric:tabular-nums}
.tip .n{color:var(--ink-2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bars{display:grid;gap:7px;background:var(--sheet);border:1px solid var(--rule);border-radius:8px;padding:14px 16px}
.bar-row{display:grid;grid-template-columns:minmax(0,260px) minmax(0,1fr) 76px;gap:12px;align-items:center;font-size:13px}
.bar-label{color:var(--ink-2)}
.bar-track{height:14px;display:flex}
.bar{background:var(--s0);border-radius:0 4px 4px 0;min-width:2px}
.is-hi .bar{background:var(--s1)}
.is-hi .bar-label{color:var(--ink);font-weight:600}
.bar-val{font-family:var(--f-mono);font-variant-numeric:tabular-nums;text-align:right}
.verdict{display:grid;grid-template-columns:auto minmax(0,1fr);gap:12px;align-items:start;
  border-left:3px solid var(--ink);padding:4px 0 4px 14px}
.findings{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,330px),1fr));gap:12px}
.finding{background:var(--sheet);border:1px solid var(--rule);border-radius:8px;padding:16px;display:grid;gap:10px;align-content:start}
.finding header{display:grid;gap:8px;justify-items:start}
.finding h3{font-size:18px;text-transform:none;letter-spacing:0;color:var(--ink);margin:0}
.finding dl{margin:0;display:grid;gap:8px}
.finding dt{font:500 11px/1.3 var(--f-mono);text-transform:uppercase;letter-spacing:.08em;color:var(--ink-3)}
.finding dd{margin:2px 0 0;font-size:14px}
.checks li{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:4px 14px;align-items:center;
  padding:10px 0;border-top:1px solid var(--rule)}
.chk-val{font:600 14px var(--f-mono);font-variant-numeric:tabular-nums;text-align:right}
.chk-detail{grid-column:1/-1;color:var(--ink-3);font-size:13px}
.runs td,.runs th{font-size:12.5px;padding:6px 10px}
.assume th{width:24%;font-weight:600}
.foot{color:var(--ink-3);font-size:12.5px;border-top:1px solid var(--rule);padding-top:14px}
@media (max-width:760px){
  .masthead,.qm{grid-template-columns:minmax(0,1fr)}
  .bar-row{grid-template-columns:minmax(0,1fr) 70px}
  .bar-label{grid-column:1/-1}
  .checks li{grid-template-columns:minmax(0,1fr) auto}
  .checks li .status{grid-column:1/-1;justify-self:start}
}
@media (prefers-reduced-motion:no-preference){.toc a{transition:border-color .15s,color .15s}}
"""

_JS = r"""
(()=>{
const DEC='.';
function at(s,x){const xs=s.x;let lo=0,hi=xs.length-1;if(x<=xs[0])return s.y[0];if(x>=xs[hi])return s.y[hi];
  while(hi-lo>1){const m=(lo+hi)>>1;if(xs[m]<=x)lo=m;else hi=m}
  const t=(x-xs[lo])/((xs[hi]-xs[lo])||1);return s.y[lo]+t*(s.y[hi]-s.y[lo])}
function num(v,d){return v.toFixed(d).replace('-','−').replace('.',DEC)}
document.querySelectorAll('.chart').forEach(ch=>{
  let data;try{data=JSON.parse(ch.querySelector('script.chart-data').textContent)}catch(e){return}
  const svg=ch.querySelector('svg'),tip=ch.querySelector('.tip'),cross=svg.querySelector('.cross');
  const dots=[...svg.querySelectorAll('.hover-dot')],f=data.frame;
  const sx=x=>f.px0+(x-f.x0)/(f.x1-f.x0)*(f.px1-f.px0),sy=y=>f.py0+(y-f.y0)/(f.y1-f.y0)*(f.py1-f.py0);
  let cur=null;
  function hide(){cur=null;cross.setAttribute('visibility','hidden');dots.forEach(d=>d.setAttribute('visibility','hidden'));tip.hidden=true}
  function show(x){
    x=Math.min(f.x1,Math.max(f.x0,x));cur=x;const px=sx(x);
    cross.setAttribute('x1',px);cross.setAttribute('x2',px);cross.setAttribute('visibility','visible');
    tip.replaceChildren();const head=document.createElement('div');head.className='tx';
    head.textContent=num(x/data.xf,data.xd)+' '+data.xu;tip.appendChild(head);
    data.series.forEach((s,i)=>{const v=at(s,x);const d=dots[i];
      d.setAttribute('cx',px);d.setAttribute('cy',sy(v));d.setAttribute('visibility','visible');
      const row=document.createElement('div');row.className='row';
      const k=document.createElement('span');k.className='k';k.style.background=s.color;
      const val=document.createElement('span');val.className='v';val.textContent=num(v,data.yd)+' '+data.yu;
      const n=document.createElement('span');n.className='n';n.textContent=s.name;
      row.append(k,val,n);tip.appendChild(row)});
    tip.hidden=false;
    const m=svg.getScreenCTM();if(!m)return;
    const p=new DOMPoint(px,f.py1).matrixTransform(m),box=ch.getBoundingClientRect();
    let left=p.x-box.left+ch.scrollLeft+12;
    if(left+tip.offsetWidth>ch.scrollLeft+ch.clientWidth-8)left=p.x-box.left+ch.scrollLeft-tip.offsetWidth-12;
    tip.style.left=Math.max(4,left)+'px'}
  svg.addEventListener('pointermove',e=>{const m=svg.getScreenCTM();if(!m)return;
    const p=new DOMPoint(e.clientX,e.clientY).matrixTransform(m.inverse());
    if(p.x<f.px0||p.x>f.px1||p.y<f.py1-10||p.y>f.py0+10){hide();return}
    show(f.x0+(p.x-f.px0)/(f.px1-f.px0)*(f.x1-f.x0))});
  svg.addEventListener('pointerleave',hide);
  ch.addEventListener('focus',()=>show(cur??(f.x0+f.x1)/2));
  ch.addEventListener('blur',hide);
  ch.addEventListener('keydown',e=>{const step=(f.x1-f.x0)/100*(e.shiftKey?10:1);
    if(e.key==='ArrowRight'){show((cur??f.x0)+step);e.preventDefault()}
    else if(e.key==='ArrowLeft'){show((cur??f.x1)-step);e.preventDefault()}
    else if(e.key==='Escape'){hide()}});
});
})();
"""
