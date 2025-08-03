"""
Market data API client for fetching quotes from external APIs.
"""

import logging
import time
from typing import Dict, List, Optional, Any
import requests
import ccxt
import pandas as pd
from datetime import datetime


class MarketDataAPI:
    """Client for fetching market data from various APIs."""
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the market data API client.
        
        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # Initialize exchange client (using ccxt for compatibility)
        self.exchange = ccxt.binance({
            'apiKey': config['api']['api_key'],
            'secret': config['api']['api_secret'],
            'timeout': config['api']['timeout'] * 1000,
            'enableRateLimit': True,
        })
        
        self.symbols = config['trading']['symbols']
        self.base_currency = config['trading']['base_currency']
    
    def get_current_price(self, symbol: str) -> Optional[float]:
        """
        Get current price for a symbol.
        
        Args:
            symbol: Trading symbol (e.g., 'BTC/USD')
            
        Returns:
            Current price or None if error
        """
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            return ticker['last']
        except Exception as e:
            self.logger.error(f"Error fetching price for {symbol}: {e}")
            return None
    
    def get_ohlcv(self, symbol: str, timeframe: str = '1h', limit: int = 100) -> Optional[pd.DataFrame]:
        """
        Get OHLCV (Open, High, Low, Close, Volume) data.
        
        Args:
            symbol: Trading symbol
            timeframe: Timeframe (1m, 5m, 1h, 1d, etc.)
            limit: Number of candles to fetch
            
        Returns:
            DataFrame with OHLCV data or None if error
        """
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            df.set_index('timestamp', inplace=True)
            return df
        except Exception as e:
            self.logger.error(f"Error fetching OHLCV for {symbol}: {e}")
            return None
    
    def get_order_book(self, symbol: str, limit: int = 20) -> Optional[Dict[str, Any]]:
        """
        Get order book for a symbol.
        
        Args:
            symbol: Trading symbol
            limit: Number of orders to fetch
            
        Returns:
            Order book dictionary or None if error
        """
        try:
            order_book = self.exchange.fetch_order_book(symbol, limit)
            return order_book
        except Exception as e:
            self.logger.error(f"Error fetching order book for {symbol}: {e}")
            return None
    
    def get_ticker(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get ticker information for a symbol.
        
        Args:
            symbol: Trading symbol
            
        Returns:
            Ticker dictionary or None if error
        """
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            return ticker
        except Exception as e:
            self.logger.error(f"Error fetching ticker for {symbol}: {e}")
            return None
    
    def get_all_prices(self) -> Dict[str, float]:
        """
        Get current prices for all configured symbols.
        
        Returns:
            Dictionary mapping symbols to prices
        """
        prices = {}
        for symbol in self.symbols:
            price = self.get_current_price(symbol)
            if price:
                prices[symbol] = price
        return prices
    
    def get_market_data(self, symbol: str, timeframe: str = '1h') -> Optional[Dict[str, Any]]:
        """
        Get comprehensive market data for a symbol.
        
        Args:
            symbol: Trading symbol
            timeframe: Timeframe for OHLCV data
            
        Returns:
            Dictionary with all market data or None if error
        """
        try:
            # Get current price
            current_price = self.get_current_price(symbol)
            if not current_price:
                return None
            
            # Get OHLCV data
            ohlcv = self.get_ohlcv(symbol, timeframe)
            if ohlcv is None:
                return None
            
            # Get ticker
            ticker = self.get_ticker(symbol)
            
            return {
                'symbol': symbol,
                'current_price': current_price,
                'ohlcv': ohlcv,
                'ticker': ticker,
                'timestamp': datetime.now(),
            }
        except Exception as e:
            self.logger.error(f"Error getting market data for {symbol}: {e}")
            return None
    
    def test_connection(self) -> bool:
        """
        Test the API connection.
        
        Returns:
            True if connection successful, False otherwise
        """
        try:
            # Try to fetch a simple ticker
            test_symbol = self.symbols[0] if self.symbols else 'BTC/USD'
            ticker = self.exchange.fetch_ticker(test_symbol)
            self.logger.info(f"API connection test successful for {test_symbol}")
            return True
        except Exception as e:
            self.logger.error(f"API connection test failed: {e}")
            return False 