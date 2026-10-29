"""
Interactive Circuit Tool (CLI)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Kullanıcının terminalden devre tasarlayabileceği, hazır şablonları koşturabileceği,
parametreleri tune edebileceği ve grafikleri oluşturabileceği interaktif arayüz.
Dil modeli kullanmaz: menü, bir modelin çağıracağı araçları (handle_tool_call) elle çağırır ve sonuçlarını gösterir.
"""

import sys
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import json
from ltspice_llm import (
    LTSpiceAdapter,
    handle_tool_call,
)


def print_banner():
    print("""
╔═══════════════════════════════════════════════════════════════════════════╗
║                  ⚡ LTspice - Etkileşimli Devre Aracı ⚡                  ║
║      ltspice-llm araçlarını elle çağıran menü (dil modeli kullanmaz)      ║
╚═══════════════════════════════════════════════════════════════════════════╝
    """)


def print_menu():
    print("""
[1] 📋 Çalışma Alanındaki Devreleri Listele (list_circuits)
[2] 🔍 Bir Devrenin Netlistini Görüntüle (get_circuit_netlist)
[3] 🚀 Hazır Devre Simüle Et (10 kHz Sallen-Key / RC Filtre)
[4] 🛠️ Parametre Güncelle & Yeniden Simüle Et (modify_parameters)
[5] 📈 Dalga Formu veya Bode Grafiği Çizdir
[6] ✍️ Özel SPICE Netlist Çalıştır
[7] 🎯 Standart E-Serisi Kuantalama (E12 / E24 / E96 Snapping)
[8] 🩺 Devre Hata ve Yakınsama Teşhisi (diagnose_circuit_issue)
[G] 🖥️ Devreyi LTspice Arayüzünde Aç (.asc Şema GUI)
[0] 🚪 Çıkış
    """)


def print_error_if_any(res: dict) -> bool:
    """Araç yanıtı başarısızsa mesajı/hataları basar ve True döner."""
    if res.get("status") == "SUCCESS":
        return False
    print("Hata:", res.get("message") or res.get("errors"))
    return True


def run_preset(adapter: LTSpiceAdapter):
    print("\n--- Hazır Devre Şablonları ---")
    print("1. 10 kHz Sallen-Key 2. Derece Butterworth Alçak Geçiren Filtre (AC)")
    print("2. Temel RC Gecikme Filtresi (TRAN)")
    choice = input("Seçiminiz (1-2): ").strip()

    if choice == "1":
        netlist = """* 10 kHz Sallen-Key Lowpass Filter
.param R1=15.915k R2=15.915k C1=1.414n C2=0.707n
V1 in 0 AC 1 0
R1 in n1 {R1}
R2 n1 inp {R2}
C1 n1 out {C1}
C2 inp 0 {C2}
E1 out 0 inp out 100000
.ac dec 20 100 1meg
.meas ac f_3db WHEN mag(V(out))=0.7071
.end
"""
        name = "sallen_key_preset"
    else:
        netlist = """* Basic RC Filter
V1 in 0 PULSE(0 5 0 1n 1n 1m 2m)
R1 in out 1k
C1 out 0 1u
.tran 3m
.meas tran v_max MAX V(out)
.end
"""
        name = "rc_preset"

    print(f"\n'{name}' simüle ediliyor...")
    res = handle_tool_call(
        "create_or_replace_circuit",
        {"circuit_name": name, "netlist_code": netlist},
        adapter=adapter,
    )
    print("Durum       :", res.get("status"))
    if print_error_if_any(res):
        return
    print("Süre (sn)   :", res.get("elapsed_seconds"))
    print("Ölçümler    :", json.dumps(res.get("measurements"), indent=2))


def main():
    print_banner()
    adapter = LTSpiceAdapter()
    print(f"✔️  LTspice Motoru: {adapter.executable_path}")
    print(f"📁 Çalışma Alanı : {adapter.workspace_dir}")

    # Eğer standart giriş bir tty değilse (otomasyon testi) sadece durumu bas ve çık
    if not sys.stdin.isatty():
        print("\n[Otomasyon Modu] Konsol etkileşimsiz modda çalıştı, sistem hazır.")
        res = handle_tool_call("list_circuits", {}, adapter=adapter)
        print(f"Toplam Kayıtlı Devre: {res.get('total_circuits')}")
        return

    while True:
        print_menu()
        try:
            choice = input("Lütfen bir işlem seçin: ").strip()
        except (EOFError, KeyboardInterrupt):
            # Windows'ta NUL girişi isatty() == True döner; EOF'ta çökmeden çık.
            print("\nİyi çalışmalar!")
            break

        if choice == "0":
            print("İyi çalışmalar!")
            break
        elif choice == "1":
            res = handle_tool_call("list_circuits", {}, adapter=adapter)
            print(f"\nToplam {res.get('total_circuits')} devre bulundu:")
            for c in res.get("circuits", []):
                print(f"  • {c['name']} (Raw: {c['has_raw']}, Log: {c['has_log']}, Plot: {c['has_plot']})")
        elif choice == "2":
            name = input("Devre adı: ").strip()
            res = handle_tool_call("get_circuit_netlist", {"circuit_name": name}, adapter=adapter)
            if res.get("status") == "SUCCESS":
                print(f"\n--- {name}.net ---\n{res['netlist']}")
            else:
                print(f"Hata: {res.get('message')}")
        elif choice == "3":
            run_preset(adapter)
        elif choice == "4":
            name = input("Devre adı: ").strip()
            param_key = input("Değiştirilecek parametre adı (örn: R1): ").strip()
            param_val = input("Yeni değer (örn: 22k): ").strip()
            res = handle_tool_call(
                "modify_parameters",
                {"circuit_name": name, "parameters": {param_key: param_val}},
                adapter=adapter,
            )
            if not print_error_if_any(res):
                print("Sonuç:", json.dumps(res.get("measurements"), indent=2))
        elif choice == "5":
            name = input("Devre adı: ").strip()
            is_bode = input("Bode grafiği mi? (e/h): ").strip().lower() == "e"
            if is_bode:
                res = handle_tool_call("plot_bode_diagram", {"circuit_name": name}, adapter=adapter)
                if not print_error_if_any(res):
                    print("Bode Dosyası:", res.get("bode_plot_saved_at"))
            else:
                # Araç, virgülle ayrılmış metni (parantez içi virgülleri koruyarak) kendisi böler.
                signals = input("Sinyaller (virgülle ayırın, örn: V(in),V(out)): ").strip()
                res = handle_tool_call("plot_transient_waveforms", {"circuit_name": name, "signals": signals}, adapter=adapter)
                if not print_error_if_any(res):
                    print("Grafik Dosyası:", res.get("plot_saved_at"))
        elif choice == "6":
            name = input("Devre adı: ").strip()
            # Netlistlerde boş satırlar olağandır; giriş ".end" satırında (veya EOF'ta) biter.
            print("Netlist satırlarını girin (Bitirmek için '.end' satırı yazın):")
            lines = []
            while True:
                try:
                    line = input()
                except EOFError:
                    break
                lines.append(line)
                if line.strip().lower() == ".end":
                    break
            res = handle_tool_call(
                "create_or_replace_circuit",
                {"circuit_name": name, "netlist_code": "\n".join(lines)},
                adapter=adapter,
                allow_file_access=True,  # netlisti dil modeli değil terminaldeki kullanıcı yazıyor
            )
            print("Sonuç:", json.dumps(res, indent=2))
        elif choice == "7":
            print("\n--- Standart E-Serisi Kuantalama ---")
            p_str = input("Parametreleri girin (örn: R1=15.915k, C1=1.414n): ").strip()
            series = input("Seri seçin (E12 / E24 / E96) [varsayılan: E24]: ").strip().upper() or "E24"
            params = {}
            for item in p_str.split(","):
                if "=" in item:
                    k, v = item.split("=", 1)
                    params[k.strip()] = v.strip()
            res = handle_tool_call(
                "quantize_parameters",
                {"parameters": params, "series": series},
                adapter=adapter,
            )
            print("\nKuantalama Sonucu:")
            print("Standart Değerler:", json.dumps(res.get("quantized_parameters"), indent=2))
            print(f"Maksimum Hata    : %{res.get('max_abs_error_pct', 0):.2f}")
        elif choice == "8":
            name = input("İncelenecek devre adı: ").strip()
            apply_f = input("Önerilen stabilizasyon yaması netliste otomatik uygulansın mı? (e/h): ").strip().lower() == "e"
            res = handle_tool_call(
                "diagnose_circuit_issue",
                {"circuit_name": name, "apply_fix": apply_f},
                adapter=adapter,
            )
            print("\nTeşhis Raporu:")
            print("Hata Var mı       :", res.get("has_error"))
            print("Hata Türü         :", res.get("issue_type"))
            print("Kök Neden         :", res.get("root_cause"))
            print("Önerilen Direktif :", res.get("suggested_options"))
            print("Öneriler          :", json.dumps(res.get("actionable_recommendations"), indent=2, ensure_ascii=False))
            if res.get("fix_applied"):
                print("✔️ ", res.get("message"))
        elif choice.upper() == "G":
            print("\n--- Devreyi LTspice Grafik Arayüzünde Aç (GUI) ---")
            name = input("Açılacak devre adı veya .asc dosya yolu: ").strip()
            try:
                adapter.open_in_gui(name)
                print(f"✔️ '{name}' LTspice grafik arayüzünde (GUI) açıldı!")
            except Exception as e:
                print("Hata:", str(e))
        else:
            print("Geçersiz seçim!")


if __name__ == "__main__":
    main()
