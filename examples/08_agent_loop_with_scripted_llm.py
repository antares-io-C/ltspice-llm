"""
Örnek 8: Sağlayıcıdan Bağımsız Araç Çağırma Döngüsü (AutonomousCircuitAgent.run)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
`AutonomousCircuitAgent.run`, bir `llm(mesajlar, araçlar)` fonksiyonunu tekrar tekrar çağırır, modelin
istediği araçları LTspice üzerinde çalıştırır ve sonuçları geçmişe ekler. Gerçek bir modele bağlamak için
`scripted_llm` yerine kendi istemcinizi sarmalayan bir fonksiyon verin, örneğin:

    def llm(messages, tools):
        # OpenAI: client.chat.completions.create(model=..., messages=<dönüştür>,
        #         tools=get_ltspice_tool_schemas("openai"))
        # Anthropic: client.messages.create(..., tools=get_ltspice_tool_schemas("anthropic"))
        # Yanıtı {"content": str|None, "tool_calls": [{"id", "name", "arguments"}]} biçimine çevirin.
        ...

Burada ağ/API anahtarı gerekmesin diye model, sırayla sabit yanıtlar veren bir betikle taklit edilir.
"""

import sys
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import json
import logging

from ltspice_llm import AutonomousCircuitAgent

NETLIST = """* Gerilim bolucu
V1 in 0 10V
R1 in out 1k
R2 out 0 1k
.op
.meas op vout FIND V(out)
.end
"""


def scripted_llm(messages, tools):
    """Bir modeli taklit eder: önce devreyi kurar, sonra R2'yi değiştirir, sonunda yanıt verir."""
    tool_results = [m for m in messages if m["role"] == "tool"]
    if len(tool_results) == 0:
        return {"content": "Devreyi kuruyorum.", "tool_calls": [
            {"id": "call_1", "name": "create_or_replace_circuit",
             "arguments": {"circuit_name": "bolucu", "netlist_code": NETLIST}}]}
    if len(tool_results) == 1:
        return {"content": "Çıkışı yükseltmek için R2'yi 3k yapıyorum.", "tool_calls": [
            {"id": "call_2", "name": "modify_parameters",
             "arguments": json.dumps({"circuit_name": "bolucu", "parameters": {"R2": "3k"}})}]}  # JSON metni de kabul edilir
    vout = json.loads(tool_results[-1]["content"])["measurements"]["vout"]
    return {"content": f"Tamamlandı: R2=3k iken V(out) = {vout:.2f} V.", "tool_calls": []}


def main():
    logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")
    logging.getLogger("ltspice_llm").setLevel(logging.INFO)  # ajan adımlarını göster, kütüphane gürültüsünü değil
    agent = AutonomousCircuitAgent()
    outcome = agent.run("10V'u bölen bir devre kur, sonra çıkışı 7.5V yap.", scripted_llm)

    print("\nSonuç:", outcome)
    print("\nAraç günlüğü:")
    for entry in agent.tool_log:
        print(f"  {entry['tool']:<28} durum={entry['status']:<8} süre={entry['elapsed_seconds']}s")
    print("\nKonuşma geçmişi (roller):", [m["role"] for m in agent.get_history()])


if __name__ == "__main__":
    main()
