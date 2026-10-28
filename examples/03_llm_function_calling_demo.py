"""
Örnek 3: LLM Araç Çağrısı (Tool / Function Calling) Simülasyonu
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Bir LLM'in (Gemini, Claude, GPT-4) JSON Schema kullanarak LTspice adaptörünü
nasıl çağıracağını ve fonksiyon çıktısını nasıl yorumlayacağını gösterir.
"""

import sys
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

import json
from ltspice_llm import get_ltspice_tool_schemas, handle_tool_call


def main():
    print("==================================================")
    print("  LLM Tool / Function Calling Entegrasyon Demosu  ")
    print("==================================================\n")

    # 1. LLM'e sunulan Tool Şemaları
    tools = get_ltspice_tool_schemas()
    print(f"Sisteme kayıtlı LLM araç sayısı: {len(tools)}")
    for t in tools:
        print(f" - {t['name']}: {t['description'][:65]}...")
    print()

    # Senaryo 1: LLM devre kurmak için simulate_circuit aracını çağırıyor
    print("--- Senaryo 1: LLM Devre Tasarlıyor ve Simüle Ediyor ---")
    mock_llm_call_1 = {
        "tool_name": "simulate_circuit",
        "arguments": {
            "circuit_name": "voltage_divider",
            "netlist_code": """* Ayarlanabilir Gerilim Bolucu
.param R1_VAL=10k
.param R2_VAL=10k

V1 in 0 5V
R1 in out {R1_VAL}
R2 out 0 {R2_VAL}

.op
.meas op vout FIND V(out)
.end
""",
            "parameters": {"R1_VAL": "10k", "R2_VAL": "10k"},
        },
    }

    print(f"LLM Çağrısı: {mock_llm_call_1['tool_name']}")
    response_1 = handle_tool_call(
        tool_name=mock_llm_call_1["tool_name"],
        arguments=mock_llm_call_1["arguments"],
    )
    print("Adaptörün LLM'e Döndürdüğü Yanıt (JSON):")
    print(json.dumps(response_1, indent=2))
    print()

    # Senaryo 2: LLM çıkış gerilimini 3.3V yapmak için parametreyi tune ediyor
    print("--- Senaryo 2: LLM Komponent Değerini Güncelliyor (Tune) ---")
    # Vout = 5V * R2 / (R1 + R2) = 3.3V  =>  R2 ~ 19.4k
    mock_llm_call_2 = {
        "tool_name": "tune_circuit_parameters",
        "arguments": {
            "circuit_name": "voltage_divider",
            "parameters": {"R2_VAL": "19.4k"},
        },
    }

    print(f"LLM Çağrısı: {mock_llm_call_2['tool_name']}")
    response_2 = handle_tool_call(
        tool_name=mock_llm_call_2["tool_name"],
        arguments=mock_llm_call_2["arguments"],
    )
    print("Adaptörün LLM'e Döndürdüğü Yeni Yanıt (JSON):")
    print(json.dumps(response_2, indent=2))

    print("\nSonuç:")
    # Araç hata döndürürse yanıtta 'measurements' anahtarı olmaz.
    print(f"İlk V(out) : {response_1.get('measurements', {}).get('vout')} V")
    print(f"Yeni V(out): {response_2.get('measurements', {}).get('vout')} V (Hedef ~3.3V)")


if __name__ == "__main__":
    main()
