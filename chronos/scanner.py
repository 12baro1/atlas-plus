"""Bounded concurrent scanner. SQLite writes are single-threaded."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from .bybit import BybitClient
from .models import Assessment
from .settings import Settings
from .storage import Store
from .strategy import evaluate
from .telegram import notify


@dataclass
class ScanResult:
    assessments: list[Assessment]
    errors: list[str]
    new_signals: int
    new_setups: int = 0


def scan_once(client: BybitClient, store: Store, cfg: Settings,
              limit: int | None = None, progress=None) -> ScanResult:
    market = client.universe(limit or cfg.scan_limit, cfg.min_turnover)
    if not market:
        raise RuntimeError("Bybit returned no eligible USDT perpetual markets")
    assessments: list[Assessment] = []
    errors = []
    new = 0
    new_setups = 0

    def analyze(ticker):
        snap = client.snapshot(ticker)
        return snap, evaluate(snap, cfg)

    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        futures = {pool.submit(analyze, ticker): ticker["symbol"] for ticker in market}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                snapshot, assessment = future.result()
                inserted = store.record(snapshot, assessment)
                assessments.append(assessment)
                candidate_changed = False
                if assessment.verdict in {"SETUP", "WATCH"}:
                    candidate_changed = store.upsert_candidate(assessment)
                    if candidate_changed:
                        new_setups += 1
                else:
                    store.clear_candidate(symbol)
                if inserted:
                    new += 1
                if inserted or (candidate_changed and assessment.verdict == "SETUP"):
                    try:
                        notify(assessment,cfg)
                    except Exception as exc:
                        errors.append(f"Telegram {symbol}: {exc.__class__.__name__}")
                if progress:
                    progress(len(assessments)+len([e for e in errors if not e.startswith('Telegram')]),
                             len(market), assessment, None)
            except Exception as exc:
                message=f"{symbol}: {exc.__class__.__name__}: {exc}"
                errors.append(message)
                if progress:
                    progress(len(assessments)+len([e for e in errors if not e.startswith('Telegram')]),
                             len(market), None, message)
    assessments.sort(
        key=lambda a: ({"SIGNAL":3,"SETUP":2,"WATCH":1}.get(a.verdict,0),a.score),
        reverse=True,
    )
    return ScanResult(assessments,errors,new,new_setups)
