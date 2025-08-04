"""
Hyperliquid WebSocket API client for real-time data streaming.
"""

import asyncio
import json
import logging
import time
from typing import Dict, List, Optional, Any, Callable
import pandas as pd
from datetime import datetime
import websocket
import threading
from collections import defaultdict, deque
import queue


class HyperliquidWebSocketAPI:
    """WebSocket-based client for Hyperliquid real-time data."""
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the WebSocket API client.
        
        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # WebSocket configuration
        self.ws_url = config['api']['ws_url']
        self.base_url = config['api']['base_url']
        self.private_key = config['api']['private_key']
        self.wallet_address = config['api']['wallet_address']
        
        # WebSocket connection
        self.ws = None
        self.is_connected = False
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 5
        
        # Data storage
        self.price_data = defaultdict(lambda: deque(maxlen=1000))  # symbol -> price history
        self.ohlcv_data = defaultdict(lambda: deque(maxlen=1000))  # symbol -> OHLCV history
        self.position_data = {}
        self.order_data = {}
        
        # Callbacks
        self.price_callbacks = []
        self.position_callbacks = []
        self.order_callbacks = []
        
        # Threading
        self.ws_thread = None
        self.data_queue = queue.Queue()
        self.running = False
        
        # Subscriptions
        self.subscribed_symbols = set()
        
        self.logger.info("Initialized Hyperliquid WebSocket API")
    
    def start(self):
        """Start the WebSocket connection and data collection."""
        if self.running:
            return
        
        self.running = True
        self.ws_thread = threading.Thread(target=self._ws_worker, daemon=True)
        self.ws_thread.start()
        
        # Start data processing thread
        self.data_thread = threading.Thread(target=self._data_processor, daemon=True)
        self.data_thread.start()
        
        self.logger.info("WebSocket API started")
    
    def stop(self):
        """Stop the WebSocket connection."""
        self.running = False
        if self.ws:
            self.ws.close()
        self.logger.info("WebSocket API stopped")
    
    def _ws_worker(self):
        """WebSocket worker thread."""
        while self.running:
            try:
                self._connect()
                self._subscribe_to_data()
                self._message_loop()
            except Exception as e:
                self.logger.error(f"WebSocket error: {e}")
                if self.reconnect_attempts < self.max_reconnect_attempts:
                    self.reconnect_attempts += 1
                    time.sleep(2 ** self.reconnect_attempts)  # Exponential backoff
                else:
                    self.logger.error("Max reconnection attempts reached")
                    break
    
    def _connect(self):
        """Establish WebSocket connection."""
        self.ws = websocket.WebSocketApp(
            self.ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close
        )
        self.ws.run_forever()
    
    def _on_open(self, ws):
        """Handle WebSocket connection open."""
        self.is_connected = True
        self.reconnect_attempts = 0
        self.logger.info("WebSocket connected")
    
    def _on_message(self, ws, message):
        """Handle incoming WebSocket messages."""
        try:
            data = json.loads(message)
            self.data_queue.put(data)
        except Exception as e:
            self.logger.error(f"Error parsing message: {e}")
    
    def _on_error(self, ws, error):
        """Handle WebSocket errors."""
        self.is_connected = False
        self.logger.error(f"WebSocket error: {error}")
    
    def _on_close(self, ws, close_status_code, close_msg):
        """Handle WebSocket connection close."""
        self.is_connected = False
        self.logger.info("WebSocket disconnected")
    
    def _data_processor(self):
        """Process incoming data from the queue."""
        while self.running:
            try:
                data = self.data_queue.get(timeout=1)
                self._process_message(data)
            except queue.Empty:
                continue
            except Exception as e:
                self.logger.error(f"Error processing data: {e}")
    
    def _process_message(self, data):
        """Process incoming WebSocket message."""
        try:
            msg_type = data.get('type')
            
            if msg_type == 'price':
                self._handle_price_update(data)
            elif msg_type == 'ohlcv':
                self._handle_ohlcv_update(data)
            elif msg_type == 'position':
                self._handle_position_update(data)
            elif msg_type == 'order':
                self._handle_order_update(data)
            else:
                self.logger.debug(f"Unknown message type: {msg_type}")
                
        except Exception as e:
            self.logger.error(f"Error processing message: {e}")
    
    def _handle_price_update(self, data):
        """Handle real-time price updates."""
        symbol = data.get('symbol')
        price = data.get('price')
        timestamp = data.get('timestamp', time.time())
        
        if symbol and price:
            self.price_data[symbol].append({
                'price': float(price),
                'timestamp': timestamp
            })
            
            # Notify callbacks
            for callback in self.price_callbacks:
                try:
                    callback(symbol, float(price), timestamp)
                except Exception as e:
                    self.logger.error(f"Price callback error: {e}")
    
    def _handle_ohlcv_update(self, data):
        """Handle real-time OHLCV updates."""
        symbol = data.get('symbol')
        ohlcv = data.get('ohlcv')
        
        if symbol and ohlcv:
            self.ohlcv_data[symbol].append(ohlcv)
    
    def _handle_position_update(self, data):
        """Handle real-time position updates."""
        position = data.get('position')
        if position:
            symbol = position.get('symbol')
            if symbol:
                self.position_data[symbol] = position
                
                # Notify callbacks
                for callback in self.position_callbacks:
                    try:
                        callback(position)
                    except Exception as e:
                        self.logger.error(f"Position callback error: {e}")
    
    def _handle_order_update(self, data):
        """Handle real-time order updates."""
        order = data.get('order')
        if order:
            order_id = order.get('id')
            if order_id:
                self.order_data[order_id] = order
                
                # Notify callbacks
                for callback in self.order_callbacks:
                    try:
                        callback(order)
                    except Exception as e:
                        self.logger.error(f"Order callback error: {e}")
    
    def _subscribe_to_data(self):
        """Subscribe to real-time data feeds."""
        if not self.is_connected:
            return
        
        # Subscribe to price feeds for all symbols
        for symbol in self.subscribed_symbols:
            self._subscribe_price_feed(symbol)
            self._subscribe_ohlcv_feed(symbol)
        
        # Subscribe to position and order updates
        if self.private_key and self.wallet_address:
            self._subscribe_position_updates()
            self._subscribe_order_updates()
    
    def _subscribe_price_feed(self, symbol: str):
        """Subscribe to real-time price feed for a symbol."""
        subscription = {
            "type": "subscribe",
            "channel": "price",
            "symbol": symbol
        }
        self.ws.send(json.dumps(subscription))
        self.logger.debug(f"Subscribed to price feed for {symbol}")
    
    def _subscribe_ohlcv_feed(self, symbol: str):
        """Subscribe to real-time OHLCV feed for a symbol."""
        subscription = {
            "type": "subscribe",
            "channel": "ohlcv",
            "symbol": symbol,
            "timeframe": "1m"
        }
        self.ws.send(json.dumps(subscription))
        self.logger.debug(f"Subscribed to OHLCV feed for {symbol}")
    
    def _subscribe_position_updates(self):
        """Subscribe to position updates."""
        subscription = {
            "type": "subscribe",
            "channel": "positions",
            "wallet": self.wallet_address
        }
        self.ws.send(json.dumps(subscription))
        self.logger.debug("Subscribed to position updates")
    
    def _subscribe_order_updates(self):
        """Subscribe to order updates."""
        subscription = {
            "type": "subscribe",
            "channel": "orders",
            "wallet": self.wallet_address
        }
        self.ws.send(json.dumps(subscription))
        self.logger.debug("Subscribed to order updates")
    
    def _message_loop(self):
        """Main message processing loop."""
        while self.is_connected and self.running:
            time.sleep(0.1)  # Small delay to prevent busy waiting
    
    def subscribe_symbol(self, symbol: str):
        """Subscribe to real-time data for a symbol."""
        self.subscribed_symbols.add(symbol)
        if self.is_connected:
            self._subscribe_price_feed(symbol)
            self._subscribe_ohlcv_feed(symbol)
    
    def get_current_price(self, symbol: str) -> Optional[float]:
        """Get current price for a symbol from WebSocket data."""
        if symbol in self.price_data and self.price_data[symbol]:
            latest = self.price_data[symbol][-1]
            return latest['price']
        return None
    
    def get_ohlcv(self, symbol: str, timeframe: str = '1m', limit: int = 100) -> Optional[pd.DataFrame]:
        """Get OHLCV data for a symbol from WebSocket data."""
        if symbol in self.ohlcv_data and self.ohlcv_data[symbol]:
            # Convert deque to DataFrame
            data = list(self.ohlcv_data[symbol])[-limit:]
            if data:
                df = pd.DataFrame(data)
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='s')
                return df
        return None
    
    def get_positions(self) -> List[Dict[str, Any]]:
        """Get current positions from WebSocket data."""
        return list(self.position_data.values())
    
    def get_open_orders(self) -> List[Dict[str, Any]]:
        """Get open orders from WebSocket data."""
        return [order for order in self.order_data.values() if order.get('status') == 'open']
    
    def add_price_callback(self, callback: Callable):
        """Add a callback for price updates."""
        self.price_callbacks.append(callback)
    
    def add_position_callback(self, callback: Callable):
        """Add a callback for position updates."""
        self.position_callbacks.append(callback)
    
    def add_order_callback(self, callback: Callable):
        """Add a callback for order updates."""
        self.order_callbacks.append(callback)
    
    def test_connection(self) -> bool:
        """Test WebSocket connection."""
        # Wait for connection to be established (max 10 seconds)
        timeout = 10
        start_time = time.time()
        
        while not self.is_connected and (time.time() - start_time) < timeout:
            time.sleep(0.1)
        
        if self.is_connected:
            self.logger.info("WebSocket connection test successful")
            return True
        else:
            self.logger.error("WebSocket connection test failed - connection not established within timeout")
            return False
    
    def start_data_collection(self):
        """Start data collection (placeholder for compatibility)."""
        self.logger.info("Data collection started (WebSocket handles this automatically)")
    
    def stop_data_collection(self):
        """Stop data collection (placeholder for compatibility)."""
        self.logger.info("Data collection stopped")
    
    def get_account_balance(self) -> Optional[Dict[str, Any]]:
        """Get account balance from Hyperliquid using REST API."""
        if not self.wallet_address:
            self.logger.warning("No wallet address configured - cannot fetch real balance")
            return None
        
        try:
            import requests
            
            # Get user state from Hyperliquid API
            # Try different API formats
            api_payloads = [
                {"type": "userState", "user": self.wallet_address},
                {"type": "userState", "user": self.wallet_address, "chainId": 42161},
                {"type": "userState", "user": self.wallet_address, "chainId": "42161"},
            ]
            
            response = None
            for payload in api_payloads:
                try:
                    response = requests.post(
                        f"{self.base_url}/info",
                        json=payload,
                        timeout=10
                    )
                    if response.status_code == 200:
                        break
                except Exception:
                    continue
            
            if response and response.status_code == 200:
                data = response.json()
                self.logger.debug(f"User state response: {data}")
                
                # Extract balance information from user state
                margin_summary = data.get('marginSummary', {})
                
                balance_info = {
                    'wallet_address': self.wallet_address,
                    'total_equity': float(margin_summary.get('accountValue', 0)),
                    'free_margin': float(margin_summary.get('freeCollateral', 0)),
                    'used_margin': float(margin_summary.get('totalNtlPos', 0)),
                    'unrealized_pnl': float(margin_summary.get('unrealizedPnl', 0)),
                    'realized_pnl': float(margin_summary.get('realizedPnl', 0)),
                }
                
                self.logger.info(f"Account balance retrieved: ${balance_info['total_equity']:.2f} total equity")
                return balance_info
            else:
                self.logger.error(f"Failed to get account balance: HTTP {response.status_code if response else 'No response'}")
                return None
                
        except Exception as e:
            self.logger.error(f"Error getting account balance: {e}")
            return None
    
    def get_asset_info(self) -> Optional[Dict[str, Any]]:
        """Get asset information from Hyperliquid using REST API."""
        try:
            import requests
            
            # Use REST API to get both meta and asset context data
            response = requests.post(
                f"{self.base_url}/info",
                json={"type": "metaAndAssetCtxs"},
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                if len(data) >= 2:
                    meta = data[0]  # Asset metadata
                    asset_contexts = data[1]  # Market context data
                    
                    # Convert to the expected format
                    universe = []
                    for i, asset in enumerate(meta.get('universe', [])):
                        # Skip delisted assets
                        if asset.get('isDelisted', False):
                            continue
                        
                        # Get corresponding market context data
                        asset_context = asset_contexts[i] if i < len(asset_contexts) else {}
                        
                        # Calculate open interest in dollars (tokens * price)
                        open_interest_tokens = float(asset_context.get('openInterest', 0))
                        mark_price = float(asset_context.get('markPx', 0))
                        open_interest_dollars = open_interest_tokens * mark_price
                        
                        # Calculate 24h volume in dollars
                        day_base_volume = float(asset_context.get('dayBaseVlm', 0))
                        volume_24h_dollars = day_base_volume * mark_price
                        
                        universe.append({
                            'name': asset['name'],
                            'maxLeverage': asset.get('maxLeverage', 10),
                            'szDecimals': asset.get('szDecimals', 0),
                            'marginTableId': asset.get('marginTableId', 0),
                            'openInterest': open_interest_dollars,
                            'markPx': mark_price,
                            'volume24h': volume_24h_dollars,
                            'bid': float(asset_context.get('impactPxs', [0, 0])[0]),
                            'ask': float(asset_context.get('impactPxs', [0, 0])[1]),
                            'funding': float(asset_context.get('funding', 0)),
                            'oraclePx': float(asset_context.get('oraclePx', 0)),
                        })
                    
                    self.logger.info(f"Retrieved {len(universe)} trading assets with market data")
                    return {'universe': universe}
                else:
                    self.logger.error("Invalid response format from Hyperliquid API")
                    return None
            else:
                self.logger.error(f"Failed to get asset info: HTTP {response.status_code}")
                return None
                
        except Exception as e:
            self.logger.error(f"Error getting asset info: {e}")
            return None
    
    def is_data_available(self, symbol: str) -> bool:
        """Check if sufficient data is available for a symbol."""
        # For WebSocket API, consider data available if we can get current price
        return self.get_current_price(symbol) is not None 