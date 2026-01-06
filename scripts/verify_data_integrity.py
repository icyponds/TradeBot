
import sys
import os
import pandas as pd
import logging
from datetime import datetime, timedelta

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config.settings import load_config
from src.api.hyperliquid_api import HyperliquidAPI
from src.utils.trade_database import TradeDatabase

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("DataVerifier")

def verify_symbol(api, db, symbol, timeframe, limit=100):
    logger.info(f"--- Verifying {symbol} {timeframe} (Last {limit} candles) ---")
    
    # 1. Fetch from DB
    current_time = datetime.now()
    # Giving a buffer for 'limit'
    # We'll just fetch all recently and tail(limit)
    start_date = current_time - timedelta(days=7) # Plenty of buffer
    
    df_db = db.get_market_data(symbol, timeframe, start_date=start_date)
    if df_db.empty:
        logger.warning("No data in DB")
        return
        
    df_db = df_db.tail(limit)
    
    # 2. Fetch from API (Snapshots are usually recent)
    end_ms = int(current_time.timestamp() * 1000)
    # Calculate approx start ms needed
    # timeframe to ms
    tf_seconds = 0
    if timeframe == '5m': tf_seconds = 300
    elif timeframe == '1h': tf_seconds = 3600
    elif timeframe == '4h': tf_seconds = 14400
    
    start_ms = end_ms - (limit * tf_seconds * 1000) - (tf_seconds * 1000 * 2) # Extra buffer
    
    candles = api.info.candles_snapshot(symbol, timeframe, start_ms, end_ms)
    
    api_data = []
    for c in candles:
        api_data.append({
            'timestamp': pd.to_datetime(c['t'], unit='ms'),
            'open': float(c['o']),
            'high': float(c['h']),
            'low': float(c['l']),
            'close': float(c['c']),
            'volume': float(c['v']),
        })
    df_api = pd.DataFrame(api_data)
    if df_api.empty:
        logger.warning("No data from API")
        return
        
    df_api.set_index('timestamp', inplace=True)
    
    # 3. Compare
    # We only verify timestamps present in BOTH
    common_indices = df_db.index.intersection(df_api.index)
    
    if len(common_indices) == 0:
        logger.warning("No overlapping timestamps found")
        return
        
    logger.info(f"Comparing {len(common_indices)} overlapping candles...")
    
    mismatches = 0
    for ts in common_indices:
        row_db = df_db.loc[ts]
        row_api = df_api.loc[ts]
        
        # Check values with small tolerance for floats
        is_ok = True
        try:
            if abs(row_db['open'] - row_api['open']) > 1e-8: is_ok = False
            if abs(row_db['high'] - row_api['high']) > 1e-8: is_ok = False
            if abs(row_db['low'] - row_api['low']) > 1e-8: is_ok = False
            if abs(row_db['close'] - row_api['close']) > 1e-8: is_ok = False
            # Volume might be tricky if it updates frequently, but historical finalized candles should match
            if abs(row_db['volume'] - row_api['volume']) > 1e-4: is_ok = False
        except Exception as e:
            logger.error(f"Error comparing {ts}: {e}")
            is_ok = False
            
        if not is_ok:
            mismatches += 1
            logger.error(f"MISMATCH at {ts}")
            logger.error(f"  DB : O={row_db['open']} H={row_db['high']} L={row_db['low']} C={row_db['close']} V={row_db['volume']}")
            logger.error(f"  API: O={row_api['open']} H={row_api['high']} L={row_api['low']} C={row_api['close']} V={row_api['volume']}")
    
    if mismatches == 0:
        logger.info(f"SUCCESS: All {len(common_indices)} candles match perfectly.")
    else:
        logger.error(f"FAILURE: Found {mismatches} mismatches.")

def run():
    config = load_config()
    api = HyperliquidAPI(config)
    db = TradeDatabase()
    
    verify_symbol(api, db, "BTC", "5m")
    verify_symbol(api, db, "BTC", "1h")
    verify_symbol(api, db, "BTC", "4h")
    verify_symbol(api, db, "XRP", "5m")

if __name__ == "__main__":
    run()
