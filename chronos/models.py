"""Typed, immutable market observations used by the research engine."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Candle:
    start_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class MarketSnapshot:
    symbol: str
    observed_ms: int
    price: float
    funding_rate: float
    next_funding_ms: int
    turnover_24h: float
    open_interest: tuple[tuple[int, float], ...]
    funding_history: tuple[float, ...]
    candles: tuple[Candle, ...]
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    ticker_open_interest: float = 0.0


@dataclass(frozen=True)
class Assessment:
    symbol: str
    observed_ms: int
    verdict: str
    reason: str
    score: float = 0.0
    metrics: dict[str, Any] = field(default_factory=dict)
    side: str = ""
    entry: float = 0.0
    stop: float = 0.0
    target: float = 0.0
    gross_rr: float = 0.0
    net_rr: float = 0.0
    next_funding_ms: int = 0
    valid_until_ms: int = 0

    @property
    def actionable(self) -> bool:
        """Only strict signals; never SETUP/WATCH."""
        return self.verdict == "SIGNAL" and self.side in {"LONG", "SHORT"}

    @property
    def has_plan(self) -> bool:
        return (self.verdict in {"SIGNAL", "SETUP", "WATCH"}
                and self.side in {"LONG", "SHORT"}
                and self.entry > 0 and self.stop > 0 and self.target > 0)
