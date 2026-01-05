# Implementation Plan: Data Management Improvements

## Goal

Fix data staleness issues identified in `data_staleness_analysis.md`:
1. Load 5min data for liquidation hunter strategy
2. Fetch in-progress boundary candle on startup
3. Enforce data availability before strategy execution
4. Persist candles to DB on timeframe boundary crossing

---

## Summary of Changes

| Component | Change |
|-----------|--------|
| `_analyze_symbol()` | Add `5m` to `target_timeframes` |
| `get_ohlcv()` | Fetch in-progress candle after historical load |
| `StrategyManager` | Add data readiness check before trading |
| `OhlcvCache` | Detect boundary crossing, trigger DB write |

---

## 1. Add 5min Timeframe Loading

### File: [strategy_manager.py](file:///Users/andreacerati/projects/TradeBot/src/strategies/strategy_manager.py#L934)

```python
# Current:
target_timeframes = ['15m', '1h', '4h', '1d']

# Change to:
target_timeframes = ['5m', '15m', '1h', '4h', '1d']
```

---

## 2. Boundary Candle Handling + WebSocket Subscription

### File: [hyperliquid_api.py](file:///Users/andreacerati/projects/TradeBot/src/api/hyperliquid_api.py#L1158)

After seeding historical data:
1. Fetch current in-progress candle
2. **Immediately subscribe to WebSocket**
3. If subscription fails → add to retry queue, move on to next symbol

### Retry Queue Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                   SUBSCRIPTION FLOW                              │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Symbol A ──► fetch candle ──► subscribe ──► ✅ Success         │
│                                                                  │
│  Symbol B ──► fetch candle ──► subscribe ──► ❌ Fail            │
│                                   │                              │
│                                   ▼                              │
│                           retry_queue.add(B)                     │
│                                   │                              │
│  Symbol C ──► fetch candle ──► subscribe ──► ✅ Success ◄───────┘
│                                                                  │
│  End of cycle:                                                   │
│    Process retry_queue: Symbol B ──► re-fetch ──► re-subscribe  │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### Code Implementation

```python
class HyperliquidAPI:
    def __init__(self, ...):
        # Queue for symbols that failed initialization
        self._pending_init_symbols: set = set()
    
    def _initialize_live_data(self, symbol: str, timeframe: str, api_symbol: str, max_retries: int = 2):
        """
        Fetch in-progress candle and subscribe to WebSocket.
        If fails after retries, adds to pending queue for later retry.
        """
        for attempt in range(max_retries):
            # Step 1: Fetch current in-progress candle
            self._append_current_candle(symbol, timeframe, api_symbol)
            
            # Step 2: Subscribe to WebSocket immediately after
            try:
                self.subscribe_symbol(symbol)
                
                # Step 3: Verify subscription is active
                if symbol in self._subscribed_symbols:
                    self.logger.info(f"Initialized live data for {symbol}/{timeframe}")
                    self._pending_init_symbols.discard(symbol)  # Remove from retry queue
                    return True
            except Exception as e:
                self.logger.warning(f"WebSocket sub failed for {symbol}: {e} (attempt {attempt+1}/{max_retries})")
                time.sleep(0.3)  # Brief delay before retry
        
        # After max_retries, add to pending queue and continue
        self._pending_init_symbols.add(symbol)
        self.logger.warning(f"Deferred {symbol} initialization to retry queue")
        return False
    
    def retry_pending_subscriptions(self):
        """Called periodically to retry failed subscriptions."""
        if not self._pending_init_symbols:
            return
        
        pending = list(self._pending_init_symbols)
        self.logger.info(f"Retrying {len(pending)} pending subscriptions: {pending}")
        
        for symbol in pending:
            # Re-attempt with fresh data
            api_symbol = self._get_api_symbol(symbol)
            for tf in ['5m', '15m', '1h', '4h']:
                self._initialize_live_data(symbol, tf, api_symbol, max_retries=1)
                
            # Rate limit: small delay between symbols
            time.sleep(0.2)

def _append_current_candle(self, symbol: str, timeframe: str, api_symbol: str):
    """Fetch in-progress candle to ensure no boundary gap."""
    now_ms = int(time.time() * 1000)
    interval_ms = self._get_interval_ms(timeframe)
    current_bar_start = now_ms - (now_ms % interval_ms)
    
    candles = self.info.candles_snapshot(api_symbol, timeframe, current_bar_start, now_ms)
    if candles:
        bar = {
            'time': candles[-1]['t'] // 1000,
            'open': float(candles[-1]['o']),
            'high': float(candles[-1]['h']),
            'low': float(candles[-1]['l']),
            'close': float(candles[-1]['c']),
            'volume': float(candles[-1]['v']),
        }
        dq = self.ohlcv_cache.cache[symbol][timeframe]
        # Only append if not already present
        if not dq or dq[-1].get('time') != bar['time']:
            dq.append(bar)
```

### Integration with Trading Loop

```python
# In StrategyManager.run_trading_cycle():
def run_trading_cycle(self, ...):
    # At start of each cycle, retry any pending subscriptions
    self.market_api.retry_pending_subscriptions()
    
    # ... rest of trading logic ...
```

> [!IMPORTANT]
> - The order matters: Fetch current candle FIRST, then subscribe
> - Failed symbols are queued and retried at the start of each trading cycle
> - Rate limiting: 0.2s delay between symbol retries to avoid API throttling

---

## 3. Strategy Data Readiness Check

### Current Data Flow (Problem)

```
┌─────────────────────────────────────────────────────────────────┐
│                      CURRENT REAL-TIME DATA FLOW                │
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
└─────────────────────────────────────────────────────────────────┘
```

### Data Source Priority (How `get_ohlcv()` Works)

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

### File: [strategy_manager.py](file:///Users/andreacerati/projects/TradeBot/src/strategies/strategy_manager.py)

Add comprehensive readiness validation before trading. **All conditions must pass:**

1. ✅ Historical candles loaded for all required timeframes
2. ✅ Current in-progress candle fetched for each timeframe
3. ✅ WebSocket subscribed for the symbol
4. ✅ WebSocket actively receiving data (last tick within threshold)

```python
def _is_data_ready_for_symbol(self, symbol: str) -> bool:
    """
    Check if all required data is available for trading.
    Returns False if ANY condition is not met.
    """
    required_timeframes = ['5m', '15m', '1h', '4h']
    
    # 1. Check historical data availability for all timeframes
    for tf in required_timeframes:
        ohlcv = self.market_api.get_ohlcv(symbol, tf, limit=20)
        if ohlcv is None or len(ohlcv) < 20:
            self.logger.debug(f"[{symbol}] Insufficient historical data for {tf}")
            return False
    
    # 2. Check current candle exists (not stale)
    for tf in required_timeframes:
        cached = self.market_api.ohlcv_cache.get(symbol, tf)
        if not cached:
            self.logger.debug(f"[{symbol}] No cached data for {tf}")
            return False
        
        last_bar_time = cached[-1].get('time', 0)
        expected_bar_time = self.market_api.ohlcv_cache._get_bar_key(time.time(), tf)
        if last_bar_time != expected_bar_time:
            self.logger.debug(f"[{symbol}/{tf}] Current candle not present (last={last_bar_time}, expected={expected_bar_time})")
            return False
    
    # 3. Check WebSocket subscription active
    if symbol not in self.market_api._subscribed_symbols:
        self.logger.debug(f"[{symbol}] WebSocket not subscribed")
        return False
    
    # 4. Check WebSocket data is fresh (recent tick received)
    if not self.market_api.health_monitor.is_ws_data_fresh():
        self.logger.debug(f"[{symbol}] WebSocket data stale")
        return False
    
    return True

# In _analyze_symbol():
def _analyze_symbol(self, symbol: str, timestamp: datetime = None):
    # NEW: Check data readiness before any analysis
    if not self._is_data_ready_for_symbol(symbol):
        self.logger.info(f"Skipping {symbol}: data not ready for trading")
        return
    
    # ... existing logic ...
```

> [!WARNING]
> This check runs on every trading cycle. If a symbol fails readiness, it's skipped until all conditions pass. This prevents trades based on stale or incomplete data.

---

## 4. Write-on-Boundary DB Persistence

### Proposed Data Flow (After Implementation)

```
┌─────────────────────────────────────────────────────────────────┐
│                      NEW DATA FLOW (WITH FIX)                    │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  WebSocket ───────► OhlcvCache ◄───────── Strategies            │
│     Tick               │   ▲               (reads)             │
│                        │   │                                    │
│                        ▼   │ (on startup)                       │
│              ON BOUNDARY CROSSING                               │
│              ─────────────────────                               │
│              When new bar starts,                               │
│              write PREVIOUS bar to DB                           │
│                        │                                        │
│                        ▼                                        │
│                   SQLite DB ──────────► Persisted on restart    │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### File: [hyperliquid_api.py](file:///Users/andreacerati/projects/TradeBot/src/api/hyperliquid_api.py#L281)

Modify `_update_bar_for_timeframe` to detect boundary crossing:

```python
def _update_bar_for_timeframe(self, timeframe: str, dq: deque, price: float, volume: float, ts: float, symbol: str = None):
    key = self._get_bar_key(ts, timeframe)
    if key is None:
        return
    
    if dq and dq[-1].get("time") == key:
        bar = dq[-1]
    else:
        # BOUNDARY CROSSED: Previous bar is complete
        if dq and self.on_bar_complete_callback:
            completed_bar = dq[-1]
            self.on_bar_complete_callback(symbol, timeframe, completed_bar)
        
        # Create new bar
        bar = {"time": key, "open": price, "high": price, "low": price, "close": price, "volume": 0.0}
        dq.append(bar)
    
    bar["close"] = price
    bar["high"] = max(bar["high"], price)
    bar["low"] = min(bar["low"], price)
    bar["volume"] = bar.get("volume", 0.0) + (volume or 0.0)
```

### New callback in HyperliquidAPI:

```python
def __init__(self, ...):
    self.ohlcv_cache.on_bar_complete_callback = self._on_bar_complete

def _on_bar_complete(self, symbol: str, timeframe: str, bar: dict):
    """Called when a candle period closes. Persist to DB."""
    if self.market_db:
        df = pd.DataFrame([bar])
        df['timestamp'] = pd.to_datetime(df['time'], unit='s')
        df.set_index('timestamp', inplace=True)
        self.market_db.insert_market_data(df, symbol, timeframe)
        self.logger.debug(f"Persisted {symbol}/{timeframe} bar @ {bar['time']}")
```

---

## 5. Database Migration

### Approach: Full wipe of market_data table

For safety and simplicity, wipe the entire `market_data` table. The bot will repopulate it correctly on next startup with proper boundary candle handling.

**SQL Migration:**
```sql
DELETE FROM market_data;
VACUUM;
```

**Or via Python:**
```python
# Run before bot starts
db.execute("DELETE FROM market_data")
db.execute("VACUUM")
```

> [!NOTE]
> First startup after wipe will be slower as all historical data is re-fetched from API. Subsequent startups use gap-fill.

---

## Implementation Order

| Step | Task | Complexity |
|------|------|------------|
| 1 | Add `5m` to `target_timeframes` | Low |
| 2 | Implement `_append_current_candle` | Medium |
| 3 | Add `_is_data_ready_for_symbol` check | Medium |
| 4 | Add `on_bar_complete_callback` for DB writes | Medium |
| 5 | Run migration to clean recent candles | Low |
| 6 | Test end-to-end | Medium |

---

## Testing Plan

1. **Unit Test**: Verify `_append_current_candle` fetches and appends correctly
2. **Unit Test**: Verify `_is_data_ready_for_symbol` blocks trading when data missing
3. **Integration Test**: Verify DB write occurs at boundary crossing
4. **E2E Test**: Start bot, verify no gaps in candle data across restart
