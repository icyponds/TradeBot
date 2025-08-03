#!/usr/bin/env python3
"""
Test script for WebSocket data collection.
"""

import sys
import time
import logging
from src.config.settings import load_config
from src.api.hyperliquid_api import HyperliquidAPI
from src.utils.logger import setup_logging


def test_websocket_data_collection():
    """Test WebSocket data collection functionality."""
    # Setup logging
    setup_logging()
    logger = logging.getLogger(__name__)
    
    logger.info("Testing WebSocket data collection...")
    
    # Load configuration
    config = load_config()
    if not config:
        logger.error("Failed to load configuration")
        return False
    
    # Initialize API
    api = HyperliquidAPI(config)
    
    # Test connection
    if not api.test_connection():
        logger.error("Failed to connect to Hyperliquid API")
        return False
    
    logger.info("API connection successful")
    
    # Start data collection
    logger.info("Starting WebSocket data collection...")
    api.start_data_collection()
    
    # Wait for data collection
    logger.info("Waiting for data collection (30 seconds)...")
    time.sleep(30)
    
    # Check data summary
    summary = api.get_data_summary()
    logger.info(f"Data summary: {summary}")
    
    # Test getting data for specific symbols
    test_symbols = ['BTC', 'ETH', 'SOL']
    
    for symbol in test_symbols:
        logger.info(f"Testing data for {symbol}...")
        
        # Check if data is available
        if api.is_data_available(symbol):
            logger.info(f"✓ Data available for {symbol}")
            
            # Get current price
            price = api.get_current_price(symbol)
            if price:
                logger.info(f"✓ Current price for {symbol}: {price}")
            
            # Get OHLCV data
            ohlcv = api.get_ohlcv(symbol)
            if ohlcv is not None and len(ohlcv) > 0:
                logger.info(f"✓ OHLCV data for {symbol}: {len(ohlcv)} candles")
                logger.info(f"  Latest candle: {ohlcv.iloc[-1]}")
            else:
                logger.warning(f"✗ No OHLCV data for {symbol}")
        else:
            logger.warning(f"✗ No data available for {symbol}")
    
    # Stop data collection
    api.stop_data_collection()
    logger.info("WebSocket data collection stopped")
    
    return True


if __name__ == "__main__":
    success = test_websocket_data_collection()
    sys.exit(0 if success else 1) 