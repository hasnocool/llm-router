from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "src/llm_router/zero_cost_router.py"
CI = ROOT / ".github/workflows/ci.yml"

ORIGINAL_CI = '''# .github/workflows/ci.yml
name: CI

on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
      - name: Install project and QA tools
        run: python -m pip install -e '.[dev]' ruff pyright
      - name: Compile
        run: python -m compileall -q src tests
      - name: Ruff critical checks
        run: ruff check src tests --select E9,F63,F7,F82
      - name: Tests
        run: pytest -q
      - name: Pyright
        run: pyright src
'''


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


text = TARGET.read_text(encoding="utf-8")

text = replace_once(
    text,
    'from .schemas import ChatRequest, ChatResponse\n\n\nclass ZeroCostModelRouter',
    '''from .schemas import ChatRequest, ChatResponse


MAX_MODEL_ATTEMPTS_PER_PROVIDER = 8
ZERO_COST_PROVIDER_FAILURE_THRESHOLD = 5
ZERO_COST_PROVIDER_BACKOFF_BASE_SECONDS = 120.0
ZERO_COST_PROVIDER_BACKOFF_MAX_SECONDS = 1800.0
ZERO_COST_RATE_LIMIT_BACKOFF_SECONDS = 60.0


class ZeroCostModelRouter''',
    "fallback constants",
)

old_order = '''    async def _order_for_request(
        self, req: ChatRequest
    ) -> tuple[list[tuple[str, str]], TaskProfile]:
        if self.settings.strategy != "zero-cost":
            profile = TaskProfile(kind="general", confidence=1.0)
            return self._order(req), profile
        profile = await self._task_profile(req)
        return self._order_with_profile(req, profile), profile
'''
new_order = '''    async def _order_for_request(
        self, req: ChatRequest
    ) -> tuple[list[tuple[str, str]], TaskProfile]:
        if self.settings.strategy != "zero-cost":
            profile = TaskProfile(kind="general", confidence=1.0)
            return self._order(req), profile
        if self._is_explicit(req):
            profile = TaskProfile(kind="general", confidence=1.0)
            return self._order(req), profile
        profile = await self._task_profile(req)
        base_order = self._order_with_profile(req, profile)
        return await self._expand_model_fallbacks(base_order, profile), profile

    def _provider_model_candidates(
        self,
        provider: str,
        preferred: str,
        catalog: dict[str, list[dict[str, object]]],
    ) -> list[str]:
        candidates: list[str] = []

        def add(model: object) -> None:
            model_id = str(model or "").strip()
            if model_id and model_id not in candidates:
                candidates.append(model_id)

        add(preferred)
        add(self.settings.provider(provider).default_model)
        for route in self.settings.models.values():
            if route.provider == provider:
                add(route.model)
        cached = self._model_cache.get(provider)
        if cached:
            for item in cached[1]:
                add(item.get("id"))
        for item in catalog.get(provider, []):
            add(item.get("model_id"))
        return candidates

    def _rank_model_alternatives(
        self,
        provider: str,
        models: list[str],
        profile: TaskProfile,
        task_rows: list[dict[str, object]],
    ) -> list[str]:
        if len(models) <= 1:
            return models
        preferred, alternatives = models[0], models[1:]
        rows_by_model: dict[str, list[dict[str, object]]] = {}
        for row in task_rows:
            if str(row.get("provider_name") or "") != provider:
                continue
            rows_by_model.setdefault(str(row.get("model") or ""), []).append(row)

        def key(item: tuple[int, str]) -> tuple[float, ...]:
            index, model = item
            rows = rows_by_model.get(model, [])
            matching = [row for row in rows if str(row.get("task_kind") or "") == profile.kind]
            matching_attempts = sum(self._as_int(row.get("attempts")) for row in matching)
            matching_successes = sum(self._as_int(row.get("successes")) for row in matching)
            total_attempts = sum(self._as_int(row.get("attempts")) for row in rows)
            total_successes = sum(self._as_int(row.get("successes")) for row in rows)
            matching_rate = matching_successes / matching_attempts if matching_attempts else 0.0
            total_rate = total_successes / total_attempts if total_attempts else 0.0
            # Current-task history is a preference, never an eligibility gate.
            # Models learned under other tasks remain later fallback candidates.
            return (
                0.0 if matching_attempts else 1.0,
                -matching_rate,
                -float(matching_attempts),
                -total_rate,
                -float(total_attempts),
                float(index),
            )

        ranked = [model for _, model in sorted(enumerate(alternatives), key=key)]
        return [preferred, *ranked]

    async def _expand_model_fallbacks(
        self,
        order: list[tuple[str, str]],
        profile: TaskProfile,
    ) -> list[tuple[str, str]]:
        if not order:
            return []
        catalog, task_rows = await self._metrics_store.run_blocking(
            lambda db: (db.get_all_provider_models(), db.get_model_task_metrics(days=30))
        )
        expanded: list[tuple[str, str]] = []
        for provider, preferred in order:
            models = self._provider_model_candidates(provider, preferred, catalog)
            ranked = self._rank_model_alternatives(provider, models, profile, task_rows)
            expanded.extend(
                (provider, model)
                for model in ranked[:MAX_MODEL_ATTEMPTS_PER_PROVIDER]
            )
        return expanded
'''
text = replace_once(text, old_order, new_order, "async model fallback order")

text = replace_once(
    text,
    '''        errors: list[str] = []
        order, profile = await self._order_for_request(req)
''',
    '''        errors: list[str] = []
        failed_providers: set[str] = set()
        order, profile = await self._order_for_request(req)
''',
    "complete failed-provider set",
)

text = replace_once(
    text,
    '''        for idx, (name, model) in enumerate(order):
            provider = self.providers[name]
            t0 = time.perf_counter()
            try:
''',
    '''        for idx, (name, model) in enumerate(order):
            if name in failed_providers:
                continue
            provider = self.providers[name]
            t0 = time.perf_counter()
            try:
''',
    "complete skip failed provider",
)

text = replace_once(
    text,
    '''            except QuotaExceededError as exc:
                errors.append(f"{name}: {exc}")
                self._mark_quota_exhausted(name)
''',
    '''            except QuotaExceededError as exc:
                errors.append(f"{name}/{model}: {exc}")
                failed_providers.add(name)
                self._mark_quota_exhausted(name)
''',
    "complete quota failure",
)

text = replace_once(
    text,
    '''            except ProviderUnavailable as exc:
                errors.append(f"{name}: {exc}")
                self._mark_retryable_failure(name, exc)
''',
    '''            except ProviderUnavailable as exc:
                errors.append(f"{name}/{model}: {exc}")
                if exc.status_code != 413:
                    self._mark_retryable_failure(name, exc)
                    failed_providers.add(name)
''',
    "complete provider unavailable",
)

text = replace_once(
    text,
    '''            except ProviderRequestError as exc:
                if explicit or exc.status_code != 404:
                    raise
                errors.append(f"{name}: {exc}")
                self._mark_provider_failure(name, exc)
''',
    '''            except ProviderRequestError as exc:
                if explicit or exc.status_code != 404:
                    raise
                errors.append(f"{name}/{model}: {exc}")
''',
    "complete model-not-found",
)

# The stream path has the same initial errors/order lines. Replace the remaining occurrence.
text = replace_once(
    text,
    '''        errors: list[str] = []
        order, profile = await self._order_for_request(req)
''',
    '''        errors: list[str] = []
        failed_providers: set[str] = set()
        order, profile = await self._order_for_request(req)
''',
    "stream failed-provider set",
)

text = replace_once(
    text,
    '''        for idx, (name, model) in enumerate(order):
            provider = self.providers[name]
            t0 = time.perf_counter()
            tool_response = False
            try:
                async for line in provider.stream(self._payload(req, model)):
                    if self._sse_has_tool_call(line):
                        tool_response = True
                    yield self._annotate_sse(line, name)
''',
    '''        for idx, (name, model) in enumerate(order):
            if name in failed_providers:
                continue
            provider = self.providers[name]
            t0 = time.perf_counter()
            tool_response = False
            emitted = False
            try:
                async for line in provider.stream(self._payload(req, model)):
                    if self._sse_has_tool_call(line):
                        tool_response = True
                    emitted = True
                    yield self._annotate_sse(line, name)
''',
    "stream emitted guard",
)

text = replace_once(
    text,
    '''            except QuotaExceededError as exc:
                errors.append(f"{name}: {exc}")
                self._mark_quota_exhausted(name)
''',
    '''            except QuotaExceededError as exc:
                if emitted:
                    raise
                errors.append(f"{name}/{model}: {exc}")
                failed_providers.add(name)
                self._mark_quota_exhausted(name)
''',
    "stream quota failure",
)

text = replace_once(
    text,
    '''            except ProviderUnavailable as exc:
                errors.append(f"{name}: {exc}")
                self._mark_retryable_failure(name, exc)
''',
    '''            except ProviderUnavailable as exc:
                if emitted:
                    raise
                errors.append(f"{name}/{model}: {exc}")
                if exc.status_code != 413:
                    self._mark_retryable_failure(name, exc)
                    failed_providers.add(name)
''',
    "stream provider unavailable",
)

text = replace_once(
    text,
    '''            except ProviderRequestError as exc:
                if explicit or exc.status_code != 404:
                    raise
                errors.append(f"{name}: {exc}")
                self._mark_provider_failure(name, exc)
''',
    '''            except ProviderRequestError as exc:
                if emitted or explicit or exc.status_code != 404:
                    raise
                errors.append(f"{name}/{model}: {exc}")
''',
    "stream model-not-found",
)

old_retry = '''    def _mark_retryable_failure(self, name: str, exc: ProviderUnavailable) -> None:
        status = self.status.get(name)
        if status is None:
            return
        self._mark_provider_failure(name, exc)
        if exc.status_code == 429:
            status.rate_limit_remaining = 0
            status.rate_limit_reset = exc.retry_after_until
'''
new_retry = '''    def _mark_provider_failure(self, name: str, exc: BaseException) -> None:
        status = self.status.get(name)
        if status is None:
            return
        now = time.time()
        status.last_error = str(exc)[:200]
        status.last_polled = now
        status.consecutive_failures += 1

        if isinstance(exc, ProviderUnavailable) and exc.status_code == 429:
            retry_at = exc.retry_after_until
            fallback = now + ZERO_COST_RATE_LIMIT_BACKOFF_SECONDS
            status.available = False
            status.backoff_until = max(
                status.backoff_until,
                float(retry_at) if retry_at is not None and retry_at > now else fallback,
            )
            return

        if status.consecutive_failures < ZERO_COST_PROVIDER_FAILURE_THRESHOLD:
            return
        exponent = min(status.consecutive_failures - ZERO_COST_PROVIDER_FAILURE_THRESHOLD, 6)
        delay = min(
            ZERO_COST_PROVIDER_BACKOFF_MAX_SECONDS,
            ZERO_COST_PROVIDER_BACKOFF_BASE_SECONDS * (2 ** exponent),
        )
        status.available = False
        status.backoff_until = max(status.backoff_until, now + delay)

    def _mark_retryable_failure(self, name: str, exc: ProviderUnavailable) -> None:
        status = self.status.get(name)
        if status is None:
            return
        # HTTP 413 is model/context specific. Trying another model on the same
        # provider should not make the provider look unhealthy.
        if exc.status_code == 413:
            return
        self._mark_provider_failure(name, exc)
        if exc.status_code == 429:
            status.rate_limit_remaining = 0
            status.rate_limit_reset = exc.retry_after_until
'''
text = replace_once(text, old_retry, new_retry, "soft provider backoff")

TARGET.write_text(text, encoding="utf-8")
CI.write_text(ORIGINAL_CI, encoding="utf-8")
Path(__file__).unlink()
