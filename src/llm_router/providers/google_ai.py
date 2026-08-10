# src/llm_router/providers/google_ai.py
from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

import httpx

from ..async_metrics import AsyncMetricsStore
from ..config import ProviderConfig
from .base import Provider, ProviderRequestError, ProviderUnavailable, get_forwarded_request_headers


class GoogleAIProvider(Provider):
    """Gemini REST adapter normalized to the OpenAI chat-completions contract."""

    def __init__(
        self,
        name: str,
        config: ProviderConfig,
        http: httpx.AsyncClient,
        metrics_store: AsyncMetricsStore | None = None,
        metrics_db: AsyncMetricsStore | None = None,
    ):
        super().__init__(name, config, http, metrics_store=metrics_store, metrics_db=metrics_db)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["x-goog-api-key"] = self.config.api_key
        headers.update(get_forwarded_request_headers())
        return headers

    def _url(self, path: str) -> str:
        return f"https://generativelanguage.googleapis.com/v1beta/{path}"

    @staticmethod
    def _normalize_model(model: str) -> str:
        return model if model.startswith("models/") else f"models/{model}"

    @staticmethod
    def _content_to_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks: list[str] = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    chunks.append(part["text"])
            return "".join(chunks)
        return str(content or "")

    def _openai_to_gemini_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        system_parts: list[dict[str, str]] = []
        contents: list[dict[str, Any]] = []
        tool_names: dict[str, str] = {}

        for message in payload.get("messages", []):
            role = message.get("role", "user")
            if role == "system":
                system_parts.append({"text": self._content_to_text(message.get("content", ""))})
                continue

            if role == "assistant":
                gemini_parts: list[dict[str, Any]] = []
                text = self._content_to_text(message.get("content", ""))
                if text:
                    gemini_parts.append({"text": text})
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    name = function.get("name")
                    if not name:
                        continue
                    call_id = call.get("id")
                    if call_id:
                        tool_names[call_id] = name
                    arguments = function.get("arguments", {})
                    if isinstance(arguments, str):
                        try:
                            arguments = json.loads(arguments)
                        except json.JSONDecodeError:
                            arguments = {"raw": arguments}
                    gemini_parts.append({"functionCall": {"name": name, "args": arguments or {}}})
                contents.append({"role": "model", "parts": gemini_parts or [{"text": ""}]})
                continue

            if role == "tool":
                name = message.get("name") or tool_names.get(message.get("tool_call_id", "")) or "tool"
                response = message.get("content", "")
                if isinstance(response, str):
                    try:
                        parsed: Any = json.loads(response)
                    except json.JSONDecodeError:
                        parsed = {"content": response}
                else:
                    parsed = response
                contents.append({
                    "role": "user",
                    "parts": [{"functionResponse": {"name": name, "response": parsed}}],
                })
                continue

            contents.append({
                "role": "user",
                "parts": [{"text": self._content_to_text(message.get("content", ""))}],
            })

        gemini: dict[str, Any] = {"contents": contents}
        if system_parts:
            gemini["systemInstruction"] = {"parts": system_parts}

        generation: dict[str, Any] = {}
        mappings = {
            "temperature": "temperature",
            "max_tokens": "maxOutputTokens",
            "top_p": "topP",
            "frequency_penalty": "frequencyPenalty",
            "presence_penalty": "presencePenalty",
        }
        for source, target in mappings.items():
            if payload.get(source) is not None:
                generation[target] = payload[source]
        stop = payload.get("stop")
        if stop:
            generation["stopSequences"] = [stop] if isinstance(stop, str) else stop
        response_format = payload.get("response_format") or {}
        if isinstance(response_format, dict) and response_format.get("type") in {"json_object", "json_schema"}:
            generation["responseMimeType"] = "application/json"
        if generation:
            gemini["generationConfig"] = generation

        function_declarations = []
        for tool in payload.get("tools") or []:
            if tool.get("type") != "function":
                continue
            function = tool.get("function") or {}
            declaration = {
                "name": function.get("name"),
                "description": function.get("description", ""),
                "parameters": function.get("parameters") or {"type": "object", "properties": {}},
            }
            if declaration["name"]:
                function_declarations.append(declaration)
        if function_declarations:
            gemini["tools"] = [{"functionDeclarations": function_declarations}]

        tool_choice = payload.get("tool_choice")
        if tool_choice == "none":
            gemini["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}
        elif tool_choice == "required":
            gemini["toolConfig"] = {"functionCallingConfig": {"mode": "ANY"}}
        elif isinstance(tool_choice, dict):
            name = (tool_choice.get("function") or {}).get("name")
            if name:
                gemini["toolConfig"] = {
                    "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [name]}
                }
        return gemini

    @staticmethod
    def _usage(data: dict[str, Any]) -> tuple[int, int]:
        metadata = data.get("usageMetadata") or {}
        return int(metadata.get("promptTokenCount", 0) or 0), int(metadata.get("candidatesTokenCount", 0) or 0)

    @staticmethod
    def _finish_reason(reason: str | None) -> str:
        return {
            "STOP": "stop",
            "MAX_TOKENS": "length",
            "SAFETY": "content_filter",
            "RECITATION": "content_filter",
        }.get(reason or "", "stop")

    def _gemini_to_openai(self, data: dict[str, Any], model: str) -> dict[str, Any]:
        candidate = (data.get("candidates") or [{}])[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for part in parts:
            if isinstance(part.get("text"), str):
                text_parts.append(part["text"])
            call = part.get("functionCall")
            if call:
                tool_calls.append({
                    "id": f"call_{uuid.uuid4().hex[:16]}",
                    "type": "function",
                    "function": {
                        "name": call.get("name", ""),
                        "arguments": json.dumps(call.get("args") or {}, separators=(",", ":")),
                    },
                })
        prompt_tokens, completion_tokens = self._usage(data)
        message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts)}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return {
            "id": f"chatcmpl-google-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": self._finish_reason(candidate.get("finishReason")),
            }],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    def _gemini_stream_chunk(self, data: dict[str, Any], model: str) -> str | None:
        candidate = (data.get("candidates") or [{}])[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        delta: dict[str, Any] = {}
        text = "".join(part.get("text", "") for part in parts if isinstance(part.get("text"), str))
        if text:
            delta["content"] = text
        tool_deltas = []
        for index, part in enumerate(parts):
            call = part.get("functionCall")
            if call:
                tool_deltas.append({
                    "index": index,
                    "id": f"call_{uuid.uuid4().hex[:16]}",
                    "type": "function",
                    "function": {
                        "name": call.get("name", ""),
                        "arguments": json.dumps(call.get("args") or {}, separators=(",", ":")),
                    },
                })
        if tool_deltas:
            delta["tool_calls"] = tool_deltas
        finish = candidate.get("finishReason")
        prompt_tokens, completion_tokens = self._usage(data)
        if not delta and not finish and not prompt_tokens and not completion_tokens:
            return None
        chunk: dict[str, Any] = {
            "id": f"chatcmpl-google-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "delta": delta,
                "finish_reason": self._finish_reason(finish) if finish else None,
            }],
        }
        if prompt_tokens or completion_tokens:
            chunk["usage"] = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            }
        return "data: " + json.dumps(chunk, separators=(",", ":"))

    async def models(self) -> list[dict[str, Any]]:
        t0 = time.perf_counter()
        status_code: int | None = None
        try:
            resp = await self._http.get(self._url("models"), headers=self._headers(), timeout=self.config.timeout_seconds)
            status_code = resp.status_code
            await self._record_rate_limits(dict(resp.headers))
            self._check_status(resp)
            data = resp.json()
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            await self._record_attempt(reservation_id=None, success=False, latency_ms=(time.perf_counter() - t0) * 1000, status_code=status_code, request_kind="model_discovery")
            raise ProviderUnavailable(f"{self.name} unreachable: {exc}") from exc
        except (ProviderUnavailable, ProviderRequestError):
            await self._record_attempt(reservation_id=None, success=False, latency_ms=(time.perf_counter() - t0) * 1000, status_code=status_code, request_kind="model_discovery")
            raise
        await self._record_attempt(reservation_id=None, success=True, latency_ms=(time.perf_counter() - t0) * 1000, status_code=status_code, request_kind="model_discovery")
        return [
            {"id": item["name"].removeprefix("models/"), "type": item.get("displayName", "")}
            for item in data.get("models", [])
            if "generateContent" in item.get("supportedGenerationMethods", [])
        ]

    async def complete(self, payload: dict[str, Any]) -> dict[str, Any]:
        reservation_id = await self._reserve_quota(payload)
        model = payload.get("model", self.config.default_model)
        t0 = time.perf_counter()
        status_code: int | None = None
        try:
            resp = await self._http.post(
                self._url(f"{self._normalize_model(model)}:generateContent"),
                headers=self._headers(),
                json=self._openai_to_gemini_payload(payload),
                timeout=self.config.timeout_seconds,
            )
            status_code = resp.status_code
            await self._record_rate_limits(dict(resp.headers))
            self._check_status(resp)
            data = resp.json()
            prompt_tokens, completion_tokens = self._usage(data)
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            await self._record_attempt(reservation_id=reservation_id, success=False, latency_ms=(time.perf_counter() - t0) * 1000, status_code=status_code)
            raise ProviderUnavailable(f"{self.name} unreachable: {exc}") from exc
        except (ProviderUnavailable, ProviderRequestError):
            await self._record_attempt(reservation_id=reservation_id, success=False, latency_ms=(time.perf_counter() - t0) * 1000, status_code=status_code)
            raise
        await self._record_attempt(
            reservation_id=reservation_id,
            success=True,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=(time.perf_counter() - t0) * 1000,
            status_code=status_code,
        )
        return self._gemini_to_openai(data, model)

    async def stream(self, payload: dict[str, Any]) -> AsyncIterator[str]:
        reservation_id = await self._reserve_quota(payload)
        model = payload.get("model", self.config.default_model)
        t0 = time.perf_counter()
        prompt_tokens = 0
        completion_tokens = 0
        status_code: int | None = None
        emitted = False
        recorded = False
        try:
            async with self._http.stream(
                "POST",
                self._url(f"{self._normalize_model(model)}:streamGenerateContent?alt=sse"),
                headers={**self._headers(), "Accept": "text/event-stream"},
                json=self._openai_to_gemini_payload(payload),
                timeout=self.config.stream_timeout_seconds,
            ) as resp:
                status_code = resp.status_code
                await self._record_rate_limits(dict(resp.headers))
                self._check_status(resp)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw:
                        continue
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    usage = self._usage(data)
                    prompt_tokens = max(prompt_tokens, usage[0])
                    completion_tokens = max(completion_tokens, usage[1])
                    chunk = self._gemini_stream_chunk(data, model)
                    if chunk:
                        emitted = True
                        yield chunk
            yield "data: [DONE]"
            await self._record_attempt(
                reservation_id=reservation_id,
                success=True,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=(time.perf_counter() - t0) * 1000,
                status_code=status_code,
            )
            recorded = True
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            await self._record_attempt(reservation_id=reservation_id, success=False, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, latency_ms=(time.perf_counter() - t0) * 1000, status_code=status_code)
            recorded = True
            if emitted:
                raise
            raise ProviderUnavailable(f"{self.name} unreachable during stream: {exc}") from exc
        except (ProviderUnavailable, ProviderRequestError):
            await self._record_attempt(reservation_id=reservation_id, success=False, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, latency_ms=(time.perf_counter() - t0) * 1000, status_code=status_code)
            recorded = True
            raise
        finally:
            if not recorded and self._metrics_store is not None:
                await self._metrics_store.cancel_reservation(reservation_id)
