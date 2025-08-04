"""
Hybrid Hyperliquid API client that uses WebSocket for real-time data and SDK for trading operations.
"""

import logging
import time
import threading
from typing import Dict, List, Optional, Any, Callable
import pandas as pd
from datetime import datetime
from collections import defaultdict, deque

from .hyperliquid_sdk_api import HyperliquidSDKAPI
from .hyperliquid_websocket_api import HyperliquidWebSocketAPI


class HyperliquidHybridAPI:
    """Hybrid API client that combines WebSocket real-time data with SDK trading operations."""
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the hybrid API client.
        
        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # Initialize both APIs
        self.sdk_api = HyperliquidSDKAPI(config)
        self.ws_api = HyperliquidWebSocketAPI(config)
        
        # Real-time data storage with thread safety
        self._price_cache = defaultdict(lambda: deque(maxlen=1000))
        self._ohlcv_cache = defaultdict(lambda: deque(maxlen=1000))
        self._position_cache = {}
        self._order_cache = {}
        
        # Thread safety
        self._cache_lock = threading.Lock()
        
        # Callbacks for real-time updates
        self._price_callbacks = []
        self._position_callbacks = []
        self._order_callbacks = []
        
        # Connection status
        self._ws_connected = False
        self._sdk_connected = False
        
        # Subscribed symbols for real-time monitoring
        self._subscribed_symbols = set()
        
        self.logger.info("Initialized Hyperliquid Hybrid API")
    
    def start(self):
        """Start both WebSocket and SDK connections."""
        self.logger.info("Starting Hybrid API...")
        
        # Test SDK connection first
        self._sdk_connected = self.sdk_api.test_connection()
        if not self._sdk_connected:
            self.logger.error("SDK API connection failed")
            return False
        
        # Try to start WebSocket API for real-time data
        try:
            self.ws_api.start()
            
            # Setup WebSocket callbacks
            self._setup_ws_callbacks()
            
            # Wait for WebSocket connection with shorter timeout
            timeout = 5
            start_time = time.time()
            while not self.ws_api.is_connected and (time.time() - start_time) < timeout:
                time.sleep(0.1)
            
            if self.ws_api.is_connected:
                self._ws_connected = True
                self.logger.info("Hybrid API started successfully with WebSocket")
                return True
            else:
                self.logger.warning("WebSocket connection failed, will use REST fallback")
                self._ws_connected = False
                return True  # Still usable with REST fallback
                
        except Exception as e:
            self.logger.warning(f"WebSocket start failed: {e}, will use REST fallback")
            self._ws_connected = False
            return True  # Still usable with REST fallback
    
    def stop(self):
        """Stop both WebSocket and SDK connections."""
        self.logger.info("Stopping Hybrid API...")
        self.ws_api.stop()
        self._ws_connected = False
        self._sdk_connected = False
        self.logger.info("Hybrid API stopped")
    
    def _setup_ws_callbacks(self):
        """Setup WebSocket callbacks for real-time data."""
        def price_callback(symbol: str, price: float, timestamp: float):
            with self._cache_lock:
                self._price_cache[symbol].append({
                    'price': price,
                    'timestamp': timestamp
                })
            
            # Notify our callbacks
            for callback in self._price_callbacks:
                try:
                    callback(symbol, price, timestamp)
                except Exception as e:
                    self.logger.error(f"Price callback error: {e}")
        
        def position_callback(position: Dict[str, Any]):
            symbol = position.get('symbol')
            if symbol:
                with self._cache_lock:
                    self._position_cache[symbol] = position
                
                # Notify our callbacks
                for callback in self._position_callbacks:
                    try:
                        callback(position)
                    except Exception as e:
                        self.logger.error(f"Position callback error: {e}")
        
        def order_callback(order: Dict[str, Any]):
            order_id = order.get('id')
            if order_id:
                with self._cache_lock:
                    self._order_cache[order_id] = order
                
                # Notify our callbacks
                for callback in self._order_callbacks:
                    try:
                        callback(order)
                    except Exception as e:
                        self.logger.error(f"Order callback error: {e}")
        
        # Register callbacks with WebSocket API
        self.ws_api.add_price_callback(price_callback)
        self.ws_api.add_position_callback(position_callback)
        self.ws_api.add_order_callback(order_callback)
    
    def subscribe_symbol(self, symbol: str):
        """Subscribe to real-time data for a symbol."""
        self._subscribed_symbols.add(symbol)
        self.ws_api.subscribe_symbol(symbol)
        self.logger.info(f"Subscribed to real-time data for {symbol}")
    
    def get_current_price(self, symbol: str) -> Optional[float]:
        """
        Get current price with real-time priority.
        Uses WebSocket data if available, falls back to SDK.
        """
        # Try WebSocket data first (real-time)
        with self._cache_lock:
            if symbol in self._price_cache and self._price_cache[symbol]:
                latest = self._price_cache[symbol][-1]
                return latest['price']
        
        # Fallback to SDK API
        return self.sdk_api.get_current_price(symbol)
    
    def get_market_data(self, symbol: str, timeframe: str = None) -> Optional[Dict[str, Any]]:
        """
        Get market data with real-time price updates.
        Uses WebSocket for current price, SDK for other data.
        """
        # Get base market data from SDK
        market_data = self.sdk_api.get_market_data(symbol, timeframe)
        if not market_data:
            return None
        
        # Update with real-time price if available
        real_time_price = self.get_current_price(symbol)
        if real_time_price:
            market_data['current_price'] = real_time_price
            market_data['timestamp'] = datetime.now()
        
        return market_data
    
    def get_ohlcv(self, symbol: str, timeframe: str = '1h', limit: int = 100) -> Optional[pd.DataFrame]:
        """
        Get OHLCV data with real-time updates.
        Uses WebSocket data if available, falls back to SDK.
        """
        # Try WebSocket OHLCV data first
        with self._cache_lock:
            if symbol in self._ohlcv_cache and self._ohlcv_cache[symbol]:
                # Convert recent data to DataFrame
                data = list(self._ohlcv_cache[symbol])[-limit:]
                if data:
                    df = pd.DataFrame(data)
                    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
                    df.set_index('timestamp', inplace=True)
                    return df
        
        # Fallback to SDK API
        return self.sdk_api.get_ohlcv(symbol, timeframe, limit)
    
    def get_positions(self) -> List[Dict[str, Any]]:
        """
        Get positions with real-time updates.
        Uses WebSocket data if available, falls back to SDK.
        """
        # Try WebSocket position data first
        with self._cache_lock:
            if self._position_cache:
                return list(self._position_cache.values())
        
        # Fallback to SDK API
        return self.sdk_api.get_positions()
    
    def get_open_orders(self) -> List[Dict[str, Any]]:
        """
        Get open orders with real-time updates.
        Uses WebSocket data if available, falls back to SDK.
        """
        # Try WebSocket order data first
        with self._cache_lock:
            if self._order_cache:
                return [order for order in self._order_cache.values() if order.get('status') == 'open']
        
        # Fallback to SDK API
        return self.sdk_api.get_open_orders()
    
    def place_order(self, symbol: str, side: str, size: float, price: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Place order using SDK API (trading operations)."""
        return self.sdk_api.place_order(symbol, side, size, price)
    
    def cancel_order(self, order_id: str) -> bool:
        """Cancel order using SDK API."""
        return self.sdk_api.cancel_order(order_id)
    
    def get_order_status(self, order_id: str) -> Optional[Dict[str, Any]]:
        """Get order status using SDK API."""
        return self.sdk_api.get_order_status(order_id)
    
    def get_account_balance(self) -> Optional[Dict[str, Any]]:
        """Get account balance using SDK API."""
        return self.sdk_api.get_account_balance()
    
    def get_asset_info(self) -> Optional[Dict[str, Any]]:
        """Get asset information using SDK API."""
        return self.sdk_api.get_asset_info()
    
    def test_connection(self) -> bool:
        """Test both WebSocket and SDK connections."""
        sdk_ok = self.sdk_api.test_connection()
        
        # Try WebSocket connection but don't fail if it doesn't work
        try:
            ws_ok = self.ws_api.test_connection()
        except Exception as e:
            self.logger.warning(f"WebSocket connection test failed: {e}")
            ws_ok = False
        
        if sdk_ok:
            if ws_ok:
                self.logger.info("Hybrid API connection test successful (WebSocket + SDK)")
                return True
            else:
                self.logger.warning("Hybrid API connection test: SDK OK, WebSocket failed - will use REST fallback")
                return True  # Still usable with REST fallback
        else:
            self.logger.error(f"Hybrid API connection test failed - SDK: {sdk_ok}, WebSocket: {ws_ok}")
            return False
    
    def is_data_available(self, symbol: str) -> bool:
        """Check if data is available (real-time or cached)."""
        # Check WebSocket data first
        with self._cache_lock:
            if symbol in self._price_cache and self._price_cache[symbol]:
                return True
        
        # Fallback to SDK check
        return self.sdk_api.is_data_available(symbol)
    
    def add_price_callback(self, callback: Callable):
        """Add callback for real-time price updates."""
        self._price_callbacks.append(callback)
    
    def add_position_callback(self, callback: Callable):
        """Add callback for real-time position updates."""
        self._position_callbacks.append(callback)
    
    def add_order_callback(self, callback: Callable):
        """Add callback for real-time order updates."""
        self._order_callbacks.append(callback)
    
    def get_data_summary(self) -> Dict[str, Any]:
        """Get summary of available data."""
        with self._cache_lock:
            return {
                'ws_connected': self._ws_connected,
                'sdk_connected': self._sdk_connected,
                'subscribed_symbols': list(self._subscribed_symbols),
                'price_cache_size': {k: len(v) for k, v in self._price_cache.items()},
                'position_cache_size': len(self._position_cache),
                'order_cache_size': len(self._order_cache),
                'timestamp': datetime.now().isoformat(),
            }
    
    def wait_for_data(self, symbol: str, timeout: int = 60) -> bool:
        """Wait for data to become available for a symbol."""
        start_time = time.time()
        while time.time() - start_time < timeout:
            if self.is_data_available(symbol):
                return True
            time.sleep(1)
        return False
    
    def start_data_collection(self):
        """Start data collection (compatibility method)."""
        self.start()
    
    def stop_data_collection(self):
        """Stop data collection (compatibility method)."""
        self.stop() 