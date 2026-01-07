
import sys
import os
import pandas as pd
import logging

# Add src to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from src.strategies.cross_sectional_momentum_strategy import CrossSectionalMomentumStrategy
from src.utils.trade_database import TradeDatabase
from src.config.settings import load_config

def debug_csm():
    # Setup logging to stdout
    logging.basicConfig(level=logging.DEBUG)
    
    config = load_config()
    # Override lookback for test
    config['strategies']['cross_sectional_momentum']['lookback_period'] = 6
    
    db = TradeDatabase()
    symbols = db.get_market_data_symbols('1h')
    print(f"Loaded {len(symbols)} symbols")
    
    # Load OHLCV for all symbols
    ohlcv_dict = {}
    for sym in symbols:
        try:
            df = db.get_market_data(sym, '1h') # Load all data
            if not df.empty:
                ohlcv_dict[sym] = df
        except Exception as e:
            print(f"Failed to load {sym}: {e}")
            
    print(f"Data loaded for {len(ohlcv_dict)} symbols")
    
    # Test CSM 1h (Long Only)
    print("\n--- Testing CSM 1h (Long Only) ---")
    csm_1h = CrossSectionalMomentumStrategy(config, timeframe='1h')
    
    # Run a few iterations
    # We need to populate universe stats first
    # So we iterate through time for the last 10 hours
    
    timestamps = list(ohlcv_dict[symbols[0]].index)[-20:] 
    
    for ts in timestamps:
        print(f"\nTime: {ts}")
        
        # 1. Update stats for all symbols
        for sym, df in ohlcv_dict.items():
            # Slice data up to ts
            current_data = df.loc[:ts]
            if len(current_data) > 20:
                csm_1h.generate_signal(sym, {'1h': current_data})
                
        # 2. Check signals
        for sym, df in ohlcv_dict.items():
            current_data = df.loc[:ts]
            if len(current_data) > 20: 
                sig = csm_1h.generate_signal(sym, {'1h': current_data})
                if sig:
                    print(f"SIGNAL {sym}: {sig}")
                else:
                    # To debug why None, we rely on the DEBUG logs printed to stdout
                    pass

    # Test CSM 4h (Short Allowed)
    print("\n--- Testing CSM 4h (Shorts Allowed) ---")
    csm_4h = CrossSectionalMomentumStrategy(config, timeframe='4h')
    # Provide 4h data... manually resample or just test logic if we had 4h data
    # For now, just testing 1h is enough to see if ANY logic works.
    
if __name__ == "__main__":
    debug_csm()
