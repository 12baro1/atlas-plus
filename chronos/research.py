"""Observational, forward-only CHRONOS research from archived snapshots.

This is NOT a backtest, an execution simulator or a validated strategy:
funding-side directional returns are hypothetical mark-to-close proxies.
All outcome bars must already be archived, time-contiguous and fully closed.
Repeated scans on the same symbol overlap; do not treat them as independent.
"""
from __future__ import annotations

import json
import math
import statistics
from collections import Counter

BAR_MS = 300_000
HORIZONS_MINUTES = (15, 60, 180)


def _finite(value):
    try:
        x = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return x if math.isfinite(x) else None


def _gate(metrics: dict, name: str) -> bool:
    return (metrics.get("gate_checks") or {}).get(name) == "PASS"


def _future_close(db, symbol: str, observed_ms: int, horizon_minutes: int):
    """Return first candle close at/after horizon if every intervening bar exists.

    Observations may be taken mid-bar: including that bar is not lookahead
    because it is read only AFTER it is archived as a closed candle.
    """
    target = observed_ms + horizon_minutes * 60_000
    start = (observed_ms // BAR_MS) * BAR_MS
    end = ((target - 1) // BAR_MS) * BAR_MS
    if end <= start:
        return None
    expected_count = 1 + (end - start) // BAR_MS
    row = db.execute(
        """SELECT COUNT(*) AS n, MAX(CASE WHEN start_ms=? THEN c END) AS last
           FROM candles WHERE symbol=? AND start_ms BETWEEN ? AND ?""",
        (end, symbol, start, end),
    ).fetchone()
    if row is None or row["n"] != expected_count:
        return None
    return _finite(row["last"])


def _summarize_returns(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean_pct": None, "median_pct": None, "positive_pct": None}
    return {
        "n": len(values),
        "mean_pct": round(statistics.mean(values) * 100, 4),
        "median_pct": round(statistics.median(values) * 100, 4),
        "positive_pct": round(100 * sum(x > 0 for x in values) / len(values), 2),
    }


def summarize(store, horizons=HORIZONS_MINUTES) -> dict:
    rows = store.db.execute(
        """SELECT symbol, observed_ms, price, funding, verdict, reason, metrics_json
           FROM observations ORDER BY observed_ms, symbol"""
    ).fetchall()
    groups = ("all", "oi_ge_3pct", "funding_strong", "oi_and_funding", "all_direct_gates")
    returns = {group: {h: [] for h in horizons} for group in groups}
    group_counts = Counter()
    reason_counts = Counter()
    growth_values = []
    symbols = set()
    dates = []

    for row in rows:
        symbols.add(row["symbol"])
        dates.append(row["observed_ms"])
        reason_counts[row["reason"]] += 1
        try:
            metrics = json.loads(row["metrics_json"])
            if not isinstance(metrics, dict):
                metrics = {}
        except (TypeError, ValueError, json.JSONDecodeError):
            metrics = {}

        growth = _finite((metrics.get("gate_metrics") or {}).get("oi_growth_60m"))
        if growth is None:
            growth = _finite(metrics.get("oi_growth_60m"))
        if growth is not None:
            growth_values.append(growth)

        funding = _finite(row["funding"])
        price = _finite(row["price"])
        oi_pass = _gate(metrics, "OI_60M")
        funding_pass = _gate(metrics, "FUNDING")
        # Old archived observations might lack gate_checks. Keep them out of
        # condition-specific cohorts, rather than reinterpreting historic flags.
        flags = {
            "all": True,
            "oi_ge_3pct": oi_pass,
            "funding_strong": funding_pass,
            "oi_and_funding": oi_pass and funding_pass,
            "all_direct_gates": all(_gate(metrics, g) for g in (
                "OI_60M", "FUNDING", "FUNDING_WINDOW",
                "SPREAD", "BOOK_DEPTH", "PRICE_STALL")),
        }
        for group, selected in flags.items():
            if selected:
                group_counts[group] += 1

        if price is None or price <= 0 or funding is None or funding == 0:
            continue
        # Hypothetical funding-payer unwind direction:
        # positive funding => short hypothesis, negative => long.
        side = -1 if funding > 0 else 1
        for horizon in horizons:
            close = _future_close(store.db, row["symbol"], row["observed_ms"], horizon)
            if close is None or close <= 0:
                continue
            signed_return = side * (close / price - 1)
            for group, selected in flags.items():
                if selected:
                    returns[group][horizon].append(signed_return)

    oi = {
        "n": len(growth_values),
        "median_pct": round(100 * statistics.median(growth_values), 4) if growth_values else None,
        "min_pct": round(100 * min(growth_values), 4) if growth_values else None,
        "max_pct": round(100 * max(growth_values), 4) if growth_values else None,
    }
    return {
        "observations": len(rows),
        "symbols": len(symbols),
        "first_observed_ms": min(dates) if dates else None,
        "last_observed_ms": max(dates) if dates else None,
        "wait_reasons": dict(reason_counts.most_common()),
        "oi_60m": oi,
        "cohorts": {
            group: {
                "observations": group_counts[group],
                "directional_proxies": {
                    str(h): _summarize_returns(returns[group][h]) for h in horizons
                },
            }
            for group in groups
        },
        "limitations": (
            "Hypothetical funding-sign directional close returns only; not net profit, "
            "fills, live signals, stops, funding payments or a backtest. "
            "Future bars must be archived continuously. Overlapping scans "
            "are correlated and not independent observations."
        ),
    }


def format_report(report: dict) -> str:
    lines = [
        "CHRONOS | ARSIV ARASTIRMASI (islem/backtest degil)",
        f"Snapshots: {report['observations']} | Symbols: {report['symbols']}",
    ]
    oi = report["oi_60m"]
    if oi["n"]:
        lines.append(
            f"OI 60m: n={oi['n']} | median={oi['median_pct']:+.2f}% "
            f"| min={oi['min_pct']:+.2f}% | max={oi['max_pct']:+.2f}%"
        )
    for cohort, info in report["cohorts"].items():
        lines.append(f"\n{cohort} | observations={info['observations']}")
        for h, item in info["directional_proxies"].items():
            if not item["n"]:
                lines.append(f"  {h}m: NO FORWARD DATA")
            else:
                lines.append(
                    f"  {h}m: n={item['n']} gross mean={item['mean_pct']:+.3f}% "
                    f"median={item['median_pct']:+.3f}% positive={item['positive_pct']:.1f}%"
                )
    lines.append("\nCAUTION: " + report["limitations"])
    return "\n".join(lines)
