from __future__ import annotations

import httpx
import pytest

from llm_router.config import ModelRoute, ProviderConfig, Settings, load_settings
from llm_router.providers.base import ProviderRequestError, ProviderUnavailable
from llm_router.router import ModelRouter
from llm_router.schemas import ChatRequest, Message


def make_settings() -> Settings:
    return Settings(
        strategy="cloud-first",
        providers={
            "huggingface": ProviderConfig(
                name="HF", base_url="https://hf.example", default_model="hf-default"
            ),
            "local": ProviderConfig(
                name="Local", base_url="http://localhost:8081", default_model="granite"
            ),
        },
        models={
            "cloud-alias": ModelRoute(provider="huggingface", model="Qwen/Qwen3-8B"),
            "local-alias": ModelRoute(provider="local", model="granite"),
        },
    )


def make_router(settings: Settings, transport: httpx.AsyncBaseTransport | None) -> ModelRouter:
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return ModelRouter(settings, client)


def req(model: str = "cloud-alias", **kw) -> ChatRequest:
    return ChatRequest(
        model=model, messages=[Message(role="user", content="hi")], **kw
    )


def json_response(body: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=body, request=httpx.Request("POST", "http://test"))


def ok_body(model: str, content: str = "ok") -> dict:
    return {
        "id": "x",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class TestOrdering:
    def test_cloud_first(self):
        r = make_router(make_settings(), None)
        assert r._order(req()) == [("huggingface", "Qwen/Qwen3-8B"), ("local", "granite")]

    def test_local_alias_primary(self):
        r = make_router(make_settings(), None)
        assert r._order(req("local-alias"))[0] == ("local", "granite")

    def test_explicit_provider_keeps_resolved_model(self):
        r = make_router(make_settings(), None)
        assert r._order(req(provider="local")) == [("local", "Qwen/Qwen3-8B")]

    def test_explicit_provider_with_colon(self):
        r = make_router(make_settings(), None)
        assert r._order(req("local:granite")) == [("local", "granite")]

    def test_local_first_flag(self):
        r = make_router(make_settings(), None)
        assert r._order(req("cloud-alias", local_first=True)) == [
            ("local", "granite"),
            ("huggingface", "Qwen/Qwen3-8B"),
        ]


class TestFailover:
    async def test_failover_to_local(self):
        calls = []

        async def handler(request):
            calls.append(request.url.path)
            if len(calls) == 1:
                return json_response({}, status=503)
            return json_response(ok_body("granite", "from-local"))

        r = make_router(make_settings(), httpx.MockTransport(handler))
        resp = await r.complete(req())
        assert resp.provider == "local"
        assert resp.choices[0].message.content == "from-local"

    async def test_bad_request_does_not_failover(self):
        async def handler(request):
            return json_response({"error": "bad"}, status=400)

        r = make_router(make_settings(), httpx.MockTransport(handler))
        with pytest.raises(ProviderRequestError):
            await r.complete(req())

    async def test_all_down_raises(self):
        async def handler(request):
            return json_response({}, status=503)

        r = make_router(make_settings(), httpx.MockTransport(handler))
        with pytest.raises(ProviderUnavailable):
            await r.complete(req())

    async def test_connection_error_failover(self):
        async def handler(request):
            if request.url.host == "hf.example":
                raise httpx.ConnectError("boom", request=request)
            return json_response(ok_body("granite"))

        r = make_router(make_settings(), httpx.MockTransport(handler))
        resp = await r.complete(req())
        assert resp.provider == "local"

    async def test_stream_failover_emits_annotated_sse(self):
        async def handler(request):
            if request.url.host == "hf.example":
                raise httpx.ConnectError("boom", request=request)
            sse = (
                'data: {"id":"s","choices":[{"index":0,"delta":{"content":"hi"}}]}'
                "\n\n"
                "data: [DONE]"
                "\n\n"
            )
            return httpx.Response(
                200, content=sse, request=request, headers={"content-type": "text/event-stream"}
            )

        r = make_router(make_settings(), httpx.MockTransport(handler))
        lines = [ln async for ln in r.stream(req())]
        assert any('"provider": "local"' in ln for ln in lines)


class TestAvailability:
    async def test_poll_ranked_providers(self):
        async def handler(request):
            if request.url.path.endswith("/models"):
                return json_response({"data": [{"id": "m1"}, {"id": "m2"}]})
            return json_response(ok_body("m1"))

        r = make_router(make_settings(), httpx.MockTransport(handler))
        status = await r.poll_all_providers()
        ranked = r.ranked_providers()
        assert len(ranked) == 2
        assert all(s.available for s in ranked)
        assert ranked[0].model_count >= ranked[1].model_count

    async def test_unavailable_provider_ranks_last(self):
        async def handler(request):
            if request.url.host == "hf.example":
                return json_response({}, status=503)
            return json_response({"data": [{"id": "m1"}]})

        r = make_router(make_settings(), httpx.MockTransport(handler))
        await r.poll_all_providers()
        ranked = r.ranked_providers()
        assert ranked[-1].name == "huggingface"
        assert not ranked[-1].available


class TestConfig:
    def test_load_settings(self, tmp_path):
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            """
[providers.huggingface]
base_url = "https://router.huggingface.co/v1"
default_model = "Qwen/Qwen3-8B"
api_key_env = "HF_TOKEN"

[providers.local]
base_url = "http://localhost:8081"
default_model = "granite"

[providers.nvidia]
base_url = "https://integrate.api.nvidia.com/v1"
default_model = "meta/llama-3.3-70b-instruct"
api_key_env = "NVIDIA_API_KEY"

[models]
qwen = { provider = "huggingface", model = "Qwen/Qwen3-8B" }
"""
        )
        s = load_settings(cfg, env={"HF_TOKEN": "hf_test", "PORT": "9000"})
        assert s.provider("huggingface").api_key == "hf_test"
        assert s.port == 9000
        assert s.resolve("qwen") == ("huggingface", "Qwen/Qwen3-8B")
        assert s.resolve("hf:Some/Model") == ("huggingface", "Some/Model")
        assert "nvidia" in s.providers
