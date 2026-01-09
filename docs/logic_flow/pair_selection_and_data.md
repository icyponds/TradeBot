# Pair Selection & Data Pipeline Logic

*Documented: 2026-01-08*

This document explains the "under the hood" behavior of the `DynamicPairSelector` and its interaction with the `HyperliquidAPI` data layer.

## 1. Dynamic Pair Rescan (The "Reload")

### Behavior
Every **60 minutes** (configured via `SCAN_INTERVAL_MINUTES`), the bot triggers a full refresh of the trading universe.

### The Process
1.  **Purge**: `PairSelector` explicitly clears its *local* caches:
    *   `self.price_history` (Local reference to OHLCV data)
    *   `self.asset_metrics` (Calculated scores)
    *   `self.backfill_queue` (Pending background loads)
2.  **Re-Ranking**: It scans **ALL** eligible assets on the exchange (~150-300 pairs) and sorts them by 24h Volume.
3.  **Selection**: It picks the **Top N** assets (`max_pairs_to_trade`, default: 40) that have the highest Composite Score.
4.  **Background Fill**: The rest of the eligible assets are queued to be loaded in the background to ensure we have data ready if they jump in rank later.

### Why do this?
*   ** Opportunity Discovery**: Ensures "dead" assets are dropped and "hot" new assets (e.g., trending memecoins) are picked up immediately.
*   **Clean Slate**: Prevents logic drift by forcing a fresh re-evaluation of the entire market state.

---

## 2. Startup Optimization & "Smart Sleep"

### The Challenge
To respect API rate limits (1200 weight/min), the bot cannot load 300 assets instantly on startup.
*   **Old Behavior**: Loaded Top 10 assets synchronously (taking ~15s) with a hard 1.5s sleep between each.
*   **New Behavior**: Loads Top 20 assets synchronously.

### The Optimization ("Smart Sleep")
We implemented logic to detect if data is coming from the **API Cache** or the **Network**.
*   **Cache Hit (< 0.2s fetch)**: The bot waits only **0.01s**.
*   **Cache Miss (> 0.2s fetch)**: The bot waits the full **1.5s** to respect rate limits.

**Result**: Re-scanning 40 already-monitored assets now takes **~0.5 seconds** instead of 60 seconds.

---

## 3. The Ranking "Score"

The `Score` (e.g., `0.722`) is a composite metric used to decide which assets to trade. It is **NOT** a simple threshold (e.g. "> 0.5"), but a **Tournament Ranking**. The bot trades the Top N highest scorers.

### Components
1.  **Liquidity**: High Volume + Open Interest (Ease of entry/exit).
2.  **Volatility**: "Good" volatility (price movement) vs. "Bad" volatility (erratic crashes).
3.  **Strategy Fit**: Does the price action match our strategies (Momentum/Mean Reversion)?
4.  **Diversification**: Penalizes assets that are highly correlated with pairs we have already selected.

---

## 4. Data Integrity & Memory Safety

### "Purging" Safety
When the 60-minute rescan "purges" the cache, **NO DATA IS LOST**.
*   **Layer 1 (PairSelector)**: Clears its *pointer* to the data.
*   **Layer 2 (HyperliquidAPI)**: Maintains a persistent `OhlcvCache` (Rolling Window). This cache handles the live WebSocket ticks.

**Result**: When `PairSelector` re-requests the data after a purge, it instantly gets the up-to-date, tick-perfect data from `HyperliquidAPI`'s memory.

### Gap Filling
If the bot is paused or a network glitch occurs:
1.  `get_ohlcv` checks the timestamp of the last stored candle.
2.  It compares it to `now`.
3.  If there is a gap, it effectively downloads *only* the missing candles from the API to fill the `trades.db` gap.

### Memory Leaks
*   **Structure**: `OhlcvCache` uses `collections.deque` with a strict `maxlen` (Default: 300).
*   **Behavior**: When a new candle arrives (via WebSocket), if the list is full, the oldest candle is automatically dropped.
*   **Usage**: Even tracking 300 assets x 7 timeframes consumes negligible memory (< 200MB).
