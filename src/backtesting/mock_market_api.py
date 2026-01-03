
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
import pandas as pd
import math

class MockMarketAPI:
    """
    Mock implementation of HyperliquidAPI for backtesting.
    Intercepts API calls and serves historical data/simulated execution.
    """
    
    def __init__(self, config: Dict[str, Any], historical_data: Dict[str, pd.DataFrame]):
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # Simulation State
        self.current_time = None  # Updated by BacktestEngine
        self.historical_data = historical_data
        
        # Trading State
        self.orders = {}  # order_id -> order_dict
        self.positions = {}  # symbol -> position_dict
        self.balances = {'USDC': 10000.0, 'ETH': 0.0} # Spot wallet
        self.perp_balance = {'withdrawable': 50000.0, 'margin_used': 0.0} # Perp wallet
        
        self.order_id_counter = 0
        
    def set_time(self, timestamp: datetime):
        """Update the simulation time."""
        self.current_time = timestamp
        
    def get_current_price(self, symbol: str) -> float:
        """Get 'current' price from historical data."""
        if not self.current_time:
            raise ValueError("Simulation time not set")
            
        df = self.historical_data.get(symbol)
        if df is None:
            self.logger.warning(f"No historical data for {symbol}")
            return None
            
        # Find row at or before current_time
        # Assumes df is indexed by datetime
        try:
            # Efficient lookup for sorted index
            idx = df.index.get_indexer([self.current_time], method='pad')[0]
            if idx == -1:
                return None
            return float(df.iloc[idx]['close'])
        except Exception as e:
            self.logger.error(f"Error getting price for {symbol}: {e}")
            return None

    def get_ohlcv(self, symbol: str, timeframe: str = '1h', limit: int = 100) -> Optional[pd.DataFrame]:
        """Get OHLCV history up to current_time."""
        if not self.current_time:
            return None
            
        df = self.historical_data.get(symbol)
        if df is None:
            return None
            
        # Filter data up to current_time
        mask = df.index <= self.current_time
        filtered_df = df.loc[mask].tail(limit)
        
        if filtered_df.empty:
            return None
            
        return filtered_df
        
    def get_spot_balance(self, asset: str) -> float:
        return self.balances.get(asset, 0.0)
        
    def get_perp_balance(self) -> Dict[str, float]:
        return self.perp_balance
        
    def ensure_perp_funds(self, amount: float) -> bool:
        # Simplistic transfer mock
        if self.balances['USDC'] >= amount:
            self.balances['USDC'] -= amount
            self.perp_balance['withdrawable'] += amount
            return True
        return False
        
    def ensure_spot_funds(self, amount: float) -> bool:
        # Simplistic transfer mock
        if self.perp_balance['withdrawable'] >= amount:
            self.perp_balance['withdrawable'] -= amount
            self.balances['USDC'] += amount
            return True
        return False

    def execute_order(self, symbol: str, side: str, size: float, 
                     reduce_only: bool = False, market_type: str = 'perp', 
                     urgency: str = 'normal', limit_price: float = None) -> Dict[str, Any]:
        """Simulate order execution."""
        
        price = self.get_current_price(symbol)
        if not price:
            self.logger.error(f"Cannot execute: no price for {symbol}")
            return {'status': 'rejected', 'reason': 'No Price'}
            
        # Slippage simulation (0.05%)
        slippage = 0.0005
        if side.lower() == 'buy':
            fill_price = price * (1 + slippage)
        else:
            fill_price = price * (1 - slippage)
            
        # Checking limits (simplified)
        cost = size * fill_price
        
        if market_type == 'spot':
            if side.lower() == 'buy':
                if self.balances.get('USDC', 0) < cost:
                    return {'status': 'rejected', 'reason': 'Insufficient Funds'}
                self.balances['USDC'] -= cost
                base_asset = symbol.split('/')[0]
                self.balances[base_asset] = self.balances.get(base_asset, 0) + size
            else: # Sell
                base_asset = symbol.split('/')[0]
                if self.balances.get(base_asset, 0) < size:
                     return {'status': 'rejected', 'reason': 'Insufficient Asset'}
                self.balances[base_asset] -= size
                self.balances['USDC'] += cost
                
        elif market_type == 'perp':
            # Simple margin check
            margin_required = cost / 3.0 # Assuming 3x leverage for check
            if self.perp_balance['withdrawable'] < margin_required and not reduce_only:
                 # In reality, this is complex (cross margin etc), keeping simple for mock
                 pass 

            # Update position tracking
            pos_key = symbol
            current_pos = self.positions.get(pos_key, {'size': 0.0, 'entry_price': 0.0, 'side': 'neutral'})
            
            new_size = current_pos['size']
            if side.lower() == 'buy':
                new_size += size
            else:
                new_size -= size
                
            # Update avg entry (simplified)
            if new_size != 0:
                # If increasing position or flipping side, update entry price logic would go here
                # Keeping it simple: if side matches, w-avg. If flip, reset.
                pass
                
            self.positions[pos_key] = {
                'symbol': symbol,
                'size': new_size,
                'entry_price': fill_price, # Simplified
                'side': 'long' if new_size > 0 else 'short' if new_size < 0 else 'neutral',
                'mark_price': price
            }
            
        self.order_id_counter += 1
        return {
            'status': 'filled',
            'filled_size': size,
            'avg_fill_price': fill_price,
            'order_id': f"mock_{self.order_id_counter}"
        }

    def get_positions(self) -> List[Dict[str, Any]]:
        return [
            {
                'symbol': p['symbol'], 
                'size': abs(p['size']), 
                'side': 'buy' if p['size'] > 0 else 'sell',
                'entry_price': p['entry_price'],
                'mark_price': self.get_current_price(p['symbol']) or p['entry_price']
            }
            for p in self.positions.values() if p['size'] != 0
        ]
        
    def check_liquidation_risk(self, symbol, threshold_pct=0.0):
        # Mock safe
        return {'at_risk': False}

    def get_open_orders(self) -> List[Dict[str, Any]]:
        """Return list of open orders."""
        # For now, backtest assumes immediate fills or rejections, so no open orders.
        return []

    def get_user_state(self) -> Dict[str, Any]:
        """Mock get_user_state for PairSelector validation."""
        return {
             'assetPositions': [],
             'marginSummary': {
                 'accountValue': self.balances['USDC'] + self.perp_balance['withdrawable'],
                 'totalMarginUsed': self.perp_balance['margin_used'],
                 'totalNtlPos': 0.0,
                 'withdrawable': self.perp_balance['withdrawable']
             },
             'crossMarginSummary': {
                 'accountValue': self.balances['USDC'] + self.perp_balance['withdrawable'],
                 'totalMarginUsed': self.perp_balance['margin_used'],
                 'totalNtlPos': 0.0,
                 'withdrawable': self.perp_balance['withdrawable']
             }
        }

    def get_asset_info(self) -> Dict[str, Any]:
        """Mock asset info."""
        universe = []
        for symbol in self.historical_data.keys():
            # Get current price
            price = self.get_current_price(symbol) or 100.0
            
            # Basic asset info structure with market data
            universe.append({
                'name': symbol,
                'szDecimals': 4,
                'maxLeverage': 50,
                'onlyIsolated': False,
                # Market stats for selector
                'openInterest': 10_000_000.0, # High OI
                'volume24h': 100_000_000.0,   # High volume
                'markPrice': price,
                'bid': price - 0.05,
                'ask': price + 0.05,
                'fundingRate': 0.0001
            })
        return {'universe': universe}
        
    def subscribe_symbol(self, symbol):
        pass

    def is_data_available(self, symbol: str) -> bool:
        """Check if data is available for symbol."""
        # In backtest, we assume data availability if symbol is in historical data
        return symbol in self.historical_data
        
    def get_account_balance(self) -> Dict[str, Any]:
        """Mock account balance."""
        equity = self.balances['USDC'] + self.perp_balance['withdrawable']
        return {
             'total_equity': equity,
             'free_margin': self.perp_balance['withdrawable'],
             'used_margin': self.perp_balance['margin_used'],
             'withdrawable': self.perp_balance['withdrawable'],
             'marginSummary': {
                 'accountValue': equity,
                 'totalMarginUsed': self.perp_balance['margin_used'],
                 'totalNtlPos': 0.0,
                 'withdrawable': self.perp_balance['withdrawable']
             }
        }
        
    def get_market_data(self, symbol: str) -> Dict[str, Any]:
        """Mock specific market data for symbol."""
        price = self.get_current_price(symbol)
        if price is None:
            return None
        return {
            'symbol': symbol,
            'current_price': price,
            'mark_price': price,
            'index_price': price,
            'funding_rate': 0.0001,
            'open_interest': 1000000.0,
            'volume_24h': 10000000.0
        }
