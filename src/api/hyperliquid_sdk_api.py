"""
Hyperliquid API client using the official Python SDK.
"""

import logging
import time
from typing import Dict, List, Optional, Any
import pandas as pd
from datetime import datetime
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange


class HyperliquidSDKAPI:
    """Client for Hyperliquid perpetual futures trading using the official SDK."""
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the Hyperliquid API client using the official SDK.
        
        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # API configuration
        self.base_url = config['api']['base_url']
        self.private_key = config['api']['private_key']
        self.wallet_address = config['api']['wallet_address']
        
        # Trading configuration
        self.symbols = config['trading']['symbols']
        self.base_currency = config['trading']['base_currency']
        
        # Initialize SDK clients
        self.info_client = Info(self.base_url)
        self.exchange_client = None
        
        # Initialize exchange client if credentials are provided
        if self.private_key and self.wallet_address:
            try:
                self.exchange_client = Exchange(
                    self.base_url,
                    self.private_key,
                    self.wallet_address
                )
                self.logger.info("Exchange client initialized with credentials")
            except Exception as e:
                self.logger.warning(f"Failed to initialize exchange client: {e}")
                self.logger.info("Running in read-only mode")
        
        # Market data cache
        self.market_data = {}
        
        self.logger.info(f"Initialized Hyperliquid SDK API for symbols: {self.symbols}")
    
    def start_data_collection(self):
        """Start data collection (placeholder for compatibility)."""
        self.logger.info("Data collection started (SDK handles this automatically)")
    
    def stop_data_collection(self):
        """Stop data collection (placeholder for compatibility)."""
        self.logger.info("Data collection stopped")
    
    def get_asset_info(self) -> Optional[Dict[str, Any]]:
        """
        Get asset information from Hyperliquid using the SDK.
        
        Returns:
            Asset information dictionary or None if error
        """
        try:
            # Get meta information using the SDK
            meta = self.info_client.meta()
            
            # Convert to our expected format
            universe = []
            for asset in meta['universe']:
                universe.append({
                    'name': asset['name'],
                    'maxLeverage': asset.get('maxLeverage', 0),
                    'szDecimals': asset.get('szDecimals', 0),
                    'marginTableId': asset.get('marginTableId', 0),
                    'priceDecimals': asset.get('priceDecimals', 0),
                    'isLinear': asset.get('isLinear', True),
                    'oracle': asset.get('oracle', ''),
                    'openInterest': 0,  # Will be fetched separately
                    'volume24h': 0,     # Will be fetched separately
                    'markPrice': 0,     # Will be fetched separately
                })
            
            result = {
                'universe': universe,
                'meta': meta
            }
            
            self.logger.info(f"Retrieved asset info for {len(universe)} assets")
            return result
            
        except Exception as e:
            self.logger.error(f"Error fetching asset info: {e}")
            return None
    
    def get_market_data(self, symbol: str, timeframe: str = None) -> Optional[Dict[str, Any]]:
        """
        Get market data for a specific symbol using the SDK.
        
        Args:
            symbol: Trading symbol (e.g., 'BTC')
            timeframe: Timeframe (not used, kept for compatibility)
            
        Returns:
            Market data dictionary or None if error
        """
        try:
            # Get current state using the SDK
            meta_and_ctxs = self.info_client.meta_and_asset_ctxs()
            
            # meta_and_ctxs is a list with 2 items:
            # [0] = meta data (universe, marginTables)
            # [1] = asset contexts with current prices
            if len(meta_and_ctxs) < 2:
                self.logger.error("Invalid response from meta_and_asset_ctxs")
                return None
            
            universe = meta_and_ctxs[0]['universe']
            asset_contexts = meta_and_ctxs[1]
            
            # Find the asset by name in universe and get corresponding context
            asset_info = None
            asset_context = None
            
            for i, asset in enumerate(universe):
                if asset.get('name') == symbol:
                    asset_info = asset
                    if i < len(asset_contexts):
                        asset_context = asset_contexts[i]
                    break
            
            if not asset_info or not asset_context:
                self.logger.warning(f"Asset {symbol} not found in state")
                return None
            
            # Get current price and other market data from asset context
            market_data = {
                'symbol': symbol,
                'current_price': float(asset_context.get('markPx', 0)),
                'bid': float(asset_context.get('impactPxs', [0, 0])[0] if asset_context.get('impactPxs') else 0),
                'ask': float(asset_context.get('impactPxs', [0, 0])[1] if asset_context.get('impactPxs') and len(asset_context.get('impactPxs')) > 1 else 0),
                'volume_24h': float(asset_context.get('dayNtlVlm', 0)),
                'open_interest': float(asset_context.get('openInterest', 0)),
                'funding_rate': float(asset_context.get('funding', 0)),
                'timestamp': datetime.now(),
                'ohlcv': None,  # Historical data needs separate call
            }
            
            self.market_data[symbol] = market_data
            return market_data
            
        except Exception as e:
            self.logger.error(f"Error fetching market data for {symbol}: {e}")
            return None
    
    def get_current_price(self, symbol: str) -> Optional[float]:
        """
        Get current price for a symbol using the SDK.
        
        Args:
            symbol: Trading symbol (e.g., 'BTC')
            
        Returns:
            Current price or None if error
        """
        market_data = self.get_market_data(symbol)
        if market_data:
            return market_data['current_price']
        return None
    
    def get_ohlcv(self, symbol: str, timeframe: str = '1h', limit: int = 100) -> Optional[pd.DataFrame]:
        """
        Get OHLCV data for a symbol using the SDK.
        
        Args:
            symbol: Trading symbol (e.g., 'BTC')
            timeframe: Timeframe (e.g., '1h', '1d')
            limit: Number of candles to fetch
            
        Returns:
            DataFrame with OHLCV data or None if error
        """
        try:
            # Note: The SDK might not have direct OHLCV endpoint
            # For now, return None - we'll need to implement this separately
            self.logger.warning(f"OHLCV data not implemented for {symbol}")
            return None
            
        except Exception as e:
            self.logger.error(f"Error fetching OHLCV for {symbol}: {e}")
            return None
    
    def get_order_book(self, symbol: str, limit: int = 20) -> Optional[Dict[str, Any]]:
        """
        Get order book for a symbol using the SDK.
        
        Args:
            symbol: Trading symbol (e.g., 'BTC')
            limit: Number of orders to fetch
            
        Returns:
            Order book dictionary or None if error
        """
        try:
            # Get order book using the SDK
            order_book = self.info_client.order_book(symbol)
            
            return {
                'symbol': symbol,
                'bids': order_book.get('bids', []),
                'asks': order_book.get('asks', []),
                'timestamp': datetime.now(),
            }
            
        except Exception as e:
            self.logger.error(f"Error fetching order book for {symbol}: {e}")
            return None
    
    def get_all_prices(self) -> Dict[str, float]:
        """
        Get current prices for all symbols using the SDK.
        
        Returns:
            Dictionary of symbol -> price
        """
        try:
            meta_and_ctxs = self.info_client.meta_and_asset_ctxs()
            prices = {}
            
            if len(meta_and_ctxs) < 2:
                return {}
            
            universe = meta_and_ctxs[0]['universe']
            asset_contexts = meta_and_ctxs[1]
            
            for i, asset in enumerate(universe):
                symbol = asset.get('name')
                if i < len(asset_contexts):
                    price = float(asset_contexts[i].get('markPx', 0))
                    if symbol and price > 0:
                        prices[symbol] = price
            
            return prices
            
        except Exception as e:
            self.logger.error(f"Error fetching all prices: {e}")
            return {}
    
    def place_order(self, symbol: str, side: str, size: float, price: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """
        Place an order using the SDK.
        
        Args:
            symbol: Trading symbol (e.g., 'BTC')
            side: 'buy' or 'sell'
            size: Order size
            price: Order price (None for market orders)
            
        Returns:
            Order response dictionary or None if error
        """
        if not self.exchange_client:
            self.logger.error("Exchange client not initialized - no private key provided")
            return None
        
        try:
            # Convert side to SDK format
            sdk_side = 'B' if side == 'buy' else 'A'
            
            # Place order using the SDK
            order_response = self.exchange_client.order(
                symbol=symbol,
                side=sdk_side,
                sz=size,
                px=price if price else 0,  # 0 for market orders
                reduce_only=False,
                order_type='LIMIT' if price else 'MARKET'
            )
            
            return {
                "status": "success",
                "order_id": order_response.get('oid'),
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
        Get current positions using the SDK.
        
        Returns:
            List of position dictionaries
        """
        if not self.exchange_client:
            return []
        
        try:
            # Get user state using the SDK
            user_state = self.exchange_client.user_state()
            
            positions = []
            for position in user_state.get('assetPositions', []):
                if float(position.get('position', 0)) != 0:
                    positions.append({
                        'symbol': position['name'],
                        'size': float(position.get('position', 0)),
                        'side': 'long' if float(position.get('position', 0)) > 0 else 'short',
                        'entry_price': float(position.get('entryPx', 0)),
                        'mark_price': float(position.get('markPx', 0)),
                        'unrealized_pnl': float(position.get('unrealizedPnl', 0)),
                    })
            
            return positions
            
        except Exception as e:
            self.logger.error(f"Error getting positions: {e}")
            return []
    
    def test_connection(self) -> bool:
        """
        Test the API connection using the SDK.
        
        Returns:
            True if connection successful, False otherwise
        """
        try:
            # Test by getting meta information
            meta = self.info_client.meta()
            if meta and 'universe' in meta:
                self.logger.info("Hyperliquid SDK API connection test successful")
                return True
            else:
                self.logger.error("Failed to get meta information")
                return False
                
        except Exception as e:
            self.logger.error(f"Hyperliquid SDK API connection test failed: {e}")
            return False
    
    def get_data_summary(self) -> Dict[str, Any]:
        """
        Get summary of available data.
        
        Returns:
            Data summary dictionary
        """
        return {
            'market_data_cache_size': len(self.market_data),
            'symbols': list(self.market_data.keys()),
            'timestamp': datetime.now().isoformat(),
        }
    
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
    
    def is_data_available(self, symbol: str) -> bool:
        """
        Check if sufficient data is available for a symbol.
        
        Args:
            symbol: Trading symbol
            
        Returns:
            True if sufficient data is available
        """
        # For now, consider data available if we can get current price
        return self.get_current_price(symbol) is not None 