"""
Autonomous Circuit Optimizer & Closed-Loop Agent
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
LTspice simülasyonu ile hedef kriterler arasında otomatik kapalı devre
optimizasyon (closed-loop tuning) yürüten modül.
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .adapter import LTSpiceAdapter
from .netlist import measured_signals

logger = logging.getLogger(__name__)


@dataclass
class OptimizationStep:
    iteration: int
    parameters: Dict[str, Any]
    measurements: Dict[str, Any]
    error: float
    elapsed: float


@dataclass
class OptimizationResult:
    target_metric: str
    target_value: float
    best_parameters: Dict[str, Any]
    best_value: float
    final_error: float
    total_iterations: int
    converged: bool
    history: List[OptimizationStep] = field(default_factory=list)
    message: str = ""  # yakınsamadıysa nedeni (LLM'in bir sonraki adımı seçebilmesi için)
    target_metrics: Optional[Dict[str, float]] = None

    def to_llm_dict(self) -> Dict[str, Any]:
        """LLM context penceresi için sade JSON: özet ve (parametre, ölçüm, hata) geçmişi."""
        d = {
            "status": "SUCCESS" if self.converged else "NOT_CONVERGED",
            "converged": self.converged,
            "target_metric": self.target_metric,
            "target_value": self.target_value,
            "best_parameters": self.best_parameters,
            "best_value": self.best_value,
            "final_error": self.final_error,
            "total_iterations": self.total_iterations,
            "message": self.message,
            "history": [
                {
                    "iteration": s.iteration,
                    "parameters": s.parameters,
                    "value": s.measurements.get(self.target_metric) if not self.target_metrics else s.measurements,
                    "error": s.error,
                }
                for s in self.history
            ],
        }
        if self.target_metrics:
            d["target_metrics"] = self.target_metrics
        return d


def _error_ratio(err: float, target: float) -> float:
    """
    Toleransla karşılaştırılan hata: bağıl; yalnızca hedef tam 0 iken (bağıl hata tanımsız) mutlak. Büyüklük eşiği
    yok: 2 µs, 10 nA gibi küçük hedefler de bağıl ölçülür. Eski 1e-5 eşiği onları mutlak hatayla hep "yakınsamış"
    sayıyordu; çok parametreli yolda düzeltilmiş, tek parametreli yolda kalmıştı (5 µs hedefi 1 µs'de, 2026-10-11).
    """
    if target == 0:
        return abs(err)
    return abs(err) / abs(target)


def _metric_error(actual: float, target: float) -> float:
    """Ölçülen değerin hedefe göre hatası (bkz. `_error_ratio`)."""
    return _error_ratio(actual - target, target)


class CircuitOptimizer:
    """
    Belirli bir hedefe (.meas metriğine) ulaşmak için devre parametrelerini
    otomatik olarak optimize eden akıllı döngü.
    """

    def __init__(self, adapter: Optional[LTSpiceAdapter] = None):
        self.adapter = adapter or LTSpiceAdapter()

    def optimize_single_parameter(
        self,
        base_netlist: str,
        circuit_name: str,
        param_name: str,
        target_meas_name: str,
        target_value: float,
        min_param: float,
        max_param: float,
        tolerance: float = 0.01,
        max_iterations: int = 15,
        geometric: Optional[bool] = None,
        cleanup_intermediate: bool = True,
        prune_signals: bool = True,
    ) -> OptimizationResult:
        """
        Tek bir parametreyi (örn. R1 veya C1) hedeflenen .meas değerine ulaştırmak için
        gradyan yönünü otomatik belirleyen ikili arama uygular.

        Metriğin parametreye göre monoton olduğu varsayılır. Hedef, iki uç noktadaki ölçümlerin
        arasında değilse arama yapılmadan `converged=False` döner. Simülasyon bir noktada
        başarısız olursa aynı aralıkta farklı noktalar denenir. Yakınsama sağlanamazsa
        `OptimizationResult.message` nedenini açıklar.

        :param geometric: True ise orta nokta geometrik ortalama ile seçilir (çok dekadlık aralıklar için).
            None ise `min_param > 0` ve `max_param / min_param >= 100` olduğunda otomatik açılır.
        :param cleanup_intermediate: True ise her adım için üretilen geçici devre dosyaları
            (.net/.log/.raw) iş bitince silinir. Her çalıştırma benzersiz adlar kullandığı için
            eşzamanlı optimizasyonlar birbirinin dosyalarını ezmez.
        :param prune_signals: True ise her iterasyonda netliste ``.save`` eklenerek yalnızca ``.meas``
            satırlarında geçen sinyaller kaydedilir. Optimizasyon dalga formuna değil ölçüme baktığı
            için bu, sonucu değiştirmeden ``.raw`` boyutunu ve disk/ayrıştırma süresini düşürür
            (ölçümde 6,8 MB -> 2,7 MB). Netlistte zaten ``.save`` varsa dokunulmaz; ``.meas`` yoksa
            etkisizdir. Ölçümler beklenmedik biçimde üretilemezse False verin.

        Not: Bu ikili arama **sırasal**dır - her adım bir öncekinin sonucuna bağlıdır, dolayısıyla
        iterasyonlar paralelleştirilemez. Bağımsız noktaları aynı anda denemek gerekiyorsa
        `LTSpiceAdapter.run_sweep` (tek süreçte ``.step`` taraması) kullanın.
        """
        prune = measured_signals(base_netlist) if prune_signals else []
        history: List[OptimizationStep] = []
        best_params: Dict[str, Any] = {}
        best_val: float = 0.0
        min_abs_error: float = float("inf")
        converged: bool = False
        run_token = uuid.uuid4().hex[:6]
        created_circuits: List[str] = []
        param_notes: List[str] = []

        def check_tolerance(err: float) -> bool:
            return _error_ratio(err, target_value) <= tolerance

        # 1. Başlangıçta yönü tespit et (min_param ve max_param noktalarını test et)
        def eval_point(val: float, step_num: int) -> Optional[float]:
            nonlocal min_abs_error, best_params, best_val
            t0 = time.time()
            step_circuit = f"{circuit_name}_opt_{run_token}_{step_num}"
            created_circuits.append(step_circuit)
            sim_res = self.adapter.run_netlist(
                netlist_content_or_path=base_netlist,
                circuit_name=step_circuit,
                parameters={param_name: val},
                summarize_raw=False,
                save_signals=prune or None,
            )
            step_elapsed = time.time() - t0

            for w in getattr(sim_res, "warnings", None) or []:
                if f"'{param_name}'" in w and w not in param_notes:
                    param_notes.append(w)

            if not sim_res.success or target_meas_name not in sim_res.measurements:
                return None

            try:
                actual = float(sim_res.measurements[target_meas_name])
            except (ValueError, TypeError):
                return None

            err = actual - target_value
            history.append(
                OptimizationStep(
                    iteration=step_num,
                    parameters={param_name: val},
                    measurements=sim_res.measurements.copy(),
                    error=err,
                    elapsed=step_elapsed,
                )
            )

            if abs(err) < min_abs_error:
                min_abs_error = abs(err)
                best_params = {param_name: val}
                best_val = actual

            return actual

        def build_result(is_converged: bool, message: str = "") -> OptimizationResult:
            if is_converged:
                message = ""
            elif param_notes:
                message = f"{message} {' '.join(param_notes)}".strip()
            return OptimizationResult(
                target_metric=target_meas_name,
                target_value=target_value,
                best_parameters=best_params,
                best_value=best_val,
                final_error=min_abs_error,
                total_iterations=len(history),
                converged=is_converged,
                history=history,
                message=message,
            )

        try:
            v_low = eval_point(min_param, 1)
            v_high = eval_point(max_param, 2)

            if v_low is None or v_high is None:
                return build_result(
                    False,
                    f"Aralığın uç noktalarından en az birinde simülasyon başarısız oldu veya "
                    f"'{target_meas_name}' ölçümü üretilemedi ({param_name}={min_param:g} ve {max_param:g}).",
                )

            # Ölçüm parametreye hiç bağlı değilse arama anlamsızdır (ör. parametre netlistte kullanılmıyor).
            if abs(v_high - v_low) <= 1e-12 * max(1.0, abs(v_low)):
                return build_result(
                    check_tolerance(min_abs_error),
                    f"'{target_meas_name}' ölçümü iki uçta da aynı ({v_low:g}): '{param_name}' parametreye "
                    "duyarsız ya da netlistte kullanılmıyor olabilir.",
                )

            # Hedef iki uç değerin arasında değilse ikili arama anlamsızdır: gereksiz simülasyon yapma.
            if not (min(v_low, v_high) <= target_value <= max(v_low, v_high)):
                return build_result(
                    check_tolerance(min_abs_error),
                    f"Hedef {target_value:g}, aralığın uçlarındaki ölçümlerin ({v_low:g} .. {v_high:g}) "
                    "arasında değil; parametre aralığını genişletin.",
                )

            # Parametre arttıkça ölçülen değer artıyor mu azalıyor mu?
            is_direct = (v_high >= v_low)

            low = min_param
            high = max_param
            if geometric is None:
                geometric = min_param > 0 and max_param / min_param >= 100

            # Bir noktada simülasyon başarısız olursa aynı aralıkta sırayla bu oranlar denenir.
            probe_fractions = (0.5, 0.35, 0.65, 0.2, 0.8)
            attempt = 0
            exhausted = False

            for i in range(3, max_iterations + 1):
                if check_tolerance(min_abs_error):
                    converged = True
                    break

                frac = probe_fractions[attempt]
                if geometric and low > 0:
                    mid = low * (high / low) ** frac
                else:
                    mid = low + (high - low) * frac
                actual = eval_point(mid, i)
                if actual is None:
                    attempt += 1
                    if attempt >= len(probe_fractions):
                        exhausted = True
                        break  # Bu aralıkta hiçbir nokta simüle edilemedi
                    continue
                attempt = 0

                err = actual - target_value
                if check_tolerance(err):
                    converged = True
                    break

                # Doğru yöne göre arama aralığını güncelle
                if is_direct:
                    if err > 0:
                        high = mid
                    else:
                        low = mid
                else:
                    if err > 0:
                        low = mid
                    else:
                        high = mid

            if check_tolerance(min_abs_error):
                converged = True

            message = ""
            if not converged:
                message = (
                    "Aralığın içinde simüle edilebilen nokta kalmadı."
                    if exhausted
                    else f"{max_iterations} iterasyonda tolerans sağlanamadı "
                    f"(en iyi mutlak hata {min_abs_error:g}); max_iterations veya tolerance değerini artırın."
                )
            return build_result(converged, message)
        finally:
            if cleanup_intermediate:
                remove = getattr(self.adapter, "remove_circuit", None)
                if callable(remove):
                    for step_circuit in created_circuits:
                        try:
                            remove(step_circuit)
                        except Exception as ex:  # temizlik hatası sonucu bozmamalı
                            logger.warning("Geçici devre '%s' silinemedi: %s", step_circuit, ex)

    def optimize_multi_parameters(
        self,
        base_netlist: str,
        circuit_name: str,
        param_bounds: Dict[str, Tuple[float, float]],
        target_metrics: Dict[str, float],
        tolerance: float = 0.02,
        max_iterations: int = 35,
        cleanup_intermediate: bool = True,
        prune_signals: bool = True,
        initial_params: Optional[Dict[str, float]] = None,
    ) -> OptimizationResult:
        """
        Birden çok parametreyi (örn: R1, C1, R2) birden çok .meas hedefine aynı anda ulaştırmak
        için Nelder-Mead simpleks optimizasyonu uygular. Çok dekadlık aralıklarda parametreler
        otomatik olarak logaritmik uzayda ölçeklenir.

        :param base_netlist: Taban SPICE netlisti.
        :param circuit_name: Devre çalışma adı.
        :param param_bounds: Parametre adları ve arama sınırları {'R1': (100, 10000), 'C1': (1n, 1u)}.
        :param target_metrics: Hedef .meas adları ve değerleri {'f_cutoff': 10000.0, 'gain': 1.0}.
        :param tolerance: Her hedef metrik için kabul edilebilir bağıl tolerans (varsayılan: 0.02 = %2).
        :param max_iterations: En fazla simülasyon iterasyonu (varsayılan: 35).
        """
        if not param_bounds:
            raise ValueError("'param_bounds' en az bir parametre içermelidir.")
        if not target_metrics:
            raise ValueError("'target_metrics' en az bir hedef metrik içermelidir.")

        param_names = list(param_bounds.keys())
        for p in param_names:
            lo, hi = param_bounds[p]
            if not lo < hi:
                raise ValueError(f"'{p}' için alt sınır ({lo}) üst sınırdan ({hi}) küçük olmalıdır.")

        prune = measured_signals(base_netlist) if prune_signals else []
        history: List[OptimizationStep] = []
        run_token = uuid.uuid4().hex[:6]
        created_circuits: List[str] = []

        # Her parametre [0, 1]'e normalize edilir (çok dekadlıysa log10 ekseninde): başlangıç simpleksi ve durma ölçütü
        # parametrenin büyüklüğünden bağımsız olur. Eski hata: scipy'nin varsayılan simpleksi x0'ın %5'i (x0 = 0 ise
        # 0.00025) olduğundan 0.1..10 log aralığında (log10 1 = 0) arama 2 simülasyonda duruyordu; mutlak
        # xatol=tolerance ve fatol=tolerance**2 de hedefe varmadan durduruyordu (ölçüldü).
        axes: List[Tuple[float, float, bool]] = []  # (alt, üst, log10 ekseni mi)
        t0: List[float] = []
        for p in param_names:
            lo, hi = param_bounds[p]
            use_log = lo > 0 and hi / lo >= 10.0
            a, b = (math.log10(lo), math.log10(hi)) if use_log else (float(lo), float(hi))
            axes.append((a, b, use_log))
            init_val = initial_params.get(p) if initial_params else None
            if init_val is not None and (not use_log or init_val > 0):
                u = math.log10(init_val) if use_log else float(init_val)
                t0.append(min(1.0, max(0.0, (u - a) / (b - a))))
            else:
                t0.append(0.5)
        # Arama sınırsız z ekseninde, t = (1 + sin z) / 2 ile her zaman [0, 1] içinde. scipy'nin sınırlı Nelder-Mead'i
        # sınırı aşan köşeyi sınıra kırpar; köşeler sınırda üst üste binince simpleks çöküp arama durur (ölçüldü: hedef
        # C = 2n iken 1n alt sınırında 6 simülasyonda durdu). Başlangıç simpleksi her eksende 0.5 rad (t'de ~0.24) adım.
        z0 = [math.asin(2.0 * t - 1.0) for t in t0]
        simplex = [list(z0)] + [[z + (0.5 if j == i else 0.0) for j, z in enumerate(z0)] for i in range(len(z0))]

        best_params: Dict[str, Any] = {}
        best_measurements: Dict[str, Any] = {}
        best_key: Tuple[float, float] = (float("inf"), float("inf"))  # (en büyük, ortalama) bağıl hata
        step_counter = 0

        def decode(z_vec) -> Dict[str, float]:
            params = {}
            for (a, b, use_log), p, z in zip(axes, param_names, z_vec, strict=True):
                t = 0.5 * (1.0 + math.sin(float(z)))
                u = a + t * (b - a)
                params[p] = 10.0 ** u if use_log else u
            return params

        def evaluate_cost(z_vec) -> float:
            nonlocal step_counter, best_key, best_params, best_measurements
            if step_counter >= max_iterations:
                return 1e6

            step_counter += 1
            curr_params = decode(z_vec)
            step_circuit = f"{circuit_name}_mopt_{run_token}_{step_counter}"
            created_circuits.append(step_circuit)

            t0 = time.time()
            sim_res = self.adapter.run_netlist(
                netlist_content_or_path=base_netlist,
                circuit_name=step_circuit,
                parameters=curr_params,
                summarize_raw=False,
                save_signals=prune or None,
            )
            elapsed = time.time() - t0

            if not sim_res.success:
                return 1e6

            # Metrik hatalarını hesapla
            err_sq_sum = 0.0
            max_rel_err = 0.0
            total_rel_err = 0.0
            all_present = True

            for m_name, t_val in target_metrics.items():
                if m_name not in sim_res.measurements:
                    all_present = False
                    break
                try:
                    act_val = float(sim_res.measurements[m_name])
                except (ValueError, TypeError):
                    all_present = False
                    break

                rel_err = _metric_error(act_val, t_val)
                max_rel_err = max(max_rel_err, rel_err)
                total_rel_err += rel_err
                err_sq_sum += rel_err ** 2

            if not all_present:
                return 1e6

            mean_rel_err = total_rel_err / len(target_metrics)
            history.append(
                OptimizationStep(
                    iteration=step_counter,
                    parameters=curr_params.copy(),
                    measurements=sim_res.measurements.copy(),
                    error=max_rel_err,
                    elapsed=elapsed,
                )
            )

            # En iyi nokta, yakınsama ölçütüyle aynı büyüklüğe (en büyük bağıl hata) göre seçilir; eşitlikte ortalama.
            # Eski hata: en iyi ortalamaya göre seçilip "converged" herhangi bir adımdan açılıyordu, yani en iyi nokta
            # toleransı aşsa da sonuç "yakınsadı" görünebiliyordu.
            if (max_rel_err, mean_rel_err) < best_key:
                best_key = (max_rel_err, mean_rel_err)
                best_params = curr_params.copy()
                best_measurements = sim_res.measurements.copy()

            return err_sq_sum

        try:
            # pyproject bağımlılığı. Yoksa ImportError görünmeli: eskiden başlangıç noktası bir kez koşulup sessizce
            # "yakınsamadı" dönülüyordu.
            import scipy.optimize
            scipy.optimize.minimize(
                evaluate_cost,
                z0,
                method="Nelder-Mead",
                # Bütçe simülasyon sayısıdır; çok küçük x/f toleransları aramanın hedefe varmadan durmasını önler
                options={"initial_simplex": simplex, "maxfev": max_iterations, "maxiter": 10 * max_iterations,
                         "xatol": 1e-9, "fatol": 1e-12},
            )

            target_repr = ", ".join(f"{k}={v:g}" for k, v in target_metrics.items())
            first_metric = next(iter(target_metrics.keys()))
            first_val = best_measurements.get(first_metric, 0.0)
            best_max_err = best_key[0]
            converged = best_max_err <= tolerance

            msg = ""
            if not converged:
                msg = (
                    f"{step_counter} iterasyonda tüm hedefler için tolerans ({tolerance:g}) sağlanamadı "
                    f"(en iyi noktada en büyük bağıl hata {best_max_err:.3%}); max_iterations veya tolerance "
                    f"değerini artırın."
                )

            return OptimizationResult(
                target_metric=target_repr,
                target_value=target_metrics[first_metric],
                best_parameters=best_params,
                best_value=first_val,
                final_error=best_max_err,
                total_iterations=len(history),
                converged=converged,
                history=history,
                message=msg,
                target_metrics=target_metrics,
            )
        finally:
            if cleanup_intermediate:
                remove = getattr(self.adapter, "remove_circuit", None)
                if callable(remove):
                    for step_circuit in created_circuits:
                        try:
                            remove(step_circuit)
                        except Exception as ex:
                            logger.warning("Geçici devre '%s' silinemedi: %s", step_circuit, ex)
