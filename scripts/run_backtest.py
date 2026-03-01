
import sys
import os
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from src.config.settings import load_config
from src.backtesting.backtest_engine import BacktestEngine

def generate_synthetic_data(symbol, start_date, end_date, freq='1h'):
    """Generate synthetic sine wave data for testing."""
    dates = pd.date_range(start=start_date, end=end_date, freq=freq)
    n = len(dates)
    
    # Sine wave with trend
    t = np.linspace(0, 4*np.pi, n)
    trend = np.linspace(100, 120, n)
    noise = np.random.normal(0, 0.5, n)
    
    price = trend + 5 * np.sin(t) + noise
    
    df = pd.DataFrame(index=dates)
    df['open'] = price
    df['high'] = price + 1
    df['low'] = price - 1
    df['close'] = price
    df['volume'] = 1000
    
    return df

import random

def run_smoke_test(days=None, start_str=None, end_str=None, random_window=None, param_overrides=None):
    # Increase log level and setup file logging prior to ANY imports or logic
    import logging
    
    # Ensure log directory exists
    log_dir = os.path.join(os.path.dirname(__file__), '..', 'logs')
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'backtest.log')
    
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, mode='w'), # Overwrite mode
            logging.StreamHandler(sys.stdout)
        ],
        force=True
    )
    print(f"Logging to: {log_file}")

    print("Running Backtest Smoke Test...")
    
    # 1. Config
    config = load_config()
    
    # 5. Enable Backtest Mode for PairSelector (load all assets instantly)
    config['mode'] = 'backtest'
    config['backtesting']['enabled'] = True
    
    # 2. Data
    from src.utils.trade_database import TradeDatabase
    db = TradeDatabase()
    
    # Determine default end_date from DB if available, otherwise now()
    default_end = datetime.now()
    default_start = default_end - timedelta(days=days or 90)
    
    # Get Timeframes required by current config
    required_timeframes = set([s.get('timeframe', '1h') for s in config['strategies']['instances']])
    # Ensure 1h is always present as it's often used for broad market check / funding (fallback)
    required_timeframes.add('1h')
    
    print(f"Required Timeframes: {required_timeframes}")
    
    # Dynamic Universe Selection
    print("Filtering asset universe based on data availability...")
    
    # We need a rough range to query. If user didn't specify, we look for data in the last X days.
    # Note: DB queries are fast, so checking a broad range is fine.
    query_start = datetime.strptime(start_str, "%Y-%m-%d") if start_str else default_start
    query_end = datetime.strptime(end_str, "%Y-%m-%d") if end_str else default_end
    
    symbols = db.get_available_symbols_for_timeframes(list(required_timeframes), query_start, query_end)
    
    if not symbols:
         print(f"CRITICAL: No assets found with data for all timeframes {required_timeframes} in range {query_start} to {query_end}")
         return
         
    print(f"Selected {len(symbols)} assets for backtest: {symbols[:5]}...")
    
    # Override config symbols
    config['trading']['symbols'] = symbols
    
    # Retrieve actual data range for the PRIMARY asset (usually BTC or first in list) to permit precise trimming
    # But for dynamic mode, we trust the query_start/end or the user input.
    db_start = query_start
    db_end = query_end
            
    # Random Window Logic
    if random_window and db_start and db_end:
        print(f"Randomly selecting {random_window} days within range {db_start} to {db_end}...")
        total_duration = db_end - db_start
        if total_duration.days <= random_window:
             print(f"Warning: Not enough data ({total_duration.days}d) for requested random window ({random_window}d). Using full range.")
             start_date = db_start
             end_date = db_end
        else:
             # Buffer of 1 day to ensure full data availability
             max_start_offset = (db_end - timedelta(days=random_window + 1)).timestamp()
             min_start_offset = db_start.timestamp()
             
             random_start_ts = random.uniform(min_start_offset, max_start_offset)
             start_date = datetime.fromtimestamp(random_start_ts)
             end_date = start_date + timedelta(days=random_window)
    else:
        # Standard Logic
        # Parse Arguments or use Defaults
        end_date = default_end
        if end_str:
            end_date = datetime.strptime(end_str, "%Y-%m-%d")
            
        start_date = default_start
        if start_str:
            start_date = datetime.strptime(start_str, "%Y-%m-%d")
        elif days:
            # If days specified, anchor from the DETERMINED end_date (DB tip or now)
            start_date = end_date - timedelta(days=days)
    
    if symbols:
        print(f"Found {len(symbols)} symbols in DB. Using range from {symbols[0]}...")
    
    print(f"Simulating: {start_date} to {end_date}")

    if not symbols:
        print("No data in DB. Loading from CSVs as fallback...")
        pass

    # Limit symbol count for performance (backtest with 231 symbols at 15min steps is very slow)
    max_symbols = 20
    if len(symbols) > max_symbols:
        print(f"Limiting from {len(symbols)} to top {max_symbols} symbols for backtest performance")
        symbols = symbols[:max_symbols]

    # 3. Configure symbols and strategy overrides BEFORE engine init
    config['trading']['dynamic_pair_selection'] = True
    config['trading']['symbols'] = symbols
    
    # 3.1 Override Strategy Lookbacks for Short Data History
    # Standard 100-period lookbacks (at 1h = 4 days) consume the entire dataset
    # for warmup, leaving no trade window. Reduce them.
    print("Overriding strategy lookbacks for backtest window...")
    config['strategies']['stat_arb']['window_size'] = 24  # 1 day
    config['strategies']['stat_arb']['correlation_lookback'] = 24
    config['strategies']['ou_mean_reversion']['estimation_lookback'] = 24
    config['strategies']['cointegration']['lookback_period'] = 24
    config['strategies']['volatility_breakout']['bb_length'] = 20
    config['strategies']['liquidation_hunter']['window'] = 20
    config['strategies']['cross_sectional_momentum']['lookback_period'] = 6

    # 3.1b Apply any --param overrides from CLI
    if param_overrides:
        print("Applying parameter overrides:")
        for override in param_overrides:
            try:
                key_path, value = override.split('=')
                strategy, param = key_path.split('.', 1)
                # Auto-detect type: try float, then int, then string
                try:
                    typed_value = float(value)
                    if typed_value == int(typed_value) and '.' not in value:
                        typed_value = int(value)
                except ValueError:
                    typed_value = value
                old_value = config['strategies'].get(strategy, {}).get(param, 'N/A')
                config['strategies'][strategy][param] = typed_value
                print(f"  {strategy}.{param}: {old_value} → {typed_value}")
            except ValueError:
                print(f"  ⚠ Invalid format '{override}' — expected strategy.param=value")

    # 3.2 Consolidate Strategies
    # - Disable SentimentML (Underperforming)
    # - Disable Liquidation Hunter 5m/1h (Underperforming)
    if 'instances' in config['strategies']:
        config['strategies']['instances'] = [
            s for s in config['strategies']['instances'] 
            if s['name'] not in ['liquidation_hunter_5m', 'liquidation_hunter_1h']
            and not s['name'].startswith('sentiment_ml')
        ]
        print("Consolidated Strategies: Disabled SentimentML & LH 5m/1h.")

    # 3.3 CLEAN SLATE: Explicitly purge ALL stale backtest data before engine init
    # BacktestEngine only clears trades, but ghost positions leak across runs
    print("Clearing stale backtest data...")
    from src.utils.trade_database import TradeDatabase as BtDb
    bt_db = BtDb(table_prefix="backtest_")
    bt_db.delete_all_trades()
    bt_db.clear_open_positions()
    try:
        bt_db.conn.execute("DELETE FROM backtest_live_position_legs")
        bt_db.conn.execute("DELETE FROM backtest_equity_snapshots")
        bt_db.conn.execute("DELETE FROM backtest_daily_pnl")
        bt_db.conn.commit()
    except Exception as e:
        print(f"  Warning: partial cleanup: {e}")
    print("  Backtest tables cleared.")

    # 3.4 Initialize BacktestEngine AFTER all config overrides
    config['backtesting']['reset_results_db'] = False  # We already cleaned above
    engine = BacktestEngine(config, historical_data=None)
    
    # 3.5 CRITICAL: Pre-populate PairSelector with available symbols
    # The PairSelector's background fetcher (which populates selected_pairs/ready_pairs)
    # does NOT run during backtests. Without this, get_ready_pairs() returns []
    # and run_trading_cycle() skips all analysis with "No ready trading pairs".
    pair_selector = engine.strategy_manager.pair_selector
    # Use only the CAPPED symbols list, not all engine.historical_data keys
    print(f"Injecting {len(symbols)} symbols into PairSelector ready set...")
    with pair_selector._pairs_lock:
        for sym in symbols:
            if sym not in pair_selector.selected_pairs:
                pair_selector.selected_pairs.append(sym)
            pair_selector.ready_pairs.add(sym)
    print(f"  Selected: {len(pair_selector.selected_pairs)}, Ready: {len(pair_selector.ready_pairs)}")
    
    # 4. Run
    # Use 15m interval to match the primary strategy timeframe
    report = engine.run(start_date, end_date, interval_minutes=15)
    
    # 5. Print Results Summary
    print("\n" + "=" * 60)
    print("BACKTEST RESULTS SUMMARY")
    print("=" * 60)
    print(f"Period: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    print(f"Final Equity: ${report['total_equity']:,.2f}")
    
    # Read actual trade stats from the backtest DB
    bt_trades = report.get('backtest_trades', 0)
    bt_pnl = report.get('backtest_total_pnl', 0)
    bt_wr = report.get('backtest_win_rate', 0)
    bt_pf = report.get('backtest_profit_factor', 0)
    bt_mdd = report.get('backtest_max_drawdown_pct', 0)
    
    print(f"Total Trades: {bt_trades}")
    print(f"Total PnL: ${bt_pnl:,.2f}")
    print(f"Win Rate: {bt_wr:.1f}%")
    print(f"Profit Factor: {bt_pf:.2f}")
    print(f"Max Drawdown: {bt_mdd:.2f}%")
    
    # Per-strategy breakdown
    try:
        db = engine.prefixed_tracker.db
        strategies = db.get_strategy_list()
        if strategies:
            print("\nPer-Strategy Breakdown:")
            print("-" * 60)
            for strat in strategies:
                stats = db.get_strategy_stats(strat)
                s_trades = int(stats.get('total_trades', 0) or 0)
                s_pnl = float(stats.get('total_pnl', 0) or 0)
                s_wr = float(stats.get('win_rate', 0) or 0)
                print(f"  {strat:<30} | {s_trades:>3} trades | ${s_pnl:>10,.2f} | WR: {s_wr:.1f}%")
    except Exception as e:
        print(f"  (Could not read strategy breakdown: {e})")
    
    print("=" * 60)


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Run Strategy Backtest',
        epilog='Example: python run_backtest.py --days 14 --param stat_arb.z_score_threshold=2.5 --param ou_mean_reversion.zscore_entry=2.0'
    )
    parser.add_argument('--days', type=int, help='Number of days to run (default: auto-detect)')
    parser.add_argument('--start', type=str, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end', type=str, help='End date (YYYY-MM-DD)')
    parser.add_argument('--random-window', type=int, help='Randomly select N days from available data')
    parser.add_argument('--param', action='append', metavar='strategy.key=value',
                        help='Override a strategy parameter (repeatable). E.g. --param stat_arb.z_score_threshold=2.5')
    
    args = parser.parse_args()
    
    # Ensure DB directory exists
    os.makedirs('data', exist_ok=True)
    
    try:
        run_smoke_test(
            days=args.days, 
            start_str=args.start, 
            end_str=args.end, 
            random_window=args.random_window,
            param_overrides=args.param
        )
    except KeyboardInterrupt:
        print("\nBacktest interrupted by user.")
    except Exception as e:
        # If logging is configured, this will go to file. If not (early crash), it might miss.
        # But we import logging inside run_smoke_test... 
        # We should probably configure logging globally or catch inside run_smoke_test.
        print(f"FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        
        # Try to log if logger exists
        import logging
        logging.getLogger("root").critical("Backtest failed with exception", exc_info=True)
