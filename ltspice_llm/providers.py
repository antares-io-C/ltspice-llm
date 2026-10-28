"""
LLM Provider Formats
~~~~~~~~~~~~~~~~~~~~
`AutonomousCircuitAgent`'ın sağlayıcıdan bağımsız geçmişini Anthropic, OpenAI ve Gemini istek biçimlerine, onların
yanıtlarını da ajanın beklediği ``{"content", "tool_calls"}`` biçimine çevirir. Hiçbir sağlayıcıya bağlanmaz ve
SDK gerektirmez: ``llm`` fonksiyonunu yazan kişi kendi istemcisini çağırır, bu modül yalnızca biçimi çevirir.

Biçimler sağlayıcı belgelerinden (Anthropic Messages API "Tool use", OpenAI Chat Completions "Function calling",
Gemini API REST "Function calling"); canlı API'yle otomatik test edilmez (tests/test_providers.py).

Örnek (Anthropic SDK)::

    from ltspice_llm import AutonomousCircuitAgent, get_ltspice_tool_schemas
    from ltspice_llm.providers import from_anthropic, to_anthropic

    def llm(history, _tools):
        system, messages = to_anthropic(history)
        response = client.messages.create(model=MODEL, max_tokens=4096, system=system, messages=messages,
                                          tools=get_ltspice_tool_schemas("anthropic"))
        return from_anthropic(response)

    AutonomousCircuitAgent().run("10 kHz alçak geçiren filtre tasarla", llm)
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

Message = Dict[str, Any]


def _arguments_dict(arguments: Any) -> Dict[str, Any]:
    """Araç argümanı sözlük ya da JSON metni olabilir (OpenAI metin gönderir); sağlayıcıya nesne olarak verilir."""
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str) and arguments.strip():
        try:
            loaded = json.loads(arguments)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _arguments_text(arguments: Any) -> str:
    """OpenAI ``function.arguments`` bir JSON metnidir."""
    return arguments if isinstance(arguments, str) else json.dumps(arguments or {}, ensure_ascii=False)


def _system_text(history: List[Message]) -> str:
    return "\n\n".join(str(m.get("content") or "") for m in history if m.get("role") == "system")


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Sözlük ya da SDK nesnesi (``response.content[0].type``) aynı biçimde okunur."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


# --------------------------------------------------------------------------- Anthropic
def to_anthropic(history: List[Message]) -> Tuple[str, List[Message]]:
    """
    Ajan geçmişi -> ``(system, messages)``. Ardışık araç sonuçları tek ``user`` mesajında ``tool_result`` blokları
    olur; mesajın ``images`` alanı aynı ``tool_result``'un içine ``image`` bloğu olarak girer.
    """
    messages: List[Message] = []
    for m in history:
        role = m.get("role")
        if role == "system":
            continue
        if role == "user":
            messages.append({"role": "user", "content": m.get("content") or ""})
        elif role == "assistant":
            calls = m.get("tool_calls") or []
            if not calls:
                messages.append({"role": "assistant", "content": m.get("content") or ""})
                continue
            blocks: List[Dict[str, Any]] = [{"type": "text", "text": m["content"]}] if m.get("content") else []
            blocks += [{"type": "tool_use", "id": c.get("id"), "name": c.get("name"),
                        "input": _arguments_dict(c.get("arguments"))} for c in calls]
            messages.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            content: List[Dict[str, Any]] = [{"type": "text", "text": str(m.get("content") or "")}]
            content += [{"type": "image", "source": {"type": "base64", "media_type": img["media_type"],
                                                     "data": img["data"]}} for img in m.get("images") or []]
            block = {"type": "tool_result", "tool_use_id": m.get("tool_call_id"), "content": content}
            previous = messages[-1] if messages else None
            if previous and previous["role"] == "user" and isinstance(previous["content"], list) and all(
                    b.get("type") == "tool_result" for b in previous["content"]):
                previous["content"].append(block)
            else:
                messages.append({"role": "user", "content": [block]})
    return _system_text(history), messages


def from_anthropic(response: Any) -> Message:
    """Messages API yanıtı (sözlük ya da SDK nesnesi) -> ``{"content", "tool_calls"}``."""
    texts: List[str] = []
    calls: List[Dict[str, Any]] = []
    for block in _get(response, "content", []) or []:
        kind = _get(block, "type")
        if kind == "text":
            texts.append(_get(block, "text", ""))
        elif kind == "tool_use":
            calls.append({"id": _get(block, "id"), "name": _get(block, "name"), "arguments": _get(block, "input", {})})
    return {"content": "\n".join(texts) if texts else None, "tool_calls": calls}


# --------------------------------------------------------------------------- OpenAI
def to_openai(history: List[Message]) -> List[Message]:
    """
    Ajan geçmişi -> Chat Completions ``messages``. ``tool`` mesajı görsel taşımaz: bir asistan turunun araç
    sonuçları bitince görseller tek bir ``user`` mesajında ``image_url`` (data URL) olarak gelir.
    """
    messages: List[Message] = []
    pending_images: List[Dict[str, Any]] = []

    def flush() -> None:
        if pending_images:
            messages.append({"role": "user", "content": list(pending_images)})
            pending_images.clear()

    for m in history:
        role = m.get("role")
        if role != "tool":
            flush()
        if role in ("system", "user"):
            messages.append({"role": role, "content": m.get("content") or ""})
        elif role == "assistant":
            message: Message = {"role": "assistant", "content": m.get("content")}
            calls = m.get("tool_calls") or []
            if calls:
                message["tool_calls"] = [{"id": c.get("id"), "type": "function", "function": {
                    "name": c.get("name"), "arguments": _arguments_text(c.get("arguments"))}} for c in calls]
            messages.append(message)
        elif role == "tool":
            messages.append({"role": "tool", "tool_call_id": m.get("tool_call_id"), "content": str(m.get("content") or "")})
            for img in m.get("images") or []:
                pending_images.append({"type": "text", "text": f"{m.get('name')} ({m.get('tool_call_id')}) görseli:"})
                pending_images.append({"type": "image_url",
                                       "image_url": {"url": f"data:{img['media_type']};base64,{img['data']}"}})
    flush()
    return messages


def from_openai(message: Any) -> Message:
    """Chat Completions ``choices[0].message`` (sözlük ya da SDK nesnesi) -> ``{"content", "tool_calls"}``."""
    calls = []
    for call in _get(message, "tool_calls", None) or []:
        function = _get(call, "function", {})
        calls.append({"id": _get(call, "id"), "name": _get(function, "name"), "arguments": _get(function, "arguments", "{}")})
    return {"content": _get(message, "content"), "tool_calls": calls}


# --------------------------------------------------------------------------- Gemini
def _function_response(content: Any) -> Dict[str, Any]:
    """functionResponse.response bir nesnedir: JSON nesnesi olmayan sonuç ``{"result": ...}`` olur."""
    try:
        loaded = json.loads(content) if isinstance(content, str) else content
    except json.JSONDecodeError:
        loaded = content
    return loaded if isinstance(loaded, dict) else {"result": loaded}


def to_gemini(history: List[Message]) -> Tuple[Optional[Dict[str, Any]], List[Message]]:
    """Ajan geçmişi -> ``(system_instruction, contents)`` (REST biçimi; ardışık araç sonuçları tek ``user`` içeriği)."""
    contents: List[Message] = []
    for m in history:
        role = m.get("role")
        if role == "user":
            contents.append({"role": "user", "parts": [{"text": m.get("content") or ""}]})
        elif role == "assistant":
            parts: List[Dict[str, Any]] = [{"text": m["content"]}] if m.get("content") else []
            parts += [{"functionCall": {"name": c.get("name"), "args": _arguments_dict(c.get("arguments"))}}
                      for c in m.get("tool_calls") or []]
            contents.append({"role": "model", "parts": parts})
        elif role == "tool":
            parts = [{"functionResponse": {"name": m.get("name"), "response": _function_response(m.get("content"))}}]
            parts += [{"inlineData": {"mimeType": img["media_type"], "data": img["data"]}} for img in m.get("images") or []]
            previous = contents[-1] if contents else None
            if previous and previous["role"] == "user" and any("functionResponse" in p for p in previous["parts"]):
                responses = [p for p in previous["parts"] if "functionResponse" in p]
                inline = [p for p in previous["parts"] if "functionResponse" not in p]
                previous["parts"] = responses + parts[:1] + inline + parts[1:]
            else:
                contents.append({"role": "user", "parts": parts})
    system = _system_text(history)
    return ({"parts": [{"text": system}]} if system else None), contents


def from_gemini(response: Any) -> Message:
    """generateContent yanıtı -> ``{"content", "tool_calls"}``. Kimliksiz çağrılara ``call_<sıra>`` verilir."""
    candidates = _get(response, "candidates", []) or []
    parts = _get(_get(candidates[0], "content", {}), "parts", []) if candidates else []
    texts: List[str] = []
    calls: List[Dict[str, Any]] = []
    for part in parts or []:
        if _get(part, "text"):
            texts.append(_get(part, "text"))
        call = _get(part, "functionCall") or _get(part, "function_call")
        if call:
            calls.append({"id": _get(call, "id") or f"call_{len(calls)}", "name": _get(call, "name"),
                          "arguments": _get(call, "args", {}) or {}})
    return {"content": "\n".join(texts) if texts else None, "tool_calls": calls}
