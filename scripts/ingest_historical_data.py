"""
Script to ingest historical market data into SQLite for backtesting.

fetches:
- Top 50 assets by volume
- 3 months of 1h candles
- Stores in data/trades.db (market_data table)
"""

import os
import sys
import time
import logging
import pandas as pd
from datetime import datetime, timedelta
import concurrent.futures

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
logger = logging.getLogger("DataIngestion")

def get_top_assets(api: HyperliquidAPI, limit: int = 50):
    """Get top N liquid assets (Perps)."""
    try:
        # Get metadata and contexts (volume, etc.)
        # Accessing underlying SDK info object directly for raw meta
        meta_and_ctxs = api.info.meta_and_asset_ctxs()
        
        universe = meta_and_ctxs[0]['universe']
        asset_ctxs = meta_and_ctxs[1]
        
        assets_with_vol = []
        
        for i, asset in enumerate(universe):
            symbol = asset['name']
            ctx = asset_ctxs[i]
            
            # Day Volume (dayNtlVlm)
            volume = float(ctx.get('dayNtlVlm', 0))
            assets_with_vol.append((symbol, volume))
            
        # Sort by volume desc
        assets_with_vol.sort(key=lambda x: x[1], reverse=True)
        
        return [x[0] for x in assets_with_vol[:limit]]
        
    except Exception as e:
        logger.error(f"Error getting top assets: {e}")
        # Fallback list
        return ['BTC', 'ETH', 'SOL', 'AVAX', 'SUI', 'APT', 'LTC', 'XRP', 'DOGE']

def fetch_history_for_symbol(api: HyperliquidAPI, db: TradeDatabase, symbol: str, days: int = 90, api_symbol: str = None, db_symbol: str = None):
    """
    Fetch history for a single symbol and insert into DB.
    
    Args:
        symbol: Human readable symbol (used for logging)
        api_symbol: Actual symbol to send to API (defaults to symbol)
        db_symbol: Symbol to store in DB (defaults to symbol)
    """
    target = api_symbol or symbol
    store_as = db_symbol or symbol
    
    logger.info(f"[{store_as}] Starting ingestion (API: {target}) for last {days} days...")
    
    end_time_dt = datetime.now()
    start_time_dt = end_time_dt - timedelta(days=days)
    
    # Timeframes to ingest
    timeframes = ['15m', '1h', '4h']
    
    for timeframe in timeframes:
        logger.info(f"[{symbol}] Starting ingestion for {timeframe} (last {days} days)...")
        
        # Mapping timeframe to ms
        timeframe_map = {
            '1m': 60 * 1000,
            '5m': 5 * 60 * 1000,
            '15m': 15 * 60 * 1000,
            '1h': 60 * 60 * 1000,
            '4h': 4 * 60 * 60 * 1000,
            '1d': 24 * 60 * 60 * 1000,
        }
        interval_ms = timeframe_map.get(timeframe)
        
        # Chunk size adjustments
        # 1 week works for 1h (168 candles)
        # For 15m, 1 week is 4 * 168 = 672 candles (safe)
        chunk_size_hours = 168 
        chunk_ms = chunk_size_hours * 3600 * 1000
        
        current_end_ms = int(end_time_dt.timestamp() * 1000)
        final_start_ms = int(start_time_dt.timestamp() * 1000)
        
        total_candles = 0
        
        while current_end_ms > final_start_ms:
            current_start_ms = max(final_start_ms, current_end_ms - chunk_ms)
            
            try:
                candles = api.info.candles_snapshot(target, timeframe, current_start_ms, current_end_ms)
                
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
                    
                    # Insert into DB
                    db.insert_market_data(df, store_as, timeframe)
                    total_candles += len(df)
                    # logger.info(f"[{symbol} {timeframe}] Inserted {len(df)} candles")
                
            except Exception as e:
                logger.error(f"[{symbol} {timeframe}] Error fetching chunk: {e}")
                time.sleep(1)
                
            current_end_ms = current_start_ms - 1
            time.sleep(0.1)
            
        logger.info(f"[{symbol}] Completed {timeframe}. Total: {total_candles}")

def fetch_funding_history(api: HyperliquidAPI, db: TradeDatabase, symbol: str, days: int = 90):
    """Fetch funding history for a symbol and insert into DB."""
    logger.info(f"[{symbol}] Ingesting funding history for last {days} days...")
    
    end_time_dt = datetime.now()
    start_time_dt = end_time_dt - timedelta(days=days)
    
    current_end_ms = int(end_time_dt.timestamp() * 1000)
    final_start_ms = int(start_time_dt.timestamp() * 1000)
    
    # Chunk size: Funding is every 1h, so not huge. Can fetch larger chunks.
    chunk_size_hours = 24 * 7 # 1 week
    chunk_ms = chunk_size_hours * 3600 * 1000
    
    total_records = 0
    
    while current_end_ms > final_start_ms:
        current_start_ms = max(final_start_ms, current_end_ms - chunk_ms)
        
        try:
            history = api.get_funding_history(symbol, current_start_ms, current_end_ms)
            
            if history:
                data = []
                for h in history:
                    data.append({
                        'timestamp': pd.to_datetime(h['time'], unit='ms'),
                        'funding': float(h['fundingRate'])
                    })
                
                df = pd.DataFrame(data)
                df.set_index('timestamp', inplace=True)
                
                # Insert into DB
                count = db.insert_funding_rates(df, symbol)
                total_records += count
                
        except Exception as e:
            logger.error(f"[{symbol}] Error fetching funding chunk: {e}")
            time.sleep(1)
            
        current_end_ms = current_start_ms - 1
        time.sleep(0.1)
        
    logger.info(f"[{symbol}] Completed Funding History. Total: {total_records}")

def run_ingestion():
    config = load_config()
    api = HyperliquidAPI(config)
    db = TradeDatabase()
    
    logger.info("Identifying Top 50 Assets...")
    top_assets = get_top_assets(api, limit=50)
    logger.info(f"Assets to ingest: {top_assets}")
    
    # Process assets (Sequential for safety, or ThreadPool?)
    # API instance is shared, but `info` calls might be thread-safe enough? 
    # Let's do sequential to avoid rate limits since we are doing heavy fetching.
    
    for symbol in top_assets:
        # 1. Fetch PERP Candles
        fetch_history_for_symbol(api, db, symbol, days=90)
        
        # 2. Fetch PERP Funding
        fetch_funding_history(api, db, symbol, days=90)
        
        # 3. Fetch SPOT (if mapped)
        spot_token = api.get_spot_token_for_perp(symbol)
        if spot_token:
            spot_api_name = api.get_spot_api_name(spot_token)
            if spot_api_name:
                fetch_history_for_symbol(
                    api, db, symbol, days=90, 
                    api_symbol=spot_api_name, 
                    db_symbol=f"{symbol}_SPOT"
                )
            else:
                logger.warning(f"Could not find API name for spot token {spot_token} (for {symbol})")

if __name__ == "__main__":
    run_ingestion()
