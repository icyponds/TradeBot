---
name: backtesting-trading-strategies
description: |
  Backtest crypto and traditional trading strategies against historical data.
  Calculates performance metrics (Sharpe, Sortino, max drawdown), generates equity curves,
  and optimizes strategy parameters. Use when user wants to test a trading strategy,
  validate signals, or compare approaches.
  Trigger with phrases like "backtest strategy", "test trading strategy", "historical performance",
  "simulate trades", "optimize parameters", or "validate signals".
allowed-tools: Read, Write, Edit, Grep, Glob, Bash(python:*)
version: 3.2.0
---

# Backtesting Trading Strategies

## Overview

All backtesting uses the TradeBot's own engine against historical OHLCV data stored locally in `data/trades.db` (`market_data` table). This tests the EXACT same strategy code that runs in production.

**Do NOT use Yahoo Finance or any external data source.** The local DB contains 1.5M+ candles across 283 symbols and 5 timeframes (15m, 1h, 4h, 1d) dating back to late 2024.

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/run_backtest.py` | **Primary runner** — config setup, DB cleanup, symbol selection, PairSelector injection, runs `BacktestEngine`, prints summary |
| `scripts/analyze_backtest.py` | **Post-hoc analysis** — reads `backtest_*` tables via `TradeDatabase` API for detailed per-strategy metrics, streaks, capital analysis |
| `src/backtesting/backtest_engine.py` | **Core engine** (library) — loads data, initializes `MockMarketAPI` + `StrategyManager`, steps through time |
| `src/backtesting/mock_market_api.py` | **Simulated exchange** — mirrors `HyperliquidAPI` interface for backtests |

## Usage

### Running a Backtest

```bash
# Default: backtest over 90 days of data
python scripts/run_backtest.py

# Specific number of days
python scripts/run_backtest.py --days 30

# Specific date range
python scripts/run_backtest.py --start 2025-12-01 --end 2026-02-01

# Random window (pick N random days from available data)
python scripts/run_backtest.py --random-window 14

# Override strategy parameters for experimentation (repeatable)
python scripts/run_backtest.py --days 14 \
  --param stat_arb.z_score_threshold=2.5 \
  --param ou_mean_reversion.zscore_entry=2.0 \
  --param volatility_breakout.squeeze_threshold=0.08
```

> [!IMPORTANT]
> Use `--param` for experimental parameter testing. **Do NOT hardcode strategy parameters in `run_backtest.py`** — production defaults live in `src/config/settings.py` and the backtest should inherit them automatically.

### Analyzing Results

After a backtest run, use `analyze_backtest.py` for a detailed report:
```bash
python scripts/analyze_backtest.py
```

Or query the DB directly:
```bash
sqlite3 data/trades.db "SELECT strategy, COUNT(*), ROUND(SUM(pnl),2) FROM backtest_trades GROUP BY strategy;"
```

## How It Works

1. Loads config from `src/config/settings.py`
2. Queries `data/trades.db` `market_data` table for available symbols across required timeframes
3. Caps to top 20 symbols for performance
4. Overrides strategy lookback periods for the backtest window
5. Cleans all `backtest_*` tables for a fresh run
6. Initializes `BacktestEngine` with `MockMarketAPI` (simulated exchange)
7. Pre-populates `DynamicPairSelector.selected_pairs` and `ready_pairs` (the background fetcher does NOT run in backtest mode)
8. Steps through time at 15-minute intervals, running `StrategyManager.run_trading_cycle()`
9. Force-closes all positions at teardown and prints a summary report

## Critical Architecture Rules

- **Config overrides BEFORE engine init**: All strategy parameter changes (lookbacks, thresholds, enabled instances) MUST be applied to the config dict BEFORE `BacktestEngine(config)` is called. The engine snapshots config at init time.
- **PairSelector injection required**: The `DynamicPairSelector` background fetcher only runs in live mode. In backtests, you MUST manually inject symbols into `pair_selector.selected_pairs` and `pair_selector.ready_pairs` after engine init. Without this, `get_ready_pairs()` returns `[]` and no analysis happens.
- **Backtest isolation**: Results are written to `backtest_` prefixed tables (e.g. `backtest_trades`, `backtest_live_positions`) via `TradeDatabase(table_prefix="backtest_")`.
- **Clean slate**: Always clear all `backtest_*` tables before a new run to prevent ghost position pollution.

## Asset Universe Selection

Backtests MUST cover **both crypto-native perps and HIP-3 perps** (builder-dex
assets: equities, indices, commodities, FX). Strategy verdicts based on crypto
alone do not generalize to the HIP-3 books the bot also trades.

Rules:
1. **Include HIP-3 assets** alongside crypto in every strategy evaluation.
   HIP-3 symbols are stored dex-prefixed (e.g. `xyz:NVDA`, `cash:GOLD`).
2. **Prefer the `xyz` dex for HIP-3** — it has the deepest liquidity
   (XYZ100 ~$1B/day) and the longest history (from Nov 2025).
3. **Dedupe duplicate underlyings across dexes** (e.g. `cash:NVDA` vs
   `flx:NVDA` vs `xyz:NVDA`): keep the listing with the most history and the
   highest notional volume (in practice this is almost always `xyz:`).
4. **Require reasonable history**: only include symbols whose candle coverage
   spans >=~70% of the backtest window; rank the remainder by average notional
   volume (close x volume) and take the top N. Do NOT select alphabetically.

`scripts/run_backtest.py` implements this ranking automatically; pass
`--max-symbols N` to widen the universe.

## Data Source

All data comes from `data/trades.db` `market_data` table. See the `trade-database-reader` skill for full schema details.
Refresh stale data with `scripts/refresh_market_data.py` (public info endpoint,
rate-limit aware, resumable; updates candles + funding for crypto natives and
`xyz` HIP-3 assets).
