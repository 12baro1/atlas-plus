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

    def recent_signals(self, limit: int = 10) -> list[dict]:
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM signals ORDER BY created_ms DESC LIMIT ?", (limit,))]

    def count(self) -> dict[str,int]:
        return {
            "observations": self.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0],
            "candles": self.db.execute("SELECT COUNT(*) FROM candles").fetchone()[0],
            "signals": self.db.execute("SELECT COUNT(*) FROM signals").fetchone()[0],
        }
