
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
    # Force enable one legacy strategy for testing
    config['strategies']['enabled'] = ['moving_average']
    
    # 2. Data
    start_date = datetime(2024, 1, 1)
    end_date = datetime(2024, 1, 7)
    
    symbol = "ETH"  # Use simple symbol to match universe name
    
    data = {}
    data[symbol] = generate_synthetic_data(symbol, start_date, end_date)
    
    # 3. Engine
    engine = BacktestEngine(config, data)
    
    # Patch PairSelector to return our symbol associated with engine's strategy manager
    # Or rely on config['trading']['symbols'] if dynamic is false.
    config['trading']['dynamic_pair_selection'] = False
    config['trading']['symbols'] = [symbol]
    
    # Increase log level
    import logging
    logging.basicConfig(level=logging.INFO)
    
    # 4. Run
    report = engine.run(start_date, end_date)
    
    print("\nTest Complete!")
    print(f"Total Equity: {report['total_equity']}")
    print(f"Orders: {report['total_orders']}")

if __name__ == "__main__":
    run_smoke_test()
