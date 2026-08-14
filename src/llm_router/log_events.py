# src/llm_router/log_events.py
from __future__ import annotations

import json
import logging
import logging.handlers
import queue
import threading
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any


# Context attached to log records so buffered events carry provider/model/request_id.
EVENT_CONTEXT: ContextVar[dict[str, Any]] = ContextVar("event_context", default={})


class _AppEventBuffer:
    """Thread-safe buffer that Python logging emitters push events into.

    Router-side background task drains this buffer into the metrics DB so that
    standard `logging` calls (warnings, exceptions) land in the dashboard event
    log without blocking the request/event loop.
    """

    def __init__(self, maxsize: int = 20_000) -> None:
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=maxsize)
        self._overflow = 0

    def put(self, event: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self._overflow += 1

    def drain(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if self._overflow:
            events.append(
                {
                    "level": "warning",
                    "source": "logging",
                    "message": f"event log buffer overflowed; dropped {self._overflow} events",
                }
            )
            self._overflow = 0
        return events

    def size(self) -> int:
        return self._queue.qsize()


# Module-level singleton consumed by the router's background drain task.
APP_EVENT_BUFFER: _AppEventBuffer = _AppEventBuffer()


class DBEventLogHandler(logging.Handler):
    """Handler that forwards formatted log records into the buffered event log."""

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "skip_event_buffer", False):
            return
        details: dict[str, Any] = {}
        if record.exc_info:
            details["exc"] = self.formatter.formatException(record.exc_info) if self.formatter else None
        if record.stack_info:
            details["stack"] = record.stack_info
        if record.args:
            try:
                details["args"] = repr(record.args)
            except Exception:
                pass
        extra = getattr(record, "details", None)
        if isinstance(extra, dict):
            details.update(extra)
        context = EVENT_CONTEXT.get()
        APP_EVENT_BUFFER.put(
            {
                "level": record.levelname.lower(),
                "source": getattr(record, "source", None) or record.name,
                "message": self.format(record),
                "details": details,
                "provider": context.get("provider"),
                "model": context.get("model"),
                "request_id": context.get("request_id", ""),
            }
        )


def attach_event_context(
    *, provider: str | None = None, model: str | None = None, request_id: str = ""
) -> Token:
    """Temporarily attach routing context to log records emitted while held.

    Returns a token; call ``reset_event_context(token)`` in a finally block.
    """
    return EVENT_CONTEXT.set(
        {
            "provider": provider,
            "model": model,
            "request_id": request_id,
        }
    )


def reset_event_context(token: Token) -> None:
    EVENT_CONTEXT.reset(token)


def configure_logging(config, *, log_file_dir: Path | None = None) -> None:
    """Configure the root logger with a console handler and optional rotating file.

    Args:
        config: LogsConfig instance (logs.level, logs.file_path, ...).
        log_file_dir: Base directory used to resolve a relative ``file_path``.
    """
    root = logging.getLogger()
    record: list[bool] = []

    def has_handler_of(handler_type: type[logging.Handler]) -> bool:
        return any(isinstance(h, handler_type) for h in root.handlers)

    if not has_handler_of(logging.StreamHandler):
        console = logging.StreamHandler()
        console.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root.addHandler(console)

    if config.file_path:
        file_path = Path(config.file_path)
        if not file_path.is_absolute() and log_file_dir is not None:
            file_path = log_file_dir / file_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        if not has_handler_of(logging.handlers.RotatingFileHandler):
            from logging.handlers import RotatingFileHandler

            file_handler = RotatingFileHandler(
                file_path,
                maxBytes=config.max_bytes,
                backupCount=config.backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s %(levelname)s %(name)s [%(process)d] "
                    "%(message)s %(exc_info)s"
                )
            )
            root.addHandler(file_handler)

    if not has_handler_of(DBEventLogHandler):
        db_handler = DBEventLogHandler()
        db_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root.addHandler(db_handler)

    root.setLevel(getattr(logging, config.level.upper(), logging.INFO))