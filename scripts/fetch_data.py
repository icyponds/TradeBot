
import os
import sys
import pandas as pd
from datetime import datetime, timedelta
import logging
import time

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config.settings import load_config
from src.api.hyperliquid_api import HyperliquidAPI

def fetch_data():
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("DataFetcher")
    
    config = load_config()
    api = HyperliquidAPI(config)
    
    # Symbols to fetch - Top liquid pairs
    symbols = ['BTC', 'ETH', 'SOL']
    
    # Time range: Last 7 days (168 hours) to give a decent sample
    # Limit is typically around 500 per call for many APIs, check implementation
    limit = 200 
    
    data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
    os.makedirs(data_dir, exist_ok=True)
    
    for symbol in symbols:
        logger.info(f"Fetching data for {symbol}...")
        try:
            # Fetch 1h candles
            candles = api.get_ohlcv(symbol, timeframe='1h', limit=limit)
            
            if candles is not None and not candles.empty:
                # Ensure index is datetime
                if not isinstance(candles.index, pd.DatetimeIndex):
                    candles.index = pd.to_datetime(candles['timestamp'], unit='ms')
                
                filename = os.path.join(data_dir, f"{symbol}_1h.csv")
                candles.to_csv(filename)
                logger.info(f"Saved {len(candles)} rows to {filename}")
            else:
                logger.warning(f"No data found for {symbol}")
                
            time.sleep(0.5) # Be nice to rate limits
                
        except Exception as e:
            logger.error(f"Error fetching {symbol}: {e}")

if __name__ == "__main__":
    fetch_data()
