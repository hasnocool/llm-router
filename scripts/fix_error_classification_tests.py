from pathlib import Path

path = Path("tests/test_error_classification_learning.py")
text = path.read_text()

marker = '''class ScriptedProvider:\n'''
helper = '''async def force_order(router, order):\n    profile = TaskProfile(kind="coding", confidence=1.0, coding_heavy=True)\n\n    async def ordered(_req):\n        return order, profile\n\n    router._order_for_request = ordered\n\n\nclass ScriptedProvider:\n'''
if text.count(marker) != 1:
    raise RuntimeError("ScriptedProvider marker mismatch")
text = text.replace(marker, helper, 1)

replacements = [
    (
        '''        router.providers["groq"] = ScriptedProvider()\n\n        response = await router.complete(request())\n''',
        '''        router.providers["groq"] = ScriptedProvider()\n        await force_order(router, [("openrouter", "paid-model"), ("openrouter", "openrouter/free"), ("groq", "groq-default")])\n\n        response = await router.complete(request())\n''',
    ),
    (
        '''        router.providers["openrouter"] = provider\n        router.providers["groq"] = ScriptedProvider()\n\n        response = await router.complete(request())\n''',
        '''        router.providers["openrouter"] = provider\n        router.providers["groq"] = ScriptedProvider()\n        await force_order(router, [("openrouter", "paid-model"), ("openrouter", "openrouter/free"), ("groq", "groq-default")])\n\n        response = await router.complete(request())\n''',
    ),
    (
        '''        router.providers["openrouter"] = first\n        router.providers["groq"] = second\n\n        response = await router.complete(request())\n''',
        '''        router.providers["openrouter"] = first\n        router.providers["groq"] = second\n        await force_order(router, [("openrouter", "paid-model"), ("openrouter", "openrouter/free"), ("groq", "groq-default")])\n\n        response = await router.complete(request())\n''',
    ),
]
for old, new in replacements:
    if text.count(old) != 1:
        raise RuntimeError(f"test replacement mismatch: {old[:60]!r}")
    text = text.replace(old, new, 1)

path.write_text(text)
