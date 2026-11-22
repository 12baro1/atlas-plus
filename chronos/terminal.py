"""Single-screen Rich live monitor; readable fallback if Rich is unavailable."""
from __future__ import annotations
from datetime import datetime,timezone
import math
import statistics
from collections import Counter
from .models import Assessment


def fmt_time(ms: int) -> str:
    return datetime.fromtimestamp(ms/1000,timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def score_label(a: Assessment) -> str:
    """Distinguish an uncomputed pressure score from an actual zero."""
    if a.has_plan or "score_is_calibrated_probability" in a.metrics:
        return f"{a.score:.1f}"
    return "-"


def reason_label(a: Assessment) -> str:
    """Show quantitative context when an early data/strategy gate rejects."""
    if a.reason == "open_interest_not_expanding":
        growth = a.metrics.get("oi_growth_60m")
        if isinstance(growth, (float, int)) and math.isfinite(growth):
            return f"{a.reason} (OI 60m {growth:+.2%})"
    if a.reason == "funding_pressure_too_small":
        funding = a.metrics.get("funding_rate")
        if isinstance(funding, (float, int)) and math.isfinite(funding):
            return f"{a.reason} (funding {funding:+.4%})"
    return a.reason


def print_result(result, counts: dict | None = None):
    print("CHRONOS | Bybit USDT perpetual | PAPER RESEARCH ONLY")
    verdicts = Counter(a.verdict for a in result.assessments)
    print(f"Scanned: {len(result.assessments)} | "
          f"SIGNAL {verdicts['SIGNAL']} | SETUP {verdicts['SETUP']} | "
          f"WATCH {verdicts['WATCH']} | WAIT {verdicts['WAIT']} | "
          f"Errors {len(result.errors)} | "
          f"New signals {result.new_signals} | New candidates {result.new_setups}")
    reasons = Counter(a.reason for a in result.assessments if a.verdict == "WAIT")
    if reasons:
        print("WAIT reasons:", ", ".join(f"{k}={v}" for k, v in reasons.most_common()))
    # Independent checks can fail in parallel. Conditional checks may be
    # skipped until their prerequisites are satisfied (not counted as FAIL).
    gate_failures = Counter(
        name for a in result.assessments
        for name, state in a.metrics.get("gate_checks", {}).items()
        if state == "FAIL"
    )
    gate_skips = Counter(
        name for a in result.assessments
        for name, state in a.metrics.get("gate_checks", {}).items()
        if state == "SKIP"
    )
    if gate_failures:
        print("Independent gate failures:", ", ".join(
            f"{k}={v}" for k, v in gate_failures.most_common()
        ))
    if gate_skips:
        print("Conditional checks skipped:", ", ".join(
            f"{k}={v}" for k, v in gate_skips.most_common()
        ))
    growth_values = [
        a.metrics["gate_metrics"]["oi_growth_60m"]
        for a in result.assessments
        if isinstance(a.metrics.get("gate_metrics", {}).get("oi_growth_60m"), (float, int))
        and math.isfinite(a.metrics["gate_metrics"]["oi_growth_60m"])
    ]
    if growth_values:
        print("Observed OI 60m: n={} | median {:+.2%} | min {:+.2%} | max {:+.2%}".format(
            len(growth_values), statistics.median(growth_values),
            min(growth_values), max(growth_values)
        ))
    for a in result.assessments:
        print(f"{a.symbol:<16} {a.verdict:<7} {score_label(a):>5}  "
              + (f"{a.side:<5} entry {a.entry:.6g} SL {a.stop:.6g} TP {a.target:.6g} netRR {a.net_rr:.2f}"
                 if a.has_plan else reason_label(a))
              + (f" [{a.metrics.get('strict_reject_reason','pending')}; ONAY BEKLIYOR]"
                 if a.verdict in {"SETUP","WATCH"} else ""))
    for error in result.errors[:8]:
        print("ERR", error)
    if counts:
        print("Archive:",counts)


class Dashboard:
    def __init__(self):
        try:
            from rich.live import Live
            self._live_cls = Live
        except ImportError:
            self._live_cls = None
        self.latest: list[Assessment] = []
        self.logs: list[str] = []
        self.completed = 0
        self.total = 0
        self.cycles = 0
        self._live = None

    def __enter__(self):
        if self._live_cls:
            self._live = self._live_cls(self.render(),refresh_per_second=2,screen=True)
            self._live.__enter__()
        return self

    def __exit__(self, *args):
        if self._live:
            self._live.__exit__(*args)

    def render(self):
        from rich.console import Group
        from rich.panel import Panel
        from rich.table import Table
        head = Table.grid(expand=True)
        head.add_column(); head.add_column(justify="right")
        head.add_row("[bold]CHRONOS[/bold] | Bybit V5 | observation-only",
                     f"Scan {self.cycles}  Progress {self.completed}/{self.total}")
        rows=Table(expand=True,show_lines=False)
        for key in ["Market","Decision","Score","Entry","SL","TP","Net RR / Reason"]:
            rows.add_column(key,overflow="ellipsis")
        for a in sorted(self.latest,key=lambda x:({"SIGNAL":3,"SETUP":2,"WATCH":1}.get(x.verdict,0),x.score),reverse=True)[:12]:
            rows.add_row(a.symbol,(a.verdict+" "+a.side if a.has_plan else "WAIT"),score_label(a),
                         f"{a.entry:.5g}" if a.has_plan else "-",
                         f"{a.stop:.5g}" if a.has_plan else "-",
                         f"{a.target:.5g}" if a.has_plan else "-",
                         f"{a.net_rr:.2f} PENDING" if a.verdict in {"SETUP","WATCH"}
                         else f"{a.net_rr:.2f}" if a.actionable else reason_label(a))
        logs="\n".join(self.logs[-7:]) or "Awaiting observations..."
        return Group(Panel(head,style="cyan"),Panel(rows,title="Pressure observations"),
                     Panel(logs,title="Events"),
                     Panel("Pressure score is heuristic, not a win probability. NO LIVE ORDERS.",style="dim"))

    def update(self, completed: int, total: int, a: Assessment | None, error: str | None):
        self.completed=completed;self.total=total
        if a:
            self.latest=[x for x in self.latest if x.symbol!=a.symbol]+[a]
            self.logs.append(f"{a.symbol}: {(a.verdict+' '+a.side) if a.has_plan else reason_label(a)}")
        if error:
            self.logs.append("ERROR: "+error[:120])
        self.logs=self.logs[-20:]
        if self._live:
            self._live.update(self.render())
        elif error:
            print(error)
