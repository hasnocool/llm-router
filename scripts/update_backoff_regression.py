from __future__ import annotations

from pathlib import Path

path = Path("tests/test_provider_backoff.py")
text = path.read_text(encoding="utf-8")
old = '''@pytest.mark.asyncio
async def test_zero_cost_retryable_failure_uses_shared_threshold() -> None:
    async with httpx.AsyncClient() as client:
        router = ZeroCostModelRouter(_settings(strategy="zero-cost"), client)
        status = router.status["groq"]
        status.available = True
        error = ProviderUnavailable("upstream 503", status_code=503)

        with patch("llm_router.router.time.time", return_value=1000.0):
            router._mark_retryable_failure("groq", error)
            router._mark_retryable_failure("groq", error)
            assert status.available is True
            router._mark_retryable_failure("groq", error)

        assert status.available is False
        assert status.consecutive_failures == 3
'''
new = '''@pytest.mark.asyncio
async def test_zero_cost_retryable_failure_uses_soft_threshold() -> None:
    async with httpx.AsyncClient() as client:
        router = ZeroCostModelRouter(_settings(strategy="zero-cost"), client)
        status = router.status["groq"]
        status.available = True
        error = ProviderUnavailable("upstream 503", status_code=503)

        with patch("llm_router.zero_cost_router.time.time", return_value=1000.0):
            for _ in range(4):
                router._mark_retryable_failure("groq", error)
            assert status.available is True
            assert status.backoff_until == 0.0
            router._mark_retryable_failure("groq", error)

        assert status.available is False
        assert status.consecutive_failures == 5
        assert status.backoff_until == 1120.0
'''
if text.count(old) != 1:
    raise RuntimeError(f"expected one legacy threshold test, found {text.count(old)}")
path.write_text(text.replace(old, new, 1), encoding="utf-8")
Path(__file__).unlink()
