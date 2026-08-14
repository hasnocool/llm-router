# scripts/fix_repair_pr9_round2.py
from __future__ import annotations

from pathlib import Path


TARGET = Path(__file__).with_name("repair_pr9_round2.py")


def main() -> None:
    text = TARGET.read_text(encoding="utf-8")

    helper_anchor = '''def remove_once(path: str, old: str) -> None:\n    replace_once(path, old, "")\n'''
    helper = '''def replace_exact_count(path: str, old: str, new: str, expected: int) -> None:\n    target = ROOT / path\n    text = target.read_text(encoding="utf-8")\n    count = text.count(old)\n    if count != expected:\n        raise RuntimeError(\n            f"{path}: expected exactly {expected} matches, found {count}: {old[:120]!r}"\n        )\n    target.write_text(text.replace(old, new), encoding="utf-8")\n\n\n'''
    if "def replace_exact_count(" not in text:
        if helper_anchor not in text:
            raise RuntimeError("could not locate helper insertion point")
        text = text.replace(helper_anchor, helper + helper_anchor, 1)

    duplicated = '''    replace_once(\n        "src/llm_router/zero_cost_router.py",\n        \'''        errors: list[str] = []\\n        order = await self._order_for_request(req)\\n        if not order:\\n            raise ProviderUnavailable("no zero-cost providers are currently eligible")\\n\\n        request_kind = classify_request_kind({"tools": req.tools, "tool_choice": req.tool_choice})\\n        explicit = self._is_explicit(req)\\n        request_id = uuid.uuid4().hex[:16]\\n        profile = await self._task_profile(req)\\n\''',\n        \'''        errors: list[str] = []\\n        order, profile = await self._order_for_request(req)\\n        if not order:\\n            raise ProviderUnavailable("no zero-cost providers are currently eligible")\\n\\n        request_kind = classify_request_kind({"tools": req.tools, "tool_choice": req.tool_choice})\\n        explicit = self._is_explicit(req)\\n        request_id = uuid.uuid4().hex[:16]\\n\''',\n    )\n'''
    if text.count(duplicated) != 2:
        raise RuntimeError(
            f"expected two duplicated complete/stream patch calls, found {text.count(duplicated)}"
        )

    replacement = duplicated.replace("replace_once(", "replace_exact_count(", 1)
    replacement = replacement[:-2] + "        2,\n    )\n"
    text = text.replace(duplicated, replacement, 1)
    text = text.replace(duplicated, "", 1)

    TARGET.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
