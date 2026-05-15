"""Unified notification facade.

The ``Notifier`` class multiplexes events to all enabled backends
(Telegram, Discord). The execution layer only knows about the ``notify``
coroutine signature; it doesn't need to care which backends are active.

Events
------
Every event is identified by a string key (e.g. ``trade_open``,
``risk_reject``, ``semi_auto_pending``) and carries a dict payload.
Backends are free to format the message however they like.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.config import Settings
from app.notifications.discord import DiscordNotifier
from app.notifications.telegram import TelegramNotifier
from app.utils.logging import get_logger

log = get_logger(__name__)

NotifyFn = Callable[[str, dict[str, Any]], Awaitable[None]]


class Notifier:
    """Multiplexed notification dispatcher."""

    def __init__(self, settings: Settings) -> None:
        self._backends: list[NotifyFn] = []
        if settings.is_telegram_enabled():
            tg = TelegramNotifier(
                token=settings.telegram_bot_token.get_secret_value(),
                chat_id=settings.telegram_chat_id,
            )
            self._backends.append(tg.send)
            log.info("telegram notifications enabled")
        if settings.is_discord_enabled():
            dc = DiscordNotifier(
                webhook_url=settings.discord_webhook_url.get_secret_value(),
            )
            self._backends.append(dc.send)
            log.info("discord notifications enabled")

    async def notify(self, event: str, payload: dict[str, Any]) -> None:
        """Dispatch an event to all enabled backends. Failures are logged, never raised."""
        for backend in self._backends:
            try:
                await backend(event, payload)
            except Exception as exc:  # noqa: BLE001
                log.warning("notification backend error: %s %s", type(exc).__name__, exc)

    @property
    def enabled(self) -> bool:
        return bool(self._backends)
