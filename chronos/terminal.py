"""Single-screen Rich live monitor; readable fallback if Rich is unavailable."""
from __future__ import annotations
from datetime import datetime,timezone
from .models import Assessment


def fmt_time(ms: int) -> str:
    return datetime.fromtimestamp(ms/1000,timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def print_result(result, counts: dict | None = None):
    print("CHRONOS | Bybit USDT perpetual | PAPER RESEARCH ONLY")
    print(f"Scanned: {len(result.assessments)} | Errors: {len(result.errors)} | New signals: {result.new_signals}")
    for a in result.assessments[:15]:
        print(f"{a.symbol:<16} {a.verdict:<7} {a.score:5.1f}  "
              + (f"{a.side:<5} entry {a.entry:.6g} SL {a.stop:.6g} TP {a.target:.6g} netRR {a.net_rr:.2f}"
                 if a.actionable else a.reason))
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
        for a in sorted(self.latest,key=lambda x:(x.actionable,x.score),reverse=True)[:12]:
            rows.add_row(a.symbol,a.side if a.actionable else "WAIT",f"{a.score:.1f}",
                         f"{a.entry:.5g}" if a.actionable else "-",
                         f"{a.stop:.5g}" if a.actionable else "-",
                         f"{a.target:.5g}" if a.actionable else "-",
                         f"{a.net_rr:.2f}" if a.actionable else a.reason)
        logs="\n".join(self.logs[-7:]) or "Awaiting observations..."
        return Group(Panel(head,style="cyan"),Panel(rows,title="Pressure observations"),
                     Panel(logs,title="Events"),
                     Panel("Pressure score is heuristic, not a win probability. NO LIVE ORDERS.",style="dim"))

    def update(self, completed: int, total: int, a: Assessment | None, error: str | None):
        self.completed=completed;self.total=total
        if a:
            self.latest=[x for x in self.latest if x.symbol!=a.symbol]+[a]
            self.logs.append(f"{a.symbol}: {a.side if a.actionable else a.reason}")
        if error:
            self.logs.append("ERROR: "+error[:120])
        self.logs=self.logs[-20:]
        if self._live:
            self._live.update(self.render())
        elif error:
            print(error)
