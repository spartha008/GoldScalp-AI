"""Settings from environment variables (prefix GOLDSCALP_) or a local .env file.

Strict: a misspelt GOLDSCALP_* variable is an error, not silently ignored.
Secrets (the optional Tiingo API key) are read only on the server and are
never sent to the browser or written to logs.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GOLDSCALP_", env_file=str(PROJECT_ROOT / ".env"), env_file_encoding="utf-8",
                                      extra="forbid", case_sensitive=False)

    # Server
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    monthly_port: int = Field(default=8001, ge=1, le=65535)   # Phase 5 research service (python -m goldscalp.monthly)
    db_path: Path = PROJECT_ROOT / "var" / "goldscalp.sqlite3"
    max_import_mb: int = Field(default=50, ge=1, le=500)

    # Instrument
    instrument_spec: Path = PROJECT_ROOT / "config" / "instruments" / "xauusd_demo.json"

    # Live market data. auto = first AVAILABLE provider in priority order: mt5 > tiingo > gold-api > none.
    live_provider: Literal["auto", "mt5", "tiingo", "gold-api", "none"] = "auto"
    # manual = never switch providers on failure (show DISCONNECTED); auto = fall back after the delay below
    # and return to the preferred provider when it is healthy. Every switch is shown on the dashboard.
    provider_fallback: Literal["manual", "auto"] = "manual"
    provider_fallback_after_seconds: int = Field(default=30, ge=5, le=3600)
    # Timezone used to DISPLAY times in the browser (all storage and calculations stay in UTC).
    display_timezone: str = "UTC"

    # MetaTrader 5: OPTIONAL, Windows only, READ-ONLY market data from your already logged-in terminal.
    # There is deliberately no login/password/server setting: GoldScalp never handles MT5 credentials.
    mt5_enabled: bool = True
    mt5_symbol: str = ""                       # empty = detect the broker's gold symbol automatically
    mt5_time_basis: str = "auto"               # auto | utc | fixed:+HH:MM | rule:ny_close | tz:<IANA zone>
    mt5_poll_interval_seconds: float = Field(default=1.0, ge=0.25, le=60)
    mt5_stale_after_seconds: int = Field(default=10, ge=2, le=600)
    mt5_terminal_path: str = ""                # optional: full path to terminal64.exe if several are installed
    mt5_require_running_terminal: bool = True  # never let GoldScalp start the terminal itself
    mt5_select_symbol: bool = True             # add a hidden gold symbol to Market Watch so ticks arrive
    mt5_init_timeout_ms: int = Field(default=10000, ge=1000, le=120000)
    # Phase 4: read ONLY the 'Server=' line of the terminal's own config/common.ini to tell a demo server from a
    # live one (account functions stay forbidden). false = environment shown as UNKNOWN.
    mt5_detect_environment: bool = True
    # Bars downloaded per timeframe when the MT5 provider (re)starts. Deeper history feeds research (Phase 5).
    mt5_backfill_bars_m1: int = Field(default=3000, ge=100, le=200000)
    mt5_backfill_bars_m5: int = Field(default=3000, ge=100, le=200000)
    mt5_backfill_bars_m15: int = Field(default=2000, ge=100, le=200000)
    mt5_backfill_bars_h1: int = Field(default=2000, ge=100, le=200000)

    tiingo_api_key: SecretStr | None = None
    tiingo_ticker: str = "xauusd"
    tiingo_threshold_level: str = "5"
    tiingo_monthly_stream_mb: int = Field(default=700, ge=10, le=100000)
    tiingo_requests_per_hour: int = Field(default=40, ge=1, le=50)
    tiingo_requests_per_day: int = Field(default=800, ge=1, le=1000)
    live_window_seconds: int = Field(default=15, ge=2, le=600)
    disconnect_after_seconds: int = Field(default=180, ge=30, le=3600)

    # Paper-trading demonstration defaults (all explicit; change them freely)
    paper_account_name: str = "Demo paper account"
    paper_starting_balance: str = "10000"
    paper_leverage: int = Field(default=1, ge=1, le=1000)
    paper_spread_mode: Literal["market", "configured", "market_else_configured"] = "market_else_configured"
    paper_configured_spread: str = "0.30"
    paper_commission_per_lot_per_side: str = "3.50"
    paper_slippage: str = "0.05"
    paper_max_quote_age_seconds: int = Field(default=90, ge=1, le=3600)
    paper_allow_delayed_fills: bool = True

    # Phase 4 live strategy evaluation (SIMULATED validation through the paper simulator only)
    live_monitor_enabled: bool = True
    live_max_quote_age_seconds: int = Field(default=15, ge=2, le=600)   # signals/validation fills need a fresher quote
    live_lookback_days: int = Field(default=5, ge=1, le=20)             # history for the as-of snapshot (= research default)

    # Optional MT5 DEMO-account execution (off by default). When true, a monitored strategy version that YOU link on
    # the Live page has each of its SIMULATED validation trades mirrored as a real order on the MT5 account your
    # terminal is logged in to - only if that account reports itself as a DEMO account (checked before every order).
    # GoldScalp still never handles MT5 credentials. Real-money accounts are always refused.
    demo_execution_enabled: bool = False
    demo_execution_max_lots: str = "0.10"                 # largest volume per demo order
    demo_execution_max_positions: int = Field(default=3, ge=1, le=20)   # open GoldScalp demo positions at once
    demo_execution_max_entry_delay_seconds: int = Field(default=90, ge=5, le=600)   # never enter later than this
    demo_execution_deviation_points: int = Field(default=30, ge=0, le=1000)          # max slippage accepted (points)
    demo_execution_magic: int = Field(default=26111400, ge=1, le=2_147_483_647)     # tags GoldScalp's demo orders

    # Phase 4 research assistant. Deterministic and local by default. The optional Claude mode sends excerpts of
    # STORED GoldScalp data (signals, simulated trades, metrics) to Anthropic: it needs BOTH settings below and the
    # 'anthropic' package (pip install -r requirements-ai.txt).
    assistant_llm_enabled: bool = False
    assistant_api_key: SecretStr | None = None    # empty = the SDK's own credential resolution (ANTHROPIC_API_KEY ...)
    assistant_model: str = Field(default="claude-opus-5-5", pattern=r"^claude-[a-z0-9-]{1,60}$")

    # Optional TradingView Advanced Chart widget: DISPLAY ONLY, loaded in the browser on request.
    # Its data never reaches the backend, analysis, alerts or the simulator.
    tradingview_widget: bool = True
    tradingview_symbol: str = Field(default="OANDA:XAUUSD", pattern=r"^[A-Z0-9_]{1,20}:[A-Z0-9_.]{1,20}$")
    # true = frame the widget with sandbox (opaque origin, strongest isolation; not verified in every browser)
    tradingview_sandbox: bool = False

    # Optional bearer token for non-local exposure (not needed on 127.0.0.1)
    auth_token: SecretStr | None = None

    @field_validator("tiingo_api_key", "auth_token", "assistant_api_key", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("mt5_time_basis")
    @classmethod
    def _check_time_basis(cls, value: str) -> str:
        value = value.strip()
        if value in ("auto", "utc", "rule:ny_close") or re.fullmatch(r"fixed:[+-]\d{2}:\d{2}", value):
            return value
        if value.startswith("tz:"):
            ZoneInfo(value[3:])
            return value
        raise ValueError("mt5_time_basis must be auto, utc, fixed:+HH:MM, rule:ny_close or tz:<IANA zone>")

    @field_validator("display_timezone")
    @classmethod
    def _check_zone(cls, value: str) -> str:
        ZoneInfo(value.strip())   # raises for an unknown zone
        return value.strip()

    @field_validator("mt5_symbol")
    @classmethod
    def _check_symbol(cls, value: str) -> str:
        value = value.strip()
        if value and not re.fullmatch(r"[A-Za-z0-9._#!+\-]{1,32}", value):
            raise ValueError("mt5_symbol must be 1-32 characters: letters, digits and . _ # ! + -")
        return value

    def resolved_provider(self) -> str:
        """The configured PREFERENCE (availability at run time is decided by the provider hub)."""
        if self.live_provider == "auto":
            if self.mt5_enabled:
                return "mt5"
            return "tiingo" if self.tiingo_api_key is not None else "gold-api"
        return self.live_provider


def load_settings(**overrides: object) -> Settings:
    known = {f"GOLDSCALP_{name.upper()}" for name in Settings.model_fields}
    unknown = sorted(k for k in os.environ if k.upper().startswith("GOLDSCALP_") and k.upper() not in known)
    if unknown:
        raise ValueError(f"unknown GoldScalp setting(s): {', '.join(unknown)}; see .env.example")
    return Settings(**overrides)
