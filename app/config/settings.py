"""Typed application settings.

All runtime configuration flows through a single Pydantic ``Settings`` model
loaded from environment variables and ``.env``. The model is cached via
``get_settings`` so the rest of the codebase can import it without re-parsing.

Design notes:
* Defaults are conservative and prop-firm-safe (low risk, tight DD).
* Secrets must come from the environment - never hard-coded.
* Numerical limits are expressed as fractions (0.01 = 1 percent), never as
  percentages, to avoid the classic 100x bug.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnv(str, Enum):
    development = "development"
    staging = "staging"
    production = "production"


class TradingMode(str, Enum):
    paper = "paper"
    semi_auto = "semi_auto"
    full_auto = "full_auto"


class Timeframe(str, Enum):
    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    M30 = "M30"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"

    def to_seconds(self) -> int:
        """Convert Timeframe to seconds."""
        m = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800,
             "H1": 3600, "H4": 14400, "D1": 86400}
        return m[self.value]


Pct = Annotated[float, Field(ge=0.0, le=1.0)]


class Settings(BaseSettings):
    """Single source of truth for runtime configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- runtime ----------------------------------------------------------
    app_env: AppEnv = AppEnv.production
    app_mode: TradingMode = TradingMode.paper
    log_level: str = "INFO"
    log_json: bool = True
    timezone: str = "UTC"

    # ---- broker -----------------------------------------------------------
    tl_base_url: str = "https://demo.tradelocker.com/backend-api"
    tl_ws_url: str = "wss://demo.tradelocker.com/backend-api/ws"
    tl_email: SecretStr = SecretStr("")
    tl_password: SecretStr = SecretStr("")
    tl_server: str = ""
    tl_account_id: str = ""
    tl_account_num: str = ""

    # ---- symbols / timeframes --------------------------------------------
    symbols: list[str] = Field(default_factory=lambda: ["EURUSD", "XAUUSD"])
    primary_timeframe: Timeframe = Timeframe.M1
    structure_timeframe: Timeframe = Timeframe.M5

    # ---- risk -------------------------------------------------------------
    account_balance: float = 100_000.0
    risk_per_trade_pct: Pct = 0.01
    max_daily_loss_pct: Pct = 0.03
    max_total_dd_pct: Pct = 0.05
    max_trades_per_day: int = Field(default=5, ge=1, le=50)
    max_consecutive_losses: int = Field(default=3, ge=1, le=10)
    target_profit_pct_min: Pct = 0.001
    target_profit_pct_max: Pct = 0.002
    max_spread_pips_fx: float = 1.5
    max_spread_pips_metals: float = 35.0
    max_slippage_pips: float = 2.0
    min_trading_days: int = Field(default=7, ge=1, le=365)
    consistency_pct: Pct = 0.15

    # ---- sessions (UTC) ---------------------------------------------------
    london_open_utc: int = Field(default=7, ge=0, le=23)
    london_close_utc: int = Field(default=11, ge=0, le=23)
    ny_open_utc: int = Field(default=12, ge=0, le=23)
    ny_close_utc: int = Field(default=16, ge=0, le=23)

    # ---- persistence ------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./data/journal.db"
    redis_url: str | None = None

    # ---- api --------------------------------------------------------------
    api_host: str = "0.0.0.0"
    api_port: int = 8080

    # ---- notifications ---------------------------------------------------
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""
    discord_webhook_url: SecretStr = SecretStr("")

    # ---- telegram bot (interactive) --------------------------------------
    admin_chat_ids: list[int] = Field(default_factory=list)

    @field_validator("admin_chat_ids", mode="before")
    @classmethod
    def _split_admin_ids(cls, v: object) -> list[int]:
        """Parse comma-separated chat IDs from env (e.g. '123,456').

        Falls back to ``telegram_chat_id`` if empty (handled in
        ``get_admin_chat_ids``).
        """
        if isinstance(v, str):
            if not v.strip():
                return []
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        if isinstance(v, list):
            return [int(x) for x in v]
        if isinstance(v, int):
            return [v]
        return []

    # ---- validators ------------------------------------------------------
    @field_validator("symbols", mode="before")
    @classmethod
    def _split_symbols(cls, v: object) -> list[str]:
        if isinstance(v, str):
            return [s.strip().upper() for s in v.split(",") if s.strip()]
        if isinstance(v, list):
            return [str(s).strip().upper() for s in v if str(s).strip()]
        raise TypeError("symbols must be str or list[str]")

    @field_validator("target_profit_pct_max")
    @classmethod
    def _tp_max_gt_min(cls, v: float, info: object) -> float:  # noqa: ANN401
        # info.data is the partially-validated model dict
        data = getattr(info, "data", {}) or {}
        tp_min = data.get("target_profit_pct_min", 0.0)
        if v < tp_min:
            raise ValueError("target_profit_pct_max must be >= target_profit_pct_min")
        return v

    # ---- convenience -----------------------------------------------------
    @property
    def symbol_timeframe_map(self) -> dict[str, str]:
        """Parse symbols list into dict[symbol -> timeframe].

        Entries can be "SYMBOL:TF" (e.g. "EURUSD:M1") or just "SYMBOL"
        (defaults to primary_timeframe for backward compat).
        """
        result: dict[str, str] = {}
        for entry in self.symbols:
            if ":" in entry:
                sym, tf = entry.split(":", 1)
                result[sym.strip()] = tf.strip()
            else:
                result[entry] = self.primary_timeframe.value
        return result

    def risk_per_trade_amount(self) -> float:
        """Absolute risk in account currency for the configured balance."""
        return self.account_balance * self.risk_per_trade_pct

    def daily_loss_limit_amount(self) -> float:
        return self.account_balance * self.max_daily_loss_pct

    def is_telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token.get_secret_value() and self.telegram_chat_id)

    def is_discord_enabled(self) -> bool:
        return bool(self.discord_webhook_url.get_secret_value())

    def get_admin_chat_ids(self) -> list[int]:
        """Return the resolved list of authorized Telegram chat IDs.

        Priority: ``admin_chat_ids`` env var. If empty, falls back to
        ``telegram_chat_id`` (the single notification recipient).
        """
        if self.admin_chat_ids:
            return self.admin_chat_ids
        if self.telegram_chat_id:
            try:
                return [int(self.telegram_chat_id)]
            except ValueError:
                return []
        return []

    def is_telegram_bot_enabled(self) -> bool:
        """True if we have both a token and at least one authorized chat ID."""
        return bool(
            self.telegram_bot_token.get_secret_value() and self.get_admin_chat_ids()
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide singleton settings instance."""
    return Settings()
