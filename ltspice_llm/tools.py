"""
LLM Tool Calling Interfaces (Function Calling)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
OpenAI, Gemini, Anthropic, Ollama ve LangChain gibi LLM ajan sistemleri için
profesyonel devre tasarım ve simülasyon araçları (Tools).

Araç sonuçlarındaki ``status`` değerleri: ``SUCCESS`` (işlem başarılı), ``FAILED`` (simülasyon çalıştı
ama başarısız oldu), ``NOT_CONVERGED`` (optimizasyon hedefe ulaşamadı), ``WARNING`` (şema kopuk pin/açık uçlu
ağ içeriyor, simülasyon yapılmadı) ve ``ERROR`` (araç çağrısı yürütülemedi: eksik/geçersiz argüman, bulunamayan
dosya, doğrulanamayan şema vb.).
"""

from __future__ import annotations

import base64
import contextvars
import copy
import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .adapter import (
    LTSpiceAdapter,
    apply_parameters,
    strip_auto_directives,
    list_element_names,
    list_param_names,
    sanitize_circuit_name,
    _set_element_value,
)
from .agent import CircuitOptimizer
from .diagnostics import SPICEDiagnostics
from .netlist import file_access_problems, insert_before_end
from .parasitics import (
    COPPER_MM_PER_OZ,
    DEFAULT_BULK_RATIO,
    analyze_hotplug_transient,
    calculate_trace_parasitics,
    get_cable_parasitics,
)
from .parser import LogParser, RawParser
from .quantizer import quantize_circuit_parameters
from .reference import ReferenceDocs, find_model_definitions
from .schematic import AscSchematic, write_asc_file
from .visualizer import WaveformVisualizer

#: `get_ltspice_tool_schemas(format=...)` için desteklenen biçimler
TOOL_SCHEMA_FORMATS = ("generic", "openai", "anthropic", "gemini")

_IMAGE_BASE64_PROPERTY = {
    "type": "boolean",
    "description": "True ise PNG görüntüsü 'image_base64' alanında Base64 olarak da döner (çok modlu LLM'ler için).",
    "default": False,
}


def _generic_tool_schemas() -> List[Dict[str, Any]]:
    """Sağlayıcıdan bağımsız ``{name, description, parameters}`` araç şemaları."""
    return [
        {
            "name": "create_or_replace_circuit",
            "description": (
                "Yeni bir devre netlisti oluşturur ve doğrudan LTspice ile simüle eder. "
                "Netlist komponentleri, kaynakları, simülasyon direktifini (.tran, .ac, .dc, .op) "
                "ve ölçüm direktiflerini (.meas) içermelidir. .step ile parametre/sıcaklık taraması yapılabilir."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "circuit_name": {
                        "type": "string",
                        "description": "Devreye verilecek kısa tanıtıcı ad (örn: 'sallen_key_filter', 'bjt_preamp').",
                    },
                    "netlist_code": {
                        "type": "string",
                        "description": "Tam SPICE netlist içeriği (* ile başlayan başlık satırı, elemanlar, analiz komutu ve .end içermeli).",
                    },
                    "parameters": {
                        "type": "object",
                        "description": "Devreye eklenecek/enjekte edilecek başlangıç .param sözlüğü (isteğe bağlı).",
                        "additionalProperties": True,
                    },
                },
                "required": ["circuit_name", "netlist_code"],
            },
        },
        {
            "name": "modify_parameters",
            "description": (
                "Var olan bir devrenin değerlerini günceller ve devreyi anında yeniden simüle ederek yeni ölçüm "
                "sonuçlarını döner. Ad, önce .param tanımı, sonra {ad} olarak kullanılan parametre, en son aynı adlı "
                "R/C/L (ve yalın V/I) elemanı olarak aranır; hiçbirine uymayan ad hata döner (sessizce yok sayılmaz)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "circuit_name": {
                        "type": "string",
                        "description": "Parametreleri güncellenecek devrenin adı.",
                    },
                    "parameters": {
                        "type": "object",
                        "description": "Yeni değerler sözlüğü (örn: {'R1': '4.7k', 'C1': '220n'}).",
                        "additionalProperties": True,
                    },
                },
                "required": ["circuit_name", "parameters"],
            },
        },
        {
            "name": "plot_transient_waveforms",
            "description": (
                "Zaman alanı (.tran) simülasyonundan belirtilen düğüm gerilimleri veya "
                "dalga formlarının grafiğini (PNG) çizer ve dosya yolunu döner. .ac analizleri için "
                "plot_bode_diagram kullanılmalıdır."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "circuit_name": {
                        "type": "string",
                        "description": "Grafiği çizilecek devrenin adı.",
                    },
                    "signals": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Çizdirilecek sinyaller (örn: ['V(in)', 'V(out)']).",
                    },
                    "title": {
                        "type": "string",
                        "description": "Grafik başlığı.",
                    },
                    "include_image_base64": _IMAGE_BASE64_PROPERTY,
                },
                "required": ["circuit_name"],
            },
        },
        {
            "name": "plot_bode_diagram",
            "description": (
                "Frekans cevabı (.ac) simülasyonundan profesyonel 2 panelli (Kazanç dB ve Faz Derece) "
                "Bode diyagramı üretir ve dosya yolunu döner. Yalnızca .ac analizi içeren devrelerde çalışır."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "circuit_name": {
                        "type": "string",
                        "description": "Bode analizi yapılacak devrenin adı.",
                    },
                    "out_node": {
                        "type": "string",
                        "description": "Çıkış sinyali adı (varsayılan: 'V(out)').",
                    },
                    "in_node": {
                        "type": "string",
                        "description": "Giriş referans sinyali adı (varsayılan: 'V(in)').",
                    },
                    "title": {
                        "type": "string",
                        "description": "Bode grafiği başlığı.",
                    },
                    "include_image_base64": _IMAGE_BASE64_PROPERTY,
                },
                "required": ["circuit_name"],
            },
        },
        {
            "name": "get_circuit_netlist",
            "description": "Daha önce kaydedilmiş bir devrenin mevcut SPICE netlist metnini okur.",
            "parameters": {
                "type": "object",
                "properties": {
                    "circuit_name": {
                        "type": "string",
                        "description": "Okunacak devrenin adı.",
                    },
                },
                "required": ["circuit_name"],
            },
        },
        {
            "name": "list_circuits",
            "description": "Çalışma alanında kayıtlı tüm devreleri ve ilişkili çıktı dosyalarını (.raw, .log, .png) listeler.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "quantize_parameters",
            "description": (
                "Devredeki teorik parametre değerlerini (örn: 15.915k, 1.414n) ticari standart E-serisi "
                "(E6, E12, E24, E96) komponent değerlerine yuvarlar (snapping) ve sapma/hata yüzdelerini hesaplar. "
                "Sonuçlar ancak modify_parameters ile uygulanıp yeniden simüle edilerek doğrulanmış olur."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "parameters": {
                        "type": "object",
                        "description": "Yuvarlanacak parametre sözlüğü (örn: {'R1': '15.915k', 'C1': '1.414n'}).",
                        "additionalProperties": True,
                    },
                    "series": {
                        "type": "string",
                        "enum": ["E6", "E12", "E24", "E96"],
                        "description": "Standart IEC serisi (varsayılan: 'E24' - %5, 'E96' - %1, 'E12' - %10).",
                        "default": "E24",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["nearest", "floor", "ceil"],
                        "description": "Yuvarlama yönü (en yakın, bir alt, bir üst).",
                        "default": "nearest",
                    },
                },
                "required": ["parameters"],
            },
        },
        {
            "name": "diagnose_circuit_issue",
            "description": (
                "Simülasyon yakınsamadığında (Singular matrix, Time step too small, Iteration limit vb.) "
                "veya hata verdiğinde log dosyasını ve devreyi derinlemesine inceler, sorunlu düğümleri tespit eder "
                "ve kanıtlanmış SPICE çözüm direktifleri (.options method=gear, cshunt, gmin) ve otomatik onarım yaması sunar."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "circuit_name": {
                        "type": "string",
                        "description": "İncelenecek devrenin adı.",
                    },
                    "error_text": {
                        "type": "string",
                        "description": "Varsa doğrudan log/hata metni (isteğe bağlı).",
                    },
                    "apply_fix": {
                        "type": "boolean",
                        "description": "Önerilen stabilizasyon direktifleri devrenin netlist dosyasına otomatik uygulansın mı? (varsayılan: false)",
                        "default": False,
                    },
                },
                "required": ["circuit_name"],
            },
        },
        {
            "name": "optimize_parameter",
            "description": (
                "Kayıtlı bir devrenin tek bir parametresini (veya R/C/L elemanının değerini), bir .meas ölçümü "
                "hedef değere ulaşana kadar ikili aramayla otomatik ayarlar. Ölçümün parametreyle monoton değiştiği "
                "varsayılır. Bulunan en iyi değer devreye uygulanmaz; kalıcı yapmak için modify_parameters çağırın."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "circuit_name": {"type": "string", "description": "Optimize edilecek kayıtlı devrenin adı."},
                    "param_name": {"type": "string", "description": "Ayarlanacak .param adı veya eleman adı (örn: 'R_VAL', 'R1')."},
                    "target_meas_name": {"type": "string", "description": "Hedeflenen .meas ölçümünün adı (örn: 'v_target')."},
                    "target_value": {"type": "number", "description": "Ölçümün ulaşması istenen değer."},
                    "min_value": {"type": "number", "description": "Parametrenin arama aralığı alt sınırı."},
                    "max_value": {"type": "number", "description": "Parametrenin arama aralığı üst sınırı."},
                    "tolerance": {
                        "type": "number",
                        "description": "Bağıl hata toleransı (varsayılan: 0.01 = %1).",
                        "default": 0.01,
                    },
                    "max_iterations": {
                        "type": "integer",
                        "description": "En fazla simülasyon sayısı (varsayılan: 15).",
                        "default": 15,
                    },
                },
                "required": [
                    "circuit_name",
                    "param_name",
                    "target_meas_name",
                    "target_value",
                    "min_value",
                    "max_value",
                ],
            },
        },
        {
            "name": "search_ltspice_reference",
            "description": (
                "LTspice kurulumuyla gelen resmî ADI LTspice AI Reference belgelerinde (komut sözdizimi, .meas "
                "örnekleri, yakınsama sorun giderme, eleman parametreleri, .asc biçimi) arama yapar ve ilgili "
                "bölümleri döner. Emin olmadığın bir sözdizimini yazmadan önce ya da bir hata mesajının çözümü için "
                "kullan; yerel belge eğitim verisiyle çelişirse belgeye güven. İsteğe bağlı 'model_name' bir "
                ".model/.subckt adının hangi LTspice kütüphane dosyasında tanımlı olduğunu da bulur."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Aranacak sözcükler (örn: '.step only one step', 'meascplxfmt polar', 'convreport').",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Dönecek en fazla bölüm sayısı (varsayılan: 3).",
                        "default": 3,
                    },
                    "model_name": {
                        "type": "string",
                        "description": "Kütüphanede aranacak model/alt devre adı (örn: 'LT1001', '1N4148'); isteğe bağlı.",
                    },
                },
                "required": ["query"],
            },
        },
    ]


def _extended_tool_schemas() -> List[Dict[str, Any]]:
    """Genişletilmiş EDA / LTspice araç şemaları (şema çizimi, parazitik, SOA, Monte Carlo)."""
    return [
        {
            "name": "create_or_replace_schematic",
            "description": (
                "Yeni bir LTspice grafik şeması (.asc) oluşturur veya günceller, pin bağlantılarını "
                "LTspice netlisterı ile doğrular (inspect_schematic) ve isteğe bağlı simüle eder. Kopuk pin ya da "
                "açık uçlu ağ varsa status WARNING döner ve simülasyon yapılmaz; 'inspection' sorunlu pinleri listeler. "
                "Bağlantı LTspice netlisterıyla doğrulanamazsa (ör. bulunamayan sembol) status ERROR, simülasyon yok."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "circuit_name": {"type": "string", "description": "Şemanın adı."},
                    "schematic_data": {
                        "type": "object",
                        "description": (
                            "Şema bileşenleri, koordinatlar ve bağlantılarını içeren yapılandırılmış sözlük "
                            "(AscSchematic.from_dict biçimi). ADI kütüphanesindeki parçalar da kullanılabilir "
                            "(ör. sembol adı 'OpAmps\\\\LT1001', 'PowerProducts\\\\LT8610'): bağlantıda pin, .asy'deki "
                            "PinName ('U1:OUT', 'U2:EN/UV') ya da netlist sırası numarasıyla ('U1:5') verilir."
                        ),
                        "additionalProperties": True,
                    },
                    "asc_content": {
                        "type": "string",
                        "description": "Ham LTspice .asc şema metni (schematic_data yerine doğrudan verilebilir).",
                    },
                    "simulate": {
                        "type": "boolean",
                        "description": "Şema oluşturulduktan sonra doğrudan simüle edilsin mi? (varsayılan: true).",
                        "default": True,
                    },
                    "parameters": {
                        "type": "object",
                        "description": "Şemaya uygulanacak .param veya eleman değeri güncellemeleri.",
                        "additionalProperties": True,
                    },
                    "inspect_only": {
                        "type": "boolean",
                        "description": "True ise simülasyon yapmadan yalnızca LTspice netlisterı ile bağlantı/kopuk pin doğrulaması yapar.",
                        "default": False,
                    },
                },
                "required": ["circuit_name"],
            },
        },
        {
            "name": "calculate_trace_parasitics",
            "description": (
                "IPC-2141 microstrip denklemleriyle (ADI MT-094) yüzey microstrip izinin direnç R, endüktans L, "
                "kapasitans C, karakteristik empedans Z0 ve yayılma gecikmesi t_pd değerlerini hesaplar ve SPICE "
                "T-eşdeğer alt devre modelini döner. Denklem 50-100 Ohm arasında en doğrudur (dışında accuracy_note); "
                "iz dielektrik kalınlığına göre çok genişse denklem geçersizdir (ERROR)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "length_mm": {"type": "number", "description": "PCB iletim hattı uzunluğu (mm)."},
                    "width_mm": {"type": "number", "description": "Hat genişliği (mm)."},
                    "height_mm": {"type": "number", "description": "Referans düzlemine dielektrik kalınlığı (mm, varsayılan 1.6).", "default": 1.6},
                    "copper_thickness_um": {"type": "number", "description": "Bakır kalınlığı (mikrometre, varsayılan 35.56 = 1 oz = 1.4 mil).", "default": 35.56},
                    "relative_permittivity": {"type": "number", "description": "Bağıl dielektrik sabiti Er (varsayılan 4.0: MT-094 örneğindeki FR-4; laminatınızınkini verin).", "default": 4.0},
                },
                "required": ["length_mm", "width_mm"],
            },
        },
        {
            "name": "analyze_hotplug_transient",
            "description": (
                "AN-88'in seri RLC modeliyle kablo takma anı aşımını hesaplar: sönüm oranı zeta, ideal tepe V_peak, "
                "kritik sönüm (zeta = 1) için eklenecek seri direnç, ve paralel sönüm kolu önerisi: C_b = bulk_ratio·C_in "
                "(tasarım seçimi, varsayılan 4) ile ideal tepeyi en aza indiren ESR ve o tepe. Yalnız paralel kol ideal "
                "modelde kritik sönüm vermez. max_voltage verilirse tepe onunla karşılaştırılır. İdeal model gerçek "
                "tepeyi küçük tahmin edebilir (AN-88 Tablo 1: donanımda 2.38 kat); sonuçtaki notu okuyun."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "vin_step": {"type": "number", "description": "Kaynak gerilim basamağı (V, örn: 12.0)."},
                    "cin_uf": {"type": "number", "description": "Girişteki seramik kapasite (uF); DC-bias'a göre düşürülmüş değeri verin."},
                    "esr_mohm": {"type": "number", "description": "Giriş kondansatörü ESR'si (mOhm, varsayılan 0 = sönümsüz kötü durum).", "default": 0.0},
                    "cable_length_m": {
                        "type": "number",
                        "description": "Kablo uzunluğu (m); L = 2 * uzunluk * 12 nH/cm (TI SLOD006 aralığının üst ucu, tutucu). Ölçülmüş endüktans varsa cable_inductance_nh verin.",
                        "default": 1.0,
                    },
                    "cable_inductance_nh": {"type": "number", "description": "Ölçülmüş kablo + konnektör endüktansı (nH, isteğe bağlı)."},
                    "cable_resistance_mohm": {"type": "number", "description": "Kablo + konnektör direnci (mOhm, varsayılan 0 = sönümsüz kötü durum).", "default": 0.0},
                    "max_voltage": {"type": "number", "description": "Girişe bağlı parçanın mutlak maksimum gerilimi (V, isteğe bağlı)."},
                    "bulk_ratio": {"type": "number", "description": "Önerilen sönüm kondansatörünün C_in'e oranı (varsayılan 4).", "default": 4.0},
                },
                "required": ["vin_step", "cin_uf"],
            },
        },
        {
            "name": "check_design_safety",
            "description": (
                "Simülasyon logundaki .meas sonuçlarını bileşen sınırları (max_voltage, max_power) ve derating "
                "katsayılarıyla karşılaştırır. Ölçüm adı kuralı: bileşen adından hemen önceki parça 'v' (gerilim) ya da "
                "'p' (güç) ile başlar, ör. v_c1, vmax_c1, p_r1, pavg_r1; bileşen adı tam eşleşir (v_c10, C1'e sayılmaz). "
                "Gerilim bileşenin uçları arasında ölçülmeli (ör. MAX V(a,b)). Ölçümü olmayan bileşen UNCHECKED, genel "
                "sonuç INCOMPLETE olur."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "circuit_name": {"type": "string", "description": "İncelenecek devrenin adı."},
                    "component_ratings": {
                        "type": "object",
                        "description": "Bileşen nominal sınırları sözlüğü (örn: {'C1': {'max_voltage': 25.0}, 'R1': {'max_power': 0.25}}).",
                        "additionalProperties": True,
                    },
                    "derating": {
                        "type": "object",
                        "description": (
                            "Derating katsayıları (sınırın kesri). Varsayılanlar bir standarttan alınmadı, proje "
                            "varsayımıdır: {'voltage': 0.8, 'voltage_semiconductor': 0.75 (Q/D), 'power': 0.5}."
                        ),
                        "additionalProperties": True,
                    },
                },
                "required": ["circuit_name", "component_ratings"],
            },
        },
        {
            "name": "simulate_monte_carlo",
            "description": (
                "Bileşen toleransları (%5 direnç, %10 kapasite vb.) altında LTspice'ın native .step mc() motoruyla "
                "Monte Carlo istatistiksel tolerans ve verim (yield) analizi koşturur."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "circuit_name": {"type": "string", "description": "Monte Carlo uygulanacak devrenin adı."},
                    "tolerances": {
                        "type": "object",
                        "description": "Bileşen veya parametre tolerans oranları (örn: {'R1': 0.05, 'R2': 0.05, 'C1': 0.10}).",
                        "additionalProperties": True,
                    },
                    "runs": {
                        "type": "integer",
                        "description": "Monte Carlo simülasyon sayısı (varsayılan: 30).",
                        "default": 30,
                    },
                    "target_specs": {
                        "type": "object",
                        "description": (
                            "İsteğe bağlı verim kabul kriterleri (örn: {'v_out': {'min': 4.75, 'max': 5.25}}); sınırlar "
                            "dahildir. Ölçümü başarısız adım geçmemiş sayılır; hiçbir adımda üretilmeyen ölçümün verimi %0."
                        ),
                        "additionalProperties": True,
                    },
                },
                "required": ["circuit_name", "tolerances"],
            },
        },
        {
            "name": "optimize_parameters",
            "description": (
                "Kayıtlı bir devrenin birden çok parametresini (ya da R/C/L eleman değerini) birden çok .meas hedefine "
                "aynı anda ulaştırmak için Nelder-Mead aramasıyla ayarlar; çok dekadlık aralıklar logaritmik taranır. "
                "Bütçe simülasyon sayısıdır. Bulunan değerler devreye uygulanmaz; kalıcı yapmak için modify_parameters."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "circuit_name": {"type": "string", "description": "Optimize edilecek kayıtlı devrenin adı."},
                    "param_bounds": {
                        "type": "object",
                        "description": "Parametre adı -> [alt, üst] (örn: {'R1': [1000, 100000], 'C1': [1e-9, 1e-6]}).",
                        "additionalProperties": True,
                    },
                    "target_metrics": {
                        "type": "object",
                        "description": "Ölçüm adı -> hedef değer (örn: {'f_cutoff': 1000, 'gain': 2}).",
                        "additionalProperties": True,
                    },
                    "tolerance": {"type": "number", "description": "Her hedef için bağıl tolerans (varsayılan 0.02).",
                                  "default": 0.02},
                    "max_iterations": {"type": "integer", "description": "En fazla simülasyon (varsayılan 35).",
                                       "default": 35},
                },
                "required": ["circuit_name", "param_bounds", "target_metrics"],
            },
        },
    ]


#: check_design_safety derating katsayıları (sınırın kesri). Bir standarttan alınmadı, proje varsayımıdır:
#: çağıran ``derating`` ile değiştirir, sonuç ``derating_applied`` olarak görünür.
_DEFAULT_DERATING = {"voltage": 0.80, "voltage_semiconductor": 0.75, "power": 0.50}
_SAFETY_RANK = {"PASS": 0, "UNCHECKED": 1, "MARGINAL": 2, "FAIL": 3}


def _measured_stress(measurements: Dict[str, Any], component: str) -> Dict[str, float]:
    """
    ``.meas`` sonuçlarından bir bileşenin en kötü (mutlak en büyük) gerilim ``v`` ve güç ``p`` değeri.

    Ad kuralı: bileşen adı ``_`` ile ayrılmış parçalar arasında tam eşleşir ve hemen önceki parça ``v`` ya da ``p``
    ile başlar (``v_c1``, ``vmax_c1``, ``p_r1``, ``pavg_r1``). ``v_c10`` C1'e, ``pavg_r1``'deki AVG gerilime sayılmaz.
    """
    want = component.upper().split("_")
    found: Dict[str, float] = {}
    for meas_name, value in measurements.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        parts = meas_name.upper().split("_")
        for i in range(1, len(parts) - len(want) + 1):
            kind = {"V": "v", "P": "p"}.get(parts[i - 1][:1])
            if kind and parts[i:i + len(want)] == want:
                found[kind] = max(found.get(kind, 0.0), abs(float(value)))
    return found


def _strip_keys(node: Any, keys: frozenset) -> Any:
    """Şema ağacından verilen anahtarları (her düzeyde) kaldırır."""
    if isinstance(node, dict):
        return {k: _strip_keys(v, keys) for k, v in node.items() if k not in keys}
    if isinstance(node, list):
        return [_strip_keys(v, keys) for v in node]
    return node


def convert_tool_schemas(schemas: List[Dict[str, Any]], fmt: str = "generic") -> List[Dict[str, Any]]:
    """
    Sağlayıcıdan bağımsız araç şemalarını LLM sağlayıcısının beklediği biçime çevirir.

    - ``generic``: ``{name, description, parameters}`` (varsayılan)
    - ``openai``: ``{"type": "function", "function": {...}}`` (Chat Completions ``tools=``)
    - ``anthropic``: ``{name, description, input_schema}`` (Messages API ``tools=``)
    - ``gemini``: ``[{"function_declarations": [...]}]``; Gemini'nin desteklemediği ``additionalProperties`` ve
      ``default`` anahtarları çıkarılır, parametresiz araçlarda ``parameters`` alanı atlanır.

    Biçimler sağlayıcı dokümantasyonuna göre üretilir; canlı API üzerinde otomatik test edilmez.
    """
    fmt = fmt.lower()
    if fmt not in TOOL_SCHEMA_FORMATS:
        raise ValueError(f"Bilinmeyen araç şeması biçimi {fmt!r}; seçenekler: {TOOL_SCHEMA_FORMATS}")

    schemas = copy.deepcopy(schemas)
    if fmt == "generic":
        return schemas
    if fmt == "openai":
        return [{"type": "function", "function": s} for s in schemas]
    if fmt == "anthropic":
        return [
            {"name": s["name"], "description": s["description"], "input_schema": s["parameters"]}
            for s in schemas
        ]

    declarations = []
    for s in schemas:
        declaration: Dict[str, Any] = {"name": s["name"], "description": s["description"]}
        params = _strip_keys(s["parameters"], frozenset({"additionalProperties", "default"}))
        if params.get("properties"):
            declaration["parameters"] = params
        declarations.append(declaration)
    return [{"function_declarations": declarations}]


def get_ltspice_tool_schemas(format: str = "generic", include_extended: bool = False) -> List[Dict[str, Any]]:
    """
    LLM'e sunulan profesyonel EDA / LTspice araçlarının JSON şemaları.

    :param format: ``"generic"`` (varsayılan), ``"openai"``, ``"anthropic"`` veya ``"gemini"``;
        bkz. `convert_tool_schemas`.
    :param include_extended: True ise şema üretimi, parazitik hesaplama, hot-plug, SOA güvenlik
        ve Monte Carlo araçlarını da ekler (varsayılan False: temel 10 araç).
    """
    schemas = _generic_tool_schemas()
    if include_extended:
        schemas = schemas + _extended_tool_schemas()
    return convert_tool_schemas(schemas, format)


def _load_arguments(arguments: Any) -> Dict[str, Any]:
    """Araç argümanlarını sözlüğe çevirir. OpenAI gibi sağlayıcılar argümanları JSON metni olarak gönderir."""
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str) and arguments.strip():
        try:
            loaded = json.loads(arguments)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _coerce_dict(value: Any) -> Dict[str, Any]:
    """Sözlük bekleyen bir argümanı (LLM bazen JSON metni gönderir) sözlüğe çevirir."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


#: `handle_tool_call(allow_file_access=...)`: çağrı boyunca araçların gördüğü izin (iş parçacığı ve async güvenli)
_FILE_ACCESS: contextvars.ContextVar[bool] = contextvars.ContextVar("ltspice_llm_file_access", default=False)


def _refused_file_access(text: str) -> Optional[Dict[str, Any]]:
    """
    LLM'den gelen netlist/şema dosya yazıyor ya da çalışma alanı dışını okuyorsa ERROR sonucu, değilse None (bkz.
    `netlist.file_access_problems`). Eskiden netlist olduğu gibi LTspice'a gidiyordu: ``.wave`` istenen yola yazar
    (inceleme 2026-10-11). Güvenilir girdide ``handle_tool_call(..., allow_file_access=True)``.
    """
    if _FILE_ACCESS.get():
        return None
    problems = file_access_problems(text)
    if not problems:
        return None
    return {
        "status": "ERROR",
        "message": ("Netlist dosya sistemine dokunuyor; güvenlik için simüle edilmedi: " + " ".join(problems)
                    + " Kütüphane adları (.lib standard.dio) ve çalışma alanındaki dosyalar serbesttir. Girdi "
                    "güvenilirse handle_tool_call(..., allow_file_access=True)."),
        "file_access": problems,
    }


def _png_base64(path: Path) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("utf-8")


def _search_reference(args: Dict[str, Any]) -> Dict[str, Any]:
    """``search_ltspice_reference`` aracı: ADI referans belgelerinde arama ve isteğe bağlı kütüphane araması."""
    query = str(args.get("query") or "").strip()
    model_name = str(args.get("model_name") or "").strip()
    if not query and not model_name:
        return {"status": "ERROR", "message": "'query' (ya da 'model_name') gereklidir."}
    try:
        max_results = max(1, min(int(args.get("max_results", 3)), 8))
    except (TypeError, ValueError):
        max_results = 3

    response: Dict[str, Any] = {"status": "SUCCESS"}
    docs = ReferenceDocs()
    if query:
        if not docs.available:
            response = {
                "status": "ERROR",
                "message": (
                    "ADI LTspice AI Reference belgeleri bulunamadı (LTspice 26+ ile %LOCALAPPDATA%\\LTspice\\reference "
                    "altına kurulur). Yolu LTSPICE_REFERENCE_DIR ortam değişkeniyle belirtebilirsiniz."
                ),
            }
        else:
            hits = docs.search(query, max_results=max_results)
            response["source"] = str(docs.directory)
            response["results"] = [sec.to_dict() for sec in hits]
            if not hits:
                response["message"] = "Eşleşen bölüm yok; daha genel sözcüklerle deneyin."
    if model_name:
        definitions = find_model_definitions(model_name)
        response["model_definitions"] = definitions
        if not definitions:
            response["model_message"] = (
                f"'{model_name}' LTspice kütüphanesinde bulunamadı; üreticinin modelini .include ile ekleyin."
            )
        elif response.get("status") == "ERROR":
            response["status"] = "SUCCESS"
    return response


def _analysis_kind_or_none(raw_file: Path) -> Optional[str]:
    """`.raw` dosyasının analiz türü; dosya okunamıyorsa `None` (asıl hata çizim aşamasında bildirilir)."""
    try:
        return RawParser.analysis_kind(raw_file)
    except Exception:
        return None


# --------------------------------------------------------------------------- saf hesap araçları (LTspice'sız)
def _tool_quantize_parameters(args: Dict[str, Any], name: str) -> Dict[str, Any]:
    params = _coerce_dict(args.get("parameters"))
    series = args.get("series", "E24")
    mode = args.get("mode", "nearest")
    return quantize_circuit_parameters(parameters=params, series=series, mode=mode)


def _tool_search_reference(args: Dict[str, Any], name: str) -> Dict[str, Any]:
    # Yalnızca kurulumdaki belgeleri okur: LTspice çalıştırmaz.
    return _search_reference(args)


def _tool_trace_parasitics(args: Dict[str, Any], name: str) -> Dict[str, Any]:
    """``calculate_trace_parasitics``: analitik IPC-2141 microstrip hesabı."""
    length_mm = float(args.get("length_mm", 0.0))
    width_mm = float(args.get("width_mm", 0.0))
    if length_mm <= 0 or width_mm <= 0:
        return {"status": "ERROR", "message": "length_mm ve width_mm pozitif olmalıdır."}
    h_mm = float(args.get("height_mm", 1.6))
    t_um = float(args.get("copper_thickness_um", COPPER_MM_PER_OZ * 1e3))
    er = float(args.get("relative_permittivity", 4.0))

    trace = calculate_trace_parasitics(
        length_mm=length_mm,
        width_mm=width_mm,
        height_mm=h_mm,
        copper_oz=t_um / (COPPER_MM_PER_OZ * 1e3),
        er=er,
    )
    return {
        "status": "SUCCESS",
        "length_mm": trace.length_mm,
        "width_mm": trace.width_mm,
        "resistance_ohm": trace.r_dc,
        "inductance_nh": trace.l_trace * 1e9,
        "capacitance_pf": trace.c_trace * 1e12,
        "characteristic_impedance_ohm": trace.z0,
        "propagation_delay_ps": trace.tpd * 1e12,
        "accuracy_note": trace.accuracy_note,
        "spice_subcircuit": trace.to_spice_subckt(f"TRACE_{name.upper()}"),
    }


def _tool_hotplug_transient(args: Dict[str, Any], name: str) -> Dict[str, Any]:
    """``analyze_hotplug_transient``: AN-88'in seri RLC modeliyle takma anı hesabı."""
    vin_step = float(args.get("vin_step", 0.0))
    cin_uf = float(args.get("cin_uf", 0.0))
    if vin_step <= 0 or cin_uf <= 0:
        return {"status": "ERROR", "message": "vin_step ve cin_uf pozitif olmalıdır."}
    if "cable_type" in args:
        return {
            "status": "ERROR",
            "message": "cable_type kaldırıldı (kaynaksız hazır değerlerdi): cable_length_m ya da ölçülmüş "
                       "cable_inductance_nh / cable_resistance_mohm verin.",
        }
    esr_mohm = float(args.get("esr_mohm", 0.0))
    l_nh = args.get("cable_inductance_nh")
    if l_nh is not None:
        cable_l = float(l_nh) * 1e-9
    else:
        cable_l = get_cable_parasitics(float(args.get("cable_length_m", 1.0))).l_cable
    cable_r = float(args.get("cable_resistance_mohm", 0.0)) * 1e-3
    max_v = args.get("max_voltage")

    analysis = analyze_hotplug_transient(
        v_in=vin_step,
        c_in=cin_uf * 1e-6,
        l_cable=cable_l,
        r_cable=cable_r + esr_mohm * 1e-3,
        max_voltage=None if max_v is None else float(max_v),
        bulk_ratio=float(args.get("bulk_ratio", DEFAULT_BULK_RATIO)),
    )
    return {
        "status": "SUCCESS",
        "cable_inductance_nh": cable_l * 1e9,
        "damping_ratio_zeta": analysis.damping_ratio,
        "peak_voltage": analysis.v_peak,
        "overshoot_percent": analysis.overshoot_percent,
        "is_safe": analysis.safe_margin_rating == "WITHIN_RATING",
        "risk_assessment": analysis.safe_margin_rating,
        "recommended_series_r_ohm": analysis.recommended_damping_r,
        "recommended_damping_c_bulk_f": analysis.recommended_bulk_c,
        "recommended_damping_esr_ohm": analysis.recommended_bulk_esr,
        "recommended_damped_peak_voltage": analysis.recommended_damped_peak,
        "note": analysis.note,
    }


# --------------------------------------------------------------------------- devre ve simülasyon araçları
def _tool_create_circuit(args: Dict[str, Any], name: str, adapter: LTSpiceAdapter) -> Dict[str, Any]:
    code = args.get("netlist_code") or args.get("code") or args.get("netlist") or ""
    params = _coerce_dict(args.get("parameters") or args.get("params")) or None
    if not str(code).strip():
        return {"status": "ERROR", "message": "Netlist kodu boş olamaz ('netlist_code' gereklidir)."}

    refused = _refused_file_access(apply_parameters(str(code), params).content if params else str(code))
    if refused:
        return refused
    # LLM girdisi her zaman netlist METNİ sayılır; asla dosya yolu olarak okunmaz.
    result = adapter.run_netlist(
        netlist_content_or_path=str(code),
        circuit_name=name,
        parameters=params,
        treat_as_text=True,
    )
    return result.to_llm_dict()


def _tool_modify_parameters(args: Dict[str, Any], name: str, adapter: LTSpiceAdapter) -> Dict[str, Any]:
    params = _coerce_dict(args.get("parameters") or args.get("params"))
    netlist_file = adapter.workspace_dir / f"{name}.net"

    if not netlist_file.exists():
        return {
            "status": "ERROR",
            "message": f"Devre dosyası bulunamadı: {netlist_file}. Önce create_or_replace_circuit çağırın.",
        }

    if params:
        content = netlist_file.read_text(encoding="utf-8", errors="ignore")
        unresolved = apply_parameters(content, params).unresolved
        if unresolved:
            return {
                "status": "ERROR",
                "message": (
                    f"Şu ad(lar) '{name}' devresinde .param, {{ad}} referansı veya R/C/L/V/I eleman adı "
                    f"olarak bulunamadı: {unresolved}. Devre değiştirilmedi ve simüle edilmedi."
                ),
                "available_parameters": list_param_names(content),
                "available_elements": list_element_names(content),
            }
        refused = _refused_file_access(apply_parameters(content, params).content)
        if refused:
            return refused

    result = adapter.run_netlist(
        netlist_content_or_path=netlist_file,
        circuit_name=name,
        parameters=params,
    )
    return result.to_llm_dict()


def _signal_list(signals: Any) -> Any:
    """LLM tek sinyali dizi yerine metin olarak gönderebilir: ``'["V(a)"]'`` ya da ``'V(in), V(a,b)'``."""
    if isinstance(signals, str):
        stripped = signals.strip()
        if stripped.startswith("["):
            try:
                signals = json.loads(stripped)
            except json.JSONDecodeError:
                signals = None
        if isinstance(signals, str):
            # Parantez içindeki virgüller ("V(a,b)") ayırıcı sayılmaz.
            signals = [s.strip() for s in re.split(r",(?![^()]*\))", signals) if s.strip()]
    return signals


def _tool_plot_transient(args: Dict[str, Any], name: str, adapter: LTSpiceAdapter) -> Dict[str, Any]:
    signals = _signal_list(args.get("signals"))
    title = args.get("title", f"LTspice Simülasyonu: {name}")
    raw_file = adapter.workspace_dir / f"{name}.raw"

    if not raw_file.exists():
        return {
            "status": "ERROR",
            "message": f".raw dosyası bulunamadı: {raw_file}. Devrenin .tran analizi içerdiğinden emin olun.",
        }
    if _analysis_kind_or_none(raw_file) == "ac":
        return {
            "status": "ERROR",
            "message": f"'{name}' devresi .ac (frekans) analizi içeriyor; bunun için plot_bode_diagram kullanın.",
        }

    png_path = WaveformVisualizer.plot_traces(
        raw_path=raw_file,
        signals=signals,
        title=title,
    )
    response: Dict[str, Any] = {
        "status": "SUCCESS",
        "plot_saved_at": str(png_path),
        "message": f"Dalga formu grafiği başarıyla üretildi: {png_path.name}",
    }
    if args.get("include_image_base64"):
        response["image_base64"] = _png_base64(png_path)
    return response


def _tool_plot_bode(args: Dict[str, Any], name: str, adapter: LTSpiceAdapter) -> Dict[str, Any]:
    out_node = args.get("out_node", "V(out)")
    in_node = args.get("in_node", "V(in)")
    title = args.get("title", f"Bode Frekans ve Faz Analizi: {name}")
    raw_file = adapter.workspace_dir / f"{name}.raw"

    if not raw_file.exists():
        return {
            "status": "ERROR",
            "message": f".raw dosyası bulunamadı: {raw_file}. Devrenin .ac analizi içerdiğinden emin olun.",
        }
    kind = _analysis_kind_or_none(raw_file)
    if kind is not None and kind != "ac":
        return {
            "status": "ERROR",
            "message": (
                f"'{name}' devresi .ac analizi içermiyor ('{kind}' analizi); Bode diyagramı için "
                "netliste bir .ac direktifi ekleyip yeniden simüle edin."
            ),
        }

    png_path = WaveformVisualizer.plot_bode(
        raw_path=raw_file,
        out_node=out_node,
        in_node=in_node,
        title=title,
    )
    response: Dict[str, Any] = {
        "status": "SUCCESS",
        "bode_plot_saved_at": str(png_path),
        "message": f"Bode diyagramı başarıyla üretildi: {png_path.name}",
    }
    if args.get("include_image_base64"):
        response["image_base64"] = _png_base64(png_path)
    return response


def _tool_get_netlist(args: Dict[str, Any], name: str, adapter: LTSpiceAdapter) -> Dict[str, Any]:
    netlist_file = adapter.workspace_dir / f"{name}.net"
    if not netlist_file.exists():
        return {"status": "ERROR", "message": f"Devre dosyası bulunamadı: {netlist_file}"}

    return {
        "status": "SUCCESS",
        "circuit_name": name,
        # Kilitlenme korumasının eklediği otomatik ".options threads" satırı devrenin parçası değildir
        "netlist": strip_auto_directives(netlist_file.read_text(encoding="utf-8", errors="ignore")),
    }


def _tool_list_circuits(args: Dict[str, Any], name: str, adapter: LTSpiceAdapter) -> Dict[str, Any]:
    circuits = []
    for net_path in adapter.workspace_dir.glob("*.net"):
        stem = net_path.stem
        has_raw = (adapter.workspace_dir / f"{stem}.raw").exists()
        has_log = (adapter.workspace_dir / f"{stem}.log").exists()
        has_png = (adapter.workspace_dir / f"{stem}.png").exists() or (
            adapter.workspace_dir / f"{stem}_bode.png"
        ).exists()
        circuits.append(
            {
                "name": stem,
                "netlist_path": str(net_path),
                "has_raw": has_raw,
                "has_log": has_log,
                "has_plot": has_png,
            }
        )
    return {
        "status": "SUCCESS",
        "total_circuits": len(circuits),
        "circuits": circuits,
    }


def _tool_diagnose(args: Dict[str, Any], name: str, adapter: LTSpiceAdapter) -> Dict[str, Any]:
    netlist_file = adapter.workspace_dir / f"{name}.net"
    log_file = adapter.workspace_dir / f"{name}.log"
    net_content = netlist_file.read_text(encoding="utf-8", errors="ignore") if netlist_file.exists() else ""
    log_content = LogParser.read_text(log_file) if log_file.exists() else ""
    extra_error = args.get("error_text", "")
    if extra_error:
        log_content = extra_error + "\n" + log_content

    report = SPICEDiagnostics.diagnose(log_content=log_content, netlist_code=net_content)
    res = report.to_dict()

    if args.get("apply_fix", False) and report.has_error and netlist_file.exists():
        patched_net = SPICEDiagnostics.apply_diagnostic_patch(net_content, report)
        netlist_file.write_text(patched_net, encoding="utf-8")
        res["fix_applied"] = True
        res["message"] = f"Önerilen stabilizasyon direktifleri '{name}.net' dosyasına uygulandı."
    else:
        res["fix_applied"] = False

    return res


def _tool_optimize(args: Dict[str, Any], name: str, adapter: LTSpiceAdapter) -> Dict[str, Any]:
    netlist_file = adapter.workspace_dir / f"{name}.net"
    if not netlist_file.exists():
        return {
            "status": "ERROR",
            "message": f"Devre dosyası bulunamadı: {netlist_file}. Önce create_or_replace_circuit çağırın.",
        }

    missing = [
        k for k in ("param_name", "target_meas_name", "target_value", "min_value", "max_value")
        if args.get(k) in (None, "")
    ]
    if missing:
        return {"status": "ERROR", "message": f"Eksik argüman(lar): {missing}"}

    try:
        target_value = float(args["target_value"])
        min_value = float(args["min_value"])
        max_value = float(args["max_value"])
        tolerance = float(args.get("tolerance", 0.01))
        max_iterations = int(args.get("max_iterations", 15))
    except (TypeError, ValueError) as ex:
        return {"status": "ERROR", "message": f"Sayısal argüman geçersiz: {ex}"}
    if not min_value < max_value:
        return {"status": "ERROR", "message": "min_value, max_value'dan küçük olmalıdır."}
    if max_iterations < 3:
        return {"status": "ERROR", "message": "max_iterations en az 3 olmalıdır (2 uç nokta + arama)."}

    optimization = CircuitOptimizer(adapter=adapter).optimize_single_parameter(
        base_netlist=netlist_file.read_text(encoding="utf-8", errors="ignore"),
        circuit_name=name,
        param_name=str(args["param_name"]),
        target_meas_name=str(args["target_meas_name"]),
        target_value=target_value,
        min_param=min_value,
        max_param=max_value,
        tolerance=tolerance,
        max_iterations=max_iterations,
    )
    res = optimization.to_llm_dict()
    if optimization.converged:
        res["hint"] = (
            f"En iyi değeri devreye kalıcı uygulamak için modify_parameters çağırın: "
            f"{optimization.best_parameters}"
        )
    return res


def _tool_optimize_multi(args: Dict[str, Any], name: str, adapter: LTSpiceAdapter) -> Dict[str, Any]:
    """``optimize_parameters``: `CircuitOptimizer.optimize_multi_parameters` (Nelder-Mead, birden çok hedef)."""
    netlist_file = adapter.workspace_dir / f"{name}.net"
    if not netlist_file.exists():
        return {"status": "ERROR",
                "message": f"Devre dosyası bulunamadı: {netlist_file}. Önce create_or_replace_circuit çağırın."}
    bounds: Dict[str, Tuple[float, float]] = {}
    try:
        for key, pair in _coerce_dict(args.get("param_bounds")).items():
            if isinstance(pair, str) or len(pair) != 2:
                raise ValueError(f"'{key}': {pair!r}")
            bounds[str(key)] = (float(pair[0]), float(pair[1]))
    except (TypeError, ValueError) as ex:
        return {"status": "ERROR", "message": f"param_bounds: her parametre için [alt, üst] sayı çifti verin ({ex})."}
    try:
        targets = {str(k): float(v) for k, v in _coerce_dict(args.get("target_metrics")).items()}
        tolerance = float(args.get("tolerance", 0.02))
        max_iterations = int(args.get("max_iterations", 35))
    except (TypeError, ValueError) as ex:
        return {"status": "ERROR", "message": f"target_metrics: her ölçüm için bir sayı verin ({ex})."}
    if not bounds:
        return {"status": "ERROR", "message": "param_bounds en az bir parametre içermeli."}
    if not targets:
        return {"status": "ERROR", "message": "target_metrics en az bir ölçüm içermeli."}
    for key, (low, high) in bounds.items():
        if not low < high:
            return {"status": "ERROR", "message": f"'{key}' için alt sınır ({low:g}) üst sınırdan ({high:g}) küçük olmalı."}

    optimization = CircuitOptimizer(adapter=adapter).optimize_multi_parameters(
        base_netlist=netlist_file.read_text(encoding="utf-8", errors="ignore"),
        circuit_name=name,
        param_bounds=bounds,
        target_metrics=targets,
        tolerance=tolerance,
        max_iterations=max_iterations,
    )
    res = optimization.to_llm_dict()
    if optimization.converged:
        res["hint"] = (f"En iyi değerleri devreye kalıcı uygulamak için modify_parameters çağırın: "
                       f"{optimization.best_parameters}")
    return res


def _tool_schematic(args: Dict[str, Any], name: str, adapter: LTSpiceAdapter) -> Dict[str, Any]:
    sch_data = _coerce_dict(args.get("schematic_data"))
    asc_code = args.get("asc_content") or args.get("asc_code")
    params = _coerce_dict(args.get("parameters") or args.get("params")) or None
    inspect_only = bool(args.get("inspect_only", False))
    simulate = bool(args.get("simulate", True)) if not inspect_only else False

    if not sch_data and not asc_code:
        return {
            "status": "ERROR",
            "message": "Şema oluşturmak için 'schematic_data' sözlüğü veya 'asc_content' metni gereklidir.",
        }

    asc_text = AscSchematic.from_dict(sch_data).render() if sch_data else str(asc_code)
    refused = _refused_file_access(apply_parameters(asc_text, params).content if params else asc_text)
    if refused:
        return refused
    asc_path = adapter.workspace_dir / f"{name}.asc"
    write_asc_file(asc_path, asc_text)

    # Bağlantı LTspice netlisteriyle doğrulanmadan simülasyona geçilmez: doğrulanamazsa ERROR, kopuk pin ya da
    # açık uçlu ağ varsa WARNING (eskiden ikisinde de SUCCESS dönülüp kopuk şema simüle ediliyordu).
    try:
        inspection = adapter.inspect_schematic(asc_path)
    except Exception as ex:
        return {
            "status": "ERROR",
            "circuit_name": name,
            "schematic_path": str(asc_path),
            "message": f"Şema LTspice netlisteriyle doğrulanamadı, simülasyon yapılmadı: {ex}",
        }
    if inspection.get("status") != "SUCCESS":
        return {
            "status": "WARNING",
            "circuit_name": name,
            "schematic_path": str(asc_path),
            "inspection": inspection,
            "message": f"Şemada bağlantı sorunu var, simülasyon yapılmadı: {inspection.get('message', '')}",
        }

    if inspect_only or not simulate:
        return {
            "status": "SUCCESS",
            "circuit_name": name,
            "schematic_path": str(asc_path),
            "inspection": inspection,
            "message": f"'{name}.asc' şeması başarıyla kaydedildi.",
        }

    result = adapter.run_netlist(
        netlist_content_or_path=asc_path,
        circuit_name=name,
        parameters=params,
    )
    res_dict = result.to_llm_dict()
    res_dict["schematic_path"] = str(asc_path)
    res_dict["inspection"] = inspection
    return res_dict


def _component_safety(
    comp: str, spec: Dict[str, Any], measurements: Dict[str, Any], derating: Dict[str, float]
) -> Dict[str, Any]:
    """Bir bileşenin ölçülen en kötü gerilim ve gücünü nominal sınırla ve derating sınırıyla karşılaştırır."""
    measured = _measured_stress(measurements, comp)
    v_factor = derating["voltage_semiconductor"] if comp[:1].upper() in ("Q", "D") else derating["voltage"]
    checks = []
    rated_v = spec.get("max_voltage") or spec.get("v_max") or spec.get("v_ceo_max")
    if rated_v is not None:
        checks.append(("Gerilim", "V", "v", measured.get("v"), float(rated_v), v_factor))
    rated_p = spec.get("max_power") or spec.get("p_max")
    if rated_p is not None:
        checks.append(("Güç", "W", "p", measured.get("p"), float(rated_p), derating["power"]))

    comp_status = "PASS" if checks else "UNCHECKED"
    details = [] if checks else ["Tanınan sınır yok (max_voltage / max_power)."]
    for label, unit, prefix, value, rated, factor in checks:
        if value is None:
            status = "UNCHECKED"
            details.append(f"{label} ölçümü yok: ör. '.meas tran {prefix}_{comp.lower()} MAX ...' ekleyin.")
        else:
            limit = rated * factor
            pct = value / rated * 100.0 if rated > 0 else float("inf")
            if value > rated:
                status = "FAIL"
                details.append(f"{label} sınırı aşıldı: {value:.4g} {unit} > {rated:.4g} {unit}.")
            elif value > limit:
                status = "MARGINAL"
                details.append(f"{label} derating sınırı aşıldı (%{pct:.1f}): {value:.4g} {unit} > "
                               f"{limit:.4g} {unit} (katsayı {factor:g}).")
            else:
                status = "PASS"
                details.append(f"{label} güvenli (%{pct:.1f} stres, sınır {limit:.4g} {unit}).")
        comp_status = max(comp_status, status, key=lambda s: _SAFETY_RANK[s])

    return {
        "component": comp,
        "status": comp_status,
        "measured_v_peak": measured.get("v"),
        "measured_power": measured.get("p"),
        "details": details,
    }


def _tool_design_safety(args: Dict[str, Any], name: str, adapter: LTSpiceAdapter) -> Dict[str, Any]:
    ratings = _coerce_dict(args.get("component_ratings"))
    if not ratings:
        return {"status": "ERROR", "message": "'component_ratings' sözlüğü gereklidir."}
    given = _coerce_dict(args.get("derating"))
    unknown = sorted(set(given) - set(_DEFAULT_DERATING))
    if unknown:
        return {"status": "ERROR", "message": f"Bilinmeyen derating anahtarı {unknown}; geçerli: {list(_DEFAULT_DERATING)}."}
    derating = {**_DEFAULT_DERATING, **{k: float(v) for k, v in given.items()}}

    log_file = adapter.workspace_dir / f"{name}.log"
    if not log_file.exists():
        return {
            "status": "ERROR",
            "message": f"Devre simülasyon logu bulunamadı: '{name}'. Önce devreyi simüle edin.",
        }
    measurements = LogParser.parse_file(log_file).measurements

    results = [_component_safety(comp, spec, measurements, derating)
               for comp, spec in ratings.items() if isinstance(spec, dict)]
    worst = max((r["status"] for r in results), key=lambda s: _SAFETY_RANK[s], default="UNCHECKED")
    return {
        "status": "SUCCESS",
        "overall_safety": "INCOMPLETE" if worst == "UNCHECKED" else worst,
        "circuit_name": name,
        "derating_applied": derating,
        "unchecked": [r["component"] for r in results if r["status"] == "UNCHECKED"],
        "components": results,
    }


def _monte_carlo_netlist(content: str, tolerances: Dict[str, Any]) -> str:
    """Toleranslı elemanın değeri ``mc(nominal, tolerans)`` olur; nominal ``.param``'lar ``.end``'in önüne yazılır."""
    mc_content = content
    nominal_params: List[str] = []
    for comp, tol in tolerances.items():
        tol_float = float(tol)
        elem = _set_element_value(mc_content, comp, f"{{mc({{{comp}_nom}}, {tol_float})}}")
        if elem is not None:
            mc_content, old_val = elem
            # İlk satır başlıktır (ADI SPICE-SYNTAX-REFERENCE "First line"); oraya yazılan .param okunmaz.
            nominal_params.append(f".param {comp}_nom={old_val}")
        else:
            param_re = re.compile(rf"(\b{re.escape(comp)}\s*=\s*)([^\s;]+)", re.IGNORECASE)
            m = param_re.search(mc_content)
            if m:
                old_p = m.group(2)
                mc_content = param_re.sub(rf"\g<1>{{mc({old_p}, {tol_float})}}", mc_content, count=1)
    return insert_before_end(mc_content, nominal_params)


def _yield_analysis(
    stepped: Dict[str, List[Dict[str, Any]]], target_specs: Dict[str, Any], runs: int
) -> Dict[str, Dict[str, Any]]:
    """
    ``target_specs`` sınırlarını (dahil) sağlayan adımların oranı. stepped_measurements: ``{ölçüm: [{"step": n,
    ölçüm: değer, ...}, ...]}`` (adapter.SimulationResult). LTspice ölçüm adlarını küçük harfe çevirir; ölçümü
    başarısız (None) adım spec'i sağlamamış sayılır. Her adımda başarısız ölçüm için LTspice 26.1.1 tablo yazmaz
    ('Measurement "x" FAIL'ed'): verim %0.
    """
    yield_analysis = {}
    for spec_name, spec_bounds in target_specs.items():
        if not isinstance(spec_bounds, dict):
            continue
        key = spec_name if spec_name in stepped else spec_name.lower()
        rows = stepped.get(key) or []
        s_min = spec_bounds.get("min")
        s_max = spec_bounds.get("max")
        passed = 0
        for row in rows:
            v = row.get(key)
            if not isinstance(v, (int, float)):
                continue  # başarısız adım spec'i sağlamamış sayılır
            if s_min is not None and v < float(s_min):
                continue
            if s_max is not None and v > float(s_max):
                continue
            passed += 1
        total = len(rows) or runs
        yield_analysis[spec_name] = {
            "total_runs": total,
            "passed_runs": passed,
            "yield_percent": (passed / total) * 100.0,
            "min_spec": s_min,
            "max_spec": s_max,
        }
        if not rows:
            yield_analysis[spec_name]["note"] = (
                "Ölçüm hiçbir adımda üretilmedi (FAIL'ed ya da tanımsız ölçüm adı); verim %0 sayıldı."
            )
    return yield_analysis


def _tool_monte_carlo(args: Dict[str, Any], name: str, adapter: LTSpiceAdapter) -> Dict[str, Any]:
    tolerances = _coerce_dict(args.get("tolerances"))
    runs = int(args.get("runs", 30))
    target_specs = _coerce_dict(args.get("target_specs"))
    if not tolerances:
        return {"status": "ERROR", "message": "'tolerances' sözlüğü gereklidir."}
    if runs < 2:
        return {"status": "ERROR", "message": "runs en az 2 olmalıdır."}

    netlist_file = adapter.workspace_dir / f"{name}.net"
    if not netlist_file.exists():
        return {
            "status": "ERROR",
            "message": f"Devre netlist dosyası bulunamadı: {netlist_file}. Önce devreyi oluşturun.",
        }

    mc_content = _monte_carlo_netlist(netlist_file.read_text(encoding="utf-8", errors="ignore"), tolerances)
    res = adapter.run_netlist(
        netlist_content_or_path=mc_content,
        circuit_name=f"{name}_mc",
        _extra_directives=[f".step param run 1 {runs} 1"],
        treat_as_text=True,
    )

    yield_analysis = {}
    if target_specs and res.success:
        yield_analysis = _yield_analysis(res.stepped_measurements, target_specs, runs)

    llm_res = res.to_llm_dict()
    llm_res["monte_carlo_runs"] = runs
    llm_res["step_statistics"] = res.step_statistics
    if yield_analysis:
        llm_res["yield_analysis"] = yield_analysis
    return llm_res


# --------------------------------------------------------------------------- dağıtım
#: Saf hesap ve belge araçları: LTspice kurulu olmasa da çalışır, adaptör oluşturulmaz.
_PURE_TOOLS: Dict[str, Callable[[Dict[str, Any], str], Dict[str, Any]]] = {
    "quantize_parameters": _tool_quantize_parameters,
    "search_ltspice_reference": _tool_search_reference,
    "calculate_trace_parasitics": _tool_trace_parasitics,
    "analyze_hotplug_transient": _tool_hotplug_transient,
}
#: Çalışma alanındaki devrelerle çalışan araçlar (eski adlar geriye dönük uyumluluk için).
_CIRCUIT_TOOLS: Dict[str, Callable[[Dict[str, Any], str, LTSpiceAdapter], Dict[str, Any]]] = {
    "create_or_replace_circuit": _tool_create_circuit,
    "simulate_circuit": _tool_create_circuit,
    "modify_parameters": _tool_modify_parameters,
    "tune_circuit_parameters": _tool_modify_parameters,
    "plot_transient_waveforms": _tool_plot_transient,
    "plot_circuit_waveforms": _tool_plot_transient,
    "plot_bode_diagram": _tool_plot_bode,
    "get_circuit_netlist": _tool_get_netlist,
    "list_circuits": _tool_list_circuits,
    "diagnose_circuit_issue": _tool_diagnose,
    "optimize_parameter": _tool_optimize,
    "optimize_parameters": _tool_optimize_multi,
    "create_or_replace_schematic": _tool_schematic,
    "create_schematic": _tool_schematic,
    "check_design_safety": _tool_design_safety,
    "simulate_monte_carlo": _tool_monte_carlo,
}


def handle_tool_call(
    tool_name: str,
    arguments: Optional[Any] = None,
    adapter: Optional[LTSpiceAdapter] = None,
    allow_file_access: bool = False,
) -> Dict[str, Any]:
    """
    LLM'den gelen araç çağrısını (Tool Call) yakalar ve LTspice üzerinde yürütür.
    Tüm olası argüman formatlarını (sözlük veya JSON metni) toleranslı bir şekilde ayrıştırır
    ve hataları yakalar.

    :param allow_file_access: False (varsayılan) iken LLM'in netlisti dosya yazamaz (``.wave``, ``.savebias``,
        ``savestate``) ve çalışma alanı dışını okuyamaz (mutlak yol, ``..`` ya da ``%VAR%``; bkz.
        `netlist.file_access_problems`).
        Girdi güvenilirse True.
    """
    args = _load_arguments(arguments)
    token = _FILE_ACCESS.set(bool(allow_file_access))

    try:
        # Ad, dosya yoluna dönüşmeden önce temizlenir:
        # LLM'den gelen "../x" gibi değerler workspace dışına çıkamaz ve oluşturma ile
        # sonraki çağrılar aynı dosyaya bakar.
        name = sanitize_circuit_name(args.get("circuit_name") or args.get("name") or "circuit")

        pure = _PURE_TOOLS.get(tool_name)
        if pure is not None:
            return pure(args, name)
        tool = _CIRCUIT_TOOLS.get(tool_name)
        if tool is None:
            return {
                "status": "ERROR",
                "message": f"Bilinmeyen araç çağrısı: '{tool_name}'",
            }
        # Adaptör try içinde oluşturulur: LTspice bulunamazsa istisna değil ERROR sözlüğü dönülür.
        return tool(args, name, adapter or LTSpiceAdapter())
    except Exception as ex:
        return {
            "status": "ERROR",
            "message": f"Araç yürütme hatası ({tool_name}): {str(ex)}",
        }
    finally:
        _FILE_ACCESS.reset(token)
