
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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("DataRepair")

def get_interval_seconds(timeframe: str) -> int:
    mapping = {
        '1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800,
        '1h': 3600, '2h': 7200, '4h': 14400, '6h': 21600, '8h': 28800,
        '12h': 43200, '1d': 86400,
    }
    return mapping.get(timeframe, 3600)

def repair_symbol(api, db, symbol, timeframe, limit=200):
    # logger.info(f"Checking {symbol} {timeframe}...")
    
    # 1. Fetch from DB
    current_time = datetime.utcnow()
    # Look back enough to cover 'limit' candles
    tf_seconds = get_interval_seconds(timeframe)
    lookback_seconds = limit * tf_seconds * 1.5 
    start_date = current_time - timedelta(seconds=lookback_seconds)
    
    df_db = db.get_market_data(symbol, timeframe, start_date=start_date)
    if df_db.empty:
        return
        
    df_db = df_db.tail(limit)
    
    # 2. Fetch from API
    end_ms = int(current_time.timestamp() * 1000)
    start_ms = int(start_date.timestamp() * 1000)
    
    try:
        candles = api.info.candles_snapshot(symbol, timeframe, start_ms, end_ms)
    except Exception as e:
        logger.error(f"API Error {symbol}: {e}")
        return

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
        return
        
    df_api.set_index('timestamp', inplace=True)
    
    # 3. Compare & Collect Fixes
    common_indices = df_db.index.intersection(df_api.index)
    
    rows_to_fix = []
    
    for ts in common_indices:
        # Skip current forming candle if it accidentally got in (should be handled elsewhere but safe to skip here)
        # Actually, we want to fix historic ones.
        # If the API candle is "finalized", we should match it.
        # But API snapshot usually includes the *current* open candle at the end.
        # We should NOT overwrite a stored completed candle with a current open one if timestamps match?
        # Usually DB stores 'start time'.
        # If TS + interval > now, it's open.
        
        candle_end = ts + timedelta(seconds=tf_seconds)
        if candle_end > current_time:
            continue # specific safety: never touch "now" candle in repair, let live bot handle it
            
        row_db = df_db.loc[ts]
        row_api = df_api.loc[ts]
        
        is_bad = False
        # Mismatch logic
        if abs(row_db['volume'] - row_api['volume']) > 1e-4: is_bad = True
        elif abs(row_db['close'] - row_api['close']) > 1e-8: is_bad = True
        elif abs(row_db['high'] - row_api['high']) > 1e-8: is_bad = True
        elif abs(row_db['low'] - row_api['low']) > 1e-8: is_bad = True
        
        if is_bad:
            logger.warning(f"MISMATCH {symbol} {timeframe} @ {ts}. DB Vol:{row_db['volume']} vs API:{row_api['volume']}")
            rows_to_fix.append(row_api)
            
    # 4. Apply Fixes
    if rows_to_fix:
        logger.info(f"Repairing {len(rows_to_fix)} candles for {symbol} {timeframe}...")
        df_fix = pd.DataFrame(rows_to_fix)
        # df_fix index is already set? No, row_api is Series, extracting it keeps index? 
        # No, appending Series to list loses index usually unless handled.
        # Let's reconstruct properly.
        # row_api is a Series with name=timestamp
        df_fix = pd.DataFrame(rows_to_fix) 
        # When creating DF from list of series, index is preserved if aligned...
        # Safer:
        df_fix = pd.concat(rows_to_fix, axis=1).T
        df_fix.index.name = 'timestamp'
        
        db.insert_market_data(df_fix, symbol, timeframe)
        logger.info(f"Fixed {symbol} {timeframe}.")

def run():
    config = load_config()
    api = HyperliquidAPI(config)
    db = TradeDatabase()
    
    # Discovery
    with db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT timeframe FROM market_data")
        timeframes = [row[0] for row in cursor.fetchall()]

    for tf in timeframes:
        symbols = db.get_market_data_symbols(tf)
        logger.info(f"Scanning {len(symbols)} symbols for {tf}...")
        for sym in symbols:
            # Handle mapping for API
            api_sym = sym
            if "_SPOT" in sym: continue
            
            # Simple check for k-mapping logic (copy paste simplified)
            if hasattr(api.info, 'name_to_coin'):
                if sym not in api.info.name_to_coin and f"k{sym}" in api.info.name_to_coin:
                    api_sym = f"k{sym}"
            
            try:
                # We reuse repair_symbol but need to pass correct api_symbol to API, 
                # but DB uses 'sym'.
                # The repair_symbol function needs to be smart about this.
                # Let's adjust repair_symbol to take api_symbol separate
                pass 
            except:
                pass
                
            # Calling modified version below inline to avoid scope mess
            repair_symbol_custom(api, db, sym, api_sym, tf)

def repair_symbol_custom(api, db, db_symbol, api_symbol, timeframe, limit=200):
    # Copy of logic above with split symbols
    current_time = datetime.utcnow()
    tf_seconds = get_interval_seconds(timeframe)
    lookback_seconds = limit * tf_seconds * 1.5 
    start_date = current_time - timedelta(seconds=lookback_seconds)
    
    df_db = db.get_market_data(db_symbol, timeframe, start_date=start_date)
    if df_db.empty: return
    df_db = df_db.tail(limit)
    
    end_ms = int(current_time.timestamp() * 1000)
    start_ms = int(start_date.timestamp() * 1000)
    
    try:
        candles = api.info.candles_snapshot(api_symbol, timeframe, start_ms, end_ms)
    except Exception as e:
        logger.error(f"API Error {api_symbol}: {e}")
        return

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
    if df_api.empty: return
    df_api.set_index('timestamp', inplace=True)
    
    common_indices = df_db.index.intersection(df_api.index)
    rows_to_fix = []
    
    for ts in common_indices:
        candle_end = ts + timedelta(seconds=tf_seconds)
        if candle_end > current_time: continue 
            
        row_db = df_db.loc[ts]
        row_api = df_api.loc[ts]
        
        is_bad = False
        if abs(row_db['volume'] - row_api['volume']) > 1e-4: is_bad = True
        elif abs(row_db['close'] - row_api['close']) > 1e-8: is_bad = True
        
        if is_bad:
            # logger.warning(f"MISMATCH {db_symbol} {timeframe} @ {ts}")
            rows_to_fix.append(row_api)
            
    if rows_to_fix:
        logger.info(f"Repairing {len(rows_to_fix)} candles for {db_symbol} {timeframe}...")
        df_fix = pd.concat(rows_to_fix, axis=1).T
        df_fix.index.name = 'timestamp'
        # Important: ensure columns are correct types
        cols = ['open', 'high', 'low', 'close', 'volume']
        for c in cols: df_fix[c] = df_fix[c].astype(float)
        
        db.insert_market_data(df_fix, db_symbol, timeframe)

if __name__ == "__main__":
    run()
