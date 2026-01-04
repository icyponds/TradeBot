
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

def run_smoke_test():
    print("Running Backtest Smoke Test...")
    
    # 1. Config
    config = load_config()
    # Use default strategy instances from settings.py (StatArb, FundingArb, etc.)
    # config['strategies']['enabled'] = ['moving_average']
    
    # 5. Enable Backtest Mode for PairSelector (load all assets instantly)
    config['mode'] = 'backtest'
    config['backtesting'] = {'enabled': True}
    
    # 2. Data
    # 2. Data
    # If using DB, we pass None to engine and it loads it
    # But we need to know start/end date.
    
    # Let's peek at DB range if available
    from src.utils.trade_database import TradeDatabase
    db = TradeDatabase()
    
    # Default to last 30 days if no data found
    end_date = datetime.now()
    start_date = end_date - timedelta(days=30)
    
    # Try to find common range from loaded symbols
    symbols = db.get_market_data_symbols('1h')
    if symbols:
        print(f"Found {len(symbols)} symbols in DB: {symbols[:5]}...")
        # Get range for first symbol
        s_start, s_end = db.get_available_data_range(symbols[0], '1h')
        if s_start and s_end:
            start_date = s_start
            end_date = s_end
            print(f"Simulating DB range: {start_date} to {end_date}")
    else:
        print("No data in DB. Loading from CSVs as fallback...")
        # ... logic to load CSVs if needed ...
        # (Original CSV loading logic removed for brevity as we move to DB)
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
    # Ensure DB directory exists
    os.makedirs('data', exist_ok=True)
    run_smoke_test()
