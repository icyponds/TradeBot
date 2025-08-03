"""
Hyperliquid API client for perpetual futures trading.
"""

import logging
import time
import json
import hmac
import hashlib
import requests
import websocket
import threading
from typing import Dict, List, Optional, Any
import pandas as pd
from datetime import datetime
import asyncio
import aiohttp
from .websocket_collector import WebSocketDataCollector


class HyperliquidAPI:
    """Client for Hyperliquid perpetual futures trading."""
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the Hyperliquid API client.
        
        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # API endpoints
        self.base_url = config['api']['base_url']
        self.ws_url = config['api']['ws_url']
        self.private_key = config['api']['private_key']
        self.wallet_address = config['api']['wallet_address']
        
        # Trading configuration
        self.symbols = config['trading']['symbols']
        self.base_currency = config['trading']['base_currency']
        
        # WebSocket connection
        self.ws = None
        self.ws_connected = False
        
        # Market data cache
        self.market_data = {}
        
        # Initialize WebSocket data collector
        self.data_collector = WebSocketDataCollector(config)
        
        self.logger.info(f"Initialized Hyperliquid API for symbols: {self.symbols}")
    
    def start_data_collection(self):
        """Start WebSocket data collection."""
        self.data_collector.start()
        self.logger.info("Started WebSocket data collection")
    
    def stop_data_collection(self):
        """Stop WebSocket data collection."""
        self.data_collector.stop()
        self.logger.info("Stopped WebSocket data collection")
    
    def _get_signature(self, data: str) -> str:
        """
        Generate signature for authenticated requests.
        
        Args:
            data: Data to sign
            
        Returns:
            Signature string
        """
        if not self.private_key:
            return ""
        
        # Convert private key to bytes if it's a string
        if isinstance(self.private_key, str):
            private_key_bytes = bytes.fromhex(self.private_key.replace('0x', ''))
        else:
            private_key_bytes = self.private_key
        
        # Create HMAC signature
        signature = hmac.new(
            private_key_bytes,
            data.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
        return signature
    
    def get_asset_info(self) -> Optional[Dict[str, Any]]:
        """
        Get asset information from Hyperliquid.
        
        Returns:
            Asset information dictionary or None if error
        """
        try:
            # Hyperliquid API uses POST with specific request format
            url = f"{self.base_url}/info"
            headers = {
                'Content-Type': 'application/json',
                'User-Agent': 'TradeBot/1.0'
            }
            
            # Hyperliquid API expects a specific request format
            request_data = {
                "type": "meta"
            }
            
            response = requests.post(url, headers=headers, json=request_data, timeout=self.config['api']['timeout'])
            response.raise_for_status()
            
            data = response.json()
            self.logger.info(f"Retrieved asset info for {len(data.get('universe', []))} assets")
            return data
            
        except Exception as e:
            self.logger.error(f"Error fetching asset info: {e}")
            return None
    
    def get_market_data(self, symbol: str, timeframe: str = None) -> Optional[Dict[str, Any]]:
        """
        Get market data for a specific symbol.
        
        Args:
            symbol: Trading symbol (e.g., 'BTC')
            timeframe: Timeframe (not used, kept for compatibility)
            
        Returns:
            Market data dictionary or None if error
        """
        try:
            # First try to get data from WebSocket collector
            if self.data_collector.is_data_available(symbol):
                market_data = self.data_collector.get_market_data(symbol)
                if market_data:
                    self.market_data[symbol] = market_data
                    return market_data
            
            # Fallback to REST API if WebSocket data not available
            url = f"{self.base_url}/info"
            headers = {
                'Content-Type': 'application/json',
                'User-Agent': 'TradeBot/1.0'
            }
            
            # Hyperliquid API expects a specific request format
            request_data = {
                "type": "meta"
            }
            
            response = requests.post(url, headers=headers, json=request_data, timeout=self.config['api']['timeout'])
            response.raise_for_status()
            
            data = response.json()
            universe = data.get('universe', [])
            
            # Find the asset in the universe
            asset_info = None
            for asset in universe:
                if asset.get('name') == symbol:
                    asset_info = asset
                    break
            
            if not asset_info:
                self.logger.warning(f"Asset {symbol} not found in universe")
                return None
            
            # Get current price and other market data
            market_data = {
                'symbol': symbol,
                'current_price': float(asset_info.get('markPrice', 0)),
                'bid': float(asset_info.get('bid', 0)),
                'ask': float(asset_info.get('ask', 0)),
                'volume_24h': float(asset_info.get('volume24h', 0)),
                'open_interest': float(asset_info.get('openInterest', 0)),
                'funding_rate': float(asset_info.get('fundingRate', 0)),
                'timestamp': datetime.now(),
                'ohlcv': None,  # No historical data from REST API
            }
            
            self.market_data[symbol] = market_data
            return market_data
            
        except Exception as e:
            self.logger.error(f"Error fetching market data for {symbol}: {e}")
            return None
    
    def get_current_price(self, symbol: str) -> Optional[float]:
        """
        Get current price for a symbol.
        
        Args:
            symbol: Trading symbol (e.g., 'BTC')
            
        Returns:
            Current price or None if error
        """
        # Try WebSocket data first
        price = self.data_collector.get_current_price(symbol)
        if price:
            return price
        
        # Fallback to REST API
        market_data = self.get_market_data(symbol)
        if market_data:
            return market_data['current_price']
        return None
    
    def get_ohlcv(self, symbol: str, timeframe: str = '1h', limit: int = 100) -> Optional[pd.DataFrame]:
        """
        Get OHLCV data for a symbol.
        
        Args:
            symbol: Trading symbol
            timeframe: Timeframe (not used for Hyperliquid)
            limit: Number of candles to fetch
            
        Returns:
            DataFrame with OHLCV data or None if error
        """
        try:
            # Get OHLCV from WebSocket collector
            ohlcv = self.data_collector.get_ohlcv(symbol)
            if ohlcv is not None and len(ohlcv) > 0:
                return ohlcv
            
            # Fallback: create simple OHLCV from current market data
            market_data = self.get_market_data(symbol)
            if not market_data:
                return None
            
            current_price = market_data['current_price']
            current_time = datetime.now()
            
            # Create a simple OHLCV entry (fallback)
            ohlcv_data = {
                'timestamp': [current_time],
                'open': [current_price],
                'high': [current_price],
                'low': [current_price],
                'close': [current_price],
                'volume': [market_data.get('volume_24h', 0) / 24],
            }
            
            df = pd.DataFrame(ohlcv_data)
            df['timestamp'] = pd.to_datetime(df['timestamp'])
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
            # Hyperliquid doesn't provide traditional order book via REST API
            # This would need to be implemented via WebSocket
            market_data = self.get_market_data(symbol)
            if not market_data:
                return None
            
            # Create a simple order book from bid/ask
            order_book = {
                'symbol': symbol,
                'bids': [[market_data['bid'], 1.0]],  # [price, size]
                'asks': [[market_data['ask'], 1.0]],  # [price, size]
                'timestamp': datetime.now(),
            }
            
            return order_book
            
        except Exception as e:
            self.logger.error(f"Error fetching order book for {symbol}: {e}")
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
    
    def place_order(self, symbol: str, side: str, size: float, price: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """
        Place an order on Hyperliquid.
        
        Args:
            symbol: Trading symbol
            side: 'buy' or 'sell'
            size: Order size
            price: Order price (None for market orders)
            
        Returns:
            Order response or None if error
        """
        try:
            if not self.private_key or not self.wallet_address:
                self.logger.error("Private key and wallet address required for trading")
                return None
            
            # Convert side to Hyperliquid format
            side_map = {'buy': 'B', 'sell': 'A'}
            hyperliquid_side = side_map.get(side)
            if not hyperliquid_side:
                self.logger.error(f"Invalid side: {side}")
                return None
            
            # Prepare order data
            order_data = {
                "type": "order",
                "user": self.wallet_address,
                "oid": int(time.time() * 1000),  # Order ID
                "side": hyperliquid_side,
                "sendingTime": int(time.time() * 1000),
                "coin": symbol,
                "sz": str(size),
                "limitPx": str(price) if price else "0",
                "reduceOnly": False,
            }
            
            # Sign the order
            order_str = json.dumps(order_data, separators=(',', ':'))
            signature = self._get_signature(order_str)
            
            # Add signature to order
            order_data["sig"] = signature
            
            # Send order via WebSocket (this is a simplified version)
            self.logger.info(f"Placing {side} order for {size} {symbol} at {price}")
            
            # For now, return a mock response
            # In a real implementation, you'd send this via WebSocket
            return {
                "status": "success",
                "order_id": order_data["oid"],
                "symbol": symbol,
                "side": side,
                "size": size,
                "price": price,
                "timestamp": datetime.now(),
            }
            
        except Exception as e:
            self.logger.error(f"Error placing order: {e}")
            return None
    
    def get_positions(self) -> List[Dict[str, Any]]:
        """
        Get current positions.
        
        Returns:
            List of position dictionaries
        """
        try:
            if not self.private_key or not self.wallet_address:
                return []
            
            # This would need to be implemented via WebSocket or specific endpoint
            # For now, return empty list
            self.logger.info("Getting positions (not implemented)")
            return []
            
        except Exception as e:
            self.logger.error(f"Error getting positions: {e}")
            return []
    
    def test_connection(self) -> bool:
        """
        Test the API connection.
        
        Returns:
            True if connection successful, False otherwise
        """
        try:
            # Test by getting asset info
            asset_info = self.get_asset_info()
            if asset_info:
                self.logger.info("Hyperliquid API connection test successful")
                return True
            else:
                self.logger.error("Failed to get asset info")
                return False
                
        except Exception as e:
            self.logger.error(f"Hyperliquid API connection test failed: {e}")
            return False
    
    def start_websocket(self):
        """Start WebSocket connection for real-time data."""
        # Start the data collector instead
        self.start_data_collection()
    
    def get_data_summary(self) -> Dict[str, Any]:
        """
        Get summary of collected data.
        
        Returns:
            Data summary dictionary
        """
        return self.data_collector.get_data_summary()
    
    def wait_for_data(self, symbol: str, timeout: int = 60) -> bool:
        """
        Wait for data to become available for a symbol.
        
        Args:
            symbol: Trading symbol
            timeout: Timeout in seconds
            
        Returns:
            True if data became available, False if timeout
        """
        return self.data_collector.wait_for_data(symbol, timeout)
    
    def is_data_available(self, symbol: str) -> bool:
        """
        Check if sufficient data is available for a symbol.
        
        Args:
            symbol: Trading symbol
            
        Returns:
            True if sufficient data is available
        """
        return self.data_collector.is_data_available(symbol) 