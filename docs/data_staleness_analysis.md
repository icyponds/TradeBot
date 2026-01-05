# Data Staleness and Strategy Execution Safety Analysis

## Executive Summary

**⚠️ POTENTIAL ISSUE IDENTIFIED**: There is **no explicit timestamp freshness validation** on historical OHLCV data before strategies execute. The codebase has safeguards for *missing* data but not for *stale* data.

---

## 1. Timeframes Loaded

The bot loads the following timeframes in `_analyze_symbol()`:

```python
target_timeframes = ['15m', '1h', '4h', '1d']
```

| Timeframe | Used By Strategies |
|-----------|-------------------|
| 5m | `liquidation_hunter_5m` (but NOT in standard fetch) |
| 15m | `stat_arb_15m`, `ou_mean_reversion_15m`, `adaptive_grid_15m` |
| 1h | `stat_arb_1h`, `ou_mean_reversion_1h`, `funding_arb_1h`, `vol_breakout_1h`, `sentiment_ml_1h` |
| 4h | `stat_arb_4h`, `csm_4h` |
| 1d | Regime detection, correlation analysis |

**Issue**: The `liquidation_hunter_5m` strategy specifies `5m` timeframe but `_analyze_symbol` only fetches `['15m', '1h', '4h', '1d']`. The 5m timeframe is NOT fetched.

---

## 2. Existing Safeguards

### 2.1 Data Availability Check
```python
# In _analyze_symbol()
if not self.market_api.is_data_available(symbol):
    self.logger.debug(f"Insufficient data for {symbol}, skipping")
    return
```
This checks if **any** data exists for the symbol, NOT if the data is **fresh**.

### 2.2 Minimum Candle Count
```python
# In _analyze_symbol()
if df is not None and len(df) >= 20:
    ohlcv_dict[tf] = df
    has_sufficient_data = True
```
Ensures at least 20 candles exist. Does NOT check the timestamp of the latest candle.

### 2.3 WebSocket Staleness (Connection Health Only)
```python
# In ConnectionHealthMonitor
ws_stale_threshold = 60.0  # seconds
```
This monitors WebSocket **connection freshness**, NOT historical OHLCV data freshness.

---

## 3. Gap in Safety Logic

### The Problem
When `get_ohlcv()` is called:
1. It checks the database for cached data
2. If data exists, it gap-fills from the API
3. If gap-fill fails (API error, rate limit, etc.), it **silently returns old data**

```python
# In get_ohlcv() - No staleness check before returning cached data
if not df.empty:
    # ... gap fill attempt ...
    # Return from cache (even if gap-fill failed and data is hours old)
    return df.tail(limit)
```

### Scenario: Stale Data Execution
1. Bot starts, cache has data from 3 hours ago
2. Gap-fill fails due to circuit breaker
3. Strategy receives 3-hour-old data
4. Strategy generates trade signal based on stale candles
5. Trade executes with potentially incorrect assumptions

---

## 4. Recommendations

### Option A: Add Freshness Validation (Low Risk)
Add a timestamp check before returning cached data:

```python
# Proposed check in get_ohlcv()
if not df.empty:
    latest_ts = df.index.max()
    max_age = pd.Timedelta(hours=2)  # Configurable threshold
    
    if pd.Timestamp.now() - latest_ts > max_age:
        self.logger.warning(f"Data for {symbol} is stale ({latest_ts}), skipping cache")
        # Force full fetch or return None
```

### Option B: Per-Strategy Freshness Requirement
Each strategy defines its own `max_data_age`:

```python
class SomeStrategy:
    max_data_age = pd.Timedelta(minutes=30)  # 15m strategy needs fresh data
```

### Option C: Gap-Fill Failure Propagation
Make gap-fill failures explicit instead of silent fallback:

```python
if gap_fill_failed and latest_ts < (now - threshold):
    raise StaleDataError(f"Cannot get fresh data for {symbol}")
```

---

## 5. Summary Table

| Component | Status | Notes |
|-----------|--------|-------|
| WebSocket connection health | ✅ Protected | 60s staleness threshold |
| Missing data check | ✅ Protected | Skips if < 20 candles |
| Historical OHLCV freshness | ❌ **NOT Protected** | No timestamp validation |
| Gap-fill failure handling | ⚠️ Silent fallback | Returns old data on failure |
| Per-strategy timeframe fetch | ⚠️ Incomplete | 5m not in default list |

---

## 6. File References

- [strategy_manager.py:_analyze_symbol](file:///Users/andreacerati/projects/TradeBot/src/strategies/strategy_manager.py#L909-L991) - Main data fetching
- [hyperliquid_api.py:get_ohlcv](file:///Users/andreacerati/projects/TradeBot/src/api/hyperliquid_api.py#L1196-L1293) - Gap-fill logic
- [settings.py](file:///Users/andreacerati/projects/TradeBot/src/config/settings.py#L230-L260) - Strategy timeframe configuration

---

## 7. WebSocket Candle Generation and Persistence

### 7.1 How Real-Time Data Flows

```
WebSocket Tick → update_ohlcv_from_tick() → OhlcvCache (IN-MEMORY) → get_ohlcv() returns data
```

| Component | Type | Persistent? |
|-----------|------|-------------|
| `OhlcvCache` | Python `deque` | ❌ **NO** |
| `market_data` table | SQLite | ✅ YES |

### 7.2 What `OhlcvCache` Does

The `OhlcvCache` class ([hyperliquid_api.py:232-293](file:///Users/andreacerati/projects/TradeBot/src/api/hyperliquid_api.py#L232-L293)):

1. **Receives ticks** via `update_from_tick(symbol, price, volume, ts)`
2. **Aggregates into candles** based on timeframe boundaries
3. **Stores in `deque`** (fixed-size, in-memory only)
4. **Never writes to SQLite**

```python
# OhlcvCache._update_bar_for_timeframe()
if dq and dq[-1].get("time") == key:
    bar = dq[-1]  # Update existing bar
else:
    bar = {"time": key, "open": price, ...}  # New bar on timeframe boundary
    dq.append(bar)  # Append to in-memory deque ONLY
```

### 7.3 When Database Writes Happen

Database writes **ONLY occur during `get_ohlcv()` API calls**:

1. On initial data fetch (no cache exists)
2. On gap-fill (fetching missing data from API)

**WebSocket-generated candles are NEVER written to SQLite.**

### 7.4 ❌ Critical Gap: No Real-Time Persistence

**Problem**: The bot runs, WebSocket generates 100 new 15m candles over 25 hours. Then:
- Bot restarts
- All 100 in-memory candles are **LOST**
- Database only has data from last `get_ohlcv()` API call
- Gap-fill must re-fetch from API

### 7.5 Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                      REAL-TIME DATA FLOW                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  WebSocket        OhlcvCache           Strategies   SQLite      │
│  ──────────       ──────────           ──────────   ──────      │
│                                                                 │
│     Tick  ──────► update_from_tick()                            │
│                       │                                         │
│                       ▼                                         │
│                   In-Memory           get_ohlcv()               │
│                   deque ◄──────────── read ◄───── Strategies    │
│                       │                                         │
│                       ▼                                         │
│                   ❌ NO WRITE ───────────────────► market_data  │
│                   to SQLite                        (DB)         │
│                                                                 │
│  API Fetch ─────────────────────────────────────► market_data   │
│  (gap-fill)                                        (DB)         │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 7.6 Recommendations

#### Option D: Periodic Flush to Database
Add a background task that periodically writes `OhlcvCache` to SQLite:

```python
# Proposed: Every 5 minutes, flush cache to DB
def _flush_ohlcv_cache_to_db(self):
    for symbol, timeframes in self.ohlcv_cache.cache.items():
        for tf, dq in timeframes.items():
            bars = list(dq)
            if bars:
                df = pd.DataFrame(bars)
                df['timestamp'] = pd.to_datetime(df['time'], unit='s')
                df.set_index('timestamp', inplace=True)
                self.market_db.insert_market_data(df, symbol, tf)
```

#### Option E: Write-Through Cache
Modify `update_from_tick()` to write to DB when a new bar is created:

```python
# When new bar is appended (timeframe boundary crossed)
if new_bar_created and self.market_db:
    self.market_db.insert_single_candle(symbol, timeframe, bar)
```

---

## 8. Updated Summary Table

| Component | Status | Notes |
|-----------|--------|-------|
| WebSocket connection health | ✅ Protected | 60s staleness threshold |
| Missing data check | ✅ Protected | Skips if < 20 candles |
| Historical OHLCV freshness | ❌ **NOT Protected** | No timestamp validation |
| Gap-fill failure handling | ⚠️ Silent fallback | Returns old data on failure |
| Per-strategy timeframe fetch | ⚠️ Incomplete | 5m not in default list |
| Real-time candle persistence | ❌ **NOT Implemented** | In-memory only, lost on restart |
| WebSocket → SQLite sync | ❌ **NOT Implemented** | Only API calls write to DB |

---

## 10. Implementation Considerations

### 10.1 How Strategies Currently Consume Data

```python
# In _analyze_symbol() → market_api.get_ohlcv()
df = self.market_api.get_ohlcv(symbol, tf, self.ohlcv_limit)
```

The `get_ohlcv()` method uses a **tiered data source**:

```
┌─────────────────────────────────────────────────────────────────┐
│                    get_ohlcv() DATA PRIORITY                     │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  1. IN-MEMORY CACHE ──────► ohlcv_cache.get()                   │
│     (fastest, ~0ms)         Returns if bars >= limit            │
│         │                                                       │
│         ▼ cache miss                                            │
│  2. SQLITE DATABASE ──────► market_db.get_market_data()         │
│     (fast, ~5-10ms)         + gap-fill from API                 │
│         │                   Seeds in-memory cache               │
│         ▼ empty/error                                           │
│  3. API FETCH ────────────► candles_snapshot()                  │
│     (slow, ~100-500ms)      Seeds both cache and DB             │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 10.2 Key Insight: Unified Interface

**Strategies don't need to change.** They all call `get_ohlcv()` which abstracts the data source.

The fix only needs to ensure in-memory cache → DB synchronization. The read path already works correctly.

### 10.3 Where WebSocket Updates Go

```python
# WebSocket tick received
self.update_ohlcv_from_tick(symbol, price, volume, ts)
    ↓
self.ohlcv_cache.update_from_tick(...)  # IN-MEMORY ONLY
    ↓
get_ohlcv() → returns from ohlcv_cache  # Strategy gets fresh data
    ↓
❌ NEVER writes to SQLite              # Lost on restart
```

### 10.4 Best Implementation Approach

**Recommendation: Hybrid Approach (Option D + minimal changes)**

```
┌─────────────────────────────────────────────────────────────────┐
│                      PROPOSED DATA FLOW                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  WebSocket ───────► OhlcvCache ◄───────── Strategies            │
│     Tick               │   ▲               (reads)             │
│                        │   │                                    │
│                        ▼   │ (on startup)                       │
│              Periodic Flush every 5 min                         │
│                        │                                        │
│                        ▼                                        │
│                   SQLite DB                                     │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Why this approach:**
1. **No changes to strategies** - They keep reading from `get_ohlcv()`
2. **No changes to WebSocket handler** - Keeps updating in-memory cache
3. **Single point of change** - Add background flush task in `HyperliquidAPI`
4. **Graceful degradation** - If flush fails, strategies still work (just lose data on restart)

### 10.5 Implementation Steps

| Step | Component | Change |
|------|-----------|--------|
| 1 | `HyperliquidAPI.__init__` | Start background flush thread |
| 2 | `HyperliquidAPI._flush_cache_to_db` | New method: iterate cache, call `insert_market_data` |
| 3 | `HyperliquidAPI.stop` | Stop flush thread, do final flush |

**Code Outline:**
```python
def __init__(self, ...):
    ...
    self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
    self._flush_interval = 300  # 5 minutes
    self._flush_stop = threading.Event()

def _flush_loop(self):
    while not self._flush_stop.wait(self._flush_interval):
        self._flush_cache_to_db()

def _flush_cache_to_db(self):
    if not self.market_db:
        return
    for symbol, timeframes in self.ohlcv_cache.cache.items():
        for tf, dq in timeframes.items():
            bars = list(dq)[-50:]  # Last 50 bars to avoid duplicates
            if bars:
                df = pd.DataFrame(bars)
                df['timestamp'] = pd.to_datetime(df['time'], unit='s')
                df.set_index('timestamp', inplace=True)
                self.market_db.insert_market_data(df, symbol, tf)
```

### 10.6 Impact Assessment

| Concern | Status | Notes |
|---------|--------|-------|
| Strategy execution changes | ❌ None required | Uses same `get_ohlcv()` interface |
| WebSocket handler changes | ❌ None required | Keeps updating in-memory cache |
| Database schema changes | ❌ None required | Uses existing `market_data` table |
| Performance impact | ⚠️ Minimal | Background thread, batched writes every 5 min |
| Data consistency | ⚠️ Eventual | 5-min lag between in-memory and DB |

---

## 11. Boundary Candle Handling (Historical → WebSocket Transition)

### 11.1 The Boundary Problem

**Scenario**: Bot starts at 6:27 PM.

| Component | What it has |
|-----------|-------------|
| API `candles_snapshot` | Returns completed candles only (5PM, 6PM). Does NOT include in-progress 6PM→7PM candle |
| WebSocket subscription | Starts receiving ticks at 6:27 PM |
| In-memory cache after seed | Last bar ends at 6PM |

**What happens at first tick (6:27:01 PM)?**

```python
# In _update_bar_for_timeframe()
key = _get_bar_key(6:27:01)  # → 6:00 PM (floored to hour)

if dq[-1].get("time") == key:  # Last bar is 5PM, not 6PM!
    bar = dq[-1]  # ❌ Would update 5PM bar incorrectly
else:
    # Creates NEW bar starting at 6PM with:
    bar = {"time": 6:00PM, "open": 6:27PM_price, ...}  # ⚠️ WRONG OPEN!
```

### 11.2 ❌ The Gap

```
┌────────────────────────────────────────────────────────────────────┐
│                         BOUNDARY GAP                               │
├────────────────────────────────────────────────────────────────────┤
│                                                                    │
│  API Data:     [5PM]  [6PM]                                       │
│                  │      │                                          │
│                  │      └─ Completed candle                        │
│                  └──────── Completed candle                        │
│                                                                    │
│  Current Time: ─────────────────────────────────────► 6:27 PM      │
│                            ↑                                       │
│                            │                                       │
│                       GAP: 6PM-6:27PM price action MISSING         │
│                       from the 6PM candle OHLCV                    │
│                                                                    │
│  First Tick Creates:  [6PM] ← WRONG OPEN (uses 6:27PM price)      │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
```

### 11.3 Research Findings

1. **Hyperliquid API Behavior**: `candles_snapshot` returns **only completed candles** with distinct close time timestamps. The in-progress candle is NOT included.

2. **OhlcvCache.seed() Behavior**: Simply appends historical bars to deque—does NOT handle in-progress candle initialization.

3. **update_from_tick() Behavior**: Creates new bar with first tick price as `open` if no matching bar exists.

4. **Impact**: The boundary candle (6PM in our example) will have:
   - **Incorrect OPEN**: First tick price (6:27PM) instead of actual 6PM open
   - **Incorrect HIGH/LOW**: Missing 27 minutes of price action
   - **Incorrect VOLUME**: Missing 27 minutes of volume

### 11.4 Recommendations

#### Option F: Fetch In-Progress Candle on Subscription

After seeding historical data, immediately fetch current candle state:

```python
def seed_with_current_candle(self, symbol: str, timeframe: str, bars: list):
    # Seed historical bars
    self.seed(symbol, timeframe, bars)
    
    # Fetch current in-progress candle from API
    now = time.time() * 1000
    current_bar_start = self._get_bar_key(now / 1000, timeframe) * 1000
    current_candle = self.info.candles_snapshot(symbol, timeframe, current_bar_start, now)
    
    if current_candle:
        bar = {
            'time': current_candle[0]['t'] // 1000,
            'open': float(current_candle[0]['o']),
            'high': float(current_candle[0]['h']),
            'low': float(current_candle[0]['l']),
            'close': float(current_candle[0]['c']),
            'volume': float(current_candle[0]['v']),
        }
        self.cache[symbol][timeframe].append(bar)
```

#### Option G: Modified end_time for API Fetch

Request candles up to `now` instead of `limit * interval_ms` ago:

```python
# Current (problematic):
end_time = int(time.time() * 1000)
candles = self.info.candles_snapshot(symbol, timeframe, start_time, end_time)
# API still returns only completed candles!

# After fetch, explicitly request current candle:
current_bar_start = end_time - (end_time % interval_ms)
current = self.info.candles_snapshot(symbol, timeframe, current_bar_start, end_time)
```

### 11.5 Verification Test

To confirm this issue exists:
```bash
# Start bot and immediately check first candle in cache
# Compare 'open' price to actual market open at candle boundary
```

---

## 9. File References (Updated)

- [OhlcvCache](file:///Users/andreacerati/projects/TradeBot/src/api/hyperliquid_api.py#L232-L293) - In-memory candle aggregation
- [OhlcvCache.seed](file:///Users/andreacerati/projects/TradeBot/src/api/hyperliquid_api.py#L250-L257) - Historical data seeding
- [_update_bar_for_timeframe](file:///Users/andreacerati/projects/TradeBot/src/api/hyperliquid_api.py#L281-L293) - Tick processing logic
- [update_ohlcv_from_tick](file:///Users/andreacerati/projects/TradeBot/src/api/hyperliquid_api.py#L1290-L1294) - Tick processing
- [insert_market_data](file:///Users/andreacerati/projects/TradeBot/src/utils/trade_database.py#L550-L588) - Database persistence
- [strategy_manager.py:_analyze_symbol](file:///Users/andreacerati/projects/TradeBot/src/strategies/strategy_manager.py#L909-L991) - Main data fetching
- [hyperliquid_api.py:get_ohlcv](file:///Users/andreacerati/projects/TradeBot/src/api/hyperliquid_api.py#L1158-L1288) - Gap-fill logic
