# CWT Crypto Predictions Agent

Backend Python agent that locates 5-minute BTC/ETH up/down prediction markets on
Polymarket and Kalshi, pulls OHLCV via Apify, forecasts the next move with the
Kronos K-line model, sizes a paper position with fractional Kelly, and scores
itself after each window resolves to recalibrate. Built on the Hermes Agent
framework with an OpenRouter free model for orchestration.

See `files/MASTER_PLAN.md` (what's being built, build order) and
`files/EXPLANATION.md` (why it's built this way) for full design rationale.

**Status:** Phase 0 — scaffold only. Agent logic lands in later phases.

## Disclaimer

Research/education project. **Not financial advice.** Default mode is
**paper trading only** — no real orders are placed. Prediction-market access
is jurisdiction-restricted (Kalshi: US KYC; Polymarket: geo-restrictions); this
project only reads public market data. Short-horizon crypto direction is close
to a coin flip — expect P(up) near 0.5 and frequent NO-TRADE outputs.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy .env.example .env          # then fill in APIFY_TOKEN / OPENROUTER_API_KEY
```

## Quick start

```bash
python run_flow.py --dry        # prints loaded config + which secrets are set
```

## Tests

```bash
pytest
```
