"""
Script to identify/fill gaps, verify data integrity, or repair mismatches in the market_data table.

Usage:
    python scripts/fill_data_gaps.py [mode]

Modes:
    fill    (default) Find and fill missing candle ranges.
    repair  Verify last 200 candles against API and overwrite mismatches.
    verify  Check integrity of last 100 candles against API (read-only).
"""

import os
import sys
import time
import argparse
import logging
import pandas as pd
from datetime import datetime, timedelta, timezone

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config.settings import load_config
from src.api.hyperliquid_api import HyperliquidAPI
from src.utils.trade_database import TradeDatabase

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("DataManager")

def get_interval_seconds(timeframe: str) -> int:
    """Get seconds for a timeframe."""
    mapping = {
        '1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800,
        '1h': 3600, '2h': 7200, '4h': 14400, '6h': 21600, '8h': 28800,
        '12h': 43200, '1d': 86400,
    }
    return mapping.get(timeframe, 3600)

# -------------------------------------------------------------------------
# GAP FILLING LOGIC
# -------------------------------------------------------------------------

def find_gaps(timestamps: List[datetime], interval_seconds: int, now: datetime) -> List[Tuple[datetime, datetime]]:
    """
    Find time gaps in a sorted list of timestamps.
    Returns list of (start_gap, end_gap).
    """
    if not timestamps:
        return []
    
    gaps = []
    tolerance = interval_seconds * 1.5
    
    # Check internal gaps
    for i in range(len(timestamps) - 1):
        diff = (timestamps[i+1] - timestamps[i]).total_seconds()
        if diff > tolerance:
            gap_start = timestamps[i] + timedelta(seconds=interval_seconds)
            gap_end = timestamps[i+1] - timedelta(seconds=1)
            gaps.append((gap_start, gap_end))
            
    # Check gap to NOW (exclude current incomplete interval)
    last_ts = timestamps[-1]
    
    # We want data up to the last *completed* candle.
    # If now is 10:12, and 5m candle. Current candle started 10:10.
    # We only want candles <= 10:05.
    max_allowed_end = now - timedelta(seconds=interval_seconds)
    
    if (max_allowed_end - last_ts).total_seconds() > tolerance:
        gap_start = last_ts + timedelta(seconds=interval_seconds)
        gap_end = max_allowed_end
        gaps.append((gap_start, gap_end))
        
    return gaps

def fetch_and_fill(api: HyperliquidAPI, db: TradeDatabase, symbol: str, timeframe: str, start: datetime, end: datetime):
    """Fetch missing data ranges and fill DB."""
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    
    logger.info(f"[{symbol} {timeframe}] Filling gap: {start} -> {end}")
    
    chunk_size_hours = 168 # 1 week
    chunk_ms = chunk_size_hours * 3600 * 1000
    
    current_end_ms = end_ms
    final_start_ms = start_ms
    
    total_filled = 0
    
    while current_end_ms > final_start_ms:
        current_start_ms = max(final_start_ms, current_end_ms - chunk_ms)
        
        api_symbol = resolve_api_symbol(api, symbol)
        
        try:
            candles = api.info.candles_snapshot(api_symbol, timeframe, current_start_ms, current_end_ms)
            
            if candles:
                data = []
                for c in candles:
                    data.append({
                        'timestamp': pd.to_datetime(c['t'], unit='ms'),
                        'open': float(c['o']),
                        'high': float(c['h']),
                        'low': float(c['l']),
                        'close': float(c['c']),
                        'volume': float(c['v']),
                    })
                
                df = pd.DataFrame(data)
                df.set_index('timestamp', inplace=True)
                
                # STRICT FILTER: Exclude incomplete data
                df = df[df.index <= end]
                
                db.insert_market_data(df, symbol, timeframe)
                total_filled += len(df)
            
        except Exception as e:
            logger.error(f"Error fetching {symbol} {timeframe}: {e}")
            time.sleep(1)
            
        current_end_ms = current_start_ms - 1
        time.sleep(0.5)
        
    if total_filled > 0:
        logger.info(f"[{symbol} {timeframe}] Filled {total_filled} candles")

# -------------------------------------------------------------------------
# REPAIR / VERIFY LOGIC
# -------------------------------------------------------------------------

def resolve_api_symbol(api, symbol):
    """Handle k-prefix logic."""
    if hasattr(api.info, 'name_to_coin'):
        if symbol not in api.info.name_to_coin:
            if f"k{symbol}" in api.info.name_to_coin:
                return f"k{symbol}"
            elif symbol.startswith('k') and symbol[1:] in api.info.name_to_coin:
                return symbol[1:]
    return symbol

def repair_or_verify_symbol(api, db, symbol: str, timeframe: str, limit=200, mode='repair'):
    """
    mode='repair': overwrites mismatches.
    mode='verify': logs mismatches only.
    """
    current_time = datetime.utcnow()
    tf_seconds = get_interval_seconds(timeframe)
    lookback_seconds = limit * tf_seconds * 1.5 
    start_date = current_time - timedelta(seconds=lookback_seconds)
    
    # 1. DB Data
    df_db = db.get_market_data(symbol, timeframe, start_date=start_date)
    if df_db.empty: return
    df_db = df_db.tail(limit)
    
    # 2. API Data
    end_ms = int(current_time.timestamp() * 1000)
    start_ms = int(start_date.timestamp() * 1000)
    api_symbol = resolve_api_symbol(api, symbol)
    
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
    
    # 3. Compare
    common_indices = df_db.index.intersection(df_api.index)
    rows_to_fix = []
    mismatch_count = 0
    
    for ts in common_indices:
        candle_end = ts + timedelta(seconds=tf_seconds)
        # Never verify/repair the currently forming candle (drift issues)
        if candle_end > current_time: continue 
            
        row_db = df_db.loc[ts]
        row_api = df_api.loc[ts]
        
        is_bad = False
        if abs(row_db['volume'] - row_api['volume']) > 1e-4: is_bad = True
        elif abs(row_db['close'] - row_api['close']) > 1e-8: is_bad = True
        elif abs(row_db['high'] - row_api['high']) > 1e-8: is_bad = True
        elif abs(row_db['low'] - row_api['low']) > 1e-8: is_bad = True
        elif abs(row_db['open'] - row_api['open']) > 1e-8: is_bad = True
        
        if is_bad:
            mismatch_count += 1
            if mode == 'verify':
                logger.warning(f"MISMATCH {symbol} {timeframe} @ {ts} | DB Vol:{row_db['volume']} vs API:{row_api['volume']}")
            elif mode == 'repair':
                rows_to_fix.append(row_api)
                
    if mode == 'repair' and rows_to_fix:
        logger.info(f"Repairing {len(rows_to_fix)} candles for {symbol} {timeframe}...")
        df_fix = pd.concat(rows_to_fix, axis=1).T
        df_fix.index.name = 'timestamp'
        cols = ['open', 'high', 'low', 'close', 'volume']
        for c in cols: df_fix[c] = df_fix[c].astype(float)
        db.insert_market_data(df_fix, symbol, timeframe)
    
    elif mode == 'verify':
        if mismatch_count == 0:
            logger.info(f"VERIFIED {symbol} {timeframe} (last {limit}): OK")
        else:
            logger.error(f"VERIFIED {symbol} {timeframe}: {mismatch_count} mismatches found!")

# -------------------------------------------------------------------------
# MAIN RUNNER
# -------------------------------------------------------------------------

def run():
    parser = argparse.ArgumentParser(description="Market Data Maintenance Tool")
    parser.add_argument('mode', default='fill', const='fill', nargs='?', 
                       choices=['fill', 'repair', 'verify'], 
                       help='Operation mode: fill (gaps), repair (mismatches), or verify (integrity)')
    args = parser.parse_args()
    
    config = load_config()
    api = HyperliquidAPI(config)
    db = TradeDatabase()
    
    # Discovery
    with db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT timeframe FROM market_data")
        timeframes = [row[0] for row in cursor.fetchall()]

    logger.info(f"Starting Data Manager in '{args.mode}' mode...")
    
    # For gap filling, we use 'now'
    # For repair, we use 'utcnow' inside the function
    now = datetime.now() # naive local? No, use utcnow for consistency if timestamps are UTC-naive
    if args.mode == 'fill':
        now = datetime.utcnow()

    for timeframe in timeframes:
        interval = get_interval_seconds(timeframe)
        symbols = db.get_market_data_symbols(timeframe)
        logger.info(f"Processing {len(symbols)} symbols for {timeframe}...")
        
        for symbol in symbols:
            if "_SPOT" in symbol: continue
            
            if args.mode == 'fill':
                timestamps = db.get_all_timestamps(symbol, timeframe)
                if not timestamps: continue
                gaps = find_gaps(timestamps, interval, now)
                if gaps:
                    logger.info(f"[{symbol} {timeframe}] Found {len(gaps)} gaps")
                    for gap_start, gap_end in gaps:
                        fetch_and_fill(api, db, symbol, timeframe, gap_start, gap_end)
            
            elif args.mode in ['repair', 'verify']:
                repair_or_verify_symbol(api, db, symbol, timeframe, limit=200, mode=args.mode)
                
    logger.info("Task Complete.")

if __name__ == "__main__":
    from typing import List, Tuple # re-import for type hints availability if needed
    run()
