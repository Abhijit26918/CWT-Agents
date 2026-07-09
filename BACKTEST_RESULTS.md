# Backtest Results

## Methodology

`core/backtest/engine.py` replays the exact same prediction and sizing logic
the live pipeline uses (`core.predict.kronos_model.predict_move` +
`core.risk.kelly.size_position`) over historical OHLCV instead of live data —
no forked/duplicated logic, so backtest and live results are directly
comparable.

- **Data**: 60 days of real 5-minute BTC/ETH OHLCV, fetched directly from
  Binance's public REST API (`core/data/binance_klines.py` — Apify's actor has
  no pagination, so a direct fetch was needed for historical depth).
- **Model**: real Kronos-mini inference (not a stub/mock), 30 Monte-Carlo
  samples per window, same as production `config.yaml`.
- **Windows**: walk-forward, evenly subsampled across the full 60-day range
  (500 windows/asset in the "thorough" run below).
- **Market price**: no historical archive of Polymarket/Kalshi odds exists, so
  `market_p_up` is a fixed synthetic 0.50 baseline. Brier score and hit rate
  (which only need model P(up) vs. actual outcome, not a market price) are the
  metrics comparable to live. PnL numbers are labeled `_synthetic` throughout
  and are illustrative only, not a real-market replay.

Reproduce with:
```bash
python backtest.py fetch --asset BTC --days 60
python backtest.py run --asset BTC --stride 3 --max-windows 500 --seed 42 --out reports/backtest_BTC_thorough.json
python backtest.py compare --asset BTC --report reports/backtest_BTC_thorough.json
```

## Results: sample size matters (150 vs. 500 windows)

We ran this twice at different sample sizes, and the numbers moved — worth
showing both rather than only the final one, since the shift itself is the
useful lesson (a 150-sample read is noisy; don't over-trust it).

| Metric | BTC (150w) | BTC (500w) | ETH (150w) | ETH (500w) | Coin-flip |
|---|---|---|---|---|---|
| N (traded) | 143 | 475 | 139 | 470 | — |
| Brier score | 0.311 | 0.286 | 0.265 | 0.309 | 0.25 |
| Hit rate | 50.3% | 54.7% | 56.8% | 49.2% | 50.0% |

Raw data: `reports/backtest_BTC.json` / `backtest_ETH.json` (150-window run),
`reports/backtest_BTC_thorough.json` / `backtest_ETH_thorough.json` (500-window run).

**Read**: at 150 windows, ETH looked like it had a clear edge (56.8%) and BTC
looked like pure noise (50.3%) — the larger 500-window run flipped that
impression almost entirely (BTC 54.7%, ETH 49.2%). Neither run shows a
dramatic edge, and that's expected: crypto markets are close to efficient at
5-minute horizons, and a large apparent edge on a 60-day sample would be more
likely to indicate overfitting or a data leak than genuine alpha. The
takeaway isn't "BTC is good, ETH is bad" — it's that 150 samples isn't enough
to conclude either way, and even 500 should be treated as a first read, not
a final verdict.

## A real bug the live loop caught (and the backtest didn't)

Running the fixed pipeline live (`run_flow.py --loop`) surfaced something the
backtest couldn't: live hit rate was initially **far worse than random**
(30–33%), with a specific pattern — "UP" predictions won only 4/43 times (9%)
while "DOWN" predictions were roughly at chance (29/61, 47.5%).

Root cause: Binance's `/klines` endpoint returns the **currently-forming
candle** if its open time is in range. The live fetch path was using that
candle's live/partial price as the model's reference point, then scoring the
prediction against that *same* candle's final close — i.e., the model was
extrapolating a move that was already half-priced-in, and got hit by
mean-reversion once the candle actually closed.

The backtest was largely immune (it only touches this edge case on the very
last of many historical windows), which is why it looked fine while live
looked broken — a good illustration of why a live paper-trading loop is a
necessary complement to backtesting, not a redundant step.

**Fix**: `core/data/binance_klines.py` now excludes any bar that hasn't
closed yet (`fetch_historical_klines`, verified via `_STEP_MS` boundary
check). Regression test: `tests/test_binance_klines.py::test_fetch_historical_klines_excludes_still_forming_candle`.
The contaminated pre-fix live predictions were backed up (not committed —
local `cwt_pre_fix_backup.db`) and the live `predictions`/`outcomes`/`calibration`
tables were reset before restarting the loop.

## Known limitations / next steps

- `market_p_up` is synthetic (see Methodology) — PnL/Kelly numbers are not a
  real-market replay.
- Kronos MC sampling is stochastic; `--seed` gives best-effort reproducibility
  only (see `core/backtest/engine.py::_seed_rngs`).
- No probability calibration step yet (Platt/isotonic scaling on
  `model_p_up` vs. actual outcome) — the only calibration mechanism today is
  the blunt Kelly-multiplier shrink/recover in `core/feedback/scoring.py`.
- The live rolling-paper loop needs real wall-clock time (days, not hours) to
  accumulate a statistically meaningful sample post-fix.
