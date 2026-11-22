"""Pending trigger plans for the funding-clock unwind hypothesis.

A conditional plan is not a prediction that the funding-payer crowd will
exit. The ENTRY is a *future breakout trigger* drawn from completed bars.
No actual order or trade is placed; price confirmation and future validation
are still required. OI need not expand: a decline can mean a release is
underway, but OI alone never identifies which side is exiting.
"""
from __future__ import annotations

import math
import statistics

from .models import Assessment, MarketSnapshot
from .settings import Settings

BAR_MS = 300_000
MIN_FUNDING = 0.000005        # tiny funding => no directional hypothesis
MIN_NET_WATCH_RR = 0.05       # reject negative after-cost scenarios
MIN_SETUP_FUNDING = 0.00002  # experimental candidate strength, not strict signal
MIN_SETUP_NET_RR = 0.75      # indicative only; no execution authorization


def conditional_plan(s: MarketSnapshot, cfg: Settings,
                     strict: Assessment, diagnostics: dict
                     ) -> tuple[Assessment | None, str]:
    """Return (conditional plan, blocker); does not modify strict SIGNAL."""
    if strict.verdict != "WAIT":
        return None, "strict_signal"

    checks = diagnostics.get("gate_checks") or {}
    # Failing a real liquidity, spread, turnover, or time prerequisite is
    # never bypassed merely to produce a setup.
    for name in ("TURNOVER", "SPREAD", "BOOK_DEPTH", "FUNDING_WINDOW"):
        if checks.get(name) != "PASS":
            return None, f"unsafe_or_missing_{name.lower()}"

    candles = list(s.candles)
    if len(candles) < 36 or len(s.funding_history) < 6:
        return None, "insufficient_history"
    if any(b.start_ms-a.start_ms != BAR_MS
           for a,b in zip(candles[-36:], candles[-35:])):
        return None, "candle_history_gap"
    if s.observed_ms-(candles[-1].start_ms+BAR_MS)>600_000:
        return None, "stale_candles"
    if not s.open_interest or s.observed_ms-s.open_interest[-1][0]>900_000:
        return None, "stale_oi"
    growth = (diagnostics.get("gate_metrics") or {}).get("oi_growth_60m")
    if not isinstance(growth,(int,float)) or not math.isfinite(growth):
        return None, "missing_oi_baseline"

    funding = s.funding_rate
    if not math.isfinite(funding):
        return None, "invalid_funding"
    history = [abs(f) for f in s.funding_history if math.isfinite(f)]
    if len(history) < 6:
        return None, "insufficient_funding_history"
    funding_baseline = statistics.median(history)
    if abs(funding) < max(MIN_FUNDING, 0.1*funding_baseline):
        return None, "funding_signal_too_weak"

    minutes_to_funding = (s.next_funding_ms-s.observed_ms)/60_000
    if not 5 < minutes_to_funding <= 180:
        return None, "too_late_for_trigger"

    try:
        bids = [(p,q) for p,q in s.bids
                if math.isfinite(p) and math.isfinite(q) and p>0 and q>0]
        asks = [(p,q) for p,q in s.asks
                if math.isfinite(p) and math.isfinite(q) and p>0 and q>0]
        bid = max(p for p,q in bids)
        ask = min(p for p,q in asks)
        if bid >= ask:
            return None, "invalid_orderbook"
        mid = (bid+ask)/2
        spread_dollars = ask-bid
        spread = spread_dollars/mid
        if spread > cfg.max_spread or not math.isfinite(s.price) or abs(s.price/mid-1)>0.01:
            return None, "spread_or_price_mismatch"

        # Do not trust the exchange depth just because an API field exists.
        side = "SHORT" if funding>0 else "LONG"
        exit_book = bids if side=="SHORT" else asks
        depth = sum(p*q for p,q in exit_book
                    if (p>=mid*0.995 if side=="SHORT" else p<=mid*1.005))
        if depth < cfg.min_depth_usdt:
            return None, "insufficient_depth"

        price_series = [c.close for c in candles[-13:]]
        if any(not math.isfinite(c) or c<=0 for c in price_series):
            return None, "invalid_candles"
        returns=[math.log(b/a) for a,b in zip(price_series,price_series[1:])]
        vol60=math.sqrt(sum(r*r for r in returns))
        if not math.isfinite(vol60) or vol60<=0:
            return None, "no_volatility"
        trs=[max(c.high-c.low,abs(c.high-p.close),abs(c.low-p.close))
             for p,c in zip(candles[-15:-1], candles[-14:])]
        atr=statistics.mean(trs)
        if atr<=0 or not math.isfinite(atr):
            return None, "no_atr"

        # An as-yet-unbroken 60-minute range supplies a measurable confirmation.
        lookback = candles[-12:]
        low = min(c.low for c in lookback)
        high = max(c.high for c in lookback)
        if not 0<low<high or not all(math.isfinite(v) for v in (low,high)):
            return None, "invalid_range"
        buffer=max(2*spread_dollars,0.05*atr)
        entry = (low-buffer) if side=="SHORT" else (high+buffer)
        trigger_gap=abs(mid-entry)/mid
        if (side=="SHORT" and not entry<mid) or (side=="LONG" and not entry>mid):
            return None, "trigger_already_crossed"
        if trigger_gap > max(0.02,3*atr/mid):
            return None, "trigger_too_far"

        stop_distance=max(1.25*atr,0.002*mid,3*spread_dollars)
        # Observed volatility at a 3h planning horizon. The target is not
        # pushed out to force RR. A fill has NOT yet occurred at entry.
        scenario_move=min(0.03*mid, vol60*math.sqrt(3)*mid)
        if scenario_move<=0 or stop_distance<=0:
            return None, "invalid_geometry"
        direction = 1 if side=="LONG" else -1
        stop=entry-direction*stop_distance
        target=entry+direction*scenario_move
        if min(entry,stop,target)<=0:
            return None, "invalid_price_levels"

        friction=2*entry*(cfg.taker_fee+cfg.slippage+spread/2)
        net_rr=(scenario_move-friction)/(stop_distance+friction)
        if not math.isfinite(net_rr) or net_rr < MIN_NET_WATCH_RR:
            return None, "no_positive_reward_after_costs"

        # Stronger grade still REQUIRES a future price break. Neither grade
        # is a trade signal; market participation is a separate decision.
        is_setup=(abs(funding)>=max(MIN_SETUP_FUNDING,0.25*funding_baseline)
                  and net_rr>=MIN_SETUP_NET_RR
                  and abs(growth)>=0.001
                  and trigger_gap <= max(0.012,2*atr/mid))
        grade="SETUP" if is_setup else "WATCH"
        score=100*(
            0.28*min(1.,abs(funding)/max(0.00008,funding_baseline))
            +0.18*min(1.,abs(growth)/0.02)
            +0.24*max(0.,1-minutes_to_funding/180)
            +0.15*min(1.,max(net_rr,0)/2)
            +0.15*max(0.,1-trigger_gap/0.02))
        expiry=min(s.next_funding_ms,s.observed_ms+cfg.max_signal_age_minutes*60_000)
        if expiry <= s.observed_ms+BAR_MS:
            return None, "trigger_expires_too_soon"
        return Assessment(
            symbol=s.symbol, observed_ms=s.observed_ms, verdict=grade,
            reason="conditional_funding_breakout_not_triggered",
            score=round(score,2),
            metrics={
                "candidate_tier":grade,
                "candidate_mode":"FUNDING_CLOCK_TRIGGER",
                "strict_reject_reason":strict.reason,
                "funding_rate":funding,
                "oi_growth_60m":growth,
                "minutes_to_funding":round(minutes_to_funding,1),
                "book_exit_depth_usdt":round(depth,2),
                "spread":round(spread,6),
                "breakout_confirm":f"5m closed candle {'BELOW' if side=='SHORT' else 'ABOVE'} {entry:.10g}",
                "reference_mid":round(mid,10),
                "trigger_gap_pct":round(trigger_gap,6),
                "scenario_vol_60m":round(vol60,6),
                "round_trip_cost_pct":round(friction/entry,6),
                "entry_is_trigger":True,
                "needs_confirmation":True,
                "research_only":True,
                "score_is_calibrated_probability":False,
            },
            side=side, entry=round(entry,10),
            stop=round(stop,10),target=round(target,10),
            gross_rr=round(scenario_move/stop_distance,3),
            net_rr=round(net_rr,3),
            next_funding_ms=s.next_funding_ms,
            valid_until_ms=expiry,
        ), ""
    except (ValueError, TypeError, OverflowError, ZeroDivisionError):
        return None, "invalid_numeric_market_data"
