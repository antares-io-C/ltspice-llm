"""
CPU Guard
~~~~~~~~~
Eşzamanlı LTspice örneklerinin makineyi kilitlemesini önleyen makine geneli çözücü iş parçacığı bütçesi.

Neden gerekli? LTspice 26'nın çözücüsü, süreç affinity'sini ve makinedeki diğer yükü yok sayarak **tüm
mantıksal çekirdekler kadar** iş parçacığı açar ("Maximum thread count: 12"). Birden fazla örnek aynı anda
çalışıp bu iş parçacıkları çekirdek bulamayınca çözücü CPU yakarak ilerlemeden döner (livelock): ölçümde
8 eşzamanlı örnekte 24 işin 23'ü takıldı. Her koşuma ``.options threads=N`` verip tüm koşumların N
toplamını çekirdek sayısıyla sınırlamak sorunu ortadan kaldırıyor (aynı yük 24/24 başarılı).

Bütçe, her çekirdek için bir kilit dosyasıyla (``slot_000.lock`` ...) tutulur. Böylece sınır aynı süreçteki
iş parçacıkları arasında da, **farklı Python süreçleri ve ajanlar arasında** da geçerlidir. Kilitler işletim
sistemi düzeyindedir: bir süreç çökse ya da öldürülse bile kilidi kendiliğinden bırakılır, bütçe asılı kalmaz.

Makineye göre ayar:

- Kullanılabilir çekirdek sayısı sürecin affinity maskesinden okunur (`available_cpus`).
- ``LTSPICE_LLM_CPU_BUDGET`` ortam değişkeni bu yazılımın LTspice'a ayıracağı toplam çekirdeği kısar
  (ör. diğer uygulamalara çekirdek bırakmak için).
- ``LTSPICE_LLM_SLOT_DIR`` kilit dosyalarının dizinini değiştirir (varsayılan: sistem geçici dizini).
"""

from __future__ import annotations

import logging
import os
import random
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)

#: Bu yazılımın tüm LTspice koşumlarına ayıracağı toplam çekirdek (çözücü iş parçacığı) üst sınırı
ENV_CPU_BUDGET = "LTSPICE_LLM_CPU_BUDGET"
#: Makine geneli kilit dosyalarının dizini
ENV_SLOT_DIR = "LTSPICE_LLM_SLOT_DIR"

_IS_WINDOWS = sys.platform == "win32"


def _env_budget() -> Optional[int]:
    raw = os.environ.get(ENV_CPU_BUDGET, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r bir tam sayı değil; yok sayıldı.", ENV_CPU_BUDGET, raw)
        return None
    if value < 1:
        logger.warning("%s=%r en az 1 olmalı; yok sayıldı.", ENV_CPU_BUDGET, raw)
        return None
    return value


def _windows_affinity_count() -> Optional[int]:
    """Windows'ta sürecin affinity maskesindeki işlemci sayısı (``os.cpu_count`` affinity'yi yok sayar)."""
    if sys.platform != "win32":  # mypy platformu bu denetimden anlar
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessAffinityMask.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t),
        ]
        kernel32.GetProcessAffinityMask.restype = wintypes.BOOL
        process_mask, system_mask = ctypes.c_size_t(0), ctypes.c_size_t(0)
        if not kernel32.GetProcessAffinityMask(
            kernel32.GetCurrentProcess(), ctypes.byref(process_mask), ctypes.byref(system_mask)
        ):
            return None
        # Birden fazla işlemci grubuna yayılan süreçlerde maske 0 döner: bilinmiyor say
        return bin(process_mask.value).count("1") or None
    except Exception:
        return None


def _affinity_count() -> Optional[int]:
    if _IS_WINDOWS:
        return _windows_affinity_count()
    if hasattr(os, "sched_getaffinity"):
        try:
            return len(os.sched_getaffinity(0)) or None
        except OSError:
            return None
    return None


def machine_cpus() -> int:
    """Makinedeki mantıksal işlemci sayısı; ``LTSPICE_LLM_CPU_BUDGET`` verilmişse onunla kısılır."""
    count = os.cpu_count() or 1
    cap = _env_budget()
    return max(1, min(count, cap) if cap else count)


def ltspice_thread_limit() -> int:
    """
    LTspice'ın açabileceği en çok çözücü iş parçacığı: makinedeki mantıksal işlemci sayısı. LTspice 26.1.1 daha büyük
    bir ``.options threads`` değerini buna kırpar (ölçüm: examples/12, 4 mantıksal çekirdekte ``threads=6`` -> 4);
    affinity'yi ve ``LTSPICE_LLM_CPU_BUDGET``'ı bilmez.
    """
    return os.cpu_count() or 1


def available_cpus() -> int:
    """
    Bu sürecin LTspice'a ayırabileceği mantıksal işlemci sayısı.

    Sürecin affinity maskesi (ör. ``start /affinity``, CI kısıtları, Process Lasso) ve
    ``LTSPICE_LLM_CPU_BUDGET`` hesaba katılır. LTspice'ın kendisi affinity'yi yok sayıp her zaman
    makinedeki tüm işlemciler kadar iş parçacığı açtığı için bu sayı ona ``.options threads`` ile verilir.
    """
    count = _affinity_count() or os.cpu_count() or 1
    return max(1, min(count, machine_cpus()))


class CpuSlotTimeout(TimeoutError):
    """İstenen çekirdek yuvası bekleme süresi içinde boşalmadı."""


def _try_lock(path: Path) -> Optional[int]:
    """Kilit dosyasını engellemeden kilitlemeyi dener; başarılıysa dosya tanıtıcısını döner."""
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o666)
    try:
        if sys.platform == "win32":  # mypy platformu bu denetimden anlar (_IS_WINDOWS'tan değil)
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        os.close(fd)
        return None


def _unlock(fd: int) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # kapatmadan önce: Windows kapanışta gecikmeli bırakabilir
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


class CpuLease:
    """Alınmış çekirdek yuvaları. ``threads`` kadar çözücü iş parçacığı kullanılabilir; iş bitince bırakın."""

    def __init__(
        self, fds: List[int], waited_seconds: float = 0.0, gate: Optional[Callable[[], Optional[int]]] = None
    ):
        self._fds = list(fds)
        self.threads = len(self._fds)
        self.waited_seconds = waited_seconds
        self._lock = threading.Lock()
        self._gate = gate

    def release(self) -> None:
        self._release(self._gate)

    def _release(self, gate: Optional[Callable[[], Optional[int]]]) -> None:
        with self._lock:
            fds, self._fds = self._fds, []
        if not fds:
            return
        # Yuvalar tek tek bırakılır; kapı tutulmazsa başka bir alıcı yarım bırakılmış kirayı görüp payından az
        # yuvayla döner (tam test paketinde yük altında 3 yerine 2).
        gate_fd = gate() if gate else None
        try:
            for fd in fds:
                _unlock(fd)
        finally:
            if gate_fd is not None:
                _unlock(gate_fd)

    @property
    def released(self) -> bool:
        return not self._fds

    def __enter__(self) -> "CpuLease":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def __del__(self) -> None:  # unutulan kira bütçeyi kalıcı olarak kilitlemesin
        try:
            # Kapısız: çöp toplayıcı aynı iş parçacığı kapıyı tutarken çalışabilir, kapıyı beklemek onu kilitler
            self._release(None)
        except Exception:
            pass


class CpuSlotPool:
    """
    Makine geneli çekirdek bütçesi: ``size`` adet kilit dosyası = ``size`` çözücü iş parçacığı.

    `acquire` boş yuvalardan en fazla ``want`` tanesini alır; en az ``minimum`` tanesi boşalana kadar bekler.
    Tek başına çalışan bir koşum tüm makineyi alır (LTspice varsayılanıyla aynı hız); aynı anda başlayan
    koşumlar çekirdekleri paylaşır; bütçe doluysa yeni koşum aşırı abone etmek yerine sırasını bekler.
    """

    def __init__(self, size: Optional[int] = None, directory: Optional[Union[str, Path]] = None):
        self.size = max(1, int(size)) if size is not None else machine_cpus()
        self.directory = Path(
            directory or os.environ.get(ENV_SLOT_DIR) or Path(tempfile.gettempdir()) / "ltspice_llm_cpu_slots"
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        self._local = threading.Lock()

    #: Kapı kilidi bu kadar saniyede alınamazsa kapısız devam edilir (kapı normalde milisaniyeden kısa tutulur)
    _GATE_TIMEOUT = 2.0

    def _slot_path(self, index: int) -> Path:
        return self.directory / f"slot_{index:03d}.lock"

    def _enter_gate(self) -> Optional[int]:
        """Yuva alma ile kira bırakmayı süreçler ve iş parçacıkları arasında sıraya koyan kapı kilidi."""
        path = self.directory / "gate.lock"
        deadline = time.monotonic() + self._GATE_TIMEOUT
        while True:
            fd = _try_lock(path)
            if fd is not None:
                return fd
            if time.monotonic() >= deadline:
                logger.warning("Çekirdek bütçesinin kapı kilidi %g sn'de alınamadı; kapısız devam ediliyor.",
                               self._GATE_TIMEOUT)
                return None
            time.sleep(0.001)

    def _try_take(self, want: int) -> List[int]:
        held: List[int] = []
        with self._local:
            gate = self._enter_gate()
            try:
                start = random.randrange(self.size)  # süreçler aynı yuvalar için yarışmasın
                for k in range(self.size):
                    if len(held) >= want:
                        break
                    fd = _try_lock(self._slot_path((start + k) % self.size))
                    if fd is not None:
                        held.append(fd)
            finally:
                if gate is not None:
                    _unlock(gate)
        return held

    def free_slots(self) -> int:
        """Şu an boş yuva sayısı (anlık görüntü; bilgi amaçlı)."""
        held = self._try_take(self.size)
        count = len(held)
        for fd in held:
            _unlock(fd)
        return count

    def acquire(
        self,
        want: int,
        minimum: int = 1,
        wait_timeout: Optional[float] = 60.0,
        contended_want: Optional[int] = None,
    ) -> CpuLease:
        """
        En az ``minimum``, en çok ``want`` yuva alır. ``wait_timeout`` saniye içinde ``minimum`` yuva
        boşalmazsa `CpuSlotTimeout` fırlatır (``None``: süresiz bekler).

        ``contended_want``: ilk deneme başarısız olduysa (bütçe başkalarınca kullanılıyor) bundan sonra en
        çok bu kadar yuva istenir. Böylece tek başına koşum tüm makineyi alırken, sıra bekleyen koşumlar
        makineyi tek tek kapmak yerine paylaşır ve eşzamanlı ilerler.
        """
        want = max(1, min(int(want), self.size))
        minimum = max(1, min(int(minimum), want))
        shared = want if contended_want is None else max(minimum, min(want, int(contended_want)))
        started = time.monotonic()
        delay = 0.02
        take = want
        while True:
            held = self._try_take(take)
            if len(held) >= minimum:
                return CpuLease(held, waited_seconds=time.monotonic() - started, gate=self._enter_gate)
            for fd in held:
                _unlock(fd)
            waited = time.monotonic() - started
            if wait_timeout is not None and waited >= wait_timeout:
                raise CpuSlotTimeout(
                    f"{wait_timeout:g} sn içinde {minimum} çekirdek yuvası boşalmadı "
                    f"(bütçe {self.size}, dizin {self.directory})."
                )
            take = shared
            time.sleep(delay + random.uniform(0, delay))
            delay = min(delay * 2, 0.5)


_POOLS: Dict[Tuple[str, int], CpuSlotPool] = {}
_POOLS_LOCK = threading.Lock()


def default_pool() -> CpuSlotPool:
    """Geçerli ortam ayarlarına (``LTSPICE_LLM_SLOT_DIR``, ``LTSPICE_LLM_CPU_BUDGET``) göre ortak havuz."""
    directory = os.environ.get(ENV_SLOT_DIR) or str(Path(tempfile.gettempdir()) / "ltspice_llm_cpu_slots")
    size = machine_cpus()
    key = (os.path.normcase(os.path.abspath(directory)), size)
    with _POOLS_LOCK:
        pool = _POOLS.get(key)
        if pool is None:
            pool = _POOLS[key] = CpuSlotPool(size=size, directory=directory)
        return pool
