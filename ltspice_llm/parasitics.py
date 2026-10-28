"""
Parasitic Modeling & Derating Engine
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Baskı devre (PCB) izleri, MLCC seramik kondansatörlerin DC-bias kapasite kaybı,
kablo endüktansı ve hot-plug (takma anı) LC aşırı gerilim analizi için
hesaplama ve netlist enjeksiyon modülü.

Her sabit yerel kaynaktan (``docs/references/text``, sayfa ``=== s.N ===``) gelir; kaynağı olmayan değer girdi olarak
istenir:

- ADI MT-094 "Microstrip and Stripline Design" s.3-4 (IPC-2141 denklemleri): microstrip Z0, C0, tpd ve doğruluk sınırı.
- ADI Basic Linear Design Bl.12 "Printed Circuit Board Design Issues" s.7-8: bakır özdirenci, 1 oz kalınlık, TC.
- TI SLOD006 "Op Amps for Everyone" s.362: tel ve iz endüktansı 6-12 nH/cm.
- ADI (Linear Technology) AN-88 "Ceramic Input Capacitors Can Cause Overvoltage Transients" s.1-4: takma anı
  seri RLC modeli, ölçülen tepeler, kritik sönüm örneği.
- ADI MT-101 "Decoupling Techniques" s.6: NP0/C0G'nin düşük gerilim katsayısı.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple, Union

from .netlist import insert_before_end
from .quantizer import format_spice_value, parse_spice_value


def _spice(value: float, unit: str = "") -> str:
    """Netliste yazılan değer: 6 anlamlı hane (varsayılan 3 hane modeli yuvarlıyordu; ölçüldü: 50 mm iz R_dc'si
    0.09696 yerine 0.0970 Ohm). Görüntüleme alanları (to_dict) 3 haneyle kalır."""
    return format_spice_value(value, precision=6, unit=unit)


# --------------------------------------------------------------------------- 1. PCB İz Parazitikleri (IPC-2141)
#: ADI Basic Linear Design Bl.12 s.7: "At 25°C the resistivity of pure copper is 1.724X10-6 Ω·cm"
COPPER_RESISTIVITY_25C = 1.724e-8  # Ohm*m
#: Aynı belge s.8: "the temperature coefficient of resistance for copper is about 0.4%/°C around room temperature"
COPPER_TEMPCO = 0.004  # 1/°C
#: 1 oz bakır kalınlığı: MT-094 s.3 "1 ounce (T=1.4)" mil; Bl.12 s.7 "0.036 mm (0.0014")"
COPPER_MM_PER_OZ = 1.4 * 0.0254
#: MT-094 s.4: "The IPC microstrip equation is most accurate between 50 and 100 Ω"
MICROSTRIP_ACCURATE_Z0 = (50.0, 100.0)


@dataclass
class TraceParasitics:
    """Bir mikroşerit PCB izinin eşdeğer RLC ve iletim hattı parametreleri."""
    r_dc: float          # DC direnci (Ohm)
    l_trace: float       # Toplam seri endüktans (Henry)
    c_trace: float       # Toplam şönt kapasitans (Farad)
    z0: float            # Karakteristik empedans (Ohm)
    tpd: float           # Yayılma gecikmesi (saniye)
    length_mm: float     # İz uzunluğu (mm)
    width_mm: float      # İz genişliği (mm)
    r_per_meter: float   # Ohm/m
    l_per_meter: float   # H/m
    c_per_meter: float   # F/m
    accuracy_note: str = ""  # Z0 IPC denkleminin en doğru olduğu aralığın dışındaysa açıklama

    def to_dict(self) -> Dict[str, Any]:
        return {
            "r_dc_ohm": self.r_dc,
            "l_trace_nh": self.l_trace * 1e9,
            "c_trace_pf": self.c_trace * 1e12,
            "z0_ohm": self.z0,
            "tpd_ps": self.tpd * 1e12,
            "length_mm": self.length_mm,
            "width_mm": self.width_mm,
            "accuracy_note": self.accuracy_note,
        }

    def to_spice_subckt(self, subckt_name: str = "PCB_TRACE", nodes: Tuple[str, str, str] = ("in", "out", "gnd")) -> str:
        """
        İzi SPICE alt devresi (.subckt in out gnd) olarak biçimlendirir (T-tipi RLC hattı). Bobinlere ``Rser=0``
        yazılır: LTspice'ın varsayılan bobin seri direnci 1 mΩ'dur (ADI CIRCUIT-ELEMENTS-REFERENCE) ve modelin R'sine
        sessizce eklenirdi.
        """
        r_half = self.r_dc / 2.0
        l_half = self.l_trace / 2.0
        n_in, n_out, gnd = nodes
        return (
            f".subckt {subckt_name} {n_in} {n_out} {gnd}\n"
            f"R1 {n_in} n1 {_spice(r_half, unit='R')}\n"
            f"L1 n1 n_mid {_spice(l_half, unit='H')} Rser=0\n"
            f"C1 n_mid {gnd} {_spice(self.c_trace, unit='F')}\n"
            f"L2 n_mid n2 {_spice(l_half, unit='H')} Rser=0\n"
            f"R2 n2 {n_out} {_spice(r_half, unit='R')}\n"
            f".ends {subckt_name}\n"
        )


def calculate_trace_parasitics(
    length_mm: float,
    width_mm: float = 0.254,     # girdi (10 mil)
    height_mm: float = 1.6,      # girdi: dielektrik kalınlığı, iz ile referans düzlemi arası
    copper_oz: float = 1.0,      # 1 oz = 1.4 mil (MT-094 s.3)
    er: float = 4.0,             # MT-094 s.3-4 örneğindeki FR-4 değeri; kendi laminatınızınkini verin
    temp_c: float = 25.0,
) -> TraceParasitics:
    """
    Yüzey microstrip izinin R, L, C, Z0 ve tpd değerlerini hesaplar.

    - Z0 (MT-094 Denk. 3, IPC-2141): ``87 / sqrt(er + 1.41) * ln(5.98 H / (0.8 W + T))`` Ohm.
    - C0 (MT-094 Denk. 4): ``0.67 (er + 1.41) / ln(5.98 H / (0.8 W + T))`` pF/in; L0 = Z0^2 * C0 (kayıpsız hat).
    - tpd (MT-094 Denk. 5): ``1.017 * sqrt(0.475 er + 0.67)`` ns/ft.
    - R_dc (Bl.12 s.7-8): ``rho * l / (W T)``, rho = 1.724e-8 Ohm*m (25 °C), TC %0.4/°C.

    MT-094 s.4: denklem 50-100 Ohm arasında en doğrudur; dışında ``accuracy_note`` doldurulur. ``5.98 H <= 0.8 W + T``
    olduğunda logaritma sıfır ya da negatiftir (iz yüksekliğine göre çok geniş): denklem geçersiz, ValueError.

    :param length_mm: İz uzunluğu (mm).
    :param width_mm: İz genişliği W (mm).
    :param height_mm: Dielektrik kalınlığı H (mm).
    :param copper_oz: Bakır ağırlığı (oz); kalınlık T = copper_oz * 1.4 mil.
    :param er: Dielektrik bağıl geçirgenliği.
    :param temp_c: Bakır sıcaklığı (°C), yalnızca R_dc'yi etkiler.
    """
    if length_mm <= 0 or width_mm <= 0 or height_mm <= 0 or copper_oz <= 0 or er <= 1.0:
        raise ValueError("Tüm fiziksel boyutlar ve malzeme sabitleri pozitif ve geçerli olmalıdır.")

    t_mm = copper_oz * COPPER_MM_PER_OZ
    length_m = length_mm * 1e-3

    rho = COPPER_RESISTIVITY_25C * (1.0 + COPPER_TEMPCO * (temp_c - 25.0))
    r_per_m = rho / ((width_mm * 1e-3) * (t_mm * 1e-3))

    arg = (5.98 * height_mm) / (0.8 * width_mm + t_mm)
    if arg <= 1.0:
        raise ValueError(
            f"IPC-2141 microstrip denklemi bu geometride geçersiz: 5.98·H = {5.98 * height_mm:.4g} mm <= "
            f"0.8·W + T = {0.8 * width_mm + t_mm:.4g} mm (iz, dielektrik kalınlığına göre çok geniş)."
        )
    log_term = math.log(arg)
    z0 = (87.0 / math.sqrt(er + 1.41)) * log_term
    c_per_m = (0.67 * (er + 1.41) / log_term) * 1e-12 / 0.0254   # pF/in -> F/m
    l_per_m = z0 * z0 * c_per_m
    tpd_s_per_m = 1.017 * math.sqrt(0.475 * er + 0.67) * 1e-9 / 0.3048  # ns/ft -> s/m

    lo, hi = MICROSTRIP_ACCURATE_Z0
    note = ""
    if not lo <= z0 <= hi:
        note = (f"Z0 = {z0:.1f} Ohm, IPC denkleminin en doğru olduğu {lo:g}-{hi:g} Ohm aralığının dışında "
                "(MT-094 s.4); alan çözücüsüyle ya da üreticinin empedans hesabıyla doğrulayın.")

    return TraceParasitics(
        r_dc=r_per_m * length_m,
        l_trace=l_per_m * length_m,
        c_trace=c_per_m * length_m,
        z0=z0,
        tpd=tpd_s_per_m * length_m,
        length_mm=length_mm,
        width_mm=width_mm,
        r_per_meter=r_per_m,
        l_per_meter=l_per_m,
        c_per_meter=c_per_m,
        accuracy_note=note,
    )


# --------------------------------------------------------------------------- 2. MLCC DC-Bias Kapasite Kaybı
#: Sınıf 1 dielektrikler. MT-101 s.6: "NP0 (also called COG) ... have nominally zero TC, plus a low voltage
#: coefficient (unlike the less stable high-K types)". Kayıp ihmal edilir (varsayım, sonuçta not olarak yazılır).
_CLASS1 = ("C0G", "NP0", "NPO", "COG")


@dataclass
class MlccDeratingResult:
    """MLCC kapasitörünün DC bias altındaki efektif değeri ve (verildiyse) parazitikleri."""
    nominal_capacitance: float
    effective_capacitance: float
    loss_percent: float
    applied_voltage: float
    rated_voltage: float
    dielectric: str
    package: str
    typical_esr: Optional[float]   # Ohm; girdi (datasheet), verilmediyse None
    typical_esl: Optional[float]   # H; girdi (datasheet), verilmediyse None
    srf_hz: Optional[float]        # Öz rezonans frekansı; yalnızca ESL verildiyse
    source: str = ""               # efektif değerin dayanağı (eğri ya da sınıf 1 varsayımı)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nominal_c_str": format_spice_value(self.nominal_capacitance, unit="F"),
            "effective_c_str": format_spice_value(self.effective_capacitance, unit="F"),
            "loss_percent": round(self.loss_percent, 2),
            "applied_voltage": self.applied_voltage,
            "rated_voltage": self.rated_voltage,
            "dielectric": self.dielectric,
            "package": self.package,
            "esr_mohm": None if self.typical_esr is None else round(self.typical_esr * 1e3, 2),
            "esl_nh": None if self.typical_esl is None else round(self.typical_esl * 1e9, 2),
            "srf_mhz": None if self.srf_hz is None else round(self.srf_hz / 1e6, 2),
            "source": self.source,
        }

    def to_spice_string(self) -> str:
        """LTspice eleman değeri (örn: '4.7u Rser=15m Lser=850p'); Rser/Lser yalnızca verildiyse yazılır."""
        parts = [_spice(self.effective_capacitance)]
        if self.typical_esr is not None:
            parts.append(f"Rser={_spice(self.typical_esr)}")
        if self.typical_esl is not None:
            parts.append(f"Lser={_spice(self.typical_esl)}")
        return " ".join(parts)


def _interpolate_curve(curve: Sequence[Tuple[float, float]], voltage: float) -> float:
    points = sorted((float(v), float(r)) for v, r in curve)
    if len(points) < 2:
        raise ValueError("DC-bias eğrisi en az iki (gerilim, C/C_nom) noktası içermelidir.")
    if any(r <= 0 for _, r in points):
        raise ValueError("DC-bias eğrisindeki kapasite oranları pozitif olmalıdır.")
    if not points[0][0] <= voltage <= points[-1][0]:
        raise ValueError(
            f"Uygulanan gerilim {voltage:g} V eğrinin aralığı dışında ({points[0][0]:g}-{points[-1][0]:g} V); "
            "eğriyi dışa uzatmak yerine üreticinin verisini genişletin."
        )
    for (v0, r0), (v1, r1) in zip(points, points[1:]):
        if v0 <= voltage <= v1:
            return r0 if v1 == v0 else r0 + (r1 - r0) * (voltage - v0) / (v1 - v0)
    return points[-1][1]


def mlcc_dc_bias_derating(
    nominal_capacitance: Union[str, float],
    applied_voltage: float,
    rated_voltage: float,
    dielectric: str = "X7R",
    package: str = "0805",
    dc_bias_curve: Optional[Sequence[Tuple[float, float]]] = None,
    esr_ohm: Optional[float] = None,
    esl_h: Optional[float] = None,
) -> MlccDeratingResult:
    """
    MLCC'nin uygulanan DC gerilimdeki efektif kapasitesini üreticinin DC-bias eğrisinden hesaplar.

    DC-bias kaybı parçaya özgüdür (dielektrik, kılıf, anma gerilimi, nominal değer); dielektrik ve kılıftan genel bir
    tablo türetilemez. AN-88 s.2 tek bir örnek verir: "Y5V ceramic capacitors will loose 80% of the initial capacitance
    under rated input voltage". Bu yüzden sınıf 2 (X7R, X5R, Y5V...) için ``dc_bias_curve`` zorunludur:
    ``[(gerilim V, C/C_nom), ...]`` (üretici aracı ya da datasheet eğrisi); aradaki gerilimler doğrusal enterpolasyonla
    okunur, eğri dışı ValueError. Sınıf 1 (C0G/NP0) için eğri verilmezse kayıp ihmal edilir (MT-101 s.6: düşük
    gerilim katsayısı).

    :param nominal_capacitance: Nominal etiket değeri (örn: '10u', '100n' veya float).
    :param applied_voltage: Kondansatör uçlarındaki DC çalışma gerilimi (Volt).
    :param rated_voltage: Anma gerilimi (Volt); uygulanan gerilim bunu aşarsa ValueError.
    :param dielectric: 'C0G', 'NP0', 'X7R', 'X5R', 'Y5V' ...
    :param package: Kılıf kodu (bilgi amaçlı, sonuca yazılır).
    :param dc_bias_curve: Üreticinin DC-bias eğrisi [(V, C/C_nom), ...].
    :param esr_ohm: Datasheet ESR'si (isteğe bağlı).
    :param esl_h: Datasheet ESL'si (isteğe bağlı); verilirse öz rezonans frekansı hesaplanır.
    """
    nom_f = parse_spice_value(str(nominal_capacitance)) if isinstance(nominal_capacitance, str) else float(nominal_capacitance)
    if nom_f <= 0:
        raise ValueError("Nominal kapasitans pozitif olmalıdır.")
    if rated_voltage <= 0:
        raise ValueError("Anma gerilimi (rated_voltage) pozitif olmalıdır.")
    applied = abs(float(applied_voltage))
    if applied > rated_voltage:
        raise ValueError(f"Uygulanan gerilim ({applied:g} V) anma gerilimini ({rated_voltage:g} V) aşıyor.")

    d_upper = dielectric.upper()
    if dc_bias_curve is not None:
        retention = _interpolate_curve(dc_bias_curve, applied)
        source = "üreticinin DC-bias eğrisi (girdi), doğrusal enterpolasyon"
    elif d_upper in _CLASS1:
        retention = 1.0
        source = "sınıf 1 dielektrik: DC-bias kaybı ihmal edildi (MT-101 s.6: düşük gerilim katsayısı; varsayım)"
    else:
        raise ValueError(
            f"{d_upper} için DC-bias kaybı parçaya özgüdür; üreticinin eğrisini dc_bias_curve=[(V, C/C_nom), ...] "
            "olarak verin (genel tablo yok; AN-88 s.2: Y5V anma geriliminde %80 kaybeder)."
        )

    eff_f = nom_f * retention
    srf = None
    if esl_h is not None and esl_h > 0:
        srf = 1.0 / (2.0 * math.pi * math.sqrt(esl_h * eff_f))

    return MlccDeratingResult(
        nominal_capacitance=nom_f,
        effective_capacitance=eff_f,
        loss_percent=(1.0 - retention) * 100.0,
        applied_voltage=applied,
        rated_voltage=rated_voltage,
        dielectric=d_upper,
        package=str(package).strip(),
        typical_esr=esr_ohm,
        typical_esl=esl_h,
        srf_hz=srf,
        source=source,
    )


# --------------------------------------------------------------------------- 3. Kablo Endüktansı
#: TI SLOD006 s.362: "Common wires and PCB traces vary between 6 nH and 12 nH per centimeter."
WIRE_NH_PER_CM_RANGE = (6.0, 12.0)


@dataclass
class CableParasitics:
    """Gidiş + dönüş iletkenli kablonun toplam seri R ve L'si (ve verildiyse iletkenler arası C)."""
    length_m: float
    r_cable: float
    l_cable: float
    c_cable: float = 0.0

    def to_spice_subckt(self, subckt_name: str = "CABLE",
                        nodes: Tuple[str, str, str, str] = ("inp", "inn", "outp", "outn")) -> str:
        """Her iletkene R/2 + L/2 (bobinlerde ``Rser=0``: LTspice varsayılanı 1 mΩ); R ya da C sıfırsa o eleman
        yazılmaz (LTspice'a 0 Ohm/0 F verilmez)."""
        p_in, n_in, p_out, n_out = nodes
        lines = [f".subckt {subckt_name} {p_in} {n_in} {p_out} {n_out}"]
        for tag, start, end in (("p", p_in, p_out), ("n", n_in, n_out)):
            node = start
            if self.r_cable > 0:
                lines.append(f"R_{tag} {node} m_{tag} {_spice(self.r_cable / 2.0, unit='R')}")
                node = f"m_{tag}"
            lines.append(f"L_{tag} {node} {end} {_spice(self.l_cable / 2.0, unit='H')} Rser=0")
        if self.c_cable > 0:
            lines.append(f"C_p {p_out} {n_out} {_spice(self.c_cable, unit='F')}")
        lines.append(f".ends {subckt_name}")
        return "\n".join(lines) + "\n"


def get_cable_parasitics(
    length_m: float = 1.0,
    nh_per_cm: float = WIRE_NH_PER_CM_RANGE[1],
    resistance_ohm: float = 0.0,
    capacitance_f: float = 0.0,
) -> CableParasitics:
    """
    Kablonun takma anı modelindeki seri endüktansı: ``L = 2 * uzunluk * nh_per_cm`` (gidiş + dönüş iletkeni).

    ``nh_per_cm`` varsayılanı SLOD006 s.362 aralığının (6-12 nH/cm) üst ucudur; iletkenler arası karşılıklı kuplaj
    ihmal edilir, yani döngü endüktansı büyük tahmin edilir: takma anı aşımı için tutucu (büyük L, küçük sönüm).
    Ölçülmüş değer varsa doğrudan `analyze_hotplug_transient`'e verin. Direnç verilmezse 0 (sönümsüz kötü durum).
    """
    if length_m <= 0 or nh_per_cm <= 0 or resistance_ohm < 0 or capacitance_f < 0:
        raise ValueError("length_m ve nh_per_cm pozitif, resistance_ohm ve capacitance_f negatif olmamalıdır.")
    return CableParasitics(
        length_m=float(length_m),
        r_cable=float(resistance_ohm),
        l_cable=2.0 * float(length_m) * 100.0 * float(nh_per_cm) * 1e-9,
        c_cable=float(capacitance_f),
    )


# --------------------------------------------------------------------------- 4. Hot-Plug LC Aşırı Gerilim (AN-88)
#: Bulk sönüm kondansatörünün C_in'e oranı için varsayılan tasarım seçimi (girdi). Yalnız paralel RC kolu ideal
#: modelde kritik sönüm vermez; LTspice 26.1.1 taraması (seri R = 0, en iyi ESR; examples/15 T4): oran 2.136 ->
#: tepe 1.51·V_in, 4 -> 1.37·V_in, 6 -> 1.29·V_in. AN-88 s.4'ün 47 µF / 0.44 Ohm örneği donanımda 25 V / 24 V verdi
#: (Tablo 3 Ch1); ideal modelde aynı devre 1.58·V_in'dir (examples/15 T2), fark devrenin diğer kayıplarından.
DEFAULT_BULK_RATIO = 4.0

_IDEAL_MODEL_NOTE = (
    "İdeal doğrusal model: sönümsüz seri RLC'nin tepesi 2·V_in'i aşamaz. AN-88 Tablo 1'de donanımda 24 V girişte "
    "57.2 V (2.38 kat) ölçüldü; seramik kapasitenin DC-bias kaybı, ESR'nin kenara bağlılığı ve endüktans doyması "
    "(AN-88 s.2) gerçek tepeyi büyütür. Sonucu DC-bias'a göre düşürülmüş C ile LTspice'ta ve donanımda doğrulayın."
)


def hotplug_step_peak(v_in: float, l_h: float, c_f: float, r_ohm: float = 0.0,
                      bulk_c: Optional[float] = None, bulk_esr: Optional[float] = None) -> float:
    """
    İdeal basamak yanıtının tepesi: V_in -> (R + sL) -> C_in, isteğe bağlı paralel (C_b + ESR) kolu.

    Transfer fonksiyonu ``H = (1 + s Rb Cb) / ((R + sL)(C Rb Cb s^2 + (C + Cb) s) + 1 + s Rb Cb)``. Adım yanıtı en
    yavaş LC periyodunun 10 katı boyunca kaba ızgarada (periyot başına 80 örnek) okunur; tepe anı, türevin (dürtü yanıtı
    ``C e^{At} B``) sıfır olduğu yer olarak o aralıkta ``brentq`` ile bulunur ve tepe durum uzayından tam hesaplanır.
    LTspice 26.1.1 ile doğrulaması tests/test_integration_ltspice.py ve examples/15.
    """
    import numpy as np
    from scipy import linalg, optimize, signal

    # Normalize birimler (zaman sqrt(L C), empedans Z0 = sqrt(L/C)): L = C = 1. SI katsayılar (~1e-18) durum matrisini
    # kötü koşullandırıyordu; yanıt zaten yalnızca R/Z0, C_b/C ve ESR/Z0 oranlarına bağlıdır.
    z0 = math.sqrt(l_h / c_f)
    r = r_ohm / z0
    cb = float(bulk_c or 0.0) / c_f
    rb = float(bulk_esr or 0.0) / z0
    if cb > 0 and rb == 0:
        # ESR'siz kol C_in'e paraleldir: tek kondansatör (pay katsayısı 0 scipy'de BadCoefficients veriyordu)
        num, den = [1.0], [1.0 + cb, r * (1.0 + cb), 1.0]
    elif cb > 0:
        a = rb * cb
        num = [rb * cb, 1.0]
        den = [a, (1.0 + cb) + r * a, r * (1.0 + cb) + rb * cb, 1.0]
    else:
        num, den = [1.0], [1.0, r, 1.0]
    A, B, C, _ = signal.tf2ss(num, den)  # kesin uygun (pay derecesi < payda): D = 0
    a_inv = linalg.inv(A)
    eye = np.eye(A.shape[0])

    def step(t: float) -> float:
        return float((C @ a_inv @ (linalg.expm(A * t) - eye) @ B).item())

    def slope(t: float) -> float:
        return float((C @ linalg.expm(A * t) @ B).item())

    period = 2.0 * math.pi * math.sqrt(1.0 + cb)
    t = np.linspace(0.0, 10.0 * period, 801)
    _, y = signal.step((num, den), T=t)
    i = int(np.argmax(y))
    peak = float(y[i])
    if 0 < i < len(t) - 1 and slope(t[i - 1]) > 0 > slope(t[i + 1]):
        peak = max(peak, step(optimize.brentq(slope, t[i - 1], t[i + 1], xtol=period * 1e-12)))
    return v_in * peak


@dataclass
class HotplugAnalysis:
    """
    AN-88 uyarınca kablo endüktansı ve giriş seramik kapasitansının takılma anı LC rezonans analizi.
    """
    v_in: float
    v_peak: float
    overshoot_percent: float
    z0: float
    damping_ratio: float
    is_underdamped: bool
    recommended_damping_r: float   # kritik sönüm (zeta = 1) için eklenecek seri direnç: max(0, 2*Z0 - R)
    recommended_bulk_c: float      # bulk_ratio * C_in (tasarım seçimi, varsayılan 4)
    recommended_bulk_esr: float    # bu C_b ile ideal tepeyi en aza indiren ESR
    safe_margin_rating: str        # "EXCEEDS_RATING" | "WITHIN_RATING" | "NO_RATING"
    max_voltage: Optional[float] = None
    recommended_damped_peak: Optional[float] = None  # önerilen C_b + ESR ile ideal tepe
    note: str = _IDEAL_MODEL_NOTE

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_voltage": self.v_in,
            "theoretical_peak_voltage": round(self.v_peak, 3),
            "overshoot_percent": round(self.overshoot_percent, 1),
            "characteristic_impedance_ohm": round(self.z0, 3),
            "damping_ratio": round(self.damping_ratio, 4),
            "is_underdamped": self.is_underdamped,
            "recommended_series_r_ohm": round(self.recommended_damping_r, 3),
            "recommended_bulk_c_str": format_spice_value(self.recommended_bulk_c, unit="F"),
            "recommended_bulk_esr_str": format_spice_value(self.recommended_bulk_esr, unit="R"),
            "recommended_damped_peak_voltage": None if self.recommended_damped_peak is None
            else round(self.recommended_damped_peak, 3),
            "assessment": self.safe_margin_rating,
            "max_voltage": self.max_voltage,
            "note": self.note,
        }


def analyze_hotplug_transient(
    v_in: float,
    c_in: Union[str, float],
    l_cable: Union[str, float],
    r_cable: Union[str, float] = 0.0,
    max_voltage: Optional[float] = None,
    bulk_ratio: float = DEFAULT_BULK_RATIO,
) -> HotplugAnalysis:
    """
    Takma anını AN-88'in seri rezonans devresiyle (s.1: L_OUT, C_IN ve toplu ESR) modelleyip ideal adım yanıtının
    tepesini hesaplar: zeta = (R/2)*sqrt(C/L); zeta < 1 iken ``V_peak = V_in (1 + exp(-pi zeta / sqrt(1 - zeta^2)))``
    (LTspice ile doğrulama: examples/15).

    Öneriler: kritik sönüm için eklenecek seri direnç ``2*Z0 - R`` (zeta = 1); paralel sönüm kolu için
    ``C_b = bulk_ratio * C_in`` ve bu C_b ile ideal tepeyi en aza indiren ESR (`hotplug_step_peak` üzerinde tek
    boyutlu arama), ``recommended_damped_peak`` o tepedir. Yalnız paralel kol kritik sönüm vermez (bkz.
    `DEFAULT_BULK_RATIO`).

    Değerlendirme: ``max_voltage`` (girişe bağlı parçanın mutlak maksimum gerilimi) verilirse V_peak ile karşılaştırılır;
    verilmezse "NO_RATING". İdeal modelin sınırı ``note``'tadır (AN-88 Tablo 1: donanımda 2.38 kat).

    :param v_in: Giriş besleme gerilimi (V).
    :param c_in: Girişteki seramik kapasite toplamı (DC-bias'a göre düşürülmüş değer verin).
    :param l_cable: Kablo + konnektör seri endüktansı.
    :param r_cable: Toplam seri direnç (kablo + ESR); varsayılan 0 = sönümsüz kötü durum.
    :param max_voltage: Girişe bağlı parçanın mutlak maksimum gerilimi (V, isteğe bağlı).
    :param bulk_ratio: Önerilen sönüm kondansatörünün C_in'e oranı (tasarım seçimi).
    """
    c_val = parse_spice_value(str(c_in)) if isinstance(c_in, str) else float(c_in)
    l_val = parse_spice_value(str(l_cable)) if isinstance(l_cable, str) else float(l_cable)
    r_val = parse_spice_value(str(r_cable)) if isinstance(r_cable, str) else float(r_cable)

    if v_in <= 0 or c_val <= 0 or l_val <= 0 or r_val < 0 or bulk_ratio <= 0:
        raise ValueError("v_in, c_in, l_cable ve bulk_ratio pozitif, r_cable negatif olmamalıdır.")

    z0 = math.sqrt(l_val / c_val)
    zeta = (r_val / 2.0) * math.sqrt(c_val / l_val)

    if zeta < 1.0:
        overshoot_ratio = math.exp((-math.pi * zeta) / math.sqrt(1.0 - zeta * zeta))
        is_underdamped = True
    else:
        overshoot_ratio = 0.0
        is_underdamped = False
    v_peak = v_in * (1.0 + overshoot_ratio)

    if max_voltage is None:
        rating = "NO_RATING"
    else:
        rating = "EXCEEDS_RATING" if v_peak > float(max_voltage) else "WITHIN_RATING"

    from scipy.optimize import minimize_scalar

    bulk_c = bulk_ratio * c_val
    best = minimize_scalar(lambda k: hotplug_step_peak(v_in, l_val, c_val, r_val, bulk_c, k * z0),
                           bounds=(0.05, 5.0), method="bounded", options={"xatol": 1e-3})
    bulk_esr = float(best.x) * z0

    return HotplugAnalysis(
        v_in=v_in,
        v_peak=v_peak,
        overshoot_percent=overshoot_ratio * 100.0,
        z0=z0,
        damping_ratio=zeta,
        is_underdamped=is_underdamped,
        recommended_damping_r=max(0.0, 2.0 * z0 - r_val),
        recommended_bulk_c=bulk_c,
        recommended_bulk_esr=bulk_esr,
        safe_margin_rating=rating,
        max_voltage=None if max_voltage is None else float(max_voltage),
        recommended_damped_peak=float(best.fun),
    )


# --------------------------------------------------------------------------- 5. Netlist Manipülasyon Yardımcıları
def inject_trace_parasitics_into_netlist(
    netlist_text: str,
    from_node: str,
    to_node: str,
    parasitics: TraceParasitics,
    element_prefix: str = "TR1",
) -> str:
    """
    ``from_node`` ile ``to_node`` arasına T-tipi RLC iz parazitiğini (R-L-C-L-R alt devresi) bağlar.
    Var olan elemanlara dokunmaz: iz iki düğüm arasına eklenir. Bir bağlantının araya girmesi için önce o bağlantıyı
    iki ayrı düğüme ayırın. Alt devre ve çağrısı ilk ``.end``'in önüne yazılır (``.ends``/``.endl`` sayılmaz).
    """
    subckt_name = f"PCB_TRACE_{element_prefix}"
    # Portlar alt devreye yerel (in out gnd): kullanıcı düğümü "n1" olsa da iç düğümlerle çakışmaz
    sub = parasitics.to_spice_subckt(subckt_name)
    call = f"X_{element_prefix} {from_node} {to_node} 0 {subckt_name}"
    block = [f"* --- Eklenen PCB İz Parazitiği ({parasitics.length_mm} mm) ---", *sub.strip().splitlines(), call]
    return insert_before_end(netlist_text, block)
