from pathlib import Path

base_path = Path("src/llm_router/providers/base.py")
zero_path = Path("src/llm_router/zero_cost_router.py")
test_path = Path("tests/test_error_classification_learning.py")

base = base_path.read_text()
old = '''class QuotaExceededError(ProviderRequestError):\n    def __init__(self, message: str, provider: str):\n        super().__init__(message, status_code=429)\n        self.provider = provider\n'''
new = '''class QuotaExceededError(ProviderRequestError):\n    def __init__(self, message: str, provider: str):\n        super().__init__(\n            message, status_code=429, error_class=ERROR_BILLING_OR_QUOTA\n        )\n        self.provider = provider\n'''
if base.count(old) != 1:
    raise RuntimeError("QuotaExceededError block mismatch")
base_path.write_text(base.replace(old, new, 1))

zero = zero_path.read_text()
old = '''        status.last_error = "local quota guard exhausted"\n        status.last_error_class = ERROR_RATE_LIMITED\n        status.last_polled = time.time()\n'''
new = '''        status.last_error = "local quota guard exhausted"\n        status.last_error_class = ERROR_BILLING_OR_QUOTA\n        status.last_polled = time.time()\n'''
if zero.count(old) != 1:
    raise RuntimeError("quota status block mismatch")
zero = zero.replace(old, new, 1)

old = '''        now = time.time()\n        status.last_error = str(exc)[:200]\n        status.last_polled = now\n        status.consecutive_failures += 1\n'''
new = '''        now = time.time()\n        status.last_error = str(exc)[:200]\n        status.last_error_class = getattr(exc, "error_class", "") or classify_provider_error(\n            getattr(exc, "status_code", None)\n        )\n        status.last_polled = now\n        status.consecutive_failures += 1\n'''
if zero.count(old) != 1:
    raise RuntimeError("zero-cost provider failure block mismatch")
zero_path.write_text(zero.replace(old, new, 1))

tests = test_path.read_text()
insert = '''\n\ndef test_zero_cost_failure_state_keeps_exact_classification(tmp_path):\n    import asyncio\n\n    async def run():\n        async with httpx.AsyncClient() as client:\n            router = ZeroCostModelRouter(settings(tmp_path), client)\n            router._mark_provider_failure(\n                "groq", ProviderUnavailable("upstream unavailable", status_code=503)\n            )\n            assert router.status["groq"].last_error_class == ERROR_PROVIDER_UNAVAILABLE\n            router._mark_quota_exhausted("groq")\n            assert router.status["groq"].last_error_class == ERROR_BILLING_OR_QUOTA\n            await router._metrics_store.close()\n\n    asyncio.run(run())\n'''
if "test_zero_cost_failure_state_keeps_exact_classification" in tests:
    raise RuntimeError("classification state regression already present")
test_path.write_text(tests + insert)
