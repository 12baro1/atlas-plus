"""Environment-driven safe defaults. No private exchange endpoints or trading."""
from __future__ import annotations
from dataclasses import dataclass
import os


def _float_env(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        raise ValueError(f"Invalid numeric environment variable: {key}") from None


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except (TypeError, ValueError):
        raise ValueError(f"Invalid integer environment variable: {key}") from None


@dataclass(frozen=True)
class Settings:
    base_url: str = "https://api.bybit.com"
    database: str = "chronos.sqlite3"
    scan_limit: int = 20
    interval_seconds: int = 900
    workers: int = 3
    timeout_seconds: float = 9.0
    min_turnover: float = 20_000_000.0
    min_depth_usdt: float = 75_000.0
    min_abs_funding: float = 0.00008
    min_oi_growth: float = 0.03
    min_pressure_score: float = 72.0
    min_depth_ratio: float = 0.65
    min_net_rr: float = 1.8
    assumed_unwind_fraction: float = 0.03
    max_spread: float = 0.0015
    taker_fee: float = 0.00055
    slippage: float = 0.0003
    max_signal_age_minutes: int = 90
    max_entry_deviation: float = 0.003
    telegram_token: str = ""
    telegram_chat_id: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls(
            base_url=os.getenv("CHRONOS_BYBIT_BASE_URL", "https://api.bybit.com").rstrip("/"),
            database=os.getenv("CHRONOS_DB", "chronos.sqlite3"),
            scan_limit=_int_env("CHRONOS_SCAN_LIMIT", 20),
            interval_seconds=_int_env("CHRONOS_INTERVAL_SECONDS", 900),
            workers=_int_env("CHRONOS_WORKERS", 3),
            timeout_seconds=_float_env("CHRONOS_TIMEOUT_SECONDS", 9.0),
            min_turnover=_float_env("CHRONOS_MIN_TURNOVER", 20_000_000.0),
            telegram_token=os.getenv("CHRONOS_TELEGRAM_TOKEN", ""),
            telegram_chat_id=os.getenv("CHRONOS_TELEGRAM_CHAT_ID", ""),
        )
        if not s.base_url.startswith("https://"):
            raise ValueError("CHRONOS_BYBIT_BASE_URL must be HTTPS")
        if not 1 <= s.scan_limit <= 100 or not 1 <= s.workers <= 8:
            raise ValueError("Scan limit must be 1..100 and workers 1..8")
        if s.interval_seconds < 60 or s.timeout_seconds <= 0 or s.min_turnover < 0:
            raise ValueError("Invalid scan interval, timeout or turnover")
        return s
