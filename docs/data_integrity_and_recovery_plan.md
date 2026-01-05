# Data Integrity and Recovery Analysis

## 1. The Core Problem: Partial Data Risk

### Scenario
The bot relies on a "Hybrid" data approach:
1.  **In-Memory (Fast)**: Accumulates WebSocket ticks into candles.
2.  **Database (Persistent)**: Stores completed candles for historical analysis.

**The Vulnerability**:
Currently, when a candle closes (e.g., 5-minute candle ends), the bot takes the **in-memory accumulated candle** and writes it to the database.

If the WebSocket connection was unstable, interrupted, or "quietly dead" during that 5-minute window:
*   **Missing Ticks**: The in-memory candle will have incomplete volume and High/Low data.
*   **Corrupt History**: This partial candle is written to the database as "truth".
*   **Permanent Damage**: Future runs will load this corrupt candle from the DB, leading to incorrect strategy signals.

### "Zombie Bot" Risk
If the bot continues running but stops receiving data for 50 minutes of a 1-hour candle, it will write a candle with extremely low volume and incorrect price range.

---

## 2. Analysis of Recovery Scenarios

### A. Unexpected Shutdown (Crash/Force Kill)
*   **What happens**: The bot dies before the current candle closes.
*   **Result**: No write to DB occurs.
*   **Recovery**: On restart, the logic detects the gap in the DB and uses `candles_snapshot` (API) to fetch the missing history.
*   **Integrity**: ✅ **SAFE**. The API fetch gets the true, complete candle.

### B. Connectivity Loss (Intermittency)
*   **What happens**: Bot misses 20% of ticks but remains running.
*   **Result**: At the end of the period, it writes the 80% complete candle to DB.
*   **Integrity**: ❌ **COMPROMISED**. The DB now holds invalid data.

---

## 3. Recommendation: "Verify-on-Write" Pattern

To guarantee data integrity, we must **never trust the in-memory aggregation for persistence**. In-memory data is for *speed* (strategy inputs); API data is for *truth* (historical records).

### The Proposed Mechanism
Instead of flushing the in-memory object to the database, we should fetch the official finalized candle from the Hyperliquid API at the moment of closure.

#### New Data Flow at Boundary Crossing:

1.  **Event**: Timeframe boundary (e.g., 10:00:00) is crossed.
2.  **Trigger**: `_on_bar_complete` is called.
3.  **Action**: 
    *   Spawn a background task (non-blocking to WebSocket thread).
    *   Wait a small buffer (e.g., 2 seconds) to ensure Exchange has finalized the candle.
    *   **FETCH**: Call `candles_snapshot` for that specific completed timeframe.
    *   **WRITE**: Persist the *API-fetched* candle to `market_data`.
    *   **HEAL**: Update the in-memory cache with this API candle (correcting any local drift).

### Why this works
*   **Source of Truth**: The Exchange's API is the definitive record.
*   **Self-Healing**: If the WebSocket missed ticks, this "sync point" corrects the local state every candle.
*   **Resilience**: Network drops during the candle don't corrupt the DB record. If the Fetch fails, it can be retried or simply skipped (leaving a gap that the next restart will fill safely).

---

## 4. Implementation Guidelines

### Architecture Changes

1.  **`HyperliquidAPI`**:
    *   Modify `_on_bar_complete` to NOT write `bar` directly.
    *   Instead, queue a "Finalization Task".
    
2.  **Background Persistence Worker**:
    *   A dedicated queue/thread consuming `(symbol, timeframe, timestamp)` tuples.
    *   Performs the `candles_snapshot` (HTTP Request).
    *   Writes to DB.

### Handling Latency
*   **Concern**: This adds an HTTP call at every candle close.
*   **Mitigation**: 
    *   It is done asynchronously.
    *   Frequency is low (even with 5m timeframe and 20 symbols, it's ~4 requests/minute).
    *   Rate limits (1200/min) are well above this usage.

### Handling "Write Failure"
If the "Finalization Task" fails (e.g., API is down):
*   **Do NOT write partial data.**
*   **Do NOTHING.**
*   **Result**: The DB has a gap.
*   **Recovery**: Next time the bot restarts (or runs a gap-fill check), it will fill this gap correctly. A gap is better than corrupt data.

---

## 5. Summary of Recommended Strategy

| Scenario | Current Handling | Proposed Handling |
| :--- | :--- | :--- |
| **Normal Operation** | Write in-memory candle | Fetch official candle & Write |
| **Packet Loss** | Write corrupt candle ❌ | Fetch official candle (Corrected) ✅ |
| **Bot Crash** | Gap left (Auto-filled on start) ✅ | Gap left (Auto-filled on start) ✅ |
| **API Failure at Write** | N/A (writes memory) | Skip write (Leave safe gap) ✅ |

### Conclusion
Adopt the **"Verify-on-Write"** pattern. Treat the `OhlcvCache` as a read-only view for strategies, but use the API as the write-source for the database. This completely eliminates the risk of persisting data staleness or corruption caused by WebSocket issues.
