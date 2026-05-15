"""Structured JSON logging.

A single ``configure_logging`` entrypoint installs a JSON formatter on the root
logger when ``log_json`` is enabled. All app modules should obtain loggers via
``get_logger(__name__)`` so log output stays consistent.

The formatter includes ISO-8601 UTC timestamps and a fixed set of fields
(``timestamp``, ``level``, ``logger``, ``message`` plus any ``extra`` dict)
which is friendly for log shippers (Loki, CloudWatch, etc).
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from pythonjsonlogger import jsonlogger


class _UtcJsonFormatter(jsonlogger.JsonFormatter):
    """JSON formatter that always emits UTC ISO-8601 timestamps."""

    def add_fields(
        self,
        log_record: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        super().add_fields(log_record, record, message_dict)
        # Standardize field names
        log_record.setdefault("timestamp", self.formatTime(record, "%Y-%m-%dT%H:%M:%S.%fZ"))
        log_record.setdefault("level", record.levelname)
        log_record.setdefault("logger", record.name)
        if "message" not in log_record:
            log_record["message"] = record.getMessage()


_CONFIGURED = False


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    """Install root-logger handlers. Idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    root = logging.getLogger()
    root.setLevel(level.upper())
    # Remove any pre-existing handlers (e.g. uvicorn's default basicConfig)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(stream=sys.stdout)
    if json_output:
        handler.setFormatter(
            _UtcJsonFormatter(
                "%(timestamp)s %(level)s %(logger)s %(message)s",
                rename_fields={"asctime": "timestamp", "levelname": "level", "name": "logger"},
            )
        )
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%SZ",
            )
        )
    root.addHandler(handler)

    # Tame noisy third-party loggers
    for noisy in ("httpx", "httpcore", "websockets", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a module-level logger. ``configure_logging`` should be called first."""
    return logging.getLogger(name)
