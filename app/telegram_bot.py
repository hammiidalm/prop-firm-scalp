"""Interactive Telegram bot for prop-firm-scalp.

Provides command-based interaction for monitoring and controlling the
trading bot directly from Telegram. Only responds to authorized chat IDs
defined in ``ADMIN_CHAT_IDS`` or ``TELEGRAM_CHAT_ID``.

Commands
--------
/start, /help   - Show help text
/status         - Bot status (mode, broker connection, balance, DD)
/positions      - Open positions
/trades [n]     - Recent trade history (default 5, max 20)
/performance    - Performance summary (winrate, profit factor, PnL)
/account        - Current account configuration
/switch <env>   - Switch environment (paper/demo/live) with hot-reload
/setrisk <dd> <max_trades> - Update risk parameters at runtime

Architecture
------------
Uses ``python-telegram-bot`` (v21+) with async Application. Fetches data
from the internal dashboard API (http://localhost:{port}) via ``httpx``,
keeping the Telegram bot decoupled from engine internals.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    filters,
)

from app.utils.logging import get_logger

log = get_logger(__name__)


class TelegramBot:
    """Interactive Telegram command bot."""

    def __init__(
        self,
        token: str,
        admin_chat_ids: list[int],
        api_base_url: str = "http://localhost:8080",
    ) -> None:
        self._token = token
        self._admin_chat_ids = set(admin_chat_ids)
        self._api_base = api_base_url
        self._http = httpx.AsyncClient(
            base_url=api_base_url,
            timeout=httpx.Timeout(10.0),
        )
        self._app: Application | None = None

    # ==================================================================
    # Lifecycle
    # ==================================================================

    async def start(self) -> None:
        """Build and start the Telegram bot (non-blocking polling)."""
        builder = Application.builder().token(self._token)
        self._app = builder.build()

        # Register command handlers
        handlers = [
            CommandHandler("start", self._cmd_help),
            CommandHandler("help", self._cmd_help),
            CommandHandler("status", self._cmd_status),
            CommandHandler("positions", self._cmd_positions),
            CommandHandler("trades", self._cmd_trades),
            CommandHandler("performance", self._cmd_performance),
            CommandHandler("account", self._cmd_account),
            CommandHandler("switch", self._cmd_switch),
            CommandHandler("setrisk", self._cmd_setrisk),
        ]
        for h in handlers:
            self._app.add_handler(h)

        # Initialize and start polling
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)  # type: ignore[union-attr]
        log.info(
            "telegram bot started polling",
            extra={"admin_ids": list(self._admin_chat_ids)},
        )

    async def stop(self) -> None:
        """Gracefully stop the bot."""
        if self._app:
            if self._app.updater and self._app.updater.running:
                await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
        await self._http.aclose()
        log.info("telegram bot stopped")

    # ==================================================================
    # Auth guard
    # ==================================================================

    def _is_authorized(self, update: Update) -> bool:
        """Check if the message comes from an authorized chat ID."""
        if not update.effective_chat:
            return False
        return update.effective_chat.id in self._admin_chat_ids

    async def _guard(self, update: Update) -> bool:
        """Return True if authorized, else silently ignore."""
        if not self._is_authorized(update):
            log.warning(
                "unauthorized telegram access attempt",
                extra={"chat_id": update.effective_chat.id if update.effective_chat else "unknown"},
            )
            return False
        return True

    # ==================================================================
    # Commands
    # ==================================================================

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        text = (
            "🤖 *PropFirm Scalp Bot*\n\n"
            "Available commands:\n\n"
            "/status — Bot status (mode, connection, balance, DD)\n"
            "/positions — Open positions\n"
            "/trades \\[n\\] — Recent trades (default 5, max 20)\n"
            "/performance — Performance summary\n"
            "/account — Account configuration\n"
            "/switch <env> — Switch environment (paper/demo/live)\n"
            "/setrisk <daily\\_loss> <max\\_trades> — Update risk params\n"
            "/help — Show this message\n"
        )
        await update.message.reply_text(text, parse_mode="MarkdownV2")  # type: ignore[union-attr]

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        try:
            resp = await self._http.get("/api/v1/status")
            data = resp.json()
        except Exception as exc:
            await self._reply_error(update, f"Failed to fetch status: {exc}")
            return

        mode = data.get("mode", "unknown")
        ws_connected = data.get("ws_connected", False)
        balance = data.get("balance", 0)
        daily_dd = data.get("daily_drawdown_pct", 0)
        disabled = data.get("risk_disabled", False)
        trades_today = data.get("trades_today", 0)

        ws_emoji = "🟢" if ws_connected else "🔴"
        risk_emoji = "⛔" if disabled else "✅"

        text = (
            f"📊 *Bot Status*\n\n"
            f"Mode: `{mode}`\n"
            f"Broker WS: {ws_emoji} {'Connected' if ws_connected else 'Disconnected'}\n"
            f"Risk: {risk_emoji} {'DISABLED' if disabled else 'Active'}\n"
            f"Balance: `${balance:,.2f}`\n"
            f"Daily DD: `{daily_dd:.2%}`\n"
            f"Trades today: `{trades_today}`\n"
        )
        await update.message.reply_text(text, parse_mode="MarkdownV2")  # type: ignore[union-attr]

    async def _cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        try:
            resp = await self._http.get("/api/v1/trades/open")
            data = resp.json()
        except Exception as exc:
            await self._reply_error(update, f"Failed to fetch positions: {exc}")
            return

        positions = data.get("positions", [])
        if not positions:
            await update.message.reply_text("📭 No open positions\\.")  # type: ignore[union-attr]
            return

        lines = ["📈 *Open Positions*\n"]
        for pos in positions:
            direction_emoji = "🟢" if pos.get("direction") == "LONG" else "🔴"
            lines.append(
                f"{direction_emoji} `{pos.get('symbol', '?')}` "
                f"{pos.get('direction', '?')} @ `{pos.get('entry_price', '?')}`\n"
                f"   SL: `{pos.get('stop_loss', '?')}` \\| TP: `{pos.get('take_profit', '?')}`\n"
                f"   Lots: `{pos.get('quantity', '?')}`"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")  # type: ignore[union-attr]

    async def _cmd_trades(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return

        # Parse limit argument
        limit = 5
        if context.args:
            try:
                limit = min(int(context.args[0]), 20)
            except (ValueError, IndexError):
                limit = 5

        try:
            resp = await self._http.get("/api/v1/trades", params={"limit": limit})
            data = resp.json()
        except Exception as exc:
            await self._reply_error(update, f"Failed to fetch trades: {exc}")
            return

        trades = data.get("trades", [])
        if not trades:
            await update.message.reply_text("📭 No trades recorded yet\\.")  # type: ignore[union-attr]
            return

        lines = [f"📋 *Last {len(trades)} Trades*\n"]
        for t in trades:
            pnl = t.get("pnl") or 0
            pnl_emoji = "✅" if pnl > 0 else "❌" if pnl < 0 else "⚪"
            lines.append(
                f"{pnl_emoji} `{t.get('symbol')}` {t.get('direction')} "
                f"PnL: `${pnl:+.2f}` "
                f"\\({t.get('status', '?')}\\)"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")  # type: ignore[union-attr]

    async def _cmd_performance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        try:
            resp = await self._http.get("/api/v1/performance")
            data = resp.json()
        except Exception as exc:
            await self._reply_error(update, f"Failed to fetch performance: {exc}")
            return

        total = data.get("total_trades", 0)
        winrate = data.get("winrate", 0)
        profit_factor = data.get("profit_factor", 0)
        total_pnl = data.get("total_pnl", 0)
        max_dd = data.get("max_drawdown_pct", 0)
        avg_rr = data.get("avg_rr", 0)

        text = (
            f"📊 *Performance Summary*\n\n"
            f"Total trades: `{total}`\n"
            f"Winrate: `{winrate:.1%}`\n"
            f"Profit Factor: `{profit_factor:.2f}`\n"
            f"Total PnL: `${total_pnl:+,.2f}`\n"
            f"Max Drawdown: `{max_dd:.2%}`\n"
            f"Avg R:R: `{avg_rr:.2f}`\n"
        )
        await update.message.reply_text(text, parse_mode="MarkdownV2")  # type: ignore[union-attr]

    async def _cmd_account(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        try:
            resp = await self._http.get("/api/v1/settings/account")
            data = resp.json()
        except Exception as exc:
            await self._reply_error(update, f"Failed to fetch account: {exc}")
            return

        text = (
            f"⚙️ *Account Configuration*\n\n"
            f"Environment: `{data.get('environment', '?')}`\n"
            f"Mode: `{data.get('mode', '?')}`\n"
            f"Broker: `{data.get('broker', '?')}`\n"
            f"Account ID: `{data.get('account_id', '?')}`\n"
            f"Symbols: `{', '.join(data.get('symbols', []))}`\n"
        )
        await update.message.reply_text(text, parse_mode="MarkdownV2")  # type: ignore[union-attr]

    async def _cmd_switch(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return

        if not context.args:
            await update.message.reply_text(  # type: ignore[union-attr]
                "Usage: `/switch <paper|demo|live>`",
                parse_mode="MarkdownV2",
            )
            return

        environment = context.args[0].lower()
        if environment not in ("paper", "demo", "live"):
            await update.message.reply_text(  # type: ignore[union-attr]
                "❌ Invalid environment\\. Use: `paper`, `demo`, or `live`",
                parse_mode="MarkdownV2",
            )
            return

        await update.message.reply_text(  # type: ignore[union-attr]
            f"🔄 Switching to `{environment}`\\.\\.\\.",
            parse_mode="MarkdownV2",
        )

        try:
            resp = await self._http.put(
                "/api/v1/settings/account",
                json={"environment": environment},
            )
            data = resp.json()
            if resp.status_code == 200:
                await update.message.reply_text(  # type: ignore[union-attr]
                    f"✅ Switched to `{environment}` successfully\\!\n"
                    f"Mode: `{data.get('mode', '?')}`\n"
                    f"Broker reconnected: `{data.get('reconnected', False)}`",
                    parse_mode="MarkdownV2",
                )
            else:
                error = data.get("detail") or data.get("error") or "Unknown error"
                await update.message.reply_text(  # type: ignore[union-attr]
                    f"❌ Switch failed: `{error}`",
                    parse_mode="MarkdownV2",
                )
        except Exception as exc:
            await self._reply_error(update, f"Switch failed: {exc}")

    async def _cmd_setrisk(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return

        if not context.args or len(context.args) < 2:
            await update.message.reply_text(  # type: ignore[union-attr]
                "Usage: `/setrisk <daily_loss_pct> <max_trades_per_day>`\n"
                "Example: `/setrisk 0.01 5`  \\(1% DD, 5 trades\\)",
                parse_mode="MarkdownV2",
            )
            return

        try:
            daily_loss = float(context.args[0])
            max_trades = int(context.args[1])
        except (ValueError, IndexError):
            await update.message.reply_text(  # type: ignore[union-attr]
                "❌ Invalid values\\. Use numbers: `/setrisk 0.01 5`",
                parse_mode="MarkdownV2",
            )
            return

        # Validation
        if not (0.001 <= daily_loss <= 0.10):
            await update.message.reply_text(  # type: ignore[union-attr]
                "❌ daily\\_loss must be between 0\\.001 \\(0\\.1%\\) and 0\\.10 \\(10%\\)",
                parse_mode="MarkdownV2",
            )
            return
        if not (1 <= max_trades <= 50):
            await update.message.reply_text(  # type: ignore[union-attr]
                "❌ max\\_trades must be between 1 and 50",
                parse_mode="MarkdownV2",
            )
            return

        try:
            resp = await self._http.put(
                "/api/v1/settings/risk",
                json={
                    "max_daily_loss_pct": daily_loss,
                    "max_trades_per_day": max_trades,
                },
            )
            data = resp.json()
            if resp.status_code == 200:
                await update.message.reply_text(  # type: ignore[union-attr]
                    f"✅ Risk parameters updated\\!\n"
                    f"Daily loss limit: `{daily_loss:.2%}`\n"
                    f"Max trades/day: `{max_trades}`",
                    parse_mode="MarkdownV2",
                )
            else:
                error = data.get("detail") or data.get("error") or "Unknown error"
                await update.message.reply_text(  # type: ignore[union-attr]
                    f"❌ Update failed: `{error}`",
                    parse_mode="MarkdownV2",
                )
        except Exception as exc:
            await self._reply_error(update, f"Update failed: {exc}")

    # ==================================================================
    # Helpers
    # ==================================================================

    async def _reply_error(self, update: Update, message: str) -> None:
        """Send a plain-text error message."""
        if update.message:
            await update.message.reply_text(f"❌ {message}")
        log.warning("telegram command error", extra={"error": message})
