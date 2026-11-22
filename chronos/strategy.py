"""CHRONOS hypothesis: funding-time carrying pressure plus newly expanded OI.

No claim that OI reveals the direction or entry prices of individual traders.
The pressure score is heuristic and is NOT a calibrated probability.
"""
from __future__ import annotations
import math
import statistics
from dataclasses import replace
from .diagnostics import inspect_gates
from .models import Assessment, MarketSnapshot
from .settings import Settings


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return min(hi, max(lo, x))


def reject(s: MarketSnapshot, reason: str, metrics: dict | None = None, score: float = 0) -> Assessment:
    return Assessment(s.symbol, s.observed_ms, "WAIT", reason, round(score, 2), metrics or {})


def evaluate(s: MarketSnapshot, cfg: Settings) -> Assessment:
    """Decision result plus independent gate diagnostics; no exchange I/O.

    The original decision engine remains unchanged; observation-only gate
    checks never permit a trade that the main engine would reject.
    """
    result = _evaluate_decision(s, cfg)
    return replace(result, metrics={
        **inspect_gates(s, cfg),
        **result.metrics,
    })


def _evaluate_decision(s: MarketSnapshot, cfg: Settings) -> Assessment:
    """One deterministic, single-symbol evaluation; no exchange I/O."""
    candles = list(s.candles)
    if (len(candles) < 36 or not math.isfinite(s.price) or s.price <= 0
            or not math.isfinite(s.funding_rate) or len(s.funding_history) < 6):
        return reject(s, "insufficient_candles_or_funding_history")
    if any(candles[i].start_ms - candles[i-1].start_ms != 300_000
           for i in range(len(candles)-36+1, len(candles))):
        return reject(s, "gapped_candle_history")
    if s.observed_ms - (candles[-1].start_ms + 300_000) > 600_000:
        return reject(s, "stale_candles")
    if s.turnover_24h < cfg.min_turnover:
        return reject(s, "low_24h_turnover")
    if not s.open_interest or s.observed_ms - s.open_interest[-1][0] > 900_000:
        return reject(s, "stale_open_interest")
    oi_time, oi_now = s.open_interest[-1]
    old = [val for ts, val in s.open_interest if ts <= oi_time - 3_600_000]
    if not old or min(oi_now, old[-1]) <= 0:
        return reject(s, "missing_60m_open_interest_baseline")
    oi_growth = (oi_now - old[-1]) / old[-1]
    if oi_growth < cfg.min_oi_growth:
        return reject(s, "open_interest_not_expanding", {"oi_growth_60m": round(oi_growth, 5)})
    if abs(s.funding_rate) < cfg.min_abs_funding:
        return reject(s, "funding_pressure_too_small", {"funding_rate": s.funding_rate})
    mins_until_funding = (s.next_funding_ms - s.observed_ms) / 60_000
    if mins_until_funding < 0 or mins_until_funding > 180:
        return reject(s, "outside_funding_time_window", {"minutes_to_funding": round(mins_until_funding, 1)})
    if not s.bids or not s.asks:
        return reject(s, "missing_orderbook")
    best_bid = max(p for p, _ in s.bids)
    best_ask = min(p for p, _ in s.asks)
    mid = (best_bid + best_ask) / 2
    if best_ask <= best_bid or mid <= 0:
        return reject(s, "crossed_orderbook")
    spread = (best_ask - best_bid) / mid
    if spread > cfg.max_spread or abs(s.price - mid) / mid > 0.01:
        return reject(s, "spread_or_price_mismatch", {"spread": round(spread, 5)})
    side = "SHORT" if s.funding_rate > 0 else "LONG"
    # A LONG crowd unwind sells into the bid; SHORT crowd unwind buys from asks.
    exit_book = s.bids if side == "SHORT" else s.asks
    depth = sum(p * q for p,q in exit_book
                if (p >= mid * 0.995 if side == "SHORT" else p <= mid * 1.005))
    if depth < cfg.min_depth_usdt:
        return reject(s, "insufficient_orderbook_depth", {"side_depth_usdt": round(depth)})
    oi_delta_quote = (oi_now - old[-1]) * mid
    estimated_exit = oi_delta_quote * cfg.assumed_unwind_fraction
    depth_ratio = estimated_exit / depth
    if depth_ratio < cfg.min_depth_ratio:
        return reject(s, "estimated_unwind_too_small", {"depth_ratio": round(depth_ratio, 3)})
    # A sudden absence of price displacement despite OI expansion is a hypothesis,
    # not proof of a trapped crowd. Compare normalized price movement to 1h vol.
    prices = [c.close for c in candles[-13:]]
    log_returns = [math.log(b / a) for a, b in zip(prices, prices[1:])]
    vol_1h = math.sqrt(sum(x*x for x in log_returns))
    displacement = abs(math.log(prices[-1] / prices[0]))
    stall_allowance = max(0.004, vol_1h * 0.75)
    if displacement > stall_allowance:
        return reject(s, "price_not_stalled", {"displacement": round(displacement, 5), "vol_1h": round(vol_1h, 5)})
    trs = [max(c.high-c.low, abs(c.high-p.close), abs(c.low-p.close))
           for p,c in zip(candles[-15:-1],candles[-14:])]
    atr = statistics.mean(trs)
    if not math.isfinite(atr) or atr <= 0 or vol_1h <= 0:
        return reject(s, "insufficient_realized_volatility")
    funding_baseline = statistics.median(abs(x) for x in s.funding_history)
    funding_score = clamp(abs(s.funding_rate) / max(0.00012, funding_baseline * 2))
    oi_score = clamp(oi_growth / 0.10)
    stall_score = clamp(1 - displacement / max(stall_allowance, 1e-10))
    depth_score = clamp(depth_ratio / 3.0)
    urgency_score = clamp(1 - mins_until_funding / 180)
    score = 100 * (0.25 * funding_score + 0.20 * oi_score + 0.15 * stall_score
                   + 0.20 * depth_score + 0.20 * urgency_score)
    metrics = {
        "side_hypothesis": side, "funding_rate": s.funding_rate,
        "minutes_to_funding": round(mins_until_funding, 1),
        "oi_growth_60m": round(oi_growth, 6), "vol_1h": round(vol_1h, 6),
        "price_displacement_60m": round(displacement, 6),
        "book_exit_depth_usdt": round(depth), "estimated_unwind_usdt": round(estimated_exit),
        "depth_ratio": round(depth_ratio, 3), "spread": round(spread, 6),
        "assumed_unwind_fraction": cfg.assumed_unwind_fraction,
        "score_is_calibrated_probability": False,
    }
    if score < cfg.min_pressure_score:
        return reject(s, "pressure_score_below_threshold", metrics, score)
    # Target magnitude is a testable scenario estimate, NOT a forecasted move.
    # Deliberately do not increase a TP merely to meet RR: both SL and TP
    # are determined independently from measured volatility / book capacity.
    stop_distance = max(atr * 1.25, mid * 0.002, (best_ask-best_bid) * 3)
    scenario_move = mid * min(0.035, 0.78 * vol_1h * math.sqrt(min(depth_ratio, 4)))
    if scenario_move <= 0:
        return reject(s, "no_projected_move", metrics, score)
    entry = mid
    direction = 1 if side == "LONG" else -1
    stop = entry - direction * stop_distance
    target = entry + direction * scenario_move
    if stop <= 0 or target <= 0:
        return reject(s, "invalid_stop_target", metrics, score)
    # Conservative assumed taker entry + taker stop/exit, half-spread and slippage.
    per_leg_friction = cfg.taker_fee + cfg.slippage + spread / 2
    round_trip = 2 * entry * per_leg_friction
    gross_rr = scenario_move / stop_distance
    net_rr = (scenario_move - round_trip) / (stop_distance + round_trip)
    metrics.update({"stop_pct": round(stop_distance / entry, 6),
                    "scenario_move_pct": round(scenario_move / entry, 6),
                    "round_trip_cost_pct": round(round_trip / entry, 6),
                    "gross_rr": round(gross_rr, 3), "net_rr": round(net_rr, 3)})
    if net_rr < cfg.min_net_rr:
        return reject(s, "net_rr_below_threshold", metrics, score)
    return Assessment(
        symbol=s.symbol, observed_ms=s.observed_ms, verdict="SIGNAL",
        reason="funding_time_carry_pressure_hypothesis", score=round(score,2),
        metrics=metrics, side=side, entry=round(entry,10), stop=round(stop,10),
        target=round(target,10), gross_rr=round(gross_rr,3), net_rr=round(net_rr,3),
        next_funding_ms=s.next_funding_ms,
        valid_until_ms=min(s.observed_ms+cfg.max_signal_age_minutes*60_000,
                           s.next_funding_ms + 60*60_000),
    )
