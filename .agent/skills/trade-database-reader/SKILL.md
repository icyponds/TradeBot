---
name: trade-database-reader
description: Guidelines on how to query and read the TradeBot SQLite database (`data/trades.db`) including live position tracking, historical trades, market data, and backtest results isolation.
---

# Trade Database Reader

The `TradeBot` ecosystem uses SQLite (`data/trades.db`) for tracking live positions, historical trade statistics, and historical market data. The underlying structure and querying patterns are defined in `src/utils/trade_database.py`.

## Core Tables & Usage

### 1. Current Live Positions (`live_positions`)
- **Purpose**: Stores active, open positions that the bot is managing.
- **Fields of Interest**: `position_id`, `strategy`, `symbol`, `side` (long/short/neutral), `size`, `entry_price`, `entry_time`.
- **Querying Rule**: Query this table when you need to understand what the bot is currently holding or if an exit order is pending.

### 2. Multi-Leg Strategies (`live_position_legs`)
- **Purpose**: Stores the individual execution legs that make up a multi-leg strategy (e.g. Statistical Arbitrage pairs).
- **Structure**: Each leg has an entry resolving back to the main `live_positions` table via the `position_id` foreign key.
- **Querying Rule**: Query this table when debugging complex strategies or verifying that individual spot/perp legs executed properly.

### 3. Historical Trades (`trades`)
- **Purpose**: Stores executed and closed trades for historical performance tracking.
- **Fields of Interest**: `pnl`, `pnl_percentage`, `exit_reason`, `entry_time`, `exit_time`.
- **Querying Rule**: Query this table for performance metrics, historical debugging, and PnL generation.

### 4. Historical Market Data (`market_data`)
- **Purpose**: Stores historical OHLCV (Open/High/Low/Close/Volume) candle data for all tracked symbols and timeframes. This is the **primary data source for backtesting**.
- **Schema**:
  ```sql
  CREATE TABLE market_data (
      symbol TEXT NOT NULL,
      timeframe TEXT NOT NULL,
      timestamp TIMESTAMP NOT NULL,
      open REAL NOT NULL,
      high REAL NOT NULL,
      low REAL NOT NULL,
      close REAL NOT NULL,
      volume REAL NOT NULL,
      PRIMARY KEY (symbol, timeframe, timestamp)
  ) WITHOUT ROWID;
  ```
- **Scale**: ~1.5M rows, 283 symbols, 5 timeframes (15m, 1h, 4h, 1d), dating from 2024-11-29 to present.
- **Index**: `idx_market_data_range ON market_data(symbol, timeframe, timestamp)` for efficient range queries.
- **Common Queries**:
  ```sql
  -- Check data availability for a symbol
  SELECT timeframe, COUNT(*), MIN(timestamp), MAX(timestamp)
  FROM market_data WHERE symbol = 'BTC'
  GROUP BY timeframe;

  -- Get 1h candles for backtesting
  SELECT * FROM market_data
  WHERE symbol = 'BTC' AND timeframe = '1h'
    AND timestamp BETWEEN '2025-12-01' AND '2026-02-01'
  ORDER BY timestamp;

  -- Find symbols with the most data
  SELECT symbol, COUNT(*) as candles FROM market_data
  WHERE timeframe = '1h' GROUP BY symbol ORDER BY candles DESC LIMIT 20;
  ```

## Backtest Isolation
- **Rule**: Backtesting runs should never interact directly with the core un-prefixed tables.
- **Implementation**: Backtest instances initialize `TradeDatabase(table_prefix="backtest_")`. 
- **Querying Rule**: When analyzing backtest behavior, always query `backtest_trades`, `backtest_equity_snapshots`, `backtest_live_positions`, etc. to ensure you aren't confusing live production states with local simulations.

## General Guideline for Developers
- For data inspection tasks, utilize standard SQLite commands (e.g., `sqlite3 data/trades.db "SELECT * FROM live_positions;"`) via bash.
- Be aware of the `metadata` table which houses the `schema_version`.
