
import sys
import os
import time
import logging
from collections import deque
from unittest.mock import MagicMock

# Add project root
sys.path.insert(0, os.getcwd())

from src.api.hyperliquid_api import HyperliquidAPI

def test_persistence_trigger():
    # Setup
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("DebugPersistence")
    
    mock_config = {
        'api': {
            'base_url': 'https://api.hyperliquid.xyz',
            'private_key': '00'*32,
            'wallet_address': '0x'+'00'*20,
            'rate_limit': {'calls_per_second': 50, 'burst_size': 100}
        }
    }
    
    api = HyperliquidAPI(mock_config)
    
    # Mock executor to verify submission without threading
    api._persistence_executor = MagicMock()
    
    # Mock DB to ensure _on_bar_complete doesn't early return
    api.market_db = MagicMock()
    
    symbol = "BTC"
    tf = "5m"
    
    # 1. Initialize Cache
    logger.info("Initializing Cache...")
    api.ohlcv_cache.ensure_timeframe(symbol, tf, 300)
    
    # 2. Seed with a bar at T=0
    # 5m bucket starts at 0. Ends at 300.
    start_time = 0 
    bar = {'time': start_time, 'open': 100, 'high': 100, 'low': 100, 'close': 100, 'volume': 10}
    api.ohlcv_cache.seed(symbol, tf, [bar])
    
    # Verify seed
    dq = api.ohlcv_cache.cache[symbol][tf]
    logger.info(f"Seeded: len={len(dq)}, last_time={dq[-1]['time']}")
    
    # 3. Update within same bucket (T=299)
    # Key should be 0.
    logger.info("Updating at T=299 (Same bucket)...")
    api.update_ohlcv_from_tick(symbol, 101, 1, 299)
    
    # Assertions
    if api._persistence_executor.submit.called:
        logger.error("❌ FAILURE: Persistence triggered prematurely!")
    else:
        logger.info("✅ SUCCESS: No persistence for T=299")
        
    # 4. Update cross boundary (T=300)
    # Key should be 300.
    logger.info("Updating at T=300 (New bucket)...")
    api.update_ohlcv_from_tick(symbol, 102, 1, 300)
    
    # Assertions
    if api._persistence_executor.submit.called:
        logger.info("✅ SUCCESS: Persistence triggered for T=300")
        args, _ = api._persistence_executor.submit.call_args
        # Args: method, symbol, timeframe, timestamp
        # Method is bound, so checking args[1:]
        logger.info(f"Call args: {args[1:]}")
        if args[1] == symbol and args[2] == tf and args[3] == 0:
             logger.info("✅ SUCCESS: Correct arguments (persisting T=0 bar)")
        else:
             logger.error(f"❌ FAILURE: Incorrect arguments. Expected T=0, got {args[3]}")
    else:
        logger.error("❌ FAILURE: Persistence NOT triggered for T=300")
        
    # Check logs for "CACHE BOUNDARY"
    
if __name__ == "__main__":
    test_persistence_trigger()
