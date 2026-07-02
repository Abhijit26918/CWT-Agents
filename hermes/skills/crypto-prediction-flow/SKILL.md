# /crypto-flow — CWT Crypto Prediction Flow

Run the full 5-stage crypto prediction pipeline for BTC and ETH on Polymarket and Kalshi, then summarise the results in a table.

## Steps

1. **Score past predictions first** (always run this before predicting):
   ```
   score_predictions()
   ```

2. **For each asset in [BTC, ETH]:**

   a. Fetch market data:
   ```
   find_markets(asset="{ASSET}")
   ```

   b. Fetch OHLCV bars via Apify:
   ```
   fetch_ohlcv(asset="{ASSET}", interval="5m", limit=1000)
   ```

   c. Run Kronos forecast:
   ```
   predict_move(asset="{ASSET}")
   ```

   d. Size the paper position — run for each venue:
   ```
   size_position(asset="{ASSET}", venue="polymarket")
   size_position(asset="{ASSET}", venue="kalshi")
   ```

3. **Summarise in a markdown table:**

| Asset | Venue | Model P(up) | Market P(up) | Edge | Side | Stake ($) |
|-------|-------|-------------|--------------|------|------|-----------|
| BTC   | polymarket | … | … | … | … | … |
| BTC   | kalshi     | … | … | … | … | … |
| ETH   | polymarket | … | … | … | … | … |
| ETH   | kalshi     | … | … | … | … | … |

Note any NO TRADE decisions and explain why (edge ≤ 0 after fees). Short-horizon crypto direction is near-random — NO TRADE is correct, disciplined behaviour.

## Notes

- All trades are **paper only**. No real orders are placed.
- Polymarket markets are 5-minute windows. Kalshi is ~15-minute or hourly — the horizon mismatch is logged and feeds the cross-venue arbitrage feature.
- If a venue's market is not yet indexed at a window boundary, the tool retries the previous window and logs a warning.
- Results are persisted to `cwt.db` (predictions table). Run `score_predictions()` again after the window closes to resolve outcomes and update calibration.
