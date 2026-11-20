"""
Canlı LTspice monitörü
~~~~~~~~~~~~~~~~~~~~~~
`ltspice_llm.events` olaylarını ve LTspice şemalarını tarayıcıda canlı gösteren yerel sunucu::

    python -m ltspice_llm.monitor                         # koşumlar: http://127.0.0.1:8765/
    python -m ltspice_llm.monitor --asc C:/proje --open   # şemalar: http://127.0.0.1:8765/sema

Şema sayfası ``--asc`` ile verilen dosya ya da klasörlerdeki (alt klasörler dahil; verilmezse çalışma klasörü ve LTspice'ın
çalışma klasörü), LTspice'ta son açılan (`reference.ltspice_recent_files`) ve koşum olaylarında geçen ``.asc`` dosyalarını
`asc_view` ile çizer. Dosya kaydedildikçe (LTspice'ta Ctrl+S ya da
`AscSchematic.save`) bir saniye içinde yenilenir; "en son kaydedileni göster" açıkken en son değişen şemaya geçer.
Sayfadaki yol kutusundan klasör ya da ``.asc`` eklenir ve × ile bırakılır; eklenenler `config_path` dosyasına yazılır,
monitör yeniden açılınca da izlenir. LTspice dosyayı açınca son açılanlar listesini diske yazmıyor (ölçüldü,
2026-11-19): başka bir klasördeki şemanın hemen görünmesi için klasör eklenmeli.

Sayfa VS Code'un Simple Browser'ında ("Simple Browser: Show"), Edge'de ya da Claude masaüstü uygulamasının tarayıcı
bölmesinde açılır. claude.ai'de yayımlanan bir Artifact bu bilgiyi canlı alamaz: başka bir kökenden ``127.0.0.1``'e
yapılan isteği tarayıcının yerel ağ koruması keser, sayfanın makinedeki dosyalara da yolu yoktur.

Koşumu yapan süreçte ``LTSPICE_LLM_EVENTS=1`` olmalı (bkz. `ltspice_llm.events`). Sunucu yalnızca ``127.0.0.1``'i
dinler, ``Host`` başlığı yerel adres olmayan isteği reddeder (DNS rebinding) ve yalnızca olaylarda geçen ``.log``
dosyalarını okur. Sayfa veriyi ``textContent`` ile yazar: hata metni LLM'den gelen netlist parçası içerebilir.
"""

from __future__ import annotations

import argparse
import json
import os
import socketserver
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union, cast
from urllib.parse import parse_qs, urlsplit

from .asc_view import render_asc
from .events import ENV_EVENTS, EventReader, MonitorState, enabled, event_dir, log_facts, page_safe
from .monitor_page import PAGE_HTML, PAGE_JS, SCHEMA_HTML, SCHEMA_JS
from .parser import LogParser
from .reference import ltspice_recent_files, ltspice_working_dir

DEFAULT_PORT = 8765
#: Log sayfada en çok bu kadar karakterle (sonu) gösterilir
MAX_LOG_CHARS = 400_000
#: Logun değişme zamanı koşumun bitişinden bu kadar saniye sonraysa log o koşumun değildir
LOG_SLACK = 2.0
CSP = ("default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; "
       "base-uri 'none'; form-action 'none'")

#: Şema taramasında atlanan klasörler
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".ruff_cache", ".pytest_cache",
             ".hypothesis", ".tox"}
#: Bir kökün taramasında en çok bu kadar dizin girdisine bakılır: çok büyük bir klasör sayfayı yavaşlatmasın
MAX_SCAN_ENTRIES = 20000
#: Şema listesinde en çok bu kadar dosya (en son değişenler)
MAX_LISTED = 500
#: Sayfadan gelen POST gövdesinin üst sınırı (bayt)
MAX_POST_BYTES = 64 * 1024
#: Reddedilecek isteğin gövdesi de bu boyuta kadar okunur (bkz. `_Handler.do_POST`)
DRAIN_BYTES = 1024 * 1024
#: Durum değiştiren isteğin taşıması gereken başlık. Başka bir sitenin formu bu başlığı koyamaz; betiği koyarsa
#: tarayıcı önce izin sorar (CORS ön isteği) ve sunucu izin vermez.
POST_HEADER = "X-LTspice-Monitor"


def config_path() -> Path:
    """
    Monitörün kalıcı ayarı (sayfadan eklenen klasörler): ``%APPDATA%\\ltspice_llm\\monitor.json``; APPDATA yoksa
    ``~/.config/ltspice_llm/monitor.json``.
    """
    base = os.environ.get("APPDATA")
    return (Path(base) if base else Path.home() / ".config") / "ltspice_llm" / "monitor.json"

_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_FILETIME_EPOCH = 11644473600.0  # 1601-01-01 ile 1970-01-01 arası saniye


def _windows_pid_alive(pid: int, started_before: float) -> bool:
    if sys.platform != "win32":  # mypy platformu bu denetimden anlar
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED  # süreç var ama sorgulanamıyor
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != _STILL_ACTIVE:
            return False
        times = [wintypes.FILETIME() for _ in range(4)]
        if kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
            created = ((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime) / 1e7 - _FILETIME_EPOCH
            if created > started_before + 1.0:  # olayları yazan süreç ölmüş, PID başka bir sürece geçmiş
                return False
        return True
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int, started_before: float) -> bool:
    """
    Süreç yaşıyor mu? Windows'ta ``started_before``'dan (sürecin olay dosyasındaki ilk satır) sonra başlamış süreç PID'i
    yeniden kullanmıştır, ölü sayılır. ``os.kill(pid, 0)`` yalnızca Windows dışında: Windows'ta süreci sonlandırır.
    """
    # POSIX kill(2): 0 ve negatif değerler süreç grubunu gösterir, tek bir süreci değil. Windows'ta PID bir DWORD'dür ve
    # ctypes fazlasını maskeler: 2**32 + p, p sürecini gösterirdi
    if not 0 < pid <= 0xFFFFFFFF:
        return False
    if sys.platform == "win32":
        return _windows_pid_alive(pid, started_before)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):  # OverflowError: pid_t 32 bit, olay dosyasında 2**31 ve üstü
        return False
    return True


def _file_stat(path: str) -> Optional[Tuple[int, float]]:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_size, info.st_mtime


def _current_log(path: str, ended: float) -> Tuple[Optional[str], str]:
    """(metin, "") ya da (None, neden): log yoksa ya da koşum bittikten sonra değiştiyse metin dönmez."""
    if not path.lower().endswith(".log"):
        return None, "Yalnızca .log dosyaları gösterilir."
    try:
        if os.stat(path).st_mtime > ended + LOG_SLACK:
            return None, "Log bu koşum bittikten sonra değişti: aynı adla sonra koşan bir devre üzerine yazmış."
        return LogParser.read_text(path), ""
    except OSError:
        return None, "Log dosyası artık yok: çalışma alanı temizlenmiş ya da aynı adla yeni bir koşum başlamış."


def read_log_facts(path: str, ended: float) -> Dict[str, Any]:
    """Biten koşumun logundan `log_facts`; log yoksa ya da sonradan değiştiyse ``{"stale": True}``."""
    text, _ = _current_log(path, ended)
    return log_facts(text) if text is not None else {"stale": True}


def _key(path: Union[str, Path]) -> str:
    return os.path.normcase(os.path.realpath(path))


def _file_entry(path: str, mtime: float, mtime_ns: int, size: int, root: Optional[Path]) -> Dict[str, Any]:
    """Şema listesindeki satır; ``folder`` kökün adıyla başlayan göreli klasör (kök yoksa tam klasör)."""
    folder = str(Path(path).parent)
    if root is not None and root.is_dir():
        try:
            relative = os.path.relpath(Path(path).parent, root)
            folder = root.name if relative == "." else str(Path(root.name) / relative)
        except ValueError:  # farklı sürücüler
            pass
    return {"path": str(Path(path)), "name": Path(path).name, "folder": folder, "mtime": mtime,
            "stamp": f"{mtime_ns}-{size}"}


def scan_schematics(root: Path, limit: Optional[int] = None) -> Tuple[List[Tuple[str, float, int, int]], bool]:
    """
    Kökteki ``.asc`` dosyaları: (yol, değişme zamanı, mtime_ns, boyut) ve tarama ``limit``'te (verilmezse çağrı anındaki
    `MAX_SCAN_ENTRIES`) kesildi mi. Kök bir dosyaysa yalnız o. `SKIP_DIRS` ve adaptörün koşum sırasındaki geçici
    kopyaları (``_ltspice_llm_*``) atlanır.
    """
    limit = MAX_SCAN_ENTRIES if limit is None else limit
    if root.is_file():
        info = root.stat()
        return ([(str(root), info.st_mtime, info.st_mtime_ns, info.st_size)]
                if root.suffix.lower() == ".asc" else []), False
    found: List[Tuple[str, float, int, int]] = []
    seen, stack = 0, [root]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    seen += 1
                    if seen > limit:
                        return found, True
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name not in SKIP_DIRS:
                                stack.append(Path(entry.path))
                        elif entry.name.lower().endswith(".asc") and not entry.name.startswith("_ltspice_llm_"):
                            info = entry.stat()
                            found.append((entry.path, info.st_mtime, info.st_mtime_ns, info.st_size))
                    except OSError:
                        continue
        except OSError:
            continue
    return found, False


class Monitor:
    """
    Olay dizinini okur, koşumların durumunu tutar ve ``asc_roots``'taki (dosya ya da klasör) şemaları çizer. HTTP
    işleyicileri aynı nesneyi kilitle paylaşır. Yalnız bu köklerdeki, koşum olaylarında (``source``) geçen ve
    ``ltspice_recent`` açıksa LTspice'ın son açılanlar listesindeki (`reference.ltspice_recent_files`) ``.asc``
    dosyaları çizilir.
    """

    def __init__(self, directory: Path, max_age: Optional[float] = 24 * 3600.0,
                 asc_roots: Sequence[Union[str, Path]] = (), ltspice_recent: bool = False,
                 saved_roots: Optional[Path] = None) -> None:
        """``saved_roots``: sayfadan eklenen kökleri saklayan JSON dosyası (bkz. `config_path`); None ise saklanmaz."""
        self.reader = EventReader(directory, max_age=max_age)
        self.state = MonitorState(read_log=read_log_facts)
        self.saved_roots = saved_roots
        self._saved = [str(path) for path in self._read_saved()]
        resolved = [Path(root).resolve() for root in [*asc_roots, *self._saved]]
        self.asc_roots = [root for i, root in enumerate(resolved) if _key(root) not in {_key(r) for r in resolved[:i]}]
        self.ltspice_recent = ltspice_recent
        self._lock = threading.Lock()
        self._roots_lock = threading.Lock()
        self._render_lock = threading.Lock()
        self._renders: Dict[str, Tuple[str, Dict[str, Any]]] = {}

    def _ingest_new(self) -> None:
        for event in self.reader.read_new():
            self.state.ingest(event)

    # ----------------------------------------------------------------------- sayfadan eklenen kökler
    def _read_saved(self) -> List[Path]:
        """Saklanan kökler: hâlâ var olanlar, çözülmüş yazımla."""
        if self.saved_roots is None:
            return []
        try:
            data = json.loads(self.saved_roots.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        roots = data.get("roots") if isinstance(data, dict) else None
        return [Path(p).resolve() for p in roots or [] if isinstance(p, str) and Path(p).exists()]

    def _write_saved(self) -> Optional[str]:
        """Saklanan kökleri yazar; yazılamazsa nedeni (kök bu oturum için yine izlenir)."""
        if self.saved_roots is None:
            return None
        try:
            self.saved_roots.parent.mkdir(parents=True, exist_ok=True)
            partial = self.saved_roots.with_name(self.saved_roots.name + ".tmp")
            partial.write_text(json.dumps({"roots": self._saved}, ensure_ascii=False, indent=1), encoding="utf-8")
            partial.replace(self.saved_roots)
        except (OSError, UnicodeError) as ex:  # UnicodeError: yolda eşleşmemiş vekil karakter
            return f"Klasör bu oturumda izleniyor ama kalıcı olarak kaydedilemedi ({self.saved_roots}: {ex})."
        return None

    def _roots_body(self, **extra: Any) -> Dict[str, Any]:
        return {"roots": [str(root) for root in self.asc_roots], **extra}

    def add_root(self, raw: str) -> Tuple[int, Dict[str, Any]]:
        """
        Sayfadan klasör (ya da tek ``.asc``) ekler: (HTTP durumu, gövde). Explorer'ın "Yol olarak kopyala"sının tırnakları
        ve ``%VAR%``/``~`` açılır. Eklenen kök `saved_roots`'a yazılır, monitör yeniden açılınca da izlenir.
        """
        text = raw.strip().strip('"').strip()
        if not text:
            return 400, {"error": "Klasör yolu boş."}
        path = Path(os.path.expandvars(os.path.expanduser(text)))
        if not (path.is_dir() or (path.is_file() and path.suffix.lower() == ".asc")):
            return 404, {"error": f"Böyle bir klasör ya da .asc dosyası yok: {text}"}
        path = path.resolve()
        with self._roots_lock:
            if _key(path) in {_key(root) for root in self.asc_roots}:
                return 409, self._roots_body(error=f"Zaten izleniyor: {path}")
            self.asc_roots = [*self.asc_roots, path]  # yeni liste: listeyi gezen istekler eskisini görür
            self._saved = [*self._saved, str(path)]
            problem = self._write_saved()
        return 200, self._roots_body(added=str(path), **({"warning": problem} if problem else {}))

    def remove_root(self, raw: str) -> Tuple[int, Dict[str, Any]]:
        """İzlenen kökü bırakır; sayfadan eklenmişse kalıcı listeden de siler."""
        text = raw.strip().strip('"').strip()
        if not text:  # boş yol çalışma klasörüne çözülür; o kök yanlışlıkla bırakılmasın
            return 400, {"error": "Klasör yolu boş."}
        key = _key(text)
        with self._roots_lock:
            remaining = [root for root in self.asc_roots if _key(root) != key]
            if len(remaining) == len(self.asc_roots):
                return 404, self._roots_body(error=f"Bu yol izlenmiyor: {raw}")
            self.asc_roots = remaining
            problem = None
            if any(_key(saved) == key for saved in self._saved):
                self._saved = [saved for saved in self._saved if _key(saved) != key]
                problem = self._write_saved()
        return 200, self._roots_body(**({"warning": problem} if problem else {}))

    def state_payload(self) -> Dict[str, Any]:
        """Yeni olayları okur ve sayfanın gösterdiği durumu döner."""
        with self._lock:
            self._ingest_new()
            payload = self.state.snapshot(time.time(), pid_alive, _file_stat)
        payload["event_dir"] = str(self.reader.directory)
        return payload

    def _event_schematics(self) -> List[str]:
        """Koşum olaylarında kaynak olarak geçen ``.asc`` dosyaları (kilit altında çağrılır)."""
        return [r["source"] for r in self.state.runs.values()
                if isinstance(r.get("source"), str) and r["source"].lower().endswith(".asc")]

    def _extra_schematics(self) -> List[str]:
        """Köklerin dışından gösterilebilen şemalar: koşum olaylarında geçenler ve (açıksa) LTspice'ın son açtıkları."""
        with self._lock:
            self._ingest_new()
            paths = [Path(source) for source in self._event_schematics()]
        if self.ltspice_recent:
            paths += [path for path in ltspice_recent_files() if path.suffix.lower() == ".asc"]
        # listedeki yazım cevaptakiyle aynı olmalı (`schematic_payload`): ``..``, kısa 8.3 ad ve sürücü harfi çözülür
        return [str(path.resolve()) for path in paths]

    def schematic_list(self) -> Dict[str, Any]:
        """İzlenen şemalar, en son değişen önce: ``path``, ``name``, ``folder``, ``mtime``, ``stamp`` (değişince değişir)."""
        sources = self._extra_schematics()
        files: Dict[str, Dict[str, Any]] = {}
        truncated = False
        for root in self.asc_roots:
            found, cut = scan_schematics(root)
            truncated = truncated or cut
            for path, mtime, mtime_ns, size in found:
                if page_safe(path):  # sayfanın açamayacağı yol (eşleşmemiş vekil) listelenmez: tıklanınca URIError
                    files.setdefault(_key(path), _file_entry(path, mtime, mtime_ns, size, root))
        for path in sources:
            if _key(path) in files or not page_safe(path):
                continue
            try:
                stat = os.stat(path)
            except OSError:
                continue
            files[_key(path)] = _file_entry(path, stat.st_mtime, stat.st_mtime_ns, stat.st_size, None)
        listed = sorted(files.values(), key=lambda f: f["mtime"], reverse=True)
        return {"now": time.time(), "roots": [str(root) for root in self.asc_roots], "files": listed[:MAX_LISTED],
                "truncated": truncated or len(listed) > MAX_LISTED}

    def _allowed(self, path: str) -> bool:
        key = _key(path)
        if not key.lower().endswith(".asc"):  # taramadaki gibi harfe duyarsız: Linux'ta normcase harfi değiştirmez
            return False
        for root in self.asc_roots:
            root_key = _key(root)
            try:
                if key == root_key or (root.is_dir() and os.path.commonpath([key, root_key]) == root_key):
                    return True
            except ValueError:  # farklı sürücüler
                continue
        return key in {_key(source) for source in self._extra_schematics()}

    def schematic_payload(self, path: str) -> Tuple[int, Dict[str, Any]]:
        """(HTTP durumu, gövde): şemanın SVG'si, görünüm kutusu, uyarılar ve sayımlar. Dosya değişmedikçe önbellekten."""
        if not path:
            return 400, {"error": "Şema yolu verilmedi."}
        if not self._allowed(path):
            return 403, {"error": "Bu dosya izlenen klasörlerde değil ya da .asc değil (monitörü --asc ile başlatın).",
                         "path": path}
        try:
            info = os.stat(path)
        except OSError:
            return 404, {"error": "Şema dosyası yok: silinmiş ya da taşınmış olabilir.", "path": path}
        stamp = f"{info.st_mtime_ns}-{info.st_size}"
        with self._render_lock:
            cached = self._renders.get(_key(path))
            if cached is not None and cached[0] == stamp:
                return 200, cached[1]
            try:
                rendered = render_asc(path)
            except (OSError, ValueError) as ex:
                return 500, {"error": f"Şema çizilemedi: {ex}", "path": path}
            canonical = Path(path).resolve()  # listedekiyle aynı yazım: sayfa yolları birebir karşılaştırır
            payload = {"path": str(canonical), "name": canonical.name, "folder": str(canonical.parent),
                       "stamp": stamp, "mtime": info.st_mtime, "svg": rendered.svg, "viewbox": list(rendered.viewbox),
                       "warnings": rendered.warnings, "counts": rendered.counts, "encoding": rendered.encoding}
            if len(self._renders) >= 32:
                self._renders.clear()
            self._renders[_key(path)] = (stamp, payload)
        return 200, payload

    def log_payload(self, run_id: str) -> Tuple[int, Dict[str, Any]]:
        """(HTTP durumu, gövde): biten koşumun logu. Aynı dosyalara sonra yazan koşum varsa log gösterilmez (409)."""
        with self._lock:
            run = self.state.runs.get(run_id)
            if run is None or not isinstance(run.get("log"), str) or run["ended"] is None:
                return 404, {"error": "Koşum bilinmiyor ya da logu yok."}
            newer = [r for r in self.state.runs.values() if (r["started"], r["seq"]) > (run["started"], run["seq"])
                     and r.get("workspace") == run.get("workspace") and r["circuit"] == run["circuit"]]
            path, ended = run["log"], run["ended"]
        if newer:
            return 409, {"error": "Aynı adla sonra koşan bir devre bu logun üzerine yazdı.", "path": path}
        text, problem = _current_log(path, ended)
        if text is None:
            return 409, {"error": problem, "path": path}
        return 200, {"run": run_id, "path": path, "text": text[-MAX_LOG_CHARS:]}


def _local_host(host: str, port: int) -> bool:
    return host.strip().lower() in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}


def _local_origin(origin: str, port: int) -> bool:
    """İsteği monitörün kendi sayfası mı gönderdi? Tarayıcı POST'a ``Origin`` koyar; başka bir sitenin formu ya da
    betiği kendi kökenini yazar."""
    return origin.strip().lower() in {f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}"}


class _Server(ThreadingHTTPServer):
    # Windows'ta SO_REUSEADDR ikinci bir sunucuyu da aynı porta bağlar; port doluysa hata verilsin
    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], monitor: Monitor) -> None:
        self.monitor = monitor
        super().__init__(address, _Handler)
        self.port = int(self.server_address[1])

    def server_bind(self) -> None:
        # HTTPServer.server_bind adresi socket.getfqdn ile ada çevirir: bazı ağlarda saniyeler sürer, burada gereksiz
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", int(self.server_address[1])


class _Handler(BaseHTTPRequestHandler):
    server_version = "ltspice-llm-monitor"

    def do_GET(self) -> None:
        server = cast(_Server, self.server)
        if not _local_host(self.headers.get("Host", ""), server.port):
            self._send(403, "text/plain; charset=utf-8", "Yalnızca yerel adresten erişilebilir.".encode("utf-8"))
            return
        parts = urlsplit(self.path)
        if parts.path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE_HTML.encode("utf-8"), page=True)
        elif parts.path == "/app.js":
            self._send(200, "text/javascript; charset=utf-8", PAGE_JS.encode("utf-8"))
        elif parts.path == "/api/state":
            self._json(200, server.monitor.state_payload())
        elif parts.path == "/api/log":
            self._json(*server.monitor.log_payload(parse_qs(parts.query).get("run", [""])[0]))
        elif parts.path == "/sema":
            self._send(200, "text/html; charset=utf-8", SCHEMA_HTML.encode("utf-8"), page=True)
        elif parts.path == "/sema.js":
            self._send(200, "text/javascript; charset=utf-8", SCHEMA_JS.encode("utf-8"))
        elif parts.path == "/api/schematics":
            self._json(200, server.monitor.schematic_list())
        elif parts.path == "/api/schematic":
            self._json(*server.monitor.schematic_payload(parse_qs(parts.query).get("path", [""])[0]))
        else:
            self._send(404, "text/plain; charset=utf-8", b"Not found")

    def do_POST(self) -> None:
        """Durum değiştiren tek istek türü: izlenen kök ekleme/bırakma. Yalnız monitörün kendi sayfasından gelir."""
        server = cast(_Server, self.server)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        # Gövde, istek reddedilecek olsa da önce okunur: Windows okunmamış veriyle kapanan bağlantıyı sıfırlar ve istemci
        # yanıt yerine bağlantı hatası alır (ölçüm 2026-11-19: reddedilen 300 isteğin 22'si). Çok büyüğü okunmaz.
        data = self.rfile.read(length) if 0 <= length <= DRAIN_BYTES else None
        if not (_local_host(self.headers.get("Host", ""), server.port)
                and _local_origin(self.headers.get("Origin", ""), server.port)
                and self.headers.get(POST_HEADER) == "1"):
            self._send(403, "text/plain; charset=utf-8", "Yalnızca monitörün kendi sayfasından.".encode("utf-8"))
            return
        if data is None or length > MAX_POST_BYTES:
            self._json(413 if length > MAX_POST_BYTES else 400, {"error": "İstek gövdesi geçersiz."})
            return
        try:
            body = json.loads(data or b"{}")
        except ValueError:
            body = None
        path = body.get("path") if isinstance(body, dict) else None
        if not isinstance(path, str):
            self._json(400, {"error": "Gövde {\"path\": \"...\"} olmalı."})
            return
        route = urlsplit(self.path).path
        if route == "/api/roots/add":
            self._json(*server.monitor.add_root(path))
        elif route == "/api/roots/remove":
            self._json(*server.monitor.remove_root(path))
        else:
            self._send(404, "text/plain; charset=utf-8", b"Not found")

    def _json(self, status: int, payload: Dict[str, Any]) -> None:
        # ASCII kaçışlı: eşleşmemiş vekil karakter (Linux'ta UTF-8 olmayan dosya adı, başka bir yazarın olayı) UTF-8'e
        # kodlanamaz ve yanıtı her istekte düşürürdü; tarayıcı kaçışı çözer
        body = json.dumps(payload, default=str).encode("ascii")
        self._send(status, "application/json; charset=utf-8", body)

    def _send(self, status: int, content_type: str, body: bytes, page: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if page:
            self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # istek başına satır basılmasın
        pass


def make_server(monitor: Monitor, port: int = DEFAULT_PORT) -> _Server:
    """``127.0.0.1:port``'u dinleyen sunucu (``port=0``: boş bir port); port doluysa `OSError`."""
    return _Server(("127.0.0.1", port), monitor)


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # boru/dosya ANSI kod sayfasıyla yazılır; Türkçe harf çökertmesin
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="backslashreplace")
    parser = argparse.ArgumentParser(prog="python -m ltspice_llm.monitor",
                                     description="LTspice koşumlarını tarayıcıda canlı izler.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"dinlenecek port (varsayılan {DEFAULT_PORT}; 0: boş bir port)")
    parser.add_argument("--dir", type=Path, default=None,
                        help="olay dizini (varsayılan: LTSPICE_LLM_EVENT_DIR ya da geçici dizindeki ltspice_llm_events)")
    parser.add_argument("--hours", type=float, default=24.0,
                        help="bu kadar saattir değişmeyen olay dosyaları okunmaz (varsayılan 24)")
    parser.add_argument("--asc", action="append", type=Path, default=None, metavar="YOL",
                        help="şeması gösterilecek .asc dosyası ya da klasör (alt klasörler dahil; birden çok verilebilir; "
                             "varsayılan: çalışma klasörü ve LTspice'ın çalışma klasörü). LTspice'ta son açılan "
                             "şemalar ve sayfadan eklenen klasörler (monitor.json) her zaman listelenir.")
    parser.add_argument("--open", action="store_true",
                        help="sayfayı varsayılan tarayıcıda da aç (--asc verildiyse şema sayfasını)")
    args = parser.parse_args(argv)
    directory = args.dir or event_dir()
    working = ltspice_working_dir()
    roots = args.asc or [Path.cwd(), *([working] if working is not None else [])]
    try:
        server = make_server(Monitor(directory, max_age=args.hours * 3600.0, asc_roots=roots, ltspice_recent=True,
                                     saved_roots=config_path()), args.port)
    except OSError as ex:
        print(f"127.0.0.1:{args.port} dinlenemedi ({ex}). Monitör zaten açık olabilir: "
              f"http://127.0.0.1:{args.port}/ (başka port için --port).", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{server.port}/"
    print(f"LTspice monitörü: {url}  şemalar: {url}sema", flush=True)
    print(f"Olay dizini: {directory}. Şemalar: {', '.join(str(r) for r in server.monitor.asc_roots)} ve LTspice'ta "
          "son açılanlar. Durdurmak için Ctrl+C.", flush=True)
    for root in server.monitor.asc_roots:
        if not root.exists():
            print(f"Uyarı: {root} yok.", flush=True)
    if not enabled():
        print(f"Not: bu kabukta {ENV_EVENTS}=1 değil; koşumları yapan süreçte açık olmalı.", flush=True)
    if args.open:
        webbrowser.open(url + ("sema" if args.asc else ""))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
