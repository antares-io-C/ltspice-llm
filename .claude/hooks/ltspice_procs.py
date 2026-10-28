"""
Hook'ların ortak yardımcıları: çalışan LTspice süreçleri ve LTspice.exe sürümü.

``tasklist`` çağrı başına ~360 ms sürdüğü için süreçler doğrudan Win32 API'siyle (ctypes) listelenir (~10 ms).
Görünür penceresi olan LTspice GUI'dir; penceresiz olanlar ``-b`` ile başlatılmış batch koşumlarıdır
(ölçüldü: ``run_netlist`` koşumu penceresiz görünür). Batch koşumun sahibi üst sürecin komut satırından
anlaşılır: içinde ``pytest`` geçiyorsa koşum test paketine aittir.
"""

import ctypes
import os
import sys
from collections import namedtuple
from ctypes import wintypes
from pathlib import Path

LTSPICE_IMAGES = {"ltspice.exe", "xviix64.exe", "scad3.exe"}
KNOWN_EXES = [
    Path(os.path.expanduser(r"~\AppData\Local\Programs\ADI\LTspice\LTspice.exe")),
    Path(r"C:\Program Files\ADI\LTspice\LTspice.exe"),
    Path(r"C:\Program Files\LTC\LTspiceXVII\XVIIx64.exe"),
    Path(r"C:\Program Files (x86)\LTC\LTspiceXVII\XVIIx64.exe"),
]

LtspiceProc = namedtuple("LtspiceProc", "pid parent_pid gui parent_cmdline")


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_void_p)]


def _kernel32():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32


def _ltspice_pids():
    """Çalışan LTspice süreçleri: ``{pid: üst_pid}``."""
    k32 = _kernel32()
    snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if snap in (None, wintypes.HANDLE(-1).value):
        return {}
    found = {}
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() in LTSPICE_IMAGES:
                found[entry.th32ProcessID] = entry.th32ParentProcessID
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return found


def _pids_with_visible_window(pids):
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    found = set()
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids:
                found.add(pid.value)
        return True

    user32.EnumWindows(proto(cb), 0)
    return found


def process_cmdline(pid):
    """Sürecin komut satırı (NtQueryInformationProcess, ProcessCommandLineInformation); okunamazsa ''."""
    k32 = _kernel32()
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtQueryInformationProcess.argtypes = [
        wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
    ntdll.NtQueryInformationProcess.restype = ctypes.c_long
    handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        size = wintypes.ULONG(0)
        ntdll.NtQueryInformationProcess(handle, 60, None, 0, ctypes.byref(size))
        if not size.value:
            return ""
        buf = ctypes.create_string_buffer(size.value)
        if ntdll.NtQueryInformationProcess(handle, 60, ctypes.addressof(buf), size, ctypes.byref(size)) != 0:
            return ""
        us = _UNICODE_STRING.from_buffer(buf)
        return ctypes.wstring_at(us.Buffer, us.Length // 2) if us.Buffer else ""
    except OSError:
        return ""
    finally:
        k32.CloseHandle(handle)


def ltspice_processes():
    """Çalışan LTspice süreçleri (``LtspiceProc`` listesi); Windows dışında boş liste."""
    if sys.platform != "win32":
        return []
    pids = _ltspice_pids()
    if not pids:
        return []
    gui = _pids_with_visible_window(set(pids))
    return [
        LtspiceProc(pid, ppid, pid in gui, "" if pid in gui else process_cmdline(ppid))
        for pid, ppid in sorted(pids.items())
    ]


def batch_processes():
    """Penceresiz (batch, ``-b``) LTspice süreçleri."""
    return [p for p in ltspice_processes() if not p.gui]


def owned_by_pytest(proc):
    return "pytest" in proc.parent_cmdline.lower()


def describe(procs):
    return ", ".join(f"PID {p.pid} ({'pytest' if owned_by_pytest(p) else 'başka süreç'})" for p in procs)


def find_ltspice_exe():
    env = os.environ.get("LTSPICE_EXE")
    if env:
        return Path(env) if Path(env).is_file() else None
    return next((p for p in KNOWN_EXES if p.is_file()), None)


def file_version(path):
    """Windows sürüm kaynağındaki dosya sürümü (ör. ``26.1.1``); okunamazsa None."""
    try:
        ver = ctypes.WinDLL("version")
        size = ver.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(str(path), 0, size, buf):
            return None
        ptr, length = ctypes.c_void_p(), wintypes.UINT()
        if not ver.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)):
            return None
        ms, ls = ctypes.cast(ptr, ctypes.POINTER(wintypes.DWORD * 13)).contents[2:4]  # dwFileVersionMS/LS
        parts = [ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF]
        while len(parts) > 3 and parts[-1] == 0:
            parts.pop()
        return ".".join(str(p) for p in parts)
    except (OSError, AttributeError, ValueError):
        return None
