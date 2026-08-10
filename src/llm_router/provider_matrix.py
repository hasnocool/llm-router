# src/llm_router/provider_matrix.py
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from typing import Any, Iterable, Mapping


PROVIDER_MATRIX_ALIASES: dict[str, str] = {
    "groq": "groq",
    "huggingface": "hugging-face-inference-providers",
    "hf": "hugging-face-inference-providers",
    "google_ai": "google-gemini-api",
    "gemini": "google-gemini-api",
    "cerebras": "cerebras",
    "nvidia": "nvidia-api-catalog",
    "openrouter": "openrouter",
    "mistral": "mistral-api-free-mode",
    "github_models": "github-models",
    "vercel": "vercel-ai-gateway",
    "cohere": "cohere",
    "cloudflare": "cloudflare-workers-ai",
    "ibm_watsonx": "ibm-watsonx-ai-runtime",
    "kilo": "kilo-gateway",
    "lightning": "lightning-ai-free",
}


@dataclass(frozen=True, slots=True)
class ProviderMatrixEntry:
    """Normalized provider/program metadata used by the router."""

    id: str
    name: str
    kind: str
    free_class: str
    cadence: str
    status: str
    confidence: str
    access_mode: str
    tokens_per_day_equivalent: int | None
    tokens_per_day_basis: str
    coding_quality: float
    context_window: str
    tool_calling: bool | str
    vision: bool | str
    openai_compatible: str
    cli_support: str
    credit_card_required: bool | str
    region: tuple[str, ...] | str
    privacy: str
    commercial_use: str
    quota_endpoint: str
    router_eligible: str
    zero_cost_routing_score: int
    reset_or_expiry: str
    models_or_scope: str
    allowance: str
    last_verified: str
    sources: tuple[str, ...]
    notes: tuple[str, ...]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ProviderMatrixEntry":
        tokens = raw.get("tokens_per_day_equivalent")
        if isinstance(tokens, float) and tokens.is_integer():
            tokens = int(tokens)
        region_raw = raw.get("region", "")
        region = (
            tuple(str(v) for v in region_raw)
            if isinstance(region_raw, (list, tuple))
            else str(region_raw or "")
        )
        return cls(
            id=str(raw["id"]),
            name=str(raw["name"]),
            kind=str(raw.get("kind", "")),
            free_class=str(raw.get("free_class", "")),
            cadence=str(raw.get("cadence", "")),
            status=str(raw.get("status", "")),
            confidence=str(raw.get("confidence", "")),
            access_mode=str(raw.get("access_mode", "")),
            tokens_per_day_equivalent=(int(tokens) if isinstance(tokens, (int, float)) else None),
            tokens_per_day_basis=str(raw.get("tokens_per_day_basis", "")),
            coding_quality=float(raw.get("coding_quality") or 0.0),
            context_window=str(raw.get("context_window", "model-dependent")),
            tool_calling=raw.get("tool_calling", "unknown"),
            vision=raw.get("vision", "unknown"),
            openai_compatible=str(raw.get("openai_compatible", "unknown")),
            cli_support=str(raw.get("cli_support", "unknown")),
            credit_card_required=raw.get("credit_card_required", "unknown"),
            region=region,
            privacy=str(raw.get("privacy", "")),
            commercial_use=str(raw.get("commercial_use", "")),
            quota_endpoint=str(raw.get("quota_endpoint") or ""),
            router_eligible=str(raw.get("router_eligible", "none")),
            zero_cost_routing_score=int(raw.get("zero_cost_routing_score") or 0),
            reset_or_expiry=str(raw.get("reset_or_expiry", "")),
            models_or_scope=str(raw.get("models_or_scope", "")),
            allowance=str(raw.get("allowance", "")),
            last_verified=str(raw.get("last_verified", "")),
            sources=tuple(str(v) for v in raw.get("sources", []) if v),
            notes=tuple(str(v) for v in raw.get("notes", []) if v),
        )

    def as_public_dict(self) -> dict[str, Any]:
        """Return stable JSON-safe fields suitable for API responses."""
        return {
            "id": self.id,
            "name": self.name,
            "free_class": self.free_class,
            "cadence": self.cadence,
            "status": self.status,
            "access_mode": self.access_mode,
            "tokens_per_day_equivalent": self.tokens_per_day_equivalent,
            "coding_quality": self.coding_quality,
            "context_window": self.context_window,
            "tool_calling": self.tool_calling,
            "vision": self.vision,
            "openai_compatible": self.openai_compatible,
            "cli_support": self.cli_support,
            "credit_card_required": self.credit_card_required,
            "region": list(self.region) if isinstance(self.region, tuple) else self.region,
            "privacy": self.privacy,
            "commercial_use": self.commercial_use,
            "quota_endpoint": self.quota_endpoint,
            "router_eligible": self.router_eligible,
            "zero_cost_routing_score": self.zero_cost_routing_score,
            "last_verified": self.last_verified,
            "sources": list(self.sources),
        }


def _matrix_paths():
    data_root = files("llm_router.data")
    return tuple(
        sorted(
            (item for item in data_root.iterdir() if item.name.startswith("provider_matrix_") and item.name.endswith(".json")),
            key=lambda item: item.name,
        )
    )


@lru_cache(maxsize=1)
def load_provider_matrix() -> tuple[ProviderMatrixEntry, ...]:
    """Load packaged matrix chunks once; no network/file I/O occurs per request."""
    providers: list[Mapping[str, Any]] = []
    for path in _matrix_paths():
        with path.open("r", encoding="utf-8") as handle:
            chunk = json.load(handle)
        if not isinstance(chunk, list):
            raise ValueError(f"invalid provider matrix chunk: {path.name}")
        providers.extend(chunk)
    return tuple(ProviderMatrixEntry.from_mapping(item) for item in providers)


@lru_cache(maxsize=1)
def provider_matrix_by_id() -> dict[str, ProviderMatrixEntry]:
    return {entry.id: entry for entry in load_provider_matrix()}


def matrix_id_for_provider(provider_name: str) -> str:
    normalized = provider_name.strip().lower().replace("-", "_")
    return PROVIDER_MATRIX_ALIASES.get(normalized, normalized.replace("_", "-"))


def get_provider_matrix_entry(provider_name: str) -> ProviderMatrixEntry | None:
    return provider_matrix_by_id().get(matrix_id_for_provider(provider_name))


def iter_provider_matrix() -> Iterable[ProviderMatrixEntry]:
    return load_provider_matrix()
