"""
Unified script to manage market data in the Trade database.
Supports: Ingesting, Gap Filling, Repairing, and Verifying data.

Usage:
    python scripts/manage_market_data.py [mode] [flags]

Modes:
    ingest  - Fetch historical data for specific range
    fill    - Find and fill gaps in existing data
    repair  - Verify against API and overwrite mismatches
    verify  - Read-only check of data integrity

Examples:
    python scripts/manage_market_data.py ingest --symbol BTC --timeframe 1h --start 2023-01-01
    python scripts/manage_market_data.py fill --symbol ETH --timeframe 15m
    python scripts/manage_market_data.py verify --assets TOP_50 --timeframe 1h
"""

import os
import sys
import time
import logging
import argparse
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
logger = logging.getLogger("MarketDataManager")

# ==============================================================================
# UTILITIES
# ==============================================================================

def get_interval_seconds(timeframe: str) -> int:
    """Get seconds for a timeframe."""
    mapping = {
        '1m': 60, '3m': 180, '5m': 300, '15m': 900, '30m': 1800,
        '1h': 3600, '2h': 7200, '4h': 14400, '6h': 21600, '8h': 28800,
        '12h': 43200, '1d': 86400,
    }
    return mapping.get(timeframe, 3600)

def resolve_api_symbol(api: HyperliquidAPI, symbol: str) -> str:
    """Handle k-prefix logic (e.g. BONK -> kBONK)."""
    if hasattr(api.info, 'name_to_coin'):
        if symbol not in api.info.name_to_coin:
            if f"k{symbol}" in api.info.name_to_coin:
                return f"k{symbol}"
            elif symbol.startswith('k') and symbol[1:] in api.info.name_to_coin:
                return symbol[1:]
    return symbol

def parse_date(date_str: str) -> datetime:
    """Parse YYYY-MM-DD string to UTC datetime."""
    dt = datetime.strptime(date_str, '%Y-%m-%d')
    return dt.replace(tzinfo=timezone.utc)

def get_target_assets(api: HyperliquidAPI, assets_arg: str) -> list:
    """Resolve target assets list from argument."""
    if not assets_arg or assets_arg == "TOP_50":
        # Logic to get top 50 by volume
        try:
            meta_and_ctxs = api.info.meta_and_asset_ctxs()
            universe = meta_and_ctxs[0]['universe']
            asset_ctxs = meta_and_ctxs[1]
            assets_with_vol = []
            for i, asset in enumerate(universe):
                ctx = asset_ctxs[i]
                vol = float(ctx.get('dayNtlVlm', 0))
                assets_with_vol.append((asset['name'], vol))
            assets_with_vol.sort(key=lambda x: x[1], reverse=True)
            return [x[0] for x in assets_with_vol[:50]]
        except Exception as e:
            logger.error(f"Error fetching top assets: {e}")
            return ['BTC', 'ETH', 'SOL', 'AVAX']
    else:
        return [s.strip() for s in assets_arg.split(',')]

# ==============================================================================
# CORE DATA LOGIC
# ==============================================================================

def fetch_and_upsert(api: HyperliquidAPI, db: TradeDatabase, symbol: str, timeframe: str, start_ts_ms: int, end_ts_ms: int):
    """
    Fetch a range of candles and insert into DB.
    Handles 'insert_market_data' which is an Upsert (REPLACE).
    """
    target_symbol = resolve_api_symbol(api, symbol)
    
    # Check rate limits automatically (retry logic)
    max_retries = 5
    for attempt in range(max_retries):
        try:
            candles = api.info.candles_snapshot(target_symbol, timeframe, start_ts_ms, end_ts_ms)
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
                
                # Insert
                db.insert_market_data(df, symbol, timeframe)
                return len(df)
            else:
                return 0 # No data returned
        except Exception as e:
            if "429" in str(e):
                wait = 2 ** (attempt + 2)
                logger.warning(f"Rate limit. Waiting {wait}s...")
                time.sleep(wait)
            else:
                logger.error(f"Error fetching {symbol}: {e}")
                time.sleep(1)
    return 0

def run_ingest(api, db, symbol, timeframe, start_dt, end_dt):
    """
    Ingest data for a specific range.
    Splits requests into safe chunks (e.g. 1 week) to avoid massive transactions/locks.
    """
    # 1 week chunks
    chunk_size = timedelta(days=7)
    current_end = end_dt
    
    total_rows = 0
    logger.info(f"[{symbol} {timeframe}] Starting ingestion: {start_dt.date()} -> {end_dt.date()}")
    
    while current_end > start_dt:
        chunk_start = max(start_dt, current_end - chunk_size)
        
        # Convert to ms
        start_ms = int(chunk_start.timestamp() * 1000)
        end_ms = int(current_end.timestamp() * 1000)
        
        rows = fetch_and_upsert(api, db, symbol, timeframe, start_ms, end_ms)
        total_rows += rows
        
        # Throttle to be nice to API and DB
        time.sleep(0.5)
        
        current_end = chunk_start
        
    logger.info(f"[{symbol} {timeframe}] Ingestion complete. Total rows: {total_rows}")

def run_fill_gaps(api, db, symbol, timeframe):
    """Find and fill gaps for a symbol."""
    logger.info(f"[{symbol} {timeframe}] Scanning for gaps...")
    
    timestamps = db.get_all_timestamps(symbol, timeframe)
    if not timestamps:
        logger.info(f"[{symbol} {timeframe}] No existing data. Skipping fill.")
        return

    interval = get_interval_seconds(timeframe)
    tolerance = interval * 1.5
    now = datetime.now()
    
    gaps = []
    
    # 1. Internal gaps
    for i in range(len(timestamps) - 1):
        diff = (timestamps[i+1] - timestamps[i]).total_seconds()
        if diff > tolerance:
            gap_start = timestamps[i] + timedelta(seconds=interval)
            gap_end = timestamps[i+1] - timedelta(seconds=1)
            gaps.append((gap_start, gap_end))
            
    # 2. Gap to NOW
    last_ts = timestamps[-1]
    max_allowed = now - timedelta(seconds=interval)
    if (max_allowed - last_ts).total_seconds() > tolerance:
        gaps.append((last_ts + timedelta(seconds=interval), max_allowed))
        
    logger.info(f"[{symbol} {timeframe}] Found {len(gaps)} gaps.")
    
    for start_g, end_g in gaps:
        logger.info(f"  Filling gap: {start_g} -> {end_g}")
        run_ingest(api, db, symbol, timeframe, start_g, end_g)

def run_verify(api, db, symbol, timeframe, mode='verify'):
    """
    Verify or Repair last N candles.
    mode='verify': log mismatches.
    mode='repair': overwrite mismatches.
    """
    limit = 200
    interval = get_interval_seconds(timeframe)
    
    end_dt = datetime.now()
    start_dt = end_dt - timedelta(seconds=limit * interval * 1.5)
    
    # 1. Fetch Local
    df_db = db.get_market_data(symbol, timeframe, start_date=start_dt)
    if df_db.empty:
        logger.warning(f"[{symbol} {timeframe}] No DB data to verify.")
        return

    # 2. Fetch Remote
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    target_symbol = resolve_api_symbol(api, symbol)
    
    candles = api.info.candles_snapshot(target_symbol, timeframe, start_ms, end_ms)
    if not candles:
        return
        
    # Convert API candles to dict for comparison
    api_map = {}
    for c in candles:
        ts = pd.to_datetime(c['t'], unit='ms')
        api_map[ts] = {
            'open': float(c['o']),
            'high': float(c['h']),
            'low': float(c['l']),
            'close': float(c['c']),
            'volume': float(c['v'])
        }
    
    # 3. Compare
    mismatches = []
    for ts, row in df_db.iterrows():
        # Timestamp match?
        if ts not in api_map:
            continue # Might be different ranges
            
        api_row = api_map[ts]
        
        # Check values
        is_bad = False
        if abs(row['close'] - api_row['close']) > 1e-8: is_bad = True
        elif abs(row['volume'] - api_row['volume']) > 1e-4: is_bad = True
        
        if is_bad:
            mismatches.append(ts)
            if mode == 'verify':
                logger.warning(f"MISMATCH {symbol} {timeframe} @ {ts}")
    
    if mode == 'verify':
        if not mismatches:
            logger.info(f"[{symbol} {timeframe}] Verified {len(df_db)} rows: OK")
        else:
            logger.error(f"[{symbol} {timeframe}] Found {len(mismatches)} mismatches!")
            
    elif mode == 'repair' and mismatches:
        logger.info(f"[{symbol} {timeframe}] Repairing {len(mismatches)} rows...")
        # Simplest repair: Just ingest the whole range of the check again
        # This overwrites everything including the bad rows
        run_ingest(api, db, symbol, timeframe, start_dt, end_dt)


# ==============================================================================
# MAIN
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(description="TradeBot Market Data Manager")
    
    # Mode
    parser.add_argument('mode', choices=['ingest', 'fill', 'repair', 'verify'], help="Operation mode")
    
    # Scope
    parser.add_argument('--symbol', type=str, help="Target symbol (e.g. BTC)")
    parser.add_argument('--assets', type=str, default="TOP_50", help="Comma-separated list or 'TOP_50' (default)")
    parser.add_argument('--timeframe', type=str, help="Timeframe (5m, 15m, 1h, 4h, 1d)")
    
    # Range
    parser.add_argument('--start', type=str, help="Start date YYYY-MM-DD (Default: 90 days ago)")
    parser.add_argument('--end', type=str, help="End date YYYY-MM-DD (Default: Now)")
    
    # DB
    parser.add_argument('--db-path', type=str, default="data/trades.db", help="Path to SQLite DB")
    
    args = parser.parse_args()
    
    # 1. Config & Init
    config = load_config()
    api = HyperliquidAPI(config)
    db = TradeDatabase(db_path=args.db_path)
    
    # 2. Resolve Timeframe
    if not args.timeframe and args.mode in ['ingest', 'fill']:
        logger.error("--timeframe is required for ingest/fill")
        return
        
    timeframes = [args.timeframe] if args.timeframe else ['5m', '15m', '1h', '4h', '1d']
    
    # 3. Resolve Assets
    if args.symbol:
        assets = [args.symbol]
    else:
        # If no single symbol, use the list
        assets = get_target_assets(api, args.assets)
        
    # 4. Resolve Dates
    now_utc = datetime.now(timezone.utc)
    
    end_dt = now_utc
    if args.end:
        end_dt = parse_date(args.end) + timedelta(hours=23, minutes=59) # End of day
        
    start_dt = end_dt - timedelta(days=90)
    if args.start:
        start_dt = parse_date(args.start)

    logger.info(f"Starting {args.mode.upper()} for {len(assets)} assets. DB: {args.db_path}")

    # 5. Execute
    for symbol in assets:
        for tf in timeframes:
            if args.mode == 'ingest':
                run_ingest(api, db, symbol, tf, start_dt, end_dt)
            elif args.mode == 'fill':
                run_fill_gaps(api, db, symbol, tf)
            elif args.mode == 'verify':
                run_verify(api, db, symbol, tf, mode='verify')
            elif args.mode == 'repair':
                run_verify(api, db, symbol, tf, mode='repair')
                
    logger.info("Operation Complete.")

if __name__ == "__main__":
    main()
