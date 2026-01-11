
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
import pandas as pd
import math
from src.api.interface import MarketInterface

class MockOhlcvCache:
    """Mock OHLCV Cache matching the real API structure."""
    def __init__(self):
        from collections import defaultdict
        from collections import defaultdict
        self.cache = defaultdict(dict)
    
    def ensure_timeframe(self, symbol: str, timeframe: str, maxlen: int):
        """Mock implementation of ensure_timeframe."""
        if timeframe not in self.cache[symbol]:
            from collections import deque
            self.cache[symbol][timeframe] = deque(maxlen=maxlen)

class MockMarketAPI(MarketInterface):
    """
    Mock implementation of HyperliquidAPI for backtesting.
    Intercepts API calls and serves historical data/simulated execution.
    """
    
    def __init__(self, config: Dict[str, Any], historical_data: Dict[str, Dict[str, pd.DataFrame]]):
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # Simulation State
        self.current_time = None  # Updated by BacktestEngine
        # Structure: {symbol: {timeframe: DataFrame}}
        self.historical_data = historical_data
        # Structure: {symbol: DataFrame(index=timestamp, columns=[funding])}
        self.historical_funding = {}
        
        # Trading State
        self.orders = {}  # order_id -> order_dict
        self.positions = {}  # symbol -> position_dict
        self.fills = [] # List of executed trades/fills
        self.balances = {'USDC': 0.0} # Spot wallet (Zero Equity for Backtest Parity)
        
        # NOTE: Removed pre-seeding of 1000 units per asset to ensure clean equity tracking.
        # Strategies must buy spot with cash or we must explicitly seed if needed for specific tests.
                
        self.perp_balance = {'withdrawable': 50000.0, 'margin_used': 0.0} # Perp wallet
        
        self.order_id_counter = 0
        
        # WebSocket Subscription State (Mock)
        self._subscribed_symbols = set()
        
        # Cache for strategy checks (Match real API structure)
        self.ohlcv_cache = MockOhlcvCache()
        
        # Initialize current_time to the earliest data point to allow pair selection before run()
        # This prevents "Simulation time not set" errors during StrategyManager init
        self._initialize_default_time()

    def reset_state(self):
        """Reset the mock exchange state for a fresh backtest."""
        self.orders = {}
        self.positions = {}
        
        # Get initial balances from config
        # Note: config structure is nested: config['backtesting']['initial_capital']
        backtest_config = self.config.get('backtesting', {})
        initial_perp = float(backtest_config.get('initial_capital', 50000.0))
        initial_spot = float(backtest_config.get('initial_spot_balance', 0.0))
        
        self.balances = {'USDC': initial_spot}
        self.perp_balance = {'withdrawable': initial_perp, 'margin_used': 0.0}
        
        self.order_id_counter = 0
        self._subscribed_symbols = set()
        self.logger.info(f"MockMarketAPI state reset (Perp: ${initial_perp:,.2f}, Spot: ${initial_spot:,.2f})")

    def get_spot_token_for_perp(self, symbol: str) -> Optional[str]:
        """Mock mapping from perp to spot token."""
        # Simple identity mapping for mock
        return symbol

    def get_spot_price(self, token: str, quote: str = 'USDC') -> Optional[float]:
        """Get spot price for token/quote."""
        # Construct expected spot symbol key in historical data
        spot_symbol = f"{token}_SPOT"
        return self.get_current_price(spot_symbol)
        
    def set_time(self, timestamp: datetime):
        """Update the simulation time."""
        self.current_time = timestamp
    
    def _initialize_default_time(self):
        """
        Initialize current_time to a sensible default based on historical data.
        This allows get_current_price and get_asset_info to work before run() starts.
        """
        earliest_time = None
        for symbol, tf_data in self.historical_data.items():
            for tf, df in tf_data.items():
                if df is not None and len(df) > 0:
                    # Use the second-to-last timestamp to ensure we have "prior" data
                    if len(df) > 1:
                        candidate = df.index[-2]
                    else:
                        candidate = df.index[-1]
                    if earliest_time is None or candidate > earliest_time:
                        earliest_time = candidate
        
        if earliest_time is not None:
            self.current_time = earliest_time
            self.logger.info(f"MockMarketAPI: Initialized default time to {self.current_time}")
        
    def _resolve_symbol(self, symbol: str) -> str:
        """
        Resolve symbol alias (e.g. BTC/USDC -> BTC_SPOT).
        Backwards compatible with normal symbols.
        """
        if symbol.endswith('/USDC'):
            # Check for _SPOT version
            base = symbol.split('/')[0]
            spot_sym = f"{base}_SPOT"
            if spot_sym in self.historical_data:
                return spot_sym
        return symbol

    def get_current_price(self, symbol: str) -> Optional[float]:
        """Get 'current' price from historical data (uses 1h data by default)."""
        if not self.current_time:
            raise ValueError("Simulation time not set")
            
        # Resolve symbol (handle Spot mapping)
        target_symbol = self._resolve_symbol(symbol)
            
        # Default to 1h for price check, fallback to any available
        symbol_data = self.historical_data.get(target_symbol)
        if not symbol_data:
            # Only warn if it's not a spot mapping that might not exist
            if not symbol.endswith('/USDC'):
                self.logger.warning(f"No historical data for {target_symbol}")
            return None
            
        # Try 1h first, then others
        df = symbol_data.get('1h')
        if df is None:
            # Fallback to first available timeframe
            if not symbol_data: # Check again in case symbol_data was empty
                self.logger.warning(f"No historical data for {symbol} in any timeframe.")
                return None
            df = next(iter(symbol_data.values()))
            
        # Find row at or before current_time
        # Assumes df is indexed by datetime
        try:
            # Efficient lookup: Binary search for index
            # side='left' returns index i such that all df.index[:i] < self.current_time
            idx = df.index.searchsorted(self.current_time, side='left')
            
            if idx == 0:
                return None
                
            # The row strictly before current_time is at idx - 1
            last_valid_idx = idx - 1
            return float(df.iloc[last_valid_idx]['close'])
        except Exception as e:
            self.logger.error(f"Error getting price for {symbol}: {e}")
            return None

    def get_ohlcv(self, symbol: str, timeframe: str = '1h', limit: int = 100) -> Optional[pd.DataFrame]:
        """Get OHLCV history up to current_time."""
        if not self.current_time:
            return None
        
        # Resolve symbol
        target_symbol = self._resolve_symbol(symbol)
        
        symbol_data = self.historical_data.get(target_symbol)
        if not symbol_data:
            return None
            
        # Get specific timeframe
        df = symbol_data.get(timeframe)
        if df is None:
            # If requested timeframe missing, maybe warn? using None for now
            return None
            
        # Filter data strictly before current_time to avoid look-ahead bias
        # Optimization: Use binary search for slicing instead of boolean mask O(N)
        
        # Get insertion index for current_time
        idx = df.index.searchsorted(self.current_time, side='left')
        
        # We want everything before this index
        if idx == 0:
            self.logger.debug(f"get_ohlcv: Current time {self.current_time} before data start {df.index[0]}")
            return None
            
        # Slice the last 'limit' rows up to 'idx'
        start_idx = max(0, idx - limit)
        filtered_df = df.iloc[start_idx:idx]
        
        self.logger.debug(f"get_ohlcv sliced: {len(filtered_df)} rows. Range: {filtered_df.index[0]} to {filtered_df.index[-1]}")
        
        if filtered_df.empty:
            return None
            
        # Update cache to satisfy StrategyManager check
        # StrategyManager expects .cache[symbol][timeframe]
        self.ohlcv_cache.cache[symbol][timeframe] = filtered_df
            
        return filtered_df
        
    def get_spot_balance(self, asset: str) -> float:
        return self.balances.get(asset, 0.0)
        
    def get_perp_balance(self) -> Dict[str, float]:
        return self.perp_balance
        
    def ensure_perp_funds(self, amount: float) -> bool:
        """Ensure perp account has at least 'amount' withdrawable."""
        current_balance = self.perp_balance['withdrawable']
        if current_balance >= amount:
            return True
            
        deficit = amount - current_balance
        # Try to transfer deficit from spot
        if self.balances['USDC'] >= deficit:
            self.balances['USDC'] -= deficit
            self.perp_balance['withdrawable'] += deficit
            return True
        return False
        
    def ensure_spot_funds(self, amount: float) -> bool:
        """Ensure spot account has at least 'amount' USDC."""
        current_balance = self.balances.get('USDC', 0.0)
        if current_balance >= amount:
            return True
            
        deficit = amount - current_balance
        # Try to transfer deficit from perp
        if self.perp_balance['withdrawable'] >= deficit:
            self.perp_balance['withdrawable'] -= deficit
            self.balances['USDC'] = self.balances.get('USDC', 0.0) + deficit
            return True
        return False

    def execute_order(self, symbol: str, side: str, size: float, 
                     reduce_only: bool = False, market_type: str = 'perp', 
                     urgency: str = 'normal', limit_price: float = None,
                     max_slippage_bps: int = None) -> Dict[str, Any]:
        """Simulate order execution."""
        
        # CRITICAL: Normalize size to magnitude. Logic below depends on positive size.
        size = abs(size)
        
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
                # CRITICAL: Strict check preventing naked shorting
                if self.balances.get(base_asset, 0) < size:
                     self.logger.warning(f"REJECTED Spot Sell {symbol}: Balance {self.balances.get(base_asset, 0)} < Size {size}")
                     return {'status': 'rejected', 'reason': 'Insufficient Asset'}
                self.balances[base_asset] -= size
                self.balances['USDC'] += cost
                
        elif market_type == 'perp':
            # Simple margin check
            margin_required = cost / 50.0  # Assuming 50x max leverage for check (generous but safe)
            # Use 'available' margin logic. In mock, 'withdrawable' is essentially free margin.
            # But we must check against total account value for cross margin, or withdrawable for isolated?
            # Keeping it simple: If available balance < req margin, reject.
            if self.perp_balance['withdrawable'] < margin_required and not reduce_only:
                 self.logger.warning(f"REJECTED Perp Order {symbol}: Rec Margin ${margin_required:.2f} > Avail ${self.perp_balance['withdrawable']:.2f}")
                 return {'status': 'rejected', 'reason': 'Insufficient Margin'} 

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
                
            # Calculate Realized PnL if reducing position
            # Simply: (Exit Price - Entry Price) * CLOSED_SIZE * DIRECTION
            # Direction: 1 for Long, -1 for Short
            
            # Determine if we are closing/reducing
            if current_pos['size'] != 0:
                # Check if we are reducing (same side but smaller magnitude, or flipping)
                # For simplicity in mock: treat any 'fill' as closing the existing portion first
                
                # Logic:
                # If Long (size > 0) and Selling (side=sell): Realize PnL
                # If Short (size < 0) and Buying (side=buy): Realize PnL
                
                is_long = current_pos['size'] > 0
                is_closing = (is_long and side.lower() == 'sell') or (not is_long and side.lower() == 'buy')
                
                if is_closing:
                    # Amount closed is min(abs(current), size)
                    closed_qty = min(abs(current_pos['size']), size)
                    
                    entry_price = current_pos['entry_price']
                    price_diff = fill_price - entry_price
                    
                    # PnL = Diff * Qty * Direction
                    # If Long, Direction = 1. If Short, Direction = -1.
                    direction = 1 if is_long else -1
                    pnl = price_diff * closed_qty * direction
                    
                    self.perp_balance['withdrawable'] += pnl
                    # Return margin (simplified: assume margin was size/leverage * price)
                    # We don't track isolated margin per pos in this simple mock perfectly, 
                    # but we should release some 'margin_used' if we were tracking it.
                    # For now, equity update via PnL is most important.
                    
            self.positions[pos_key] = {
                'symbol': symbol,
                'size': new_size,
                'entry_price': fill_price if current_pos['size'] == 0 else current_pos['entry_price'], # Keep old entry if not fully closed/flipped? 
                # Simplified: If flip, new entry is fill_price. If partial close, keep old entry.
                # Just using fill_price for new positions or weighted avg could be better but complex.
                # Let's keep simple: If direction changes (sign flip), take new price. Else keep old.
                'side': 'long' if new_size > 0 else 'short' if new_size < 0 else 'neutral',
                'mark_price': price
            }
            
            # Fix Entry Price logic for partials vs flips vs adds
            if current_pos['size'] != 0 and new_size != 0:
                # Check if adding to position (Same Sign and Magnitude Increased)
                is_same_side = (current_pos['size'] * new_size > 0)
                is_increasing = abs(new_size) > abs(current_pos['size'])
                
                if is_same_side and is_increasing:
                    # Calculate Weighted Average Entry Price
                    old_size_abs = abs(current_pos['size'])
                    added_size_abs = abs(new_size) - old_size_abs
                    old_entry = current_pos['entry_price']
                    
                    # WAVG = ((Old * OldPrice) + (Added * NewPrice)) / Total
                    total_value = (old_size_abs * old_entry) + (added_size_abs * fill_price)
                    wavg_price = total_value / abs(new_size)
                    
                    self.positions[pos_key]['entry_price'] = wavg_price
                    
                elif is_same_side:
                    # Reducing position (Partial Close) -> Keep Entry Price
                    self.positions[pos_key]['entry_price'] = current_pos['entry_price']
                else:
                    # Flip (Sign Change) -> Remaining part is new position at fill price
                    self.positions[pos_key]['entry_price'] = fill_price
                    
            elif current_pos['size'] == 0 and new_size != 0:
                 self.positions[pos_key]['entry_price'] = fill_price
            
        # Calculate Fee (0.05% Taker assumed for simplicity)
        fee_rate = 0.0005
        fee = cost * fee_rate
        
        # Deduct fee from USDC/Balances
        if market_type == 'spot':
            self.balances['USDC'] -= fee
        elif market_type == 'perp':
            self.perp_balance['withdrawable'] -= fee

        self.order_id_counter += 1
        order_id = f"mock_{self.order_id_counter}"
        
        result = {
            'status': 'filled',
            'filled_size': size,
            'avg_fill_price': fill_price,
            'order_id': order_id,
            'symbol': symbol,
            'side': side,
            'timestamp': self.current_time,
            'fee': fee,
            'total_fee': fee  # Alias
        }
        
        self.orders[order_id] = result
        
        # Record fill
        fill_record = {
            'coin': symbol,
            'side': 'Buy' if side.lower() in ('buy', 'long') else 'Sell', # Match API format (Buy/Sell)
            'px': fill_price,
            'sz': size,
            'time': int(self.current_time.timestamp() * 1000) if self.current_time else int(datetime.now().timestamp() * 1000),
            'oid': order_id,
            'dir': f"Open {side.capitalize()}" if result.get('status') == 'filled' else "Close", # Simplified dir
            'fee': fee, # ADDED: Fee for PnL calculations
            'feeToken': 'USDC' # ADDED: Token for fee
        }
        self.fills.append(fill_record)
        
        return result

    def get_execution_fee(self, order_id: Any) -> float:
        """Get execution fee for a specific order."""
        order = self.orders.get(order_id)
        if order:
            return order.get('fee', 0.0)
        return 0.0

    def get_positions(self) -> List[Dict[str, Any]]:
        """Return list of current positions."""
        result = []
        for symbol, pos in self.positions.items():
            if pos['size'] != 0:
                result.append(pos)
        return result

    def get_position(self, symbol: str) -> Dict[str, Any]:
        """Get current position for a specific symbol."""
        pos = self.positions.get(symbol, {})
        if not pos or pos.get('size', 0) == 0:
             return {'symbol': symbol, 'size': 0.0, 'side': 'neutral', 'entry_price': 0.0, 'unrealized_pnl': 0.0}
        return pos

    def get_user_fills(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get recent fills/trades."""
        # Return last N fills, sorted by time desc (usually API returns newest first)
        return sorted(self.fills, key=lambda x: x['time'], reverse=True)[:limit]
        
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

    def get_asset_info(self) -> Optional[Dict[str, Any]]:
        """
        Get mock asset info.
        For verification tests, we scan the mock filesystem/cache AND provide some synthetic defaults.
        """
        universe = []
        
        # 1. Native Assets (Defaults)
        for sym in ["BTC", "ETH", "SOL", "HBAR", "USDC"]:
            universe.append({
                'name': sym,
                'szDecimals': 2,
                'maxLeverage': 50,
                'dex': 'Native'
            })
            
        # 2. HIP-3 Assets (Synthetic for tests)
        # Any symbol starting with a lowercase letter is theoretically HIP-3
        # But we can just add a specific placeholder for tests
        universe.append({
            'name': 'xyzABCD',
            'szDecimals': 4,
            'maxLeverage': 20,
            'dex': 'Hyperliquidity',
            'assetId': 12345
        })

        return {'universe': universe, 'meta': {'universe': universe}}
        
    def subscribe_symbol(self, symbol):
        """Mock subscription."""
        self._subscribed_symbols.add(symbol)
        
        # Parity with HyperliquidAPI: Initialize standard timeframes
        standard_timeframes = ['5m', '15m', '1h', '4h', '1d']
        for tf in standard_timeframes:
            self.ohlcv_cache.ensure_timeframe(symbol, tf, maxlen=1000)
        
    def unsubscribe_symbol(self, symbol):
        """Mock unsubscription."""
        self._subscribed_symbols.discard(symbol)

    def is_data_available(self, symbol: str) -> bool:
        """Check if data is available for symbol."""
        # In backtest, we assume data availability if symbol is in historical data
        return self._resolve_symbol(symbol) in self.historical_data
        
    def get_account_balance(self) -> Dict[str, Any]:
        """Mock account balance."""
        # Calculate Spot Wallet Value (Cash + Assets)
        spot_value = self.balances.get('USDC', 0.0)
        
        for asset, amount in self.balances.items():
            if asset == 'USDC' or amount == 0:
                continue
            
            # Find price for asset
            # Try asset_SPOT first, then just asset
            price = self.get_current_price(f"{asset}_SPOT")
            if not price:
                price = self.get_current_price(asset)
            
            if price:
                spot_value += amount * price

        # Calculate Unrealized PnL (Perps)
        unrealized_pnl = 0.0
        for p in self.positions.values():
            if p['size'] == 0: continue
            
            # Update mark price if possible
            current_price = self.get_current_price(p['symbol'])
            if current_price:
                p['mark_price'] = current_price
            
            diff = p['mark_price'] - p['entry_price']
            # Long: profit if Mark > Entry (Diff > 0)
            # Short: profit if Mark < Entry (Diff < 0) -> but Size is negative?
            # Our positions storage uses signed size? 
            # In execute_order: new_size -= size (for sell). So yes, signed.
            # PnL = (Price - Entry) * Size?
            # Long: (110 - 100) * 1 = 10. Correct.
            # Short: (90 - 100) * -1 = -10 * -1 = 10. Correct.
            unrealized_pnl += diff * p['size']

        equity = spot_value + self.perp_balance['withdrawable'] + unrealized_pnl
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
            'funding_rate': self.get_funding_rate(symbol) or 0.0,
            'volume_24h': 10000000.0
        }

    def get_asset_meta(self, symbol: str) -> Dict[str, Any]:
        """Mock asset metadata with realistic leverage limits."""
        # Determine leverage based on asset class
        max_leverage = 20 # Default Altcoin
        
        base = symbol.split('/')[0].split('_')[0]
        
        majors = {'BTC', 'ETH'}
        memes = {'BONK', 'PEPE', 'TRUMP', 'SHIB', 'DOGE', 'WIF', 'kBONK', 'kPEPE'}
        
        if base in majors:
            max_leverage = 50
        elif base in memes:
            max_leverage = 5
            
        return {
            'maxLeverage': max_leverage,
            'szDecimals': 4,
            'onlyIsolated': False,
            'name': symbol
        }

    def update_leverage(self, symbol: str, leverage: int, is_cross: bool = True) -> Dict[str, Any]:
        """Mock update leverage."""
        return {'status': 'ok'}

    def set_funding_data(self, funding_data: Dict[str, pd.DataFrame]):
        """Inject historical funding rates."""
        self.historical_funding = funding_data

    def get_funding_rate(self, symbol: str) -> Optional[float]:
        """Get current funding rate from history."""
        if not self.current_time:
            return None
            
        df = self.historical_funding.get(symbol)
        if df is None:
            # Fallback for symbols without funding history (like Spot?)
            return 0.0
            
        # Find row at or before current_time
        try:
            # Funding is hourly, so look for nearest prior value strictly before current time
            # For funding, the timestamp usually represents when it is paid/known.
            # So we should be able to see the value AT current_time.
            mask = df.index <= self.current_time
            if not mask.any():
                return 0.0
            last_valid_idx = df.index[mask][-1]
            return float(df.loc[last_valid_idx]['funding_rate'])
        except Exception as e:
            # self.logger.error(f"Error getting funding for {symbol}: {e}")
            return 0.0

    def get_funding_arb_eligible_symbols(self) -> List[Dict[str, Any]]:
        """
        Get list of symbols eligible for funding arbitrage (have both perp and spot).
        For backtest, we check if we have data for both.
        """
        eligible = []
        # Find all keys in historical_data that end with _SPOT
        spot_symbols = {k for k in self.historical_data.keys() if k.endswith('_SPOT')}
        
        for spot_sym in spot_symbols:
            base_perp = spot_sym.replace('_SPOT', '')
            if base_perp in self.historical_data:
                # We have both
                eligible.append({
                    'symbol': base_perp,
                    'spot_symbol': f"{base_perp}/USDC", # Strategy expects this format
                    'funding_rate': self.get_funding_rate(base_perp) or 0.0
                })
        return eligible
