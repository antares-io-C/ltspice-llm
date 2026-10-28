"""
Örnek 5: Araç Çağrılarıyla Filtre Tasarımı (Betikli Gösterim)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Şu kullanıcı isteğinin araçlarla baştan sona nasıl çözüldüğünü gösterir:
"Bana 10 kHz kesim frekanslı 2. derece Butterworth Sallen-Key alçak geçiren
filtre tasarla. Frekans cevabını (Bode) çıkar, -3dB frekansını ölç, ardından
basamak cevabını (transient) test edip grafiklerini oluştur."

Dil modeli kullanılmaz: bir modelin bu istek için yapacağı araç çağrıları ve gerekçesi betikte sabittir. Ölçümler ve
grafikler gerçek LTspice koşumlarından gelir. Gerçek bir modelle araç döngüsü için: examples/08 ve
AutonomousCircuitAgent.
"""

import sys
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from ltspice_llm import LTSpiceAdapter, handle_tool_call


def main():
    print("=================================================================")
    print("  Araç Çağrılarıyla Filtre Tasarımı (betikli gösterim)          ")
    print("=================================================================\n")

    user_request = (
        "Bana 10 kHz kesim frekanslı 2. derece Butterworth Sallen-Key "
        "alçak geçiren filtre tasarla. Bode diyagramını ve basamak yanıtını çıkar."
    )
    print(f"KULLANICI: '{user_request}'\n")

    adapter = LTSpiceAdapter()

    # Adım 1: Devre topolojisi ve teorik değerler (bir modelin gerekçesi; betikte sabit)
    print(">>> [MODELİN GEREKÇESİ (betikte sabit)]:")
    print("    1. Topoloji: 2. Derece Sallen-Key Alçak Geçiren Filtre (Butterworth Q=0.707).")
    print("    2. Formül: f_c = 1 / (2 * pi * sqrt(R1 * R2 * C1 * C2))")
    print("    3. Seçim: R1 = R2 = 10kΩ. C1 = 2.25nF, C2 = 1.125nF (veya normalize değerler).")
    print("    4. Op-Amp Modeli: 100k DC kazançlı, yüksek bant genişlikli VCVS (E1).\n")

    # Adım 2: LLM Frekans Analizi (.AC) İçin Devreyi Kuruyor
    print(">>> [ARAÇ ÇAĞRISI]: create_or_replace_circuit (.ac frekans analizi)")
    sallen_key_ac_netlist = """* 10 kHz Sallen-Key 2nd Order Butterworth Lowpass Filter
.param R1_V=15.915k
.param R2_V=15.915k
.param C1_V=1.414n
.param C2_V=0.707n

* Giris AC Kaynagi
V1 in 0 AC 1 0

* Filtre Agi
R1 in n1 {R1_V}
R2 n1 inp {R2_V}
C1 n1 out {C1_V}
C2 inp 0 {C2_V}

* Ideal OPAMP Modeli (Voltaj Kontrollu Gerilim Kaynagi VCVS: gain=1e5)
E1 out 0 inp out 100000

* Analiz ve Olcum Direktifleri
.ac dec 25 100 1meg
.meas ac f_3db WHEN mag(V(out))=0.7071
.meas ac max_gain MAX mag(V(out))
.end
"""

    ac_res = handle_tool_call(
        "create_or_replace_circuit",
        {
            "circuit_name": "sallen_key_10k",
            "netlist_code": sallen_key_ac_netlist,
        },
        adapter=adapter,
    )

    print(f"    Simülasyon Durumu: {ac_res.get('status')}")
    ac_meas = ac_res.get("measurements", {})
    if ac_res.get("status") != "SUCCESS" or "f_3db" not in ac_meas or "max_gain" not in ac_meas:
        print(f"    Hata: {ac_res.get('message') or ac_res.get('errors')}")
        return
    print(f"    Ölçülen -3dB Kesim Frekansı: {ac_meas['f_3db']:.2f} Hz (Hedef: 10,000 Hz)")
    print(f"    Maksimum DC Kazancı         : {ac_meas['max_gain']:.4f} dB\n")

    # Adım 3: LLM Bode Diyagramını Çizdiriyor
    print(">>> [ARAÇ ÇAĞRISI]: plot_bode_diagram")
    bode_res = handle_tool_call(
        "plot_bode_diagram",
        {
            "circuit_name": "sallen_key_10k",
            "out_node": "V(out)",
            "in_node": "V(in)",
            "title": "10 kHz Sallen-Key Butterworth Filtre Frekans Cevabı",
        },
        adapter=adapter,
    )
    print(f"    Bode Grafiği Kaydedildi: {bode_res.get('bode_plot_saved_at')}\n")

    # Adım 4: LLM Zaman Alanı (.TRAN) Basamak Cevabı İçin Devreyi Güncelliyor
    print(">>> [ARAÇ ÇAĞRISI]: create_or_replace_circuit (.tran basamak cevabi)")
    sallen_key_tran_netlist = """* 10 kHz Sallen-Key Transient Step Response
.param R1_V=15.915k
.param R2_V=15.915k
.param C1_V=1.414n
.param C2_V=0.707n

* 0V'dan 5V'a 0.2ms genislikli basamak darbesi
V1 in 0 PULSE(0 5 10u 1n 1n 200u 500u)

* Filtre Agi
R1 in n1 {R1_V}
R2 n1 inp {R2_V}
C1 n1 out {C1_V}
C2 inp 0 {C2_V}

* OPAMP
E1 out 0 inp out 100000

* 0.5 milisaniyelik gecici rejim analizi
.tran 0.1u 0.5m
.meas tran v_overshoot MAX V(out)
.meas tran t_rise TRIG V(out)=0.5 TARG V(out)=4.5
.end
"""

    tran_res = handle_tool_call(
        "create_or_replace_circuit",
        {
            "circuit_name": "sallen_key_10k_step",
            "netlist_code": sallen_key_tran_netlist,
        },
        adapter=adapter,
    )

    print(f"    Transient Durumu : {tran_res.get('status')}")
    tran_meas = tran_res.get("measurements", {})
    if tran_res.get("status") != "SUCCESS" or "v_overshoot" not in tran_meas:
        print(f"    Hata: {tran_res.get('message') or tran_res.get('errors')}")
        return
    print(f"    Maksimum Gerilim : {tran_meas['v_overshoot']:.3f} V")
    if "t_rise" in tran_meas:
        print(f"    Yükselme Süresi  : {tran_meas['t_rise']*1e6:.2f} µs\n")

    # Adım 5: LLM Basamak Yanıtı Grafiğini Çizdiriyor
    print(">>> [ARAÇ ÇAĞRISI]: plot_transient_waveforms")
    plot_res = handle_tool_call(
        "plot_transient_waveforms",
        {
            "circuit_name": "sallen_key_10k_step",
            "signals": ["V(in)", "V(out)"],
            "title": "Sallen-Key Filtre 5V Basamak (Step) Yanıtı",
        },
        adapter=adapter,
    )
    print(f"    Dalga Formu Kaydedildi: {plot_res.get('plot_saved_at')}\n")

    # Adım 6: LLM Kullanıcıya Mühendislik Raporu Sunuyor (tüm sayılar ölçümlerden hesaplanır)
    target_hz = 10_000.0
    step_v = 5.0
    f_err_pct = abs(ac_meas["f_3db"] - target_hz) / target_hz * 100.0
    overshoot_pct = max(0.0, (tran_meas["v_overshoot"] - step_v) / step_v * 100.0)
    print("=================================================================")
    print("        KULLANICIYA RAPOR (sayılar LTspice ölçümünden)          ")
    print("=================================================================")
    print("""
Sayın Kullanıcı,

İstediğiniz 10 kHz kesim frekansına sahip 2. Derece Butterworth Sallen-Key
Alçak Geçiren Filtre devresi tasarlanmış ve LTspice üzerinde simüle edilmiştir.

📊 Doğrulanan Spesifikasyonlar:
-------------------------------------------------------------
1. Hedeflenen Kesim Frekansı : 10,000 Hz
2. Ölçülen -3dB Frekansı    : {:.2f} Hz (Hata: %{:.2f})
3. Geçirme Bandı Kazancı    : {:.4f} dB
4. Butterworth Özelliği     : Geçirme bandında ripple yok (düz yanıt, teorik).
5. Basamak Girişi (0V->5V)  : Tepe değeri {:.2f}V, aşım %{:.1f} (Butterworth teorik: ~%4.3).

📁 Üretilen Grafikler:
-------------------------------------------------------------
- Bode Frekans & Faz Grafiği : {}
- Zaman Alanı Basamak Yanıtı : {}

Komponent Listesi (BOM):
- R1 = 15.9 kΩ
- R2 = 15.9 kΩ
- C1 = 1.41 nF
- C2 = 707 pF
- OpAmp = Birlik kazançlı aktif tampon
""".format(
        ac_meas['f_3db'],
        f_err_pct,
        ac_meas['max_gain'],
        tran_meas['v_overshoot'],
        overshoot_pct,
        bode_res.get('bode_plot_saved_at', ''),
        plot_res.get('plot_saved_at', '')
    ))


if __name__ == "__main__":
    main()
