"""Live trading entrypoint.

Starts the full engine (broker connection, WebSocket streaming, strategy,
risk management, execution, dashboard API, and notifications) alongside
the interactive Telegram bot for command-based monitoring and control.

Usage:
    python -m scripts.run_live                  # uses .env defaults
    APP_MODE=semi_auto python -m scripts.run_live  # semi-auto mode
    APP_MODE=full_auto python -m scripts.run_live  # full-auto mode

The engine runs until SIGINT (Ctrl+C) or SIGTERM, at which point it
gracefully shuts down all subsystems including the Telegram bot.
"""

from __future__ import annotations

import asyncio
import sys

from app.config import get_settings
from app.engine import Engine
from app.utils.logging import configure_logging, get_logger


async def _start_telegram_bot(settings) -> object | None:
    """Start the interactive Telegram bot if configured.

    Returns the TelegramBot instance (for graceful shutdown) or None
    if not enabled.
    """
    if not settings.is_telegram_bot_enabled():
        return None

    from app.telegram_bot import TelegramBot

    bot = TelegramBot(
        token=settings.telegram_bot_token.get_secret_value(),
        admin_chat_ids=settings.get_admin_chat_ids(),
        api_base_url=f"http://localhost:{settings.api_port}",
    )
    await bot.start()
    return bot


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
            "telegram_bot": settings.is_telegram_bot_enabled(),
        },
    )

    engine = Engine(settings)
    telegram_bot = None

    try:
        # Start the engine (broker, strategy, API server, etc.)
        await engine.start()

        # Start the Telegram bot after the API is ready
        # (it fetches data from the internal API)
        telegram_bot = await _start_telegram_bot(settings)
        if telegram_bot:
            log.info("telegram interactive bot started")

        # Install signal handlers and wait for shutdown
        engine._install_signal_handlers()

        tasks: list[asyncio.Task] = []
        if engine._api_server:
            tasks.append(asyncio.create_task(engine._api_server.serve(), name="api-server"))

        # Wait for shutdown signal
        await engine._shutdown_event.wait()

    except KeyboardInterrupt:
        log.info("keyboard interrupt received")
    except Exception as exc:
        log.exception("fatal error: %s", exc)
        sys.exit(1)
    finally:
        log.info("shutting down...")

        # Stop Telegram bot first (quick, non-blocking)
        if telegram_bot:
            try:
                await telegram_bot.stop()
                log.info("telegram bot stopped")
            except Exception as exc:  # noqa: BLE001
                log.warning("telegram bot stop error: %s", exc)

        # Stop the engine (broker, WS, journal, API)
        await engine.stop()

        log.info("=== prop-firm-scalp stopped ===")


def main() -> None:
    """Sync entrypoint for ``pyproject.toml`` scripts and Docker CMD."""
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
