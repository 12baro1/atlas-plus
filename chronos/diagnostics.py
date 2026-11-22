"""Independent, observational gate checks for CHRONOS research.

These checks MUST NOT change the strategy verdict, thresholds or execution.
They show which independent conditions fail even if the main decision chain
already returned WAIT. Missing/unreliable inputs are NOT counted as PASS.
"""
from __future__ import annotations

import math
from .models import MarketSnapshot
from .settings import Settings


def inspect_gates(s: MarketSnapshot, cfg: Settings) -> dict:
    checks: dict[str, str] = {}
    metrics: dict[str, float] = {}

    def record(name: str, ok: bool) -> None:
        checks[name] = "PASS" if ok else "FAIL"

    if math.isfinite(s.turnover_24h):
        record("TURNOVER", s.turnover_24h >= cfg.min_turnover)

    if math.isfinite(s.funding_rate):
        record("FUNDING", abs(s.funding_rate) >= cfg.min_abs_funding)
        metrics["funding_rate"] = s.funding_rate

    if s.next_funding_ms > 0 and s.observed_ms > 0:
        minutes = (s.next_funding_ms - s.observed_ms) / 60_000
        if math.isfinite(minutes):
            record("FUNDING_WINDOW", 0 <= minutes <= 180)
            metrics["minutes_to_funding"] = round(minutes, 1)

    if s.open_interest:
        oi = sorted((t, v) for t, v in s.open_interest
                    if isinstance(v, (int, float)) and math.isfinite(v) and v > 0)
        if oi:
            latest_time, latest = oi[-1]
            old = [value for ts, value in oi if ts <= latest_time - 3_600_000]
            if old:
                growth = (latest - old[-1]) / old[-1]
                if math.isfinite(growth):
                    record("OI_60M", growth >= cfg.min_oi_growth)
                    metrics["oi_growth_60m"] = round(growth, 6)

    if s.bids and s.asks and math.isfinite(s.funding_rate):
        try:
            bid = max(p for p, _ in s.bids if p > 0)
            ask = min(p for p, _ in s.asks if p > 0)
            mid = (bid + ask) / 2
            if 0 < bid < ask and math.isfinite(mid) and mid > 0:
                spread = (ask - bid) / mid
                valid_spread = (spread <= cfg.max_spread
                                and math.isfinite(s.price) and s.price > 0
                                and abs(s.price-mid)/mid <= 0.01)
                record("SPREAD", valid_spread)
                metrics["spread"] = round(spread, 6)
                # Positive funding hypothesizes LONG unwind (selling into bids).
                exit_book = s.bids if s.funding_rate > 0 else s.asks
                depth = sum(p*q for p, q in exit_book
                            if p > 0 and q > 0 and
                            (p >= mid*0.995 if s.funding_rate > 0
                             else p <= mid*1.005))
                if math.isfinite(depth):
                    record("BOOK_DEPTH", depth >= cfg.min_depth_usdt)
                    metrics["book_exit_depth_usdt"] = round(depth, 2)
                    growth = metrics.get("oi_growth_60m")
                    if growth is not None and s.open_interest and depth > 0:
                        # Use the SAME observed OI baseline as the decision model.
                        oi_now = max(s.open_interest, key=lambda row: row[0])[1]
                        if math.isfinite(oi_now) and oi_now > 0:
                            delta_quote = oi_now * growth / (1+growth) * mid
                            ratio = delta_quote * cfg.assumed_unwind_fraction / depth
                            if math.isfinite(ratio):
                                record("UNWIND_DEPTH", ratio >= cfg.min_depth_ratio)
                                metrics["depth_ratio"] = round(ratio, 4)
        except (ValueError, TypeError, OverflowError, ZeroDivisionError):
            pass

    candles = list(s.candles[-13:])
    if len(candles) == 13 and all(
        b.start_ms-a.start_ms == 300_000 and a.close > 0 and b.close > 0
        for a, b in zip(candles, candles[1:])
    ):
        returns = [math.log(b.close/a.close) for a, b in zip(candles, candles[1:])]
        vol = math.sqrt(sum(x*x for x in returns))
        displacement = abs(math.log(candles[-1].close/candles[0].close))
        allowance = max(0.004, vol*0.75)
        record("PRICE_STALL", displacement <= allowance)
        metrics["price_displacement_60m"] = round(displacement, 6)

    return {
        "gate_checks": checks,
        "gate_metrics": metrics,
        "gate_diagnostics_only": True,
    }
