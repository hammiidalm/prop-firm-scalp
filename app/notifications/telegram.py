"""Telegram notification backend.

Sends formatted trade alerts via the Telegram Bot API (sendMessage).
Uses ``httpx`` directly for minimal dependencies; no need for the full
python-telegram-bot SDK at runtime (it's available if you want richer
interactions like inline keyboards for semi-auto confirmation).

Message formatting uses MarkdownV2 for clean presentation of trade data.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.utils.logging import get_logger

log = get_logger(__name__)

_TELEGRAM_API = "https://api.telegram.org"


class TelegramNotifier:
    """Sends notifications to a Telegram chat via Bot API."""

    def __init__(self, token: str, chat_id: str, *, timeout: float = 10.0) -> None:
        self._token = token
        self._chat_id = chat_id
        self._http = httpx.AsyncClient(
            base_url=f"{_TELEGRAM_API}/bot{token}",
            timeout=httpx.Timeout(timeout),
        )

    async def send(self, event: str, payload: dict[str, Any]) -> None:
        text = self._format(event, payload)
        try:
            resp = await self._http.post(
                "/sendMessage",
                json={
                    "chat_id": self._chat_id,
                    "text": text,
                    "parse_mode": "MarkdownV2",
                    "disable_web_page_preview": True,
                },
            )
            if resp.status_code >= 400:
                log.warning("telegram send failed: %s %s", resp.status_code, resp.text[:200])
        except httpx.HTTPError as exc:
            log.warning("telegram http error: %s", exc)

    async def close(self) -> None:
        await self._http.aclose()

    # ---- formatting ------------------------------------------------------
    def _format(self, event: str, payload: dict[str, Any]) -> str:
        """Map event types to human-readable Telegram messages."""
        formatters = {
            "trade_open": self._fmt_trade_open,
            "trade_close": self._fmt_trade_close,
            "risk_reject": self._fmt_risk_reject,
            "semi_auto_pending": self._fmt_semi_auto,
            "order_failed": self._fmt_order_failed,
        }
        formatter = formatters.get(event)
        if formatter:
            return formatter(payload)
        # Fallback: generic event dump
        return self._escape(f"📡 {event}\n{self._flat_dict(payload)}")

    def _fmt_trade_open(self, p: dict[str, Any]) -> str:
        symbol = p.get("symbol", "?")
        direction = p.get("direction", "?")
        entry = p.get("entry_price", "?")
        sl = p.get("stop_loss", "?")
        tp = p.get("take_profit", "?")
        lots = p.get("quantity", "?")
        rr = p.get("rr_ratio", "?")
        session = p.get("session", "?")
        emoji = "🟢" if direction == "LONG" else "🔴"

        # Extract confluence info from structure_tags or reason
        tags = p.get("structure_state", {}).get("tags", [])
        if isinstance(tags, list):
            confluence_tag = next((t for t in tags if t.startswith("CONFLUENCE:")), None)
        else:
            confluence_tag = None
        confluence_score = confluence_tag.split(":")[1] if confluence_tag else "?"
        # Count factors (tags that aren't CONFLUENCE:xx or SESSION_OK)
        factor_tags = [t for t in (tags if isinstance(tags, list) else [])
                       if not t.startswith("CONFLUENCE:")]
        factors_hit = len([t for t in factor_tags
                          if t not in ("SESSION_OK",) and "FVG" not in t])

        lines = [
            f"{emoji} *TRADE OPENED*",
            f"Symbol: `{symbol}`",
            f"Direction: `{direction}`",
            f"Entry: `{entry}`",
            f"SL: `{sl}` \\| TP: `{tp}`",
            f"Lots: `{lots}` \\| RR: `{rr}`",
            f"Confluence: `{confluence_score}/100` • `{factors_hit}/5 factors`",
            f"Session: `{session}`",
        ]
        return "\n".join(self._escape_line(l) for l in lines)

    def _fmt_trade_close(self, p: dict[str, Any]) -> str:
        symbol = p.get("symbol", "?")
        pnl = p.get("pnl", 0)
        status = p.get("status", "?")
        reason = p.get("exit_reason", "?")
        emoji = "✅" if "WIN" in str(status) else "❌" if "LOSS" in str(status) else "⚪"
        lines = [
            f"{emoji} *TRADE CLOSED*",
            f"Symbol: `{symbol}`",
            f"PnL: `{pnl:.2f}`" if isinstance(pnl, (int, float)) else f"PnL: `{pnl}`",
            f"Status: `{status}`",
            f"Reason: `{reason}`",
        ]
        return "\n".join(self._escape_line(l) for l in lines)

    def _fmt_risk_reject(self, p: dict[str, Any]) -> str:
        return self._escape(
            f"⚠️ SIGNAL REJECTED\n"
            f"Symbol: {p.get('symbol', '?')}\n"
            f"Reason: {p.get('reason', '?')}\n"
            f"Detail: {p.get('detail', '')}"
        )

    def _fmt_semi_auto(self, p: dict[str, Any]) -> str:
        symbol = p.get("symbol", "?")
        direction = p.get("direction", "?")
        entry = p.get("entry", "?")
        sl = p.get("sl", "?")
        tp = p.get("tp", "?")
        rr = p.get("rr", "?")
        lots = p.get("lots", "?")
        ttl = p.get("ttl_sec", 90)
        pending_id = p.get("pending_id", "?")
        emoji = "🟡"
        lines = [
            f"{emoji} *SETUP DETECTED \\- CONFIRM?*",
            f"ID: `{pending_id}`",
            f"Symbol: `{symbol}` \\| Dir: `{direction}`",
            f"Entry: `{entry}`",
            f"SL: `{sl}` \\| TP: `{tp}`",
            f"RR: `{rr}` \\| Lots: `{lots}`",
            f"⏱ Expires in `{ttl}s`",
            "",
            "Reply `/confirm {pending_id}` to execute",
        ]
        return "\n".join(lines)

    def _fmt_order_failed(self, p: dict[str, Any]) -> str:
        return self._escape(
            f"🚨 ORDER FAILED\nSymbol: {p.get('symbol', '?')}\nError: {p.get('error', '?')}"
        )

    # ---- MarkdownV2 escaping helpers ------------------------------------
    @staticmethod
    def _escape(text: str) -> str:
        """Escape all MarkdownV2 special chars."""
        special = r"_*[]()~`>#+-=|{}.!"
        out = []
        for ch in text:
            if ch in special:
                out.append(f"\\{ch}")
            else:
                out.append(ch)
        return "".join(out)

    @staticmethod
    def _escape_line(line: str) -> str:
        """Light escape: preserve backtick blocks and bold markers."""
        # Already manually escaped in formatters; pass through.
        return line

    @staticmethod
    def _flat_dict(d: dict[str, Any]) -> str:
        return "\n".join(f"  {k}: {v}" for k, v in d.items())
