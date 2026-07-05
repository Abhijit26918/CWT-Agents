# CWT Crypto Predictions Agent — Technical Deep Dive

> Interview preparation document. Covers stack, architecture, data flow,
> database schema, algorithms, API integrations, design decisions, and
> likely interview questions.

---

## 1. What the Project Does (30-second version)

Every 5 minutes, the system:
1. Reads the current BTC/ETH up/down prediction market odds from Polymarket and Kalshi
2. Fetches the last 1000 candlestick bars from Binance (via Apify)
3. Runs a K-line ML model (Kronos) 30 times (Monte Carlo) → gets P(up) for the next bar.
4. Compares model probability to market price → if edge exists, sizes a paper bet using Kelly criterion
5. After each window closes, scores past bets, computes calibration metrics, and adjusts bet sizing

It's a **research pipeline**, not a live trading bot — all bets are paper (simulated).

---

## 2. Full Tech Stack

| Layer | Technology | Why |
|---|---|---|
| Language | Python 3.10 | Torch + ML ecosystem |
| Framework | Hermes Agent (Nous Research) | Required by assignment; CLI agent with plugin + skill system |
| LLM | OpenRouter → deepseek/deepseek-chat-v3.1:free | Free tier, tool-calling, ≥64K context |
| ML Model | Kronos-mini (4M params, AAAI 2026) | Purpose-built K-line (candlestick) foundation model |
| Data Fetch | Apify → parseforge/binance-prices-scraper | Assignment requirement; wraps Binance public klines |
| Markets | Polymarket (Gamma + CLOB API), Kalshi Trade API v2 | The two prediction markets in the brief |
| Database | SQLite (via stdlib sqlite3) | Zero-ops, file-based, no server needed |
| Config | YAML (pyyaml) + .env (python-dotenv) + Pydantic v2 | Typed settings, secrets separated from config |
| Dashboard | Streamlit | Rapid UI for live predictions + scoreboard |
| Testing | pytest + responses (HTTP mocking) | 68 tests, fully offline |
| Logging | Python logging + RotatingFileHandler | Per-run `run_id`, audit trail |
| Validation | Pydantic BaseModel | Typed AppConfig with nested models |

---

## 3. Repository Structure (with purpose)

```
d:\intern Assignments\
│
├── run_flow.py              # Headless entry point (no LLM)
│                            # Flags: --dry, --cache, --demo, --loop --interval N
│
├── config.yaml              # Business config (assets, model ids, risk params)
├── .env                     # Secrets (APIFY_TOKEN, OPENROUTER_API_KEY) — git-ignored
├── .env.example             # Template committed to repo
├── requirements.txt         # Python deps
├── pytest.ini               # testpaths = tests (excludes vendor/)
│
├── core/                    # ALL business logic — pure Python, no LLM, fully testable
│   ├── config.py            # load_config() → AppConfig (Pydantic), load_settings()
│   ├── db.py                # init_db() → sqlite3.Connection, idempotent schema
│   ├── logging_setup.py     # setup_logging() → run_id (UUID prefix on every log line)
│   ├── pipeline.py          # run_once(cfg, conn, ...) → RunReport — orchestrates all agents
│   │
│   ├── markets/
│   │   ├── __init__.py      # MarketData dataclass, MarketNotIndexed exception
│   │   ├── polymarket.py    # Agent 1a: slug builder, Gamma API, CLOB midpoint
│   │   └── kalshi.py        # Agent 1b: series→event→market discovery, strike-ladder parsing
│   │
│   ├── data/
│   │   └── apify_ohlcv.py   # Agent 2: Apify fetch, normalize, cache, persist
│   │
│   ├── predict/
│   │   └── kronos_model.py  # Agent 3: singleton model load, MC P(up), 512-bar cap
│   │
│   ├── risk/
│   │   └── kelly.py         # Agent 4: edge, f* formula, fractional Kelly, clamping
│   │
│   ├── feedback/
│   │   └── scoring.py       # Agent 5: resolve outcomes, Brier score, Kelly recalibration
│   │
│   └── scale/
│       └── arbitrage.py     # Cross-venue arb, cross-horizon consistency check
│
├── hermes/
│   ├── plugins/crypto-predictions/
│   │   ├── plugin.yaml      # Manifest: tools, hooks, required env vars
│   │   ├── __init__.py      # register(ctx) → wires schemas→handlers
│   │   ├── schemas.py       # JSON Schema for each tool (what the LLM sees)
│   │   └── tools.py         # Thin handlers: validate → call core/ → return JSON
│   │
│   └── skills/crypto-prediction-flow/
│       └── SKILL.md         # /crypto-flow: orchestration recipe for Hermes
│
├── dashboard/
│   └── app.py               # Streamlit: live predictions + scoreboard + PnL curve
│
├── vendor/
│   └── Kronos/              # git-cloned, git-ignored (not committed)
│
└── tests/
    ├── fixtures/            # Recorded JSON responses (Polymarket, Kalshi, Apify)
    ├── test_skeleton.py     # Config load + DB schema creation
    ├── test_polymarket_slug.py  # Slug math + mocked Gamma/CLOB calls
    ├── test_kalshi.py       # Series discovery + odds parsing
    ├── test_apify_ohlcv.py  # Normalization + fake Apify client
    ├── test_kronos.py       # MC P(up) logic with FakePredictor
    ├── test_kelly.py        # Pure Kelly math (edge, f*, clamping)
    ├── test_pipeline_fakes.py  # End-to-end with all fakes → green-build gate
    ├── test_feedback.py     # Resolve, Brier, Kelly shrink/recover
    └── test_arbitrage.py    # Cross-venue + cross-horizon arb math
```

---

## 4. Database Schema (SQLite) + ER Diagram

```sql
markets          ohlcv               predictions
────────         ─────────           ───────────
id (PK)          asset               id (PK)
ts_window        interval            ts
asset            open_time (PK)      asset
venue            open                venue
horizon          high                horizon
up_ref           low                 model_p_up
down_ref         close               market_p_up
implied_up       volume              edge
implied_down     amount              side
window_close_ts  source              kelly_fraction
fetched_at                           stake_paper
                                     window_close_ts
                                     status (OPEN/RESOLVED)
                                     created_at

outcomes                calibration          runs
────────                ───────────          ────
prediction_id (PK,FK)  asset (PK)           id (PK)
resolved_at            n                    started_at
actual_direction       brier                finished_at
won (0/1)              hit_rate             n_markets
pnl_paper              kelly_multiplier     n_predictions
                        updated_at           notes
```

**Relationships:**
```
predictions ──(1:1)──► outcomes        (one outcome per resolved prediction)
predictions ──(N:1)──► calibration     (many predictions per asset → one calibration row)
```

**Key design choices:**
- `PRIMARY KEY (asset, interval, open_time)` on ohlcv → natural deduplication
- `INSERT OR IGNORE` on ohlcv inserts → safe to call `fetch_ohlcv` multiple times
- `calibration.asset` is a string PK → one row per asset, `UPSERT` on update
- `outcomes.prediction_id` references `predictions.id` → enforces data integrity

---

## 5. The 5-Agent Data Flow

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         run_once() per 5-minute cycle                   │
└─────────────────────────────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────┐
│  FEEDBACK FIRST  │  score_predictions(conn, cfg)
│  (Agent 5 runs   │  → find OPEN predictions where window_close_ts ≤ now
│  at the START)   │  → look up actual direction from ohlcv table
│                  │  → write outcomes, update calibration, adjust Kelly
└────────┬─────────┘
         │
         ▼  FOR EACH ASSET (BTC, ETH)
┌──────────────────┐
│  AGENT 2         │  fetch_ohlcv(asset, symbol, interval="5m", limit=1000)
│  Data Agent      │  → Apify actor (parseforge/binance-prices-scraper)
│                  │  → Normalize: openTime(ms→s), capitalize→lowercase cols
│                  │  → Compute amount = close × volume if missing
│                  │  → Sort ascending, persist to ohlcv (INSERT OR IGNORE)
│                  │  → Cache to .cache/ohlcv_{symbol}_{interval}.json
└────────┬─────────┘
         │  df: 1000 rows × [open_time, open, high, low, close, volume, amount]
         ▼
┌──────────────────┐
│  AGENT 3         │  predict_move(df, cfg, predictor=None)
│  Prediction      │  → Slice to last cfg.kronos.lookback rows (≤512)
│  Agent           │  → Build x_timestamp (DatetimeIndex from open_time seconds)
│                  │  → Build y_timestamp (next bar = last open_time + 300s)
│                  │  → Loop N=30 times (Monte Carlo):
│                  │      pred = predictor.predict(df, x_ts, y_ts, pred_len=1,
│                  │                               T=1.0, top_p=0.9, sample_count=1)
│                  │      if pred["close"].iloc[-1] > last_close: ups += 1
│                  │  → p_up = ups / N  (float in [0, 1])
└────────┬─────────┘
         │  p_up: float
         ▼  FOR EACH VENUE (polymarket, kalshi)
┌──────────────────┐
│  AGENT 1         │  find_market(asset, horizon="5m")
│  Market Agent    │
│                  │  Polymarket:
│                  │  → window_start = floor(now / 300) × 300
│                  │  → slug = f"{asset.lower()}-updown-5m-{window_start}"
│                  │  → GET gamma-api.polymarket.com/events?slug={slug}
│                  │  → Parse markets[0].clobTokenIds (JSON string → list)
│                  │  → GET clob.polymarket.com/midpoint?token_id={up_token}
│                  │  → implied_up = float(response["mid"])
│                  │  → Retry previous window if slug not found yet
│                  │
│                  │  Kalshi:
│                  │  → GET /series?category=Crypto → find KXBTCD/KXETHD
│                  │  → GET /events?series_ticker=KXBTCD&status=open
│                  │  → Sort by strike_date → soonest event
│                  │  → GET /markets?event_ticker={ticker}
│                  │  → Find "at the money" market: min(|mid - 0.5|)
│                  │  → implied_up = (yes_bid_dollars + yes_ask_dollars) / 2
└────────┬─────────┘
         │  MarketData: implied_up, implied_down, window_close_ts
         ▼
┌──────────────────┐
│  AGENT 4         │  size_position(p_up, implied_up, implied_down, ...)
│  Risk Agent      │
│                  │  For UP side:
│                  │    edge_up = p_up - implied_up - fee
│                  │    f_up = (p_up - implied_up) / (1 - implied_up)
│                  │
│                  │  For DOWN side:
│                  │    p_down = 1 - p_up
│                  │    edge_down = p_down - implied_down - fee
│                  │    f_down = (p_down - implied_down) / (1 - implied_down)
│                  │
│                  │  Pick side with max edge (if both ≤ 0 → NONE)
│                  │  kelly_fraction = clamp(kelly_multiplier × f*, 0, f_max)
│                  │  stake_paper = kelly_fraction × bankroll
│                  │
│                  │  → INSERT INTO predictions (status='OPEN')
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│  RunReport       │  Printed as table:
│                  │  Asset | Venue | Model P(up) | Market P(up) | Edge | Side | Stake
└──────────────────┘

  [Next cycle — 5 minutes later]
         │
         ▼
  FEEDBACK AGENT runs again at top of next cycle:
  → Finds predictions whose window_close_ts ≤ now
  → Looks up OHLCV bar: open_time = window_close_ts - 300
  → actual_direction = "UP" if close > open else "DOWN"
  → won = (our side == actual_direction)
  → pnl = stake × (1-c)/c  if won  else  -stake
  → UPDATE predictions SET status='RESOLVED'
  → Compute Brier score, hit_rate per asset
  → Adjust kelly_multiplier (shrink if Brier > 0.30, recover if Brier < 0.22)
```

---

## 6. Key Algorithms Explained

### 6.1 Kelly Criterion

**Problem:** You have an edge in a binary bet. How much of your bankroll should you bet to maximise long-run growth?

**Setup:**
- You buy a contract that pays $1 if the outcome is YES
- The contract costs `c` (= market implied probability, e.g. 0.50)
- Your model says the true probability is `p` (e.g. 0.60)
- Your edge is `p - c = 0.10`

**Kelly fraction:**
```
f* = (p - c) / (1 - c)
```
Example: (0.60 - 0.50) / (1 - 0.50) = 0.10 / 0.50 = **20%**

**Why we use quarter-Kelly (×0.25):**
Full Kelly maximises geometric growth in theory, but is extremely aggressive — a few miscalibrated predictions can wipe out a large fraction of the bankroll. Quarter-Kelly sacrifices some growth rate for much lower variance.

**Clamping:** We also cap at `f_max = 10%` regardless of Kelly output, and subtract fees before declaring an edge. No edge after fees → NO TRADE.

**DOWN side:** Symmetric. p_down = 1 - p_up, and f* = (p_down - c_down) / (1 - c_down).

### 6.2 Brier Score

Measures **calibration** — are our probabilities honest?

```
Brier = mean((p_up - actual_outcome)²)
```
Where `actual_outcome = 1` if the move was UP, `0` if DOWN.

- **Perfect calibration:** Brier = 0.0
- **Random guessing at 0.5:** Brier = 0.25
- **Completely wrong:** Brier = 1.0

Threshold: if Brier > 0.30, multiply Kelly multiplier by 0.80 (shrink bets by 20%).

### 6.3 Monte Carlo P(up)

Kronos is a **probabilistic** model — each inference has stochastic sampling (top-p = 0.9). To get a probability rather than a single price forecast:

```python
ups = 0
for _ in range(30):
    pred = predictor.predict(df=lookback, ..., T=1.0, top_p=0.9, sample_count=1)
    if pred["close"].iloc[-1] > last_close:
        ups += 1
p_up = ups / 30
```

Why 30 samples? Enough to estimate a stable probability (std error ≈ 0.09) without being too slow on CPU.

### 6.4 Polymarket Slug (Deterministic Market Discovery)

Polymarket 5-minute markets have slugs computed from the clock — no API scan needed:

```python
window_start = (int(time.time()) // 300) * 300
slug = f"btc-updown-5m-{window_start}"
```

At boundary (new window just opened), the slug might not be indexed for a few seconds. Solution: retry with previous window's slug.

### 6.5 Kalshi "At the Money" Selection

Kalshi provides a price ladder (e.g. "Will BTC be above $X?") not a single up/down market. To approximate P(up):

```python
def _atm_distance(market):
    mid = (yes_bid + yes_ask) / 2
    return abs(mid - 0.5)

best_market = min(all_markets, key=_atm_distance)
```

The market priced closest to 0.50 is the one nearest the current price — it gives the most informative signal about direction.

### 6.6 Cross-Venue Arbitrage

```
If |polymarket_implied_up - kalshi_implied_up| > 2 × fee:
    → BUY the underpriced venue, note the spread
```

Example: Polymarket prices BTC UP at 0.60, Kalshi at 0.50. Round-trip cost = 2 × 0.02 = 0.04. Spread = 0.10 > 0.04 → arb opportunity of 0.06.

---

## 7. API Integration Details

### 7.1 Polymarket

| Endpoint | Purpose |
|---|---|
| `GET gamma-api.polymarket.com/events?slug={slug}` | Find event by deterministic slug |
| `GET clob.polymarket.com/midpoint?token_id={id}` | Get implied prob from order book midpoint |

**Response parsing:**
- `event.markets[0].outcomes` → JSON string `'["Up","Down"]'` → `json.loads()`
- `event.markets[0].clobTokenIds` → JSON string `'["111","222"]'` → Up token is index 0
- `midpoint_response["mid"]` → string e.g. `"0.54"` → `float()`

**No auth required.** Public read-only.

### 7.2 Kalshi

| Endpoint | Purpose |
|---|---|
| `GET /series?category=Crypto` | Find BTC/ETH series tickers |
| `GET /events?series_ticker=KXBTCD&status=open` | List open events |
| `GET /markets?event_ticker={ticker}` | Get price ladder for event |

**Response quirks (learned from live testing):**
- Events have `event_ticker` field (not `ticker`)
- Events are sorted by `strike_date` (not `close_time`)
- Market prices: `yes_bid_dollars` / `yes_ask_dollars` in [0,1] range

**No auth required.** Public read-only.

### 7.3 Apify

```python
from apify_client import ApifyClient
client = ApifyClient(os.environ["APIFY_TOKEN"])
run = client.actor("parseforge/binance-prices-scraper").call(run_input={
    "mode": "klines", "symbol": "BTCUSDT", "interval": "5m", "limit": 1000
})
rows = list(client.dataset(run["defaultDatasetId"]).iterate_items())
```

**Normalization:**
- `openTime` (ms) → `open_time` (seconds): `value // 1000`
- Column rename: `Open→open`, `High→high`, `Low→low`, `Close→close`, `Volume→volume`
- Compute `amount = close × volume` if missing
- Sort ascending by `open_time`

### 7.4 Kronos (HuggingFace)

```python
from model import KronosTokenizer, Kronos, KronosPredictor  # from vendor/Kronos
tok = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-2k")
mdl = Kronos.from_pretrained("NeoQuasar/Kronos-mini")
predictor = KronosPredictor(mdl, tok, device="cpu", max_context=512)
```

**Key constraint:** Kronos-mini supports up to 2048 bars context. We feed the last 400 bars explicitly (rather than relying on silent truncation). Trained on Binance OHLCV in K-line format — exactly what we feed it.

**Input format:** DataFrame with columns `[open, high, low, close, volume, amount]` + DatetimeIndex for timestamps.

---

## 8. The Two Entry Points

```
┌─────────────────────────────────────────────────────┐
│                    core/*                           │
│  (business logic — deterministic, LLM-free)        │
└─────────────────────────────────────────────────────┘
              ▲                    ▲
              │                    │
   ┌──────────┴─────┐    ┌────────┴──────────────┐
   │  run_flow.py   │    │  Hermes /crypto-flow   │
   │  (headless)    │    │  (LLM-orchestrated)    │
   │                │    │                        │
   │  - No LLM      │    │  - LLM calls tools     │
   │  - For demo,   │    │  - /crypto-flow skill  │
   │    cron, CI    │    │    in SKILL.md          │
   │  - Always      │    │  - Same core/ logic    │
   │    works       │    │  - Needs Hermes CLI    │
   └────────────────┘    └────────────────────────┘
```

**Why two entry points?**
- LLMs can fail (rate limits, timeouts, model rotation on free tier)
- `run_flow.py` proves the pipeline works independent of the LLM
- The LLM in Hermes only does orchestration glue — not the actual computation
- This = "LLM-light" design: LLM cost ≈ $0

---

## 9. Hermes Plugin Architecture

```
plugin.yaml          → Declares: tools, hooks, required env vars
__init__.py          → register(ctx): wires schemas → handlers, injects shared state
schemas.py           → JSON Schema for each tool (what the LLM sees as a function)
tools.py             → handlers: validate input → call core/* → return JSON dict

SKILL.md             → /crypto-flow: step-by-step recipe the LLM follows
```

**Shared context (ctx dict):**
```python
ctx = {
    "cfg": AppConfig,          # loaded once
    "conn": sqlite3.Connection, # shared across tool calls
    "ohlcv": {},               # {asset: DataFrame} — filled by fetch_ohlcv
    "markets": {},             # {f"{asset}_{venue}": MarketData} — filled by find_markets
    "p_up": {},                # {asset: float} — filled by predict_move
}
```

The `post_tool_call` hook fires after every tool call and logs:
`TOOL find_markets | args={asset: BTC} | ok=True | latency=1250ms`

---

## 10. Testing Strategy (68 tests, all offline)

| Test file | What it tests | How |
|---|---|---|
| test_skeleton.py | Config loads, DB schema created | Real files, tmp DB |
| test_polymarket_slug.py | Slug math, JSON parsing, mocked HTTP | `responses` library |
| test_kalshi.py | Series discovery, odds parsing | `responses` library |
| test_apify_ohlcv.py | Normalization, FakeApifyClient, DB persist | FakeApifyClient |
| test_kronos.py | MC P(up) loop, lookback slice | FakePredictor |
| test_kelly.py | Edge math, f* formula, clamping | Pure Python |
| test_pipeline_fakes.py | End-to-end flow → SQLite rows | All fakes |
| test_feedback.py | Resolve, Brier, Kelly shrink | Seeded SQLite |
| test_arbitrage.py | Cross-venue + cross-horizon math | Pure Python |

**Injectable dependencies pattern:**
```python
def fetch_ohlcv(..., client=None):
    if client is None:
        client = ApifyClient(os.environ["APIFY_TOKEN"])  # real
    # use client (real or fake)
```

This means every external service is replaceable in tests — no network calls, no API keys needed.

---

## 11. Config System

`config.yaml` → loaded by `load_config()` → validated by Pydantic → typed `AppConfig`

```python
class AppConfig(BaseModel):
    assets: list[str]           # ["BTC", "ETH"]
    symbols: dict[str, str]     # {"BTC": "BTCUSDT"}
    horizon: str                # "5m"
    kronos: KronosConfig        # model, tokenizer, device, lookback, mc_samples
    apify: ApifyConfig          # actor id
    risk: RiskConfig            # bankroll, kelly_multiplier, f_max, fee
    llm: LlmConfig              # provider, model
    venues: list[str]           # ["polymarket", "kalshi"]
    mode: str                   # "paper"
    db_path: str                # "cwt.db"
```

Secrets (APIFY_TOKEN, OPENROUTER_API_KEY) are loaded separately via `.env` into `Settings` — never in `config.yaml`, never committed.

---

## 12. Scaling Features Built

### Cross-Venue Arbitrage
Compare Polymarket vs Kalshi for the same asset/horizon. If |spread| > 2×fee → signal.

```python
net_edge = abs(polymarket_implied_up - kalshi_implied_up) - 2 * fee
if net_edge > 0:
    → ArbSignal(buy cheaper venue, sell expensive venue)
```

### Cross-Horizon Consistency
A 15m UP probability should roughly equal three consecutive 5m UP probabilities:
```
P(15m up) ≈ P(5m up)₁ × P(5m up)₂ × P(5m up)₃   (independence assumption)
```
If they diverge by more than fees → flag inconsistency.

### Streamlit Dashboard
- Live table: open predictions with model vs market P(up), edge, side, stake
- Scoreboard: per-asset Brier score, hit rate, Kelly multiplier
- Cumulative paper PnL curve over time
- Auto-refresh every 30 seconds

---



## 13. What Could Be Improved 

1. **Kronos not fine-tuned:** We use Kronos zero-shot on 5-minute crypto. Fine-tuning on BTC/ETH 5m data would likely improve calibration significantly.

2. **Only 10 bars cached:** The current OHLCV cache has only 10 bars (test fixture). A real run with `python run_flow.py` (no `--cache`) would fetch 1000 bars from Apify. 10 bars gives Kronos very little context → probabilities near 0.5 or noisy.

3. **Independence assumption in cross-horizon arb:** We assume three 5m windows are independent to compute the 15m joint probability. Markets are not independent (autocorrelation exists), so this is an approximation.

4. **Paper-only:** Real execution adds latency (you need to be first), slippage, minimum order sizes, and legal/KYC constraints (Polymarket geo-restricts, Kalshi requires US KYC for trading).

5. **Free LLM model rotation:** OpenRouter's `:free` roster changes. The config needs a live check before each run to confirm the model still exists and supports tool-calling.
