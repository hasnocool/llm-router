# scripts/repair_pr9_round2.py
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: str, old: str, new: str) -> None:
    target = ROOT / path
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected exactly one match, found {count}: {old[:120]!r}")
    target.write_text(text.replace(old, new, 1), encoding="utf-8")


def remove_once(path: str, old: str) -> None:
    replace_once(path, old, "")


def patch_logging_defaults() -> None:
    replace_once(
        "src/llm_router/config.py",
        "    log_message_bodies: bool = True\n",
        "    log_message_bodies: bool = False\n",
    )
    replace_once(
        "src/llm_router/config.py",
        '            logs_raw.get("log_message_bodies", True),\n',
        '            logs_raw.get("log_message_bodies", False),\n',
    )
    replace_once(
        "config.toml",
        "# Persist request/response message bodies to the dashboard message log.\nlog_message_bodies = true\n",
        "# Persist full request/response bodies only when explicitly enabled for local debugging.\n# Bodies may contain private or personal data.\nlog_message_bodies = false\n",
    )
    replace_once(
        ".env.example",
        "# Logging (see [logs] in config.toml; these override defaults)\nLLM_ROUTER_LOG_LEVEL=INFO\nLLM_ROUTER_LOG_MESSAGE_BODIES=true\n",
        "# Logging (see [logs] in config.toml; these override defaults)\nLLM_ROUTER_LOG_LEVEL=INFO\n# Captures full prompt/response bodies in SQLite. Enable only for local debugging.\nLLM_ROUTER_LOG_MESSAGE_BODIES=false\n",
    )


def patch_metrics_db() -> None:
    redundant = '''        decisions_cols = {row["name"] for row in conn.execute("PRAGMA table_info(routing_decisions)")}\n        if not decisions_cols:\n            conn.execute(\n                "CREATE TABLE IF NOT EXISTS routing_decisions ("\n                "request_id TEXT PRIMARY KEY,"\n                "occurred_at REAL NOT NULL,"\n                "task_kind TEXT NOT NULL DEFAULT 'general',"\n                "request_kind TEXT NOT NULL DEFAULT 'chat',"\n                "explicit INTEGER NOT NULL DEFAULT 0,"\n                "selected_provider TEXT,"\n                "selected_model TEXT NOT NULL DEFAULT '',"\n                "selected_rank INTEGER NOT NULL DEFAULT 0,"\n                "selected_score REAL NOT NULL DEFAULT 0,"\n                "learned_bonus REAL NOT NULL DEFAULT 0,"\n                "exploration_bonus REAL NOT NULL DEFAULT 0,"\n                "candidate_json TEXT NOT NULL DEFAULT '[]',"\n                "epsilon REAL NOT NULL DEFAULT 0,"\n                "notes TEXT NOT NULL DEFAULT '',"\n                "created_at REAL NOT NULL DEFAULT 0"\n                ")"\n            )\n        adapt_cols = {row["name"] for row in conn.execute("PRAGMA table_info(routing_adaptation_stats)")}\n        if not adapt_cols:\n            conn.execute(\n                "CREATE TABLE IF NOT EXISTS routing_adaptation_stats ("\n                "provider_name TEXT NOT NULL,"\n                "task_kind TEXT NOT NULL DEFAULT 'general',"\n                "attempts INTEGER NOT NULL DEFAULT 0,"\n                "successes INTEGER NOT NULL DEFAULT 0,"\n                "failures INTEGER NOT NULL DEFAULT 0,"\n                "total_reward REAL NOT NULL DEFAULT 0,"\n                "total_latency_ms REAL NOT NULL DEFAULT 0,"\n                "latency_count INTEGER NOT NULL DEFAULT 0,"\n                "last_attempt_at REAL NOT NULL DEFAULT 0,"\n                "last_success_at REAL NOT NULL DEFAULT 0,"\n                "last_failure_at REAL NOT NULL DEFAULT 0,"\n                "PRIMARY KEY(provider_name, task_kind)"\n                ")"\n            )\n'''
    remove_once("src/llm_router/metrics_db.py", redundant)

    replace_once(
        "src/llm_router/metrics_db.py",
        '''            FROM provider_request_events\n            WHERE occurred_at >= ?\n            GROUP BY provider_name\n            ORDER BY attempts DESC, provider_name\n''',
        '''            FROM provider_request_events\n            WHERE occurred_at >= ? AND request_kind != 'model_discovery'\n            GROUP BY provider_name\n            ORDER BY attempts DESC, provider_name\n''',
    )
    replace_once(
        "src/llm_router/metrics_db.py",
        '''            FROM provider_request_events\n            WHERE occurred_at >= ?\n            GROUP BY day\n            ORDER BY day ASC\n''',
        '''            FROM provider_request_events\n            WHERE occurred_at >= ? AND request_kind != 'model_discovery'\n            GROUP BY day\n            ORDER BY day ASC\n''',
    )
    replace_once(
        "src/llm_router/metrics_db.py",
        '''                meta_values = [meta.get(key) for key in self.MODEL_META_KEYS]\n                # Defaulting logic - ensure all NOT NULL columns have values\n                for i, key in enumerate(self.MODEL_META_KEYS):\n                    if meta_values[i] is None:\n                        if key == "rate_limit_source":\n                            meta_values[i] = ""\n                        elif key in ("input_cost_per_1m", "output_cost_per_1m", "request_cost", "meta_updated_at"):\n                            meta_values[i] = None # These allow NULL\n                        else:\n                            # INTEGER columns: max_output_tokens, rpm, rpd, tpm, tpd, rps\n                            meta_values[i] = 0\n                \n                meta_updated = meta.get("meta_updated_at") or now\n                meta_values[self.MODEL_META_KEYS.index("meta_updated_at")] = meta_updated\n''',
        '''                # Only rate_limit_source is NOT NULL. Keep every other missing\n                # metadata value as NULL so unknown capacity is not mistaken for zero.\n                meta_values = [\n                    "" if key == "rate_limit_source" and meta.get(key) is None else meta.get(key)\n                    for key in self.MODEL_META_KEYS\n                ]\n                meta_updated = meta.get("meta_updated_at") or now\n                meta_values[self.MODEL_META_KEYS.index("meta_updated_at")] = meta_updated\n''',
    )
    replace_once(
        "src/llm_router/metrics_db.py",
        '''            conn.execute("DELETE FROM app_events WHERE occurred_at < ?", (cutoff_ts,))\n            conn.execute("DELETE FROM provider_quota_reservations WHERE expires_at <= ?", (now_ts,))\n''',
        '''            conn.execute("DELETE FROM app_events WHERE occurred_at < ?", (cutoff_ts,))\n            conn.execute("DELETE FROM routing_decisions WHERE occurred_at < ?", (cutoff_ts,))\n            # routing_adaptation_stats intentionally remains cumulative; it is a\n            # compact per-provider/task aggregate rather than a per-request log.\n            conn.execute("DELETE FROM provider_quota_reservations WHERE expires_at <= ?", (now_ts,))\n''',
    )


def patch_provider_error_handling() -> None:
    replace_once(
        "src/llm_router/providers/base.py",
        "            except (httpx.ResponseNotRead, httpx.DecodingError, UnicodeError):\n",
        "            except (httpx.HTTPError, httpx.StreamError, UnicodeError):\n",
    )


def patch_gemini_schema() -> None:
    for line in (
        '            "uniqueItems",\n',
        '            "prefixItems",\n',
        '            "oneOf",\n',
        '            "allOf",\n',
        '            "not",\n',
    ):
        remove_once("src/llm_router/providers/google_ai.py", line)


def patch_event_logging() -> None:
    replace_once(
        "src/llm_router/log_events.py",
        '''    def emit(self, record: logging.LogRecord) -> None:\n        details: dict[str, Any] = {}\n''',
        '''    def emit(self, record: logging.LogRecord) -> None:\n        if getattr(record, "skip_event_buffer", False):\n            return\n        details: dict[str, Any] = {}\n''',
    )

    replacements = {
        'logging.getLogger(__name__).exception("failed to persist event log entry")': 'logger.exception("failed to persist event log entry", extra={"skip_event_buffer": True})',
        'logging.getLogger(__name__).warning("event log drain failed: %s", exc)': 'logger.warning("event log drain failed: %s", exc, extra={"skip_event_buffer": True})',
        'logging.getLogger(__name__).exception("app event recording failed")': 'logger.exception("app event recording failed", extra={"skip_event_buffer": True})',
        'logging.getLogger(__name__).exception("router event recording failed")': 'logger.exception("router event recording failed", extra={"skip_event_buffer": True})',
        'logging.getLogger(__name__).exception("message log recording failed")': 'logger.exception("message log recording failed", extra={"skip_event_buffer": True})',
    }
    for old, new in replacements.items():
        replace_once("src/llm_router/router.py", old, new)

    replace_once(
        "src/llm_router/router.py",
        '''        record_fn = getattr(logging, level.lower(), None)\n        try:\n            if callable(record_fn):\n                extra: dict[str, object] = {"source": source}\n                if details:\n                    extra["details"] = details\n                record_fn("%s", message, extra=extra)\n        finally:\n            reset_event_context(token)\n''',
        '''        level_no = {\n            "debug": logging.DEBUG,\n            "info": logging.INFO,\n            "warning": logging.WARNING,\n            "error": logging.ERROR,\n            "critical": logging.CRITICAL,\n        }.get(level.lower(), logging.INFO)\n        try:\n            extra: dict[str, object] = {"source": source}\n            if details:\n                extra["details"] = details\n            logger.log(level_no, "%s", message, extra=extra)\n        finally:\n            reset_event_context(token)\n''',
    )


def patch_router_edge_cases() -> None:
    replace_once(
        "src/llm_router/router.py",
        '''        try:\n            obj = json.loads(payload)\n        except json.JSONDecodeError:\n            return False\n        for choice in obj.get("choices") or []:\n            delta = choice.get("delta") or {}\n            if delta.get("tool_calls"):\n                return True\n        return False\n''',
        '''        try:\n            obj = json.loads(payload)\n        except json.JSONDecodeError:\n            return False\n        if not isinstance(obj, dict):\n            return False\n        choices = obj.get("choices")\n        if not isinstance(choices, list):\n            return False\n        for choice in choices:\n            if not isinstance(choice, dict):\n                continue\n            delta = choice.get("delta")\n            if isinstance(delta, dict) and delta.get("tool_calls"):\n                return True\n        return False\n''',
    )
    replace_once(
        "src/llm_router/router.py",
        '''    @staticmethod\n    def _json_body(value: object, max_chars: int = 4_000) -> str:\n        try:\n            raw = json.dumps(value, ensure_ascii=False, default=str)\n        except (TypeError, ValueError):\n            raw = str(value)\n        if len(raw) > max_chars:\n            raw = raw[:max_chars] + '..."(truncated)'\n        return raw\n''',
        '''    @staticmethod\n    def _json_body(value: object, max_chars: int = 4_000) -> str:\n        try:\n            raw = json.dumps(value, ensure_ascii=False, default=str)\n        except (TypeError, ValueError):\n            raw = json.dumps(str(value), ensure_ascii=False)\n        if len(raw) <= max_chars:\n            return raw\n        if max_chars < 2:\n            return "0"\n\n        def encoded_preview(length: int) -> str:\n            return json.dumps(\n                {"truncated": True, "preview": raw[:length]},\n                ensure_ascii=False,\n                separators=(",", ":"),\n            )\n\n        low, high = 0, min(len(raw), max_chars)\n        best = "0"\n        while low <= high:\n            mid = (low + high) // 2\n            candidate = encoded_preview(mid)\n            if len(candidate) <= max_chars:\n                best = candidate\n                low = mid + 1\n            else:\n                high = mid - 1\n        return best\n''',
    )


def patch_task_profile_reuse() -> None:
    replace_once(
        "src/llm_router/zero_cost_router.py",
        '''    async def _order_for_request(self, req: ChatRequest) -> list[tuple[str, str]]:\n        if self.settings.strategy != "zero-cost":\n            return self._order(req)\n        profile = await self._task_profile(req)\n        return self._order_with_profile(req, profile)\n''',
        '''    async def _order_for_request(\n        self, req: ChatRequest\n    ) -> tuple[list[tuple[str, str]], TaskProfile]:\n        if self.settings.strategy != "zero-cost":\n            profile = TaskProfile(kind="general", confidence=1.0)\n            return self._order(req), profile\n        profile = await self._task_profile(req)\n        return self._order_with_profile(req, profile), profile\n''',
    )
    replace_once(
        "src/llm_router/zero_cost_router.py",
        '''        errors: list[str] = []\n        order = await self._order_for_request(req)\n        if not order:\n            raise ProviderUnavailable("no zero-cost providers are currently eligible")\n\n        request_kind = classify_request_kind({"tools": req.tools, "tool_choice": req.tool_choice})\n        explicit = self._is_explicit(req)\n        request_id = uuid.uuid4().hex[:16]\n        profile = await self._task_profile(req)\n''',
        '''        errors: list[str] = []\n        order, profile = await self._order_for_request(req)\n        if not order:\n            raise ProviderUnavailable("no zero-cost providers are currently eligible")\n\n        request_kind = classify_request_kind({"tools": req.tools, "tool_choice": req.tool_choice})\n        explicit = self._is_explicit(req)\n        request_id = uuid.uuid4().hex[:16]\n''',
    )
    replace_once(
        "src/llm_router/zero_cost_router.py",
        '''        errors: list[str] = []\n        order = await self._order_for_request(req)\n        if not order:\n            raise ProviderUnavailable("no zero-cost providers are currently eligible")\n\n        request_kind = classify_request_kind({"tools": req.tools, "tool_choice": req.tool_choice})\n        explicit = self._is_explicit(req)\n        request_id = uuid.uuid4().hex[:16]\n        profile = await self._task_profile(req)\n''',
        '''        errors: list[str] = []\n        order, profile = await self._order_for_request(req)\n        if not order:\n            raise ProviderUnavailable("no zero-cost providers are currently eligible")\n\n        request_kind = classify_request_kind({"tools": req.tools, "tool_choice": req.tool_choice})\n        explicit = self._is_explicit(req)\n        request_id = uuid.uuid4().hex[:16]\n''',
    )


def patch_stale_ui_responses() -> None:
    replace_once(
        "src/llm_router/static/dashboard.js",
        "let charts = {};\n",
        "let charts = {};\nlet loadVersion = 0;\n",
    )
    replace_once(
        "src/llm_router/static/dashboard.js",
        '''async function load() {\n  const days = document.getElementById("days").value;\n  document.getElementById("status").textContent = "loading…";\n''',
        '''async function load() {\n  const version = ++loadVersion;\n  const days = document.getElementById("days").value;\n  document.getElementById("status").textContent = "loading…";\n''',
    )
    replace_once(
        "src/llm_router/static/dashboard.js",
        '''    state = await resp.json();\n    render();\n''',
        '''    const nextState = await resp.json();\n    if (version !== loadVersion) return;\n    state = nextState;\n    render();\n''',
    )
    replace_once(
        "src/llm_router/static/dashboard.js",
        '''  } catch (err) {\n    document.getElementById("status").textContent = "error: " + err.message;\n''',
        '''  } catch (err) {\n    if (version !== loadVersion) return;\n    document.getElementById("status").textContent = "error: " + err.message;\n''',
    )

    replace_once(
        "src/llm_router/static/logs.js",
        "let timer = null;\n",
        "let timer = null;\nlet loadVersion = 0;\n",
    )
    replace_once(
        "src/llm_router/static/logs.js",
        '''async function load() {\n  const q = new URLSearchParams({\n''',
        '''async function load() {\n  const version = ++loadVersion;\n  const q = new URLSearchParams({\n''',
    )
    replace_once(
        "src/llm_router/static/logs.js",
        '''    state = await resp.json();\n    populateProviders();\n''',
        '''    const nextState = await resp.json();\n    if (version !== loadVersion) return;\n    state = nextState;\n    populateProviders();\n''',
    )
    replace_once(
        "src/llm_router/static/logs.js",
        '''  } catch (err) {\n    status.textContent = "error: " + err.message;\n''',
        '''  } catch (err) {\n    if (version !== loadVersion) return;\n    status.textContent = "error: " + err.message;\n''',
    )


def add_regression_tests() -> None:
    path = ROOT / "tests/test_merge_gate_regressions.py"
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "from types import MethodType\n\nimport httpx\n",
        "from types import MethodType\nimport json\nimport logging\nimport tomllib\nfrom pathlib import Path\n\nimport httpx\n",
        1,
    )
    text = text.replace(
        "from llm_router.config import ProviderConfig, Settings\n",
        "from llm_router.config import LogsConfig, ProviderConfig, Settings\n",
        1,
    )
    text = text.replace(
        "from llm_router.providers.base import classify_request_kind\n",
        "from llm_router.log_events import APP_EVENT_BUFFER, DBEventLogHandler\nfrom llm_router.metrics_db import MetricsDB\nfrom llm_router.providers.base import classify_request_kind\n",
        1,
    )
    text = text.replace(
        "from llm_router.routing_score import RouteScore\n",
        "from llm_router.router import ModelRouter\nfrom llm_router.routing_score import RouteScore\n",
        1,
    )
    additions = r'''


def test_default_message_body_logging_is_opt_in() -> None:
    assert LogsConfig().log_message_bodies is False


def test_repository_config_is_valid_toml() -> None:
    parsed = tomllib.loads(Path("config.toml").read_text(encoding="utf-8"))
    assert parsed["logs"]["log_message_bodies"] is False


def test_gemini_schema_strips_unsupported_composition_keywords() -> None:
    cleaned = GoogleAIProvider._sanitize_schema(
        {
            "type": "object",
            "properties": {
                "value": {
                    "type": "string",
                    "oneOf": [{"type": "string"}],
                    "allOf": [{"type": "string"}],
                    "not": {"type": "number"},
                    "prefixItems": [{"type": "string"}],
                    "uniqueItems": True,
                    "anyOf": [{"type": "string"}, {"type": "number"}],
                }
            },
        }
    )
    value = cleaned["properties"]["value"]
    assert "oneOf" not in value
    assert "allOf" not in value
    assert "not" not in value
    assert "prefixItems" not in value
    assert "uniqueItems" not in value
    assert "anyOf" in value


def test_sse_tool_call_detection_ignores_non_object_json() -> None:
    assert ModelRouter._sse_has_tool_call("data: []") is False
    assert ModelRouter._sse_has_tool_call("data: 5") is False
    assert ModelRouter._sse_has_tool_call(
        'data: {"choices":[{"delta":{"tool_calls":[{"id":"x"}]}}]}'
    ) is True


def test_truncated_message_body_remains_valid_json() -> None:
    encoded = ModelRouter._json_body({"body": "x" * 1000}, max_chars=80)
    decoded = json.loads(encoded)
    assert len(encoded) <= 80
    assert isinstance(decoded, (dict, int))
    if isinstance(decoded, dict):
        assert decoded["truncated"] is True


def test_event_handler_can_skip_persistence_diagnostics() -> None:
    APP_EVENT_BUFFER.drain()
    handler = DBEventLogHandler()
    record = logging.LogRecord(
        name="llm_router.router",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="db unavailable",
        args=(),
        exc_info=None,
    )
    record.skip_event_buffer = True
    handler.emit(record)
    assert APP_EVENT_BUFFER.drain() == []


def test_provider_model_unknown_limits_stay_null_and_preserve_learned_values(tmp_path) -> None:
    db = MetricsDB(tmp_path / "metrics.db")
    try:
        db.upsert_provider_models("demo", [("m1", "discovery", {"rpm": 10})])
        db.upsert_provider_models("demo", [("m1", "discovery", {})])
        row = db.get_all_provider_models()["demo"][0]
        assert row["rpm"] == 10
        for key in ("max_output_tokens", "rpd", "tpm", "tpd", "rps"):
            assert row[key] is None
        assert row["rate_limit_source"] == ""
        assert row["meta_updated_at"] is not None
    finally:
        db.close()


def test_provider_attempt_metrics_exclude_model_discovery(tmp_path) -> None:
    db = MetricsDB(tmp_path / "metrics.db")
    try:
        db.reconcile_reservation(
            None,
            "demo",
            True,
            request_kind="model_discovery",
        )
        db.reconcile_reservation(
            None,
            "demo",
            True,
            request_kind="chat",
        )
        rows = db.get_provider_attempt_metrics(days=1)
        assert len(rows) == 1
        assert rows[0]["provider_name"] == "demo"
        assert rows[0]["attempts"] == 1
    finally:
        db.close()
'''
    if "test_default_message_body_logging_is_opt_in" in text:
        raise RuntimeError("round-2 regression tests already present")
    path.write_text(text + additions, encoding="utf-8")


def main() -> None:
    patch_logging_defaults()
    patch_metrics_db()
    patch_provider_error_handling()
    patch_gemini_schema()
    patch_event_logging()
    patch_router_edge_cases()
    patch_task_profile_reuse()
    patch_stale_ui_responses()
    add_regression_tests()


if __name__ == "__main__":
    main()
