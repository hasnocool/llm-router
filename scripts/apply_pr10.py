# scripts/apply_pr10.py
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one match, found {count}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


def patch_router() -> None:
    replace_once(
        "src/llm_router/router.py",
        'logger = logging.getLogger(__name__)\n\n\n@dataclass\nclass ProviderStatus:\n',
        '''logger = logging.getLogger(__name__)\n\nPROVIDER_FAILURE_THRESHOLD = 3\nPROVIDER_BACKOFF_BASE_SECONDS = 300.0\nPROVIDER_BACKOFF_MAX_SECONDS = 3600.0\nPROVIDER_RATE_LIMIT_BACKOFF_SECONDS = 60.0\n\n\n@dataclass\nclass ProviderStatus:\n''',
    )
    replace_once(
        "src/llm_router/router.py",
        '''    latency_p50_ms: float = 0.0\n    latency_p99_ms: float = 0.0\n\n\nclass ModelRouter:\n''',
        '''    latency_p50_ms: float = 0.0\n    latency_p99_ms: float = 0.0\n    consecutive_failures: int = 0\n    backoff_until: float = 0.0\n\n\nclass ModelRouter:\n''',
    )
    replace_once(
        "src/llm_router/router.py",
        '''        self._model_failures[name] = 0\n        self._model_backoff_until[name] = 0.0\n        self._model_cache[name] = (now, models)\n        status = self.status[name]\n        status.available = True\n        status.model_count = len(models)\n        status.latency_ms = (time.perf_counter() - t0) * 1000\n        status.last_error = ""\n        status.last_polled = time.time()\n        return models\n\n    def _mark_provider_success(self, name: str, latency_ms: float) -> None:\n        status = self.status[name]\n        status.available = True\n        status.latency_ms = latency_ms\n        status.last_error = ""\n        status.last_polled = time.time()\n\n    def _mark_provider_failure(self, name: str, exc: BaseException) -> None:\n        status = self.status[name]\n        status.last_error = str(exc)[:200]\n        status.last_polled = time.time()\n        if not isinstance(exc, ProviderUnavailable) or exc.status_code != 429:\n            status.available = False\n\n    def ranked_providers(self) -> list[ProviderStatus]:\n        return sorted(\n            self.status.values(),\n            key=lambda status: (status.available, status.model_count, -status.latency_ms),\n            reverse=True,\n        )\n''',
        '''        self._model_failures[name] = 0\n        self._model_backoff_until[name] = 0.0\n        self._model_cache[name] = (now, models)\n        status = self.status[name]\n        status.model_count = len(models)\n        self._mark_provider_success(name, (time.perf_counter() - t0) * 1000)\n        return models\n\n    def _mark_provider_success(self, name: str, latency_ms: float) -> None:\n        status = self.status[name]\n        status.available = True\n        status.consecutive_failures = 0\n        status.backoff_until = 0.0\n        status.latency_ms = latency_ms\n        status.last_error = ""\n        status.last_polled = time.time()\n\n    def _mark_provider_failure(self, name: str, exc: BaseException) -> None:\n        status = self.status[name]\n        now = time.time()\n        status.last_error = str(exc)[:200]\n        status.last_polled = now\n        status.consecutive_failures += 1\n\n        # A provider that explicitly rate-limits us should be cooled down\n        # immediately. Other transient failures get two chances before the\n        # provider is removed from automatic routing.\n        if isinstance(exc, ProviderUnavailable) and exc.status_code == 429:\n            retry_at = exc.retry_after_until\n            fallback = now + PROVIDER_RATE_LIMIT_BACKOFF_SECONDS\n            status.available = False\n            status.backoff_until = max(\n                status.backoff_until,\n                float(retry_at) if retry_at is not None and retry_at > now else fallback,\n            )\n            return\n\n        if status.consecutive_failures < PROVIDER_FAILURE_THRESHOLD:\n            return\n\n        exponent = min(status.consecutive_failures - PROVIDER_FAILURE_THRESHOLD, 8)\n        delay = min(\n            PROVIDER_BACKOFF_MAX_SECONDS,\n            PROVIDER_BACKOFF_BASE_SECONDS * (2 ** exponent),\n        )\n        status.available = False\n        status.backoff_until = max(status.backoff_until, now + delay)\n\n    def _provider_in_backoff(self, name: str, *, now: float | None = None) -> bool:\n        status = self.status.get(name)\n        if status is None:\n            return False\n        current = time.time() if now is None else now\n        return status.backoff_until > current\n\n    def ranked_providers(self) -> list[ProviderStatus]:\n        now = time.time()\n        return sorted(\n            self.status.values(),\n            key=lambda status: (\n                status.backoff_until <= now,\n                status.available,\n                status.model_count,\n                -status.latency_ms,\n            ),\n            reverse=True,\n        )\n''',
    )
    replace_once(
        "src/llm_router/router.py",
        '''            candidates = [n for n in pool if n in available]\n''',
        '''            candidates = [\n                n for n in pool\n                if n in available and not self._provider_in_backoff(n)\n            ]\n''',
    )
    replace_once(
        "src/llm_router/router.py",
        '''        order_names = [primary]\n        for s in ranked:\n            if s.name != primary and s.name in available and s.name in pool:\n                order_names.append(s.name)\n''',
        '''        order_names = [] if self._provider_in_backoff(primary) else [primary]\n        for s in ranked:\n            if (\n                s.name != primary\n                and s.name in available\n                and s.name in pool\n                and not self._provider_in_backoff(s.name)\n            ):\n                order_names.append(s.name)\n''',
    )
    replace_once(
        "src/llm_router/router.py",
        '''                "latency_p50_ms": status.latency_p50_ms,\n                "latency_p99_ms": status.latency_p99_ms,\n                "rate_limits": [\n''',
        '''                "latency_p50_ms": status.latency_p50_ms,\n                "latency_p99_ms": status.latency_p99_ms,\n                "consecutive_failures": status.consecutive_failures,\n                "backoff_until": status.backoff_until,\n                "in_backoff": self._provider_in_backoff(name),\n                "rate_limits": [\n''',
    )


def patch_zero_cost_router() -> None:
    replace_once(
        "src/llm_router/zero_cost_router.py",
        '''    def _mark_retryable_failure(self, name: str, exc: ProviderUnavailable) -> None:\n        status = self.status.get(name)\n        if status is None:\n            return\n        status.last_error = str(exc)[:200]\n        status.last_polled = time.time()\n        if exc.status_code == 429:\n            status.rate_limit_remaining = 0\n            status.rate_limit_reset = exc.retry_after_until\n        else:\n            status.available = False\n''',
        '''    def _mark_retryable_failure(self, name: str, exc: ProviderUnavailable) -> None:\n        status = self.status.get(name)\n        if status is None:\n            return\n        self._mark_provider_failure(name, exc)\n        if exc.status_code == 429:\n            status.rate_limit_remaining = 0\n            status.rate_limit_reset = exc.retry_after_until\n''',
    )


def patch_routing_score() -> None:
    replace_once(
        "src/llm_router/routing_score.py",
        '''        if status.last_polled > 0 and not status.available:\n            reasons.append("unavailable")\n''',
        '''        if status.last_polled > 0 and not status.available:\n            # Tiered failures only hard-block a route during its active\n            # cooldown. Once the cooldown expires, allow a probe request so a\n            # recovered provider can self-heal without a process restart.\n            backoff_until = float(getattr(status, "backoff_until", 0.0) or 0.0)\n            if backoff_until > time.time():\n                reasons.append("unavailable")\n''',
    )


def patch_changelog() -> None:
    replace_once(
        "CHANGELOG.md",
        '''### Fixed\n\n''',
        '''### Fixed\n\n- Replaced one-failure provider blocking with a three-failure threshold, exponential automatic-routing backoff, immediate rate-limit cooldown, and success-based self-healing.\n''',
    )


def add_tests() -> None:
    target = ROOT / "tests/test_provider_backoff.py"
    target.write_text(
        '''# tests/test_provider_backoff.py\nfrom __future__ import annotations\n\nfrom unittest.mock import patch\n\nimport httpx\n\nfrom llm_router.config import ProviderConfig, Settings\nfrom llm_router.providers.base import ProviderUnavailable\nfrom llm_router.router import (\n    PROVIDER_BACKOFF_BASE_SECONDS,\n    ModelRouter,\n    ProviderStatus,\n)\nfrom llm_router.routing_score import ZeroCostPolicy, runtime_route_score\nfrom llm_router.schemas import ChatRequest, Message\nfrom llm_router.zero_cost_router import ZeroCostModelRouter\n\n\ndef _settings(*, strategy: str = "cloud-first") -> Settings:\n    return Settings(\n        strategy=strategy,\n        providers={\n            "groq": ProviderConfig("Groq", "https://groq.example", "groq-default"),\n            "huggingface": ProviderConfig("HF", "https://hf.example", "hf-default"),\n        },\n        routing_providers=["groq", "huggingface"],\n    )\n\n\ndef test_provider_is_blocked_only_after_three_consecutive_failures() -> None:\n    with httpx.Client():\n        pass\n    router = ModelRouter(_settings(), httpx.AsyncClient())\n    try:\n        status = router.status["groq"]\n        status.available = True\n        error = ProviderUnavailable("upstream 503", status_code=503)\n        with patch("llm_router.router.time.time", return_value=1000.0):\n            router._mark_provider_failure("groq", error)\n            assert status.available is True\n            assert status.consecutive_failures == 1\n            assert status.backoff_until == 0.0\n\n            router._mark_provider_failure("groq", error)\n            assert status.available is True\n            assert status.consecutive_failures == 2\n\n            router._mark_provider_failure("groq", error)\n            assert status.available is False\n            assert status.consecutive_failures == 3\n            assert status.backoff_until == 1000.0 + PROVIDER_BACKOFF_BASE_SECONDS\n\n            router._mark_provider_failure("groq", error)\n            assert status.backoff_until == 1000.0 + PROVIDER_BACKOFF_BASE_SECONDS * 2\n    finally:\n        import asyncio\n        asyncio.run(router._http.aclose())\n\n\ndef test_success_resets_failure_count_and_backoff() -> None:\n    router = ModelRouter(_settings(), httpx.AsyncClient())\n    try:\n        status = router.status["groq"]\n        status.available = False\n        status.consecutive_failures = 5\n        status.backoff_until = 9999.0\n        with patch("llm_router.router.time.time", return_value=1234.0):\n            router._mark_provider_success("groq", 42.0)\n        assert status.available is True\n        assert status.consecutive_failures == 0\n        assert status.backoff_until == 0.0\n        assert status.latency_ms == 42.0\n    finally:\n        import asyncio\n        asyncio.run(router._http.aclose())\n\n\ndef test_rate_limit_uses_retry_after_immediately() -> None:\n    router = ModelRouter(_settings(), httpx.AsyncClient())\n    try:\n        status = router.status["groq"]\n        status.available = True\n        error = ProviderUnavailable("rate limited", status_code=429, retry_after_until=1500)\n        with patch("llm_router.router.time.time", return_value=1000.0):\n            router._mark_provider_failure("groq", error)\n        assert status.available is False\n        assert status.consecutive_failures == 1\n        assert status.backoff_until == 1500.0\n    finally:\n        import asyncio\n        asyncio.run(router._http.aclose())\n\n\ndef test_automatic_routing_skips_backoff_but_explicit_provider_can_probe() -> None:\n    router = ModelRouter(_settings(), httpx.AsyncClient())\n    try:\n        router.status["groq"].available = False\n        router.status["groq"].backoff_until = 1500.0\n        auto = ChatRequest(model="auto", messages=[Message(role="user", content="hi")])\n        explicit = ChatRequest(model="groq", messages=[Message(role="user", content="hi")])\n        with patch("llm_router.router.time.time", return_value=1000.0):\n            assert [name for name, _ in router._order(auto)] == ["huggingface"]\n            assert router._order(explicit) == [("groq", "groq-default")]\n        with patch("llm_router.router.time.time", return_value=1600.0):\n            assert "groq" in [name for name, _ in router._order(auto)]\n    finally:\n        import asyncio\n        asyncio.run(router._http.aclose())\n\n\ndef test_zero_cost_score_retries_provider_after_backoff_expires() -> None:\n    status = ProviderStatus(\n        name="local", available=False, last_polled=1000.0, backoff_until=1300.0\n    )\n    policy = ZeroCostPolicy()\n    with patch("llm_router.routing_score.time.time", return_value=1200.0):\n        blocked = runtime_route_score("local", None, status, policy)\n    with patch("llm_router.routing_score.time.time", return_value=1400.0):\n        retryable = runtime_route_score("local", None, status, policy)\n    assert blocked.eligible is False\n    assert "unavailable" in blocked.reasons\n    assert retryable.eligible is True\n    assert "unavailable" not in retryable.reasons\n\n\ndef test_zero_cost_retryable_failure_uses_shared_threshold() -> None:\n    router = ZeroCostModelRouter(_settings(strategy="zero-cost"), httpx.AsyncClient())\n    try:\n        status = router.status["groq"]\n        status.available = True\n        error = ProviderUnavailable("upstream 503", status_code=503)\n        with patch("llm_router.router.time.time", return_value=1000.0):\n            router._mark_retryable_failure("groq", error)\n            router._mark_retryable_failure("groq", error)\n            assert status.available is True\n            router._mark_retryable_failure("groq", error)\n        assert status.available is False\n        assert status.consecutive_failures == 3\n    finally:\n        import asyncio\n        asyncio.run(router._http.aclose())\n''',
        encoding="utf-8",
    )


def main() -> None:
    patch_router()
    patch_zero_cost_router()
    patch_routing_score()
    patch_changelog()
    add_tests()


if __name__ == "__main__":
    main()
