import json
import logging
from typing import Any

import httpx

from .config import settings

logger = logging.getLogger(__name__)

DEEPSEEK_MODEL_ALIASES = {"deepseek": "deepseek-chat"}


def _resolve_model(name: str) -> str:
    return DEEPSEEK_MODEL_ALIASES.get(name.strip().lower(), name)


def _log_messages_summary(messages: list[dict[str, str]], max_chars: int = 200) -> str:
    parts = []
    for i, m in enumerate(messages):
        role = m.get("role", "?")
        content = (m.get("content") or "")
        length = len(content)
        preview = content[:max_chars] + "..." if length > max_chars else content
        parts.append(f"[{i}] {role}({length} chars): {preview!r}")
    return " | ".join(parts)


async def chat_completion(
    messages: list[dict[str, str]],
    *,
    json_mode: bool = True,
    timeout: float = 120.0,
) -> str:
    base = settings.llm_base_url.rstrip("/")
    if "/v1" not in base and "deepseek" in base.lower():
        base = f"{base}/v1"
    url = f"{base}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.llm_api_key}",
        "Content-Type": "application/json",
    }
    model = _resolve_model(settings.llm_model)
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    logger.info(
        "LLM request: url=%s model=%s messages_count=%s json_mode=%s timeout=%s | %s",
        url,
        model,
        len(messages),
        json_mode,
        timeout,
        _log_messages_summary(messages),
    )

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=body)
        resp.raise_for_status()

    data = resp.json()
    msg = data["choices"][0].get("message") or {}
    content = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or ""

    if not content and reasoning:
        content = reasoning
        logger.info("LLM: using reasoning_content as content (content was empty)")
    content = (content or "").strip()
    usage = data.get("usage") or {}
    logger.info(
        "LLM response: status=%s content_len=%s reasoning_len=%s usage=%s",
        resp.status_code,
        len(content),
        len(reasoning),
        usage,
    )
    if content and len(content) <= 600:
        logger.debug("LLM response body: %s", content)
    elif content:
        logger.debug("LLM response preview: %s...", content[:500])
    return content


def _find_json_objects(text: str) -> list[tuple[int, int]]:
    text = text.strip()
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(text):
        start = text.find("{", i)
        if start == -1:
            break
        depth = 0
        end = -1
        for j in range(start, len(text)):
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break
        if end != -1:
            spans.append((start, end))
            i = end
        else:
            i = start + 1
    return spans


def extract_json_from_text(text: str) -> dict[str, Any]:
    text = text.strip()
    spans = _find_json_objects(text)
    if not spans:
        raise ValueError("JSON object not found in response")
    for start, end in reversed(spans):
        chunk = text[start:end]
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            continue
    raise ValueError("No valid JSON object found in response")
