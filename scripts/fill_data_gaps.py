"""
Script to identify and fill gaps in the market_data table.

Logic:
1. Iterate through all symbols and timeframes in the database.
2. For each, retrieve all existing timestamps.
3. Identify gaps > 1 interval (plus small tolerance).
4. Also identify gap from last timestamp to NOW.
5. Fetch and insert missing data.
"""

import os
import sys
import time
import logging
import pandas as pd
from datetime import datetime, timedelta
from typing import List, Tuple

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
logger = logging.getLogger("GapFiller")

def get_interval_seconds(timeframe: str) -> int:
    """Get seconds for a timeframe."""
    mapping = {
        '1m': 60,
        '3m': 180,
        '5m': 300,
        '15m': 900,
        '30m': 1800,
        '1h': 3600,
        '2h': 7200,
        '4h': 14400,
        '6h': 21600,
        '8h': 28800,
        '12h': 43200,
        '1d': 86400,
    }
    return mapping.get(timeframe, 3600)

def find_gaps(timestamps: List[datetime], interval_seconds: int, now: datetime) -> List[Tuple[datetime, datetime]]:
    """
    Find time gaps in a sorted list of timestamps.
    Returns list of (start_gap, end_gap).
    """
    if not timestamps:
        return []
    
    gaps = []
    tolerance = interval_seconds * 1.5  # Allow small drift, but >1.5 intervals is a gap
    
    # Check internal gaps
    for i in range(len(timestamps) - 1):
        diff = (timestamps[i+1] - timestamps[i]).total_seconds()
        if diff > tolerance:
            # Gap starts one interval after current
            gap_start = timestamps[i] + timedelta(seconds=interval_seconds)
            # Gap ends just before next
            gap_end = timestamps[i+1] - timedelta(seconds=1)
            gaps.append((gap_start, gap_end))
            
    # Check gap to NOW (exclude current incomplete interval)
    last_ts = timestamps[-1]
    # We want data up to the last *completed* candle.
    # If now is 10:12, and 5m candle. Current candle started 10:10.
    # We only want candles <= 10:05.
    # So max_allowed_ts = now - interval.
    # Any gap ending after max_allowed_ts should be capped.
    
    max_allowed_end = now - timedelta(seconds=interval_seconds)
    
    # If last_ts is already recent enough (e.g. 10:05), diff is small.
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
    
    # Reuse ingestion logic: chunking
    chunk_size_hours = 168 # 1 week
    chunk_ms = chunk_size_hours * 3600 * 1000
    
    current_end_ms = end_ms
    final_start_ms = start_ms
    
    total_filled = 0
    
    while current_end_ms > final_start_ms:
        current_start_ms = max(final_start_ms, current_end_ms - chunk_ms)
        
        # Determine correct API symbol
        api_symbol = symbol
        
        # Check internal SDK map if available
        if hasattr(api.info, 'name_to_coin'):
            if symbol not in api.info.name_to_coin:
                # Try k-prefix
                if f"k{symbol}" in api.info.name_to_coin:
                    api_symbol = f"k{symbol}"
                # Try removing k-prefix if it has it
                elif symbol.startswith('k') and symbol[1:] in api.info.name_to_coin:
                     api_symbol = symbol[1:]

        try:
            # Use candles_snapshot
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
                
                # Double-check: Filter out any incomplete candles (timestamp > end)
                # 'end' here is passed from find_gaps, which is already capped.
                # But to be safe against API returning current candle if end is close.
                # Actually, API usually interprets 'end' as inclusive/exclusive depending.
                # Let's enforce strictly that we don't want anything after 'now - interval' if possible,
                # but 'end' is the gap_end. Strict filter:
                df = df[df.index <= end]
                
                db.insert_market_data(df, symbol, timeframe)
                total_filled += len(df)
            
        except Exception as e:
            logger.error(f"Error fetching {symbol} {timeframe}: {e}")
            time.sleep(1) # Backoff
            
        # Move back
        current_end_ms = current_start_ms - 1
        time.sleep(0.5) # Rate limit protection
        
    if total_filled > 0:
        logger.info(f"[{symbol} {timeframe}] Filled {total_filled} candles")

def run_gap_filler():
    config = load_config()
    api = HyperliquidAPI(config)
    db = TradeDatabase()
    
    # 1. Get all timeframes present in DB
    # We can iterate through standard timeframes or discover them.
    # Discovery is better.
    # SQL: SELECT DISTINCT timeframe FROM market_data
    with db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT timeframe FROM market_data")
        timeframes = [row[0] for row in cursor.fetchall()]
        
    now = datetime.now()
    
    logger.info(f"Found timeframes: {timeframes}")
    
    for timeframe in timeframes:
        interval = get_interval_seconds(timeframe)
        symbols = db.get_market_data_symbols(timeframe)
        logger.info(f"Checking {len(symbols)} symbols for {timeframe}...")
        
        for symbol in symbols:
            # Skip _SPOT for now if complicated, or handle if needed.
            # Assuming standard perp symbols work fine.
            if "_SPOT" in symbol:
                continue
                
            timestamps = db.get_all_timestamps(symbol, timeframe)
            if not timestamps:
                continue
                
            gaps = find_gaps(timestamps, interval, now)
            
            if gaps:
                logger.info(f"[{symbol} {timeframe}] Found {len(gaps)} gaps")
                for gap_start, gap_end in gaps:
                    fetch_and_fill(api, db, symbol, timeframe, gap_start, gap_end)
            else:
                logger.debug(f"[{symbol} {timeframe}] No gaps found")

if __name__ == "__main__":
    run_gap_filler()
