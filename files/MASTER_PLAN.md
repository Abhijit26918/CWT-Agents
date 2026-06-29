# MASTER PLAN — CrowdWisdomTrading Crypto Predictions Agent

> **Read me first.** This is the single source of truth for building the project.
> It is written so an agentic coder (Claude Code in VS Code) can build it module by
> module, each with a Definition of Done and a copy-paste prompt. Build in the order
> in §13. Do not skip the testing strategy (§11) — it lets you build the whole thing
> at **~$0 cost**.

---

## 0. TL;DR of what we are building

A backend Python project that runs a 5-stage crypto-prediction flow using the
**Hermes Agent** framework (Nous Research), an **OpenRouter** free model for the LLM,
**Apify** for market data, and **Kronos** for the up/down forecast:

1. **Market Agent** — finds the next short-horizon BTC/ETH *up/down* prediction markets on **Polymarket** and **Kalshi**, and reads their implied probabilities.
2. **Data Agent** — uses **Apify** to fetch the last ~1000 OHLCV bars for the asset.
3. **Prediction Agent** — runs **Kronos** to estimate P(next move = up).
4. **Risk Agent** — sizes a (paper) position with the **Kelly criterion** using model probability vs market price.
5. **Feedback Agent** — after the window resolves, scores the prediction, updates accuracy/calibration, and adjusts the Kelly multiplier — the Hermes "agent loop feedback."

Plus scaling work (§12): multi-horizon ensemble, cross-venue / 15m-vs-5m arbitrage, and a user-visibility dashboard.

**Default mode is paper / read-only.** We never place real money trades in v1. We only *read* public market data and *simulate* sizing and PnL. This is safer, needs no exchange auth, matches the deliverable (a video of it working), and sidesteps jurisdiction/KYC issues (Kalshi is US-KYC only; Polymarket geo-restricts some regions). A `--live` flag is left as a documented, disabled extension point.

---

## 1. Key design decisions (and why)

| Decision | Rationale |
|---|---|
| **Plain-Python `core/` + Hermes wrapper** | All business logic (market discovery, data, Kronos, Kelly, scoring) lives in plain, deterministic, unit-testable Python. Hermes wraps each as a *tool* and a *skill*. This keeps logic testable for free and keeps the LLM out of the hot path. |
| **5 "agents" = 5 Hermes plugin tools** | Hermes is a CLI agent (not a LangGraph library). The idiomatic way to express a multi-agent flow is: register tools in a plugin, and a *skill* (`/crypto-flow`) tells the Hermes agent the exact order to call them. The Hermes agent is the orchestrator; each tool is a sub-agent step. |
| **Headless `run_flow.py` AND `/crypto-flow` skill** | Two entry points to the same `core/`. Headless = reliable demo + cron + zero-LLM testing. Skill = satisfies "agents with a flow" and shows real Hermes usage in the video. |
| **OpenRouter free model** | Required by the brief. The LLM only does orchestration glue + the feedback reflection — a tiny slice — so cost stays ~$0. |
| **Paper / read-only default** | Safe, no auth, legal across jurisdictions, and the brief only needs predictions + sizing, not real fills. |
| **SQLite** | Zero-ops persistence for markets, OHLCV, predictions, outcomes, calibration, run logs. |

---

## 2. The five agents, precisely

### Agent 1 — Market Agent (`find_markets`)
Find the *current* short-horizon up/down market for BTC and ETH on both venues and read implied probability.

- **Polymarket** has real 5-minute and 15-minute up/down markets. The slug is **deterministic** from the clock — no scanning:
  - 5m window start: `ts = floor(now_utc / 300) * 300` → slug `btc-updown-5m-{ts}` / `eth-updown-5m-{ts}`
  - 15m window start: `ts = floor(now_utc / 900) * 900` → slug `btc-updown-15m-{ts}` / `eth-updown-15m-{ts}`
  - Resolve via **Gamma API** (public, no auth): `GET https://gamma-api.polymarket.com/events?slug={slug}` → event → `markets[0]`. Parse `outcomes` (JSON string like `["Up","Down"]`) and `clobTokenIds` (array) → `up_token_id`, `down_token_id`.
  - Implied prob via **CLOB** (public read): `GET https://clob.polymarket.com/midpoint?token_id={up_token_id}` → mid price ≈ P(up). Optionally `GET https://clob.polymarket.com/price?token_id=...&side=BUY` for best ask. Prefer the `py-clob-client` or `polymarket-apis` package, but raw `requests` is fine for read-only.
- **Kalshi** has crypto up/down but the **shortest is ~15-minute / hourly, not 5-minute**. Handle the mismatch explicitly: for Kalshi, target the *nearest available* short horizon (15m if present, else hourly).
  - Public REST v2, no auth for market data, base `https://api.elections.kalshi.com/trade-api/v2`.
  - Discover: `GET /series?category=Crypto` → find the BTC/ETH up/down series (tickers look like `KXBTC`, `KXETH`, `KXBTCD`); `GET /events?series_ticker={ticker}&status=open` → pick the soonest-closing event; `GET /markets?event_ticker={...}` → the YES/NO market.
  - Implied prob: read `yes_bid` / `yes_ask` (cents) → `P(up) ≈ (yes_bid + yes_ask) / 200`. Or `GET /markets/{ticker}/orderbook`.
  - Official SDK `kalshi-python` exists; raw `requests` is fine for read-only.
- **Output** (persist to `markets`): for each (asset, venue, horizon): `up_token/ticker`, `down_token`, `implied_up`, `implied_down`, `window_close_ts`, `fetched_at`.
- **Gotcha:** if the deterministic Polymarket slug isn't indexed yet (small latency at boundary), retry the previous window or wait a few seconds. Log, don't crash.

### Agent 2 — Data Agent (`fetch_ohlcv`)
Fetch the last ~1000 bars of OHLCV via **Apify** (required by brief; Apify wraps Binance public klines, free monthly credits).

- `pip install apify-client`. Token from Apify Console → Integrations → API tokens, stored in `.env` as `APIFY_TOKEN`.
- Use a Binance **klines** actor, e.g. `parseforge/binance-prices-scraper` (modes include `klines`, intervals `1s–1M`) or `getdataforme/binance-price-history-scraper`. Pin the actor id in config so it's swappable.
- Pattern:
  ```python
  from apify_client import ApifyClient
  client = ApifyClient(os.environ["APIFY_TOKEN"])
  run = client.actor("parseforge/binance-prices-scraper").call(run_input={
      "mode": "klines", "symbol": "BTCUSDT", "interval": "5m", "limit": 1000
  })
  rows = list(client.dataset(run["defaultDatasetId"]).iterate_items())
  ```
- Normalize to a pandas DataFrame with columns `["open_time","open","high","low","close","volume","amount"]` (compute `amount = close*volume` if the actor doesn't return it). Sort ascending by time. Persist to `ohlcv` (dedup on `(asset, interval, open_time)`).
- **Interval choice:** base flow uses `interval="5m"` to match the 5m market (predict next 5m bar). The 1m→aggregate path is the scaling feature (§12).
- **Resilience:** Apify runs can be slow/cold-start. Add a timeout + one retry, and a `--cache` mode that reuses the last dataset for dev so you don't burn credits.

### Agent 3 — Prediction Agent (`predict_move`)
Estimate **P(up)** for the next bar with **Kronos** (decoder-only K-line foundation model, MIT, AAAI 2026).

- Install: `git clone https://github.com/shiyu-coder/Kronos.git`, `pip install -r requirements.txt` (brings torch). Vendor it under `vendor/Kronos` or install as a path dependency.
- Load (models on Hugging Face Hub):
  ```python
  from model import Kronos, KronosTokenizer, KronosPredictor
  tok = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
  mdl = Kronos.from_pretrained("NeoQuasar/Kronos-small")
  predictor = KronosPredictor(mdl, tok, device="cpu", max_context=512)
  ```
- **CRITICAL gotcha — 512 context cap:** Kronos-small/base support **max_context = 512**. We fetched 1000 bars (satisfies the brief), but **feed only the last ≤512** to Kronos. Slice explicitly (`df.tail(400)`) — don't rely on silent truncation. (Kronos-mini, 4M params, supports 2048 context with the `Kronos-Tokenizer-2k` tokenizer — keep as a documented alternative for the 1m×many-bars scaling path.)
- **Turning a price forecast into P(up):** Kronos is probabilistic (Monte-Carlo). Run N stochastic single-step forecasts and take the fraction that close above the last known close:
  ```python
  ups = 0
  for _ in range(N):                      # N = 30 by default
      pred = predictor.predict(df=lookback[["open","high","low","close","volume","amount"]],
                               x_timestamp=lookback_ts, y_timestamp=future_ts,
                               pred_len=1, T=1.0, top_p=0.9, sample_count=1)
      if pred["close"].iloc[-1] > last_close: ups += 1
  p_up = ups / N
  ```
  (Mirror the demo's MC approach in Kronos-demo `update_predictions.py`.) Cache loaded models as a module singleton — loading is the slow part, not inference.
- **Output:** `p_up` (calibrated — see Agent 5), plus raw forecast for logging. Persist nothing here; Agent 4 writes the prediction row.
- **Speed:** Kronos-small on CPU for one 1-step forecast × 30 samples is fine for a demo (seconds–low minutes). Note CPU-only in README.

### Agent 4 — Risk Agent (`size_position`)
Decide side and size using the **Kelly criterion** for binary prediction-market contracts.

- A contract costs `c` (= market implied prob of that outcome) and pays `$1` if it resolves true. Your model says P(up) = `p`.
- **Edge:** `edge_up = p - c_up`. If `edge_up > 0`, consider **UP**; else evaluate **DOWN** with `p_down = 1 - p`, `c_down = implied_down`.
- **Kelly fraction** for buying a $1-payout contract at cost `c` with win prob `p`:
  - `f* = (p - c) / (1 - c)` for the UP side (valid when `p > c`).
  - Symmetric for DOWN: `f* = ((1-p) - c_down) / (1 - c_down)`.
- **Use fractional Kelly:** `stake_fraction = clamp(kelly_multiplier * f*, 0, f_max)` with defaults `kelly_multiplier = 0.25`, `f_max = 0.10`. `kelly_multiplier` is tuned down by the Feedback Agent if calibration is poor. If `f* <= 0` after fees, **no trade** (this is a valid, common output — log it).
- Account for fees/spread: subtract a configurable `fee` (e.g. use ask not mid; Polymarket min order = 5 shares) before declaring an edge.
- **Output (persist to `predictions`):** asset, venue, horizon, `model_p_up`, `market_p_up`, `edge`, `side` (UP/DOWN/NONE), `kelly_fraction`, `stake_paper = kelly_fraction * bankroll_paper`, `status='OPEN'`.

### Agent 5 — Feedback Agent (`score_predictions`) — the Hermes loop
After a window closes, resolve the outcome, score, and feed back.

- **Resolve:** fetch the actual close at `window_close_ts` (re-use Agent 2's Apify klines, or the venue's resolution). `actual_direction = up if close_end > close_start else down`.
- **Score:** for each `OPEN` prediction whose window has closed: `won = (side == actual_direction)`; `pnl_paper`: if won, `stake_paper * (1 - c)/c`; else `-stake_paper`. Set `status='RESOLVED'`. Write to `outcomes`.
- **Calibrate / feed back (per asset):**
  - Rolling **hit rate** and **Brier score** = mean((p_up − outcome)²).
  - If recent Brier is poor / overconfident, **shrink `kelly_multiplier`** (e.g. ×0.8, floored at 0.05); if well-calibrated and profitable, allow it to recover toward 0.25.
  - Optionally fit a simple **probability calibrator** (isotonic / Platt on accumulated (p_up, outcome) pairs) and apply it in Agent 3. Keep it off until you have ≥~50 samples.
  - Persist to `calibration` (asset, n, brier, hit_rate, kelly_multiplier, updated_at).
- **Why this is "the Hermes agent loop feedback":** the skill runs Market→Data→Predict→Size, records the bet, and on the next run (or a scheduled run) calls `score_predictions`, which mutates the parameters the next decision uses. Hermes' own self-improving loop (it offers to save skills after multi-step tasks; `post_tool_call`/`on_session_end` hooks) layers on top — but the deterministic calibration loop above is the real, gradeable mechanism.

---

## 3. Repository layout

```
cwt-crypto-agent/
├── README.md
├── MASTER_PLAN.md            # this file
├── EXPLANATION.md            # the why/how doc
├── pyproject.toml            # or requirements.txt
├── .env.example              # APIFY_TOKEN=, OPENROUTER_API_KEY=, etc. (never commit real .env)
├── .gitignore                # .env, *.db, vendor/Kronos weights cache, __pycache__
├── config.yaml               # assets, intervals, actor id, kelly defaults, model id
├── core/
│   ├── __init__.py
│   ├── config.py             # load config.yaml + .env, typed settings
│   ├── db.py                 # SQLite connection + schema migrations
│   ├── markets/
│   │   ├── polymarket.py     # slug builder, Gamma + CLOB read
│   │   └── kalshi.py         # series/event/market discovery + odds read
│   ├── data/apify_ohlcv.py   # Apify klines fetch + normalize + cache
│   ├── predict/kronos_model.py  # KronosPredictor wrapper + MC P(up)
│   ├── risk/kelly.py         # edge + Kelly sizing
│   ├── feedback/scoring.py   # resolve, Brier, calibration, kelly_multiplier
│   ├── scale/                # §12: multi_horizon.py, arbitrage.py
│   └── pipeline.py           # the orchestration function used by both entry points
├── run_flow.py               # headless entry point (no LLM) — demo + cron
├── hermes/
│   ├── plugins/crypto-predictions/
│   │   ├── plugin.yaml        # manifest: provides_tools, provides_hooks, requires_env
│   │   ├── __init__.py        # register(ctx): wire schemas -> handlers, log hook
│   │   ├── schemas.py         # tool schemas the LLM sees
│   │   └── tools.py           # thin handlers that call core/*
│   └── skills/crypto-prediction-flow/
│       └── SKILL.md           # /crypto-flow orchestration recipe
├── dashboard/app.py          # §12 Streamlit/FastAPI user visibility
├── logs/                     # rotating file logs
└── tests/
    ├── fixtures/             # recorded JSON for Apify/Polymarket/Kalshi
    ├── test_kelly.py
    ├── test_polymarket_slug.py
    ├── test_pipeline_fakes.py
    └── ...
```

---

## 4. SQLite schema

```sql
CREATE TABLE IF NOT EXISTS markets (
  id INTEGER PRIMARY KEY,
  ts_window INTEGER, asset TEXT, venue TEXT, horizon TEXT,
  up_ref TEXT, down_ref TEXT,          -- token id or market ticker
  implied_up REAL, implied_down REAL,
  window_close_ts INTEGER, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS ohlcv (
  asset TEXT, interval TEXT, open_time INTEGER,
  open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL,
  source TEXT, PRIMARY KEY (asset, interval, open_time)
);
CREATE TABLE IF NOT EXISTS predictions (
  id INTEGER PRIMARY KEY,
  ts INTEGER, asset TEXT, venue TEXT, horizon TEXT,
  model_p_up REAL, market_p_up REAL, edge REAL,
  side TEXT, kelly_fraction REAL, stake_paper REAL,
  window_close_ts INTEGER, status TEXT DEFAULT 'OPEN', created_at TEXT
);
CREATE TABLE IF NOT EXISTS outcomes (
  prediction_id INTEGER PRIMARY KEY REFERENCES predictions(id),
  resolved_at TEXT, actual_direction TEXT, won INTEGER, pnl_paper REAL
);
CREATE TABLE IF NOT EXISTS calibration (
  asset TEXT PRIMARY KEY, n INTEGER, brier REAL, hit_rate REAL,
  kelly_multiplier REAL DEFAULT 0.25, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT,
  n_markets INTEGER, n_predictions INTEGER, notes TEXT
);
```

---

## 5. Configuration (`config.yaml`)

```yaml
assets: [BTC, ETH]
symbols: { BTC: BTCUSDT, ETH: ETHUSDT }
horizon: "5m"            # base market horizon
ohlcv_interval: "5m"
ohlcv_limit: 1000
kronos:
  model: "NeoQuasar/Kronos-small"
  tokenizer: "NeoQuasar/Kronos-Tokenizer-base"
  device: "cpu"
  lookback: 400          # <= 512 hard cap
  mc_samples: 30
apify:
  actor: "parseforge/binance-prices-scraper"
risk:
  bankroll_paper: 1000.0
  kelly_multiplier: 0.25
  f_max: 0.10
  fee: 0.01
llm:
  provider: "openrouter"
  model: "deepseek/deepseek-chat-v3.1:free"   # verify live; see §6
venues: [polymarket, kalshi]
mode: "paper"            # paper | live(disabled)
```

---

## 6. Hermes + OpenRouter setup

- **Install Hermes** (Python 3.11+; needs a model with ≥64K context):
  `curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`
- **Pick OpenRouter free model** (the brief allows "any free model"):
  - Run `hermes model` → choose **OpenRouter** → paste `OPENROUTER_API_KEY` (free, no card; add $10 once to raise 50/day → 1000/day if you iterate a lot).
  - The `:free` roster **rotates** (Qwen3-Coder & DeepSeek-R1 lost `:free` in late June 2026). Before fixing the model, check the live list filtered for tool support: `openrouter.ai/models?supported_parameters=tools` + the Free filter.
  - **Primary candidates with tool-calling + ≥64K context:** `deepseek/deepseek-chat-v3.1:free`, `meta-llama/llama-4-maverick:free`, `qwen/qwen3-235b-a22b:free`, `openai/gpt-oss-120b:free`. **Fallback:** `openrouter/free` (auto-router).
  - Configure a Hermes `fallback_providers` chain so a 429/removed model doesn't kill a run.
- **Plugin install:** drop the plugin dir at `~/.hermes/plugins/crypto-predictions/` (or project-local `./.hermes/plugins/` with `HERMES_ENABLE_PROJECT_PLUGINS=true`). `plugin.yaml` declares `provides_tools: [find_markets, fetch_ohlcv, predict_move, size_position, score_predictions]`, `provides_hooks: [post_tool_call]`, and `requires_env: [APIFY_TOKEN]`.
- **Skill install:** put `SKILL.md` at `~/.hermes/skills/crypto-prediction-flow/` → becomes `/crypto-flow`.

**`tools.py` is thin** — each handler validates input, calls the matching `core/` function, returns JSON. No business logic in the plugin. The `post_tool_call` hook appends a structured log line per tool call (this is your free logging + audit trail).

**`SKILL.md` flow (the orchestration recipe):**
```
For each asset in [BTC, ETH]:
  1. find_markets(asset) on polymarket and kalshi -> implied probs
  2. fetch_ohlcv(asset, interval=5m, limit=1000)
  3. predict_move(asset) -> p_up
  4. size_position(asset, venue) -> side, kelly_fraction, stake (paper)
Then: score_predictions() to resolve any matured windows and update calibration.
Summarize the table: asset | venue | model_p_up | market_p_up | edge | side | stake.
```

---

## 7. `core/pipeline.py` (shared by both entry points)

One function, no LLM, fully testable:
```python
def run_once(cfg, db, venues=("polymarket","kalshi")) -> RunReport:
    score_predictions(db, cfg)                 # resolve matured windows first
    report = RunReport()
    for asset in cfg.assets:
        ohlcv = fetch_ohlcv(asset, cfg)        # Apify
        p_up  = predict_move(asset, ohlcv, cfg)# Kronos MC, then apply calibrator
        for venue in venues:
            mk = find_market(asset, venue, cfg)# Polymarket/Kalshi
            if not mk: continue
            decision = size_position(p_up, mk, db, cfg)  # Kelly
            persist_prediction(db, asset, venue, p_up, mk, decision)
            report.add(asset, venue, p_up, mk, decision)
    return report
```
`run_flow.py` = `run_once` + pretty table + optional `--loop --interval 300` for live cadence. `/crypto-flow` = the LLM calling the same tools in the same order.

---

## 8. Logging & error handling (extra-points item)

- `logging` with a rotating file handler in `logs/` + console; one `run_id` per invocation, threaded through.
- Every external call (Apify, Gamma, CLOB, Kalshi, OpenRouter) wrapped with: timeout, 1 retry w/ exponential backoff, typed exceptions (`MarketNotIndexed`, `ApifyRunFailed`, `KronosLoadError`), and **graceful degradation** (skip a venue/asset and continue rather than abort the run).
- The Hermes `post_tool_call` hook logs `{run_id, tool, args_summary, latency_ms, ok}`.
- Never log secrets. Redact tokens in any echoed config.

---

## 9. Security & secrets

- `.env` only; `.env.example` committed, real `.env` git-ignored.
- `APIFY_TOKEN`, `OPENROUTER_API_KEY` from env. Kalshi/Polymarket auth NOT needed (read-only).
- For the submission email, the brief says **"APIFY tokens used — must!"** → include your Apify token **in the email body**, *not* in the repo. Rotate/regenerate it after grading.

---

## 10. Dependencies

```
apify-client
pandas
numpy
requests
torch            # via Kronos requirements
huggingface_hub
python-dotenv
pydantic
pytest
responses        # HTTP mocking in tests
streamlit        # dashboard (or fastapi+uvicorn)
# Kronos: installed from cloned repo's requirements.txt
# Hermes: installed via its own installer (not pip into this venv)
# optional: py-clob-client / polymarket-apis, kalshi-python
```

---

## 11. No-cost testing strategy (build the whole thing for ~$0)

The point: **never call a paid/rate-limited service in a normal test run.**

- **LLM:** all `core/` and `run_flow.py` tests run with **zero LLM calls** (the LLM only orchestrates in Hermes). One *optional* integration test hits OpenRouter, gated behind `RUN_LLM_TESTS=1`.
- **Apify:** record one real dataset to `tests/fixtures/binance_btc_5m.json`; `fetch_ohlcv` accepts an injectable client → tests use a fake client returning the fixture. A `--cache` flag reuses the last live dataset during dev.
- **Polymarket / Kalshi:** use `responses` to mock Gamma/CLOB/Kalshi endpoints from recorded JSON. Unit-test the **slug/window math** and **odds parsing** without network.
- **Kronos:** unit-test the MC→P(up) logic with a **fake predictor** (returns a fixed distribution). One slow, opt-in test loads Kronos-mini on a tiny synthetic df.
- **Kelly:** pure-function tests — `f*=(p−c)/(1−c)`, no-trade when `p<=c`, clamping, fractional multiplier, DOWN-side symmetry.
- **Feedback:** seed `predictions`+`ohlcv`, run `score_predictions`, assert `won`, `pnl_paper`, Brier, and `kelly_multiplier` shrink.
- **Pipeline:** `test_pipeline_fakes.py` runs `run_once` end-to-end with all fakes → asserts rows land in SQLite. This is your green-build gate.

Target: `pytest` green offline; one `make demo` that runs a real, cached, free end-to-end pass.

---

## 12. Scaling work ("think outside the box" — the differentiator)

Implement at least one well; design-note the rest.

1. **Multi-horizon ensemble (brief's idea 1):** fetch `1m` bars, forecast the next 5 (`pred_len=5`, Kronos-mini for 2048 context), aggregate to a 5m directional call, and **blend** with the direct 5m forecast (e.g. average the two P(up)). Disagreement between them is itself a confidence signal → widen the no-trade band when they disagree.
2. **Internal / cross-horizon arbitrage (brief's idea 2):** the 15m up/down should be roughly consistent with the path of the three constituent 5m windows. If `P(15m up)` implied by the market is inconsistent with the 5m markets' implied path, flag an edge. Also **cross-venue**: Polymarket vs Kalshi implied prob for the closest matching horizon — if they diverge beyond fees, flag it.
3. **User visibility (brief's idea 3):** `dashboard/app.py` (Streamlit) showing live: per-asset/venue model P(up) vs market price, edge, side, paper stake, and the **running scoreboard** (hit rate, Brier, paper PnL curve). Optionally push alerts via Hermes' messaging gateway (Telegram/Discord) since Hermes supports 18+ platforms out of the box.
4. **Parallelism:** Hermes can spawn isolated subagents per asset/venue; Kronos has `predict_batch` for multiple series at once.
5. **Ties back to the brief's "creating Ads":** the structured research output (prediction + edge + market mispricing + rationale) is exactly the raw material CWT turns into trading-signal content/ads — expose it as a clean JSON "research card" per opportunity.

---

## 13. Build order (5–7 day plan, each with a Definition of Done)

> Tell Claude Code: *"Build phase N from MASTER_PLAN.md. Stop at the DoD, run the tests, then wait."* Do not let it build everything in one shot.

- **Phase 0 — Scaffold (Day 1):** repo layout, `config.py`, `db.py` + schema, logging, `pyproject`, `.env.example`, empty modules, CI-less `pytest` skeleton. Install Hermes + pick OpenRouter free model.
  **DoD:** `hermes` chats with the free model; `pytest` green on skeleton; `python run_flow.py --dry` prints config.
- **Phase 1 — Market Agent (Day 2):** `polymarket.py` (slug + Gamma + CLOB) and `kalshi.py` (discovery + odds), persist to `markets`.
  **DoD:** `python -m core.markets.polymarket` prints live BTC & ETH 5m up/down odds; slug-math + odds-parse unit tests green.
- **Phase 2 — Data + Prediction (Day 3):** `apify_ohlcv.py` (1000 bars, normalized, cached) and `kronos_model.py` (load + MC P(up), 512-cap slice).
  **DoD:** `fetch_ohlcv` returns 1000-row DF from a fixture; `predict_move` returns a P(up) in (0,1) for BTC/ETH from cached data.
- **Phase 3 — Risk + Pipeline (Day 4):** `kelly.py` and `core/pipeline.py`; wire `run_flow.py` end-to-end.
  **DoD:** `python run_flow.py` runs Market→Data→Predict→Size for both assets/venues and writes `predictions`; `test_pipeline_fakes.py` green.
- **Phase 4 — Feedback + Hermes wrapper (Day 5):** `scoring.py` (resolve, Brier, calibration); the plugin (`plugin.yaml/__init__/schemas/tools`) + `SKILL.md`; `post_tool_call` logging.
  **DoD:** `/crypto-flow` inside Hermes runs all 5 tools and prints the summary table; after a matured window, `score_predictions` writes `outcomes` and updates `calibration`.
- **Phase 5 — Scale + polish (Day 6–7):** one scaling feature fully (recommend the dashboard + cross-venue arb), README, demo video, record Apify token usage, final cleanup.
  **DoD:** dashboard shows live predictions + scoreboard; README quick-start works on a clean clone; 2–4 min video recorded.

---

## 14. Claude Code prompt library (copy-paste, one per module)

**Setup**
> Read `MASTER_PLAN.md`. Create the repo layout in §3, `config.py`/`config.yaml` per §5, `db.py` with the §4 schema and an idempotent `init_db()`, rotating logging per §8, and `.env.example`. Add `pytest` and a trivial passing test. Don't implement the agents yet. Then stop and run `pytest`.

**Agent 1 — Polymarket**
> Implement `core/markets/polymarket.py` per §2 Agent 1: deterministic slug builder for 5m/15m windows, `get_event(slug)` via Gamma, parse `outcomes`+`clobTokenIds`, `get_implied_prob(token_id)` via CLOB midpoint. Pure functions for slug/window math. Add `tests/test_polymarket_slug.py` and an odds-parse test using a recorded JSON fixture with `responses`. Handle "market not indexed yet" by retrying the previous window. Stop and run tests.

**Agent 1 — Kalshi**
> Implement `core/markets/kalshi.py` per §2 Agent 1: discover the soonest-closing BTC/ETH up/down event via `/series`→`/events`→`/markets` (public, no auth), read `yes_bid/yes_ask`→implied P(up). Note in a docstring that Kalshi's shortest crypto up/down is ~15m/hourly, so we target the nearest horizon. Mock endpoints in tests. Stop and run tests.

**Agent 2 — Apify**
> Implement `core/data/apify_ohlcv.py` per §2 Agent 2 using `apify-client`. Accept an injectable client for testing. Normalize to the documented DataFrame, compute `amount` if missing, sort ascending, persist to `ohlcv` with dedup, add timeout+retry, and a `--cache` path. Test with `tests/fixtures/binance_btc_5m.json` and a fake client. Stop and run tests.

**Agent 3 — Kronos**
> Implement `core/predict/kronos_model.py` per §2 Agent 3. Load tokenizer+model once (module singleton). `predict_move(df)` slices the last `cfg.kronos.lookback` (≤512) rows, runs `mc_samples` single-step stochastic forecasts, returns `p_up = mean(pred_close > last_close)`. Apply the calibrator from `calibration` if available. Unit-test the MC→P(up) logic with a fake predictor; mark the real-model test opt-in. Stop and run tests.

**Agent 4 — Kelly**
> Implement `core/risk/kelly.py` per §2 Agent 4: `edge`, `f* = (p−c)/(1−c)`, DOWN-side symmetry, fee subtraction, fractional `kelly_multiplier` from `calibration`, clamp to `[0, f_max]`, `NONE` when no edge. Pure functions. Full unit tests incl. boundary cases. Stop and run tests.

**Pipeline + headless**
> Implement `core/pipeline.py:run_once` (§7) and `run_flow.py` (pretty table, `--loop --interval`, `--dry`, `--cache`). Add `tests/test_pipeline_fakes.py` running the full flow with fakes asserting rows in SQLite. Stop and run tests.

**Agent 5 — Feedback**
> Implement `core/feedback/scoring.py` per §2 Agent 5: resolve OPEN predictions whose window closed (actual direction from cached klines), compute `won`/`pnl_paper`, rolling hit-rate + Brier per asset, shrink/recover `kelly_multiplier`, persist `outcomes`+`calibration`. Tests seed data and assert all of it. Stop and run tests.

**Hermes plugin + skill**
> Create `hermes/plugins/crypto-predictions/` (`plugin.yaml`, `__init__.py:register(ctx)` wiring schemas→thin handlers that call `core/*`, `schemas.py`, `tools.py`) for the 5 tools + a `post_tool_call` logging hook, `requires_env: [APIFY_TOKEN]`. Create `hermes/skills/crypto-prediction-flow/SKILL.md` implementing the §6 flow as `/crypto-flow`. Handlers contain NO business logic. Provide install instructions in README.

**Scaling**
> Implement `core/scale/multi_horizon.py` (1m→5 bars blended with direct 5m) and `core/scale/arbitrage.py` (cross-venue + 15m-vs-3×5m consistency), and `dashboard/app.py` (Streamlit: live predictions table + scoreboard with paper-PnL curve, hit rate, Brier). Wire both into the pipeline behind config flags. Tests for the arb/consistency math. Stop and run tests.

---

## 15. Acceptance checklist (maps to the brief's evaluation criteria)

- [ ] **Working functionality:** `/crypto-flow` and `run_flow.py` both run all 5 stages for BTC+ETH on Polymarket+Kalshi.
- [ ] **Code clarity/organization:** plain-Python `core/`, thin Hermes wrappers, typed config, docstrings.
- [ ] **Built with a coding agent:** developed with Claude Code in VS Code (mention in README + video).
- [ ] **Scale:** ≥1 scaling feature fully working (dashboard + cross-venue arb recommended).
- [ ] **Logging & error handling:** rotating logs, retries, graceful degradation, `post_tool_call` audit.
- [ ] **Feedback loop:** calibration + kelly_multiplier update after resolution.
- [ ] **Deliverables:** public repo link; Apify token in the email (not repo); 2–4 min demo video.

---

## 16. Disclaimers to put in the README

This is a research/education project. It is **not financial advice**. Default mode is **paper trading only**; no real orders are placed. Prediction-market access is **jurisdiction-restricted** (Kalshi: US KYC; Polymarket: geo-restrictions) — the project only reads public market data. Short-horizon crypto direction is **close to a coin flip**; expect P(up) near 0.5 and frequent NO-TRADE outputs — that is correct, disciplined behavior, not a bug.
