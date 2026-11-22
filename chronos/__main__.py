"""CHRONOS command line entry point: python -m chronos COMMAND."""
from __future__ import annotations
import argparse
import json
import time
from .bybit import BybitClient, BybitDataError
from .replay import replay_all
from .research import summarize, format_report
from .scanner import scan_once
from .settings import Settings
from .storage import Store
from .terminal import Dashboard, fmt_time, print_result


def main() -> int:
    parser=argparse.ArgumentParser(description="Independent Bybit funding-time pressure research engine")
    subs=parser.add_subparsers(dest="command",required=True)
    for command in ("once","run"):
        sub=subs.add_parser(command)
        sub.add_argument("--limit",type=int,help="Top USDT perpetual pairs by 24h turnover")
    subs.choices["run"].add_argument("--interval",type=int,help="Scan frequency (seconds, min 60)")
    subs.choices["run"].add_argument("--plain",action="store_true",help="No full-screen display")
    subs.add_parser("doctor",help="Check connectivity and storage")
    subs.add_parser("report",help="Read archived signals")
    subs.add_parser("research",help="Analyze archived OI, gate cohorts and future closed-bar proxy outcomes")
    subs.add_parser("replay",help="Evaluate archived paper signals with later closed bars")
    args=parser.parse_args()
    cfg=Settings.from_env()
    if getattr(args,"limit",None) is not None and not 1 <= args.limit <= 100:
        parser.error("--limit must be 1..100")
    if getattr(args,"interval",None) is not None and args.interval < 60:
        parser.error("--interval must be >=60")
    store=Store(cfg.database)
    try:
        if args.command=="report":
            print("CHRONOS | counts:",store.count())
            print("ACTIVE CHRONOS CANDIDATES (not trade signals):")
            for plan in store.recent_candidates(20):
                print(plan["tier"],plan["symbol"],plan["side"],
                      "score",plan["score"],"entry",plan["entry"],
                      "SL",plan["stop"],"TP",plan["target"],
                      "netRR",plan["net_rr"])
            print("STRICT HISTORICAL SIGNALS:")
            for row in store.recent_signals(20):
                print(fmt_time(row["created_ms"]),row["symbol"],row["side"],
                      "score",row["score"],"net RR",row["net_rr"],
                      "entry",row["entry"],"SL",row["stop"],"TP",row["target"])
            return 0
        if args.command=="replay":
            result=replay_all(store,cfg)
            print(json.dumps(result,indent=2,ensure_ascii=False))
            return 0
        if args.command=="research":
            print(format_report(summarize(store)))
            return 0
        client=BybitClient(cfg.base_url,cfg.timeout_seconds)
        if args.command=="doctor":
            print("CHRONOS | read-only endpoint",cfg.base_url)
            print("Bybit server:",client.get("/v5/market/time"))
            print("Active crypto USDT perpetual instruments:",len(client.active_symbols()))
            print("Excluded identified non-crypto perpetuals:",client.excluded_tradfi)
            print("Eligible universe sample:",[x["symbol"] for x in client.universe(5,cfg.min_turnover)])
            print("SQLite:",store.count())
            return 0
        if args.command=="once":
            result=scan_once(client,store,cfg,args.limit)
            print_result(result,store.count())
            return 0 if len(result.assessments)>0 else 2
        if args.command=="run":
            interval=args.interval or cfg.interval_seconds
            with_rich = not args.plain and Dashboard()._live_cls is not None
            if not with_rich:
                while True:
                    start=time.monotonic()
                    try:
                        print_result(scan_once(client,store,cfg,args.limit),store.count())
                    except Exception as exc:
                        print("SCAN ERROR:",exc)
                    time.sleep(max(1,interval-(time.monotonic()-start)))
            else:
                with Dashboard() as display:
                    while True:
                        start=time.monotonic()
                        display.cycles+=1;display.completed=0;display.total=args.limit or cfg.scan_limit
                        display.latest=[]  # Discard stale prior-cycle data.
                        try:
                            scan_once(client,store,cfg,args.limit,display.update)
                        except Exception as exc:
                            display.logs.append(f"SCAN ERROR: {exc}")
                            if display._live:display._live.update(display.render())
                        time.sleep(max(1,interval-(time.monotonic()-start)))
        return 0
    except KeyboardInterrupt:
        return 130
    except (BybitDataError,ValueError,RuntimeError) as exc:
        print(f"CHRONOS ERROR: {exc}")
        return 1
    finally:
        store.close()


if __name__=="__main__":
    raise SystemExit(main())
