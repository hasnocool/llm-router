from __future__ import annotations

from pathlib import Path

path = Path(__file__).with_name("apply_model_fallback_resilience.py")
text = path.read_text(encoding="utf-8")

needle = '''def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)
'''
replacement = needle + '''\n\ndef replace_first(text: str, old: str, new: str, label: str) -> str:\n    count = text.count(old)\n    if count < 1:\n        raise RuntimeError(f"{label}: expected at least one match, found {count}")\n    return text.replace(old, new, 1)\n'''
if text.count(needle) != 1:
    raise RuntimeError("replace_once helper shape changed")
text = text.replace(needle, replacement, 1)

for label in (
    "complete failed-provider set",
    "complete quota failure",
    "complete provider unavailable",
    "complete model-not-found",
):
    marker = f'    "{label}",\n)'
    marker_at = text.index(marker)
    call_at = text.rfind("text = replace_once(", 0, marker_at)
    if call_at < 0:
        raise RuntimeError(f"could not locate replace call for {label}")
    text = text[:call_at] + text[call_at:].replace(
        "text = replace_once(", "text = replace_first(", 1
    )

path.write_text(text, encoding="utf-8")
Path(__file__).unlink()
