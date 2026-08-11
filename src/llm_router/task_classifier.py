# src/llm_router/task_classifier.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings


@dataclass(frozen=True, slots=True)
class TaskProfile:
    kind: str
    confidence: float
    needs_tools: bool = False
    needs_json: bool = False
    needs_vision: bool = False
    needs_long_context: bool = False
    speed_sensitive: bool = False
    coding_heavy: bool = False


def classify_task(payload: dict[str, Any]) -> TaskProfile:
    text = _payload_text(payload).lower()
    tools = bool(payload.get("tools") or payload.get("tool_choice"))
    needs_json = any(k in text for k in ("json", "schema", "structured output", "structured json"))
    needs_vision = any(k in text for k in ("image", "screenshot", "vision", "ocr"))
    needs_long_context = len(text) > 6000 or any(k in text for k in ("long context", "entire document", "full transcript"))
    coding = any(k in text for k in ("python", "javascript", "typescript", "bug", "stack trace", "refactor", "code"))
    speed = any(k in text for k in ("quick", "fast", "low latency", "speed", "cheap", "simple"))
    reasoning = any(k in text for k in ("reason", "compare", "analyze", "plan", "tradeoff", "why"))

    kind = "general"
    confidence = 0.35
    if needs_vision:
        kind, confidence = "vision", 0.95
    elif needs_json:
        kind, confidence = "structured", 0.90
    elif tools:
        kind, confidence = "tool_use", 0.90
    elif coding:
        kind, confidence = "coding", 0.85
    elif needs_long_context:
        kind, confidence = "long_context", 0.80
    elif reasoning:
        kind, confidence = "reasoning", 0.60
    elif speed:
        kind, confidence = "fast", 0.55
    elif len(text) < 80:
        kind, confidence = "simple", 0.55

    return TaskProfile(
        kind=kind,
        confidence=confidence,
        needs_tools=tools,
        needs_json=needs_json,
        needs_vision=needs_vision,
        needs_long_context=needs_long_context,
        speed_sensitive=speed,
        coding_heavy=coding,
    )


async def refine_task_profile_with_model(
    settings: Settings,
    payload: dict[str, Any],
    base: TaskProfile,
) -> TaskProfile:
    if not settings.classifier.fallback_enabled:
        return base
    if base.confidence >= settings.classifier.fallback_confidence_threshold:
        return base

    openrouter = settings.providers.get("openrouter")
    if not openrouter or not openrouter.api_key:
        return base

    prompt = _payload_text(payload)
    if not prompt.strip():
        return base

    system = (
        "Classify the user task into one label: coding, structured, tool_use, vision, "
        "long_context, reasoning, fast, simple, general. Return JSON only with keys "
        "kind, confidence, needs_tools, needs_json, needs_vision, needs_long_context, speed_sensitive, coding_heavy."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt[:4000]},
    ]

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{openrouter.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {openrouter.api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://llm-router.local",
                    "X-Title": "llm-router",
                },
                json={
                    "model": "openrouter/free",
                    "messages": messages,
                    "temperature": 0,
                    "max_tokens": 128,
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            data = resp.json()
            text = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
            if not text:
                return base
            import json

            parsed = json.loads(text)
    except Exception:
        return base

    kind = str(parsed.get("kind") or base.kind)
    confidence = float(parsed.get("confidence") or base.confidence)
    return TaskProfile(
        kind=kind,
        confidence=max(base.confidence, confidence),
        needs_tools=bool(parsed.get("needs_tools", base.needs_tools)),
        needs_json=bool(parsed.get("needs_json", base.needs_json)),
        needs_vision=bool(parsed.get("needs_vision", base.needs_vision)),
        needs_long_context=bool(parsed.get("needs_long_context", base.needs_long_context)),
        speed_sensitive=bool(parsed.get("speed_sensitive", base.speed_sensitive)),
        coding_heavy=bool(parsed.get("coding_heavy", base.coding_heavy)),
    )


def _payload_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for message in payload.get("messages", []):
        if not isinstance(message, dict):
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str):
                        parts.append(text)
    return " ".join(parts)
