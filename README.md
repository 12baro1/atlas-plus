# CHRONOS — Funding-time position carrying pressure research

**Independent from Atlas. Uses Bybit V5 public market data only. No private keys, auto-execution, or order submission.**

CHRONOS tests a specific and falsifiable hypothesis: when open interest has expanded while price has stalled, and a large funding payment is approaching, the side paying funding *may* have an increasing incentive to reduce exposure. A possible unwind can matter more if nearby order-book depth is relatively thin. This is a hypothesis, **not proof of positioning direction, a calibrated exit probability or profitability**.

The project deliberately contains **no Atlas SMC, indicators-as-strategies, execution engines, Android APK code, or Gate.io adapters**. Architecture patterns were reconsidered from the original Atlas project, but CHRONOS implementation is independent.

## Supported data and decisions

- Public Bybit V5: linear USDT perpetual instruments, 24h tickers, closed 5-minute candles, 5-minute historical OI, historical funding observations, and 50-level order books.
- Enumerate **global Bybit USDT crypto linear perpetual symbols** from the global API host, excluding instruments explicitly identified as traditional-market underlyings through V5 `marketRegion`, `underlyingTicker` or `xstocks` metadata; rank by 24h turnover and scan a bounded top-N selection (default minimum 24h turnover is 20 million USDT; lower `CHRONOS_MIN_TURNOVER` to include smaller coins). Instruments without enough classification metadata remain eligible.
- Show **all symbols analyzed** in one-shot CLI output, plus counts for WAIT reasons and independent prerequisite failures (OI, funding, time window, book depth, spread, price stall). **Conditional UNWIND_DEPTH is SKIP, not FAIL, until sufficient OI growth, funding pressure, depth and spread prerequisites pass**. This avoids treating falling OI as a negative forced liquidation estimate. One-shot reports also show measured OI 60m distribution (median/min/max) for research. Gate diagnostics never override the strategy decision. The number of eligible symbols may be smaller than `--limit` when daily turnover filters out others.
- Use V5 `symbolType` enums (`commodity`, `stock`, `forex`, `ETF`) and known TradFi contract identifiers as fallback to exclude metal, stock and ETF contracts. Bybit may add new asset types, so this is not a certified exhaustive classification. No entry/score thresholds are reduced merely to manufacture signals.
- Estimate OI expansion vs 60 minutes earlier; **OI is two-sided and does not tell which side opened which trades**.
- Measure funding magnitude relative to recent funding; consider time until next funding, normalized price stall, and *assumed* partial unwind size relative to nearby depth.
- Return `WAIT` with a specific reason, or a *research-only* `LONG`/`SHORT` hypothesis with entry reference, stop, target, gross RR, and **estimated net RR including taker fees, half-spread and slippage**.
- Save all completed candles, assessments and deduplicated signals to SQLite (one new hypothesis per symbol/side/funding event); emit optional one-way Telegram alerts.
- Keep a compact terminal dashboard. Replay observed signals using **subsequently archived** candle highs/lows, conservative stop-first collisions and next-candle entry/deviation guard.

## Quick start — Android UserLAnd (Ubuntu) / Linux

Requirements: Python **3.10+**, network access to the Bybit API from your account/region, and free storage space.

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git tmux
# Use HTTPS/SSH GitHub remote matching YOUR repository:
git clone https://github.com/12baro1/atlas-plus.git
cd atlas-plus
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt   # Rich UI, optional
python -m chronos doctor
python -m chronos once --limit 5
python -m chronos run --limit 20 --interval 900
```

To keep a terminal session:

```bash
tmux new -s chronos
source .venv/bin/activate
python -m chronos run --limit 20 --interval 900
# Detach: Ctrl+B then D ; reattach: tmux attach -t chronos
```

On Android, UserLAnd/tmux **cannot guarantee uninterrupted background operation**; Android may kill the process. SQLite WAL is used to minimize corruption after interruptions. Avoid 1-minute scans over large symbol counts: Bybit rate limits apply.

### Configuration

Edit `.env.example` for reference; export needed variables in your shell or `source` a locally created and trusted `.env` with `set -a`/`set +a`. Python **does not implicitly execute `.env`**. Never commit a file containing a bot token.

```bash
export CHRONOS_BYBIT_BASE_URL=https://api.bybit.com
export CHRONOS_SCAN_LIMIT=20
export CHRONOS_DB=$HOME/chronos.sqlite3
# Optional Telegram one-way signal messages:
export CHRONOS_TELEGRAM_TOKEN='...'
export CHRONOS_TELEGRAM_CHAT_ID='...'
```

CHRONOS is configured to enumerate **global Bybit V5 USDT linear perpetual contracts** from `https://api.bybit.com`, rather than a Türkiye-specific market selection. This public, read-only endpoint may be inaccessible from some networks or jurisdictions. Per [Bybit's official integration guide](https://bybit-exchange.github.io/docs/v5/guide), Türkiye-account users are assigned a regional host for account API activity; CHRONOS does **not** use that regional host as a fallback because the requested data universe is global. If `doctor` reports HTTP 403 or 404, confirm the actual availability and lawful access of the chosen endpoint from UserLAnd. The code does not attempt to bypass geographic restrictions. The public market-data API should not require keys. The code intentionally never calls private endpoints.

### Commands

| Command | Description |
|---|---|
| `python -m chronos doctor` | Check read-only API / instruments / SQLite |
| `python -m chronos once --limit 20` | Single bounded scan; archive observations |
| `python -m chronos run --limit 20 --interval 900` | Continuously scan in Rich fullscreen terminal |
| `python -m chronos run --plain` | Stream readable text without Rich |
| `python -m chronos report` | List historical signal hypotheses |
| `python -m chronos replay` | Forward-only replay when **future closed bars have been archived** |
| `python -m unittest discover -s tests -v` | Run offline deterministic unit tests |

### Signals and trading costs

The `score/100` is a **heuristic index, not a probability**. An entry reference is the midpoint of the observed top of book; next-bar fills can differ. A stop is the greater of ATR × 1.25, 0.2% of mid, and 3×spread. The target is an independently calculated volatility/estimated-depth-pressure scenario (not pushed out merely to pass an RR filter). Default trading friction assumes **two taker executions** plus per-leg slippage and half-spread. Fees and funding intervals may differ by contract/VIP status. Funding settlement paid or credited during the hold is **not included** in estimated net RR; it must be modeled before relying on performance results. Note that the first version uses static default cost assumptions, not symbol-specific live fee rates.

The source of directional inference is *funding sign*: positive funding suggests examining potential LONG pressure (SHORT hypothesis); negative funding suggests examining SHORT pressure (LONG hypothesis). Neither is sufficient without OI growth, limited price displacement, proximity to funding, adequate depth and net RR. The assumed unwind fraction (3% of 60-minute OI increase) is a **research parameter, not measured liquidations**.

### Replay limitations

`replay` is a preliminary **paper journal**, not proof of positive edge. It does not reconstruct historic order books or simulate queued orders, fees that change by volume tier, actual funding cashflows, latency, or outages. The candle archive is populated on active scans; if future candles are missing, results remain pending or unfilled rather than invented. High and low in the same bar are always evaluated as hitting stop first. A real strategy requires clean, forward out-of-sample testing and proper benchmark comparison against simple funding/OI rules before enabling any execution features.

### Planned research milestones

1. Collect sufficient time-stamped real Bybit snapshots across different market conditions.
2. Fit / calibrate an actual exit probability from labeled observations, instead of treating the heuristic score as predictive.
3. Model true funding payments, symbol-level execution costs, and missing data before comparison with baseline strategies.
4. Walk-forward validation by time, instrument and volatility regime, followed by independent paper trading.

**No guarantee of profit. No financial or trade execution actions are implemented.**
