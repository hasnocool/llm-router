# scripts/fix_pr10_explicit.py
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


def replace_exact_count(path: str, old: str, new: str, expected: int) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != expected:
        raise RuntimeError(f"{path}: expected {expected} matches, found {count}: {old[:120]!r}")
    target.write_text(text.replace(old, new), encoding="utf-8")


def main() -> None:
    replace_once(
        "src/llm_router/router.py",
        '''    def _is_explicit(self, req: ChatRequest) -> bool:\n        colon_provider = req.model.partition(":")[0] if ":" in req.model else None\n        return bool(req.provider or normalize_provider(colon_provider) in self.providers)\n''',
        '''    def _explicit_provider(self, req: ChatRequest) -> str | None:\n        if req.provider:\n            return normalize_provider(req.provider)\n        model = req.model or ""\n        if ":" in model:\n            provider_hint = normalize_provider(model.partition(":")[0])\n            return provider_hint if provider_hint in self.providers else None\n        provider_name = normalize_provider(model)\n        return provider_name if provider_name in self.providers else None\n\n    def _is_explicit(self, req: ChatRequest) -> bool:\n        return self._explicit_provider(req) is not None\n''',
    )
    replace_once(
        "src/llm_router/router.py",
        '''        colon_provider = req.model.partition(":")[0] if ":" in req.model else None\n        explicit = req.provider or (\n            colon_provider if normalize_provider(colon_provider) in self.providers else None\n        )\n        if explicit:\n            name = normalize_provider(explicit)\n''',
        '''        explicit = self._explicit_provider(req)\n        if explicit:\n            name = explicit\n''',
    )
    replace_exact_count(
        "src/llm_router/zero_cost_router.py",
        '''        colon_provider = req.model.partition(":")[0] if ":" in req.model else None\n        explicit = req.provider or (\n            colon_provider if normalize_provider(colon_provider) in self.providers else None\n        )\n        if explicit:\n            return super()._order(req)\n''',
        '''        explicit = self._explicit_provider(req)\n        if explicit:\n            return super()._order(req)\n''',
        2,
    )


if __name__ == "__main__":
    main()
