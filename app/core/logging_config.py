"""
Structured logging setup — stdlib `logging` only, no external dependency.

Industry-grade services log in a machine-parseable shape (so they can be
shipped to something like CloudWatch/Datadog/ELK) rather than bare
`print()`. This module gives every other module in the app a consistent
`get_logger(__name__)` and configures the root logger once, at import time
of `app.main`, to emit single-line JSON records to stdout — the standard
"just write to stdout, let the platform collect it" pattern used by
Docker/Kubernetes/most PaaS log drains (Render, Railway, etc. all tail
stdout).

Log level is configurable via AI_LEADFLOW_LOG_LEVEL (default INFO) so a
deploy can turn on DEBUG without a code change.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any


class JsonFormatter(logging.Formatter):
    """Renders each log record as one JSON line.

    Deliberately minimal (no external `python-json-logger` dependency) —
    this is exactly the kind of thing worth keeping stdlib-only per the
    project's zero-hard-dependency philosophy for app/core/.
    """

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        payload: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Any structured extras passed via logger.info(..., extra={...})
        # ride along as top-level JSON keys (e.g. conversation_id, intent).
        for key, value in record.__dict__.items():
            if key in _RESERVED_LOG_RECORD_KEYS:
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


_RESERVED_LOG_RECORD_KEYS = set(logging.makeLogRecord({}).__dict__.keys())

_CONFIGURED = False


def configure_logging() -> None:
    """Idempotent — safe to call from multiple entry points (main.py,
    scripts/, tests) without double-attaching handlers."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    level_name = os.environ.get("AI_LEADFLOW_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [handler]  # replace any default handler uvicorn/etc added
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)
