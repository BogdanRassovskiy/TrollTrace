"""Клиент OpenRouter (OpenAI-совместимый API) для разметки нарративов.

Тексты уходят внешнему провайдеру, поэтому запрос просит только провайдеров с нулевым
хранением данных (`provider.zdr`), которые не используют их для обучения
(`provider.data_collection = deny`). `require_parameters` не ставится: с ним OpenRouter
отсекает почти всех провайдеров Anthropic и OpenAI, — ответ проверяется сам (labeling).
Ключ — OPENROUTER_API_KEY в .env; без него кандидаты просто ждут.
"""

import json
import logging
import time
from dataclasses import dataclass

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

API_URL = "https://openrouter.ai/api/v1/chat/completions"
TIMEOUT = 120
RETRIES = 3


class LLMError(Exception):
    pass


@dataclass
class Completion:
    data: dict
    model: str
    cost_usd: float | None
    prompt_tokens: int | None
    completion_tokens: int | None


def is_configured() -> bool:
    return bool(settings.OPENROUTER_API_KEY)


def complete_json(model: str, system: str, user: str, schema: dict, *,
                  client: httpx.Client | None = None) -> Completion:  # fmt: skip
    """Один запрос со строгой JSON-схемой ответа; повтор при 429/5xx и сетевых ошибках."""
    if not is_configured():
        raise LLMError("Chybí OPENROUTER_API_KEY.")
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "decision", "strict": True, "schema": schema},
        },
        "provider": {"zdr": True, "data_collection": "deny"},
        "usage": {"include": True},
    }
    headers = {
        "Authorization": f"Bearer {settings.OPENROUTER_API_KEY}",
        "X-Title": "TrollTrace (research, UJEP)",  # hlavička musí být ASCII
    }
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT)
    try:
        for attempt in range(1, RETRIES + 1):
            try:
                response = client.post(API_URL, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                error = f"Síťová chyba: {exc}"
            else:
                if response.status_code == 200:
                    return _parse(response.json(), model)
                error = f"HTTP {response.status_code}: {response.text[:1000]}"
                if response.status_code not in {408, 429} and response.status_code < 500:
                    raise LLMError(error)
            logger.warning("OpenRouter, pokus %s/%s: %s", attempt, RETRIES, error)
            time.sleep(2**attempt)
        raise LLMError(error)
    finally:
        if own:
            client.close()


def _parse(body: dict, model: str) -> Completion:
    if body.get("error"):
        raise LLMError(str(body["error"])[:300])
    try:
        content = body["choices"][0]["message"]["content"]
        data = json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise LLMError(f"Neplatná odpověď modelu: {exc}") from exc
    usage = body.get("usage") or {}
    return Completion(
        data=data,
        model=body.get("model") or model,
        cost_usd=usage.get("cost"),
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
    )
