"""
LTspice Core Adapter
~~~~~~~~~~~~~~~~~~~~
LTspice simülatörünü arka planda (headless/batch mode) çalıştıran,
parametreleri güncelleyen ve sonuçları toplayan çekirdek modül.
"""

from __future__ import annotations

import ctypes
import functools
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

from .cpu_guard import CpuLease, CpuSlotPool, CpuSlotTimeout, available_cpus, default_pool, ltspice_thread_limit
from .diagnostics import SPICEDiagnostics
from .netlist import (
    ANALYSIS_DIRECTIVE_RE,
    END_DIRECTIVE_RE,
    insert_before_end,
    ltspice_visible,
    measured_signals,
    single_connection_nodes,
    validated_save_signal,
)
from .parser import LogParser, RawParser, WaveformSummary
from .quantizer import parse_spice_value
from .schematic import decode_asc_bytes, encode_asc_text

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

# Geriye dönük uyumluluk için eski (özel) adlar
_END_DIRECTIVE_RE = END_DIRECTIVE_RE
_ANALYSIS_DIRECTIVE_RE = ANALYSIS_DIRECTIVE_RE

# ".param R1=1k R2={2*R1} C1 100n ; yorum" satırındaki "isim=değer" çiftleri (.asc şemalarında TEXT satırları da eşleşir)
_PARAM_LINE_RE = re.compile(
    r"^\s*(?:TEXT\s+[-0-9]+\s+[-0-9]+\s+\w+\s+\d+\s+!)?\.param\b", re.IGNORECASE
)
_PARAM_PAIR_RE = re.compile(
    r"(?P<name>[A-Za-z_]\w*)(?P<eq>\s*=\s*)(?P<val>\{[^}]*\}|[^\s;{}=]+)"
)
# Eski sözdizimi: ".param NAME VALUE"
_PARAM_LEGACY_RE = re.compile(
    r"^(?P<head>\s*(?:TEXT\s+[-0-9]+\s+[-0-9]+\s+\w+\s+\d+\s+!)?\.param\s+)(?P<name>[A-Za-z_]\w*)(?P<sp>\s+)(?P<val>\{[^}]*\}|[^\s;=]+)",
    re.IGNORECASE,
)
_PARAM_NAME_RE = re.compile(r"^[A-Za-z_]\w*$")
# Eleman değeri olarak kabul edilen ilk belirteç: sayı veya {ifade}
_NUMERIC_VALUE_RE = re.compile(r"^(?:\{[^}]*\}|[+-]?\.?\d)")

# LTspice'ın çalışma alanına bıraktığı çıktı dosyası uzantıları (.db: LTspice 26'nın devre başına yazdığı önbellek)
_ARTIFACT_SUFFIXES = (".net", ".asc", ".log", ".raw", ".db", ".png")
# run_batch'in her işe açtığı alt dizinler (_batch_<ad>_<sıra>); temizlik ve kota bunlara da bakar
_BATCH_DIR_PREFIX = "_batch_"


# --------------------------------------------------------------------------- çökme kodu
#: Hata önem dereceli NTSTATUS çıkış kodları (0xC0000000 ve üstü: süreç işlenmemiş bir istisnayla bitti). Adlar
#: Windows'un ileti tablosundan (ntdll.dll, FormatMessage; examples/16 yazdırır), ezberden değil.
_NTSTATUS_NAMES = {
    0xC0000005: "erişim ihlali (geçersiz bellek erişimi)",
    0xC00000FD: "yığın taşması",
    0xC0000094: "tamsayı sıfıra bölme",
    0xC0000409: "yığın arabelleği taşması",
    0xC0000017: "yetersiz bellek",
    0xC000001D: "geçersiz işlemci komutu",
    0xC000013A: "Ctrl+C ile sonlandırıldı",
}
#: Yalnızca LTspice'ta ölçülen nedenler (examples/16)
_NTSTATUS_HINTS = {
    0xC00000FD: ("LTspice 26.1.1 kendini doğrudan ya da dolaylı çağıran bir .subckt veya .func tanımında hata "
                 "mesajı yazmak yerine bu kodla çöküyor (ölçüldü); alt devre ve fonksiyon tanımlarını kontrol edin."),
}


def _crash_description(returncode: Optional[int]) -> Optional[str]:
    """
    Süreç çöktüyse (Windows: NTSTATUS hata kodu, POSIX: sinyal) açıklaması, değilse None.

    LTspice çöktüğünde stderr boştur ve log yalnızca başlıktır (ölçüldü: kendini çağıran alt devre, 0xC00000FD).
    Eskiden çıkış kodu yalnızca stderr doluysa yazılıyordu; sonuç "dalga formu üretilemedi, sözdizimini kontrol
    edin" diyordu. Hata mesajlı sıradan koşumlar rc=1 döner (ölçüldü: 'Division by zero.'); onlar çökme değildir.
    """
    if returncode is None:
        return None
    if returncode < 0:
        try:
            name = signal.Signals(-returncode).name
        except ValueError:
            name = f"sinyal {-returncode}"
        return f"LTspice çöktü ({name}); log ve .raw eksik ya da yarım olabilir."
    code = returncode & 0xFFFFFFFF
    if code < 0xC0000000:
        return None
    name = _NTSTATUS_NAMES.get(code, "bilinmeyen NTSTATUS hatası")
    text = f"LTspice çöktü: {name} (0x{code:08X}); log ve .raw eksik ya da yarım olabilir."
    hint = _NTSTATUS_HINTS.get(code)
    return f"{text} {hint}" if hint else text


# --------------------------------------------------------------------------- Windows iş nesnesi (Job Object)
_JOB_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
_JOB_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_EXTENDED_LIMIT_INFORMATION = 9  # JOBOBJECTINFOCLASS


@functools.lru_cache(maxsize=None)
def _job_api() -> Tuple[Any, Any]:
    """kernel32 işlevleri (bağımsız değişken türleriyle) ve JOBOBJECT_EXTENDED_LIMIT_INFORMATION yapısı."""
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k32.QueryInformationJobObject.restype = wintypes.BOOL
    k32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class Io(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", Io), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    return k32, Extended


class _ProcessJob:
    """
    LTspice süreçlerinin bağlandığı Windows iş nesnesi. Tanıtıcı Python süreci boyunca açık kalır.

    - ``KILL_ON_JOB_CLOSE``: Python süreci nasıl biterse bitsin (çökme, görev yöneticisi) tanıtıcı kapanır ve
      çekirdek işteki LTspice'ları sonlandırır. Ölçüm (examples/16): ebeveyn öldürülünce LTspice yetim kalıp 10 sn'de
      19,7 CPU-sn harcadı; dosya kilitli çekirdek bütçesi ise hemen 12/12 boşaldı, yani yeni koşumlar yetimle
      birlikte çekirdekleri aşırı abone eder (livelock, docs/LESSONS_LEARNED_PERFORMANCE.md §4). İşle 0,5 sn'de öldü.
    - ``DIE_ON_UNHANDLED_EXCEPTION``: işteki her sürece SEM_NOGPFAULTERRORBOX uygular; çöken LTspice Windows Hata
      Bildirimi'ni (WerFault) beklemez. Ölçüm (varsayılan hata kipi): 3,5-4,1 sn -> 0,28-0,30 sn. Python sürecinin
      kendi hata kipine (SetErrorMode) dokunulmaz.

    `open_in_gui` ile açılan pencere işe alınmaz: Python bitince kapanmamalı.
    """

    LIMIT_FLAGS = _JOB_LIMIT_KILL_ON_JOB_CLOSE | _JOB_LIMIT_DIE_ON_UNHANDLED_EXCEPTION

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handle: Optional[int] = None
        self._unavailable = not _IS_WINDOWS

    def _job(self) -> Optional[int]:
        with self._lock:
            if self._handle is None and not self._unavailable:
                try:
                    k32, extended = _job_api()
                    handle = k32.CreateJobObjectW(None, None)
                    info = extended()
                    info.BasicLimitInformation.LimitFlags = self.LIMIT_FLAGS
                    if not handle or not k32.SetInformationJobObject(
                            handle, _JOB_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)):
                        raise OSError(f"Win32 hata {ctypes.get_last_error()}")  # type: ignore[attr-defined]
                    self._handle = handle
                except (OSError, AttributeError) as ex:
                    self._unavailable = True
                    logger.debug("LTspice iş nesnesi kurulamadı (yetim süreç koruması yok): %s", ex)
            return self._handle

    def assign(self, process: Any) -> bool:
        """Süreci işe bağlar; bağlanamazsa (Windows değil, sahte süreç, işletim sistemi reddetti) False."""
        process_handle = getattr(process, "_handle", None)  # subprocess.Popen'ın Windows süreç tanıtıcısı
        if process_handle is None:
            return False  # testlerin sahte Popen'ı: PID'le (999) gerçek bir sürece dokunulmaz
        job = self._job()
        if job is None:
            return False
        try:  # en iyi çaba: koruma kurulamazsa koşum yine de sürer (istisna _execute'u bozmasın)
            if _job_api()[0].AssignProcessToJobObject(job, int(process_handle)):
                return True
            logger.debug("LTspice süreci iş nesnesine bağlanamadı: Win32 hata %s",
                         ctypes.get_last_error())  # type: ignore[attr-defined]
        except (OSError, ValueError, TypeError, ctypes.ArgumentError) as ex:
            logger.debug("LTspice süreci iş nesnesine bağlanamadı: %s", ex)
        return False

    def limit_flags(self) -> int:
        """İşin ``LimitFlags`` değeri (işletim sisteminden okunur); iş yoksa 0."""
        job = self._job()
        if job is None:
            return 0
        k32, extended = _job_api()
        info = extended()
        if not k32.QueryInformationJobObject(
                job, _JOB_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info), None):
            return 0
        return int(info.BasicLimitInformation.LimitFlags)


_PROCESS_JOB = _ProcessJob()


def _assign_to_job(process: Any) -> bool:
    """LTspice sürecini sürecin iş nesnesine bağlar (bkz. `_ProcessJob`)."""
    return _PROCESS_JOB.assign(process)


def sanitize_circuit_name(name: Any) -> str:
    """
    Devre adını dosya sistemi için güvenli hale getirir (yol ayırıcıları ve özel karakterler atılır).
    Adaptör ve araçlar aynı fonksiyonu kullanır; böylece oluşturma ve sonraki çağrılar aynı dosyaya bakar.
    """
    safe_name = "".join(c for c in str(name) if c.isalnum() or c in ("_", "-"))
    return safe_name.strip("-_") or "circuit"


def _validated_value(key: str, value: Any) -> str:
    """Parametre/eleman değerini doğrular (satır sonu, yorum veya kontrol karakteri enjeksiyonu engellenir)."""
    value_str = str(value).strip()
    if not value_str or any(c in value_str for c in "\r\n;") or any(ord(c) < 32 for c in value_str):
        raise ValueError(f"Geçersiz parametre değeri ({key}): {value_str!r}")
    return value_str


def _update_param(content: str, key: str, value_str: str) -> Tuple[str, bool]:
    """
    Var olan bir .param tanımının değerini değiştirir; (yeni_metin, bulundu_mu) döner.
    Aynı satırdaki birden fazla parametreyi (".param R1=1k R2=2k") ve "+" devam satırlarını bozmaz.
    """
    key_l = key.lower()
    found = False
    lines = content.splitlines(keepends=True)
    in_param_block = False  # ".param" satırının "+" ile başlayan devam satırları da taranır

    for i, line in enumerate(lines):
        if _PARAM_LINE_RE.match(line):
            in_param_block = True
        elif not (in_param_block and line.lstrip().startswith("+")):
            in_param_block = False
            continue
        body = line.rstrip("\r\n")
        eol = line[len(body):]
        code, semi, comment = body.partition(";")

        def _replace_pair(m: "re.Match[str]") -> str:
            nonlocal found
            if m.group("name").lower() == key_l:
                found = True
                return f"{m.group('name')}{m.group('eq')}{value_str}"
            return m.group(0)

        new_code = _PARAM_PAIR_RE.sub(_replace_pair, code)
        if not found and "=" not in code:
            legacy = _PARAM_LEGACY_RE.match(code)
            if legacy and legacy.group("name").lower() == key_l:
                found = True
                new_code = (
                    f"{legacy.group('head')}{legacy.group('name')}{legacy.group('sp')}{value_str}"
                    f"{code[legacy.end():]}"
                )
        if new_code != code:
            lines[i] = new_code + semi + comment + eol
        if found:
            break

    return "".join(lines), found


def _set_param(content: str, key: str, value: Any) -> str:
    """
    Netlist içinde .param değerini günceller; yoksa .end öncesine ekler.
    Aynı satırdaki birden fazla parametreyi (".param R1=1k R2=2k") bozmaz.
    """
    key = str(key)
    if not _PARAM_NAME_RE.match(key):
        raise ValueError(f"Geçersiz parametre adı: {key!r}")
    value_str = _validated_value(key, value)

    content, found = _update_param(content, key, value_str)
    if found:
        return content
    if content.lstrip().startswith("Version ") and "SHEET " in content:
        sep = "" if content.endswith("\n") else "\n"
        return f"{content}{sep}TEXT 0 0 Left 2 !.param {key}={value_str}\n"
    return insert_before_end(content, [f".param {key}={value_str}"])


def _is_param_referenced(content: str, key: str) -> bool:
    """`{key}` (veya `{2*key}` gibi bir ifade içinde) ya da `.step param key` olarak kullanılıyor mu?"""
    word = rf"(?<![\w.]){re.escape(key)}(?!\w)"
    return bool(
        re.search(rf"\{{[^}}\n]*{word}[^}}\n]*\}}", content, re.IGNORECASE)
        or re.search(rf"^[ \t]*\.step\s+param\s+{re.escape(key)}(?!\w)", content, re.IGNORECASE | re.MULTILINE)
    )


def _replace_value_field(field_text: str, kind: str, value_str: str) -> Optional[Tuple[str, str]]:
    """
    Eleman değer alanının ilk belirtecini (``1k``, ``{R}``, ``R=1k``; V/I'da ``DC 5``'in 5'i) ``value_str`` yapar;
    (yeni alan, eski değer) döner. Alanın geri kalanı (``tc=...``, ``Rser=...``) korunur. İlk belirteç düz bir sayı
    ya da ``{ifade}`` değilse (``PULSE(...)``, ``SINE(...)``, model adı) None: değer bir sayıyla değiştirilemez.

    Netlist satırında alan düğümlerden sonrasıdır; ``.asc``'de ``SYMATTR Value`` satırının değeri. LTspice netlisterı
    o değeri olduğu gibi netlistin değer alanına yazar (tests/data/ref26_sch_connected: ``SYMATTR Value 5`` ->
    ``V1 N001 0 5``), bu yüzden iki biçimde aynı kural geçerli.
    """
    prefix = ""
    if kind in "VI":
        dc = re.match(r"(?i)(DC[ \t]+)", field_text)
        if dc:
            prefix, field_text = dc.group(1), field_text[dc.end():]
    tok = re.match(r"(?P<lead>[A-Za-z_]\w*=)?(?P<val>\{[^}]*\}|\S+)(?P<after>.*)$", field_text)
    if not tok or not _NUMERIC_VALUE_RE.match(tok.group("val")):
        return None
    return f"{prefix}{tok.group('lead') or ''}{value_str}{tok.group('after')}", tok.group("val")


def _asc_value_line(lines: Sequence[str], name: str) -> Optional[int]:
    """
    ``SYMATTR InstName <name>`` taşıyan sembolün ``SYMATTR Value`` satırının sırası; sembol ya da değeri yoksa None.

    Sembol bloğu ``SYMBOL`` satırını izleyen ``WINDOW``/``SYMATTR`` satırlarıdır ve öznitelik sırası sabit değildir:
    LTspice 26.1.1'in kendi örneği Applications/LT1963-1.5.asc'de C1'in Value ve SpiceLine satırları InstName'den önce,
    bir örnekle-tut şemasında (tests/test_parameters.py) V1'in Value satırı InstName'den önce. Eskiden yalnızca
    ``InstName`` satırının hemen ardındaki ``Value`` aranıyordu; bu semboller bulunamıyordu.
    """
    in_symbol, inst, value_at = False, None, None
    for i, line in enumerate([*lines, ""]):
        if in_symbol and line.startswith(("SYMATTR ", "WINDOW ")):
            parts = line.split(None, 2)
            if parts[0] == "SYMATTR" and len(parts) > 1:
                if parts[1] == "InstName":
                    inst = parts[2].strip() if len(parts) > 2 else ""
                elif parts[1] == "Value":
                    value_at = i
            continue
        if in_symbol and inst is not None and inst.lower() == name.lower():
            return value_at
        in_symbol, inst, value_at = line.startswith("SYMBOL "), None, None
    return None


def _set_element_value(content: str, name: str, value_str: str) -> Optional[Tuple[str, str]]:
    """
    R/C/L elemanının (ve yalın V/I kaynağının) değerini değiştirir; (yeni_metin, eski_değer) döner.
    Eleman bulunamaz veya değeri düz bir sayı/ifade değilse (PULSE, SINE, model adı vb.) `None` döner
    (bkz. `_replace_value_field`; ``.asc``'de değer satırı `_asc_value_line` ile bulunur).
    """
    kind = name[:1].upper()
    if kind not in "RCLVI":
        return None
    if _is_asc_text(content):
        lines = content.splitlines(keepends=True)
        at = _asc_value_line([ln.rstrip("\r\n") for ln in lines], name)
        if at is None:
            return None
        body = lines[at].rstrip("\r\n")
        replaced = _replace_value_field(body[len("SYMATTR Value"):].strip(), kind, value_str)
        if replaced is None:
            return None
        lines[at] = f"SYMATTR Value {replaced[0]}{lines[at][len(body):]}"
        return "".join(lines), replaced[1]
    head_re = re.compile(
        rf"^(?P<head>[ \t]*{re.escape(name)}[ \t]+\S+[ \t]+\S+[ \t]+)(?P<rest>[^;\r\n]*)(?P<tail>;.*)?$",
        re.IGNORECASE,
    )
    lines = content.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if i == 0 or line.lstrip().startswith(("*", ".", "+")):  # ilk satır SPICE'ta başlıktır
            continue
        body = line.rstrip("\r\n")
        eol = line[len(body):]
        m = head_re.match(body)
        if not m:
            continue
        replaced = _replace_value_field(m.group("rest"), kind, value_str)
        if replaced is None:
            return None
        lines[i] = f"{m.group('head')}{replaced[0]}{m.group('tail') or ''}{eol}"
        return "".join(lines), replaced[1]
    return None


def list_param_names(content: str) -> List[str]:
    """Netlistte ``.param`` ile tanımlı parametre adları (tanım sırasıyla, tekrarsız)."""
    names: List[str] = []
    in_param_block = False
    for line in content.splitlines():
        if _PARAM_LINE_RE.match(line):
            in_param_block = True
        elif not (in_param_block and line.lstrip().startswith("+")):
            in_param_block = False
            continue
        code = line.split(";", 1)[0]
        found = [m.group("name") for m in _PARAM_PAIR_RE.finditer(code)]
        if not found and "=" not in code:
            legacy = _PARAM_LEGACY_RE.match(code)
            if legacy:
                found = [legacy.group("name")]
        for name in found:
            if name.lower() not in (n.lower() for n in names):
                names.append(name)
    return names


def list_element_names(content: str) -> List[str]:
    """Netlistteki eleman adları (R1, C1, V1...): yorum, direktif ve devam satırları hariç ilk belirteçler."""
    names: List[str] = []
    for idx, line in enumerate(content.splitlines()):
        stripped = line.strip()
        if idx == 0 or not stripped or stripped[0] in "*;.+":  # ilk satır SPICE'ta başlıktır
            continue
        name = stripped.split()[0]
        if name.lower() not in (n.lower() for n in names):
            names.append(name)
    return names


# .asc şemalarında direktifler "TEXT x y Left 2 !.save ..." satırlarında durur
_ASC_DIRECTIVE_PREFIX = r"^[ \t]*(?:TEXT\s+[-0-9]+\s+[-0-9]+\s+\w+\s+\d+\s+!)?"
_ANY_SAVE_RE = re.compile(_ASC_DIRECTIVE_PREFIX + r"\.save\b", re.IGNORECASE | re.MULTILINE)
# ".options ... threads=N" (kullanıcı çözücü iş parçacığı sayısını zaten belirlemiş mi?)
_THREADS_OPTION_RE = re.compile(
    _ASC_DIRECTIVE_PREFIX + r"\.opt(?:ion)?s?\b[^\n;]*\bthreads\s*=\s*(?P<val>[^\s;]*)",
    re.IGNORECASE | re.MULTILINE,
)
#: Adaptörün kendi eklediği ".options threads" satırını işaretler. Araçlar çalışma alanındaki devre dosyasını
#: hem kaynak hem hedef olarak kullandığı için satır dosyada kalabilir; işaret sayesinde bir sonraki koşumda
#: kullanıcı ayarı sanılmaz, güncel bütçeyle yeniden yazılır.
AUTO_THREADS_MARK = "ltspice_llm:auto-threads"
_AUTO_THREADS_LINE_RE = re.compile(
    rf"^[^\n]*\.opt(?:ion)?s?\b[^\n]*;\s*{re.escape(AUTO_THREADS_MARK)}[^\n]*(?:\r?\n|$)",
    re.IGNORECASE | re.MULTILINE,
)


def strip_auto_directives(content: str) -> str:
    """Adaptörün otomatik eklediği ``.options threads=... ; ltspice_llm:auto-threads`` satırlarını siler."""
    return _AUTO_THREADS_LINE_RE.sub("", content)


def _user_threads(content: str) -> Tuple[bool, Optional[int]]:
    """
    Kullanıcının netlistte kendisinin verdiği ``threads`` değeri: (var_mı, sayı ya da çözülemediyse None).
    Yalnızca LTspice'ın okuduğu kısma bakılır: ``.end`` sonrasındaki ayar sayılmaz, adaptör kendi sınırını ekler.
    """
    match = _THREADS_OPTION_RE.search(strip_auto_directives(ltspice_visible(content)))
    if not match:
        return False, None
    try:
        return True, max(1, int(float(match.group("val"))))
    except (ValueError, OverflowError):  # 'inf', '1e999': int(float(...)) OverflowError
        return True, None


def _is_asc_text(content: str) -> bool:
    return content.lstrip().startswith("Version ") and "SHEET " in content


def _read_circuit_file(path: Path) -> Tuple[str, Optional[str]]:
    """
    Devre dosyasını okur; (metin, .asc kodlaması ya da None) döner.

    ``.asc`` şemalar LTspice'ın kodlamasıyla (cp1252 ya da UTF-16LE) çözülür: eskiden UTF-8 + ``errors="ignore"``
    ile okunuyordu ve ``µ`` baytı (0xB5) sessizce siliniyordu (``1µ`` -> ``1`` = 1 F; ölçümde τ 1 ms yerine
    ölçülemedi), UTF-16 şemalar ise hiç çalışmıyordu. Netlistler UTF-8 okunur, satır sonları LF'ye çevrilir.
    """
    data = path.read_bytes()
    if path.suffix.lower() == ".asc":
        return decode_asc_bytes(data)
    text = _normalize_newlines(data.decode("utf-8", errors="ignore"))
    if text.startswith("﻿"):
        text = text[1:]
    return text, None


def _normalize_newlines(text: str) -> str:
    """
    CRLF ve CR'yi LF yapar (Python'un metin kipi gibi). Netlist metin kipinde yazılır (Windows'ta LF -> CRLF);
    bayt olarak okunup böyle çevrilmezse her okuma-yazma turunda bir CR birikir, ``.end\\r`` tanınmaz ve
    ``.options threads`` LTspice'ın okumadığı ``.end`` sonrasına eklenir (ölçüm: 4 eşzamanlı koşumun hepsi
    ``Maximum thread count: 12``, takılma bekçisinin ``threads=1`` yeniden denemesi de etkisiz).
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _write_circuit_file(path: Path, content: str, asc_encoding: Optional[str] = None) -> None:
    """Devre dosyasını yazar: ``.asc`` LTspice'ın okuyabileceği kodlamayla (asla UTF-8), netlist UTF-8 ile."""
    if path.suffix.lower() == ".asc":
        path.write_bytes(encode_asc_text(content, asc_encoding))
    else:
        path.write_text(content, encoding="utf-8")


def _staging_stem(stem: str) -> str:
    """
    Kaynağın klasöründe koşulan kopyanın adı (uzantısız). Süreç ve iş parçacığına özgüdür: kullanıcının dosyalarıyla
    ve aynı klasördeki eşzamanlı koşumlarla çakışmaz (`schematic_to_netlist`'in geçici kopyasıyla aynı biçim).
    """
    return f"_ltspice_llm_{stem}_{os.getpid()}_{threading.get_ident()}"


def _same_dir(a: Path, b: Path) -> bool:
    """İki yol aynı klasör mü (büyük/küçük harf, kısa ad ve bağlantı farkı gözetmeden)."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _collect_staged_outputs(staged: Path, target: Path, notes: List[str]) -> None:
    """
    Kaynağın klasöründe koşulan kopyayı ve LTspice'ın yanına yazdığı her dosyayı (.log, .raw, .op.raw, .db, şemadan
    üretilen .net) çalışma alanındaki adlarına taşır; kullanıcının klasöründe geçici dosya kalmaz. Steplenmiş ``.raw``
    yanındaki ``.log``'la birlikte taşınır: spicelib adımları oradan okur.
    """
    for path in staged.parent.glob(f"{staged.stem}.*"):
        dest = target.with_name(target.stem + path.name[len(staged.stem):])
        try:
            os.replace(path, dest)
        except OSError:  # başka bir sürücü: kopyala, sonra sil
            try:
                shutil.copyfile(path, dest)
                path.unlink()
            except OSError as ex:
                notes.append(f"'{path}' çalışma alanına taşınamadı: {ex}")


def _add_directive(content: str, line: str) -> str:
    """Bir SPICE direktifini ekler: netlistte ``.end`` öncesine, ``.asc`` şemada yeni bir TEXT satırı olarak."""
    if _is_asc_text(content):
        sep = "" if content.endswith("\n") else "\n"
        return f"{content}{sep}TEXT 0 0 Left 2 !{line}\n"
    return insert_before_end(content, [line])


def _apply_save_directive(content: str, save_signals: Sequence[str]) -> Tuple[str, List[str]]:
    """
    Netliste ``.save`` direktifi ekleyerek kaydedilecek sinyalleri sınırlar; (yeni_metin, sinyaller) döner.

    ``.meas`` satırlarında geçen sinyaller listeye otomatik eklenir: aksi halde LTspice o sinyalleri
    kaydetmez ve ölçümler üretilemez. Netlistte zaten bir ``.save`` varsa dokunulmaz.
    """
    signals: List[str] = []
    for sig in list(save_signals) + measured_signals(content):
        norm = validated_save_signal(sig)
        if norm.lower() not in (s.lower() for s in signals):
            signals.append(norm)
    if not signals or _ANY_SAVE_RE.search(ltspice_visible(content)):
        return content, []
    return _add_directive(content, f".save {' '.join(signals)}"), signals


def _apply_solver_threads(content: str, threads: int) -> str:
    """
    ``.options threads=N`` ekleyerek LTspice çözücüsünün iş parçacığı sayısını sınırlar.

    Önceki koşumlardan kalan otomatik satır yenisiyle değiştirilir; kullanıcı netlistte ``threads``
    değerini kendisi belirlediyse dokunulmaz.
    """
    content = strip_auto_directives(content)
    if _user_threads(content)[0]:
        return content
    return _add_directive(content, f".options threads={int(threads)} ; {AUTO_THREADS_MARK}")


@dataclass
class _ExecOutcome:
    """Tek bir LTspice süreç koşumunun sonucu."""
    errors: List[str]
    stalled: bool
    #: LTspice süreci başlatıldı mı (Popen hata vermedi)
    launched: bool = False


def _file_signature(path: Path) -> Optional[Tuple[int, int]]:
    """Takılma bekçisi için dosyanın (boyut, değişiklik zamanı) imzası; dosya yoksa None."""
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


@dataclass
class ParameterApplication:
    """`apply_parameters` sonucu: yeni netlist metni, yapılan değişiklikler ve çözülemeyen adlar."""
    content: str
    changes: Dict[str, str] = field(default_factory=dict)
    unresolved: List[str] = field(default_factory=list)


def apply_parameters(content: str, parameters: Dict[str, Any]) -> ParameterApplication:
    """
    Verilen ad/değer çiftlerini netliste uygular. Her ad için sırayla:

    1. Var olan ``.param`` tanımı güncellenir.
    2. Netlistte ``{ad}`` olarak kullanılıyorsa (ama tanımlı değilse) ``.param`` eklenir.
    3. Aynı adlı bir R/C/L/V/I elemanı varsa elemanın değeri değiştirilir.
    4. Hiçbiri yoksa ``.param`` yine eklenir ama ad `unresolved` listesine girer (etkisizdir).
    """
    result = ParameterApplication(content=content)
    for raw_key, raw_value in parameters.items():
        key = str(raw_key)
        if not _PARAM_NAME_RE.match(key):
            raise ValueError(f"Geçersiz parametre adı: {key!r}")
        value_str = _validated_value(key, raw_value)

        updated, found = _update_param(result.content, key, value_str)
        if found:
            result.content = updated
            result.changes[key] = f".param {key}={value_str}"
        elif _is_param_referenced(result.content, key):
            result.content = _set_param(result.content, key, value_str)
            result.changes[key] = f".param {key}={value_str} (eklendi)"
        else:
            element = _set_element_value(result.content, key, value_str)
            if element:
                result.content, old_value = element
                result.changes[key] = f"eleman {key}: {old_value} -> {value_str}"
            else:
                result.content = _set_param(result.content, key, value_str)
                result.changes[key] = f".param {key}={value_str} (eklendi ama kullanılmıyor)"
                result.unresolved.append(key)
    return result


@dataclass
class SimulationResult:
    """Simülasyon çalıştırma çıktısını temsil eden veri sınıfı."""
    success: bool
    netlist_path: Path
    log_path: Optional[Path] = None
    raw_path: Optional[Path] = None
    elapsed_seconds: float = 0.0
    measurements: Dict[str, Any] = field(default_factory=dict)
    operating_points: Dict[str, float] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    raw_summary: Optional[Dict[str, WaveformSummary]] = None
    stepped_measurements: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    step_statistics: Dict[str, Dict[str, float]] = field(default_factory=dict)
    diagnostics: Optional[Dict[str, Any]] = None
    failed_measurements: List[str] = field(default_factory=list)
    step_parameters: List[Dict[str, float]] = field(default_factory=list)
    parameter_changes: Dict[str, str] = field(default_factory=dict)
    #: Koşumda LTspice çözücüsünün kullandığı iş parçacığı sayısı: adaptörün verdiği sınır ya da netlistin kendi
    #: ``threads`` değeri; LTspice ikisini de mantıksal çekirdek sayısına kırpar. None: LTspice varsayılanı, adaptörün
    #: çözemediği bir ifade (ör. ``threads={N}``) ya da LTspice'ın hiç başlatılamadığı koşum.
    solver_threads: Optional[int] = None
    #: Aynı dosyalara (aynı ``circuit_name`` + çalışma dizini) yazan sonraki bir koşum bu sonucun .raw/.log
    #: dosyalarının üzerine yazdıysa True. O anda ``raw_path``/``log_path`` None yapılır: eski sonuç, üzerine
    #: yazılmış dosyadan sessizce başka bir devrenin dalga formunu okumaz. Ölçümler ve özetler korunur.
    superseded: bool = False
    #: LTspice'ın ``.meas`` sonuçlarını yazdığı SQLite dosyası (bkz. `parser.read_measure_db`); yoksa None
    db_path: Optional[Path] = None
    #: ``.four`` sonuçları: sinyal, temel frekans, THD (%), harmonikler (bkz. `parser.parse_fourier_blocks`)
    fourier: List[Dict[str, Any]] = field(default_factory=list)
    #: ``convreport``/``.options debugtran`` zorluk puanları ve yakınsama hatası zamanı
    convergence: Dict[str, Any] = field(default_factory=dict)

    def to_llm_dict(self, include_traces_summary: bool = True) -> Dict[str, Any]:
        """
        LLM context penceresi için optimize edilmiş JSON formatı.

        ``status``: "SUCCESS" simülasyon hatasız bitti, "FAILED" simülasyon çalıştı ama başarısız oldu
        (ayrıntı ``errors``/``diagnostics`` içinde). Aracın kendisi çalıştırılamadığında araç katmanı
        ayrıca ``"ERROR"`` döner.
        """
        data: Dict[str, Any] = {
            "status": "SUCCESS" if self.success else "FAILED",
            "elapsed_seconds": round(self.elapsed_seconds, 4),
            "measurements": self.measurements,
            "operating_points": self.operating_points,
            "errors": self.errors,
            "warnings": self.warnings,
        }
        if self.failed_measurements:
            data["failed_measurements"] = self.failed_measurements
        if self.parameter_changes:
            data["parameter_changes"] = self.parameter_changes
        if self.stepped_measurements:
            data["stepped_measurements"] = self.stepped_measurements
        if self.step_parameters:
            data["step_parameters"] = self.step_parameters
        if self.step_statistics:
            data["step_statistics"] = self.step_statistics
        if self.diagnostics:
            data["diagnostics"] = self.diagnostics
        if self.fourier:
            data["fourier"] = self.fourier
        if self.convergence:
            # Büyük devrelerde puan tablosu yüzlerce satır olabilir: LLM'e en zor 5 cihaz/düğüm gider
            data["convergence"] = {
                k: (dict(list(v.items())[:5]) if isinstance(v, dict) else v) for k, v in self.convergence.items()
            }
        if include_traces_summary and self.raw_summary:
            data["waveforms"] = {
                sig: summary.to_dict()
                for sig, summary in self.raw_summary.items()
            }
        return data


# .raw yolu -> o dosyaları gösteren son sonuç (zayıf referans). Süreç genelindedir: aynı çalışma dizinini
# paylaşan farklı adaptörler de birbirinin eski sonucunu geçersiz kılar.
_LIVE_RESULTS: Dict[str, "weakref.ReferenceType[SimulationResult]"] = {}
_LIVE_RESULTS_LOCK = threading.Lock()


def _result_key(raw_path: Path) -> str:
    return os.path.normcase(str(raw_path.resolve()))


def _supersede_previous_result(raw_path: Path) -> None:
    """Aynı .raw/.log dosyalarına yazacak yeni koşumdan önce, o dosyaları gösteren eski sonucu geçersiz kılar."""
    with _LIVE_RESULTS_LOCK:
        ref = _LIVE_RESULTS.pop(_result_key(raw_path), None)
    previous = ref() if ref is not None else None
    if previous is None:
        return
    previous.superseded = True
    previous.raw_path = None
    previous.log_path = None
    previous.warnings.append(
        f"Bu sonucun dosyaları ({raw_path.stem}.raw/.log) aynı circuit_name ile yapılan sonraki bir koşumla "
        "üzerine yazıldı; dalga formu artık bu sonuca ait değil. Farklı devreler için ayrı circuit_name kullanın."
    )
    logger.info("'%s' dosyaları yeni koşumla üzerine yazılıyor; önceki sonuç geçersiz kılındı.", raw_path.stem)


def _register_result(raw_path: Path, result: SimulationResult) -> None:
    with _LIVE_RESULTS_LOCK:
        _LIVE_RESULTS[_result_key(raw_path)] = weakref.ref(result)


def _run_names(file_name: str) -> Tuple[str, ...]:
    """
    Bir çalışma alanı dosyasının ait olabileceği koşum adları; ilki kotada gruplandığı koşumdur. ``x_bode.png``
    `plot_bode_diagram`'ın ``x`` için çizdiği grafiktir (visualizer.plot_bode) ama ``x_bode`` adlı devrenin dalga formu
    grafiği de olabilir: iki ad da döner, biri elde tutuluyorsa dosya silinmez.
    """
    stem = file_name.split(".")[0].lower()
    if file_name.lower().endswith("_bode.png"):
        return stem[: -len("_bode")], stem
    return (stem,)


def _live_runs() -> Set[Tuple[str, str]]:
    """Hâlâ elde tutulan sonuçların (dizin, ad) çiftleri: kota bu koşumların dosyalarını silmez."""
    with _LIVE_RESULTS_LOCK:
        items = list(_LIVE_RESULTS.items())
    live: Set[Tuple[str, str]] = set()
    for key, ref in items:
        if ref() is not None:
            path = Path(key)
            live.add((os.path.normcase(str(path.parent)), path.name.split(".")[0].lower()))
    return live


class LTSpiceAdapter:
    """
    LTspice ile LLM'ler arasında köprü oluşturan ana adaptör sınıfı.

    Not: Çalışma dosyaları ``circuit_name``'den türetilen sabit adlarla yazılır. Aynı çalışma
    dizininde aynı adı eşzamanlı kullanan iki çağrı birbirinin dosyalarını ezer; paralel iş
    akışlarında her biri için ayrı ``workspace_dir`` kullanın. Aynı adla art arda koşulduğunda
    yeni koşum önceki `SimulationResult`'ı geçersiz kılar (``superseded=True``, ``raw_path`` ve
    ``log_path`` None): eski sonuç, üzerine yazılmış dosyadan sessizce yeni devrenin verisini okumaz.

    Performans: bir parametre taraması için ``run_sweep`` (LTspice'ın kendi ``.step``'i, tek süreç)
    ``run_netlist``'i N kez çağırmaktan hızlıdır; süreç başlatma maliyeti (~0,5 sn) bir kez ödenir.
    Yalnızca ilgilenilen sinyalleri kaydetmek için ``save_signals`` kullanın.
    """

    KNOWN_WIN_PATHS = [
        Path(os.path.expanduser(r"~\AppData\Local\Programs\ADI\LTspice\LTspice.exe")),
        Path(r"C:\Program Files\ADI\LTspice\LTspice.exe"),
        Path(r"C:\Program Files\LTC\LTspiceXVII\XVIIx64.exe"),
        Path(r"C:\Program Files (x86)\LTC\LTspiceXVII\XVIIx64.exe"),
        Path(r"C:\Program Files (x86)\LTC\LTspiceIV\scad3.exe"),
    ]
    KNOWN_MAC_PATHS = [
        Path("/Applications/LTspice.app/Contents/MacOS/LTspice"),
    ]
    #: LTspice yolunu açıkça belirten ortam değişkeni (Linux/Wine sarmalayıcıları dahil her platformda geçerli)
    ENV_EXECUTABLE = "LTSPICE_EXE"
    RAW_FORMATS = ("auto", "ascii", "binary")

    def __init__(
        self,
        executable_path: Optional[Union[str, Path]] = None,
        workspace_dir: Optional[Union[str, Path]] = None,
        default_timeout: int = 30,
        raw_format: str = "auto",
        solver_threads: Optional[int] = None,
        cpu_guard: Union[bool, CpuSlotPool] = True,
        stall_timeout: Optional[float] = 20.0,
        slot_wait_timeout: Optional[float] = None,
        workspace_quota_mb: Optional[float] = None,
    ):
        """
        LTspice adaptörünü başlatır.

        :param executable_path: LTspice çalıştırılabilir dosya yolu (verilmezse ``LTSPICE_EXE`` ortam
            değişkeni, PATH ve bilinen kurulum konumları sırayla denenir).
        :param workspace_dir: Simülasyon dosyalarının tutulacağı geçici/kalıcı dizin.
        :param default_timeout: Simülasyon başına maksimum süre (saniye).
        :param raw_format: ``.raw`` biçimi. ``"auto"`` (varsayılan) ve ``"binary"`` ikili biçimi seçer;
            ikili biçim hem çok daha hızlıdır (ölçümde ~3x simülasyon, ~18x ayrıştırma, ~6x küçük dosya)
            hem de steplenmiş (``.step``) dosyaları spicelib yalnızca ikili biçimde okuyabilir.
            ``"ascii"`` yalnızca ``.raw`` dosyasını elle okumak/incelemek gerektiğinde kullanılmalıdır
            ve ``.step`` ile birlikte çalışmaz.
        :param solver_threads: Bir koşumun en çok kaç çözücü iş parçacığı kullanacağı. ``None``: bu sürecin
            kullanabildiği tüm çekirdekler (`available_cpus`). Netlist ``threads`` değerini kendisi
            belirliyorsa o kullanılır.
        :param cpu_guard: Kilitlenme koruması (varsayılan açık). LTspice çözücüsü affinity'yi ve diğer yükü
            yok sayarak tüm çekirdekler kadar iş parçacığı açar; birden fazla örnek aynı anda çalışınca
            çözücü ilerlemeden döner (livelock). Koruma her koşuma ``.options threads=N`` verir ve tüm
            koşumların N toplamını **makine genelinde** (aynı süreçteki iş parçacıkları, diğer Python
            süreçleri ve ajanlar dahil) çekirdek sayısıyla sınırlar; bütçe doluysa yeni koşum bekler.
            Bir `CpuSlotPool` verilerek özel bir bütçe kullanılabilir. ``False``: LTspice varsayılanı.
        :param stall_timeout: Çözücünün ``.log``/``.raw`` dosyaları bu kadar saniye hiç ilerlemezse koşum
            takılmış (livelock) sayılır, öldürülür ve ``threads=1`` ile bir kez yeniden denenir. Bu,
            korumanın dışında kalan yükler içindir (LTspice arayüzünde açık bir simülasyon, başka araçlar).
            Normal koşumlarda dosyalar saniyenin altında aralıklarla büyür; çok büyük devrelerde çalışma
            noktası hesabı uzun sürüyorsa değeri artırın. ``None``: kapalı.
        :param slot_wait_timeout: Çekirdek bütçesi doluyken en çok kaç saniye beklenir (varsayılan:
            ``max(60, 2 × timeout)``). Süre dolarsa koşum aşırı abone etmek yerine hatayla döner.
        :param workspace_quota_mb: Verilirse her koşumdan (ve `run_batch`'ten) sonra çalışma alanı bu sınıra
            indirilir: en eski koşumlar silinir, elde tutulan `SimulationResult`'ların dosyaları silinmez (bkz.
            `cleanup_workspace`). Varsayılan kapalı: grafik araçları .raw'ı sonradan ``circuit_name`` ile okur ve
            silinmiş bir koşumu yeniden simüle etmek gerekir. Ölçüm (2026-10-06): kotasız varsayılan çalışma alanı
            9 günde 3,16 GB'a çıktı (%99'u .raw).
        """
        if raw_format not in self.RAW_FORMATS:
            raise ValueError(f"Geçersiz raw_format {raw_format!r}; seçenekler: {self.RAW_FORMATS}")
        if solver_threads is not None and (isinstance(solver_threads, bool) or int(solver_threads) < 1):
            raise ValueError(f"solver_threads en az 1 olmalı: {solver_threads!r}")
        if stall_timeout is not None and float(stall_timeout) <= 0:
            raise ValueError(f"stall_timeout pozitif olmalı: {stall_timeout!r}")
        if workspace_quota_mb is not None and not float(workspace_quota_mb) > 0:
            raise ValueError(f"workspace_quota_mb pozitif olmalı: {workspace_quota_mb!r}")
        self.workspace_quota_mb = float(workspace_quota_mb) if workspace_quota_mb is not None else None
        self.solver_threads = int(solver_threads) if solver_threads is not None else None
        self.cpu_guard = cpu_guard
        self.stall_timeout = float(stall_timeout) if stall_timeout is not None else None
        self.slot_wait_timeout = slot_wait_timeout
        self.raw_format = raw_format
        self.executable_path = self._locate_executable(executable_path)
        self.workspace_dir = (
            Path(workspace_dir) if workspace_dir else Path(tempfile.gettempdir()) / "ltspice_llm_runs"
        )
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.default_timeout = default_timeout

    def _locate_executable(self, custom_path: Optional[Union[str, Path]]) -> Path:
        """Sistemdeki LTspice executable dosyasını tespit eder."""
        if custom_path:
            p = Path(custom_path).resolve()
            if p.is_file():
                return p
            raise FileNotFoundError(f"Belirtilen LTspice yolu bulunamadı: {custom_path}")

        env_path = os.environ.get(self.ENV_EXECUTABLE)
        if env_path:
            p = Path(env_path).expanduser().resolve()
            if p.is_file():
                return p
            raise FileNotFoundError(f"{self.ENV_EXECUTABLE} ortam değişkeni geçersiz bir yolu gösteriyor: {env_path}")

        # Sistem PATH kontrolü
        in_path = shutil.which("LTspice") or shutil.which("LTspice.exe") or shutil.which("XVIIx64.exe")
        if in_path:
            return Path(in_path).resolve()

        # Bilinen konumlara bak
        for p in [*self.KNOWN_WIN_PATHS, *self.KNOWN_MAC_PATHS]:
            if p.is_file():
                return p.resolve()

        # spicelib yardımıyla arama
        try:
            from spicelib.simulators.ltspice_simulator import LTspice
            if LTspice.is_available() and LTspice.spice_exe:
                cand = Path(LTspice.spice_exe[0]).resolve()
                if cand.is_file():
                    return cand
        except Exception:
            pass

        raise FileNotFoundError(
            "LTspice executable (LTspice.exe / XVIIx64.exe / LTspice.app) sistemde bulunamadı. "
            f"Lütfen executable_path argümanı veya {self.ENV_EXECUTABLE} ortam değişkeni ile dosya yolunu belirtin."
        )

    def _use_ascii_raw(self) -> bool:
        """
        Bu çalıştırmada LTspice'a `-ascii` verilip verilmeyeceği (bkz. `raw_format`): yalnızca açıkça istenince.

        ``"auto"`` her zaman ikili (binary) seçer: ölçülen 500 kHz senkron buck devresinde ASCII
        `.raw` simülasyonu 3,54 sn / 39,2 MB / 0,95 sn ayrıştırma sürerken ikili biçim
        1,09 sn / 6,8 MB / 0,05 sn sürüyor (~3x simülasyon, ~18x ayrıştırma). Özet değerler
        iki biçimde de birebir aynı çıkıyor (tran/ac/dc/op/noise'da bağıl fark 0,0). Açık ``"ascii"`` seçimi
        ``.step``'li netlistte de uygulanır (tests/test_adapter_platform.py); spicelib steplenmiş ASCII .raw'ı okuyamaz,
        özet bunu açık bir hatayla bildirir (`parser.open_raw`, examples/18 T1).
        """
        return self.raw_format == "ascii"

    def run_netlist(
        self,
        netlist_content_or_path: Union[str, Path],
        circuit_name: str = "circuit",
        parameters: Optional[Dict[str, Any]] = None,
        timeout: Optional[int] = None,
        summarize_raw: bool = True,
        treat_as_text: bool = False,
        save_signals: Optional[Sequence[str]] = None,
        _extra_directives: Optional[Sequence[str]] = None,
        _run_dir: Optional[Path] = None,
    ) -> SimulationResult:
        """
        Bir netlist'i LTspice üzerinde headless modda çalıştırır.

        :param netlist_content_or_path: Netlist dosya yolu veya doğrudan SPICE netlist metni. Dosya yolu verilen
            devre (``.net`` ya da ``.asc``) LTspice arayüzündeki gibi kendi klasöründe koşulur: yanındaki model
            dosyaları (göreli ``.include``/``.lib``) ve semboller bulunur. Koşum sırasında klasörde
            ``_ltspice_llm_*`` adlı geçici bir kopya durur; kopya ve çıktıları bitince çalışma alanına taşınır, kaynak
            dosyaya dokunulmaz. Klasöre yazılamazsa koşum çalışma alanında yapılır ve uyarı eklenir.
        :param circuit_name: Çalışma dosyasının adı. Aynı adla yapılan yeni koşum bu dosyaların üzerine
            yazar ve önceki sonucu geçersiz kılar (`SimulationResult.superseded`); farklı devrelerin
            ``.raw`` dosyalarını sonradan okuyacaksanız her birine ayrı ad verin.
        :param parameters: Netlist'e uygulanacak ad/değer sözlüğü (bkz. `apply_parameters`): var olan
            .param güncellenir, yoksa aynı adlı R/C/L/V/I elemanının değeri değiştirilir; hiçbirine
            uymayan ad uyarı olarak bildirilir.
        :param timeout: Zaman aşımı süresi (saniye).
        :param summarize_raw: Çıkan .raw dalga formunun özetini LLM için hazırla.
        :param treat_as_text: True ise ilk argüman asla dosya yolu sayılmaz, her zaman netlist metnidir.
            Güvenilmeyen kaynaktan (LLM aracı vb.) gelen girdiler için True verilmelidir.
        :param save_signals: Verilirse netliste ``.save`` eklenir ve LTspice yalnızca bu sinyalleri
            yazar (``["V(out)", "I(L1)"]``). ``.meas`` satırlarında geçen sinyaller otomatik eklenir,
            böylece ölçümler bozulmaz. Netlistte zaten ``.save`` varsa dokunulmaz. Ölçümde
            ``.raw`` 6,8 MB'tan 2,7 MB'a, ayrıştırma 52 ms'ten 22 ms'e indi.
        """
        run_timeout = timeout or self.default_timeout
        t0 = time.perf_counter()

        # Netlist dosyasını hazırla
        target_netlist, param_app = self._prepare_netlist(
            netlist_content_or_path, circuit_name, parameters, treat_as_text=treat_as_text,
            save_signals=save_signals, extra_directives=_extra_directives,
        )
        target_text = _read_circuit_file(target_netlist)[0]
        raw_path = target_netlist.with_suffix(".raw")

        # Önceki çalıştırmadan kalan log/raw/db dosyalarını temizle; onları gösteren eski sonuç artık geçersiz
        _supersede_previous_result(raw_path)
        for stale_path in (target_netlist.with_suffix(".log"), raw_path, target_netlist.with_suffix(".db")):
            if stale_path.exists():
                stale_path.unlink()

        run_notes: List[str] = []
        source_dir = (Path(netlist_content_or_path).resolve().parent
                      if self._looks_like_path(netlist_content_or_path, treat_as_text) else _run_dir)
        run_path = self._stage_next_to_source(target_netlist, source_dir, run_notes)
        try:
            errors, threads = self._run_ltspice(run_path, target_text, run_timeout, run_notes)
        finally:
            if run_path != target_netlist:
                _collect_staged_outputs(run_path, target_netlist, run_notes)
        elapsed = time.perf_counter() - t0
        result = self._collect_result(target_netlist, target_text, param_app, errors, run_notes, summarize_raw)
        result.elapsed_seconds = elapsed
        result.solver_threads = threads
        _register_result(raw_path, result)
        if self.workspace_quota_mb is not None:  # kayıttan sonra: bu sonucun dosyaları korunur
            self.cleanup_workspace(max_total_mb=self.workspace_quota_mb)
        return result

    def _stage_next_to_source(self, target_netlist: Path, source_dir: Optional[Path], notes: List[str]) -> Path:
        """
        LTspice'ın başlatılacağı netlist: kaynağın klasörü verilmiş ve çalışma alanı değilse hazırlanan netlistin oraya
        yazılan kopyası, değilse ``target_netlist``. LTspice göreli ``.include``/``.lib`` yollarını netlistin
        klasöründen, sembolleri ve hiyerarşik blokları şemanın klasöründen çözer (ADI SIMULATION-COMMANDS-REFERENCE
        ".INCLUDE" ve ".LIB" Search order; SCHEMATIC-REFERENCE "Symbol and Library Search Paths"). Çalışma alanındaki
        kopyada model dosyası "File not found" verir, bulunamayan sembol netlistten uyarısız düşer (LTspice 26.1.1,
        examples/12). Klasöre yazılamazsa çalışma alanındaki dosya döner ve uyarı eklenir.
        """
        if source_dir is None or _same_dir(source_dir, self.workspace_dir):
            return target_netlist
        staged = source_dir / f"{_staging_stem(target_netlist.stem)}{target_netlist.suffix}"
        try:
            shutil.copyfile(target_netlist, staged)
        except OSError as ex:
            notes.append(
                f"Devre kendi klasöründe koşulamadı ({source_dir}: {ex}); çalışma alanında koşuldu. Göreli "
                ".include/.lib yolları bulunamayabilir; şemanın yanındaki bir sembol bulunamazsa LTspice onu "
                "netlistten uyarısız düşürür."
            )
            return target_netlist
        return staged

    def _run_ltspice(
        self, target_netlist: Path, target_text: str, run_timeout: float, run_notes: List[str]
    ) -> Tuple[List[str], Optional[int]]:
        """
        LTspice'ı çekirdek bütçesinden kira alarak koşturur; (hatalar, LTspice'ın kullandığı iş parçacığı sayısı) döner.

        Kira her durumda bırakılır. Çözücü takılırsa (livelock) koşum bir kez ``threads=1`` ile yeniden denenir;
        kullanıcı netlistte ``threads`` verdiyse bu değer ezilmez ve yeniden deneme yapılmaz. O durumda dönen sayı
        kiradan değil netlistten gelir. LTspice verilen sayıyı mantıksal çekirdek sayısına kırparak kullanır
        (`ltspice_thread_limit`); dönen sayı da kırpılır. Adaptörün çözemediği bir ifadede (ör. ``threads={N}``) ve
        LTspice hiç başlatılamadıysa None döner. Takılan koşumun yeniden denemesi başlatılamazsa son başlatılan
        (takılan) koşumun sayısı döner.
        """
        log_path, raw_path = target_netlist.with_suffix(".log"), target_netlist.with_suffix(".raw")
        # LTspice komutu: headless (-b); ASCII .raw yalnızca raw_format="ascii" açıkça seçildiyse (bkz. _use_ascii_raw)
        cmd = [str(self.executable_path), "-b"]
        if self._use_ascii_raw():
            cmd.append("-ascii")
        cmd.append(str(target_netlist))
        logger.debug("LTspice komutu: %s", cmd)

        errors: List[str] = []
        user_set, user_value = _user_threads(target_text)
        if user_set and user_value and user_value > available_cpus():
            run_notes.append(
                f"Netlist '.options threads={user_value}' veriyor ama bu süreç {available_cpus()} çekirdek "
                "kullanabiliyor; başka LTspice koşumlarıyla aynı anda çözücü kilitlenebilir."
            )
        lease: Optional[CpuLease] = None
        threads: Optional[int] = None
        ran: Optional[int] = None  # son başlatılan LTspice'a verilen sayı (None: varsayılan ya da hiç başlamadı)
        try:
            try:
                lease, threads = self._acquire_threads(
                    user_value if user_set else self.solver_threads, user_set, run_timeout, run_notes
                )
            except CpuSlotTimeout as ex:
                errors.append(
                    f"LTspice başlatılmadı: çekirdek bütçesi dolu ({ex}). Aynı anda çok fazla LTspice koşumu "
                    "var; birkaç koşumun bitmesini bekleyin ya da slot_wait_timeout değerini artırın."
                )
            if not errors:
                if threads is not None and not user_set:
                    self._write_solver_threads(target_netlist, threads)
                outcome = self._execute(cmd, run_timeout, (log_path, raw_path), self.stall_timeout)
                if outcome.launched:
                    ran = user_value if user_set else threads  # user_value None: çözülemeyen ifade, ör. {N}
                if outcome.stalled and not user_set and threads != 1:
                    # Takılmanın en olası nedeni çok iş parçacıklı çözücünün aşırı abone olması: tek iş
                    # parçacığıyla bir kez daha dene (bekçi kapalı, böylece yavaş ama sağlıklı koşum kesilmez).
                    stalled = (f"Çözücü {self.stall_timeout:g} sn ilerlemedi (olası livelock, "
                               f"threads={threads or 'varsayılan'}); koşum durduruldu")
                    if lease is not None:
                        lease.release()
                    lease = None
                    for stale in (log_path, raw_path):
                        if stale.exists():
                            stale.unlink()
                    try:
                        lease, threads = self._acquire_threads(1, False, run_timeout, run_notes)
                    except CpuSlotTimeout as ex:
                        run_notes.append(f"{stalled}; threads=1 ile yeniden deneme başlatılamadı (çekirdek bütçesi dolu).")
                        outcome = _ExecOutcome([f"Yeniden deneme başlatılamadı: çekirdek bütçesi dolu ({ex})."], False)
                    else:
                        self._write_solver_threads(target_netlist, 1)
                        outcome = self._execute(cmd, run_timeout, (log_path, raw_path), None)
                        if outcome.launched:
                            ran = threads
                            run_notes.append(f"{stalled} ve threads=1 ile yeniden çalıştırıldı.")
                        else:
                            run_notes.append(f"{stalled}; threads=1 ile yeniden deneme başlatılamadı.")
                errors.extend(outcome.errors)
        finally:
            if lease is not None:
                lease.release()
        # LTspice verilen sayıyı mantıksal çekirdek sayısına kırpar
        return errors, (min(ran, ltspice_thread_limit()) if ran is not None else None)

    def _collect_result(
        self,
        target_netlist: Path,
        target_text: str,
        param_app: Optional[ParameterApplication],
        errors: List[str],
        run_notes: List[str],
        summarize_raw: bool,
    ) -> SimulationResult:
        """
        Koşumun sonucunu toplar: log (başarı logdan belirlenir, çıkış kodundan değil), uyarılar, ``.raw`` özeti;
        başarısız koşumda teşhis motoru çalışır.
        """
        log_path, raw_path = target_netlist.with_suffix(".log"), target_netlist.with_suffix(".raw")
        db_path = target_netlist.with_suffix(".db")

        # Log dosyasını analiz et
        log_res = LogParser.parse_file(log_path) if log_path.exists() else None
        from_log: Dict[str, Any] = {}
        warnings: List[str] = []
        if log_res:
            errors.extend(log_res.errors)
            warnings = list(log_res.warnings)
            from_log = dict(
                measurements=log_res.measurements,
                operating_points=log_res.operating_points,
                stepped_measurements=log_res.stepped_measurements,
                step_statistics=log_res.step_statistics,
                failed_measurements=log_res.failed_measurements,
                step_parameters=log_res.step_parameters,
                fourier=log_res.fourier,
                convergence=log_res.convergence,
            )
        elif not errors:
            errors.append("Simülasyon log dosyası üretilemedi.")
        warnings.extend(run_notes)
        warnings.extend(self._netlist_warnings(target_netlist, target_text, param_app))

        # Raw dosyasını analiz et (özet çıkar)
        raw_summary = None
        if summarize_raw and raw_path.exists() and len(errors) == 0:
            try:
                raw_summary = RawParser.summarize_file(raw_path)
                total_steps = max((s.total_steps for s in raw_summary.values()), default=1)
                if total_steps > 3:
                    warnings.append(
                        f"Dalga formu özeti yalnızca ilk 3 adımı kapsıyor (toplam {total_steps} adım)."
                    )
            except Exception as e:
                warnings.append(f"Raw veri özeti çıkarılamadı: {str(e)}")

        # Eğer simülasyon bir analiz türü içeriyorsa (.tran/.ac/.dc/.op/.four/.tf/.noise) ama .raw oluşmadıysa hata sayılır
        has_analysis = bool(ANALYSIS_DIRECTIVE_RE.search(target_text))
        if has_analysis and not raw_path.exists() and len(errors) == 0:
            errors.append("Simülasyon dalga formu (.raw) üretilemedi. Devre sözdizimi veya bağlantılarını kontrol edin.")

        success = (len(errors) == 0) and (log_path.exists())

        # Hata durumunda SPICE teşhis motorunu çalıştır
        diagnostics = None
        if not success:
            log_content = LogParser.read_text(log_path) if log_path.exists() else ""
            report = SPICEDiagnostics.diagnose(log_content, netlist_code=target_text, errors_list=errors)
            if report.has_error:
                diagnostics = report.to_dict()

        return SimulationResult(
            success=success,
            netlist_path=target_netlist,
            log_path=log_path if log_path.exists() else None,
            raw_path=raw_path if raw_path.exists() else None,
            errors=errors,
            warnings=warnings,
            raw_summary=raw_summary,
            diagnostics=diagnostics,
            parameter_changes=dict(param_app.changes) if param_app else {},
            db_path=db_path if db_path.exists() else None,
            **from_log,
        )

    @staticmethod
    def _netlist_warnings(
        target_netlist: Path, target_text: str, param_app: Optional[ParameterApplication]
    ) -> List[str]:
        """LTspice'ın bildirmediği netlist sorunları: tek pine bağlı düğüm ve hiçbir yere uymayan parametre adı."""
        warnings: List[str] = []
        if target_netlist.suffix.lower() == ".net":  # şemada inspect_schematic bakar (NC_xx, açık uçlu ağ)
            dangling = single_connection_nodes(target_text)
            if dangling:
                warnings.append(
                    "Netlistte tek bir pine bağlı düğüm: "
                    + ", ".join(f"{d['node']} ({d['element']})" for d in dangling)
                    + ". LTspice bunu bildirmez; düğüm adında yazım hatası (out/vout) ya da bağlanmamış pin olabilir. "
                    "Bilerek boş bırakılan pin ya da yüksüz ölçüm çıkışıysa yok sayın."
                )
        for key in param_app.unresolved if param_app else ():
            warnings.append(
                f"'{key}' adlı .param/eleman netlistte bulunamadı ve '{{{key}}}' olarak da kullanılmıyor; "
                "eklenen .param etkisizdir, sonuçlar değişmemiş olabilir."
            )
        return warnings

    #: Takılma bekçisinin çözücü dosyalarını yoklama aralığı (saniye)
    _WATCH_INTERVAL = 0.25

    def _guard_pool(self) -> Optional[CpuSlotPool]:
        if isinstance(self.cpu_guard, CpuSlotPool):
            return self.cpu_guard
        return default_pool() if self.cpu_guard else None

    def _acquire_threads(
        self, desired: Optional[int], exact: bool, run_timeout: float, notes: List[str]
    ) -> Tuple[Optional[CpuLease], Optional[int]]:
        """
        Çekirdek bütçesinden kira alır; (kira, çözücü iş parçacığı sayısı) döner.

        ``desired`` None ise bu sürecin kullanabildiği tüm çekirdekler istenir. ``exact`` (kullanıcı netlistte
        ``threads`` vermişse) o kadar yuva boşalana kadar beklenir; aksi halde boş olan kadarıyla yetinilir.
        Koruma kapalıysa kira alınmaz ve ``desired`` olduğu gibi döner (None: LTspice varsayılanı).
        """
        pool = self._guard_pool()
        if pool is None:
            return None, desired
        cpus = available_cpus()
        want = min(desired or cpus, cpus)
        wait = self.slot_wait_timeout if self.slot_wait_timeout is not None else max(60.0, 2.0 * run_timeout)
        # Sıra beklemek zorunda kalan koşum makineyi tek başına kapmak yerine adil bir pay ister
        share = None if exact else max(1, cpus // self.DEFAULT_BATCH_WORKERS)
        lease = pool.acquire(want, minimum=want if exact else 1, wait_timeout=wait, contended_want=share)
        if lease.waited_seconds >= 1.0:
            notes.append(
                f"Diğer LTspice koşumlarının çekirdekleri bırakması için {lease.waited_seconds:.1f} sn beklendi."
            )
        return lease, lease.threads

    @staticmethod
    def _write_solver_threads(netlist_path: Path, threads: int) -> None:
        text, asc_encoding = _read_circuit_file(netlist_path)
        updated = _apply_solver_threads(text, threads)
        if updated != text:
            _write_circuit_file(netlist_path, updated, asc_encoding)

    def _execute(
        self, cmd: List[str], run_timeout: float, watch_paths: Sequence[Path], stall_timeout: Optional[float]
    ) -> _ExecOutcome:
        """
        LTspice'ı çalıştırır. ``stall_timeout`` verilmişse ``watch_paths`` dosyaları o kadar saniye hiç
        değişmediğinde süreci takılmış sayıp sonlandırır (``stalled=True``).

        Windows'ta süreç Python'la birlikte ölen, çökünce WerFault'u beklemeyen bir iş nesnesine bağlanır
        (`_ProcessJob`). Çökme (NTSTATUS / sinyal) stderr boş olsa da hata olarak yazılır (`_crash_description`).
        """
        popen_kwargs: Dict[str, Any] = {}
        if not _IS_WINDOWS:
            popen_kwargs["start_new_session"] = True  # zaman aşımında tüm süreç grubunu sonlandırabilmek için

        errors: List[str] = []
        stalled = launched = False
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                **popen_kwargs,
            )
            launched = True
            _assign_to_job(process)
            started = time.monotonic()
            last_signature: Any = object()
            last_change = started
            stderr = ""
            while True:
                remaining = run_timeout - (time.monotonic() - started)
                if remaining <= 0:
                    errors.append(
                        f"Simülasyon zaman aşımına uğradı ({run_timeout} saniye). Devre yakınsamıyor olabilir."
                    )
                    self._kill_process_tree(process)
                    break
                try:
                    _, stderr = process.communicate(
                        timeout=min(remaining, self._WATCH_INTERVAL) if stall_timeout else remaining
                    )
                    break
                except subprocess.TimeoutExpired:
                    if not stall_timeout:
                        continue
                    now = time.monotonic()
                    signature = tuple(_file_signature(p) for p in watch_paths)
                    if signature != last_signature:
                        last_signature, last_change = signature, now
                    elif now - last_change >= stall_timeout:
                        stalled = True
                        errors.append(
                            f"LTspice çözücüsü {stall_timeout:g} sn boyunca ilerlemedi (.log/.raw değişmedi) ve "
                            "durduruldu. Olası neden: aynı anda çalışan LTspice örnekleri çekirdekleri aşırı "
                            "abone ediyor (livelock)."
                        )
                        self._kill_process_tree(process)
                        break
            crash = _crash_description(process.returncode)
            if crash and not errors:
                errors.append(f"{crash} stderr: {stderr.strip()}" if stderr and stderr.strip() else crash)
            elif process.returncode != 0 and stderr and not errors:
                errors.append(f"LTspice process error (code {process.returncode}): {stderr.strip()}")
        except Exception as ex:
            errors.append(f"Simülasyon yürütme hatası: {str(ex)}")
        return _ExecOutcome(errors, stalled, launched)

    #: `run_batch` için varsayılan eşzamanlı örnek sayısı. i7-1255U (2 P + 8 E çekirdek) üzerinde
    #: ölçülen verim 4 örnekte 2,39x, 6'da 2,38x, 12'de 1,93x; daha fazlası verimi artırmıyor.
    DEFAULT_BATCH_WORKERS = 4

    def default_workers(self) -> int:
        """`run_batch` için varsayılan eşzamanlı LTspice örneği sayısı (en çok `DEFAULT_BATCH_WORKERS`)."""
        return max(1, min(self.DEFAULT_BATCH_WORKERS, available_cpus()))

    @staticmethod
    def threads_per_worker(workers: int) -> int:
        """``workers`` eşzamanlı örnekte çekirdeklerin aşırı abone olmaması için örnek başına çözücü iş parçacığı."""
        return max(1, available_cpus() // max(1, int(workers)))

    def run_batch(
        self,
        jobs: Sequence[Dict[str, Any]],
        max_workers: Optional[int] = None,
        timeout: Optional[int] = None,
    ) -> List["SimulationResult"]:
        """
        Birbirinden **bağımsız** devreleri eşzamanlı LTspice örnekleriyle koşturur.

        Her iş kendi alt çalışma dizinini aldığı için dosya adları çakışmaz. Sonuçlar giriş sırasıyla
        döner. LTspice süreçlerini beklemek G/Ç ağırlıklı olduğundan iş parçacığı havuzu kullanılır:
        Windows'ta ``ProcessPoolExecutor``'ın ``if __name__ == "__main__"`` ve seri hale getirme
        tuzakları oluşmaz.

        **Çözücü iş parçacıkları otomatik sınırlanır.** LTspice çözücüsü varsayılan olarak tüm mantıksal
        çekirdekler kadar iş parçacığı açar ("Maximum thread count: 12"); süreç affinity'sini ve diğer
        yükü yok sayar. Birkaç örnek aynı anda çalışıp bu iş parçacıkları çekirdek bulamayınca çözücü
        CPU yakarak **ilerlemeden döner** (livelock). Ölçüm (i7-1255U, 12 mantıksal çekirdek, iş başına
        normalde ~1 sn)::

            8 örnek, threads=12 (varsayılan)   ->  1/24 başarılı, 23'ü 40 sn'de öldürüldü
            8 örnek, threads=1                 -> 24/24 başarılı, 8 iş ~3,6 sn
            4 örnek × 3 çekirdek, threads=12   ->  0/12 başarılı
            4 örnek × 3 çekirdek, threads=3    -> 12/12 başarılı

        Bu yüzden her işe ``.options threads=çekirdek//max_workers`` eklenir (bkz. `threads_per_worker`;
        adaptörde `solver_threads` verilmişse o, netlistte ``threads`` varsa o kullanılır). Koruma
        (``cpu_guard``) açıksa işçiler aynı makine geneli bütçeyi paylaşır: aynı anda başka süreçler de
        LTspice çalıştırıyorsa işçiler daha az iş parçacığıyla çalışır ya da sırasını bekler.

        **Beklenen hızlanma donanımla sınırlıdır.** Aynı makinede 12 işlik yük: 4 örnekte 2,39x,
        6'da 2,38x, 12'de 1,93x. Hibrit işlemcide E-çekirdek P-çekirdekten 1,95x yavaştır ve tüm
        çekirdekler ortak bir güç bütçesini paylaşır; kusursuz çekirdek sabitlemeyle bile tavan ~3,3x.

        Tek bir devrenin **parametre taraması** için bunu değil `run_sweep`'i kullanın: LTspice'ın
        kendi ``.step``'i tek süreçte çalışır.

        :param jobs: Her biri `run_netlist` anahtar argümanlarını taşıyan sözlükler. En az
            ``netlist_content_or_path`` verilmelidir.
        :param max_workers: Eşzamanlı LTspice örneği sayısı (varsayılan `default_workers`).
            Mantıksal çekirdek sayısının üstündeki değerler kırpılır: iş başına 1 iş parçacığıyla bile
            çekirdekler aşırı abone olur.
        :param timeout: İş başına zaman aşımı; işin kendi ``timeout``'u önceliklidir.
        :returns: Giriş sırasıyla `SimulationResult` listesi. Bir iş istisna atarsa o iş için
            ``success=False`` ve hatayı ``errors`` içinde taşıyan bir sonuç döner.
        """
        job_list = [dict(j) for j in jobs]
        if not job_list:
            return []

        cpus = available_cpus()
        requested = max_workers if max_workers is not None else self.default_workers()
        workers = max(1, min(int(requested), cpus, len(job_list)))
        if max_workers is not None and int(max_workers) > cpus:
            logger.warning(
                "max_workers=%s mantıksal çekirdek sayısının (%s) üstünde; %s'e kırpıldı. Daha fazla "
                "eşzamanlı LTspice örneği çözücüyü aşırı abone edip kilitler.",
                max_workers, cpus, workers,
            )
        threads = self.solver_threads or self.threads_per_worker(workers)
        if workers * threads > cpus:
            logger.warning(
                "%s örnek × solver_threads=%s > %s çekirdek: %s",
                workers, threads, cpus,
                "koruma işçilere daha az iş parçacığı verecek ya da onları sıraya alacak."
                if self._guard_pool() is not None
                else "LTspice çözücüsü aşırı abone olup kilitlenebilir; solver_threads değerini düşürün.",
            )

        def run_one(index_job: Tuple[int, Dict[str, Any]]) -> "SimulationResult":
            index, job = index_job
            name = str(job.get("circuit_name", f"batch_{index}"))
            worker = LTSpiceAdapter(
                executable_path=self.executable_path,
                workspace_dir=self.workspace_dir / f"_batch_{sanitize_circuit_name(name)}_{index}",
                default_timeout=job.get("timeout") or timeout or self.default_timeout,
                raw_format=self.raw_format,
                solver_threads=threads,
                cpu_guard=self.cpu_guard,
                stall_timeout=self.stall_timeout,
                slot_wait_timeout=self.slot_wait_timeout,
            )
            kwargs = {k: v for k, v in job.items() if k != "netlist_content_or_path"}
            kwargs.setdefault("circuit_name", name)
            return worker.run_netlist(job["netlist_content_or_path"], **kwargs)

        results: List[Optional[SimulationResult]] = [None] * len(job_list)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(run_one, (i, j)): i for i, j in enumerate(job_list)}
            for future in as_completed(futures):
                index = futures[future]
                try:
                    results[index] = future.result()
                except Exception as ex:
                    logger.warning("Toplu koşumda %s. iş başarısız: %s", index, ex)
                    results[index] = SimulationResult(
                        success=False,
                        netlist_path=self.workspace_dir / f"batch_{index}",
                        errors=[f"Toplu koşum hatası: {ex}"],
                    )
        if self.workspace_quota_mb is not None:  # işçiler kotasız koşar; kota bütün alana toplu koşumdan sonra
            self.cleanup_workspace(max_total_mb=self.workspace_quota_mb)
        return [r for r in results if r is not None]

    def run_sweep(
        self,
        netlist_content_or_path: Union[str, Path],
        parameter: str,
        values: Sequence[Any],
        circuit_name: str = "sweep",
        timeout: Optional[int] = None,
        summarize_raw: bool = False,
        treat_as_text: bool = False,
        save_signals: Optional[Sequence[str]] = None,
    ) -> SimulationResult:
        """
        Bir parametreyi LTspice'ın kendi ``.step`` direktifiyle **tek süreçte** tarar.

        Aynı taramayı ``run_netlist``'i N kez çağırarak yapmaya göre belirgin biçimde hızlıdır:
        LTspice süreci (~0,5 sn) ve devre kurulumu N kez değil bir kez ödenir. Ölçülen 8 noktalı
        buck taramasında sıralı 8 çağrı 7,32 sn sürerken ``run_sweep`` 3,86 sn sürdü (1,9x) ve
        ölçüm değerleri birebir aynı çıktı.

        Aynı işi süreç havuzuyla (``ProcessPoolExecutor``) paralelleştirmek bu makinede **daha
        yavaştır ve güvenilir değildir**: LTspice çözücüsü zaten çok iş parçacıklıdır
        ("Maximum thread count: 12"), 8 eşzamanlı örnek CPU'yu aşırı abone edip koşumları
        dakikalar sürecek biçimde kilitledi. Bağımsız *farklı* devreleri paralelleştirmek
        gerekiyorsa `run_batch` kullanın.

        :param parameter: Taranacak parametre ya da eleman adı. Netlistte ``{ad}`` olarak
            kullanılıyorsa doğrudan taranır; aynı adlı bir R/C/L/V/I elemanı varsa elemanın değeri
            otomatik olarak ``{ad}`` ile parametrelendirilir.
        :param values: Taranacak değerler (``["10u", "22u"]`` ya da sayılar).
        :param summarize_raw: Steplenmiş ``.raw`` özeti yalnızca ilk 3 adımı kapsar; varsayılan
            olarak kapalıdır çünkü tarama sonuçları ``stepped_measurements`` içinde tam gelir.
        :returns: ``stepped_measurements`` / ``step_statistics`` alanları dolu bir `SimulationResult`.
        """
        if not _PARAM_NAME_RE.match(str(parameter)):
            raise ValueError(f"Geçersiz parametre adı: {parameter!r}")
        value_list = [_validated_value(parameter, v) for v in values]
        if not value_list:
            raise ValueError("'values' boş olamaz; taranacak en az bir değer verin.")
        # LTspice tek adımlık .step'i ("This results in only one step.") koşmadan reddeder, .raw yazmaz.
        # Tekrarlanan değerleri de kendisi atar ("Removed duplicate values."); 1k ile 1000 aynı değerdir.
        distinct: List[str] = []
        seen_numbers: List[float] = []
        for v in value_list:
            try:
                number = parse_spice_value(v)
            except (TypeError, ValueError):
                number = None
            if number is not None and any(math.isclose(number, s, rel_tol=1e-12, abs_tol=0.0) for s in seen_numbers):
                continue
            if number is not None:
                seen_numbers.append(number)
            distinct.append(v)
        if len(distinct) < 2:
            raise ValueError(
                f"run_sweep en az iki farklı değer ister (verilen: {value_list}); LTspice tek adımlık .step'i "
                "koşmadan reddeder. Tek değer için run_netlist(parameters={...}) kullanın."
            )
        value_list = distinct

        content = self._read_source(netlist_content_or_path, treat_as_text=treat_as_text)
        # Dosya yolu verildiyse tarama da kaynağın klasöründe koşulur (bkz. run_netlist): şema orada netliste çevrilse
        # bile netlistteki göreli .include/.lib yolları çalışma alanında bulunmaz.
        source_dir = (Path(netlist_content_or_path).resolve().parent
                      if self._looks_like_path(netlist_content_or_path, treat_as_text) else None)
        if content.lstrip().startswith("Version ") and "SHEET " in content:
            # Şemayı LTspice'ın kendi netlisteriyle (-netlist) metin netliste çevir
            content = self.schematic_to_netlist(
                netlist_content_or_path, circuit_name=f"{circuit_name}_schematic", treat_as_text=treat_as_text
            )

        # Parametre netlistte {ad} olarak kullanılmıyorsa, aynı adlı elemanı parametrele
        if not _is_param_referenced(content, parameter):
            element = _set_element_value(content, parameter, f"{{{parameter}}}")
            if element is None:
                raise ValueError(
                    f"'{parameter}' netlistte '{{{parameter}}}' olarak kullanılmıyor ve aynı adlı bir "
                    "R/C/L/V/I elemanı da yok; taranacak bir şey bulunamadı."
                )
            content, _ = element

        step_line = f".step param {parameter} list {' '.join(value_list)}"
        return self.run_netlist(
            content,
            circuit_name=circuit_name,
            timeout=timeout,
            summarize_raw=summarize_raw,
            treat_as_text=True,
            save_signals=save_signals,
            _extra_directives=[step_line],
            _run_dir=source_dir,
        )

    @staticmethod
    def _looks_like_path(content_or_path: Union[str, Path], treat_as_text: bool = False) -> bool:
        """Argüman var olan bir dosya yolu mu, yoksa netlist metni mi? (`treat_as_text` her zaman metin der.)"""
        if treat_as_text:
            return False
        if isinstance(content_or_path, Path):
            return True
        return (
            isinstance(content_or_path, str)
            and "\n" not in content_or_path
            and "\r" not in content_or_path
            and os.path.isfile(content_or_path)
        )

    def _read_source(self, content_or_path: Union[str, Path], treat_as_text: bool = False) -> str:
        """Netlist metnini döner: argüman bir dosya yolu ise dosyayı okur, değilse metnin kendisidir."""
        if self._looks_like_path(content_or_path, treat_as_text):
            return _read_circuit_file(Path(content_or_path))[0]
        return str(content_or_path)

    @staticmethod
    def _kill_process_tree(process: "subprocess.Popen[str]") -> None:
        """Yalnızca verilen süreci (ve çocuklarını) sonlandırır; diğer LTspice pencerelerine dokunmaz."""
        try:
            if _IS_WINDOWS:  # testler POSIX yolunu bu bayrakla sınar (sys.platform'la değil)
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    capture_output=True,
                    check=False,
                )
            else:
                # Süreç start_new_session ile kendi grubunda başlatıldı: yalnızca o grup sonlandırılır.
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)  # type: ignore[attr-defined,unused-ignore]
        except Exception:
            pass
        try:
            process.kill()
        except Exception:
            pass
        try:
            process.communicate(timeout=5)
        except Exception:
            pass

    def _prepare_netlist(
        self,
        content_or_path: Union[str, Path],
        circuit_name: str,
        parameters: Optional[Dict[str, Any]] = None,
        treat_as_text: bool = False,
        save_signals: Optional[Sequence[str]] = None,
        extra_directives: Optional[Sequence[str]] = None,
    ) -> Tuple[Path, Optional[ParameterApplication]]:
        """Netlist içeriğini veya dosyasını hazır hale getirir; dosya yolu ile parametre uygulama sonucunu döner."""
        is_path = self._looks_like_path(content_or_path, treat_as_text)
        is_asc = False
        asc_encoding: Optional[str] = None
        if is_path:
            source_path = Path(content_or_path)
            is_asc = source_path.suffix.lower() == ".asc"
            content, asc_encoding = _read_circuit_file(source_path)
        else:
            content = str(content_or_path)
            is_asc = content.lstrip().startswith("Version ") and "SHEET " in content
            if not is_asc:
                content = _normalize_newlines(content)

        # Parametreleri akıllıca güncelle veya ekle (duplicate .param önlenir, eleman değerleri de desteklenir)
        param_app: Optional[ParameterApplication] = None
        if parameters:
            param_app = apply_parameters(content, parameters)
            content = param_app.content

        for directive in extra_directives or ():
            content = _add_directive(content, directive)

        # Yalnızca istenen sinyalleri kaydet: .raw dosyasını ve ayrıştırma süresini küçültür
        if save_signals:
            content, _ = _apply_save_directive(content, save_signals)

        # Önceki koşumdan dosyada kalmış otomatik threads satırı: run_netlist güncel bütçeyle yeniden yazar
        content = strip_auto_directives(content)

        ext = ".asc" if is_asc else ".net"
        target_path = self.workspace_dir / f"{sanitize_circuit_name(circuit_name)}{ext}"
        _write_circuit_file(target_path, content, asc_encoding)

        return target_path, param_app

    def open_in_gui(self, circuit_name_or_path: Union[str, Path]) -> subprocess.Popen:
        """
        Belirtilen devreyi (.asc şema veya .net) LTspice grafik arayüzünde (GUI) açar.
        Kullanıcının devreyi görsel olarak incelemesini, prob ile tıklamasını ve UI üzerinden
        koşturmasını sağlar. Arka planda non-blocking olarak LTspice'ı başlatır.
        """
        path = Path(circuit_name_or_path)
        if not path.is_file():
            candidates = [
                self.workspace_dir / f"{sanitize_circuit_name(circuit_name_or_path)}.asc",
                self.workspace_dir / f"{sanitize_circuit_name(circuit_name_or_path)}.net",
                Path("circuits") / f"{circuit_name_or_path}.asc",
                Path("circuits") / f"{circuit_name_or_path}",
            ]
            for cand in candidates:
                if cand.is_file():
                    path = cand
                    break
        if not path.is_file():
            raise FileNotFoundError(f"Açılacak devre dosyası bulunamadı: {circuit_name_or_path}")

        cmd = [str(self.executable_path), str(path.resolve())]
        logger.info("LTspice GUI başlatılıyor: %s", cmd)
        return subprocess.Popen(cmd)

    _version_cache: Dict[str, Optional[str]] = {}

    def ltspice_version(self) -> Optional[str]:
        """
        LTspice sürümü (``LTspice.exe -version`` -> ``"26.1.1"``); okunamazsa None. Süreç başına önbelleklenir.

        Not: ``-v`` kısaltması sürüm basmaz, arayüzü açıp bekler; kullanmayın.
        """
        key = str(self.executable_path)
        if key not in self._version_cache:
            version: Optional[str] = None
            try:
                proc = subprocess.run(
                    [key, "-version"], capture_output=True, text=True, errors="replace", timeout=30
                )
                text = (proc.stdout or "").strip()
                version = text.splitlines()[0].strip() if text else None
            except (OSError, subprocess.SubprocessError):
                version = None
            self._version_cache[key] = version
        return self._version_cache[key]

    def schematic_to_netlist(
        self,
        asc_source: Union[str, Path],
        circuit_name: str = "schematic",
        treat_as_text: bool = False,
        timeout: Optional[float] = None,
    ) -> str:
        """
        Bir ``.asc`` şemayı LTspice'ın kendi netlisteriyle (``LTspice.exe -netlist``, başsız, ~0,5 sn) metin
        netliste çevirir ve netlist metnini döner.

        Dosya yolu verilirse şemanın bir kopyası **aynı klasörde** geçici adla çevrilir ve hemen silinir: yerel
        semboller (.asy) ve göreli ``.include`` yolları ancak böyle çözülür. (LTspice 26.1.1'de belgelenen
        ``-I<yol>`` arama yolu anahtarı çalışmıyor; argümanı dosya adı sanıyor.) Klasöre yazılamıyorsa kopya
        çalışma alanında çevrilir. Metin verilirse çalışma alanında çevrilir; yalnızca standart semboller çözülür.
        Kullanıcının kendi ``<ad>.net`` dosyasının üzerine yazılmaz.

        :raises RuntimeError: LTspice netlist üretemezse (örn. bulunamayan sembol).
        """
        run_timeout = timeout or max(30.0, float(self.default_timeout))
        if self._looks_like_path(asc_source, treat_as_text):
            src = Path(asc_source)
            data = src.read_bytes()
            stem = src.stem
            candidates = [src.parent, self.workspace_dir / "_netlist"]
        else:
            text = str(asc_source)
            if not _is_asc_text(text):
                raise ValueError("schematic_to_netlist bir .asc şema bekliyor ('Version 4 / SHEET ...').")
            data = encode_asc_text(text)
            stem = sanitize_circuit_name(circuit_name)
            candidates = [self.workspace_dir / "_netlist"]

        last_error = ""
        for folder in candidates:
            tmp_asc = folder / f"_ltspice_llm_{stem}_{os.getpid()}_{threading.get_ident()}.asc"
            tmp_net = tmp_asc.with_suffix(".net")
            try:
                folder.mkdir(parents=True, exist_ok=True)
                tmp_asc.write_bytes(data)
            except OSError as ex:
                last_error = f"{folder} klasörüne yazılamadı: {ex}"
                continue
            try:
                proc = subprocess.run(
                    [str(self.executable_path), "-netlist", str(tmp_asc)],
                    capture_output=True, text=True, errors="replace", timeout=run_timeout,
                )
                if tmp_net.exists():
                    netlist = LogParser._decode_log_bytes(tmp_net.read_bytes())
                    lines = netlist.splitlines()
                    if lines and lines[0].startswith("*"):
                        lines[0] = f"* {stem}.asc"  # geçici dosya yolu yerine şemanın adı
                    return "\n".join(lines) + "\n"
                last_error = (
                    f"LTspice netlist üretmedi (rc={proc.returncode}). {(proc.stdout or '').strip()} "
                    "Olası neden: şemadaki bir sembol bulunamadı (yerel .asy dosyaları şemayla aynı klasörde olmalı)."
                )
            except subprocess.TimeoutExpired:
                last_error = f"LTspice -netlist {run_timeout:g} sn içinde bitmedi."
            finally:
                for p in (tmp_asc, tmp_net, tmp_asc.with_suffix(".log")):
                    try:
                        p.unlink()
                    except OSError:
                        pass
        raise RuntimeError(f"Şema netliste çevrilemedi: {last_error}")

    #: LTspice netlisterının bağlantısız pinlere verdiği ad ("I1 NC_01 NC_02 1m")
    _NC_NET_RE = re.compile(r"^NC_\d+$", re.IGNORECASE)
    #: Netlisterın adsız ağlara verdiği ad ("N001")
    _AUTO_NET_RE = re.compile(r"^N\d{3,}$")

    def inspect_schematic(self, asc_source: Union[str, Path], treat_as_text: bool = False) -> Dict[str, Any]:
        """
        Bir şemanın bağlantısını LTspice'ın kendi netlisteriyle doğrular (koordinat hesabına güvenmeden).

        ADI, yapay zekânın konum hesabıyla ürettiği şemaların hizasız pin ve kopuk bağlantıya çok açık olduğunu
        belirtir. Netlister hiçbir yere değmeyen pinlere ``NC_xx`` adını verir; tek bir pine bağlı adsız
        (``N001``) ağlar da açık uçtur. Dönüş::

            {"status": "SUCCESS" | "WARNING", "netlist": "...",
             "unconnected_pins": [{"element": "I1", "pin": 1, "pin_name": "+", "net": "NC_01"}, ...],
             "dangling_nets": [{"net": "N003", "element": "R2"}], "message": "..."}
        """
        netlist = self.schematic_to_netlist(asc_source, treat_as_text=treat_as_text)
        unconnected: List[Dict[str, Any]] = []
        auto_net_users: Dict[str, List[str]] = {}
        for idx, line in enumerate(netlist.splitlines()):
            stripped = line.strip()
            if idx == 0 or not stripped or stripped[0] in "*;.+":
                continue
            code, _, comment = stripped.partition(";")
            pin_names: List[str] = []
            if comment.startswith("§pnba"):
                pin_names = comment[len("§pnba"):].strip().split(")")
            tokens = code.split()
            element = tokens[0]
            for pos, token in enumerate(tokens[1:], start=1):
                if self._NC_NET_RE.match(token):
                    entry: Dict[str, Any] = {"element": element, "pin": pos, "net": token}
                    if pos <= len(pin_names) and pin_names[pos - 1]:
                        entry["pin_name"] = pin_names[pos - 1]
                    unconnected.append(entry)
                elif self._AUTO_NET_RE.match(token):
                    auto_net_users.setdefault(token, []).append(element)
        dangling = [
            {"net": net, "element": users[0]} for net, users in sorted(auto_net_users.items()) if len(users) == 1
        ]
        problems = len(unconnected) + len(dangling)
        return {
            "status": "WARNING" if problems else "SUCCESS",
            "netlist": netlist,
            "unconnected_pins": unconnected,
            "dangling_nets": dangling,
            "message": (
                f"{len(unconnected)} bağlantısız pin, {len(dangling)} açık uçlu ağ bulundu; şemayı LTspice'ta "
                "açıp tel uçlarının pin koordinatlarına tam oturduğunu kontrol edin."
                if problems else "Tüm pinler bağlı (LTspice netlisterı)."
            ),
        }

    def _prepare_netlist_file(
        self,
        content_or_path: Union[str, Path],
        circuit_name: str,
        parameters: Optional[Dict[str, Any]] = None,
        treat_as_text: bool = False,
    ) -> Path:
        """Netlist içeriğini veya dosyasını hazır hale getirir ve çalışma alanındaki dosya yolunu döner."""
        return self._prepare_netlist(content_or_path, circuit_name, parameters, treat_as_text=treat_as_text)[0]

    def remove_circuit(self, circuit_name: str) -> List[str]:
        """
        Bir devrenin çalışma alanındaki dosyalarını (.net/.log/.raw/.db/.png, .op.raw ve _bode.png) siler.
        Silinen dosya adlarını döner; yalnızca bu adaptörün ``workspace_dir`` dizinine dokunur.
        """
        stem = sanitize_circuit_name(circuit_name)
        removed: List[str] = []
        names = [f"{stem}{suffix}" for suffix in _ARTIFACT_SUFFIXES] + [f"{stem}_bode.png", f"{stem}.op.raw"]
        for file_name in names:
            path = self.workspace_dir / file_name
            if path.is_file():
                try:
                    path.unlink()
                    removed.append(file_name)
                except OSError as ex:
                    logger.warning("'%s' silinemedi: %s", path, ex)
        return removed

    def cleanup_workspace(
        self, max_age_seconds: Optional[float] = None, max_total_mb: Optional[float] = None
    ) -> int:
        """
        Çalışma alanındaki simülasyon çıktılarını (.net/.asc/.log/.raw/.db/.png) siler; silinen dosya sayısını döner.

        `run_batch`'in alt dizinleri (``_batch_*``) de taranır, boşalanlar silinir. Başka alt dizinlere ve başka
        uzantılara dokunulmaz. Ölçüm (2026-10-06): varsayılan çalışma alanı 9 günde 3,16 GB; 1,82 GB'ı ``_batch_*``
        dizinlerindeydi ve eski sürüm alt dizinlere hiç girmiyordu.

        :param max_age_seconds: Bu kadar saniyeden eski dosyalar silinir (hepsi için ``0``).
        :param max_total_mb: Kota. Toplam bunun üstündeyse en eski koşumlar (aynı dizin ve addaki dosyalar birlikte)
            toplam sınırın altına inene kadar silinir. Hâlâ elde tutulan bir `SimulationResult`'ın dosyaları
            silinmez; bu yüzden toplam sınırın üstünde kalabilir. Başka bir Python sürecinin elindeki sonuçlar
            görülmez: aynı çalışma alanını paylaşan süreçler kotayı dikkatle kullanmalı.
        """
        if max_age_seconds is None and max_total_mb is None:
            raise ValueError("cleanup_workspace: max_age_seconds ya da max_total_mb verin.")
        removed = 0
        if max_age_seconds is not None:
            now = time.time()
            for path, st in self._workspace_artifacts():
                if now - st.st_mtime >= max_age_seconds and self._unlink_artifact(path):
                    removed += 1
        if max_total_mb is not None:
            removed += self._enforce_quota(float(max_total_mb) * 1e6)
        for folder in self._artifact_dirs()[1:]:
            try:
                folder.rmdir()  # yalnızca boşsa silinir
            except OSError:
                pass
        return removed

    def _artifact_dirs(self) -> List[Path]:
        """Çalışma alanı ve adaptörün kendi `run_batch` alt dizinleri."""
        try:
            batch = sorted(p for p in self.workspace_dir.iterdir() if p.is_dir() and p.name.startswith(_BATCH_DIR_PREFIX))
        except OSError:
            batch = []
        return [self.workspace_dir, *batch]

    def _workspace_artifacts(self) -> List[Tuple[Path, os.stat_result]]:
        found: List[Tuple[Path, os.stat_result]] = []
        for folder in self._artifact_dirs():
            try:
                with os.scandir(folder) as entries:
                    for entry in entries:
                        if entry.is_file() and os.path.splitext(entry.name)[1].lower() in _ARTIFACT_SUFFIXES:
                            found.append((Path(entry.path), entry.stat()))
            except OSError:
                continue
        return found

    @staticmethod
    def _unlink_artifact(path: Path) -> bool:
        try:
            path.unlink()
            return True
        except OSError as ex:
            logger.warning("'%s' silinemedi: %s", path, ex)
            return False

    def _enforce_quota(self, limit_bytes: float) -> int:
        """En eski koşumları (dizin + ilk noktaya kadarki ad: ``x.raw``, ``x.op.raw``, ``x.log``, ``x_bode.png``) siler."""
        files = self._workspace_artifacts()
        total = sum(st.st_size for _, st in files)
        if total <= limit_bytes:
            return 0
        live = _live_runs()
        runs: Dict[Tuple[str, str], List[Tuple[Path, os.stat_result, bool]]] = {}
        resolved: Dict[Path, str] = {}
        for path, st in files:
            if path.parent not in resolved:
                resolved[path.parent] = os.path.normcase(str(path.parent.resolve()))
            names = _run_names(path.name)
            held = any((resolved[path.parent], name) in live for name in names)
            runs.setdefault((resolved[path.parent], names[0]), []).append((path, st, held))
        removed = 0
        for members in sorted(runs.values(), key=lambda run: max(st.st_mtime for _, st, _ in run)):
            if total <= limit_bytes:
                break
            for path, st, held in members:
                if not held and self._unlink_artifact(path):
                    total -= st.st_size
                    removed += 1
        return removed
