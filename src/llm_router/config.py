# src/llm_router/config.py
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config.toml"


class ConfigError(RuntimeError):
    pass


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    default_model: str
    timeout_seconds: float = 60.0
    stream_timeout_seconds: float = 240.0
    api_key: str | None = None


@dataclass
class ModelRoute:
    provider: str
    model: str


@dataclass
class QuotaConfig:
    daily_request_limit: int | None = None
    daily_token_limit: int | None = None
    quota_reset_hour: int = 0


@dataclass
class MetricsConfig:
    db_path: str | None = None
    flush_interval_seconds: int = 30
    retention_days: int = 30
    report_interval_seconds: int = 300
    model_cache_ttl_seconds: int = 21_600
    model_failure_backoff_seconds: int = 300
    model_failure_backoff_max_seconds: int = 3_600


@dataclass
class LogsConfig:
    """Logging configuration: console/file handlers plus the debugging event log."""

    level: str = "INFO"
    file_path: str | None = None
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 5
    log_message_bodies: bool = True
    max_body_chars: int = 120_000


@dataclass
class ProviderPricing:
    input_cost_per_1m_tokens: float = 0.0
    output_cost_per_1m_tokens: float = 0.0


@dataclass
class AnalyticsConfig:
    currency: str = "USD"
    default_input_cost_per_1m_tokens: float = 0.0
    default_output_cost_per_1m_tokens: float = 0.0
    error_rate_warning_threshold: float = 0.05
    error_rate_critical_threshold: float = 0.10
    failover_rate_warning_threshold: float = 0.15
    pricing: dict[str, ProviderPricing] = field(default_factory=dict)


@dataclass
class ClassifierConfig:
    enabled: bool = True
    fallback_enabled: bool = True
    fallback_confidence_threshold: float = 0.65


@dataclass
class Settings:
    strategy: str = "cloud-first"
    timeout_seconds: float = 60.0
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    models: dict[str, ModelRoute] = field(default_factory=dict)
    quotas: dict[str, QuotaConfig] = field(default_factory=dict)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    logs: LogsConfig = field(default_factory=LogsConfig)
    analytics: AnalyticsConfig = field(default_factory=AnalyticsConfig)
    classifier: ClassifierConfig = field(default_factory=ClassifierConfig)
    host: str = "0.0.0.0"
    port: int = 8000
    # Providers allowed to participate in automatic/fallback routing. Empty/None
    # means all configured providers are eligible. Explicit "provider:model"
    # requests always work regardless of this list.
    routing_providers: list[str] = field(default_factory=list)
    router_api_key: str | None = None

    def provider(self, name: str) -> ProviderConfig:
        try:
            return self.providers[name]
        except KeyError:
            raise ConfigError(f"unknown provider: {name!r}")

    def quota(self, name: str) -> QuotaConfig | None:
        return self.quotas.get(name)

    def resolve(self, model: str) -> tuple[str, str]:
        if ":" in model:
            provider_hint, _, rest = model.partition(":")
            pname = normalize_provider(provider_hint)
            if pname in self.providers:
                return pname, (rest or self.provider(pname).default_model)
        route = self.models.get(model)
        if route is not None:
            return route.provider, route.model
        return "huggingface", model


def _env_bool(env: dict[str, str], name: str, default: bool) -> bool:
    value = env.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def normalize_provider(name: str | None) -> str:
    aliases = {
        "hf": "huggingface",
        "huggingface": "huggingface",
        "local": "local",
        "llama": "local",
        "llamacpp": "local",
        "gemini": "google_ai",
        "google": "google_ai",
    }
    return aliases.get(name.lower(), name.lower()) if name else ""


def load_settings(
    config_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> Settings:
    resolved_env: dict[str, str] = dict(os.environ) if env is None else dict(env)
    path = config_path or DEFAULT_CONFIG_PATH
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")

    dotenv_path = path.parent / ".env"
    if dotenv_path.exists():
        dotenv_env = {
            key: value
            for key, value in dotenv_values(dotenv_path).items()
            if value is not None
        }
        resolved_env = {**dotenv_env, **resolved_env}

        for key, value in dotenv_env.items():
            os.environ.setdefault(key, value)

    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    providers: dict[str, ProviderConfig] = {}
    for name, pc in raw.get("providers", {}).items():
        base = pc.get("base_url", "")
        if resolved_env.get("LOCAL_BASE_URL") and name == "local":
            base = resolved_env["LOCAL_BASE_URL"]
        providers[name] = ProviderConfig(
            name=pc.get("name", name),
            base_url=base.rstrip("/"),
            default_model=pc.get("default_model", ""),
            timeout_seconds=pc.get("timeout_seconds", raw.get("timeout_seconds", 60.0)),
            stream_timeout_seconds=pc.get("stream_timeout_seconds", 240.0),
            api_key=resolved_env.get(pc["api_key_env"]) if pc.get("api_key_env") else None,
        )

    models = {
        alias: ModelRoute(provider=route["provider"], model=route["model"])
        for alias, route in raw.get("models", {}).items()
    }

    quotas = {
        name: QuotaConfig(
            daily_request_limit=qc.get("daily_requests"),
            daily_token_limit=qc.get("daily_tokens"),
            quota_reset_hour=qc.get("reset_hour", 0),
        )
        for name, qc in raw.get("quotas", {}).items()
    }

    metrics_raw = raw.get("metrics", {})
    metrics = MetricsConfig(
        db_path=metrics_raw.get("db_path"),
        flush_interval_seconds=metrics_raw.get("flush_interval_seconds", 30),
        retention_days=metrics_raw.get("retention_days", 30),
        report_interval_seconds=metrics_raw.get("report_interval_seconds", 300),
        model_cache_ttl_seconds=metrics_raw.get("model_cache_ttl_seconds", 21_600),
        model_failure_backoff_seconds=metrics_raw.get("model_failure_backoff_seconds", 300),
        model_failure_backoff_max_seconds=metrics_raw.get(
            "model_failure_backoff_max_seconds", 3_600
        ),
    )

    logs_raw = raw.get("logs", {})
    logs = LogsConfig(
        level=resolved_env.get("LLM_ROUTER_LOG_LEVEL", logs_raw.get("level", "INFO")),
        file_path=logs_raw.get("file_path"),
        max_bytes=logs_raw.get("max_bytes", 10 * 1024 * 1024),
        backup_count=logs_raw.get("backup_count", 5),
        log_message_bodies=_env_bool(
            resolved_env,
            "LLM_ROUTER_LOG_MESSAGE_BODIES",
            logs_raw.get("log_message_bodies", True),
        ),
        max_body_chars=logs_raw.get("max_body_chars", 120_000),
    )

    analytics_raw = raw.get("analytics", {})
    pricing_raw = analytics_raw.get("pricing", {})
    analytics_default_input = float(analytics_raw.get("default_input_cost_per_1m_tokens", 0.0))
    analytics_default_output = float(analytics_raw.get("default_output_cost_per_1m_tokens", 0.0))
    analytics = AnalyticsConfig(
        currency=str(analytics_raw.get("currency", "USD")),
        default_input_cost_per_1m_tokens=analytics_default_input,
        default_output_cost_per_1m_tokens=analytics_default_output,
        error_rate_warning_threshold=float(analytics_raw.get("error_rate_warning_threshold", 0.05)),
        error_rate_critical_threshold=float(analytics_raw.get("error_rate_critical_threshold", 0.10)),
        failover_rate_warning_threshold=float(analytics_raw.get("failover_rate_warning_threshold", 0.15)),
        pricing={
            provider: ProviderPricing(
                input_cost_per_1m_tokens=float(
                    price.get("input_cost_per_1m_tokens", analytics_default_input)
                ),
                output_cost_per_1m_tokens=float(
                    price.get("output_cost_per_1m_tokens", analytics_default_output)
                ),
            )
            for provider, price in pricing_raw.items()
        },
    )

    classifier_raw = raw.get("classifier", {})
    classifier = ClassifierConfig(
        enabled=bool(classifier_raw.get("enabled", True)),
        fallback_enabled=bool(classifier_raw.get("fallback_enabled", True)),
        fallback_confidence_threshold=float(classifier_raw.get("fallback_confidence_threshold", 0.65)),
    )

    routing_raw = raw.get("routing", {})
    routing_providers = []
    for name in routing_raw.get("providers", []):
        normalized = normalize_provider(name)
        if normalized not in providers:
            raise ConfigError(
                f"[routing] providers references unknown provider {name!r}; "
                f"configured providers: {', '.join(providers)}"
            )
        routing_providers.append(normalized)

    return Settings(
        strategy=raw.get("strategy", "cloud-first"),
        timeout_seconds=raw.get("timeout_seconds", 60.0),
        providers=providers,
        models=models,
        quotas=quotas,
        metrics=metrics,
        logs=logs,
        analytics=analytics,
        classifier=classifier,
        host=resolved_env.get("HOST", "0.0.0.0"),
        port=int(resolved_env.get("PORT", "8000")),
        routing_providers=routing_providers,
        router_api_key=resolved_env.get("ROUTER_API_KEY") or None,
    )
