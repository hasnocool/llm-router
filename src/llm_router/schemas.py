# src/llm_router/schemas.py
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class FlexibleModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class ImageURL(FlexibleModel):
    url: str
    detail: str | None = None


class ContentPart(FlexibleModel):
    type: str
    text: str | None = None
    image_url: ImageURL | dict[str, Any] | None = None


class Message(FlexibleModel):
    role: str
    content: str | list[ContentPart | dict[str, Any]] | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class ChatRequest(FlexibleModel):
    model: str = Field(default="")
    messages: list[Message]
    temperature: float | None = None
    max_tokens: int | None = None
    max_completion_tokens: int | None = None
    top_p: float | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None
    stop: str | list[str] | None = None
    stream: bool = False
    stream_options: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None
    response_format: dict[str, Any] | None = None
    seed: int | None = None
    user: str | None = None
    provider: str | None = None
    local_first: bool | None = None


class ChatMessage(FlexibleModel):
    role: str
    content: str | None = ""
    reasoning_content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class Choice(FlexibleModel):
    index: int = 0
    message: ChatMessage
    finish_reason: str | None = "stop"


class ChatUsage(FlexibleModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatResponse(FlexibleModel):
    id: str = "chatcmpl-router"
    object: Literal["chat.completion"] = "chat.completion"
    created: int = 0
    model: str = ""
    choices: list[Choice]
    usage: ChatUsage | None = None
    provider: str | None = None


class StreamingChunk(FlexibleModel):
    id: str = "chatcmpl-router"
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int = 0
    model: str = ""
    choices: list[dict[str, Any]] = Field(default_factory=list)
    provider: str | None = None


class ModelInfo(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int = 0
    owned_by: str


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelInfo]


class ProviderInfo(BaseModel):
    name: str
    available: bool
    model_count: int
    latency_ms: float
    last_error: str
    last_polled: float
    daily_calls_used: int = 0
    daily_calls_remaining: int | None = None
    daily_tokens_used: int = 0
    daily_tokens_remaining: int | None = None
    rate_limit_remaining: int | None = None
    rate_limit_reset: int | None = None
    latency_p50_ms: float = 0.0
    latency_p99_ms: float = 0.0


class ProviderList(BaseModel):
    providers: list[ProviderInfo]


class MetricsResponse(BaseModel):
    provider: str
    daily_calls_used: int
    daily_calls_remaining: int | None
    daily_tokens_used: int
    daily_tokens_remaining: int | None
    rate_limit_remaining: int | None
    rate_limit_reset: int | None
    rate_limit_type: str | None = None
    rate_limits: list[dict[str, Any]] = Field(default_factory=list)
    latency_p50_ms: float
    latency_p99_ms: float
    history: list[dict[str, Any]] = Field(default_factory=list)


class AllMetricsResponse(BaseModel):
    providers: dict[str, MetricsResponse]
