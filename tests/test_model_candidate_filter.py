from llm_router.zero_cost_router import ZeroCostModelRouter


def test_discovered_model_filter_rejects_obvious_non_chat_models() -> None:
    check = ZeroCostModelRouter._looks_chat_capable_model

    assert check("chat-alt", {"type": "chat"}) is True
    assert check("text-embedding-3-small", {"type": "embedding"}) is False
    assert check("whisper-large-v3", {"task": "transcription"}) is False
    assert check("rerank-v3", {}) is False
