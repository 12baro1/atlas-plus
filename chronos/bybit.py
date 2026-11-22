"""Public-only Bybit V5 market-data adapter; stdlib networking for UserLAnd.

Only documented GET /v5/market/* endpoints are callable. No credentials,
order submission, websocket dependence, or automatic regional fallbacks.
"""
from __future__ import annotations
import json
import math
import time
from urllib import error, parse, request
from .models import Candle, MarketSnapshot

ALLOWED_ENDPOINTS = frozenset({
    "/v5/market/tickers", "/v5/market/instruments-info", "/v5/market/kline",
    "/v5/market/open-interest", "/v5/market/funding/history", "/v5/market/orderbook",
    "/v5/market/time",
})


class BybitDataError(RuntimeError):
    pass


def positive(value: object, name: str, allow_zero: bool = False) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise BybitDataError(f"Invalid {name}") from None
    if not math.isfinite(result) or result < 0 or (result == 0 and not allow_zero):
        raise BybitDataError(f"Invalid {name}")
    return result


# Known traditional-asset contracts, used only when exchange metadata is absent.
# Do NOT block gold-backed crypto tokens (e.g. XAUT or PAXG) by substring.
KNOWN_TRADFI_CONTRACTS = frozenset({
    "XAUUSDT", "XAGUSDT", "CLUSDT", "NVDAUSDT", "SOXLUSDT",
})
TRADFI_SYMBOL_TYPES = frozenset({
    "commodity", "stock", "forex", "etf", "xstocks", "mstocks",
})


def crypto_underlying(instrument: dict) -> bool:
    """Limit the scan to crypto underlyings, retaining new crypto listings.

    Bybit V5 officially separates TradFi via symbolType, marketRegion and
    underlyingTicker. Some old metal instruments may omit the newer metadata,
    so exclude a short list of identified underlying contracts as a fallback.
    """
    symbol = str(instrument.get("symbol") or "").upper().strip()
    kind = str(instrument.get("symbolType") or "").lower().strip()
    if kind in TRADFI_SYMBOL_TYPES or symbol in KNOWN_TRADFI_CONTRACTS:
        return False
    if str(instrument.get("marketRegion") or "").strip():
        return False
    if str(instrument.get("underlyingTicker") or "").strip():
        return False
    return True


class BybitClient:
    def __init__(self, base_url: str = "https://api.bybit.com", timeout: float = 9.0):
        if not base_url.startswith("https://"):
            raise ValueError("Bybit API base URL must use HTTPS")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._instrument_cache: tuple[float, set[str]] | None = None
        self.excluded_tradfi = 0

    def get(self, endpoint: str, **params: object) -> dict:
        if endpoint not in ALLOWED_ENDPOINTS:
            raise ValueError(f"Private/unknown exchange endpoint disallowed: {endpoint}")
        qs = parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = self.base_url + endpoint + ("?" + qs if qs else "")
        last_error = ""
        for attempt in range(3):
            try:
                req = request.Request(url, headers={"User-Agent": "ChronosResearch/0.1", "Accept": "application/json"})
                with request.urlopen(req, timeout=self.timeout) as response:
                    payload = json.load(response)
                if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
                    raise BybitDataError("Malformed Bybit response")
                code = payload.get("retCode")
                if code != 0:
                    message = str(payload.get("retMsg", "unknown"))
                    # transient server / rate-limit errors; permission/region failures are terminal
                    if code in {10006, 10016} and attempt < 2:
                        time.sleep(0.5 * (2 ** attempt))
                        continue
                    raise BybitDataError(f"Bybit API retCode={code}: {message}")
                return payload["result"]
            except error.HTTPError as exc:
                last_error = f"HTTP {exc.code} for {endpoint} at {self.base_url}"
                if exc.code == 404:
                    raise BybitDataError(
                        last_error + "; endpoint unavailable on this host. "
                        "CHRONOS requires Bybit global V5 market data; "
                        "check CHRONOS_BYBIT_BASE_URL and network access."
                    ) from exc
                if exc.code == 403:
                    raise BybitDataError(
                        last_error + "; access denied from this network/region. "
                        "CHRONOS will not silently switch exchange hosts."
                    ) from exc
                if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise BybitDataError(last_error) from exc
            except (error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_error = f"Bybit connection error for {endpoint}: {exc}"
                if attempt == 2:
                    raise BybitDataError(last_error) from exc
            time.sleep(0.5 * (2 ** attempt))
        raise BybitDataError(last_error or "Bybit request failed")

    def active_symbols(self) -> set[str]:
        now = time.monotonic()
        if self._instrument_cache and now - self._instrument_cache[0] < 1200:
            return self._instrument_cache[1]
        cursor: str | None = None
        symbols: set[str] = set()
        excluded: set[str] = set()
        for _ in range(15):
            data = self.get("/v5/market/instruments-info", category="linear", limit=1000, cursor=cursor)
            for row in data.get("list") or []:
                if (row.get("quoteCoin") == "USDT" and row.get("status") == "Trading"
                        and row.get("contractType") == "LinearPerpetual"):
                    symbol = str(row.get("symbol"))
                    if crypto_underlying(row):
                        symbols.add(symbol)
                    else:
                        excluded.add(symbol)
            new_cursor = data.get("nextPageCursor") or None
            if not new_cursor or new_cursor == cursor:
                break
            cursor = new_cursor
        if not symbols:
            raise BybitDataError("No active USDT perpetual instruments returned")
        self.excluded_tradfi = len(excluded)
        self._instrument_cache = (now, symbols)
        return symbols

    def universe(self, limit: int = 20, min_turnover: float = 20_000_000.0) -> list[dict]:
        active = self.active_symbols()
        rows = self.get("/v5/market/tickers", category="linear").get("list") or []
        parsed = []
        for row in rows:
            try:
                symbol = str(row.get("symbol", ""))
                turnover = positive(row.get("turnover24h"), "turnover", allow_zero=True)
                price = positive(row.get("lastPrice"), "price")
                funding = float(row.get("fundingRate"))
                next_funding = int(row.get("nextFundingTime"))
                if not math.isfinite(funding) or next_funding <= 0:
                    continue
                if symbol not in active or turnover < min_turnover:
                    continue
                parsed.append({"symbol": symbol, "turnover": turnover, "price": price,
                               "funding": funding, "next_funding_ms": next_funding,
                               "ticker_oi": positive(row.get("openInterest"), "OI", allow_zero=True)})
            except (BybitDataError, TypeError, ValueError, OverflowError):
                continue
        return sorted(parsed, key=lambda row: row["turnover"], reverse=True)[:limit]

    def snapshot(self, ticker: dict) -> MarketSnapshot:
        symbol = ticker["symbol"]
        raw_candles = self.get("/v5/market/kline", category="linear", symbol=symbol, interval="5", limit=100).get("list") or []
        raw_oi = self.get("/v5/market/open-interest", category="linear", symbol=symbol, intervalTime="5min", limit=40).get("list") or []
        raw_funding = self.get("/v5/market/funding/history", category="linear", symbol=symbol, limit=30).get("list") or []
        raw_book = self.get("/v5/market/orderbook", category="linear", symbol=symbol, limit=50)
        observed_ms = int(time.time() * 1000)
        candles: list[Candle] = []
        for r in raw_candles:
            try:
                ts = int(r[0]); o,h,l,c,v = (positive(x, "candle", allow_zero=i == 4) for i,x in enumerate(r[1:6]))
                if ts + 300_000 > observed_ms or h < max(o,c,l) or l > min(o,c,h):
                    continue
                candles.append(Candle(ts,o,h,l,c,v))
            except (IndexError, TypeError, ValueError, BybitDataError):
                continue
        candles = sorted({c.start_ms: c for c in candles}.values(), key=lambda x: x.start_ms)
        oi = []
        for r in raw_oi:
            try:
                oi.append((int(r["timestamp"]), positive(r["openInterest"], "open interest")))
            except (KeyError, TypeError, ValueError, BybitDataError):
                pass
        oi = sorted(set(oi))
        funding = []
        for r in raw_funding:
            try:
                value = float(r["fundingRate"])
                if math.isfinite(value):
                    funding.append(value)
            except (KeyError, TypeError, ValueError):
                pass

        def levels(field: str) -> tuple[tuple[float, float], ...]:
            output = []
            for r in raw_book.get(field) or []:
                try:
                    p = positive(r[0], "level price")
                    q = positive(r[1], "level quantity")
                    output.append((p, q))
                except (IndexError, TypeError, BybitDataError):
                    pass
            return tuple(output)

        return MarketSnapshot(
            symbol=symbol, observed_ms=observed_ms, price=ticker["price"],
            funding_rate=ticker["funding"], next_funding_ms=ticker["next_funding_ms"],
            turnover_24h=ticker["turnover"], open_interest=tuple(oi),
            funding_history=tuple(funding), candles=tuple(candles),
            bids=levels("b"), asks=levels("a"), ticker_open_interest=ticker["ticker_oi"],
        )
