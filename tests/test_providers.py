"""
Sağlayıcıdan bağımsız ajan geçmişinin sağlayıcı biçimlerine çevrilmesi (LTspice ve ağ gerektirmez).

Beklenen yapılar sağlayıcı belgelerinden (2026-10-24), koddan değil:
- Anthropic Messages API, "Tool use": asistan içeriğinde ``{"type": "tool_use", "id", "name", "input"}``; sonuçlar bir
  sonraki ``user`` mesajında ``{"type": "tool_result", "tool_use_id", "content": [...]}``; görsel bloğu
  ``{"type": "image", "source": {"type": "base64", "media_type", "data"}}``; sistem istemi ayrı ``system`` alanı.
- OpenAI Chat Completions, "Function calling": asistan ``tool_calls`` öğesi ``{"id", "type": "function",
  "function": {"name", "arguments": <JSON metni>}}``; her sonuç ``{"role": "tool", "tool_call_id", "content"}`` ve
  asistan mesajını hemen izler. ``tool`` mesajı görsel taşımaz: görsel, ardından gelen ``user`` mesajında
  ``{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}``.
- Gemini API (REST) "Function calling": ``model`` rolünde ``{"functionCall": {"name", "args"}}``, sonuç ``user`` rolünde
  ``{"functionResponse": {"name", "response": <nesne>}}``; görsel ``{"inlineData": {"mimeType", "data"}}``; sistem
  istemi ``system_instruction``.
"""

from ltspice_llm.providers import from_anthropic, from_gemini, from_openai, to_anthropic, to_gemini, to_openai

HISTORY = [
    {"role": "system", "content": "SİSTEM"},
    {"role": "user", "content": "RC filtre kur"},
    {"role": "assistant", "content": "Kuruyorum", "tool_calls": [
        {"id": "c1", "name": "create_or_replace_circuit", "arguments": {"circuit_name": "rc", "netlist_code": "* t"}},
        {"id": "c2", "name": "plot_transient_waveforms", "arguments": '{"circuit_name": "rc"}'},  # OpenAI gibi metin
    ]},
    {"role": "tool", "name": "create_or_replace_circuit", "tool_call_id": "c1", "content": '{"status": "SUCCESS"}'},
    {"role": "tool", "name": "plot_transient_waveforms", "tool_call_id": "c2",
     "content": '{"status": "SUCCESS", "image_attached": true}', "images": [{"media_type": "image/png", "data": "AAAA"}]},
    {"role": "assistant", "content": "Bitti"},
]


def test_history_to_anthropic_messages():
    system, messages = to_anthropic(HISTORY)
    assert system == "SİSTEM"
    assert messages == [
        {"role": "user", "content": "RC filtre kur"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Kuruyorum"},
            {"type": "tool_use", "id": "c1", "name": "create_or_replace_circuit",
             "input": {"circuit_name": "rc", "netlist_code": "* t"}},
            {"type": "tool_use", "id": "c2", "name": "plot_transient_waveforms", "input": {"circuit_name": "rc"}},
        ]},
        {"role": "user", "content": [  # iki sonuç tek user mesajında
            {"type": "tool_result", "tool_use_id": "c1", "content": [{"type": "text", "text": '{"status": "SUCCESS"}'}]},
            {"type": "tool_result", "tool_use_id": "c2", "content": [
                {"type": "text", "text": '{"status": "SUCCESS", "image_attached": true}'},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
            ]},
        ]},
        {"role": "assistant", "content": "Bitti"},
    ]


def test_history_to_openai_messages():
    assert to_openai(HISTORY) == [
        {"role": "system", "content": "SİSTEM"},
        {"role": "user", "content": "RC filtre kur"},
        {"role": "assistant", "content": "Kuruyorum", "tool_calls": [
            {"id": "c1", "type": "function", "function": {
                "name": "create_or_replace_circuit", "arguments": '{"circuit_name": "rc", "netlist_code": "* t"}'}},
            {"id": "c2", "type": "function", "function": {
                "name": "plot_transient_waveforms", "arguments": '{"circuit_name": "rc"}'}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": '{"status": "SUCCESS"}'},
        {"role": "tool", "tool_call_id": "c2", "content": '{"status": "SUCCESS", "image_attached": true}'},
        {"role": "user", "content": [  # tool mesajları bitince, görsel için
            {"type": "text", "text": "plot_transient_waveforms (c2) görseli:"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]},
        {"role": "assistant", "content": "Bitti"},
    ]


def test_history_to_gemini_contents():
    system_instruction, contents = to_gemini(HISTORY)
    assert system_instruction == {"parts": [{"text": "SİSTEM"}]}
    assert contents == [
        {"role": "user", "parts": [{"text": "RC filtre kur"}]},
        {"role": "model", "parts": [
            {"text": "Kuruyorum"},
            {"functionCall": {"name": "create_or_replace_circuit", "args": {"circuit_name": "rc", "netlist_code": "* t"}}},
            {"functionCall": {"name": "plot_transient_waveforms", "args": {"circuit_name": "rc"}}},
        ]},
        {"role": "user", "parts": [
            {"functionResponse": {"name": "create_or_replace_circuit", "response": {"status": "SUCCESS"}}},
            {"functionResponse": {"name": "plot_transient_waveforms",
                                  "response": {"status": "SUCCESS", "image_attached": True}}},
            {"inlineData": {"mimeType": "image/png", "data": "AAAA"}},
        ]},
        {"role": "model", "parts": [{"text": "Bitti"}]},
    ]


def test_tool_result_that_is_not_an_object_is_wrapped_for_gemini():
    """functionResponse.response bir nesnedir (Gemini belgesi); JSON olmayan sonuç {'result': ...} olur."""
    history = [{"role": "user", "content": "x"},
               {"role": "assistant", "content": None, "tool_calls": [{"id": "a", "name": "t", "arguments": {}}]},
               {"role": "tool", "name": "t", "tool_call_id": "a", "content": "düz metin"}]
    _, contents = to_gemini(history)
    assert contents[-1] == {"role": "user", "parts": [{"functionResponse": {"name": "t", "response": {"result": "düz metin"}}}]}
    _, messages = to_anthropic(history)
    assert messages[1] == {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "t", "input": {}}]}


def test_responses_become_the_agent_format():
    """AutonomousCircuitAgent.run'ın beklediği biçim: {"content", "tool_calls": [{"id", "name", "arguments"}]}."""
    anthropic = {"content": [{"type": "text", "text": "Simüle ediyorum"},
                             {"type": "tool_use", "id": "t1", "name": "list_circuits", "input": {}}]}
    assert from_anthropic(anthropic) == {
        "content": "Simüle ediyorum", "tool_calls": [{"id": "t1", "name": "list_circuits", "arguments": {}}]}

    openai = {"content": None, "tool_calls": [
        {"id": "o1", "type": "function", "function": {"name": "list_circuits", "arguments": "{}"}}]}
    assert from_openai(openai) == {"content": None, "tool_calls": [{"id": "o1", "name": "list_circuits", "arguments": "{}"}]}

    gemini = {"candidates": [{"content": {"role": "model", "parts": [
        {"text": "Bakıyorum"}, {"functionCall": {"name": "list_circuits", "args": {}}}]}}]}
    assert from_gemini(gemini) == {
        "content": "Bakıyorum", "tool_calls": [{"id": "call_0", "name": "list_circuits", "arguments": {}}]}

    assert from_anthropic({"content": [{"type": "text", "text": "Bitti"}]}) == {"content": "Bitti", "tool_calls": []}


def test_sdk_style_objects_are_read_like_dicts():
    """SDK'lar sözlük değil nesne döner (response.content[0].type); ikisi de okunur."""
    from types import SimpleNamespace as N

    response = N(content=[N(type="text", text="a"), N(type="tool_use", id="t", name="n", input={"x": 1})])
    assert from_anthropic(response) == {"content": "a", "tool_calls": [{"id": "t", "name": "n", "arguments": {"x": 1}}]}
    message = N(content="b", tool_calls=[N(id="o", function=N(name="n", arguments='{"x": 1}'))])
    assert from_openai(message) == {"content": "b", "tool_calls": [{"id": "o", "name": "n", "arguments": '{"x": 1}'}]}


def test_agent_loop_through_the_anthropic_format(tmp_path):
    """Gerçek sınır: LLM API'si (sahte istemci). Ajan geçmişi to_anthropic ile gider, yanıt from_anthropic ile döner;
    ikinci istekte araç sonucu, çağrının kimliğiyle bir user mesajında tool_result olarak bulunur."""
    import sys

    from ltspice_llm import AutonomousCircuitAgent, LTSpiceAdapter

    requests = []

    def llm(history, _tools):
        system, messages = to_anthropic(history)
        requests.append((system, messages))
        if len(requests) == 1:
            return from_anthropic({"content": [{"type": "tool_use", "id": "t1", "name": "list_circuits", "input": {}}]})
        return from_anthropic({"content": [{"type": "text", "text": "Devre yok."}]})

    agent = AutonomousCircuitAgent(adapter=LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path))
    out = agent.run("devreleri listele", llm)
    assert out == {"finished": True, "steps": 2, "final_message": "Devre yok."}
    system, messages = requests[1]
    assert system.startswith("Sen uzman düzeyinde")
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    (result,) = messages[2]["content"]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "t1"
    assert '"total_circuits": 0' in result["content"][0]["text"]


# Anthropic "preserved thinking" (Claude Opus 5.5, Claude Fable 5.1; Claude API belgeleri, 2026-10-29): aynı modelle
# devam ederken düşünme blokları API'ye değiştirilmeden geri gönderilir; bir bloğu atmak ya da önceki bir turu
# düzenlemek sonraki blokları geçersiz kılar (2026-08-31'den sonra açılan hesaplarda 400). Bu modellerde düşünme hep
# açıktır: her yanıtta (metni boş olsa da) imzalı bir thinking bloğu gelir.
THINKING_TOOL_TURN = [
    {"type": "thinking", "thinking": "", "signature": "imza-1"},
    {"type": "text", "text": "Devreleri listeliyorum."},
    {"type": "tool_use", "id": "t1", "name": "list_circuits", "input": {}},
]


def test_agent_history_sent_to_anthropic_is_append_only_with_thinking_blocks(tmp_path):
    """Gerçek sınır: LLM API'si (sahte yanıtlar). İkinci istek birincinin mesajlarını aynen taşır, ardından modelin
    turunu düşünme bloğu dahil değiştirmeden ve araç sonucunu ekler. Sonraki run() da aynı geçmişi sürdürür: araç
    çağırmayan son tur da düşünme bloğuyla geri gider."""
    import sys

    from ltspice_llm import AutonomousCircuitAgent, LTSpiceAdapter

    final_turn = [{"type": "thinking", "thinking": "", "signature": "imza-2"}, {"type": "text", "text": "Devre yok."}]
    replies = [THINKING_TOOL_TURN, final_turn,
               [{"type": "thinking", "thinking": "", "signature": "imza-3"}, {"type": "text", "text": "Tamam."}]]
    requests = []

    def llm(history, _tools):
        requests.append(to_anthropic(history)[1])
        return from_anthropic({"content": replies[len(requests) - 1]})

    agent = AutonomousCircuitAgent(adapter=LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path))
    assert agent.run("devreleri listele", llm) == {"finished": True, "steps": 2, "final_message": "Devre yok."}
    assert agent.run("teşekkürler", llm)["final_message"] == "Tamam."
    first, second, third = requests
    assert second[:len(first)] == first
    assert second[len(first)] == {"role": "assistant", "content": THINKING_TOOL_TURN}
    assert [b["tool_use_id"] for b in second[len(first) + 1]["content"]] == ["t1"]
    assert len(second) == len(first) + 2
    assert third == second + [{"role": "assistant", "content": final_turn}, {"role": "user", "content": "teşekkürler"}]


def test_turn_after_a_mid_output_fallback_echoes_only_what_the_api_accepts():
    """Anthropic Claude API belgeleri, "Refusals and fallback" -> "Echoing fallback turns back": akışlı yanıtta model
    çıktının ortasında değişirse son ``fallback`` bloğundan önceki thinking, redacted_thinking ve tool_use blokları geri
    yollanmaz; metin ve sınırdan sonrası olduğu gibi gider, ``fallback`` bloğu yok sayılan bir işarettir (tutulabilir).
    Sınırdan önceki araç çağrısı reddedilen modelindir: yürütülmez."""
    declined = [{"type": "thinking", "thinking": "", "signature": "imza-a"}, {"type": "text", "text": "Önce"},
                {"type": "tool_use", "id": "t0", "name": "list_circuits", "input": {}}]
    after = [{"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-5"}},
             {"type": "thinking", "thinking": "", "signature": "imza-b"}, {"type": "text", "text": "Sonra"},
             {"type": "tool_use", "id": "t1", "name": "list_circuits", "input": {}}]
    reply = from_anthropic({"content": declined + after})
    assert reply["tool_calls"] == [{"id": "t1", "name": "list_circuits", "arguments": {}}]
    assert reply["content"] == "Önce\nSonra"
    assert reply["anthropic_content"] == [declined[1]] + after


def test_sdk_objects_with_thinking_are_kept_in_their_api_form():
    """SDK yanıt nesneleri ``to_dict()`` ile API'nin alan adlarıyla (``from_`` değil ``from``) saklanır; düz metin ve
    araç çağrısı içeren yanıtın biçimi değişmez (test_responses_become_the_agent_format). Akışsız yanıtta ``fallback``
    bloğu içeriğin başında gelir (Anthropic belgeleri, "Refusals and fallback")."""
    from types import SimpleNamespace as N

    blocks = [{"type": "fallback", "from": {"model": "a"}, "to": {"model": "b"}},
              {"type": "thinking", "thinking": "", "signature": "imza-1"},
              {"type": "text", "text": "a"}]
    response = N(content=[N(type=b["type"], text=b.get("text"), to_dict=lambda b=b: dict(b)) for b in blocks])
    reply = from_anthropic(response)
    assert reply["content"] == "a" and reply["tool_calls"] == []
    _, messages = to_anthropic([{"role": "user", "content": "u"}, {"role": "assistant", **reply}])
    assert messages[1] == {"role": "assistant", "content": blocks}


def test_openai_image_message_waits_until_every_tool_result_is_sent():
    """OpenAI: asistanın tool_calls'ına verilen her 'tool' mesajı onu hemen izler; araya giren user mesajı isteği
    geçersiz kılar. Görselli sonuç ilk sırada olsa da görsel mesajı en sona gelir."""
    history = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "a", "name": "plot", "arguments": {}},
                                                              {"id": "b", "name": "list", "arguments": {}}]},
        {"role": "tool", "name": "plot", "tool_call_id": "a", "content": "{}",
         "images": [{"media_type": "image/png", "data": "QQ=="}]},
        {"role": "tool", "name": "list", "tool_call_id": "b", "content": "{}"},
    ]
    messages = to_openai(history)
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "tool", "user"]
    assert messages[-1]["content"][1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,QQ=="}}
