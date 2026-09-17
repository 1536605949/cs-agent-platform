"""Optional LLM adapter (OpenAI-compatible ``/chat/completions``).

The platform is fully functional without an LLM: the built-in engine answers
from the FAQ knowledge base and the business tools. When ``LLM_ENABLED=true``
the model is used for two narrow, well-bounded jobs:

* **intent fallback** – only when the deterministic rules are not confident;
* **answer composition** – rewriting retrieved grounding into a natural reply.

Every call degrades gracefully: on timeout, HTTP error or malformed payload the
caller receives ``None`` and falls back to the deterministic path. A model
outage therefore never produces a failed conversation.
"""

from __future__ import annotations

import json
import logging

import httpx

from ..config import Settings, get_settings

logger = logging.getLogger("app.llm")

_INTENT_SYSTEM_PROMPT = (
    "You are an intent classifier for an e-commerce customer service desk. "
    "Classify the user's latest message into exactly one of these labels: "
    "refund, order_query, tech_support, human_handoff, faq, other. "
    'Reply with strict JSON only, e.g. {"intent":"refund","confidence":0.82}.'
)

_ANSWER_SYSTEM_PROMPT = (
    "You are a professional, concise e-commerce customer service agent. "
    "Answer strictly from the provided knowledge base excerpts and tool results. "
    "If the context does not contain the answer, say so honestly and offer to "
    "escalate to a human agent. Never invent order numbers, amounts or policies. "
    "Reply in the same language as the user, in at most 4 short sentences."
)


class LLMClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._client: httpx.AsyncClient | None = None

    @property
    def available(self) -> bool:
        s = self.settings
        return bool(s.llm_enabled and s.llm_api_key and s.llm_base_url)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.llm_base_url.rstrip("/"),
                timeout=self.settings.llm_timeout_seconds,
                headers={
                    "Authorization": f"Bearer {self.settings.llm_api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 512,
        temperature: float = 0.2,
        json_mode: bool = False,
    ) -> str | None:
        """Single completion call. Returns ``None`` on any failure."""
        if not self.available:
            return None
        payload: dict = {
            "model": self.settings.llm_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            response = await self._get_client().post("/chat/completions", json=payload)
            if response.status_code >= 400:
                logger.warning("llm_http_error status=%s body=%s", response.status_code, response.text[:300])
                return None
            data = response.json()
            return (data["choices"][0]["message"]["content"] or "").strip() or None
        except Exception as exc:  # noqa: BLE001 - the deterministic path is the fallback
            logger.warning("llm_call_failed error=%s", exc)
            return None

    async def classify_intent(self, text: str, *, previous_intent: str = "") -> tuple[str, float] | None:
        """Ask the model for an intent label. Returns ``None`` when unavailable."""
        hint = f" Previous turn intent: {previous_intent}." if previous_intent else ""
        content = await self.chat(
            [
                {"role": "system", "content": _INTENT_SYSTEM_PROMPT},
                {"role": "user", "content": f"{text}{hint}"},
            ],
            max_tokens=64,
            temperature=0.0,
            json_mode=True,
        )
        if not content:
            return None
        try:
            data = json.loads(content)
            intent = str(data.get("intent", "")).strip()
            confidence = float(data.get("confidence", 0.6))
        except (ValueError, TypeError):
            return None
        if intent not in {"refund", "order_query", "tech_support", "human_handoff", "faq", "other"}:
            return None
        return intent, max(0.0, min(1.0, confidence))

    async def compose_answer(
        self,
        question: str,
        *,
        context_blocks: list[str],
        history: list[tuple[str, str]] | None = None,
    ) -> str | None:
        """Rewrite grounding into a natural answer. Returns ``None`` on failure."""
        if not context_blocks:
            return None

        grounding = "\n\n".join(f"[{index + 1}] {block}" for index, block in enumerate(context_blocks))
        messages: list[dict[str, str]] = [{"role": "system", "content": _ANSWER_SYSTEM_PROMPT}]
        for role, content in (history or [])[-6:]:
            messages.append({"role": "assistant" if role != "user" else "user", "content": content})
        messages.append(
            {
                "role": "user",
                "content": f"Knowledge base excerpts:\n{grounding}\n\nCustomer question: {question}",
            }
        )
        return await self.chat(messages, max_tokens=400, temperature=0.3)


_client: LLMClient | None = None


def get_llm() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
