# CWT Crypto Predictions Agent

Backend Python agent that runs a 5-stage crypto prediction pipeline every 5 minutes:

1. **Market Agent** — finds BTC/ETH up/down markets on Polymarket (5m) and Kalshi (15m/hourly), reads implied probabilities
2. **Data Agent** — fetches the last 1000 OHLCV bars via Apify/Binance (free tier)
3. **Prediction Agent** — runs Kronos K-line foundation model (Monte-Carlo → P(up))
4. **Risk Agent** — sizes a paper position with fractional Kelly criterion
5. **Feedback Agent** — after each window resolves, scores predictions, updates Brier score, recalibrates Kelly multiplier

**Scaling features:** cross-venue arbitrage detection (Polymarket vs Kalshi), cross-horizon consistency check (15m vs 3×5m), Streamlit dashboard.

Built with **Claude Code** in VS Code. Framework: **Hermes Agent** plugin + skill structure (see §Hermes below). LLM: **OpenRouter** free model.

> **Disclaimer:** Research/education project. Paper trading only — no real orders. Not financial advice. Short-horizon crypto direction is near-random; expect P(up) ≈ 0.5 and frequent NO-TRADE outputs.

---

## Quick Start

### 1. Clone and set up

```bash
git clone https://github.com/Abhijit26918/CWT-Agents.git
cd CWT-Agents
python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux/Mac
source .venv/bin/activate

pip install -r requirements.txt
pip install apify-client torch einops huggingface_hub==0.33.1 tqdm safetensors streamlit
```

### 2. Clone Kronos (ML model)

```bash
git clone --depth 1 https://github.com/shiyu-coder/Kronos.git vendor/Kronos
```

### 3. Set up environment

```bash
cp .env.example .env
# Edit .env and fill in:
#   APIFY_TOKEN=apify_api_...        (from apify.com → Settings → Integrations → API tokens)
#   OPENROUTER_API_KEY=sk-or-...     (from openrouter.ai → Keys, free tier)
#   HF_HOME=D:/HuggingFace           (or any path with ~3GB free for Kronos weights)
```

### 4. Run

```bash
# Dry run — prints config and checks secrets
python run_flow.py --dry

# One prediction cycle (downloads Kronos weights on first run ~500MB)
python run_flow.py

# Use cached Apify data (dev mode, no credit spend)
python run_flow.py --cache

# Live loop every 5 minutes
python run_flow.py --loop --interval 300

# Dashboard
.venv\Scripts\streamlit run dashboard/app.py      # Windows
streamlit run dashboard/app.py                    # Linux/Mac
```

### 5. Tests

```bash
pytest          # 85 tests, all offline (no API calls)
```

### 6. Backtest

```bash
python backtest.py fetch --asset BTC --days 60
python backtest.py run --asset BTC --stride 3 --max-windows 500 --seed 42 --out reports/backtest_BTC_thorough.json
python backtest.py compare --asset BTC --report reports/backtest_BTC_thorough.json
```

See [BACKTEST_RESULTS.md](BACKTEST_RESULTS.md) for methodology, results, and a
real bug the live loop caught that the backtest didn't.

---

## Hermes Agent Integration (Linux/macOS)

The pipeline also runs as a Hermes Agent plugin with a `/crypto-flow` skill.

```bash
# Install Hermes (Linux/macOS only)
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash

# Configure OpenRouter
hermes model   # choose OpenRouter, paste API key

# Install plugin
cp -r hermes/plugins/crypto-predictions ~/.hermes/plugins/
cp -r hermes/skills/crypto-prediction-flow ~/.hermes/skills/

# Run
hermes
> /crypto-flow
```

**Windows users:** use WSL2 for Hermes. The headless `python run_flow.py` demonstrates all 5 agents on Windows.

---

## Project Structure

```
core/
  markets/      polymarket.py + kalshi.py     (Agent 1)
  data/         apify_ohlcv.py                 (Agent 2)
  predict/      kronos_model.py                (Agent 3)
  risk/         kelly.py                       (Agent 4)
  feedback/     scoring.py                     (Agent 5 — feedback loop)
  scale/        arbitrage.py                   (cross-venue + cross-horizon arb)
  backtest/     engine.py                      (walk-forward historical backtest)
  data/         binance_klines.py               (direct Binance historical fetch, backtest + live)
  pipeline.py                                  (shared orchestration)
hermes/
  plugins/crypto-predictions/                  (Hermes plugin — 5 tools + post_tool_call hook)
  skills/crypto-prediction-flow/SKILL.md       (/crypto-flow skill)
dashboard/app.py                               (Streamlit — live predictions + scoreboard)
run_flow.py                                    (headless entry point)
backtest.py                                    (backtest CLI — fetch / run / compare)
reports/                                       (backtest result JSON — see BACKTEST_RESULTS.md)
tests/                                         (85 tests, all offline)
```

## Submission Notes

- Apify token must be included in the submission email (not in this repo — see `.env.example`)
- Paper trading only — `--live` flag is intentionally disabled in v1
- Kronos runs CPU-only (`device: "cpu"` in `config.yaml`); switch to `Kronos-mini` for faster CPU inference
