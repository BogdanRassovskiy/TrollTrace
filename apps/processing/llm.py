"""Клиент OpenRouter (OpenAI-совместимый API) для разметки нарративов.

Тексты уходят внешнему провайдеру. Ограничения провайдеров — настройки: нулевое хранение
(`provider.zdr`, `llm-zero-retention`) и запрет обучения на запросах
(`provider.data_collection = deny`, `llm-deny-training`). С 7.11.2026 оба выключены: тексты
публичные, личных данных исследования в запросах нет (решение исследователя), а без
ограничений доступно больше дешёвых провайдеров. `require_parameters` не ставится:
с ним OpenRouter отсекает почти всех провайдеров Anthropic и OpenAI, — ответ проверяется сам.
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
    reasoning_tokens: int | None = None  # входят в completion_tokens


def split_model(spec: str) -> tuple[str, str]:
    """«openai/gpt-6-luna@low» → (model, уровень рассуждений); без «@» — ("…", "")."""
    model, _, effort = spec.partition("@")
    return model, effort


def is_configured() -> bool:
    return bool(settings.OPENROUTER_API_KEY)


def complete_json(model: str, system: str, user: str, schema: dict, *,
                  client: httpx.Client | None = None,
                  max_tokens: int | None = None, reasoning_effort: str = "",
                  zero_retention: bool | None = None,
                  deny_training: bool | None = None) -> Completion:  # fmt: skip
    """Один запрос со строгой JSON-схемой ответа; повтор при 429/5xx и сетевых ошибках.

    max_tokens — потолок ответа: без него OpenRouter резервирует под ответ максимум модели
    и при малом остатке кредитов отказывает (402), даже если ответ короткий."""
    if not is_configured():
        raise LLMError("Chybí OPENROUTER_API_KEY.")
    model, spec_effort = split_model(model)
    reasoning_effort = spec_effort or reasoning_effort
    from apps.panel.defaults import get_setting

    if zero_retention is None:
        zero_retention = bool(get_setting("llm-zero-retention"))
    if deny_training is None:
        deny_training = bool(get_setting("llm-deny-training"))
    provider = {}
    if deny_training:
        provider["data_collection"] = "deny"
    if zero_retention:
        provider["zdr"] = True
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "decision", "strict": True, "schema": schema},
        },
        "usage": {"include": True},
    }
    if provider:
        payload["provider"] = provider
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if reasoning_effort:
        payload["reasoning"] = {"effort": reasoning_effort}
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
        choice = body["choices"][0]
        content = choice["message"]["content"]
        if not content and choice.get("finish_reason") == "length":
            raise LLMError("Odpověď modelu useknuta limitem tokenů (finish_reason=length).")
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
        reasoning_tokens=(usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
    )
