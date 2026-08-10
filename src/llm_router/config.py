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


@dataclass
class Settings:
    strategy: str = "cloud-first"
    timeout_seconds: float = 60.0
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    models: dict[str, ModelRoute] = field(default_factory=dict)
    quotas: dict[str, QuotaConfig] = field(default_factory=dict)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)
    host: str = "0.0.0.0"
    port: int = 8000

    def provider(self, name: str) -> ProviderConfig:
        try:
            return self.providers[name]
        except KeyError:
            raise ConfigError(f"unknown provider: {name!r}")

    def quota(self, name: str) -> QuotaConfig | None:
        return self.quotas.get(name)

    def resolve(self, model: str) -> tuple[str, str]:
        """Resolve a client-supplied model string to (provider_name, model_id)."""
        if ":" in model:
            provider_hint, _, rest = model.partition(":")
            pname = normalize_provider(provider_hint)
            if pname in self.providers:
                return pname, (rest or self.provider(pname).default_model)
        route = self.models.get(model)
        if route is not None:
            return route.provider, route.model
        return "huggingface", model


def normalize_provider(name: str | None) -> str:
    aliases = {
        "hf": "huggingface",
        "huggingface": "huggingface",
        "local": "local",
        "llama": "local",
        "llamacpp": "local",
    }
    return aliases.get(name.lower(), name.lower()) if name else ""


def load_settings(
    config_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> Settings:
    env = env if env is not None else dict(os.environ)
    path = config_path or DEFAULT_CONFIG_PATH
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")

    dotenv_path = path.parent / ".env"
    if dotenv_path.exists():
        env = {**dotenv_values(dotenv_path), **env}

    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    providers: dict[str, ProviderConfig] = {}
    for name, pc in raw.get("providers", {}).items():
        base = pc.get("base_url", "")
        if env.get("LOCAL_BASE_URL") and name == "local":
            base = env["LOCAL_BASE_URL"]
        providers[name] = ProviderConfig(
            name=pc.get("name", name),
            base_url=base.rstrip("/"),
            default_model=pc.get("default_model", ""),
            timeout_seconds=pc.get("timeout_seconds", raw.get("timeout_seconds", 60.0)),
            stream_timeout_seconds=pc.get("stream_timeout_seconds", 240.0),
            api_key=env.get(pc["api_key_env"]) if pc.get("api_key_env") else None,
        )

    models = {
        alias: ModelRoute(provider=route["provider"], model=route["model"])
        for alias, route in raw.get("models", {}).items()
    }

    quotas = {}
    for name, qc in raw.get("quotas", {}).items():
        quotas[name] = QuotaConfig(
            daily_request_limit=qc.get("daily_requests"),
            daily_token_limit=qc.get("daily_tokens"),
            quota_reset_hour=qc.get("reset_hour", 0),
        )

    metrics_raw = raw.get("metrics", {})
    metrics = MetricsConfig(
        db_path=metrics_raw.get("db_path"),
        flush_interval_seconds=metrics_raw.get("flush_interval_seconds", 30),
        retention_days=metrics_raw.get("retention_days", 30),
        report_interval_seconds=metrics_raw.get("report_interval_seconds", 300),
    )

    return Settings(
        strategy=raw.get("strategy", "cloud-first"),
        timeout_seconds=raw.get("timeout_seconds", 60.0),
        providers=providers,
        models=models,
        quotas=quotas,
        metrics=metrics,
        host=env.get("HOST", "0.0.0.0"),
        port=int(env.get("PORT", "8000")),
    )