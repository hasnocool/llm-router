# scripts/repair_pr9.py
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected exactly one match, found {count}: {old[:80]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


def replace_exact_count(path: str, old: str, new: str, expected: int) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != expected:
        raise RuntimeError(f"{path}: expected {expected} matches, found {count}: {old[:80]!r}")
    target.write_text(text.replace(old, new), encoding="utf-8")


def patch_config() -> None:
    replace_once(
        "src/llm_router/config.py",
        """        route = self.models.get(model)\n        if route is not None:\n            return route.provider, route.model\n        return \"huggingface\", model\n""",
        """        provider_name = normalize_provider(model)\n        if provider_name in self.providers:\n            return provider_name, self.provider(provider_name).default_model\n        route = self.models.get(model)\n        if route is not None:\n            return route.provider, route.model\n        return \"huggingface\", model\n""",
    )
    replace_once(
        "src/llm_router/config.py",
        """        level=resolved_env.get(\"LLM_ROUTER_LOG_LEVEL\", logs_raw.get(\"level\", \"INFO\")),\n""",
        """        level=str(resolved_env.get(\"LLM_ROUTER_LOG_LEVEL\") or logs_raw.get(\"level\", \"INFO\") or \"INFO\"),\n""",
    )


def patch_zero_cost_router() -> None:
    old_scores = """            scores = [\n                score for score in self.route_scores()\n                if score.provider in pool\n                and self.settings.provider(score.provider).default_model\n            ]\n"""
    new_scores = """            scores = [\n                score for score in self.route_scores()\n                if score.eligible\n                and score.provider in pool\n                and self.settings.provider(score.provider).default_model\n            ]\n"""
    replace_exact_count("src/llm_router/zero_cost_router.py", old_scores, new_scores, 2)
    replace_exact_count(
        "src/llm_router/zero_cost_router.py",
        """                if len(order) == 1:\n                    raise\n""",
        """                if explicit or exc.status_code != 404:\n                    raise\n""",
        2,
    )


def patch_google_ai() -> None:
    replace_once(
        "src/llm_router/providers/google_ai.py",
        """                if key == \"properties\":\n                    cleaned[key] = {\n                        name: GoogleAIProvider._sanitize_schema(sub)\n                        for name, sub in value.items()\n                        if isinstance(value, dict)\n                    }\n""",
        """                if key == \"properties\":\n                    if not isinstance(value, dict):\n                        continue\n                    cleaned[key] = {\n                        name: GoogleAIProvider._sanitize_schema(sub)\n                        for name, sub in value.items()\n                    }\n""",
    )


def patch_request_classification() -> None:
    replace_once(
        "src/llm_router/providers/base.py",
        """def classify_request_kind(payload: dict[str, Any]) -> str:\n    if payload.get(\"tools\") or payload.get(\"tool_choice\"):\n        return \"tool_call\"\n    return \"chat\"\n""",
        """def classify_request_kind(payload: dict[str, Any]) -> str:\n    tool_choice = payload.get(\"tool_choice\")\n    if tool_choice == \"none\":\n        return \"chat\"\n    if payload.get(\"tools\") or tool_choice:\n        return \"tool_call\"\n    return \"chat\"\n""",
    )


def patch_auth() -> None:
    replace_once(
        "src/llm_router/main.py",
        """async def optional_router_auth(request: Request, call_next):\n    if _settings is not None and _settings.router_api_key and request.url.path.startswith(\"/v1/\"):\n""",
        """async def optional_router_auth(request: Request, call_next):\n    protected = request.url.path.startswith((\"/v1/\", \"/dashboard\", \"/logs\", \"/analytics\"))\n    if _settings is not None and _settings.router_api_key and protected:\n""",
    )


def patch_router_typing_and_dashboard() -> None:
    path = "src/llm_router/router.py"
    replace_once(
        path,
        """                extra = {\"source\": source}\n""",
        """                extra: dict[str, object] = {\"source\": source}\n""",
    )
    replace_once(
        path,
        """    @staticmethod\n    def _estimate_cost(prompt_tokens: int, completion_tokens: int, input_rate: float, output_rate: float) -> float:\n""",
        """    @staticmethod\n    def _as_int(value: object, default: int = 0) -> int:\n        if isinstance(value, bool):\n            return int(value)\n        if isinstance(value, (int, float, str)):\n            try:\n                return int(value)\n            except (TypeError, ValueError):\n                return default\n        return default\n\n    @staticmethod\n    def _as_float(value: object, default: float = 0.0) -> float:\n        if isinstance(value, bool):\n            return float(value)\n        if isinstance(value, (int, float, str)):\n            try:\n                return float(value)\n            except (TypeError, ValueError):\n                return default\n        return default\n\n    @staticmethod\n    def _estimate_cost(prompt_tokens: int, completion_tokens: int, input_rate: float, output_rate: float) -> float:\n""",
    )
    replace_once(
        path,
        """        provider_rows = {row.get(\"provider_name\") or \"\": row for row in provider_attempts}\n        router_rows = {row.get(\"provider_name\") or \"\": row for row in router_attempts}\n""",
        """        provider_rows = {str(row.get(\"provider_name\") or \"\"): row for row in provider_attempts}\n        router_rows = {str(row.get(\"provider_name\") or \"\"): row for row in router_attempts}\n""",
    )
    conversions = {
        'int(attempt.get("attempts") or 0)': 'self._as_int(attempt.get("attempts"))',
        'int(attempt.get("successes") or 0)': 'self._as_int(attempt.get("successes"))',
        'int(attempt.get("failures") or max(0, attempts - successes))': 'self._as_int(attempt.get("failures"), max(0, attempts - successes))',
        'int(route.get("failovers") or 0)': 'self._as_int(route.get("failovers"))',
        'int(attempt.get("prompt_tokens") or 0)': 'self._as_int(attempt.get("prompt_tokens"))',
        'int(attempt.get("completion_tokens") or 0)': 'self._as_int(attempt.get("completion_tokens"))',
        'int(attempt.get("total_tokens") or 0)': 'self._as_int(attempt.get("total_tokens"))',
        'float(attempt.get("avg_latency_ms") or 0.0)': 'self._as_float(attempt.get("avg_latency_ms"))',
        'int(route.get("streams") or 0)': 'self._as_int(route.get("streams"))',
        'int(route.get("explicit_requests") or 0)': 'self._as_int(route.get("explicit_requests"))',
        'int(route.get("requests") or 0)': 'self._as_int(route.get("requests"))',
        'sum(int(row.get("requests") or 0) for row in router_attempts)': 'sum(self._as_int(row.get("requests")) for row in router_attempts)',
    }
    for old, new in conversions.items():
        replace_once(path, old, new)

    replace_once(
        path,
        """                out.append(ModelInfo(id=name, owned_by=\"router\"))\n""",
        """                seen.add(name)\n                out.append(ModelInfo(id=name, owned_by=\"router\"))\n""",
    )
    replace_once(
        path,
        """        total_calls_remaining = 0\n        total_tokens_remaining = 0\n""",
        """        total_calls_remaining: int | None = None\n        total_tokens_remaining: int | None = None\n""",
    )
    replace_once(
        path,
        """            if calls_remaining is not None:\n                total_calls_remaining += calls_remaining\n            if tokens_remaining is not None:\n                total_tokens_remaining += tokens_remaining\n""",
        """            if calls_remaining is not None:\n                total_calls_remaining = (total_calls_remaining or 0) + calls_remaining\n            if tokens_remaining is not None:\n                total_tokens_remaining = (total_tokens_remaining or 0) + tokens_remaining\n""",
    )


def patch_existing_tests() -> None:
    replace_once(
        "tests/test_dashboard_metrics.py",
        '    assert classify_request_kind({"tools": [], "tool_choice": "none"}) == "tool_call"\n',
        '    assert classify_request_kind({"tools": [], "tool_choice": "none"}) == "chat"\n',
    )
    target = ROOT / "tests/test_dashboard_metrics.py"
    text = target.read_text(encoding="utf-8")
    old = """        assert page.status_code in (200, 500)\n        if page.status_code == 200:\n            assert page.headers[\"content-type\"].startswith(\"text/html\")\n"""
    if old in text:
        target.write_text(
            text.replace(
                old,
                """        assert page.status_code == 200\n        assert page.headers[\"content-type\"].startswith(\"text/html\")\n""",
                1,
            ),
            encoding="utf-8",
        )


def add_regression_tests() -> None:
    target = ROOT / "tests/test_merge_gate_regressions.py"
    target.write_text(
        '''# tests/test_merge_gate_regressions.py\nfrom __future__ import annotations\n\nfrom types import MethodType\n\nimport httpx\n\nfrom llm_router.config import ProviderConfig, Settings\nfrom llm_router.providers.base import classify_request_kind\nfrom llm_router.providers.google_ai import GoogleAIProvider\nfrom llm_router.routing_score import RouteScore\nfrom llm_router.schemas import ChatRequest, Message\nfrom llm_router.task_classifier import TaskProfile\nfrom llm_router.zero_cost_router import ZeroCostModelRouter\n\n\ndef _provider(name: str, model: str) -> ProviderConfig:\n    return ProviderConfig(name=name, base_url=f"https://{name}.example", default_model=model)\n\n\ndef test_bare_provider_id_resolves_to_its_default_model() -> None:\n    settings = Settings(\n        providers={\n            "groq": _provider("groq", "groq-default"),\n            "huggingface": _provider("huggingface", "hf-default"),\n        }\n    )\n    assert settings.resolve("groq") == ("groq", "groq-default")\n\n\ndef test_tool_choice_none_is_chat_even_when_tools_are_present() -> None:\n    assert classify_request_kind({"tools": [{"type": "function"}], "tool_choice": "none"}) == "chat"\n\n\ndef test_gemini_schema_sanitizer_ignores_malformed_properties() -> None:\n    cleaned = GoogleAIProvider._sanitize_schema({"type": "object", "properties": "invalid"})\n    assert cleaned == {"type": "object"}\n\n\nasync def test_auto_routing_excludes_ineligible_scores() -> None:\n    settings = Settings(\n        strategy="zero-cost",\n        providers={\n            "groq": _provider("groq", "groq-default"),\n            "huggingface": _provider("huggingface", "hf-default"),\n        },\n        routing_providers=["groq", "huggingface"],\n    )\n    async with httpx.AsyncClient() as client:\n        router = ZeroCostModelRouter(settings, client)\n\n        def fake_scores(self, *, primary=None):\n            return [\n                RouteScore("groq", None, 99, 99.0, "recurring", False, ("quota-exhausted",)),\n                RouteScore("huggingface", None, 80, 80.0, "recurring", True),\n            ]\n\n        router.route_scores = MethodType(fake_scores, router)\n        req = ChatRequest(model="auto", messages=[Message(role="user", content="hi")])\n        profile = TaskProfile(kind="general", confidence=1.0)\n        assert router._order_with_profile(req, profile) == [("huggingface", "hf-default")]\n''',
        encoding="utf-8",
    )


def main() -> None:
    patch_config()
    patch_zero_cost_router()
    patch_google_ai()
    patch_request_classification()
    patch_auth()
    patch_router_typing_and_dashboard()
    patch_existing_tests()
    add_regression_tests()


if __name__ == "__main__":
    main()
