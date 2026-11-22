"""Independent, unconfirmed CHRONOS setup-plan generator.

SIGNAL remains the strict original model. SETUP and WATCH are *research
plans*, not execution recommendations and never place exchange orders.
Level geometry is derived from observed closed-candle volatility; profit
targets are scenarios, not liquidity predictions or assured returns.
"""
from __future__ import annotations

import math
import statistics

from .models import Assessment, MarketSnapshot
from .settings import Settings

# These are *candidate classification* criteria, distinct from the strict
# signal gates. They are research hypotheses, not validated trading edges.
MIN_WATCH_FUNDING = 0.00001
MIN_WATCH_OI_GROWTH = 0.001
MIN_WATCH_NET_RR = 0.25
MIN_SETUP_FUNDING = 0.000025
MIN_SETUP_OI_GROWTH = 0.002
MIN_SETUP_NET_RR = 1.0
MAX_TARGET_MOVE_PCT = 0.03
BAR_MS = 300_000

HARD_REJECTIONS = frozenset({
    "insufficient_candles_or_funding_history",
    "gapped_candle_history",
    "stale_candles",
    "low_24h_turnover",
    "stale_open_interest",
    "missing_60m_open_interest_baseline",
    "missing_orderbook",
    "crossed_orderbook",
    "spread_or_price_mismatch",
    "insufficient_orderbook_depth",
    "outside_funding_time_window",
    "price_not_stalled",
    "insufficient_realized_volatility",
    "invalid_stop_target",
})


def candidate_setup(s: MarketSnapshot, cfg: Settings,
                    rejected: Assessment, diagnostics: dict) -> Assessment | None:
    """Build a lower-conviction, explicitly pending plan from valid observations.

    Conservative hard guards remain enforced; missing values cannot become
    positive signals, and the funding sign never identifies actual positions.
    """
    if rejected.verdict != "WAIT" or rejected.reason in HARD_REJECTIONS:
        return None

    checks = diagnostics.get("gate_checks") or {}
    needed = ("TURNOVER", "SPREAD", "BOOK_DEPTH", "FUNDING_WINDOW", "PRICE_STALL")
    if any(checks.get(name) != "PASS" for name in needed):
        return None

    candles = list(s.candles)
    if len(candles) < 36 or len(s.funding_history) < 6:
        return None
    if any(b.start_ms-a.start_ms != BAR_MS
           for a, b in zip(candles[-36:], candles[-35:])):
        return None
    if s.observed_ms - (candles[-1].start_ms+BAR_MS) > 600_000:
        return None
    if not s.open_interest or s.observed_ms-s.open_interest[-1][0] > 900_000:
        return None

    metric = diagnostics.get("gate_metrics") or {}
    growth = metric.get("oi_growth_60m")
    if growth is None or not math.isfinite(growth) or growth < MIN_WATCH_OI_GROWTH:
        return None
    if not math.isfinite(s.funding_rate) or abs(s.funding_rate) < MIN_WATCH_FUNDING:
        return None

    try:
        hist = [abs(v) for v in s.funding_history if math.isfinite(v)]
        if len(hist) < 6:
            return None
        baseline = statistics.median(hist)
        if abs(s.funding_rate) < max(MIN_WATCH_FUNDING, baseline * 0.5):
            return None

        best_bid = max(p for p, q in s.bids if p > 0 and q > 0)
        best_ask = min(p for p, q in s.asks if p > 0 and q > 0)
        if best_bid >= best_ask:
            return None
        mid = (best_bid + best_ask)/2
        spread = (best_ask - best_bid)/mid
        if spread > cfg.max_spread or abs(s.price/mid-1) > 0.01:
            return None

        # Use only the last 12 completed five-minute returns.
        close = [c.close for c in candles[-13:]]
        if any(p <= 0 or not math.isfinite(p) for p in close):
            return None
        r = [math.log(b/a) for a,b in zip(close,close[1:])]
        realized_60m = math.sqrt(sum(x*x for x in r))
        if realized_60m <= 0:
            return None
        displacement = abs(math.log(close[-1]/close[0]))
        if displacement > max(0.004, realized_60m*0.75):
            return None

        trs = [max(cur.high-cur.low, abs(cur.high-prev.close),
                   abs(cur.low-prev.close))
               for prev,cur in zip(candles[-15:-1],candles[-14:])]
        atr = statistics.mean(trs)
        if not math.isfinite(atr) or atr <= 0:
            return None
        # Natural volatility scenario at a 3h horizon. No target inflation
        # to satisfy a particular RR threshold.
        stop_distance = max(1.1*atr, 0.002*mid, 3*(best_ask-best_bid))
        target_distance = min(MAX_TARGET_MOVE_PCT*mid,
                              realized_60m * math.sqrt(3) * mid)
        if target_distance <= 0 or stop_distance <= 0:
            return None

        side = "SHORT" if s.funding_rate > 0 else "LONG"
        direction = -1 if side == "SHORT" else 1
        stop = mid - direction*stop_distance
        target = mid + direction*target_distance
        if stop <= 0 or target <= 0:
            return None
        fee_pct = 2*(cfg.taker_fee+cfg.slippage+spread/2)
        total_cost = mid*fee_pct
        net_rr = (target_distance-total_cost)/(stop_distance+total_cost)
        if not math.isfinite(net_rr) or net_rr < MIN_WATCH_NET_RR:
            return None

        until = (s.next_funding_ms-s.observed_ms)/60_000
        if not 0 <= until <= 180:
            return None

        is_setup = (
            growth >= MIN_SETUP_OI_GROWTH
            and abs(s.funding_rate) >= max(MIN_SETUP_FUNDING, baseline)
            and net_rr >= MIN_SETUP_NET_RR
        )
        # Ranking index is heuristic, NEVER win probability.
        score = 100*(
            0.28*min(1., abs(s.funding_rate)/max(0.00008,baseline*2))
            + 0.24*min(1., growth/0.03)
            + 0.20*max(0., 1-until/180)
            + 0.12*max(0., 1-displacement/max(0.004,realized_60m*0.75))
            + 0.16*min(1., max(0.,net_rr)/2.0)
        )
        status = "SETUP" if is_setup else "WATCH"
        return Assessment(
            symbol=s.symbol, observed_ms=s.observed_ms, verdict=status,
            reason="pending_funding_pressure_confirmation",
            score=round(score, 2),
            metrics={
                "candidate_tier": status,
                "strict_reject_reason": rejected.reason,
                "funding_rate": s.funding_rate,
                "oi_growth_60m": growth,
                "minutes_to_funding": round(until,1),
                "realized_vol_60m": round(realized_60m, 6),
                "volatility_scenario_pct": round(target_distance/mid, 6),
                "round_trip_cost_pct": round(fee_pct, 6),
                "spread": round(spread, 6),
                "score_is_calibrated_probability": False,
                "research_only": True,
                "entry_is_indicative_mid": True,
                "needs_confirmation": True,
            },
            side=side, entry=round(mid,10), stop=round(stop,10),
            target=round(target,10),
            gross_rr=round(target_distance/stop_distance,3),
            net_rr=round(net_rr,3),
            next_funding_ms=s.next_funding_ms,
            valid_until_ms=min(s.observed_ms+cfg.max_signal_age_minutes*60_000,
                               s.next_funding_ms+60*60_000),
        )
    except (ValueError, TypeError, OverflowError, ZeroDivisionError):
        return None
