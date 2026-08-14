from pathlib import Path

BASE = Path("src/llm_router/providers/base.py")
ROUTER = Path("src/llm_router/router.py")
ZERO = Path("src/llm_router/zero_cost_router.py")
DASH = Path("src/llm_router/static/dashboard.js")
TEST = Path("tests/test_error_classification_learning.py")
DASH_TEST = Path("tests/test_dashboard_ui.py")
CHANGELOG = Path("CHANGELOG.md")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


base = BASE.read_text()
base = replace_once(
    base,
    '''RETRYABLE_STATUSES = {402, 413, 429, 500, 502, 503, 504}\n''',
    '''RETRYABLE_STATUSES = {402, 413, 429, 500, 502, 503, 504}\n\nERROR_REQUEST_INCOMPATIBLE = "request_incompatible"\nERROR_AUTHENTICATION = "authentication"\nERROR_BILLING_OR_QUOTA = "billing_or_quota"\nERROR_MODEL_UNAVAILABLE = "model_unavailable"\nERROR_CONTEXT_LIMIT = "context_limit"\nERROR_RATE_LIMITED = "rate_limited"\nERROR_PROVIDER_UNAVAILABLE = "provider_unavailable"\nERROR_REQUEST_ERROR = "request_error"\n\n\ndef classify_provider_error(status_code: int | None) -> str:\n    if status_code is None:\n        return ERROR_PROVIDER_UNAVAILABLE\n    if status_code in {400, 422}:\n        return ERROR_REQUEST_INCOMPATIBLE\n    if status_code in {401, 403}:\n        return ERROR_AUTHENTICATION\n    if status_code == 402:\n        return ERROR_BILLING_OR_QUOTA\n    if status_code == 404:\n        return ERROR_MODEL_UNAVAILABLE\n    if status_code == 413:\n        return ERROR_CONTEXT_LIMIT\n    if status_code == 429:\n        return ERROR_RATE_LIMITED\n    if 500 <= status_code <= 599:\n        return ERROR_PROVIDER_UNAVAILABLE\n    return ERROR_REQUEST_ERROR\n\n\ndef upstream_error_detail(body: str, max_chars: int = 400) -> str:\n    if not body:\n        return ""\n    detail = ""\n    try:\n        data = json.loads(body)\n    except (json.JSONDecodeError, TypeError):\n        data = None\n    if isinstance(data, dict):\n        error = data.get("error")\n        if isinstance(error, dict):\n            detail = str(error.get("message") or error.get("detail") or "")\n        elif isinstance(error, str):\n            detail = error\n        if not detail:\n            detail = str(data.get("message") or data.get("detail") or "")\n    if not detail:\n        detail = " ".join(body.split())\n    return detail[:max_chars]\n''',
    "error classification constants",
)
base = replace_once(
    base,
    '''class ProviderUnavailable(Exception):\n    def __init__(\n        self,\n        message: str,\n        status_code: int | None = None,\n        retry_after_until: int | None = None,\n    ):\n        super().__init__(message)\n        self.status_code = status_code\n        self.retry_after_until = retry_after_until\n\n\nclass ProviderRequestError(Exception):\n    def __init__(self, message: str, status_code: int | None = None, body: str = ""):\n        super().__init__(message)\n        self.status_code = status_code\n        self.body = body\n''',
    '''class ProviderUnavailable(Exception):\n    def __init__(\n        self,\n        message: str,\n        status_code: int | None = None,\n        retry_after_until: int | None = None,\n        *,\n        body: str = "",\n        error_class: str = "",\n    ):\n        super().__init__(message)\n        self.status_code = status_code\n        self.retry_after_until = retry_after_until\n        self.body = body\n        self.error_class = error_class or classify_provider_error(status_code)\n\n\nclass ProviderRequestError(Exception):\n    def __init__(\n        self,\n        message: str,\n        status_code: int | None = None,\n        body: str = "",\n        *,\n        error_class: str = "",\n    ):\n        super().__init__(message)\n        self.status_code = status_code\n        self.body = body\n        self.error_class = error_class or classify_provider_error(status_code)\n''',
    "classified exceptions",
)
base = replace_once(
    base,
    '''    async def _check_status(self, resp: httpx.Response) -> None:\n        if resp.status_code in RETRYABLE_STATUSES:\n            raise ProviderUnavailable(\n                f"{self.name} returned HTTP {resp.status_code}",\n                status_code=resp.status_code,\n                retry_after_until=retry_after_timestamp(resp.headers),\n            )\n        if resp.status_code >= 400:\n            try:\n                await resp.aread()\n                body = resp.text[:2000]\n            except (httpx.HTTPError, httpx.StreamError, UnicodeError):\n                body = ""\n            raise ProviderRequestError(\n                f"{self.name} returned HTTP {resp.status_code}",\n                status_code=resp.status_code,\n                body=body,\n            )\n''',
    '''    async def _check_status(self, resp: httpx.Response) -> None:\n        if resp.status_code < 400:\n            return\n        try:\n            await resp.aread()\n            body = resp.text[:2000]\n        except (httpx.HTTPError, httpx.StreamError, UnicodeError):\n            body = ""\n        error_class = classify_provider_error(resp.status_code)\n        detail = upstream_error_detail(body)\n        message = f"{self.name} returned HTTP {resp.status_code}"\n        if detail:\n            message += f": {detail}"\n        if resp.status_code in RETRYABLE_STATUSES:\n            raise ProviderUnavailable(\n                message,\n                status_code=resp.status_code,\n                retry_after_until=retry_after_timestamp(resp.headers),\n                body=body,\n                error_class=error_class,\n            )\n        raise ProviderRequestError(\n            message,\n            status_code=resp.status_code,\n            body=body,\n            error_class=error_class,\n        )\n''',
    "upstream error body extraction",
)
BASE.write_text(base)

router = ROUTER.read_text()
router = replace_once(
    router,
    '''    QuotaExceededError,\n    classify_request_kind,\n    classify_response_kind,\n)\n''',
    '''    QuotaExceededError,\n    classify_provider_error,\n    classify_request_kind,\n    classify_response_kind,\n)\n''',
    "router classifier import",
)
router = replace_once(
    router,
    '''    consecutive_failures: int = 0\n    backoff_until: float = 0.0\n''',
    '''    consecutive_failures: int = 0\n    backoff_until: float = 0.0\n    last_error_class: str = ""\n''',
    "provider status error class",
)
router = replace_once(
    router,
    '''        status.last_error = ""\n        status.last_polled = time.time()\n''',
    '''        status.last_error = ""\n        status.last_error_class = ""\n        status.last_polled = time.time()\n''',
    "clear provider error class",
)
router = replace_once(
    router,
    '''        status.last_error = str(exc)[:200]\n        status.last_polled = now\n        status.consecutive_failures += 1\n''',
    '''        status.last_error = str(exc)[:200]\n        status.last_error_class = getattr(exc, "error_class", "") or classify_provider_error(\n            getattr(exc, "status_code", None)\n        )\n        status.last_polled = now\n        status.consecutive_failures += 1\n''',
    "provider failure error class",
)
router = replace_once(
    router,
    '''        try:\n            if isinstance(error, ProviderRequestError) and error.body:\n                error_json = error.body[:max_chars]\n        except AttributeError:\n            pass\n''',
    '''        try:\n            body = getattr(error, "body", "") if error is not None else ""\n            if body:\n                error_json = str(body)[:max_chars]\n        except AttributeError:\n            pass\n''',
    "message log provider unavailable body",
)
router = replace_once(
    router,
    '''        recent_events = await self._metrics_store.get_recent_router_events(events)\n        analytics = await self.get_analytics_data(days=max(1, days))\n''',
    '''        recent_events = await self._metrics_store.get_recent_router_events(events)\n        for event in recent_events:\n            if not event.get("success"):\n                event["error_class"] = classify_provider_error(\n                    self._as_int(event.get("status_code"), default=0) or None\n                )\n        analytics = await self.get_analytics_data(days=max(1, days))\n''',
    "dashboard event classifications",
)
router = replace_once(
    router,
    '''                "last_error": status.last_error,\n                "last_polled": status.last_polled,\n''',
    '''                "last_error": status.last_error,\n                "last_error_class": status.last_error_class,\n                "last_polled": status.last_polled,\n''',
    "dashboard provider classification",
)
ROUTER.write_text(router)

zero = ZERO.read_text()
zero = replace_once(
    zero,
    '''    ProviderRequestError,\n    ProviderUnavailable,\n    QuotaExceededError,\n    classify_request_kind,\n    classify_response_kind,\n)\n''',
    '''    ERROR_AUTHENTICATION,\n    ERROR_BILLING_OR_QUOTA,\n    ERROR_CONTEXT_LIMIT,\n    ERROR_MODEL_UNAVAILABLE,\n    ERROR_PROVIDER_UNAVAILABLE,\n    ERROR_RATE_LIMITED,\n    ERROR_REQUEST_INCOMPATIBLE,\n    ProviderRequestError,\n    ProviderUnavailable,\n    QuotaExceededError,\n    classify_provider_error,\n    classify_request_kind,\n    classify_response_kind,\n)\n''',
    "zero-cost error imports",
)
zero = replace_once(
    zero,
    '''        add(preferred)\n        add(self.settings.provider(provider).default_model)\n''',
    '''        add(preferred)\n        if provider == "openrouter":\n            add("openrouter/free")\n        add(self.settings.provider(provider).default_model)\n''',
    "openrouter free fallback preference",
)
zero = replace_once(
    zero,
    '''        catalog, task_rows = await self._metrics_store.run_blocking(\n            lambda db: (db.get_all_provider_models(), db.get_model_task_metrics(days=30))\n        )\n        expanded: list[tuple[str, str]] = []\n        for provider, preferred in order:\n            models = self._provider_model_candidates(provider, preferred, catalog)\n            ranked = self._rank_model_alternatives(provider, models, profile, task_rows)\n            expanded.extend(\n                (provider, model)\n                for model in ranked[:MAX_MODEL_ATTEMPTS_PER_PROVIDER]\n            )\n        return expanded\n''',
    '''        catalog, task_rows, recent_events = await self._metrics_store.run_blocking(\n            lambda db: (\n                db.get_all_provider_models(),\n                db.get_model_task_metrics(days=30),\n                db.get_recent_router_events(limit=500),\n            )\n        )\n        incompatibility_counts: dict[tuple[str, str], int] = {}\n        for event in recent_events:\n            if self._as_int(event.get("success")) != 0:\n                continue\n            if str(event.get("task_kind") or "general") != profile.kind:\n                continue\n            if self._as_int(event.get("status_code")) not in {400, 404, 413, 422}:\n                continue\n            key = (str(event.get("provider_name") or ""), str(event.get("model") or ""))\n            incompatibility_counts[key] = incompatibility_counts.get(key, 0) + 1\n\n        expanded: list[tuple[str, str]] = []\n        for provider, preferred in order:\n            models = self._provider_model_candidates(provider, preferred, catalog)\n            ranked = self._rank_model_alternatives(provider, models, profile, task_rows)\n            filtered = [\n                model for index, model in enumerate(ranked)\n                if index == 0 or incompatibility_counts.get((provider, model), 0) < 2\n            ]\n            expanded.extend(\n                (provider, model)\n                for model in filtered[:MAX_MODEL_ATTEMPTS_PER_PROVIDER]\n            )\n        return expanded\n''',
    "persistent model incompatibility memory",
)
# Add configuration-failure helper before quota helper.
zero = replace_once(
    zero,
    '''    def _mark_quota_exhausted(self, name: str) -> None:\n''',
    '''    def _mark_configuration_failure(self, name: str, exc: BaseException) -> None:\n        status = self.status.get(name)\n        if status is None:\n            return\n        status.available = False\n        status.last_error = str(exc)[:200]\n        status.last_error_class = getattr(exc, "error_class", "") or classify_provider_error(\n            getattr(exc, "status_code", None)\n        )\n        status.last_polled = time.time()\n        status.backoff_until = 0.0\n\n    def _mark_quota_exhausted(self, name: str) -> None:\n''',
    "configuration failure helper",
)
zero = replace_once(
    zero,
    '''        status.last_error = "local quota guard exhausted"\n        status.last_polled = time.time()\n''',
    '''        status.last_error = "local quota guard exhausted"\n        status.last_error_class = ERROR_RATE_LIMITED\n        status.last_polled = time.time()\n''',
    "quota classification",
)
# Complete ProviderUnavailable handling.
old = '''            except ProviderUnavailable as exc:\n                errors.append(f"{name}/{model}: {exc}")\n                latency_ms = (time.perf_counter() - t0) * 1000\n                if exc.status_code != 413:\n                    self._mark_retryable_failure(name, exc)\n                    failed_providers.add(name)\n                else:\n                    await self._record_router_event(\n                        provider=name, model=model, stream=False, explicit=explicit,\n                        failover_index=idx, success=False, task_kind=profile.kind,\n                        request_kind=request_kind, latency_ms=latency_ms,\n                        status_code=exc.status_code, request_id=request_id,\n                    )\n'''
new = '''            except ProviderUnavailable as exc:\n                errors.append(f"{name}/{model}: {exc}")\n                latency_ms = (time.perf_counter() - t0) * 1000\n                error_class = getattr(exc, "error_class", "") or classify_provider_error(exc.status_code)\n                if error_class in {ERROR_BILLING_OR_QUOTA, ERROR_CONTEXT_LIMIT}:\n                    await self._record_router_event(\n                        provider=name, model=model, stream=False, explicit=explicit,\n                        failover_index=idx, success=False, task_kind=profile.kind,\n                        request_kind=request_kind, latency_ms=latency_ms,\n                        status_code=exc.status_code, request_id=request_id,\n                    )\n                else:\n                    self._mark_retryable_failure(name, exc)\n                    failed_providers.add(name)\n'''
zero = replace_once(zero, old, new, "complete classified unavailable")
# Details classification in complete provider-unavailable log.
zero = replace_once(
    zero,
    '''                    details={"status_code": exc.status_code},\n                )\n                continue\n            except ProviderRequestError as exc:\n                if explicit or exc.status_code != 404:\n                    raise\n''',
    '''                    details={"status_code": exc.status_code, "error_class": error_class},\n                )\n                continue\n            except ProviderRequestError as exc:\n                if explicit:\n                    raise\n                error_class = getattr(exc, "error_class", "") or classify_provider_error(exc.status_code)\n                if error_class == ERROR_AUTHENTICATION:\n                    self._mark_configuration_failure(name, exc)\n                    failed_providers.add(name)\n                elif error_class not in {ERROR_REQUEST_INCOMPATIBLE, ERROR_MODEL_UNAVAILABLE}:\n                    raise\n''',
    "complete classified request errors",
)
zero = replace_once(
    zero,
    '''                    details={"status_code": exc.status_code},\n                )\n                continue\n            latency_ms = (time.perf_counter() - t0) * 1000\n''',
    '''                    details={"status_code": exc.status_code, "error_class": error_class},\n                )\n                continue\n            latency_ms = (time.perf_counter() - t0) * 1000\n''',
    "complete request log classification",
)
# Stream ProviderUnavailable non-emitted section.
old = '''                errors.append(f"{name}/{model}: {exc}")\n                if exc.status_code != 413:\n                    self._mark_retryable_failure(name, exc)\n                    failed_providers.add(name)\n                else:\n                    await self._record_router_event(\n                        provider=name, model=model, stream=True, explicit=explicit,\n                        failover_index=idx, success=False, task_kind=profile.kind,\n                        request_kind=request_kind, latency_ms=latency_ms,\n                        status_code=exc.status_code, request_id=request_id,\n                    )\n'''
new = '''                errors.append(f"{name}/{model}: {exc}")\n                error_class = getattr(exc, "error_class", "") or classify_provider_error(exc.status_code)\n                if error_class in {ERROR_BILLING_OR_QUOTA, ERROR_CONTEXT_LIMIT}:\n                    await self._record_router_event(\n                        provider=name, model=model, stream=True, explicit=explicit,\n                        failover_index=idx, success=False, task_kind=profile.kind,\n                        request_kind=request_kind, latency_ms=latency_ms,\n                        status_code=exc.status_code, request_id=request_id,\n                    )\n                else:\n                    self._mark_retryable_failure(name, exc)\n                    failed_providers.add(name)\n'''
zero = replace_once(zero, old, new, "stream classified unavailable")
zero = replace_once(
    zero,
    '''                    details={"status_code": exc.status_code},\n                )\n                continue\n            except ProviderRequestError as exc:\n                if emitted or explicit or exc.status_code != 404:\n                    raise\n''',
    '''                    details={"status_code": exc.status_code, "error_class": error_class},\n                )\n                continue\n            except ProviderRequestError as exc:\n                if emitted or explicit:\n                    raise\n                error_class = getattr(exc, "error_class", "") or classify_provider_error(exc.status_code)\n                if error_class == ERROR_AUTHENTICATION:\n                    self._mark_configuration_failure(name, exc)\n                    failed_providers.add(name)\n                elif error_class not in {ERROR_REQUEST_INCOMPATIBLE, ERROR_MODEL_UNAVAILABLE}:\n                    raise\n''',
    "stream classified request errors",
)
zero = replace_once(
    zero,
    '''                    details={"status_code": exc.status_code},\n                )\n                continue\n\n        await self._record_router_event(\n''',
    '''                    details={"status_code": exc.status_code, "error_class": error_class},\n                )\n                continue\n\n        await self._record_router_event(\n''',
    "stream request log classification",
)
# Midstream classification details and only true provider/rate errors health-penalize.
zero = replace_once(
    zero,
    '''                if emitted:\n                    self._mark_retryable_failure(name, exc)\n                    await self._record_router_event(\n''',
    '''                if emitted:\n                    error_class = getattr(exc, "error_class", "") or classify_provider_error(exc.status_code)\n                    if error_class in {ERROR_PROVIDER_UNAVAILABLE, ERROR_RATE_LIMITED}:\n                        self._mark_retryable_failure(name, exc)\n                    await self._record_router_event(\n''',
    "midstream classified health",
)
ZERO.write_text(zero)

# Dashboard exact classifications.
dash = DASH.read_text()
dash = replace_once(
    dash,
    '''    mini("Failures", fmt(provider.consecutive_failures || 0));\n''',
    '''    mini("Failures", fmt(provider.consecutive_failures || 0));\n    mini("Error class", provider.last_error_class || "none");\n''',
    "provider card error class",
)
dash = replace_once(
    dash,
    '''    result.appendChild(el("span", `tag ${event.success ? "ok" : "err"}`, event.success ? (event.response_kind || "ok") : "failed"));\n''',
    '''    result.appendChild(el("span", `tag ${event.success ? "ok" : "err"}`, event.success ? (event.response_kind || "ok") : (event.error_class || "failed")));\n''',
    "activity error class",
)
DASH.write_text(dash)

# Dashboard contract test.
dash_test = DASH_TEST.read_text()
dash_test = replace_once(
    dash_test,
    '''    assert "provider.consecutive_failures" in script\n''',
    '''    assert "provider.consecutive_failures" in script\n    assert "provider.last_error_class" in script\n    assert "event.error_class" in script\n''',
    "dashboard classification assertions",
)
DASH_TEST.write_text(dash_test)

# Focused behavior tests.
TEST.write_text('''from __future__ import annotations\n\nimport time\n\nimport httpx\nimport pytest\n\nfrom llm_router.config import MetricsConfig, ProviderConfig, Settings\nfrom llm_router.providers.base import (\n    ERROR_AUTHENTICATION,\n    ERROR_BILLING_OR_QUOTA,\n    ERROR_CONTEXT_LIMIT,\n    ERROR_MODEL_UNAVAILABLE,\n    ERROR_PROVIDER_UNAVAILABLE,\n    ERROR_RATE_LIMITED,\n    ERROR_REQUEST_INCOMPATIBLE,\n    Provider,\n    ProviderRequestError,\n    ProviderUnavailable,\n    classify_provider_error,\n)\nfrom llm_router.schemas import ChatRequest, Message\nfrom llm_router.task_classifier import TaskProfile\nfrom llm_router.zero_cost_router import ZeroCostModelRouter\n\n\ndef settings(tmp_path, providers=None) -> Settings:\n    provider_map = providers or {\n        "openrouter": ProviderConfig("OpenRouter", "https://openrouter.example", "paid-model"),\n        "groq": ProviderConfig("Groq", "https://groq.example", "groq-default"),\n    }\n    return Settings(\n        strategy="zero-cost",\n        providers=provider_map,\n        routing_providers=list(provider_map),\n        metrics=MetricsConfig(db_path=str(tmp_path / "metrics.db"), report_interval_seconds=0),\n    )\n\n\ndef request(stream=False) -> ChatRequest:\n    return ChatRequest(model="auto", messages=[Message(role="user", content="write code")], stream=stream)\n\n\ndef ok(model):\n    return {\n        "id": "x", "created": 1, "model": model,\n        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],\n        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},\n    }\n\n\n@pytest.mark.parametrize(("status", "expected"), [\n    (400, ERROR_REQUEST_INCOMPATIBLE),\n    (422, ERROR_REQUEST_INCOMPATIBLE),\n    (401, ERROR_AUTHENTICATION),\n    (403, ERROR_AUTHENTICATION),\n    (402, ERROR_BILLING_OR_QUOTA),\n    (404, ERROR_MODEL_UNAVAILABLE),\n    (413, ERROR_CONTEXT_LIMIT),\n    (429, ERROR_RATE_LIMITED),\n    (503, ERROR_PROVIDER_UNAVAILABLE),\n    (None, ERROR_PROVIDER_UNAVAILABLE),\n])\ndef test_provider_error_classification(status, expected):\n    assert classify_provider_error(status) == expected\n\n\n@pytest.mark.asyncio\nasync def test_check_status_preserves_upstream_detail_for_400_and_402(tmp_path):\n    async with httpx.AsyncClient() as client:\n        provider = Provider("demo", ProviderConfig("Demo", "https://demo.example", "m"), client)\n        for status, error_type, error_class in [\n            (400, ProviderRequestError, ERROR_REQUEST_INCOMPATIBLE),\n            (402, ProviderUnavailable, ERROR_BILLING_OR_QUOTA),\n        ]:\n            response = httpx.Response(status, json={"error": {"message": "tool choice unsupported"}})\n            with pytest.raises(error_type) as caught:\n                await provider._check_status(response)\n            assert "tool choice unsupported" in str(caught.value)\n            assert caught.value.error_class == error_class\n            assert "tool choice unsupported" in caught.value.body\n\n\nclass ScriptedProvider:\n    def __init__(self, failures=None):\n        self.failures = failures or {}\n        self.calls = []\n\n    async def complete(self, payload):\n        model = str(payload["model"])\n        self.calls.append(model)\n        failure = self.failures.get(model)\n        if failure:\n            raise failure\n        return ok(model)\n\n\n@pytest.mark.asyncio\nasync def test_400_tries_another_same_provider_model_without_health_penalty(tmp_path):\n    async with httpx.AsyncClient() as client:\n        router = ZeroCostModelRouter(settings(tmp_path), client)\n        provider = ScriptedProvider({\n            "paid-model": ProviderRequestError("bad tools", status_code=400),\n        })\n        router.providers["openrouter"] = provider\n        router._model_cache["openrouter"] = (time.time(), [{"id": "paid-model"}, {"id": "chat-alt"}])\n        router.providers["groq"] = ScriptedProvider()\n\n        response = await router.complete(request())\n\n        assert response.provider == "openrouter"\n        assert provider.calls[:2] == ["paid-model", "openrouter/free"]\n        assert router.status["openrouter"].consecutive_failures == 0\n        await router._metrics_store.close()\n\n\n@pytest.mark.asyncio\nasync def test_openrouter_402_falls_back_to_free_route_without_provider_penalty(tmp_path):\n    async with httpx.AsyncClient() as client:\n        router = ZeroCostModelRouter(settings(tmp_path), client)\n        provider = ScriptedProvider({\n            "paid-model": ProviderUnavailable("payment required", status_code=402),\n        })\n        router.providers["openrouter"] = provider\n        router.providers["groq"] = ScriptedProvider()\n\n        response = await router.complete(request())\n\n        assert response.provider == "openrouter"\n        assert response.model == "openrouter/free"\n        assert provider.calls == ["paid-model", "openrouter/free"]\n        assert router.status["openrouter"].consecutive_failures == 0\n        await router._metrics_store.close()\n\n\n@pytest.mark.asyncio\nasync def test_auth_failure_disables_provider_without_transient_failure_counter(tmp_path):\n    async with httpx.AsyncClient() as client:\n        router = ZeroCostModelRouter(settings(tmp_path), client)\n        first = ScriptedProvider({\n            "paid-model": ProviderRequestError("invalid token", status_code=401),\n        })\n        second = ScriptedProvider()\n        router.providers["openrouter"] = first\n        router.providers["groq"] = second\n\n        response = await router.complete(request())\n\n        status = router.status["openrouter"]\n        assert response.provider == "groq"\n        assert status.available is False\n        assert status.consecutive_failures == 0\n        assert status.last_error_class == ERROR_AUTHENTICATION\n        await router._metrics_store.close()\n\n\n@pytest.mark.asyncio\nasync def test_repeated_model_task_incompatibility_skips_nonpreferred_alternative(tmp_path):\n    async with httpx.AsyncClient() as client:\n        router = ZeroCostModelRouter(settings(tmp_path), client)\n        router._model_cache["groq"] = (time.time(), [\n            {"id": "groq-default"}, {"id": "known-bad"}, {"id": "known-good"},\n        ])\n        await router._metrics_store.run_blocking(lambda db: [\n            db.record_router_event(\n                provider="groq", model="known-bad", stream=False, explicit=False,\n                failover_index=1, success=False, task_kind="coding", request_kind="chat",\n                status_code=400, occurred_at=time.time() - offset,\n            )\n            for offset in (10, 20)\n        ])\n\n        expanded = await router._expand_model_fallbacks(\n            [("groq", "groq-default")], TaskProfile(kind="coding", confidence=1.0, coding_heavy=True)\n        )\n\n        assert ("groq", "known-bad") not in expanded\n        assert ("groq", "known-good") in expanded\n        await router._metrics_store.close()\n''')

changelog = CHANGELOG.read_text()
changelog = replace_once(
    changelog,
    '''### Fixed\n\n''',
    '''### Fixed\n\n- Classified upstream failures by request compatibility, authentication, billing/quota, model availability, context limits, rate limits, and provider outages; automatic routing now learns model/task incompatibilities, prefers `openrouter/free` after OpenRouter access failures, preserves upstream error details, and exposes failure classes in the live dashboard.\n''',
    "changelog entry",
)
CHANGELOG.write_text(changelog)
