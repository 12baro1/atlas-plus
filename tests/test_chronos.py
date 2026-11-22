import dataclasses
import io
from contextlib import redirect_stdout
import math
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from chronos.bybit import BybitClient, BybitDataError, crypto_underlying
from chronos.models import Candle, MarketSnapshot
from chronos.settings import Settings
from chronos.strategy import evaluate
from chronos.storage import Store
from chronos.replay import replay_signal, replay_all
from chronos.scanner import ScanResult
from chronos.terminal import print_result, score_label, reason_label


def fixture(funding=0.001, oi_new=1_000_000, now=1_800_000_000_000):
    # Completed 5m candles, bounded oscillation with no persistent displacement.
    start=now-60*300_000
    candles=[]
    for i in range(59):
        close=100.4 if i%2 else 99.6
        candles.append(Candle(start+i*300_000,100, max(close,100)+0.1,
                              min(close,100)-0.1,close,1000))
    oi=tuple((now-30*300_000 + i*300_000, 700_000+(oi_new-700_000)*i/29)
             for i in range(30))
    return MarketSnapshot(symbol="BTCUSDT",observed_ms=now,price=100,
        funding_rate=funding,next_funding_ms=now+30*60_000,
        turnover_24h=90_000_000,open_interest=oi,
        funding_history=(0.00003,)*20,candles=tuple(candles),
        bids=((99.99,800),(99.95,100),(99.9,100)),
        asks=((100.01,800),(100.05,100),(100.1,100)),
        ticker_open_interest=oi_new)


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.cfg=Settings()

    def test_positive_funding_short(self):
        a=evaluate(fixture(),self.cfg)
        self.assertEqual(a.verdict,"SIGNAL",a.reason+" "+repr(a.metrics))
        self.assertEqual(a.side,"SHORT")
        self.assertGreaterEqual(a.net_rr,self.cfg.min_net_rr)
        self.assertGreater(a.stop,a.entry)
        self.assertLess(a.target,a.entry)
        self.assertIs(a.metrics["score_is_calibrated_probability"],False)

    def test_negative_funding_long(self):
        a=evaluate(fixture(funding=-0.001),self.cfg)
        self.assertEqual(a.verdict,"SIGNAL",a.reason)
        self.assertEqual(a.side,"LONG")
        self.assertLess(a.stop,a.entry)
        self.assertGreater(a.target,a.entry)

    def test_no_funding_pressure_no_signal(self):
        self.assertEqual(evaluate(fixture(funding=0.000001),self.cfg).reason,
                         "funding_pressure_too_small")

    def test_no_oi_expansion_no_signal(self):
        self.assertEqual(evaluate(fixture(oi_new=700_000),self.cfg).reason,
                         "open_interest_not_expanding")

    def test_stale_market_data_no_signal(self):
        f=fixture()
        f=dataclasses.replace(f,observed_ms=f.observed_ms+60*60_000)
        self.assertEqual(evaluate(f,self.cfg).reason,"stale_candles")

    def test_crossed_book_no_signal(self):
        f=dataclasses.replace(fixture(),asks=((99,900),))
        self.assertEqual(evaluate(f,self.cfg).reason,"crossed_orderbook")

    def test_wide_spread_no_signal(self):
        f=dataclasses.replace(fixture(),asks=((102,900),))
        self.assertEqual(evaluate(f,self.cfg).reason,"spread_or_price_mismatch")

    def test_missing_funding_history_blocks(self):
        f=dataclasses.replace(fixture(),funding_history=())
        self.assertEqual(evaluate(f,self.cfg).reason,"insufficient_candles_or_funding_history")

    def test_never_treats_score_as_probability(self):
        a=evaluate(fixture(),self.cfg)
        self.assertLessEqual(a.score,100)
        self.assertIs(a.metrics["score_is_calibrated_probability"],False)

    
class TerminalDiagnosticTests(unittest.TestCase):
    def test_oi_wait_shows_true_growth_and_no_fake_zero_score(self):
        a = evaluate(fixture(oi_new=700_000), Settings())
        self.assertEqual(a.reason, "open_interest_not_expanding")
        self.assertEqual(score_label(a), "-")
        self.assertIn("OI 60m +0.00%", reason_label(a))
        output = io.StringIO()
        with redirect_stdout(output):
            print_result(ScanResult([a], [], 0))
        self.assertIn("OI 60m +0.00%", output.getvalue())
        self.assertRegex(output.getvalue(), r"WAIT\s+-\s+open_interest_not_expanding")

    def test_one_shot_output_shows_full_scan_and_wait_reason_counts(self):
        from chronos.models import Assessment
        assessments = [Assessment(f"TOKEN{i}USDT", 1800000000000, "WAIT",
                                  "open_interest_not_expanding", 0.0,
                                  {"oi_growth_60m": 0.001})
                       for i in range(30)]
        output = io.StringIO()
        with redirect_stdout(output):
            print_result(ScanResult(assessments, [], 0))
        text = output.getvalue()
        self.assertIn("TOKEN29USDT", text)
        self.assertIn("open_interest_not_expanding=30", text)

    def test_independent_gates_expose_other_failures_after_oi_rejection(self):
        f = fixture(oi_new=700_000, funding=0.000001)
        f = dataclasses.replace(f, next_funding_ms=f.observed_ms+240*60_000)
        assessment = evaluate(f, Settings())
        self.assertEqual(assessment.reason, "open_interest_not_expanding")
        checks = assessment.metrics["gate_checks"]
        self.assertEqual(checks["OI_60M"], "FAIL")
        self.assertEqual(checks["FUNDING"], "FAIL")
        self.assertEqual(checks["FUNDING_WINDOW"], "FAIL")
        self.assertEqual(checks["PRICE_STALL"], "PASS")
        self.assertTrue(assessment.metrics["gate_diagnostics_only"])
        self.assertFalse(assessment.actionable)

    def test_conditional_unwind_not_failed_for_falling_oi(self):
        a = evaluate(fixture(oi_new=650_000), Settings())
        self.assertEqual(a.reason, "open_interest_not_expanding")
        self.assertEqual(a.metrics["gate_checks"]["OI_60M"], "FAIL")
        self.assertEqual(a.metrics["gate_checks"]["UNWIND_DEPTH"], "SKIP")
        self.assertNotIn("depth_ratio", a.metrics["gate_metrics"])

    def test_conditional_unwind_not_failed_for_positive_but_low_oi(self):
        a = evaluate(fixture(oi_new=714_000), Settings())
        self.assertEqual(a.reason, "open_interest_not_expanding")
        self.assertEqual(a.metrics["gate_checks"]["UNWIND_DEPTH"], "SKIP")

    def test_conditional_unwind_evaluated_only_after_prerequisites(self):
        a = evaluate(fixture(), Settings())
        self.assertEqual(a.metrics["gate_checks"]["UNWIND_DEPTH"], "PASS")
        self.assertGreater(a.metrics["gate_metrics"]["depth_ratio"], 0)

    def test_conditional_skip_and_oi_distribution_in_cli(self):
        from chronos.models import Assessment
        checks = {"OI_60M": "FAIL", "UNWIND_DEPTH": "SKIP"}
        sample = [
            Assessment("ONEUSDT", 1, "WAIT", "open_interest_not_expanding",
                       metrics={"gate_checks": checks, "gate_metrics": {"oi_growth_60m": -0.01}}),
            Assessment("TWOUSDT", 2, "WAIT", "open_interest_not_expanding",
                       metrics={"gate_checks": checks, "gate_metrics": {"oi_growth_60m": 0.02}}),
        ]
        output = io.StringIO()
        with redirect_stdout(output):
            print_result(ScanResult(sample, [], 0))
        result = output.getvalue()
        self.assertIn("Conditional checks skipped: UNWIND_DEPTH=2", result)
        self.assertIn("Observed OI 60m: n=2 | median +0.50%", result)
        self.assertNotIn("UNWIND_DEPTH=2", result.split("Conditional checks skipped:")[0])

    def test_independent_gate_failures_in_scan_summary(self):
        from chronos.models import Assessment
        a = Assessment("BTCUSDT", 1, "WAIT", "open_interest_not_expanding",
                       metrics={"gate_checks": {"OI_60M": "FAIL", "FUNDING": "FAIL"}})
        output = io.StringIO()
        with redirect_stdout(output):
            print_result(ScanResult([a], [], 0))
        self.assertIn("Independent gate failures:", output.getvalue())
        self.assertIn("OI_60M=1", output.getvalue())
        self.assertIn("FUNDING=1", output.getvalue())

    def test_completed_score_remains_numeric(self):
        a = evaluate(fixture(), Settings())
        self.assertEqual(a.verdict, "SIGNAL")
        self.assertNotEqual(score_label(a), "-")

    def test_low_funding_wait_has_numeric_diagnostic(self):
        a = evaluate(fixture(funding=0.000001), Settings())
        self.assertEqual(score_label(a), "-")
        self.assertIn("funding +0.0001%", reason_label(a))


class BybitTests(unittest.TestCase):
    def test_global_bybit_default_and_no_regional_fallback(self):
        self.assertEqual(Settings().base_url, "https://api.bybit.com")
        with patch.dict("os.environ", {"CHRONOS_BYBIT_BASE_URL": "https://api.bybit.com"}):
            self.assertEqual(Settings.from_env().base_url, "https://api.bybit.com")
        self.assertEqual(BybitClient().base_url, "https://api.bybit.com")

    def test_http_404_identifies_host_without_fallback(self):
        from urllib.error import HTTPError
        from io import BytesIO
        with patch("chronos.bybit.request.urlopen", side_effect=HTTPError(
                "https://api.bybit.com/v5/market/instruments-info",404,"Not Found",{},BytesIO())):
            with self.assertRaises(BybitDataError) as cm:
                BybitClient().active_symbols()
        self.assertIn("api.bybit.com",str(cm.exception))
        self.assertIn("HTTP 404",str(cm.exception))

    def test_private_endpoint_rejected(self):
        with self.assertRaises(ValueError):
            BybitClient().get("/v5/order/create")

    def test_https_required(self):
        with self.assertRaises(ValueError):
            BybitClient("http://localhost")

    def test_instrument_tradfi_filter(self):
        self.assertTrue(crypto_underlying({"symbol": "BTCUSDT", "marketRegion": "", "underlyingTicker": ""}))
        self.assertFalse(crypto_underlying({"symbol": "NVDAUSDT", "marketRegion": "US"}))
        self.assertFalse(crypto_underlying({"symbol": "XAUUSDT", "underlyingTicker": "XAU"}))
        self.assertFalse(crypto_underlying({"symbol": "STOCKUSDT", "symbolType": "xstocks"}))
        self.assertTrue(crypto_underlying({"symbol": "NEWCOINUSDT"}))

    def test_bybit_enum_types_and_missing_tradfi_metadata(self):
        for symbol_type in ("commodity", "stock", "forex", "ETF", "xstocks", "mstocks"):
            with self.subTest(symbol_type=symbol_type):
                self.assertFalse(crypto_underlying({"symbol": "EXAMPLEUSDT", "symbolType": symbol_type}))
        for symbol in ("XAUUSDT", "XAGUSDT", "CLUSDT", "NVDAUSDT", "SOXLUSDT"):
            with self.subTest(symbol=symbol):
                self.assertFalse(crypto_underlying({"symbol": symbol}))
        for symbol in ("XAUTUSDT", "PAXGUSDT", "BTCUSDT", "ETHUSDT"):
            with self.subTest(symbol=symbol):
                self.assertTrue(crypto_underlying({"symbol": symbol}))
        self.assertTrue(crypto_underlying({"symbol": "NEWUSDT", "symbolType": "innovation"}))

    def test_tradfi_excluded_from_global_ticker_ranking(self):
        class Fake(BybitClient):
            def get(self, path, **params):
                if path.endswith("instruments-info"):
                    return {"list": [
                        {"symbol": "BTCUSDT", "quoteCoin": "USDT", "status": "Trading", "contractType": "LinearPerpetual", "marketRegion": "", "underlyingTicker": ""},
                        {"symbol": "NVDAUSDT", "quoteCoin": "USDT", "status": "Trading", "contractType": "LinearPerpetual", "marketRegion": "US"},
                        {"symbol": "XAUUSDT", "quoteCoin": "USDT", "status": "Trading", "contractType": "LinearPerpetual", "underlyingTicker": "XAU"},
                    ]}
                return {"list": [
                    {"symbol": "BTCUSDT", "turnover24h": "90000000", "lastPrice": "100", "fundingRate": "0.0001", "nextFundingTime": "1800000000000", "openInterest": "100"},
                    {"symbol": "NVDAUSDT", "turnover24h": "900000000", "lastPrice": "100", "fundingRate": "0.0001", "nextFundingTime": "1800000000000", "openInterest": "100"},
                    {"symbol": "XAUUSDT", "turnover24h": "900000000", "lastPrice": "100", "fundingRate": "0.0001", "nextFundingTime": "1800000000000", "openInterest": "100"}
                ]}
        client = Fake()
        self.assertEqual([r["symbol"] for r in client.universe(10)], ["BTCUSDT"])
        self.assertEqual(client.excluded_tradfi, 2)

    def test_universe_ignores_inactive_and_spot(self):
        class Fake(BybitClient):
            def get(self,path,**params):
                if path.endswith("instruments-info"):
                    return {"list":[
                        {"symbol":"BTCUSDT","quoteCoin":"USDT","status":"Trading","contractType":"LinearPerpetual"},
                        {"symbol":"ETHUSDT","quoteCoin":"USDT","status":"Settled","contractType":"LinearPerpetual"}
                    ]}
                return {"list":[
                    {"symbol":"BTCUSDT","turnover24h":"90000000","lastPrice":"100",
                     "fundingRate":"0.0001","nextFundingTime":"1800000000000","openInterest":"100"},
                    {"symbol":"ETHUSDT","turnover24h":"90000000","lastPrice":"100",
                     "fundingRate":"0.0001","nextFundingTime":"1800000000000","openInterest":"100"}
                ]}
        self.assertEqual([x["symbol"] for x in Fake().universe(10)], ["BTCUSDT"])

    def test_kline_only_closed(self):
        now=1_800_000_000_000
        class Fake(BybitClient):
            def get(self,path,**params):
                if path.endswith("kline"):
                    return {"list":[
                        [str(now-100000),"10","11","9","10","5"],
                        [str(now-600000),"10","11","9","10","5"]]}
                if path.endswith("open-interest"):
                    return {"list":[{"timestamp":str(now-1000000),"openInterest":"100"}]}
                if path.endswith("funding/history"):
                    return {"list":[{"fundingRate":"0.0001"}]}
                return {"b":[["10","100"]],"a":[["10.1","100"]]}
        with patch("chronos.bybit.time.time",return_value=now/1000):
            snap=Fake().snapshot({"symbol":"BTCUSDT","turnover":1e8,"price":10,
                                  "funding":0.001,"next_funding_ms":now+1e6,"ticker_oi":100})
        self.assertEqual(len(snap.candles),1)
        self.assertEqual(snap.candles[0].start_ms,now-600000)


class StorageReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(str(Path(self.temp.name)/"chronos.sqlite3"))
        self.cfg=Settings()
    def tearDown(self):
        self.store.close();self.temp.cleanup()

    def test_dedupe_and_roundtrip(self):
        s=fixture();a=evaluate(s,self.cfg)
        self.assertTrue(self.store.record(s,a))
        self.assertFalse(self.store.record(s,a))
        self.assertEqual(self.store.count()["signals"],1)
        self.assertEqual(self.store.count()["observations"],1)
        self.assertGreater(self.store.count()["candles"],0)

    def test_forward_only_replay_and_stop_collision(self):
        s=fixture();a=evaluate(s,self.cfg)
        self.assertTrue(self.store.record(s,a))
        sig=self.store.recent_signals()[0]
        self.assertEqual(replay_signal(self.store,sig,self.cfg)["status"],"PENDING")
        start=(s.observed_ms//300_000+1)*300_000
        entry=sig["entry"]
        with self.store.db:
            self.store.db.execute("INSERT INTO candles VALUES (?,?,?,?,?,?,?)",
                ("BTCUSDT",start,entry,sig["stop"]+1,sig["target"]-1,entry,1000))
        result=replay_signal(self.store,sig,self.cfg)
        self.assertEqual(result["status"],"STOP")
        self.assertLess(result["net_r"],0)

    def test_replay_no_hindsight(self):
        s=fixture();a=evaluate(s,self.cfg);self.store.record(s,a)
        summary=replay_all(self.store,self.cfg)
        self.assertEqual(summary["finished"],0)
        self.assertIsNone(summary["win_rate"])


if __name__=="__main__":
    unittest.main()
