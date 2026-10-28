"""
SPICE Convergence and Error Diagnostics Engine
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
LTspice simülasyonları başarısız olduğunda (Time step too small, Singular Matrix,
Iteration limit reached, Floating node, tanımsız model, reddedilen .step vb.) log çıktısını ve netlist
yapısını otomatik analiz ederek hata nedenini teşhis eder, sorunlu düğüm ve elemanları tespit eder
ve LLM için somut SPICE çözüm seçenekleri ve otomatik onarım yamaları üretir.

Öneriler ADI LTspice AI Reference'ın TROUBLESHOOTING-GUIDE.md belgesindeki sırayı izler ("önce devreyi
düzelt", sonra en az agresif seçenek). Hata mesajları LTspice 26.1.1'in gerçek çıktısıyla doğrulanmıştır;
ayrıntı: ``docs/LESSONS_LEARNED_LTSPICE_REFERENCE.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .netlist import insert_before_end
from .parser import CONVERGENCE_SCORE_THRESHOLD, parse_convergence_report


class SPICEIssueType(str, Enum):
    TIMESTEP_TOO_SMALL = "TIMESTEP_TOO_SMALL"
    SINGULAR_MATRIX = "SINGULAR_MATRIX"
    PARALLEL_VOLTAGE_SOURCES = "PARALLEL_VOLTAGE_SOURCES"
    FLOATING_NODE = "FLOATING_NODE"
    DC_OP_CONVERGENCE = "DC_OP_CONVERGENCE_FAILURE"
    MISSING_MODEL_OR_SUBCKT = "MISSING_MODEL_OR_SUBCKT"
    MISSING_INCLUDE_FILE = "MISSING_INCLUDE_FILE"
    SWEEP_DIRECTIVE_ERROR = "SWEEP_DIRECTIVE_ERROR"
    MULTIPLE_ANALYSES = "MULTIPLE_ANALYSES"
    SYNTAX_ERROR = "SYNTAX_ERROR"
    UNKNOWN = "UNKNOWN_ERROR"


@dataclass
class DiagnosisReport:
    """SPICE simülasyon hata teşhis raporu."""
    has_error: bool
    issue_type: SPICEIssueType
    matched_error_lines: List[str] = field(default_factory=list)
    nodes_involved: List[str] = field(default_factory=list)
    components_involved: List[str] = field(default_factory=list)
    root_cause_tr: str = ""
    suggested_options: List[str] = field(default_factory=list)
    actionable_recommendations: List[str] = field(default_factory=list)
    suggested_netlist_patch: str = ""
    #: ``convreport``/``debugtran`` zorluk puanları (yalnızca logda varsa; en yüksek 5 cihaz/düğüm)
    convergence: Dict[str, Any] = field(default_factory=dict)
    #: Ayrıntı için okunacak ADI referans bölümleri (``search_ltspice_reference`` aracıyla aranabilir)
    references: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "has_error": self.has_error,
            "issue_type": self.issue_type.value,
            "matched_error_lines": self.matched_error_lines,
            "nodes_involved": self.nodes_involved,
            "components_involved": self.components_involved,
            "root_cause": self.root_cause_tr,
            "suggested_options": self.suggested_options,
            "actionable_recommendations": self.actionable_recommendations,
            "suggested_netlist_patch": self.suggested_netlist_patch,
        }
        if self.convergence:
            data["convergence"] = self.convergence
        if self.references:
            data["references"] = self.references
        return data


# Sweep (.step/.dc) reddi: LTspice simülasyonu hiç başlatmaz, .raw yazmaz (ADI Troubleshooting §Sweep Directive Errors)
_SWEEP_FIXES = [
    (re.compile(r"results in only one step", re.I),
     "Her .step en az iki farklı adım üretmeli. Tek değerle bir kez koşmak için .step yerine .param "
     "(sıcaklık için .temp) kullanın."),
    (re.compile(r"sweep spec results in only one point", re.I),
     ".dc en az iki nokta ister: list'e iki farklı değer verin ya da start ile stop'u farklı yapın."),
    (re.compile(r"only duplicate values listed", re.I),
     "list değerlerinin hepsi aynı; en az iki farklı değer verin."),
    (re.compile(r"increment must not be zero", re.I), "Artım (increment) sıfır olamaz."),
    (re.compile(r"expected list (?:numbers|of expressions/numbers) here", re.I),
     "list ve file= yalnızca düz sayı alır ({} ifadesi olmaz): '.step param R list 1k 2.2k 4.7k'."),
    (re.compile(r"dc sweep source must be an independent source", re.I),
     ".dc yalnızca bağımsız kaynak (V…/I…) ya da temp tarar. Direnç veya model parametresi taramak için "
     "'.step param' kullanın."),
]
# Yalnızca .step/.dc satırında anlamlı olan genel sözdizimi mesajları
_SWEEP_CONTEXT_FIXES = [
    (re.compile(r'expected "\(" here', re.I),
     "'.step' parametre taramasında 'param' anahtar sözcüğü zorunlu: '.step param Rx 1k 3k 1k'."),
    (re.compile(r"expected number here", re.I),
     "oct|dec|lin öğeden önce gelir: '.step dec param freq 1k 1Meg 10' (ve list ile birlikte kullanılamaz)."),
    (re.compile(r"expected expression or literal here", re.I),
     ".dc'de 'param' biçimi yoktur ve oct|dec|lin kaynak adından önce gelir ('.dc dec V1 1 100 10'). "
     "Parametre taramak için '.step param' kullanın."),
]

# ADI TROUBLESHOOTING-GUIDE.md "Improve Transient Convergence": "listed in recommended order to try", değerler 'Try'
# sütunundan. Tablonun maxstep satırının değeri devreye göre ('explicit'): seçenek değil, öneri metni olarak verilir.
# ADI'nin örneği ilk üçünü birlikte uygular: ".options method=gear itl4=50 cshunt=1f".
_TIMESTEP_OPTIONS = [
    ".options method=gear",
    ".options itl4=50",
    ".options cshunt=1e-15",
    ".options abstol=1e-10",
    ".options gshunt=1e-15",
    ".options reltol=0.005",
    ".options solver=alt",
]
_TIMESTEP_FIRST_STEP = 3
# Aynı belge "Improve DC Operating Point Convergence": "Try these in order (least to most aggressive)". Eskiden sıra bunun
# tersiydi (gshunt -> ... -> solver=alt) ve yama en agresif ikisini birlikte ekliyordu (CHANGELOG 1.4.0 açık konular).
_DC_OP_OPTIONS = [
    ".options solver=alt",
    ".options abstol=1e-10",
    ".options reltol=0.005",
    ".options gmin=1e-10",
    ".options gshunt=1e-15",
]
_OPTION_NAME_RE = re.compile(r"([A-Za-z_]\w*)\s*=")


def _next_options(ladder: List[str], existing: Set[str], first_step: int = 1) -> List[str]:
    """
    Yamaya girecek seçenekler: ``ladder`` (ADI'nin denenecek sırası) içinde netlistte henüz ayarlanmamış olanların
    ilki. Merdivenin ilk ``first_step`` seçeneğinden hiçbiri ayarlı değilse ilk adımın hepsi (geçici rejimde ADI'nin
    örneği üçünü birlikte uygular). Kullanıcının ayarladığı seçenek atlanır, ezilmez.
    """
    def names(option: str) -> Set[str]:
        return {n.lower() for n in _OPTION_NAME_RE.findall(option)}

    missing = [opt for opt in ladder if not names(opt) & existing]
    if not any(names(opt) & existing for opt in ladder[:first_step]):
        return [opt for opt in ladder[:first_step] if opt in missing]
    return missing[:1]


def _lookup_library(name: str) -> List[Dict[str, str]]:
    """Ad LTspice kütüphanesinde tanımlı mı? (Testlerde değiştirilebilsin diye ayrı fonksiyon.)"""
    try:
        from .reference import find_model_definitions
        return find_model_definitions(name)
    except Exception:
        return []


def _subckt_name_from_line(source_line: str) -> Optional[str]:
    """``X1 b 0 FOOAMP gain=2`` -> ``FOOAMP`` (parametreden önceki son belirteç)."""
    tokens = source_line.split(";", 1)[0].split()
    if len(tokens) < 2 or not tokens[0][:1].upper() == "X":
        return None
    names = [t for t in tokens[1:] if "=" not in t]
    return names[-1] if names else None


@dataclass
class _LogScan:
    """Log taramasının sonucu: görülen hata türleri, eşleşen satırlar, sorunlu düğüm ve elemanlar."""
    flags: Set[str] = field(default_factory=set)
    matched_lines: List[str] = field(default_factory=list)
    nodes: Set[str] = field(default_factory=set)
    components: Set[str] = field(default_factory=set)
    sweep_fixes: List[str] = field(default_factory=list)
    missing_names: List[str] = field(default_factory=list)
    missing_files: List[str] = field(default_factory=list)
    #: Netlistte zaten ayarlı ``.options`` adları (yama sıradaki seçeneği bunlara göre seçer; bkz. `_next_options`)
    existing_options: Set[str] = field(default_factory=set)

    def match(self, flag: str, line: str) -> None:
        self.flags.add(flag)
        if line not in self.matched_lines:
            self.matched_lines.append(line)

    @property
    def node_str(self) -> str:
        return f" ({', '.join(sorted(self.nodes))})" if self.nodes else ""

    @property
    def comp_str(self) -> str:
        return f" ({', '.join(sorted(self.components))})" if self.components else ""


@dataclass
class _Advice:
    """Bir hata türünün kök nedeni, önerileri, ADI başvuruları, netlist yaması ve denenecek ``.options`` sırası."""
    issue_type: SPICEIssueType
    root_cause: str
    recommendations: List[str]
    references: List[str]
    patch: str
    suggested_options: List[str] = field(default_factory=list)


def _convergence_summary(convergence: Dict[str, Any], scan: _LogScan) -> Dict[str, Any]:
    """
    ``convreport``/``debugtran`` sonucundaki sorunlu düğüm ve cihazları taramaya ekler (ADI'ye göre ~50'nin üstü
    sorunlu) ve rapora girecek özeti döner: en zor 5 cihaz/düğüm ile hata zamanı.
    """
    scan.nodes.update(convergence.get("trouble_nodes", []))
    scan.components.update(convergence.get("trouble_instances", []))
    for key, target in (("device_scores", scan.components), ("node_scores", scan.nodes)):
        for name, score in convergence.get(key, {}).items():
            if score >= CONVERGENCE_SCORE_THRESHOLD:
                target.add(name)
    summary: Dict[str, Any] = {
        key: dict(list(convergence[key].items())[:5]) for key in ("device_scores", "node_scores") if key in convergence
    }
    for key in ("failure_time", "failure_timestep"):
        if key in convergence:
            summary[key] = convergence[key]
    return summary


def _bleeders(nodes: Set[str]) -> List[str]:
    """Boşta düğümleri şaseye bağlayan yüksek değerli dirençler (yamada)."""
    return [f"R_bleed_{n} {n} 0 100Meg" for n in sorted(nodes)]


# --------------------------------------------------------------------------- hata türüne göre tavsiye
def _advise_multiple_analyses(scan: _LogScan, conv: Dict[str, Any]) -> _Advice:
    return _Advice(
        SPICEIssueType.MULTIPLE_ANALYSES,
        root_cause=(
            "Netlistte birden fazla analiz komutu var (LTspice: 'More than one analysis specified.'). "
            "LTspice bir koşumda yalnızca bir analiz (.tran/.ac/.dc/.op/.noise/.tf) çalıştırır ve bu durumda hiç "
            "simülasyon yapmaz."
        ),
        recommendations=[
            "Analiz komutlarından yalnızca birini bırakın; diğerlerini silin ya da ';' ile yoruma çevirin.",
            "Hem zaman hem frekans sonucu gerekiyorsa devreyi farklı circuit_name ile iki ayrı koşumda çalıştırın.",
        ],
        references=["SCHEMATIC-REFERENCE.md: Common SPICE Analysis Commands"],
        patch="* Birden fazla analiz komutu: yalnızca birini bırakın.",
    )


def _advise_sweep(scan: _LogScan, conv: Dict[str, Any]) -> _Advice:
    return _Advice(
        SPICEIssueType.SWEEP_DIRECTIVE_ERROR,
        root_cause=(
            "Bir .step/.dc tarama direktifi simülasyon başlamadan reddedildi; LTspice koşumu tamamen bıraktı "
            "(.raw yazılmadı)."
        ),
        recommendations=scan.sweep_fixes,
        references=[
            "TROUBLESHOOTING-GUIDE.md: Sweep Directive Errors",
            "SIMULATION-COMMANDS-REFERENCE.md: .STEP — Parameter Sweeps",
        ],
        patch="* .step/.dc direktifini düzeltin (bkz. actionable_recommendations).",
    )


def _advise_missing_file(scan: _LogScan, conv: Dict[str, Any]) -> _Advice:
    file_str = f" ({', '.join(scan.missing_files)})" if scan.missing_files else ""
    recommendations = [
        "Göreli yollar önce netlistin klasöründe, sonra kullanıcı kütüphanesinde (Documents\\LTspice), "
        "arama yollarında ve LTspice'ın lib/cmp, lib/sub klasörlerinde aranır; emin değilseniz mutlak yol verin.",
    ]
    for f in scan.missing_files:
        stem = re.split(r"[\\/]", f)[-1].rsplit(".", 1)[0]
        for entry in [e for e in _lookup_library(stem) if e["kind"] != "file"][:2]:
            # Otomatik yama yapılmaz: hatalı satır netlistte kaldıkça koşum yine başarısız olur
            recommendations.append(
                f"'{stem}' LTspice kütüphanesinde {entry['file']} içinde tanımlı: '{f}' satırını "
                f"'{entry['directive']}' ile değiştirin."
            )
    return _Advice(
        SPICEIssueType.MISSING_INCLUDE_FILE,
        root_cause=f".include/.lib ile istenen dosya bulunamadı{file_str}.",
        recommendations=recommendations,
        references=["SIMULATION-COMMANDS-REFERENCE.md: .LIB — Include Library"],
        patch="* .include/.lib yolunu düzeltin.",
    )


def _advise_missing_model(scan: _LogScan, conv: Dict[str, Any]) -> _Advice:
    scan.components.update(scan.missing_names)
    recommendations: List[str] = []
    patch_lines: List[str] = []
    for name in scan.missing_names:
        entries = _lookup_library(name)
        if entries:
            entry = entries[0]
            recommendations.append(
                f"'{name}' LTspice kütüphanesinde {entry['file']} içinde tanımlı: netliste "
                f"'{entry['directive']}' ekleyin."
            )
            if entry["directive"] not in patch_lines:
                patch_lines.append(entry["directive"])
        else:
            recommendations.append(
                f"'{name}' LTspice kütüphanesinde bulunamadı: üreticinin modelini .include ile ekleyin "
                "(alt devre ise eleman X ile başlamalı ve pin sırası .subckt port sırasıyla aynı olmalı)."
            )
    recommendations.append(
        "'.model 1N4148 D' gibi parametresiz bir satır gerçek parçayı değil, varsayılan parametreli ideal "
        "bir diyot tanımlar; gerçek model için standart kütüphaneyi (.lib standard.dio) kullanın."
    )
    return _Advice(
        SPICEIssueType.MISSING_MODEL_OR_SUBCKT,
        root_cause=(
            f"Tanımsız model veya alt devre{scan.comp_str}. Kullanılan eleman için .model/.subckt tanımı yok ya da "
            "kütüphane (.lib/.include) yüklenmemiş. Elle yazılan netlistlerde LTspice'ın standart modelleri "
            "(1N4148, 2N2222...) bile otomatik yüklenmez; şema (.asc) netlisteri bunları kendisi ekler."
        ),
        recommendations=recommendations,
        references=["DEVICE-MODELS-GUIDE.md: Third-Party Models", "SIMULATION-COMMANDS-REFERENCE.md: .LIB — Include Library"],
        patch="\n".join(["* Eksik model kütüphaneleri", *patch_lines]) if patch_lines else (
            "* Lütfen eksik .model veya .subckt tanımını sağlayın."
        ),
    )


def _advise_syntax(scan: _LogScan, conv: Dict[str, Any]) -> _Advice:
    return _Advice(
        SPICEIssueType.SYNTAX_ERROR,
        root_cause="SPICE netlistinde sözdizimi veya direktif hatası var.",
        recommendations=[
            "Netlist satırlarının formatını (ElemanAdı Düğüm1 Düğüm2 ... Değer) kontrol edin; hata satırı "
            "matched_error_lines içinde '| satır:' ile verilir.",
            "Direktiflerin (.tran, .ac, .meas) doğru parametrelerle yazıldığını doğrulayın.",
            "'.options reject_number_tails' açıksa sayı sonundaki her harf (5V, 100pF) sözdizimi hatasıdır; "
            "birimsiz yazın (5, 100p).",
        ],
        references=["SPICE-SYNTAX-REFERENCE.md: Engineering Multipliers"],
        patch="* Sözdizimini kontrol edin.",
    )


def _advise_floating_node(scan: _LogScan, conv: Dict[str, Any]) -> _Advice:
    return _Advice(
        SPICEIssueType.FLOATING_NODE,
        root_cause=(
            f"Düğüm(ler){scan.node_str} yalnızca bir akım kaynağına bağlı ve şaseye DC yolu yok{scan.comp_str}; "
            "çalışma noktası tanımsız. Ardından gelen yakınsama hataları bunun sonucudur."
        ),
        recommendations=[
            f"Boşta kalan düğümleri{scan.node_str} şaseye yüksek değerli bir dirençle (örn. 100Meg) bağlayın ya da "
            "kaynağın yükünü devreye ekleyin.",
            "Düğümün çalışma noktası bilerek .ic ile tanımlanıyorsa bu bir uyarıdır; aksi halde devre hatası.",
        ],
        references=["TROUBLESHOOTING-GUIDE.md: Fix the Circuit"],
        patch="* Floating Node Resolution Patch\n" + "\n".join(_bleeders(scan.nodes)),
    )


def _advise_parallel_sources(scan: _LogScan, conv: Dict[str, Any]) -> _Advice:
    return _Advice(
        SPICEIssueType.PARALLEL_VOLTAGE_SOURCES,
        root_cause=(
            f"Paralel bağlı ideal gerilim kaynakları ve/veya bobinler{scan.comp_str} aşırı tanımlı bir devre matrisi "
            "oluşturuyor. İdeal gerilim kaynakları (ve DC'de kısa devre olan bobinler) seri direnç olmadan "
            "birbirine paralel bağlanamaz."
        ),
        recommendations=[
            "Gerilim kaynaklarına seri direnç ekleyin (örn: Rser=1m) veya paralel bağlantıyı kaldırın.",
            "Bağlantı düğümlerini kontrol ederek kaynakların farklı düğümlere bağlandığından emin olun.",
            "Bobin dahilse: LTspice bobinlere varsayılan 1 mΩ seri direnç ekler; bobinde Rser=0 yazmak ya da "
            "'.options thev_induc=1' bu direnci kaldırır ve kaynak ile bobin paralel kalır (ölçüldü).",
        ],
        references=["DEVICE-MODELS-GUIDE.md: Default Parasitic Resistance"],
        patch="* Paralel gerilim kaynaklarını düzeltin veya kaynaklara seri direnç ekleyin.",
    )


def _advise_timestep(scan: _LogScan, conv: Dict[str, Any]) -> _Advice:
    when = f" (t = {conv['failure_time']:g} s)" if "failure_time" in conv else ""
    recommendations = [
        "Önce devreyi düzeltin: yüksek empedanslı düğümlere küçük kapasite (1f-1p) ekleyin; ideal anahtarlarda "
        "Vh<0 (yumuşak geçiş) kullanın; mantık kapılarına Trise=10n verin; davranışsal kaynağın çıkışını "
        "gecikmesiz kendi girişine bağlamayın; sinyal kaynaklarına 'Rser=1 Cpar=1p' ekleyin.",
        "Sorunlu cihazı bulmak için analize 'convreport' ekleyin ('.tran 10u convreport') ya da "
        "'.options debugtran' kullanın; puanı ~50'nin üstündeki cihaz/düğümler sorunludur (1'in altı önemsiz).",
        "Seçenekleri ADI'nin sırasıyla deneyin: method=gear -> itl4=50 -> cshunt=1e-15 -> abstol=1e-10 -> "
        "gshunt=1e-15 -> reltol=0.005 -> solver=alt. ADI'nin örneği ilk üçünü birlikte uygular (otomatik yama da "
        "öyle); sonrakileri tek tek ekleyin. itl4 dışındakiler doğruluktan ödün verir.",
        "maxstep: varsayılan Tstop/1024. Adımı sınırlamak gerekiyorsa değeri devrenin en hızlı olayına göre açıkça "
        "verin ('.tran Tstep Tstop 0 Tmaxstep' ya da '.options maxstep=...'; ADI tablosunda 'explicit').",
        "Gear yapay sönüm ekler ve kararsız bir devreyi kararlı gösterebilir; salınım ya da kararlılık "
        "sonucunu varsayılan trap yöntemiyle de doğrulayın. Gear simülasyonu hızlandırmaz.",
    ]
    if conv.get("device_scores"):
        top = ", ".join(f"{k}={v:g}" for k, v in conv["device_scores"].items())
        recommendations.insert(1, f"convreport puanları (cihaz): {top}.")
    patch_lines = _next_options(_TIMESTEP_OPTIONS, scan.existing_options, _TIMESTEP_FIRST_STEP)
    return _Advice(
        SPICEIssueType.TIMESTEP_TOO_SMALL,
        root_cause=(
            f"Geçici rejim (Transient) analizinde çözücü bir zaman noktasında çözüm bulamadı ve adımı sıfıra "
            f"indirdi{when}{scan.comp_str}{scan.node_str}. ADI'ye göre yaygın nedenler: I-V eğrisindeki süreksizlikler, "
            "sıfır yükselme/düşme süreleri, türevde kırılmalar, kapasitesiz çok yüksek empedanslı düğümler ve "
            "davranışsal kaynakların anlık (gecikmesiz) geri beslemesi."
        ),
        recommendations=recommendations,
        references=[
            "TROUBLESHOOTING-GUIDE.md: Improve Transient Convergence",
            "TROUBLESHOOTING-GUIDE.md: Debugging with Convergence Reports",
        ],
        patch=_patch_text("* SPICE Convergence Stabilization Patch", patch_lines),
        suggested_options=list(_TIMESTEP_OPTIONS),
    )


def _patch_text(header: str, lines: List[str]) -> str:
    """Yama metni; eklenecek satır yoksa boş (her seçenek zaten ayarlı: yama bir şey değiştirmez)."""
    return "\n".join([header, *lines]) if lines else ""


def _advise_singular_matrix(scan: _LogScan, conv: Dict[str, Any]) -> _Advice:
    return _Advice(
        SPICEIssueType.SINGULAR_MATRIX,
        root_cause=(
            f"Matris tekil (Singular Matrix). SPICE matris denkleminde sıfıra bölme oluştu{scan.node_str}. "
            "ADI'ye göre bu bir topoloji sorunudur: boşta (floating) kalan düğüm ya da gerilim kaynağı/bobin döngüsü."
        ),
        recommendations=[
            f"Tespit edilen sorunlu düğümleri{scan.node_str} şaseye (0) yüksek dirençle (örn. 100Meg veya 1G) bağlayın.",
            "Saf gerilim kaynaklarının paralel veya indüktörlerin doğrudan seri bağlanmadığından emin olun.",
            "ADI tekil matris için '.options' önermez (topoloji sorunu); devreyi düzeltin. Bulunamayan düğümde "
            "'convreport' ile sorunlu cihazı görün.",
        ],
        references=["TROUBLESHOOTING-GUIDE.md: Simulation Stops with Errors", "TROUBLESHOOTING-GUIDE.md: Fix the Circuit"],
        patch=_patch_text("* SPICE Singular Matrix Resolution Patch", _bleeders(scan.nodes)),
    )


def _advise_dc_operating_point(scan: _LogScan, conv: Dict[str, Any]) -> _Advice:
    patch_lines = _next_options(_DC_OP_OPTIONS, scan.existing_options)
    return _Advice(
        SPICEIssueType.DC_OP_CONVERGENCE,
        root_cause=(
            "DC çalışma noktası bulunamadı: LTspice doğrudan Newton, Gmin adımlama, kaynak adımlama ve sözde "
            "geçici rejim (pseudo transient) yöntemlerinin hepsini denedi ve iterasyon limitine ulaştı. Doğrusal "
            "olmayan modeller, pozitif geri besleme ya da hiç sabit noktası olmayan bir davranışsal ifade nedeni olabilir."
        ),
        recommendations=[
            "Hassas düğümlere başlangıç tahmini verin: '.nodeset V(node)=x' (yalnızca öneri) veya '.ic V(node)=x' (zorlar).",
            "'.tran 1m startup' kaynakları ilk 20 µs'de sıfırdan açarak çalışma noktasını kolaylaştırır.",
            "Seçenekleri ADI'nin sırasıyla, en az agresiften başlayarak tek tek deneyin: solver=alt (daha hassas, "
            "daha yavaş) -> abstol=1e-10 -> reltol=0.005 -> gmin=1e-10 -> gshunt=1e-15. Otomatik yama her teşhiste "
            "netlistte ayarlı olmayan sıradakini ekler.",
            "'uic' ile çalışma noktasını atlamak ADI'ye göre DC çalışma noktası sorunları için geçerli bir çözüm "
            "değildir: fiziksel olmayan bir başlangıç durumu yaratır ve sonucu bozar.",
            "Sabit noktası olmayan bir ifade (ör. 'B1 a 0 V=if(V(a)>0.5,0,1)') hiçbir seçenekle yakınsamaz; "
            "devreyi düzeltin (histerezis, gecikme ya da yumuşak geçiş ekleyin).",
        ],
        references=["TROUBLESHOOTING-GUIDE.md: Improve DC Operating Point Convergence"],
        patch=_patch_text("* SPICE DC Operating Point Convergence Patch", patch_lines),
        suggested_options=list(_DC_OP_OPTIONS),
    )


def _advise_unknown(scan: _LogScan, conv: Dict[str, Any]) -> _Advice:
    return _Advice(
        SPICEIssueType.UNKNOWN,
        root_cause="Bilinmeyen SPICE simülasyon hatası.",
        recommendations=[
            "Log çıktısını inceleyin (matched_error_lines).",
            "Yakınsama sorunu olabilir: analize 'convreport' ekleyip ('.tran 10u convreport') sorunlu "
            "cihaz/düğümleri görün; ayrıntı için search_ltspice_reference aracını kullanın.",
        ],
        references=["TROUBLESHOOTING-GUIDE.md"],
        patch="",
    )


#: Kök neden önceliği (bayrak, şu bayraklardan biri varsa atla, tavsiye): simülasyon başlamadan reddedilen netlist
#: hataları > topoloji > yakınsama. Yakınsama hatasıyla birlikte görülen sözdizimi mesajı kök neden sayılmaz.
_ADVICE_ORDER: List[Tuple[str, Tuple[str, ...], Callable[[_LogScan, Dict[str, Any]], _Advice]]] = [
    ("multi", (), _advise_multiple_analyses),
    ("sweep", (), _advise_sweep),
    ("missing_file", (), _advise_missing_file),
    ("missing_model", (), _advise_missing_model),
    ("syntax", ("timestep", "iter", "singular"), _advise_syntax),
    ("floating_error", (), _advise_floating_node),
    ("parallel_v", (), _advise_parallel_sources),
    ("timestep", (), _advise_timestep),
    ("singular", (), _advise_singular_matrix),
    ("iter", (), _advise_dc_operating_point),
]


def _advise(scan: _LogScan, conv: Dict[str, Any]) -> _Advice:
    for flag, unless, advise in _ADVICE_ORDER:
        if flag in scan.flags and not scan.flags.intersection(unless):
            return advise(scan, conv)
    return _advise_unknown(scan, conv)


class SPICEDiagnostics:
    """SPICE Hata ve Yakınsama Teşhis Motoru."""

    _TIMESTEP_RE = re.compile(r"time\s*step\s*too\s*small", re.IGNORECASE)
    _SINGULAR_RE = re.compile(r"singular\s*matrix", re.IGNORECASE)
    # Yalın "node X" alternatifi yoktur: "Node Convergence Difficulty Score:" satırından "Convergence" adında
    # sahte bir düğüm üretiyordu. Boşta düğümler _FLOATING_RE ile ayrıca yakalanır.
    _NODE_RE = re.compile(
        r"(?:check\s+node|trouble\s+with\s+node)\s+\"?([a-zA-Z0-9_\.:#]+)\"?",
        re.IGNORECASE,
    )
    _COMP_RE = re.compile(
        r"(?:trouble\s+with\s+(?:\w+-)?instance|unknown\s+subcircuit|unknown\s+model|can(?:not|'t)\s+find\s+"
        r"(?:definition\s+of\s+)?model|undefined\s+model|connected\s+to\s+current\s+source)\s+\"?([a-zA-Z0-9_\.:#§]+)\"?",
        re.IGNORECASE,
    )
    _PARALLEL_PAIR_RE = re.compile(
        r"(?:voltage source|inductor)\s+(?P<a>\S+)\s+and\s+(?:voltage source|inductor)\s+(?P<b>\S+)\s+are paralleled",
        re.IGNORECASE,
    )
    _FLOATING_RE = re.compile(r"node\s+(?P<node>\S+)\s+is\s+floating", re.IGNORECASE)
    _FLOATING_ERROR_RE = re.compile(r"error:\s*node\s+\S+\s+is\s+floating", re.IGNORECASE)
    _ITER_RE = re.compile(
        r"(?:iteration\s*limit\s*reached|no\s*dc\s*operating\s*point|failed\s*to\s*find\s*operating\s*point)",
        re.IGNORECASE,
    )
    _MODEL_RE = re.compile(
        r"(?:unknown\s+subcircuit|unknown\s+model|can(?:not|'t)\s+find\s+(?:definition\s+of\s+)?model|"
        r"could\s+not\s+find\s+(?:model|subcircuit)|undefined\s+model|sub-circuit\s+name\s+is\s+not\s+defined)",
        re.IGNORECASE,
    )
    _MISSING_FILE_RE = re.compile(r"(?:file\s+not\s+found|could\s+not\s+open)", re.IGNORECASE)
    _SYNTAX_RE = re.compile(
        r"(?:syntax\s+error|unrecognized|bad\s+syntax|questionable\s+use|expected\s+.+\s+here)",
        re.IGNORECASE,
    )
    _PARALLEL_V_RE = re.compile(
        r"(?:voltage\s+source\s+.*parallele|over-defined\s+circuit\s+matrix|loop\s+of\s+voltage\s+sources)",
        re.IGNORECASE,
    )
    _MULTI_ANALYSIS_RE = re.compile(r"more\s+than\s+one\s+analysis", re.IGNORECASE)
    _UNDEFINED_MODEL_NAME_RE = re.compile(r"undefined\s+model\s+\"([^\"]+)\"", re.IGNORECASE)
    #: Parser'ın derleyici mesajına eklediği kaynak satır: "...net(8): mesaj | satır: .step param Rx list 1k"
    _SOURCE_LINE_RE = re.compile(r"\|\s*satır:\s*(?P<src>.+)$")
    #: Mesajda görülünce yalnızca bayrak koyan hata türleri
    _FLAG_PATTERNS = (
        ("timestep", _TIMESTEP_RE),
        ("singular", _SINGULAR_RE),
        ("parallel_v", _PARALLEL_V_RE),
        ("iter", _ITER_RE),
        ("multi", _MULTI_ANALYSIS_RE),
        ("floating_error", _FLOATING_ERROR_RE),
        ("syntax", _SYNTAX_RE),
    )

    @classmethod
    def _source_line(cls, line: str) -> str:
        m = cls._SOURCE_LINE_RE.search(line)
        return m.group("src").strip() if m else ""

    @classmethod
    def diagnose(
        cls,
        log_content: str,
        netlist_code: Optional[str] = None,
        errors_list: Optional[List[str]] = None,
    ) -> DiagnosisReport:
        """
        Log çıktısını ve varsa hata listesini analiz ederek teşhis raporu döner.

        Öncelik: simülasyon başlamadan reddedilen netlist hataları (birden fazla analiz, .step/.dc, eksik dosya,
        tanımsız model, sözdizimi) > topoloji (akım kaynağına bağlı boşta düğüm, paralel kaynak) > yakınsama
        (time step, singular matrix, DC çalışma noktası). Bkz. `_ADVICE_ORDER`.
        """
        all_lines = (errors_list or []) + log_content.splitlines()
        scan = cls._scan(all_lines)
        scan.existing_options = cls._existing_option_names(netlist_code) if netlist_code else set()
        conv_summary = _convergence_summary(parse_convergence_report("\n".join(all_lines)), scan)

        if not scan.matched_lines and not errors_list:
            return DiagnosisReport(
                has_error=False,
                issue_type=SPICEIssueType.UNKNOWN,
                root_cause_tr="Simülasyonda herhangi bir yakınsama veya sözdizimi hatası tespit edilmedi.",
            )

        advice = _advise(scan, conv_summary)
        if scan.nodes and advice.issue_type not in (SPICEIssueType.FLOATING_NODE, SPICEIssueType.SINGULAR_MATRIX):
            floating = sorted(m.group("node") for l in all_lines for m in cls._FLOATING_RE.finditer(l))
            if floating:
                advice.recommendations.append(f"LTspice boşta düğüm bildirdi: {', '.join(dict.fromkeys(floating))}.")

        return DiagnosisReport(
            has_error=True,
            issue_type=advice.issue_type,
            matched_error_lines=scan.matched_lines or (errors_list or []),
            nodes_involved=sorted(scan.nodes),
            components_involved=sorted(scan.components),
            root_cause_tr=advice.root_cause,
            suggested_options=advice.suggested_options,
            actionable_recommendations=advice.recommendations,
            suggested_netlist_patch=advice.patch,
            convergence=conv_summary,
            references=advice.references,
        )

    @classmethod
    def _scan(cls, all_lines: List[str]) -> _LogScan:
        """Her mesajı bir kez tarar: hata türleri, eşleşen satırlar, sorunlu düğüm/elemanlar, eksik model ve dosyalar."""
        scan = _LogScan()
        seen_messages: Set[str] = set()
        for raw in all_lines:
            line = raw.strip()
            if not line:
                continue
            source = cls._source_line(line)
            message = line.split(" | satır:", 1)[0]
            # Parser'ın kaynak satırla zenginleştirdiği hata (errors_list) ile ham logdaki aynı satır bir kez sayılır
            if message in seen_messages:
                continue
            seen_messages.add(message)
            is_sweep_line = bool(re.match(r"\.(?:step|dc)\b", source, re.IGNORECASE))

            for flag, regex in cls._FLAG_PATTERNS:
                if regex.search(message):
                    scan.match(flag, line)
            for regex, fix in _SWEEP_FIXES + (_SWEEP_CONTEXT_FIXES if is_sweep_line else []):
                if regex.search(message):
                    scan.match("sweep", line)
                    if fix not in scan.sweep_fixes:
                        scan.sweep_fixes.append(fix)
            if cls._MODEL_RE.search(message):
                scan.match("missing_model", line)
                undefined = cls._UNDEFINED_MODEL_NAME_RE.search(message)
                name = undefined.group(1) if undefined else _subckt_name_from_line(source)
                if name and name not in scan.missing_names:
                    scan.missing_names.append(name)
            if cls._MISSING_FILE_RE.search(message) and re.match(r"\.(?:inc|include|lib)\b", source, re.I):
                scan.match("missing_file", line)
                parts = source.split(None, 1)
                if len(parts) > 1:
                    scan.missing_files.append(parts[1].strip().strip('"'))
            cls._collect_names(message, scan)
        return scan

    @classmethod
    def _collect_names(cls, message: str, scan: _LogScan) -> None:
        """Mesajdaki sorunlu düğüm ve eleman adları (boşta düğüm, ``check node``, ``trouble with instance``...)."""
        for m in cls._FLOATING_RE.finditer(message):
            scan.nodes.add(m.group("node"))
        for m in cls._NODE_RE.finditer(message):
            node_name = m.group(1).strip()
            if node_name.lower() not in ("is", "at", "to", "the", "and", "of"):
                scan.nodes.add(node_name)
        for m in cls._COMP_RE.finditer(message):
            scan.components.add(m.group(1).strip())
        for m in cls._PARALLEL_PAIR_RE.finditer(message):
            scan.components.update((m.group("a"), m.group("b")))

    _OPTION_LINE_RE = re.compile(r"^\s*\.(?:options?|opt)\b", re.IGNORECASE)
    _OPTION_NAME_RE = _OPTION_NAME_RE

    @classmethod
    def _existing_option_names(cls, netlist_code: str) -> Set[str]:
        """Netlistte ``.options`` ile (devam satırları dahil) zaten ayarlanmış seçenek adları."""
        names: Set[str] = set()
        in_option_block = False
        for line in netlist_code.splitlines():
            if cls._OPTION_LINE_RE.match(line):
                in_option_block = True
            elif not (in_option_block and line.lstrip().startswith("+")):
                in_option_block = False
                continue
            code = line.split(";", 1)[0]
            names.update(n.lower() for n in cls._OPTION_NAME_RE.findall(code))
        return names

    @classmethod
    def apply_diagnostic_patch(cls, netlist_code: str, report: DiagnosisReport) -> str:
        """
        Önerilen çözümleri (``.options`` satırları, ``R_bleed`` dirençleri ve eksik ``.lib`` satırları)
        netlist dosyasının '.end' satırından hemen öncesine ekler.

        Yalnızca yamadaki (``suggested_netlist_patch``) seçenekler eklenir; ``suggested_options`` listesinin geri
        kalanı denenecek sıradır. Kullanıcının netlistte zaten ayarladığı bir seçenek (örn. ``gmin=1e-12``) farklı
        bir değerle ikinci kez eklenmez; ``.end ; yorum`` biçimindeki bitiş satırı da tanınır.
        """
        if not report.has_error or not report.suggested_netlist_patch.strip():
            return netlist_code

        existing_options = cls._existing_option_names(netlist_code)
        existing_lines = {l.strip().lower() for l in netlist_code.splitlines()}
        patch_lines = [p.strip() for p in report.suggested_netlist_patch.splitlines()]

        options_to_add: List[str] = []
        for opt_clean in (p for p in patch_lines if cls._OPTION_LINE_RE.match(p)):
            opt_names = {n.lower() for n in cls._OPTION_NAME_RE.findall(opt_clean)}
            if opt_clean.lower() in existing_lines or (opt_names & existing_options):
                continue
            options_to_add.append(opt_clean)

        existing_elements = {
            l.split()[0].lower() for l in netlist_code.splitlines() if l.strip() and l.strip()[0] not in "*;."
        }
        bleeders = [
            p for p in patch_lines if p.startswith("R_bleed") and p.split()[0].lower() not in existing_elements
        ]
        libs = [p for p in patch_lines if re.match(r"\.(?:lib|include|inc)\s", p, re.I) and p.lower() not in existing_lines]

        if not options_to_add and not bleeders and not libs:
            return netlist_code

        return insert_before_end(
            netlist_code,
            ["", "* Auto-Applied Diagnostics Fixes", *libs, *options_to_add, *bleeders],
        )
