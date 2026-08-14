# src/llm_router/zero_cost_router.py
from __future__ import annotations

import time
import uuid
from typing import AsyncIterator

import httpx

from .config import Settings, normalize_provider
from .provider_matrix import get_provider_matrix_entry, load_provider_matrix
from .providers.base import (
    ProviderRequestError,
    ProviderUnavailable,
    QuotaExceededError,
    classify_request_kind,
    classify_response_kind,
)
from .router import ModelRouter, ProviderStatus
from .routing_score import RouteScore, ZeroCostPolicy, route_sort_key, runtime_route_score
from .task_classifier import TaskProfile, classify_task, refine_task_profile_with_model
from .schemas import ChatRequest, ChatResponse


MAX_MODEL_ATTEMPTS_PER_PROVIDER = 8
ZERO_COST_PROVIDER_FAILURE_THRESHOLD = 5
ZERO_COST_PROVIDER_BACKOFF_BASE_SECONDS = 120.0
ZERO_COST_PROVIDER_BACKOFF_MAX_SECONDS = 1800.0
ZERO_COST_RATE_LIMIT_BACKOFF_SECONDS = 60.0


class ZeroCostModelRouter(ModelRouter):
    """ModelRouter variant that drains renewable free routes before paid/finite routes."""

    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        policy: ZeroCostPolicy | None = None,
    ) -> None:
        load_provider_matrix()
        super().__init__(settings, http)
        self.zero_cost_policy = policy or ZeroCostPolicy.from_env()

    def score_provider(self, name: str, *, primary: bool = False) -> RouteScore:
        return runtime_route_score(
            name,
            get_provider_matrix_entry(name),
            self.status.get(name),
            self.zero_cost_policy,
            primary=primary,
        )

    def route_scores(self, *, primary: str | None = None) -> list[RouteScore]:
        scores = [self.score_provider(name, primary=(name == primary)) for name in self.providers]
        return sorted(scores, key=route_sort_key)

    def ranked_providers(self) -> list[ProviderStatus]:
        if self.settings.strategy != "zero-cost":
            return super().ranked_providers()
        scores = self.route_scores()
        order = {score.provider: idx for idx, score in enumerate(scores) if score.eligible}
        return sorted(
            self.status.values(),
            key=lambda status: order.get(status.name, len(order) + 1_000),
        )

    def _order(self, req: ChatRequest) -> list[tuple[str, str]]:
        if self.settings.strategy != "zero-cost":
            return super()._order(req)

        explicit = self._explicit_provider(req)
        if explicit:
            return super()._order(req)

        primary, primary_model = self.settings.resolve(req.model or "qwen3-8b")

        # "auto" (or an empty model string) picks the best-ranked eligible
        # provider in the routing pool using its default model, with failover.
        if not req.model or req.model.lower() in {"auto", "best", "*"}:
            profile = classify_task(req.model_dump()) if self.settings.classifier.enabled else TaskProfile(kind="general", confidence=1.0)
            pool = self._routing_pool()
            scores = [
                score for score in self.route_scores()
                if score.eligible
                and score.provider in pool
                and self.settings.provider(score.provider).default_model
            ]
            scores = self._apply_task_profile(scores, profile)
            names = [score.provider for score in scores]
            if req.local_first is True and "local" in names:
                names.remove("local")
                names.insert(0, "local")
            elif req.local_first is False and "local" in names:
                names = [name for name in names if name != "local"] + ["local"]
            return [
                (name, self.settings.provider(name).default_model)
                for name in names
            ]

        profile = classify_task(req.model_dump()) if self.settings.classifier.enabled else TaskProfile(kind="general", confidence=1.0)
        scores = self._apply_task_profile([score for score in self.route_scores(primary=primary) if score.eligible], profile)
        names = [score.provider for score in scores]

        if req.local_first is True and "local" in names:
            names.remove("local")
            names.insert(0, "local")
        elif req.local_first is False and "local" in names:
            names = [name for name in names if name != "local"] + ["local"]

        return [
            (
                name,
                primary_model if name == primary else self.settings.provider(name).default_model,
            )
            for name in names
        ]

    async def _task_profile(self, req: ChatRequest) -> TaskProfile:
        base = classify_task(req.model_dump()) if self.settings.classifier.enabled else TaskProfile(kind="general", confidence=1.0)
        return await refine_task_profile_with_model(self.settings, req.model_dump(), base)

    def _task_specific_fallback_order(self, profile: TaskProfile) -> list[str]:
        order: list[str] = []
        if profile.needs_vision:
            order.extend(["openrouter", "huggingface", "groq"])
        elif profile.needs_json or profile.needs_tools:
            order.extend(["openrouter", "groq", "huggingface"])
        elif profile.coding_heavy:
            order.extend(["groq", "huggingface", "openrouter"])
        elif profile.speed_sensitive:
            order.extend(["groq", "openrouter", "huggingface"])
        else:
            order.extend(["groq", "openrouter", "huggingface"])
        if profile.needs_long_context:
            order.append("local")
        return [name for name in order if name in self.providers]

    def _merge_fallback_order(self, names: list[str], profile: TaskProfile) -> list[str]:
        merged: list[str] = []
        for name in self._task_specific_fallback_order(profile) + names:
            if name in names and name not in merged:
                merged.append(name)
        return merged or names

    async def _order_for_request(
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

    def _order_with_profile(self, req: ChatRequest, profile: TaskProfile) -> list[tuple[str, str]]:
        if self.settings.strategy != "zero-cost":
            return super()._order(req)

        explicit = self._explicit_provider(req)
        if explicit:
            return super()._order(req)

        primary, primary_model = self.settings.resolve(req.model or "qwen3-8b")
        if not req.model or req.model.lower() in {"auto", "best", "*"}:
            pool = self._routing_pool()
            scores = [
                score for score in self.route_scores()
                if score.eligible
                and score.provider in pool
                and self.settings.provider(score.provider).default_model
            ]
            scores = self._apply_task_profile(scores, profile)
            names = self._merge_fallback_order([score.provider for score in scores], profile)
            if req.local_first is True and "local" in names:
                names.remove("local")
                names.insert(0, "local")
            elif req.local_first is False and "local" in names:
                names = [name for name in names if name != "local"] + ["local"]
            return [(name, self.settings.provider(name).default_model) for name in names]

        scores = [score for score in self.route_scores(primary=primary) if score.eligible]
        scores = self._apply_task_profile(scores, profile)
        names = self._merge_fallback_order([score.provider for score in scores], profile)
        if req.local_first is True and "local" in names:
            names.remove("local")
            names.insert(0, "local")
        elif req.local_first is False and "local" in names:
            names = [name for name in names if name != "local"] + ["local"]
        return [
            (
                name,
                primary_model if name == primary else self.settings.provider(name).default_model,
            )
            for name in names
        ]

    def _apply_task_profile(self, scores: list[RouteScore], profile: TaskProfile) -> list[RouteScore]:
        if not scores:
            return scores
        weighted: list[tuple[float, RouteScore]] = []
        for score in scores:
            bonus = self._task_bonus(score.provider, profile)
            weighted.append((score.dynamic_score + bonus, score))
        weighted.sort(key=lambda item: (-item[0], item[1].provider))
        return [item[1] for item in weighted]

    def _task_bonus(self, provider: str, profile: TaskProfile) -> float:
        entry = get_provider_matrix_entry(provider)
        bonus = 0.0
        if not entry:
            return bonus
        if profile.coding_heavy and provider in {"huggingface", "openrouter", "groq"}:
            bonus += 4.0
        if profile.needs_tools and entry.tool_calling in {True, "yes", "model-dependent"}:
            bonus += 4.0
        if profile.needs_json and entry.openai_compatible == "yes":
            bonus += 2.5
        if profile.needs_vision and entry.vision in {True, "yes", "model-dependent"}:
            bonus += 3.0
        if profile.needs_long_context and entry.context_window:
            bonus += 2.0
        if profile.speed_sensitive and provider in {"groq", "openrouter"}:
            bonus += 2.0
        return bonus

    async def complete(self, req: ChatRequest) -> ChatResponse:
        if self.settings.strategy != "zero-cost":
            return await super().complete(req)

        errors: list[str] = []
        failed_providers: set[str] = set()
        order, profile = await self._order_for_request(req)
        if not order:
            raise ProviderUnavailable("no zero-cost providers are currently eligible")

        request_kind = classify_request_kind({"tools": req.tools, "tool_choice": req.tool_choice})
        explicit = self._is_explicit(req)
        request_id = uuid.uuid4().hex[:16]
        await self._log_event(
            level="info",
            source="router",
            message=f"zero-cost chat request: model={req.model!r} kind={request_kind} explicit={explicit}",
            model=req.model,
            request_id=request_id,
            details={"provider_count": len(order), "task_kind": profile.kind, "task_confidence": profile.confidence},
        )
        for idx, (name, model) in enumerate(order):
            if name in failed_providers:
                continue
            provider = self.providers[name]
            t0 = time.perf_counter()
            try:
                data = await provider.complete(self._payload(req, model))
            except QuotaExceededError as exc:
                errors.append(f"{name}/{model}: {exc}")
                failed_providers.add(name)
                self._mark_quota_exhausted(name)
                await self._record_message_log(
                    request_id=request_id, req=req, model=model, stream=False, explicit=explicit,
                    success=False, request_kind=request_kind, error=exc, provider=name,
                    status_code=429, latency_ms=(time.perf_counter() - t0) * 1000,
                )
                await self._log_event(
                    level="warning",
                    source="router",
                    message=f"provider {name} quota exhausted: {exc}",
                    provider=name, model=model, request_id=request_id,
                )
                continue
            except ProviderUnavailable as exc:
                errors.append(f"{name}/{model}: {exc}")
                if exc.status_code != 413:
                    self._mark_retryable_failure(name, exc)
                    failed_providers.add(name)
                await self._record_message_log(
                    request_id=request_id, req=req, model=model, stream=False, explicit=explicit,
                    success=False, request_kind=request_kind, error=exc, provider=name,
                    status_code=exc.status_code, latency_ms=(time.perf_counter() - t0) * 1000,
                )
                await self._log_event(
                    level="warning",
                    source="router",
                    message=f"provider {name} unavailable: {exc}",
                    provider=name, model=model, request_id=request_id,
                    details={"status_code": exc.status_code},
                )
                continue
            except ProviderRequestError as exc:
                if explicit or exc.status_code != 404:
                    raise
                errors.append(f"{name}/{model}: {exc}")
                await self._record_message_log(
                    request_id=request_id, req=req, model=model, stream=False, explicit=explicit,
                    success=False, request_kind=request_kind, error=exc, provider=name,
                    status_code=exc.status_code, latency_ms=(time.perf_counter() - t0) * 1000,
                )
                await self._log_event(
                    level="error",
                    source="router",
                    message=f"provider {name} request failed: {exc}",
                    provider=name, model=model, request_id=request_id,
                    details={"status_code": exc.status_code},
                )
                continue
            latency_ms = (time.perf_counter() - t0) * 1000
            self._mark_provider_success(name, latency_ms)
            await self._enrich_status_with_metrics(name)
            raw_usage = data.get("usage")
            usage = raw_usage if isinstance(raw_usage, dict) else {}
            await self._record_router_event(
                provider=name,
                model=model,
                stream=False,
                explicit=explicit,
                failover_index=idx,
                success=True,
                task_kind=profile.kind,
                request_kind=request_kind,
                response_kind=classify_response_kind(data),
                prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                latency_ms=latency_ms,
                status_code=200,
                request_id=request_id,
            )
            await self._log_event(
                level="info",
                source="router",
                message=f"provider {name} served {model} in {latency_ms:.0f}ms",
                provider=name, model=model, request_id=request_id,
            )
            await self._record_message_log(
                request_id=request_id, req=req, model=model, stream=False, explicit=explicit,
                success=True, request_kind=request_kind,
                response_kind=classify_response_kind(data),
                prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                latency_ms=latency_ms, status_code=200, response=data, provider=name,
            )
            return self._to_response(data, model, provider_name=name)

        await self._record_router_event(
            provider=None,
            model=req.model,
            stream=False,
            explicit=explicit,
            failover_index=len(order),
            success=False,
            task_kind=profile.kind,
            request_kind=request_kind,
            status_code=None,
            request_id=request_id,
        )
        await self._log_event(
            level="error",
            source="router",
            message="all zero-cost providers failed: " + "; ".join(errors),
            model=req.model,
            request_id=request_id,
            details={"errors": errors, "task_kind": profile.kind, "task_confidence": profile.confidence},
        )
        raise ProviderUnavailable("all zero-cost providers failed: " + "; ".join(errors))

    async def stream(self, req: ChatRequest) -> AsyncIterator[str]:
        if self.settings.strategy != "zero-cost":
            async for line in super().stream(req):
                yield line
            return

        errors: list[str] = []
        failed_providers: set[str] = set()
        order, profile = await self._order_for_request(req)
        if not order:
            raise ProviderUnavailable("no zero-cost providers are currently eligible")

        request_kind = classify_request_kind({"tools": req.tools, "tool_choice": req.tool_choice})
        explicit = self._is_explicit(req)
        request_id = uuid.uuid4().hex[:16]
        await self._log_event(
            level="info",
            source="router",
            message=f"zero-cost stream request: model={req.model!r} kind={request_kind} explicit={explicit}",
            model=req.model,
            request_id=request_id,
            details={"provider_count": len(order), "task_kind": profile.kind, "task_confidence": profile.confidence},
        )
        for idx, (name, model) in enumerate(order):
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
                latency_ms = (time.perf_counter() - t0) * 1000
                self._mark_provider_success(name, latency_ms)
                await self._enrich_status_with_metrics(name)
                await self._record_router_event(
                    provider=name,
                    model=model,
                    stream=True,
                    explicit=explicit,
                    failover_index=idx,
                    success=True,
                    task_kind=profile.kind,
                    request_kind=request_kind,
                    response_kind="tool_call" if tool_response else "chat",
                    latency_ms=latency_ms,
                    status_code=200,
                    request_id=request_id,
                )
                await self._log_event(
                    level="info",
                    source="router",
                    message=f"provider {name} streamed {model} in {latency_ms:.0f}ms",
                    provider=name, model=model, request_id=request_id,
                )
                await self._record_message_log(
                    request_id=request_id, req=req, model=model, stream=True, explicit=explicit,
                    success=True, request_kind=request_kind,
                    response_kind="tool_call" if tool_response else "chat",
                    latency_ms=latency_ms, status_code=200, provider=name,
                )
                return
            except QuotaExceededError as exc:
                if emitted:
                    raise
                errors.append(f"{name}/{model}: {exc}")
                failed_providers.add(name)
                self._mark_quota_exhausted(name)
                await self._record_message_log(
                    request_id=request_id, req=req, model=model, stream=True, explicit=explicit,
                    success=False, request_kind=request_kind, error=exc, provider=name,
                    status_code=429, latency_ms=(time.perf_counter() - t0) * 1000,
                )
                await self._log_event(
                    level="warning",
                    source="router",
                    message=f"provider {name} quota exhausted: {exc}",
                    provider=name, model=model, request_id=request_id,
                )
                continue
            except ProviderUnavailable as exc:
                if emitted:
                    raise
                errors.append(f"{name}/{model}: {exc}")
                if exc.status_code != 413:
                    self._mark_retryable_failure(name, exc)
                    failed_providers.add(name)
                await self._record_message_log(
                    request_id=request_id, req=req, model=model, stream=True, explicit=explicit,
                    success=False, request_kind=request_kind, error=exc, provider=name,
                    status_code=exc.status_code, latency_ms=(time.perf_counter() - t0) * 1000,
                )
                await self._log_event(
                    level="warning",
                    source="router",
                    message=f"provider {name} stream unavailable: {exc}",
                    provider=name, model=model, request_id=request_id,
                    details={"status_code": exc.status_code},
                )
                continue
            except ProviderRequestError as exc:
                if emitted or explicit or exc.status_code != 404:
                    raise
                errors.append(f"{name}/{model}: {exc}")
                await self._record_message_log(
                    request_id=request_id, req=req, model=model, stream=True, explicit=explicit,
                    success=False, request_kind=request_kind, error=exc, provider=name,
                    status_code=exc.status_code, latency_ms=(time.perf_counter() - t0) * 1000,
                )
                await self._log_event(
                    level="error",
                    source="router",
                    message=f"provider {name} stream failed: {exc}",
                    provider=name, model=model, request_id=request_id,
                    details={"status_code": exc.status_code},
                )
                continue

        await self._record_router_event(
            provider=None,
            model=req.model,
            stream=True,
            explicit=explicit,
            failover_index=len(order),
            success=False,
            task_kind=profile.kind,
            request_kind=request_kind,
            status_code=None,
            request_id=request_id,
        )
        await self._log_event(
            level="error",
            source="router",
            message="all zero-cost providers failed: " + "; ".join(errors),
            model=req.model,
            request_id=request_id,
            details={"errors": errors, "task_kind": profile.kind, "task_confidence": profile.confidence},
        )
        raise ProviderUnavailable("all zero-cost providers failed: " + "; ".join(errors))

    def _mark_quota_exhausted(self, name: str) -> None:
        status = self.status.get(name)
        if status is None:
            return
        status.daily_calls_remaining = 0
        status.last_error = "local quota guard exhausted"
        status.last_polled = time.time()

    def _mark_provider_failure(self, name: str, exc: BaseException) -> None:
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

    def provider_matrix_view(self) -> list[dict]:
        scores = {score.provider: score for score in self.route_scores()}
        output: list[dict] = []
        for name in self.providers:
            entry = get_provider_matrix_entry(name)
            score = scores[name]
            item = entry.as_public_dict() if entry is not None else {"id": None, "name": name}
            item["configured_provider"] = name
            item["routing"] = score.as_dict()
            output.append(item)
        output.sort(key=lambda item: route_sort_key(scores[item["configured_provider"]]))
        return output
