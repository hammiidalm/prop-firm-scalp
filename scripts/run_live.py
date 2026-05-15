"""Live trading entrypoint.

Starts the full engine (broker connection, WebSocket streaming, strategy,
risk management, execution, dashboard API, and notifications).

Usage:
    python -m scripts.run_live                  # uses .env defaults
    APP_MODE=semi_auto python -m scripts.run_live  # semi-auto mode
    APP_MODE=full_auto python -m scripts.run_live  # full-auto mode

The engine runs until SIGINT (Ctrl+C) or SIGTERM, at which point it
gracefully shuts down all subsystems.
"""

from __future__ import annotations

import asyncio
import sys

from app.config import get_settings
from app.engine import Engine
from app.utils.logging import configure_logging, get_logger


async def _main() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    log = get_logger(__name__)

    log.info(
        "=== prop-firm-scalp starting ===",
        extra={
            "mode": settings.app_mode.value,
            "env": settings.app_env.value,
            "symbols": settings.symbols,
        },
    )

    engine = Engine(settings)
    try:
        await engine.run()
    except KeyboardInterrupt:
        log.info("keyboard interrupt received")
    except Exception as exc:
        log.exception("fatal error: %s", exc)
        sys.exit(1)
    finally:
        log.info("=== prop-firm-scalp stopped ===")


def main() -> None:
    """Sync entrypoint for ``pyproject.toml`` scripts and Docker CMD."""
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
