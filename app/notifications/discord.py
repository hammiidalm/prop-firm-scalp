"""Discord notification backend via webhook.

Simple embed-based messages posted to a Discord channel webhook URL.
No bot token required - just the webhook URL from channel settings.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.utils.logging import get_logger

log = get_logger(__name__)


class DiscordNotifier:
    """Posts trade events to a Discord webhook as rich embeds."""

    def __init__(self, webhook_url: str, *, timeout: float = 10.0) -> None:
        self._url = webhook_url
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(timeout))

    async def send(self, event: str, payload: dict[str, Any]) -> None:
        embed = self._build_embed(event, payload)
        body = {
            "username": "PropFirm Scalper",
            "embeds": [embed],
        }
        try:
            resp = await self._http.post(self._url, json=body)
            if resp.status_code >= 400:
                log.warning("discord webhook failed: %s %s", resp.status_code, resp.text[:200])
        except httpx.HTTPError as exc:
            log.warning("discord http error: %s", exc)

    async def close(self) -> None:
        await self._http.aclose()

    def _build_embed(self, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        color_map = {
            "trade_open": 0x00FF00,     # green
            "trade_close": 0xFF6600,    # orange
            "risk_reject": 0xFFCC00,    # yellow
            "semi_auto_pending": 0x3399FF,  # blue
            "order_failed": 0xFF0000,   # red
        }
        color = color_map.get(event, 0x808080)

        title_map = {
            "trade_open": "🟢 Trade Opened",
            "trade_close": "📊 Trade Closed",
            "risk_reject": "⚠️ Signal Rejected",
            "semi_auto_pending": "🟡 Setup Detected",
            "order_failed": "🚨 Order Failed",
        }
        title = title_map.get(event, f"📡 {event}")

        fields = []
        for key, val in payload.items():
            if key in ("structure_state",):  # skip noisy nested fields
                continue
            fields.append({
                "name": key.replace("_", " ").title(),
                "value": f"`{val}`" if not isinstance(val, dict) else "...",
                "inline": True,
            })
            if len(fields) >= 12:
                break

        return {
            "title": title,
            "color": color,
            "fields": fields,
            "footer": {"text": "prop-firm-scalp"},
        }
