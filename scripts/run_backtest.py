
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

def run_smoke_test(days=None, start_str=None, end_str=None, random_window=None):
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
    config['backtesting'] = {'enabled': True}
    
    # 2. Data
    from src.utils.trade_database import TradeDatabase
    db = TradeDatabase()
    
    # Try to find common range from loaded symbols if not manually specified
    symbols = db.get_market_data_symbols('1h')
    
    # Determine default end_date from DB if available, otherwise now()
    default_end = datetime.now()
    default_start = default_end - timedelta(days=90)
    
    db_start = None
    db_end = None

    if symbols:
        # Get range for first symbol (assuming somewhat synchronized)
        db_start, db_end = db.get_available_data_range(symbols[0], '1h')
        if db_end:
            default_end = db_end
        if db_start:
            default_start = db_start
            
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

    # 3. Engine
    # Initialize with None to trigger DB load
    # 3. Engine
    # Initialize with None to trigger DB load
    # Persistence path is handled by BacktestEngine (uses prefix now)
        
    engine = BacktestEngine(config, historical_data=None)
    
    # Configure symbols
    config['trading']['dynamic_pair_selection'] = True # Allow selector to pick from available
    config['trading']['symbols'] = symbols # Limit to what we have
    
    # 3.1 Override Strategy Lookbacks for Short Data History
    # Since we only have ~4-5 days of data in DB, standard 100-period lookbacks 
    # (at 1h = 4 days) consume the entire dataset for warmup, leaving no trade window.
    print("Overriding strategy lookbacks for short backtest window...")
    config['strategies']['stat_arb']['window_size'] = 24  # 1 day
    config['strategies']['stat_arb']['correlation_lookback'] = 24
    config['strategies']['ou_mean_reversion']['estimation_lookback'] = 24
    config['strategies']['cointegration']['lookback_period'] = 24
    config['strategies']['volatility_breakout']['bb_length'] = 20 # Already low
    config['strategies']['volatility_breakout']['bb_length'] = 20 # Already low
    config['strategies']['liquidation_hunter']['window'] = 20 # Already low
    config['strategies']['cross_sectional_momentum']['lookback_period'] = 6 # Shorten for backtest warmup

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
    

    
    # 4. Run
    # Use 15m interval to match the primary strategy timeframe (StatArb 15m)
    # This increases the chance of catching signals compared to the default 60m step
    report = engine.run(start_date, end_date, interval_minutes=15)
    
    print("\nTest Complete!")
    print(f"Total Equity: {report['total_equity']}")
    print(f"Orders: {report['total_orders']}")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='Run Strategy Backtest')
    parser.add_argument('--days', type=int, help='Number of days to run (default: auto-detect)')
    parser.add_argument('--start', type=str, help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end', type=str, help='End date (YYYY-MM-DD)')
    parser.add_argument('--random-window', type=int, help='Randomly select N days from available data')
    
    args = parser.parse_args()
    
    # Ensure DB directory exists
    os.makedirs('data', exist_ok=True)
    
    try:
        run_smoke_test(
            days=args.days, 
            start_str=args.start, 
            end_str=args.end, 
            random_window=args.random_window
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
