"""
WebSocket data collector for Hyperliquid real-time data.
"""

import logging
import json
import time
import threading
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
import pandas as pd
import websocket
from collections import defaultdict, deque


class WebSocketDataCollector:
    """Collects real-time data from Hyperliquid WebSocket and builds OHLCV candles."""
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the WebSocket data collector.
        
        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # WebSocket configuration
        self.ws_url = config['api']['ws_url']
        self.ws = None
        self.ws_connected = False
        
        # Data storage
        self.price_buffer = defaultdict(list)  # symbol -> list of (price, timestamp)
        self.ohlcv_cache = {}  # symbol -> DataFrame of candles
        self.last_update = {}  # symbol -> last update timestamp
        self.current_candles = {}  # symbol -> current candle data
        
        # Configuration
        self.timeframe = config['strategies']['timeframe']
        self.ohlcv_limit = config['strategies']['ohlcv_limit']
        self.symbols = config['trading']['symbols'] if 'symbols' in config['trading'] else []
        
        # Threading
        self.data_lock = threading.Lock()
        self.running = False
        self.collection_thread = None
        
        # Timeframe mapping
        self.timeframe_seconds = {
            '1m': 60,
            '5m': 300,
            '15m': 900,
            '30m': 1800,
            '1h': 3600,
            '4h': 14400,
            '1d': 86400,
        }
        
        self.logger.info(f"Initialized WebSocket collector for {self.timeframe} timeframe")
    
    def start(self):
        """Start the WebSocket data collection."""
        if self.running:
            self.logger.warning("WebSocket collector already running")
            return
        
        self.running = True
        self.collection_thread = threading.Thread(target=self._run_websocket)
        self.collection_thread.daemon = True
        self.collection_thread.start()
        
        self.logger.info("WebSocket data collection started")
    
    def stop(self):
        """Stop the WebSocket data collection."""
        self.running = False
        if self.ws:
            self.ws.close()
        
        if self.collection_thread:
            self.collection_thread.join(timeout=5)
        
        self.logger.info("WebSocket data collection stopped")
    
    def _run_websocket(self):
        """Run the WebSocket connection."""
        try:
            self.ws = websocket.WebSocketApp(
                self.ws_url,
                on_open=self._on_ws_open,
                on_message=self._on_ws_message,
                on_error=self._on_ws_error,
                on_close=self._on_ws_close
            )
            
            self.ws.run_forever()
            
        except Exception as e:
            self.logger.error(f"WebSocket error: {e}")
            self.ws_connected = False
    
    def _on_ws_open(self, ws):
        """WebSocket open callback."""
        self.logger.info("WebSocket connected")
        self.ws_connected = True
        
        # Subscribe to market data for all symbols
        self._subscribe_to_market_data()
    
    def _on_ws_message(self, ws, message):
        """WebSocket message callback."""
        try:
            data = json.loads(message)
            self._handle_market_data(data)
        except Exception as e:
            self.logger.error(f"Error handling WebSocket message: {e}")
    
    def _on_ws_error(self, ws, error):
        """WebSocket error callback."""
        self.logger.error(f"WebSocket error: {error}")
        self.ws_connected = False
    
    def _on_ws_close(self, ws, close_status_code, close_msg):
        """WebSocket close callback."""
        self.logger.info("WebSocket disconnected")
        self.ws_connected = False
    
    def _subscribe_to_market_data(self):
        """Subscribe to market data for all symbols."""
        try:
            # Subscribe to all available symbols
            subscribe_msg = {
                "type": "subscribe",
                "channel": "market",
                "symbols": self.symbols if self.symbols else ["*"]  # Subscribe to all if no specific symbols
            }
            
            self.ws.send(json.dumps(subscribe_msg))
            self.logger.info(f"Subscribed to market data for {len(self.symbols) if self.symbols else 'all'} symbols")
            
        except Exception as e:
            self.logger.error(f"Error subscribing to market data: {e}")
    
    def _handle_market_data(self, data: Dict[str, Any]):
        """Handle incoming market data."""
        try:
            if data.get('type') == 'market':
                symbol = data.get('symbol')
                price = float(data.get('price', 0))
                timestamp = datetime.now()
                
                if symbol and price > 0:
                    self._process_price_update(symbol, price, timestamp)
                    
        except Exception as e:
            self.logger.error(f"Error processing market data: {e}")
    
    def _process_price_update(self, symbol: str, price: float, timestamp: datetime):
        """Process a price update and build OHLCV candles."""
        with self.data_lock:
            # Add to price buffer
            self.price_buffer[symbol].append((price, timestamp))
            
            # Limit buffer size
            max_buffer_size = self.ohlcv_limit * 2  # Keep extra data for building candles
            if len(self.price_buffer[symbol]) > max_buffer_size:
                self.price_buffer[symbol] = self.price_buffer[symbol][-max_buffer_size:]
            
            # Update current candle
            self._update_current_candle(symbol, price, timestamp)
            
            # Build complete candles
            self._build_complete_candles(symbol)
            
            # Update last update timestamp
            self.last_update[symbol] = timestamp
    
    def _update_current_candle(self, symbol: str, price: float, timestamp: datetime):
        """Update the current candle with new price data."""
        if symbol not in self.current_candles:
            self.current_candles[symbol] = {
                'open': price,
                'high': price,
                'low': price,
                'close': price,
                'volume': 0,
                'start_time': timestamp,
                'count': 1
            }
        else:
            candle = self.current_candles[symbol]
            candle['high'] = max(candle['high'], price)
            candle['low'] = min(candle['low'], price)
            candle['close'] = price
            candle['count'] += 1
    
    def _build_complete_candles(self, symbol: str):
        """Build complete candles from price updates."""
        if symbol not in self.current_candles:
            return
        
        current_candle = self.current_candles[symbol]
        timeframe_seconds = self.timeframe_seconds.get(self.timeframe, 60)
        
        # Check if current candle is complete
        candle_duration = (datetime.now() - current_candle['start_time']).total_seconds()
        
        if candle_duration >= timeframe_seconds:
            # Create complete candle
            complete_candle = {
                'timestamp': current_candle['start_time'],
                'open': current_candle['open'],
                'high': current_candle['high'],
                'low': current_candle['low'],
                'close': current_candle['close'],
                'volume': current_candle['volume'],
            }
            
            # Add to OHLCV cache
            if symbol not in self.ohlcv_cache:
                self.ohlcv_cache[symbol] = []
            
            self.ohlcv_cache[symbol].append(complete_candle)
            
            # Limit cache size
            if len(self.ohlcv_cache[symbol]) > self.ohlcv_limit:
                self.ohlcv_cache[symbol] = self.ohlcv_cache[symbol][-self.ohlcv_limit:]
            
            # Reset current candle
            self.current_candles[symbol] = {
                'open': current_candle['close'],
                'high': current_candle['close'],
                'low': current_candle['close'],
                'close': current_candle['close'],
                'volume': 0,
                'start_time': datetime.now(),
                'count': 1
            }
    
    def get_ohlcv(self, symbol: str) -> Optional[pd.DataFrame]:
        """
        Get OHLCV data for a symbol.
        
        Args:
            symbol: Trading symbol
            
        Returns:
            DataFrame with OHLCV data or None if not available
        """
        with self.data_lock:
            if symbol not in self.ohlcv_cache or not self.ohlcv_cache[symbol]:
                return None
            
            # Convert to DataFrame
            df = pd.DataFrame(self.ohlcv_cache[symbol])
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            df.set_index('timestamp', inplace=True)
            
            return df
    
    def get_current_price(self, symbol: str) -> Optional[float]:
        """
        Get current price for a symbol.
        
        Args:
            symbol: Trading symbol
            
        Returns:
            Current price or None if not available
        """
        with self.data_lock:
            if symbol in self.current_candles:
                return self.current_candles[symbol]['close']
            elif symbol in self.price_buffer and self.price_buffer[symbol]:
                return self.price_buffer[symbol][-1][0]
            return None
    
    def get_market_data(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get comprehensive market data for a symbol.
        
        Args:
            symbol: Trading symbol
            
        Returns:
            Market data dictionary or None if not available
        """
        with self.data_lock:
            current_price = self.get_current_price(symbol)
            if not current_price:
                return None
            
            ohlcv = self.get_ohlcv(symbol)
            
            return {
                'symbol': symbol,
                'current_price': current_price,
                'ohlcv': ohlcv,
                'timestamp': datetime.now(),
                'last_update': self.last_update.get(symbol),
            }
    
    def is_data_available(self, symbol: str) -> bool:
        """
        Check if sufficient data is available for a symbol.
        
        Args:
            symbol: Trading symbol
            
        Returns:
            True if sufficient data is available
        """
        with self.data_lock:
            if symbol not in self.ohlcv_cache:
                return False
            
            # Check if we have enough candles for analysis
            required_candles = max(20, self.ohlcv_limit // 2)  # At least 20 candles
            return len(self.ohlcv_cache[symbol]) >= required_candles
    
    def get_data_summary(self) -> Dict[str, Any]:
        """
        Get summary of collected data.
        
        Returns:
            Data summary dictionary
        """
        with self.data_lock:
            summary = {
                'connected': self.ws_connected,
                'symbols_with_data': len(self.ohlcv_cache),
                'total_candles': sum(len(candles) for candles in self.ohlcv_cache.values()),
                'symbols': list(self.ohlcv_cache.keys()),
                'timeframe': self.timeframe,
            }
            
            return summary
    
    def wait_for_data(self, symbol: str, timeout: int = 60) -> bool:
        """
        Wait for data to become available for a symbol.
        
        Args:
            symbol: Trading symbol
            timeout: Timeout in seconds
            
        Returns:
            True if data became available, False if timeout
        """
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            if self.is_data_available(symbol):
                return True
            time.sleep(1)
        
        return False 