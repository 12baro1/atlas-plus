"""Transactional SQLite market archive, deduplicated hypotheses and research journal."""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from .models import Assessment, MarketSnapshot


class Store:
    def __init__(self, filename: str):
        self.filename = filename
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(filename, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS observations (
              symbol TEXT NOT NULL, observed_ms INTEGER NOT NULL, price REAL NOT NULL,
              funding REAL NOT NULL, oi REAL NOT NULL, verdict TEXT NOT NULL,
              score REAL NOT NULL, reason TEXT NOT NULL, metrics_json TEXT NOT NULL,
              PRIMARY KEY(symbol, observed_ms));
            CREATE INDEX IF NOT EXISTS obs_time ON observations(observed_ms);
            CREATE TABLE IF NOT EXISTS candles (
              symbol TEXT NOT NULL, start_ms INTEGER NOT NULL,
              o REAL NOT NULL, h REAL NOT NULL, l REAL NOT NULL, c REAL NOT NULL, v REAL NOT NULL,
              PRIMARY KEY(symbol, start_ms));
            CREATE TABLE IF NOT EXISTS signals (
              id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, side TEXT NOT NULL,
              created_ms INTEGER NOT NULL, next_funding_ms INTEGER NOT NULL,
              valid_until_ms INTEGER NOT NULL, entry REAL NOT NULL, stop REAL NOT NULL,
              target REAL NOT NULL, score REAL NOT NULL, gross_rr REAL NOT NULL,
              net_rr REAL NOT NULL, metrics_json TEXT NOT NULL,
              UNIQUE(symbol, side, next_funding_ms));
            CREATE INDEX IF NOT EXISTS signals_time ON signals(created_ms);
            CREATE TABLE IF NOT EXISTS candidates (
              symbol TEXT PRIMARY KEY, side TEXT NOT NULL,
              tier TEXT NOT NULL, created_ms INTEGER NOT NULL,
              updated_ms INTEGER NOT NULL, next_funding_ms INTEGER NOT NULL,
              valid_until_ms INTEGER NOT NULL, entry REAL NOT NULL,
              stop REAL NOT NULL, target REAL NOT NULL,
              score REAL NOT NULL, net_rr REAL NOT NULL,
              reason TEXT NOT NULL, metrics_json TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS candidates_expiry ON candidates(valid_until_ms);
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def record(self, snap: MarketSnapshot, assessment: Assessment) -> bool:
        """Return True only for a newly recorded (not repeated) signal."""
        with self.db:
            self.db.execute("""INSERT OR REPLACE INTO observations
               (symbol,observed_ms,price,funding,oi,verdict,score,reason,metrics_json)
               VALUES (?,?,?,?,?,?,?,?,?)""", (
                 snap.symbol,snap.observed_ms,snap.price,snap.funding_rate,
                 snap.open_interest[-1][1] if snap.open_interest else 0,
                 assessment.verdict,assessment.score,assessment.reason,
                 json.dumps(assessment.metrics,allow_nan=False)))
            self.db.executemany("""INSERT OR IGNORE INTO candles
                (symbol,start_ms,o,h,l,c,v) VALUES (?,?,?,?,?,?,?)""",
                [(snap.symbol,c.start_ms,c.open,c.high,c.low,c.close,c.volume)
                 for c in snap.candles])
            if not assessment.actionable:
                return False
            cursor = self.db.execute("""INSERT OR IGNORE INTO signals
               (symbol,side,created_ms,next_funding_ms,valid_until_ms,entry,stop,
                target,score,gross_rr,net_rr,metrics_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
                 assessment.symbol,assessment.side,assessment.observed_ms,
                 assessment.next_funding_ms,assessment.valid_until_ms,
                 assessment.entry,assessment.stop,assessment.target,
                 assessment.score,assessment.gross_rr,assessment.net_rr,
                 json.dumps(assessment.metrics,allow_nan=False)))
            return cursor.rowcount > 0

    def upsert_candidate(self, a: Assessment) -> bool:
        """Keep only the latest candidate for each symbol, never append stale ones.

        Returns True for a newly created candidate or changed event/tier.
        Price changes alone update the current candidate without counting new.
        """
        if a.verdict not in {"SETUP", "WATCH"} or not a.has_plan:
            return False
        old = self.db.execute(
            "SELECT side,tier,created_ms,next_funding_ms FROM candidates WHERE symbol=?",
            (a.symbol,)
        ).fetchone()
        changed = (old is None or old["side"] != a.side or
                   old["tier"] != a.verdict or
                   old["next_funding_ms"] != a.next_funding_ms)
        created = (a.observed_ms if changed else old["created_ms"])
        with self.db:
            self.db.execute("""INSERT INTO candidates
               (symbol,side,tier,created_ms,updated_ms,next_funding_ms,
                valid_until_ms,entry,stop,target,score,net_rr,reason,metrics_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol) DO UPDATE SET
                side=excluded.side,tier=excluded.tier,
                created_ms=excluded.created_ms,updated_ms=excluded.updated_ms,
                next_funding_ms=excluded.next_funding_ms,
                valid_until_ms=excluded.valid_until_ms,entry=excluded.entry,
                stop=excluded.stop,target=excluded.target,score=excluded.score,
                net_rr=excluded.net_rr,reason=excluded.reason,
                metrics_json=excluded.metrics_json""",
                (a.symbol,a.side,a.verdict,created,a.observed_ms,
                 a.next_funding_ms,a.valid_until_ms,a.entry,a.stop,a.target,
                 a.score,a.net_rr,a.reason,json.dumps(a.metrics,allow_nan=False)))
        return changed

    def clear_candidate(self, symbol: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM candidates WHERE symbol=?", (symbol,))

    def recent_candidates(self, limit: int = 20, now_ms: int | None = None) -> list[dict]:
        import time
        now_ms = int(time.time()*1000) if now_ms is None else now_ms
        return [dict(row) for row in self.db.execute(
            """SELECT * FROM candidates WHERE valid_until_ms>=?
               ORDER BY CASE tier WHEN 'SETUP' THEN 1 ELSE 0 END DESC,
                        score DESC LIMIT ?""", (now_ms,limit))]

    def recent_signals(self, limit: int = 10) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM signals ORDER BY created_ms DESC LIMIT ?", (limit,))]

    def count(self) -> dict[str,int]:
        return {
            "observations": self.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0],
            "candles": self.db.execute("SELECT COUNT(*) FROM candles").fetchone()[0],
            "signals": self.db.execute("SELECT COUNT(*) FROM signals").fetchone()[0],
            "candidates": self.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0],
        }
