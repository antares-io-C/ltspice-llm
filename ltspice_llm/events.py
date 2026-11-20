"""
Koşum olayları
~~~~~~~~~~~~~~
LTspice koşumlarının durum geçişlerini süreç başına bir JSON Lines dosyasına yazar ve bu dosyalardan canlı bir görünüm
kurar. Tarayıcıdaki pencereyi `ltspice_llm.monitor` açar (``python -m ltspice_llm.monitor``).

Olay yazımı varsayılan olarak kapalıdır; ``LTSPICE_LLM_EVENTS`` ``1``, ``true``, ``yes`` ya da ``on`` (büyük/küçük harf
ve baştaki/sondaki boşluk önemsiz) olunca açılır. Dizin ``LTSPICE_LLM_EVENT_DIR``, yoksa sistem
geçici dizininde ``ltspice_llm_events``. Her süreç kendi dosyasına yazar (``<pid>-<başlangıç ms>.jsonl``): Windows'ta
ekleme kipi süreçler arasında atomik değildir, ortak bir dosyada iki sürecin satırları karışabilirdi. Yazılamayan olay
düşer (``logger.debug``); koşumun sonucu değişmez. Yeni dosya açılırken `RETENTION_DAYS` günden eski olay dosyaları
silinir; dizindeki başka adlı ``.jsonl`` dosyalarına dokunulmaz (dizin kullanıcının kendi verisinin durduğu yer olabilir).

Her satır bir olaydır: ``t`` (Unix zamanı, sn), ``kind``, ``pid``; koşum olaylarında ``run`` (koşumun kimliği). Metin
alanları (``argv`` öğeleri, hata ve uyarılar, istisna) 300 karakterde kesilir: ilk 299 karakter ve ``…``.

- ``process``: dosyanın ilk satırı; ``argv`` yalnız ilk öğeyi (betiğin ya da modülün yolu) taşır. Komut satırı
  argümanları yazılmaz: gizli bilgi taşıyabilirler ve dosya `RETENTION_DAYS` gün kalır.
- ``run_start``: ``run_netlist`` başladı; ``circuit``, ``workspace`` (çalışma dizini; aynı ad ve dizinle sonra koşan
  devre bu koşumun dosyalarının üzerine yazar), ``source`` (dosyadan koşulduysa ``.net``/``.asc`` dosyasının tam yolu,
  metinse null), ``timeout`` (sn), ``guard`` (çekirdek bütçesi açık mı), ``stall`` (takılma bekçisi, sn; kapalıysa null).
- ``cpu_request``: çekirdek bütçesinden kira isteniyor; ``want``. Bütçe doluysa sıradaki olay gecikir.
- ``cpu_lease``: kira alındı; ``threads``, ``waited`` (sn), ``budget`` (bütçedeki yuva sayısı).
- ``ltspice_start``: LTspice başladı; ``ltspice_pid``, ``netlist`` (LTspice'ın koştuğu dosya; ``.raw`` ve ``.log``
  onun yanında büyür).
- ``ltspice_exit``: LTspice bitti ya da durduruldu; ``outcome`` (``exited``, ``timeout``, ``stalled``), ``returncode``,
  ``seconds``, ``crash`` (çökme açıklaması ya da null).
- ``cpu_release``: kira bırakıldı; ``threads``.
- ``run_end``: sonuç; ``success``, ``elapsed``, ``threads``, ``n_errors``, ``errors`` (ilk 3), ``n_warnings``,
  ``warnings`` (ilk 3), ``n_meas`` (ayrı ölçüm adı sayısı), ``log``, ``raw``. ``run_netlist`` istisnayla çıktıysa
  ``success`` false ve ``exception``.

Bir koşumun olay sırası (bütçe açık, çözücü takılmadan): ``run_start``, ``cpu_request``, ``cpu_lease``,
``ltspice_start``, ``ltspice_exit``, ``cpu_release``, ``run_end``. Bütçe kapalıysa ``cpu_*`` olayları yoktur. Takılan
çözücü tek iş parçacığıyla yeniden denenince ``ltspice_exit`` (``stalled``) ve ``cpu_release``'ten sonra
``cpu_request``, ``cpu_lease``, ``ltspice_start``, ``ltspice_exit``, ``cpu_release`` tekrarlanır. Bütçe süresinde
boşalmadıysa ``cpu_request``'ten sonra doğrudan ``run_end`` gelir; LTspice başlatılamadıysa ``ltspice_start`` ve
``ltspice_exit`` yoktur.
"""

from __future__ import annotations

import itertools
import json
import logging
import math
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from .adapter import SimulationResult

logger = logging.getLogger(__name__)

#: Olay yazımını açan ortam değişkeni (1/true/yes/on)
ENV_EVENTS = "LTSPICE_LLM_EVENTS"
#: Olay dosyalarının dizini
ENV_EVENT_DIR = "LTSPICE_LLM_EVENT_DIR"
#: Yeni olay dosyası açılırken bu kadar gündür değişmeyen olay dosyaları silinir
RETENTION_DAYS = 7.0
#: Olay dosyasının adı (`_stem`); saklama süresi yalnızca bu adlara uygulanır
_EVENT_FILE = re.compile(r"\d+-\d+\.jsonl")
#: Olaydaki yol bundan uzunsa yol değildir: Windows'un en uzun yolu 32767 UTF-16 birimi; daha uzununu CPython ``os.stat``
#: ``ValueError`` ile reddeder ("path too long for Windows")
_MAX_PATH_CHARS = 32000
#: Olay zamanının (Unix zamanı, sn) mutlak üst sınırı (~5138 yılı): iki uç zamanın farkı (süre) sonsuza taşmasın
_MAX_EVENT_TIME = 1e11
#: Kiranın (iş parçacığı sayısı) makul üst sınırı: LTspice iş parçacığını mantıksal çekirdek sayısına kırpar
#: (`cpu_guard.ltspice_thread_limit`); daha büyüğü bütçeye sayılmaz, toplamı da JSON'a yazılamayacak kadar büyümez
_MAX_LEASE = 4096

_ON = {"1", "true", "yes", "on"}
_MAX_TEXT = 300
_MAX_ITEMS = 3
_STARTED_MS = int(time.time() * 1000)
_LOCK = threading.Lock()
_LOCAL = threading.local()
_COUNTER = itertools.count(1)


def enabled() -> bool:
    """Olay yazımı açık mı (``LTSPICE_LLM_EVENTS``)."""
    return os.environ.get(ENV_EVENTS, "").strip().lower() in _ON


def event_dir() -> Path:
    """Olay dosyalarının dizini: ``LTSPICE_LLM_EVENT_DIR``, yoksa sistem geçici dizininde ``ltspice_llm_events``."""
    return Path(os.environ.get(ENV_EVENT_DIR) or Path(tempfile.gettempdir()) / "ltspice_llm_events")


def _stem() -> str:
    return f"{os.getpid()}-{_STARTED_MS}"


def _clip(text: Any) -> str:
    text = str(text)
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 1] + "…"


def _line(record: Dict[str, Any]) -> str:
    """
    Olay satırı. Sonlu olmayan sayı null yazılır: ``Infinity`` geçerli JSON değildir ve okuyucu satırı atardı (ör.
    ``stall_timeout=float("inf")``). Metin ASCII kaçışlıdır: UTF-8'e kodlanamayan ad (Linux'ta ``surrogateescape``'li
    yol) satırı hiç yazdırmazdı.
    """
    safe = {key: None if isinstance(value, float) and not math.isfinite(value) else value
            for key, value in record.items()}
    return json.dumps(safe, default=str) + "\n"


def _prune(directory: Path, now: float) -> None:
    cutoff = now - RETENTION_DAYS * 86400.0
    for old in directory.glob("*.jsonl"):
        if not _EVENT_FILE.fullmatch(old.name):  # kullanıcının seçtiği dizindeki başka .jsonl dosyalarına dokunulmaz
            continue
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass


def emit(kind: str, run: Optional[str] = None, **fields: Any) -> None:
    """
    Olayı bu sürecin olay dosyasına ekler. Olay yazımı kapalıysa hiçbir şey yapmaz; yazılamayan olay düşer ve istisna
    çağırana geçmez. ``run`` verilmezse iş parçacığının içinde bulunduğu koşumun kimliği (`RunScope`) yazılır.
    """
    if not enabled():
        return
    now = time.time()
    record: Dict[str, Any] = {"t": round(now, 3), "kind": kind, "pid": os.getpid()}
    run = run if run is not None else getattr(_LOCAL, "run", None)
    if run is not None:
        record["run"] = run
    record.update(fields)
    try:
        directory = event_dir()
        path = directory / f"{_stem()}.jsonl"
        with _LOCK:
            text = _line(record)
            if not path.exists():
                directory.mkdir(parents=True, exist_ok=True)
                _prune(directory, now)
                # yalnız betiğin yolu: komut satırı argümanları gizli bilgi (anahtar, parola) taşıyabilir
                header = {"t": record["t"], "kind": "process", "pid": record["pid"],
                          "argv": [_clip(a) for a in sys.argv[:1]]}
                text = _line(header) + text
            with open(path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
    except Exception as ex:  # olaylar gözlem içindir: yazılamaması koşumu asla bozmamalı
        logger.debug("Koşum olayı yazılamadı (%s): %s", kind, ex)


def result_fields(result: "SimulationResult") -> Dict[str, Any]:
    """``run_end`` olayının sonuç alanları."""
    errors, warnings = [str(e) for e in result.errors], [str(w) for w in result.warnings]
    return {
        "success": bool(result.success),
        "elapsed": round(result.elapsed_seconds, 3),
        "threads": result.solver_threads,
        "n_errors": len(errors),
        "errors": [_clip(e) for e in errors[:_MAX_ITEMS]],
        "n_warnings": len(warnings),
        "warnings": [_clip(w) for w in warnings[:_MAX_ITEMS]],
        "n_meas": len(set(result.measurements) | set(result.stepped_measurements)),
        "log": str(result.log_path) if result.log_path else None,
        "raw": str(result.raw_path) if result.raw_path else None,
    }


class RunScope:
    """
    Bir ``run_netlist`` çağrısını koşum olarak işaretler: girişte ``run_start``, çıkışta ``run_end`` yazar ve iş
    parçacığının koşum kimliğini ayarlar; arada `emit` ile yazılan olaylar bu kimliği taşır. Çıkışa kadar ``result``
    atanmalıdır; istisnayla çıkılırsa ``run_end`` istisnayı yazar ve istisna yayılmaya devam eder.
    """

    def __init__(self, **fields: Any) -> None:
        with _LOCK:
            self.run = f"{_stem()}-{next(_COUNTER)}"
        self.fields = fields
        self.result: Optional["SimulationResult"] = None
        self._previous: Optional[str] = None

    def __enter__(self) -> "RunScope":
        self._previous = getattr(_LOCAL, "run", None)
        _LOCAL.run = self.run
        emit("run_start", run=self.run, **self.fields)
        return self

    def __exit__(self, exc_type: Any, exc: Optional[BaseException], tb: Any) -> None:
        _LOCAL.run = self._previous
        if exc is not None:
            emit("run_end", run=self.run, success=False, exception=_clip(f"{type(exc).__name__}: {exc}"))
        elif self.result is not None:
            emit("run_end", run=self.run, **result_fields(self.result))


# --------------------------------------------------------------------------- okuma
class EventReader:
    """
    Olay dizinini artımlı okur: dosya başına okunan bayt konumu tutulur, satır sonu gelmemiş yarım satır sonraki okumaya
    kalır. İlk görüldüğünde ``max_age`` saniyedir değişmemiş dosya atlanır; yeniden yazılırsa baştan okunur. Olay dosyası
    dış girdidir: JSON nesnesi olmayan, ``kind``'ı metin olmayan ya da ``NaN``/``Infinity``/taşan sayı içeren satır atlanır.
    ``NaN``/``Infinity`` RFC 8259 §6'da sayı değildir ve tarayıcının JSON.parse'ı reddeder; ``1e400`` geçerli JSON'dur ama
    Python'da sonsuz olur ve sayfaya ``Infinity`` diye geri yazılırdı.
    """

    #: Bir okumada dosya başına en çok bu kadar bayt okunur, kalanı sonraki okumaya kalır
    MAX_CHUNK = 4 * 1024 * 1024

    def __init__(self, directory: Path, max_age: Optional[float] = 24 * 3600.0) -> None:
        self.directory = Path(directory)
        self.max_age = max_age
        self._offsets: Dict[str, int] = {}

    def read_new(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Son okumadan beri eklenen olaylar: her dosyanınkiler dosyadaki sırasıyla; ``src`` olayın dosyasının adıdır."""
        now = time.time() if now is None else now
        try:
            paths = sorted(self.directory.glob("*.jsonl"))
        except OSError:
            return []
        present = {p.name for p in paths}
        for gone in [name for name in self._offsets if name not in present]:
            del self._offsets[gone]
        events: List[Dict[str, Any]] = []
        for path in paths:
            try:
                info = path.stat()
            except OSError:
                continue
            offset = self._offsets.get(path.name)
            if offset is None:
                if self.max_age is not None and info.st_mtime < now - self.max_age:
                    continue
                offset = 0
            if info.st_size < offset:  # dosya silinip yeniden yazıldı
                offset = 0
            if info.st_size > offset:
                try:
                    with open(path, "rb") as handle:
                        handle.seek(offset)
                        chunk = handle.read(self.MAX_CHUNK)
                except OSError:
                    continue
                end = chunk.rfind(b"\n")
                if end >= 0:
                    offset += end + 1
                    events.extend(self._parse(chunk[:end], path.stem))
            self._offsets[path.name] = offset
        return events

    @staticmethod
    def _parse(block: bytes, src: str) -> List[Dict[str, Any]]:
        events = []
        for raw in block.split(b"\n"):
            try:
                event = json.loads(raw, parse_constant=_reject_constant, parse_float=_finite_float)
            except ValueError:
                continue
            if isinstance(event, dict) and isinstance(event.get("kind"), str):
                event["src"] = src
                events.append(event)
        return events


def _reject_constant(name: str) -> Any:
    """``NaN``/``Infinity`` JSON'da sayı değildir (RFC 8259 §6); tarayıcının JSON.parse'ı da reddeder: satır atlanır."""
    raise ValueError(f"JSON sayısı değil: {name}")


def _finite_float(text: str) -> float:
    """``1e400`` gibi taşan sayı Python'da sonsuz olur ve sayfaya ``Infinity`` diye giderdi: satır atlanır."""
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"sonlu olmayan sayı: {text}")
    return value


def _path_field(value: Any) -> Optional[str]:
    """
    Olaydaki dosya yolu (``source``, ``netlist``, ``log``) ya da None. Olay dosyası dış girdidir: metin olmayan, NUL içeren
    ya da aşırı uzun değeri işletim sistemi ``ValueError`` ile reddeder ve değer durumda kaldıkça sayfa her istekte
    düşerdi; metin olmayan ``source`` sayfanın betiğini (``schemaLink``) durdururdu.
    """
    if not isinstance(value, str) or "\0" in value:
        return None
    if len(value.encode("utf-16-le", "surrogatepass")) // 2 > _MAX_PATH_CHARS:  # Windows sınırı UTF-16 birimiyle
        return None
    try:
        os.fsencode(value)  # Linux'ta UTF-8'e kodlanamayan vekil: os.stat UnicodeEncodeError verirdi
    except UnicodeError:
        return None
    return value


def page_safe(text: str) -> bool:
    """
    Sayfa bu metni bağlantıya ya da isteğe koyabilir mi? Sayfa yolu ``encodeURIComponent`` ile kodlar; o da eşleşmemiş
    vekilde (Linux'ta UTF-8 olmayan dosya adı, ``surrogateescape``) URIError atar (ECMA-262 "Encode"). Sunucu ``?path=``
    değerini UTF-8 çözdüğü için böyle bir yol zaten istenemez.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _source_field(value: Any) -> Optional[str]:
    """``source``: `_path_field`'dan geçen ve sayfanın bağlantıya koyabildiği (`page_safe`) yol, değilse None."""
    path = _path_field(value)
    if path is None or not page_safe(path):
        return None
    return path


_THREADS_LINE = re.compile(r"^Maximum thread count: (\d+)\s*$", re.MULTILINE)
_METHOD_LINE = re.compile(r"^method = (\S.*?)\s*$", re.MULTILINE)
_ELAPSED_LINE = re.compile(r"^Total elapsed time: ([0-9.]+) seconds\.\s*$", re.MULTILINE)


def log_facts(text: str) -> Dict[str, Any]:
    """
    LTspice logunun güvenilirlik satırları: ``Maximum thread count`` (``threads``), ``method`` ve
    ``Total elapsed time`` (``solver_seconds``). Satırlar tam eşleşir: ``...: 12`` satırı 1 diye okunmaz.
    """
    facts: Dict[str, Any] = {}
    threads, method, elapsed = _THREADS_LINE.search(text), _METHOD_LINE.search(text), _ELAPSED_LINE.findall(text)
    if threads:
        facts["threads"] = int(threads.group(1))
    if method:
        facts["method"] = method.group(1)
    if elapsed:
        facts["solver_seconds"] = float(elapsed[-1])
    return facts


def process_label(argv: List[str]) -> str:
    """Olayı yazan sürecin kısa adı: ``pytest``, betiğin dosya adı, ``python -m <paket>`` ya da ``python -c``."""
    first = argv[0] if argv else ""
    parts = [p for p in re.split(r"[\\/]", first) if p]  # Linux'ta da Windows yolu bölünür
    if not parts:
        return "python"
    name, parent = parts[-1], parts[-2] if len(parts) > 1 else ""
    if name.lower().startswith(("pytest", "py.test")) or (name == "__main__.py" and parent.lower() == "pytest"):
        return "pytest"
    if name == "-c":
        return "python -c"
    if name == "__main__.py" and parent:
        return f"python -m {parent}"
    return name


#: Koşumun canlı aşamaları (`MonitorState.stage`)
STAGES = ("preparing", "waiting_cpu", "running", "stalled", "collecting")


class MonitorState:
    """
    Olaylardan koşumların durumunu kurar. G/Ç yapmaz: süreç ve dosya bilgisi `snapshot`'a, logun okunması
    ``read_log``'a (yol, koşumun bitiş zamanı -> `log_facts` sözlüğü) fonksiyon olarak verilir. Türü bozuk olay yalnız
    kendisi düşer: zamanı sonlu bir sayı olmayan olay atlanır, liste olmayan ``argv`` boş sayılır, kullanılamayan yol
    alanı (`_path_field`) yok sayılır.
    """

    def __init__(self, read_log: Optional[Callable[[str, float], Dict[str, Any]]] = None, keep: int = 200) -> None:
        self.runs: Dict[str, Dict[str, Any]] = {}
        self.processes: Dict[str, Dict[str, Any]] = {}
        self.events_seen = 0
        self.budget: Optional[int] = None
        self._read_log = read_log
        self._keep = keep
        self._seq = 0
        self._end_seq = 0

    def ingest(self, event: Dict[str, Any]) -> None:
        self.events_seen += 1
        kind, src = event.get("kind"), str(event.get("src", ""))
        try:
            t = float(event.get("t") or 0.0)
        except (TypeError, ValueError, OverflowError):  # zamanı sayı olmayan satır (başka bir yazar): yalnız o düşer
            return
        if not math.isfinite(t):  # "nan" metni float'a sonlu olmayan sayı olarak çevrilir
            return
        if abs(t) > _MAX_EVENT_TIME:  # sonlu ama uç zamanların farkı (süre) taşar: 1e308 - (-1e308)
            return
        if kind == "process":
            argv = event.get("argv")
            argv = [str(a) for a in argv] if isinstance(argv, list) else []
            self.processes[src] = {"pid": event.get("pid"), "first": t, "label": process_label(argv)}
            return
        run_id = event.get("run")
        if not isinstance(run_id, str):
            return
        if kind == "run_start":
            self._seq += 1
            self.runs[run_id] = {
                "seq": self._seq,  # okuma sırası: aynı milisaniyede başlayan iki koşumu da sıralar
                "run": run_id, "src": src, "pid": event.get("pid"), "circuit": str(event.get("circuit", "")),
                "workspace": event.get("workspace"), "source": _source_field(event.get("source")), "started": t,
                "timeout": event.get("timeout"), "guard": bool(event.get("guard")),
                "stall": event.get("stall"), "status": "active", "requested": False, "lease": 0, "threads": None,
                "waited": None, "attempts": 0, "exits": [], "ltspice_pid": None, "netlist": None, "ended": None,
                "end_seq": 0,  # bitişin okuma sırası: aynı saat adımında biten iki koşumu sıralar
            }
            return
        run = self.runs.get(run_id)
        if run is None:  # başlangıcı okuma penceresinden önce
            return
        if kind == "cpu_request":
            run["requested"] = True
        elif kind == "cpu_lease":
            threads = event.get("threads")
            run.update(requested=False, lease=threads if isinstance(threads, int) and 0 <= threads <= _MAX_LEASE else 0,
                       threads=threads, waited=event.get("waited"))
            if isinstance(event.get("budget"), int):
                self.budget = event["budget"]
        elif kind == "cpu_release":
            run["lease"] = 0
        elif kind == "ltspice_start":
            run["attempts"] += 1
            run.update(ltspice_pid=event.get("ltspice_pid"), netlist=_path_field(event.get("netlist")))
        elif kind == "ltspice_exit":
            run["exits"].append({k: event.get(k) for k in ("outcome", "returncode", "seconds", "crash")})
        elif kind == "run_end":
            self._end_seq += 1
            run.update(status="ok" if event.get("success") else "failed", ended=t, end_seq=self._end_seq)
            for key in ("elapsed", "threads", "n_errors", "errors", "n_warnings", "warnings", "n_meas", "log",
                        "exception"):
                if key in event:
                    run[key] = _path_field(event[key]) if key == "log" else event[key]
            if isinstance(run.get("log"), str) and self._read_log is not None:
                run["log_facts"] = self._read_log(run["log"], t)
            self._trim()

    def _trim(self) -> None:
        finished = sorted((r for r in self.runs.values() if r["status"] != "active"),
                          key=lambda r: (r["ended"], r["end_seq"]))
        for run in finished[: max(0, len(finished) - self._keep)]:
            del self.runs[run["run"]]

    @staticmethod
    def stage(run: Dict[str, Any]) -> str:
        """Bitmemiş koşumun aşaması (`STAGES`)."""
        exits = run["exits"]
        if run["attempts"] > len(exits):
            return "running"
        if exits:
            return "stalled" if exits[-1]["outcome"] == "stalled" else "collecting"
        return "waiting_cpu" if run["requested"] else "preparing"

    def _label(self, run: Dict[str, Any]) -> str:
        return str(self.processes.get(run["src"], {}).get("label", "?"))

    def snapshot(
        self,
        now: float,
        alive: Callable[[int, float], bool],
        stat: Callable[[str], Optional[Tuple[int, float]]],
    ) -> Dict[str, Any]:
        """
        Sayfanın gösterdiği durum. ``alive(pid, ilk_olay_zamanı)`` süreci yaşamayan bitmemiş koşumu ``abandoned``
        yapar; ``stat(yol)`` (boyut, değişme zamanı) ya da None döner ve çalışan koşumun ``.raw``/``.log`` büyümesini
        gösterir.
        """
        verdicts: Dict[str, bool] = {}
        for run in self.runs.values():
            if run["status"] != "active" or not isinstance(run["pid"], int):
                continue
            src = run["src"]
            if src not in verdicts:
                verdicts[src] = alive(run["pid"], self.processes.get(src, {}).get("first", run["started"]))
            if not verdicts[src]:
                run.update(status="abandoned", ended=now)
        active = sorted((r for r in self.runs.values() if r["status"] == "active"), key=lambda r: r["started"])
        # eşit bitişte bitişin okuma sırası: Windows'ta Python 3.13 öncesi saat 15,6 ms adımla ilerler, iki koşum aynı t'yi alır
        finished = sorted((r for r in self.runs.values() if r["status"] != "active"),
                          key=lambda r: (r["ended"], r["end_seq"]), reverse=True)
        counts = {status: sum(r["status"] == status for r in finished) for status in ("ok", "failed", "abandoned")}
        return {
            "now": now,
            "events_seen": self.events_seen,
            # bitmiş koşumun kirası sayılmaz: cpu_release olayı kaybolsa da (yazılamadı) bütçe boşalır
            "budget": {"in_use": sum(r["lease"] for r in active), "size": self.budget},
            "active": [self._active_view(r, now, stat) for r in active],
            "finished": [self._finished_view(r) for r in finished],
            "counts": counts,
        }

    def _active_view(
        self, run: Dict[str, Any], now: float, stat: Callable[[str], Optional[Tuple[int, float]]]
    ) -> Dict[str, Any]:
        stage = self.stage(run)
        raw = log = None
        if isinstance(run["netlist"], str):
            netlist = Path(run["netlist"])
            if netlist.name:  # adı boş yoldan ("/", "C:/") .raw/.log türetilemez: PurePath.with_suffix ValueError verir
                raw, log = stat(str(netlist.with_suffix(".raw"))), stat(str(netlist.with_suffix(".log")))
        changed = max((s[1] for s in (raw, log) if s is not None), default=None)
        return {
            "run": run["run"], "circuit": run["circuit"], "process": self._label(run), "pid": run["pid"],
            "stage": stage, "elapsed": round(now - run["started"], 1), "timeout": run["timeout"],
            "guard": run["guard"], "stall": run["stall"], "threads": run["threads"], "waited": run["waited"],
            "attempts": run["attempts"], "ltspice_pid": run["ltspice_pid"], "source": run["source"],
            "raw_bytes": raw[0] if raw else None, "log_bytes": log[0] if log else None,
            # .raw/.log'un son değişiminden beri geçen süre: takılma bekçisinin baktığı sinyal
            "since_change": round(max(0.0, now - changed), 1) if stage == "running" and changed is not None else None,
        }

    def _finished_view(self, run: Dict[str, Any]) -> Dict[str, Any]:
        elapsed = run.get("elapsed")
        return {
            "run": run["run"], "circuit": run["circuit"], "process": self._label(run), "status": run["status"],
            "started": run["started"], "ended": run["ended"],
            "elapsed": elapsed if elapsed is not None else round(run["ended"] - run["started"], 1),
            "threads": run["threads"], "outcomes": [e["outcome"] for e in run["exits"]],
            "n_errors": run.get("n_errors", 0), "errors": run.get("errors", []),
            "n_warnings": run.get("n_warnings", 0), "warnings": run.get("warnings", []),
            "n_meas": run.get("n_meas"), "exception": run.get("exception"), "source": run["source"],
            "log": isinstance(run.get("log"), str), "log_facts": run.get("log_facts", {}),
        }
