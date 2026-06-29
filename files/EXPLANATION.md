# EXPLANATION — Why and How

> Companion to `MASTER_PLAN.md`. The master plan says *what to build*; this says
> *why it's built that way*. Use it to understand the system, narrate the demo
> video, and answer follow-up questions in an interview.

---

## 1. How I read the assignment (and one trap I avoided)

The brief asks for a backend Python project on the **Hermes Agent** framework with a 5-stage flow: find the next-5-min BTC/ETH up/down markets on **Polymarket** and **Kalshi**, fetch recent OHLCV via **Apify**, predict the next move with **Kronos**, size risk with **Kelly**, and close a **feedback loop**. Plus scaling ideas, logging, and a demo video.

The Project-Overview line says the tool is "for creating Ads," which doesn't match the rest of the scope. That's CWT's downstream product talking — they turn predictions into trading-signal *content*. So the real deliverable is the **prediction/research pipeline**, and I make its output a clean structured "research card" (prediction + edge + mispricing + rationale) that *feeds* ad/signal generation. I didn't build an ad generator; I built the research engine the ads would be made from.

**The trap:** assuming "Hermes Agent framework" is a Python library you `import` and wire into a graph (like LangGraph). It isn't. Hermes is a **self-improving CLI agent** (closer to Claude Code) with a **plugin + skills** system and OpenRouter as a first-class LLM provider. If you build a plain library pipeline and bolt "Hermes" on as a name, a reviewer who knows the framework will spot it instantly. So the architecture below uses Hermes *the way it's meant to be used*.

---

## 2. The core architectural idea

**Business logic in plain Python; Hermes wraps it.**

- Everything that must be correct and testable — market discovery, Apify fetch, Kronos forecasting, Kelly sizing, scoring — lives in a plain `core/` package. Deterministic, unit-tested, runs offline, costs nothing.
- The 5 "agents" become **5 Hermes plugin tools** (`find_markets`, `fetch_ohlcv`, `predict_move`, `size_position`, `score_predictions`). A Hermes **skill** (`/crypto-flow`) tells the Hermes agent the order to call them. So the *Hermes agent is the orchestrator* and each *tool is a sub-agent step* — that's the genuine multi-agent flow the brief asks for.
- There are **two entry points to the same core**: a headless `run_flow.py` (no LLM — for the demo, cron, and free testing) and the `/crypto-flow` skill (LLM-orchestrated — for the "agents with a flow" requirement and the video).

Why this is the right call:
- **Reliability:** the demo can't fail because a free LLM rate-limited mid-run; the headless path proves the pipeline works on its own.
- **Cost ≈ $0:** the LLM only does orchestration glue and the feedback reflection — the smallest, highest-value slice. Free model + a near-empty hot path.
- **Testability:** you can build and verify 100% of the logic with zero paid calls (see §6).
- **Honesty:** it uses Hermes for what Hermes is good at, not as a sticker.

---

## 3. How each agent works, in plain language

**1. Market Agent.** Polymarket actually runs 5-minute up/down markets for BTC and ETH, and the market's web address is *computable from the clock* (`btc-updown-5m-{unix-window-start}`), so we don't have to scan — we jump straight to the current window via the public Gamma API, read the two outcome tokens, and get the implied probability from the CLOB midpoint. The token price *is* the crowd's probability (a token at $0.54 ≈ 54% chance). **Kalshi is different:** its shortest crypto up/down is ~15-minute / hourly, not 5-minute. Rather than pretend otherwise, the design targets Kalshi's nearest available horizon and records the mismatch — which then becomes a *feature* in the arbitrage scaling work (comparing horizons across venues).

**2. Data Agent.** The brief requires Apify for data, so we use an Apify actor that wraps Binance's public klines (free, no exchange key) to pull the last ~1000 OHLCV bars, normalize them to a clean DataFrame, and cache them so dev iterations don't burn Apify credits.

**3. Prediction Agent.** Kronos is an open-source foundation model trained specifically on candlesticks — it "speaks K-line." It's probabilistic, so to get a *probability* of up rather than a single price guess, we run it ~30 times (Monte Carlo) and take the fraction of runs that close higher. The important subtlety: Kronos-small only accepts **512 bars of context**, so even though we fetched 1000 (to satisfy the brief and for caching/resolution), we feed it only the last ~400. Handling that explicitly — instead of letting it silently truncate — is the kind of detail that signals you actually ran the thing.

**4. Risk Agent.** Given the model's probability `p` and the market's price `c`, the **edge** is `p − c`. The Kelly criterion says the optimal fraction of bankroll to stake on a $1-payout contract bought at `c` is `f* = (p − c) / (1 − c)`. We deliberately use **quarter-Kelly** and cap the stake, because full Kelly is brutal when your probabilities are even slightly miscalibrated — and short-horizon crypto direction is noisy. If there's no edge after fees, the correct output is **NO TRADE** — and that will happen often. A system that mostly says "no edge, skip" on 5-minute coin-flips is behaving correctly, not failing.

**5. Feedback Agent.** This is "the Hermes agent loop." After a window closes, we check what actually happened, mark the paper bet win/loss, and compute two things per asset: a **hit rate** and a **Brier score** (how well-calibrated the probabilities were). If the model is overconfident, we automatically **shrink the Kelly multiplier** so the next bets are smaller; if it's calibrated and profitable, we let it recover. With enough samples we can even fit a calibration curve and correct the raw probabilities. So the loop literally changes the parameters the next decision uses — that's a real, gradeable feedback mechanism, with Hermes' own self-improvement (saving skills, hooks) layered on top.

---

## 4. The Kelly math, intuitively

You're offered a bet that pays $1 if you're right, and it costs `c` (the market price). You think the true chance is `p`.

- If `p > c`, the market is underpricing the outcome — you have an edge.
- Bet too small and you leave growth on the table; bet too big and one bad streak wipes you out. Kelly is the fraction that maximizes long-run growth: `f* = (p − c)/(1 − c)`.
- Example: model says `p = 0.60`, market price `c = 0.50` → `f* = 0.10/0.50 = 0.20` (20% of bankroll at full Kelly). At quarter-Kelly we'd stake 5%, capped at 10%.
- We subtract fees/spread first and require the edge to survive them. No surviving edge → stake 0.

---

## 5. Why the cost is essentially zero

Everything expensive is either free-tier or off the hot path:
- **Apify:** free monthly credits + local caching during dev.
- **Polymarket/Kalshi market data:** public, no auth, no cost.
- **Kronos:** open weights, runs on CPU.
- **OpenRouter:** free model; and because the LLM only orchestrates (a handful of calls per run), even the 50-requests/day free cap is plenty. A one-time $10 top-up lifts it to 1000/day if you iterate heavily.

This mirrors the "LLM touches only the smallest, highest-value slice" principle — the rest is free, local, deterministic tooling.

---

## 6. How it's built and tested for free

Built **module by module with Claude Code in VS Code**, each phase stopping at a Definition of Done and running tests before moving on (so the agent never sprawls). Tests **never hit a paid or rate-limited service**:
- The LLM is out of the `core/` tests entirely (one optional, env-gated integration test touches OpenRouter).
- Apify, Polymarket, and Kalshi calls are mocked from one-time recorded JSON fixtures; the slug/window math and odds parsing are tested as pure functions.
- Kronos's Monte-Carlo→probability logic is tested with a fake predictor; the real model test is opt-in.
- The full pipeline runs end-to-end against fakes and asserts rows land in SQLite — that's the green-build gate.

So you can develop the entire thing offline and only do a single real, cached, free pass for the demo.

---

## 7. The scaling ideas, and why they're more than buzzwords

- **Multi-horizon ensemble:** forecast five 1-minute bars and roll them into a 5-minute call, then blend with the direct 5-minute forecast. When the two disagree, that *disagreement* is a confidence signal — widen the no-trade band. (This is the brief's own idea, made concrete.)
- **Arbitrage / consistency checks:** a 15-minute up/down should be roughly consistent with the path of its three 5-minute windows; and Polymarket vs Kalshi should price the same horizon similarly. When they don't (beyond fees), that's a flagged opportunity. The venue-horizon mismatch from §3 turns from a nuisance into signal.
- **User visibility:** a Streamlit dashboard showing live model-vs-market, edge, side, paper stake, and a running scoreboard (hit rate, Brier, paper-PnL curve). Optional push alerts via Hermes' built-in messaging gateways (Telegram/Discord) — basically free given the framework.

---

## 8. Honest limitations (say these out loud — it reads as maturity)

- Short-horizon (5-min) crypto direction is near-random; expect P(up) close to 0.5 and frequent NO-TRADE. The value is in the *disciplined edge detection and risk sizing*, not in beating a coin flip.
- Kronos is zero-shot here (not fine-tuned on 5-min crypto); fine-tuning is a documented next step.
- Paper-only by design — real execution adds slippage, latency, fills, and legal/KYC constraints we deliberately don't touch in v1.
- OpenRouter's free roster rotates; the config picks a verified tool-calling model with a fallback chain, but it needs a quick live check before the run.

---

## 9. Demo video script (2–4 minutes)

1. **15s** — "CrowdWisdomTrading crypto predictions agent, built on Hermes Agent with an OpenRouter free model, Apify data, and the Kronos forecasting model. Default mode is paper trading — research, not real orders."
2. **30s** — Show the repo in VS Code; point out plain-Python `core/`, the Hermes `plugin` + `skill`, and that it was built with Claude Code.
3. **45s** — Run `python run_flow.py` headless. Walk the table: for BTC/ETH on Polymarket & Kalshi — model P(up), market price, edge, side, paper stake. Note where it says NO TRADE and why.
4. **45s** — In Hermes, run `/crypto-flow`. Show the agent calling the five tools in order and summarizing — same result, LLM-orchestrated.
5. **30s** — Open the dashboard: live predictions + scoreboard (hit rate, Brier, paper-PnL). Mention the cross-venue arbitrage flag.
6. **20s** — Show `score_predictions` resolving a matured window and the Kelly multiplier adjusting — "this is the feedback loop."
7. **15s** — Mention logging/error handling and that everything runs on free tiers. Done.

---

## 10. Submission email checklist (to gilad@crowdwisdomtrading.com)

- [ ] Public GitHub/GitLab repo link (clean history, README with quick-start, MASTER_PLAN.md + EXPLANATION.md included).
- [ ] **Apify token in the email body** (the brief requires it) — *not* committed to the repo; regenerate it after grading.
- [ ] Link to the 2–4 min demo video (unlisted is fine).
- [ ] One short paragraph: what you built, the Hermes plugin+skill approach, the cost-zero design, and which scaling feature you implemented fully.
- [ ] Note that it's paper/read-only by design and why (safety + jurisdiction + matches the deliverable).

---

## 11. One-paragraph summary you can paste anywhere

A backend Python agent that, every 5 minutes, locates the current BTC/ETH up/down prediction markets on Polymarket and Kalshi, reads their implied probabilities, pulls the latest OHLCV via Apify, forecasts the next move with the Kronos K-line foundation model (Monte-Carlo probability of up), computes the edge against market price, and sizes a paper position with a calibration-aware fractional Kelly criterion — then, after each window resolves, scores itself and tunes its own risk multiplier. It's built on the Hermes Agent framework (five tools orchestrated by a `/crypto-flow` skill, with a headless runner for demos and free testing), uses an OpenRouter free model only for orchestration, and runs end-to-end on free tiers.
