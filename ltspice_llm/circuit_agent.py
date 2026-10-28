"""
Circuit Agent Tool Harness
~~~~~~~~~~~~~~~~~~~~~~~~~~
Bir LLM ajanının LTspice'ı yönlendirmesi için gereken tarafı sağlar: araç şemaları,
sistem promptu, konuşma geçmişi ve araç çağrılarını yürüten/loglayan bir sarmalayıcı.

Not: Bu sınıf hiçbir LLM sağlayıcısına doğrudan bağlanmaz ve API anahtarı kullanmaz. Model çağrısını
çağıran taraf bir ``llm`` fonksiyonu olarak verir (bkz. `AutonomousCircuitAgent.run`); böylece OpenAI,
Gemini, Anthropic, Ollama vb. hepsiyle aynı döngü kullanılabilir.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from .adapter import LTSpiceAdapter
from .system_prompts import CIRCUIT_DESIGNER_SYSTEM_PROMPT
from .tools import get_ltspice_tool_schemas, handle_tool_call

logger = logging.getLogger(__name__)

#: `AutonomousCircuitAgent.run` için model fonksiyonu: (mesajlar, araç şemaları) -> yanıt sözlüğü.
#: Yanıt: ``{"content": str | None, "tool_calls": [{"id": str, "name": str, "arguments": dict | str}, ...]}``
LLMCallable = Callable[[List[Dict[str, Any]], List[Dict[str, Any]]], Dict[str, Any]]


class AutonomousCircuitAgent:
    """
    Bir LLM ajanı için LTspice araç sarmalayıcısı (bkz. modül notu).

    `model_name`, `api_key` ve `base_url` yalnızca çağıranın kendi LLM istemcisini yapılandırırken
    kullanabilmesi için saklanır; bu sınıf bunları kullanmaz ve ortam değişkeninden anahtar okumaz.

    Konuşma geçmişi (``conversation_history``) sağlayıcıdan bağımsız biçimdedir:
    ``{"role": "system"|"user"|"assistant"|"tool", "content": ...}``; asistan mesajları ``tool_calls``,
    araç mesajları ``name`` ve ``tool_call_id`` taşıyabilir. Sağlayıcıya özgü biçime çevirmek çağıranın işidir.
    ``tool_log`` ise yürütülen her araç çağrısının (ad, argüman, durum, süre) denetim kaydıdır.

    ``include_extended=True`` modele temel 10 aracın yanında şema çizimi, iz parazitikleri, hot-plug, SOA/derating ve
    Monte Carlo araçlarını da sunar (bkz. `get_ltspice_tool_schemas`).
    """

    def __init__(
        self,
        adapter: Optional[LTSpiceAdapter] = None,
        model_name: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        include_extended: bool = False,
        allow_file_access: bool = False,
    ):
        self.adapter = adapter or LTSpiceAdapter()
        self.model_name = model_name
        self.api_key = api_key
        self.base_url = base_url
        self.tools = get_ltspice_tool_schemas(include_extended=include_extended)
        #: False: modelin netlisti dosya yazamaz, çalışma alanı dışını okuyamaz (bkz. `handle_tool_call`)
        self.allow_file_access = allow_file_access
        self.conversation_history: List[Dict[str, Any]] = [
            {"role": "system", "content": CIRCUIT_DESIGNER_SYSTEM_PROMPT}
        ]
        self.tool_log: List[Dict[str, Any]] = []

    def reset(self) -> None:
        """Ajan konuşma geçmişini ve araç günlüğünü sıfırlar."""
        self.conversation_history = [
            {"role": "system", "content": CIRCUIT_DESIGNER_SYSTEM_PROMPT}
        ]
        self.tool_log = []

    def get_history(self) -> List[Dict[str, Any]]:
        """Ajan konuşma geçmişinin bağımsız bir kopyasını döner (dışarıdan değiştirmek geçmişi bozmaz)."""
        return copy.deepcopy(self.conversation_history)

    def add_user_message(self, content: str) -> None:
        """Geçmişe bir kullanıcı mesajı ekler."""
        self.conversation_history.append({"role": "user", "content": content})

    def add_assistant_message(
        self,
        content: Optional[str] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Geçmişe bir asistan mesajı ekler; model araç çağırdıysa `tool_calls` ile birlikte."""
        message: Dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            message["tool_calls"] = copy.deepcopy(tool_calls)
        self.conversation_history.append(message)

    def add_tool_result(
        self,
        tool_name: str,
        result: Dict[str, Any],
        tool_call_id: Optional[str] = None,
    ) -> None:
        """
        Geçmişe bir araç sonucu ekler (içerik JSON metni olarak saklanır).

        Grafik araçlarının ``image_base64`` alanı metne girmez, mesajın ``images`` listesine (``media_type``, ``data``)
        alınır ve sonuca ``image_attached: true`` yazılır. Eskiden basit bir RC grafiği geçmişe 72.524 karakterlik
        metin olarak giriyordu (inceleme 2026-10-11): model görseli göremez, token harcanır. Sağlayıcı biçimine
        çevirirken görsel bloğu üretmek için `ltspice_llm.providers`'a bakın.
        """
        images: List[Dict[str, str]] = []
        if isinstance(result, dict) and isinstance(result.get("image_base64"), str):
            result = dict(result)
            images.append({"media_type": "image/png", "data": result.pop("image_base64")})
            result["image_attached"] = True
        message: Dict[str, Any] = {
            "role": "tool",
            "name": tool_name,
            "content": json.dumps(result, ensure_ascii=False, default=str),
        }
        if images:
            message["images"] = images
        if tool_call_id is not None:
            message["tool_call_id"] = tool_call_id
        self.conversation_history.append(message)

    def execute_tool(self, tool_name: str, arguments: Optional[Any] = None) -> Dict[str, Any]:
        """Belirtilen araç çağrısını çalıştırır, `tool_log`'a kaydeder ve sonucu döner."""
        t0 = time.perf_counter()
        result = handle_tool_call(tool_name, arguments or {}, adapter=self.adapter,
                                  allow_file_access=self.allow_file_access)
        self.tool_log.append(
            {
                "tool": tool_name,
                "arguments": arguments or {},
                "status": result.get("status"),
                "elapsed_seconds": round(time.perf_counter() - t0, 4),
            }
        )
        return result

    def run_design_step(
        self,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Ajan adımı yürütür ve ayrıntıyı `logging` ile (INFO düzeyi) kaydeder."""
        args = arguments or {}
        logger.info("[Ajan Araç Çağrısı] %s", tool_name)
        logger.info("[Parametreler] %s", json.dumps(args, indent=2, ensure_ascii=False, default=str))
        res = self.execute_tool(tool_name, args)
        logger.info("[Sonuç] Durum: %s", res.get("status"))
        if "measurements" in res:
            logger.info("[Ölçümler] %s", res["measurements"])
        if res.get("errors"):
            logger.info("[Hatalar] %s", res["errors"])
        return res

    def list_circuits(self) -> Dict[str, Any]:
        """Çalışma alanındaki kayıtlı devreleri listeler."""
        return self.execute_tool("list_circuits", {})

    def run(self, user_message: str, llm: LLMCallable, max_steps: int = 10) -> Dict[str, Any]:
        """
        Sağlayıcıdan bağımsız araç çağırma (tool-calling) döngüsünü yürütür.

        Kullanıcı mesajını geçmişe ekler; her adımda ``llm(mesajlar, araçlar)`` çağrılır. Model araç
        çağırırsa her biri `execute_tool` ile çalıştırılıp sonucu geçmişe eklenir ve döngü sürer; araç
        çağırmayan bir yanıt gelirse biter. ``llm`` fonksiyonunu çağıran, kendi sağlayıcı istemcisine
        (mesaj/araç biçim dönüşümü dahil) bağlar.

        :return: ``{"finished": bool, "steps": int, "final_message": str}``. ``max_steps`` dolduğunda
            model hâlâ araç çağırıyorsa ``finished`` False olur.
        """
        self.add_user_message(user_message)
        last_content = ""
        for step in range(1, max_steps + 1):
            response = llm(self.get_history(), self.tools)
            content = response.get("content")
            tool_calls = response.get("tool_calls") or []
            last_content = content or last_content
            self.add_assistant_message(content, tool_calls)

            if not tool_calls:
                return {"finished": True, "steps": step, "final_message": content or ""}

            for call in tool_calls:
                tool_name = call.get("name", "")
                result = self.execute_tool(tool_name, call.get("arguments"))
                self.add_tool_result(tool_name, result, call.get("id"))

        return {"finished": False, "steps": max_steps, "final_message": last_content}
