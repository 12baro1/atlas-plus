"""Conservative forward-only journal replay of archived closed 5m candles.

This is NOT a historical order-book backtest; it needs observations gathered
by CHRONOS first. Same-bar target/stop collisions resolve against the trade.
"""
from __future__ import annotations
import json
from .settings import Settings
from .storage import Store


def replay_signal(store: Store, sig: dict, cfg: Settings) -> dict:
    start = ((sig["created_ms"] // 300_000) + 1) * 300_000
    horizon = sig["valid_until_ms"]
    # Only fully-closed candles before expiry.
    bars = store.db.execute("""SELECT * FROM candles WHERE symbol=? AND start_ms>=?
                       AND start_ms+300000<=? ORDER BY start_ms""",
                       (sig["symbol"],start,horizon)).fetchall()
    if not bars:
        return {"id":sig["id"],"status":"PENDING","reason":"missing_future_bars"}
    first = bars[0]
    if first["start_ms"] != start:
        return {"id":sig["id"],"status":"UNFILLED","reason":"first_future_bar_missing"}
    entry = first["o"]
    if abs(entry/sig["entry"]-1) > cfg.max_entry_deviation:
        return {"id":sig["id"],"status":"UNFILLED","reason":"entry_deviation"}
    direction = 1 if sig["side"] == "LONG" else -1
    stop = sig["stop"]; target=sig["target"]
    if direction * (entry-stop) <= 0 or direction * (target-entry) <= 0:
        return {"id":sig["id"],"status":"UNFILLED","reason":"entry_invalidates_geometry"}
    outcome = None; exit_price = None
    previous_start = start - 300_000
    for bar in bars:
        if bar["start_ms"] != previous_start + 300_000:
            return {"id":sig["id"],"status":"PENDING","reason":"missing_candle_in_holding_window"}
        previous_start = bar["start_ms"]
        if direction == 1:
            hit_stop = bar["l"] <= stop; hit_target = bar["h"] >= target
        else:
            hit_stop = bar["h"] >= stop; hit_target = bar["l"] <= target
        if hit_stop:
            outcome = "STOP";exit_price=stop;break
        if hit_target:
            outcome = "TARGET";exit_price=target;break
    if outcome is None:
        latest = store.db.execute("SELECT MAX(start_ms) FROM candles WHERE symbol=?",
                                  (sig["symbol"],)).fetchone()[0]
        if latest is None or latest + 300_000 < horizon:
            return {"id":sig["id"],"status":"PENDING","reason":"horizon_not_observed"}
        outcome="TIME";exit_price=bars[-1]["c"]
    spread = float(json.loads(sig["metrics_json"]).get("spread",0))
    friction = 2 * (cfg.taker_fee+cfg.slippage+spread/2)*entry
    risk = abs(entry-stop)+friction
    net_r = (direction*(exit_price-entry)-friction)/risk
    return {"id":sig["id"],"symbol":sig["symbol"],"side":sig["side"],
            "status":outcome,"net_r":round(net_r,4),"entry":entry,"exit":exit_price}


def replay_all(store: Store, cfg: Settings) -> dict:
    sigs=[dict(r) for r in store.db.execute("SELECT * FROM signals ORDER BY created_ms")]
    outcomes=[replay_signal(store, r, cfg) for r in sigs]
    finished=[r for r in outcomes if r["status"] in {"STOP","TARGET","TIME"}]
    positive=sum(r["net_r"]>0 for r in finished)
    return {"signals":len(sigs),"finished":len(finished),
            "pending_or_unfilled":len(outcomes)-len(finished),
            "net_r":round(sum(r["net_r"] for r in finished),4),
            "win_rate":round(positive/len(finished),4) if finished else None,
            "outcomes":outcomes}
