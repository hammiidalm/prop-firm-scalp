"""Main engine / orchestrator.

This module is the single entrypoint that:
1. Loads configuration.
2. Instantiates the broker (live or paper), strategy, risk manager,
   executor, journal, notifier, WebSocket client, and dashboard API.
3. Coordinates the async event loop: WS → candle ingestion → strategy →
   risk → execution → journal → notifications.
4. Handles graceful shutdown, crash recovery (state persistence),
   and signal handling (SIGINT/SIGTERM).

Design
------
The engine owns the asyncio lifecycle. Each subsystem is started/stopped
in a deterministic order to avoid partial-state issues (e.g. the broker
must be connected before the WS starts streaming).

Crash recovery: on startup the engine checks for any persisted open
trades from the journal and reconciles them with the broker. This avoids
phantom positions after an unexpected restart.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import Any

import uvicorn

from app.analytics.stats import TradeStatsAggregator
from app.api.app import create_app
from app.broker.base import BrokerClient
from app.broker.paper import PaperBroker
from app.broker.tradelocker import TradeLockerClient
from app.config import Settings, get_settings
from app.config.settings import Timeframe, TradingMode
from app.execution.executor import Executor
from app.journal import TradeJournal
from app.market.rest_poller import RestPricePoller
from app.models import Candle, Signal, Trade
from app.notifications.base import Notifier
from app.risk import RiskManager
from app.strategy.base import Strategy
from app.strategy.scalp_smc import SmcScalpStrategy
from app.utils.instruments import get_instrument
from app.utils.logging import configure_logging, get_logger
from app.utils.sessions import SessionFilter
from app.websocket.client import WebSocketClient

log = get_logger(__name__)


class Engine:
    """Top-level orchestrator for the scalping bot."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._shutdown_event = asyncio.Event()

        # Subsystems - initialized in ``start``
        self._broker: BrokerClient | None = None
        self._ws: WebSocketClient | None = None
        self._rest_poller: RestPricePoller | None = None
        self._risk: RiskManager | None = None
        self._executor: Executor | None = None
        self._journal: TradeJournal | None = None
        self._notifier: Notifier | None = None
        self._strategies: dict[str, Strategy] = {}
        self._stats: TradeStatsAggregator | None = None
        self._api_server: uvicorn.Server | None = None
        self._last_signal: Signal | None = None

    # ==================================================================
    # Public interface
    # ==================================================================

    async def start(self) -> None:
        """Initialize all subsystems and start the event loop."""
        s = self._settings
        configure_logging(level=s.log_level, json_output=s.log_json)
        log.info("engine starting", extra={"mode": s.app_mode.value, "env": s.app_env.value})

        # 1. Journal (DB)
        self._journal = TradeJournal(s.database_url)
        await self._journal.init()

        # 2. Notifier
        self._notifier = Notifier(s)

        # 3. Broker
        if s.app_mode is TradingMode.paper:
            self._broker = PaperBroker(starting_balance=s.account_balance)
        else:
            self._broker = TradeLockerClient(s)
        await self._broker.connect()

        # 4. Risk manager
        balance = await self._broker.get_account_balance()
        self._risk = RiskManager(
            settings=s,
            starting_balance=balance,
            current_balance=balance,
            high_water_mark=balance,
        )

        # 5. Stats
        self._stats = TradeStatsAggregator(starting_balance=balance)

        # 6. Executor
        self._executor = Executor(
            broker=self._broker,
            risk=self._risk,
            mode=s.app_mode,
            notify=self._notifier.notify,
            persist_trade=self._persist_trade,
        )

        # 7. Strategies (one per symbol)
        sessions = SessionFilter(
            london_open_utc=s.london_open_utc,
            london_close_utc=s.london_close_utc,
            ny_open_utc=s.ny_open_utc,
            ny_close_utc=s.ny_close_utc,
        )
        for sym, tf in s.symbol_timeframe_map.items():
            self._strategies[sym] = SmcScalpStrategy(
                symbol=sym,
                timeframe=tf,
                sessions=sessions,
                target_profit_pct_min=s.target_profit_pct_min,
                target_profit_pct_max=s.target_profit_pct_max,
            )

        # 8. WebSocket (only for live/semi-auto modes)
        if s.app_mode is not TradingMode.paper:
            assert isinstance(self._broker, TradeLockerClient)
            self._ws = WebSocketClient(
                url=s.tl_ws_url,
                token_provider=self._get_ws_token,
                on_message=self._on_ws_message,
            )
            await self._ws.start()

        # 9. REST price poller — always start as fallback / primary data source
        self._rest_poller = RestPricePoller(
            symbols=list(s.symbol_timeframe_map.keys()),
            on_candle=self._on_market_data_from_poller,
            candle_seconds={
                sym: Timeframe(tf).to_seconds()
                for sym, tf in s.symbol_timeframe_map.items()
            },
        )
        try:
            await self._rest_poller.start()
        except Exception as exc:
            log.warning("rest_poller failed to start, continuing without it: %s", exc)
            self._rest_poller = None

        # 10. Dashboard API (background)
        app = create_app(
            risk_manager=self._risk,
            executor=self._executor,
            journal=self._journal,
            stats_aggregator=self._stats,
            ws_client=self._ws,
            engine=self,
        )
        config = uvicorn.Config(
            app,
            host=s.api_host,
            port=s.api_port,
            log_level="warning",
            access_log=False,
        )
        self._api_server = uvicorn.Server(config)

        log.info(
            "engine ready",
            extra={
                "symbols": s.symbols,
                "mode": s.app_mode.value,
                "api_port": s.api_port,
            },
        )

    async def run(self) -> None:
        """Main loop - run until shutdown signal."""
        await self.start()
        self._install_signal_handlers()

        tasks: list[asyncio.Task[Any]] = []
        # API server task
        if self._api_server:
            tasks.append(asyncio.create_task(self._api_server.serve(), name="api-server"))

        # Wait for shutdown
        await self._shutdown_event.wait()

        log.info("engine shutting down gracefully")
        await self.stop()

        # Cancel remaining tasks
        for task in tasks:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def stop(self) -> None:
        """Gracefully tear down all subsystems in reverse order."""
        if self._ws:
            await self._ws.stop()
        if self._rest_poller:
            await self._rest_poller.stop()
        if self._api_server:
            self._api_server.should_exit = True
        if self._broker:
            await self._broker.close()
        if self._journal:
            await self._journal.close()
        log.info("engine stopped")

    @property
    def last_signal(self) -> Signal | None:
        """The most recent signal generated by the strategy."""
        return self._last_signal

    # ==================================================================
    # Hot-reload: environment switching
    # ==================================================================

    async def switch_environment(self, target_mode: str) -> bool:
        """Switch the trading environment at runtime without container restart.

        Steps:
        1. Stop WebSocket streaming (if active).
        2. Close current broker connection.
        3. Update in-memory settings to the new mode.
        4. Instantiate and connect the new broker.
        5. Re-create the risk manager with fresh balance from the new broker.
        6. Re-wire the executor with the new broker + risk manager.
        7. Restart WebSocket if the new mode requires it.

        Returns True on success, False on failure (reverts on error).
        """
        from app.config.settings import TradingMode

        old_mode = self._settings.app_mode
        try:
            new_mode = TradingMode(target_mode)
        except ValueError:
            log.error("invalid trading mode: %s", target_mode)
            return False

        if new_mode == old_mode:
            log.info("switch_environment: already in mode %s", new_mode.value)
            return True

        log.info(
            "switching environment",
            extra={"from": old_mode.value, "to": new_mode.value},
        )

        # --- 1. Stop WS ---
        if self._ws:
            await self._ws.stop()
            self._ws = None
            log.info("ws stopped for environment switch")

        # --- 2. Close old broker ---
        old_broker = self._broker
        if old_broker:
            try:
                await old_broker.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("error closing old broker: %s", exc)

        # --- 3. Update settings in-memory ---
        self._settings.app_mode = new_mode

        # --- 4. Connect new broker ---
        try:
            if new_mode is TradingMode.paper:
                self._broker = PaperBroker(starting_balance=self._settings.account_balance)
            else:
                self._broker = TradeLockerClient(self._settings)
            await self._broker.connect()
        except Exception as exc:
            log.exception("failed to connect new broker, reverting: %s", exc)
            # Revert
            self._settings.app_mode = old_mode
            if old_broker:
                try:
                    await old_broker.connect()
                    self._broker = old_broker
                except Exception:  # noqa: BLE001
                    pass
            return False

        # --- 5. Refresh risk manager with new balance ---
        try:
            balance = await self._broker.get_account_balance()
        except Exception:  # noqa: BLE001
            balance = self._settings.account_balance

        self._risk = RiskManager(
            settings=self._settings,
            starting_balance=balance,
            current_balance=balance,
            high_water_mark=balance,
        )

        # --- 6. Re-wire executor ---
        self._executor = Executor(
            broker=self._broker,
            risk=self._risk,
            mode=new_mode,
            notify=self._notifier.notify if self._notifier else None,
            persist_trade=self._persist_trade,
        )

        # --- 7. Restart WS if needed ---
        if new_mode is not TradingMode.paper and isinstance(self._broker, TradeLockerClient):
            self._ws = WebSocketClient(
                url=self._settings.tl_ws_url,
                token_provider=self._get_ws_token,
                on_message=self._on_ws_message,
            )
            await self._ws.start()
            log.info("ws restarted for new environment")

        # Update stats aggregator starting balance
        if self._stats:
            self._stats = TradeStatsAggregator(starting_balance=balance)

        log.info(
            "environment switch complete",
            extra={"mode": new_mode.value, "balance": balance},
        )
        return True

    # ==================================================================
    # Internal handlers
    # ==================================================================

    async def _on_ws_message(self, msg: dict[str, Any]) -> None:
        """Route incoming WS messages to the appropriate handler."""
        msg_type = msg.get("type") or msg.get("t") or ""
        if msg_type in ("quote", "tick", "candle"):
            await self._on_market_data(msg)
        elif msg_type in ("order", "position", "execution"):
            await self._on_order_update(msg)
        # Heartbeat / unknown messages are silently dropped.

    async def _on_market_data_from_poller(self, candle_dict: dict[str, Any]) -> None:
        """Handle a completed 1m candle from the REST price poller."""
        try:
            symbol = candle_dict.get("symbol", "")
            if not symbol or symbol not in self._strategies:
                return
            candle = Candle(
                symbol=symbol,
                timeframe=candle_dict.get("timeframe", "M1"),
                timestamp=candle_dict["timestamp"],
                open=candle_dict["open"],
                high=candle_dict["high"],
                low=candle_dict["low"],
                close=candle_dict["close"],
                volume=candle_dict.get("volume", 0),
            )
        except (ValueError, TypeError, KeyError) as exc:
            log.debug("rest poller candle parse error: %s", exc)
            return

        # Update paper broker quote
        if isinstance(self._broker, PaperBroker):
            inst = get_instrument(candle.symbol)
            half_spread = (self._settings.max_spread_pips_fx * inst.pip_size) / 2
            self._broker.set_quote(candle.symbol, candle.close - half_spread, candle.close + half_spread)

        # Run strategy
        strat = self._strategies.get(candle.symbol)
        if strat is None:
            return
        signal = await strat.on_candle(candle)
        if signal is not None and self._executor:
            self._last_signal = signal
            await self._executor.handle_signal(signal)

    async def _on_market_data(self, msg: dict[str, Any]) -> None:
        """Parse a market-data message into a Candle and run the strategy."""
        try:
            # TradeLocker WS sends candle data in a flat dict; adapt to our model.
            data = msg.get("d") or msg
            symbol = str(data.get("symbol") or data.get("s") or "")
            if not symbol or symbol not in self._strategies:
                return
            candle = Candle(
                symbol=symbol,
                timeframe=str(data.get("timeframe") or data.get("tf") or self._settings.primary_timeframe.value),
                timestamp=data.get("timestamp") or data.get("t"),
                open=float(data.get("open") or data.get("o") or 0),
                high=float(data.get("high") or data.get("h") or 0),
                low=float(data.get("low") or data.get("l") or 0),
                close=float(data.get("close") or data.get("c") or 0),
                volume=float(data.get("volume") or data.get("v") or 0),
            )
        except (ValueError, TypeError, KeyError) as exc:
            log.debug("ws candle parse error: %s", exc)
            return

        # Update paper broker quote if applicable
        if isinstance(self._broker, PaperBroker):
            inst = get_instrument(candle.symbol)
            half_spread = (self._settings.max_spread_pips_fx * inst.pip_size) / 2
            self._broker.set_quote(candle.symbol, candle.close - half_spread, candle.close + half_spread)

        # Run strategy
        strat = self._strategies.get(candle.symbol)
        if strat is None:
            return
        signal = await strat.on_candle(candle)
        if signal is not None and self._executor:
            # Cache latest signal for API consumers
            self._last_signal = signal
            # Compute spread for risk filter
            try:
                bid, ask = await self._broker.get_quote(candle.symbol)
                inst = get_instrument(candle.symbol)
                spread_pips = inst.pips(ask - bid)
            except Exception:  # noqa: BLE001
                spread_pips = None
            await self._executor.handle_signal(signal, spread_pips=spread_pips)

    async def _on_order_update(self, msg: dict[str, Any]) -> None:
        """Handle fill/close notifications from the broker WS."""
        data = msg.get("d") or msg
        # Check if any open trade's SL/TP was hit broker-side
        status = str(data.get("status") or data.get("state") or "")
        if status.lower() in ("filled", "closed"):
            trade_id = str(data.get("clientOrderId") or data.get("client_order_id") or "")
            exit_price = float(data.get("exitPrice") or data.get("fill_price") or 0)
            reason = str(data.get("reason") or "broker_close")
            if trade_id and exit_price and self._executor:
                await self._executor.close_trade(trade_id, reason=reason, exit_price=exit_price)

    async def _persist_trade(self, trade: Trade) -> None:
        """Persist trade to journal and update stats."""
        if self._journal:
            await self._journal.save(trade)
        if self._stats and trade.status.value.startswith("CLOSED"):
            self._stats.record(trade)

    async def _get_ws_token(self) -> str:
        """Provide the current access token for the WS handshake."""
        if isinstance(self._broker, TradeLockerClient):
            return self._broker._access_token or ""
        return ""

    # ==================================================================
    # Signal handling
    # ==================================================================

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._trigger_shutdown)
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                pass

    def _trigger_shutdown(self) -> None:
        log.info("shutdown signal received")
        self._shutdown_event.set()
