
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
    # Configure separate backtest DB
    db_path = os.path.join('data', 'backtest_results.db')
    if os.path.exists(db_path):
        os.remove(db_path)  # Clear previous results
        print(f"Cleared previous backtest DB: {db_path}")
        
    config['persistence'] = {'db_path': 'data/backtest_results.db'}
    engine = BacktestEngine(config, historical_data=None)
    
    # Configure symbols
    config['trading']['dynamic_pair_selection'] = True # Allow selector to pick from available
    config['trading']['symbols'] = symbols # Limit to what we have
    
    # Increase log level
    import logging
    logging.basicConfig(level=logging.INFO)
    
    # 4. Run
    report = engine.run(start_date, end_date)
    
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
    
    run_smoke_test(
        days=args.days, 
        start_str=args.start, 
        end_str=args.end, 
        random_window=args.random_window
    )
