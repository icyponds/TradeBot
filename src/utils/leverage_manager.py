"""
Dynamic leverage management for trading strategies.
"""

import logging
from typing import Dict, Any, Optional, Tuple
from datetime import datetime
import math


class LeverageManager:
    """Manages dynamic leverage based on strategy and trade conditions."""
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the leverage manager.
        
        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # Risk management configuration
        self.margin_buffer = config['risk_management']['margin_buffer_percentage'] / 100
        self.liquidation_threshold = config['risk_management']['liquidation_risk_threshold'] / 100
        
        # Trading configuration
        self.max_position_size = config['trading']['max_position_size']
        self.risk_percentage = config['trading']['risk_percentage'] / 100
        self.stop_loss_percentage = config['trading']['stop_loss_percentage'] / 100
        
        # Position tracking
        self.open_positions = {}  # symbol -> position info
        self.total_margin_used = 0.0
        self.available_margin = 0.0
        
        self.logger.info("Initialized dynamic leverage manager")
    
    def calculate_dynamic_leverage(self, symbol: str, strategy_name: str, signal_strength: float, 
                                 market_volatility: float, current_price: float) -> float:
        """
        Calculate dynamic leverage based on strategy and market conditions.
        
        Args:
            symbol: Trading symbol
            strategy_name: Name of the strategy
            signal_strength: Signal strength (0-1)
            market_volatility: Market volatility measure
            current_price: Current asset price
            
        Returns:
            Dynamic leverage value
        """
        # Base leverage ranges for different strategies
        strategy_leverage_ranges = {
            'moving_average': (8, 15),  # Conservative for trend following
            'rsi': (12, 20),            # Aggressive for mean reversion
            'scalping': (15, 25),       # Very aggressive for scalping
            'default': (10, 18),        # Default range
        }
        
        # Get leverage range for strategy
        min_leverage, max_leverage = strategy_leverage_ranges.get(strategy_name, strategy_leverage_ranges['default'])
        
        # Adjust leverage based on signal strength
        # Stronger signals get higher leverage
        signal_multiplier = 0.5 + (signal_strength * 0.5)  # 0.5 to 1.0
        
        # Adjust leverage based on volatility
        # Lower volatility = higher leverage, higher volatility = lower leverage
        volatility_multiplier = max(0.3, 1.0 - market_volatility)  # 0.3 to 1.0
        
        # Calculate base leverage
        base_leverage = min_leverage + (max_leverage - min_leverage) * signal_multiplier
        
        # Apply volatility adjustment
        dynamic_leverage = base_leverage * volatility_multiplier
        
        # Ensure leverage is within reasonable bounds
        dynamic_leverage = max(5, min(25, dynamic_leverage))
        
        self.logger.info(f"Dynamic leverage for {symbol} ({strategy_name}): {dynamic_leverage:.1f}x "
                        f"(signal: {signal_strength:.2f}, volatility: {market_volatility:.2f})")
        
        return dynamic_leverage
    
    def calculate_leveraged_position_size(self, symbol: str, current_price: float, available_capital: float,
                                        strategy_name: str, signal_strength: float, market_volatility: float) -> Tuple[float, float, float]:
        """
        Calculate leveraged position size with dynamic leverage.
        
        Args:
            symbol: Trading symbol
            current_price: Current asset price
            available_capital: Available capital for trading
            strategy_name: Name of the strategy
            signal_strength: Signal strength (0-1)
            market_volatility: Market volatility measure
            
        Returns:
            Tuple of (position_size, margin_required, leverage_used)
        """
        # Calculate dynamic leverage
        leverage = self.calculate_dynamic_leverage(symbol, strategy_name, signal_strength, market_volatility, current_price)
        
        # Calculate risk amount (2% of capital)
        risk_amount = available_capital * self.risk_percentage
        
        # Calculate maximum position value with leverage
        max_position_value = available_capital * leverage
        
        # Calculate position size based on risk and leverage
        # For aggressive scalping, we can risk more due to tight stops
        position_value = min(risk_amount * leverage * 2, max_position_value)  # 2x risk for aggressive scalping
        
        # Calculate position size in units
        position_size = position_value / current_price
        
        # Calculate margin required
        margin_required = position_value / leverage
        
        # Apply margin buffer
        margin_required *= (1 + self.margin_buffer)
        
        # Ensure we don't exceed max position size
        if position_value > self.max_position_size:
            position_value = self.max_position_size
            position_size = position_value / current_price
            margin_required = position_value / leverage * (1 + self.margin_buffer)
        
        return position_size, margin_required, leverage
    
    def calculate_stop_loss_with_leverage(self, entry_price: float, side: str, leverage: float) -> float:
        """
        Calculate stop loss price considering leverage.
        
        Args:
            entry_price: Entry price
            side: 'long' or 'short'
            leverage: Leverage used
            
        Returns:
            Stop loss price
        """
        # Tighter stops for higher leverage
        base_stop_percentage = self.stop_loss_percentage
        leverage_adjusted_stop = base_stop_percentage / math.sqrt(leverage / 10)  # Adjust for leverage
        
        if side == 'long':
            return entry_price * (1 - leverage_adjusted_stop)
        else:
            return entry_price * (1 + leverage_adjusted_stop)
    
    def calculate_take_profit_with_leverage(self, entry_price: float, side: str, leverage: float, 
                                         strategy_take_profit: float = None) -> float:
        """
        Calculate take profit price considering leverage and strategy-specific logic.
        
        Args:
            entry_price: Entry price
            side: 'long' or 'short'
            leverage: Leverage used
            strategy_take_profit: Strategy-specific take profit price (if provided)
            
        Returns:
            Take profit price
        """
        if strategy_take_profit is not None:
            # Use strategy-specific take profit
            return strategy_take_profit
        
        # Fallback to leverage-adjusted calculation
        # Higher leverage = higher profit targets
        base_tp_percentage = 0.06  # 6% default (removed from config)
        leverage_adjusted_tp = base_tp_percentage * math.sqrt(leverage / 10)
        
        if side == 'long':
            return entry_price * (1 + leverage_adjusted_tp)
        else:
            return entry_price * (1 - leverage_adjusted_tp)
    
    def check_liquidation_risk(self, symbol: str, current_price: float, position_size: float, entry_price: float, side: str) -> Tuple[bool, float]:
        """
        Check liquidation risk for a position.
        
        Args:
            symbol: Trading symbol
            current_price: Current price
            position_size: Position size
            entry_price: Entry price
            side: 'long' or 'short'
            
        Returns:
            Tuple of (is_at_risk, risk_percentage)
        """
        # Get leverage from position info
        if symbol not in self.open_positions:
            return False, 0.0
        
        leverage = self.open_positions[symbol]['leverage']
        
        # Calculate unrealized PnL
        if side == 'long':
            pnl_percentage = (current_price - entry_price) / entry_price
        else:
            pnl_percentage = (entry_price - current_price) / entry_price
        
        # Calculate liquidation price (approximate)
        # For long positions: liquidation_price = entry_price * (1 - 1/leverage)
        # For short positions: liquidation_price = entry_price * (1 + 1/leverage)
        if side == 'long':
            liquidation_price = entry_price * (1 - 1/leverage)
            distance_to_liquidation = (current_price - liquidation_price) / entry_price
        else:
            liquidation_price = entry_price * (1 + 1/leverage)
            distance_to_liquidation = (liquidation_price - current_price) / entry_price
        
        # Calculate risk percentage
        risk_percentage = (1 - distance_to_liquidation) * 100
        
        # Check if at risk
        is_at_risk = risk_percentage > (self.liquidation_threshold * 100)
        
        return is_at_risk, risk_percentage
    
    def can_open_position(self, symbol: str, margin_required: float, available_capital: float) -> bool:
        """
        Check if we can open a new position.
        
        Args:
            symbol: Trading symbol
            margin_required: Margin required for the position
            available_capital: Available capital
            
        Returns:
            True if position can be opened
        """
        # Check if we have enough margin
        if margin_required > available_capital:
            self.logger.warning(f"Insufficient margin for {symbol}: {margin_required} > {available_capital}")
            return False
        
        # Check total margin usage
        total_margin_after = self.total_margin_used + margin_required
        max_total_margin = available_capital * 0.8  # Use max 80% of capital
        
        if total_margin_after > max_total_margin:
            self.logger.warning(f"Total margin usage too high: {total_margin_after} > {max_total_margin}")
            return False
        
        return True
    
    def record_position(self, symbol: str, side: str, size: float, entry_price: float, leverage: float, margin_used: float):
        """
        Record a new position.
        
        Args:
            symbol: Trading symbol
            side: Position side
            size: Position size
            entry_price: Entry price
            leverage: Leverage used
            margin_used: Margin used
        """
        self.open_positions[symbol] = {
            'side': side,
            'size': size,
            'entry_price': entry_price,
            'leverage': leverage,
            'margin_used': margin_used,
            'entry_time': datetime.now(),
        }
        
        self.total_margin_used += margin_used
        self.available_margin -= margin_used
        
        self.logger.info(f"Recorded {side} position for {symbol}: {size} @ {entry_price} with {leverage:.1f}x leverage")
    
    def close_position(self, symbol: str, exit_price: float) -> Optional[Dict[str, Any]]:
        """
        Close a position and calculate PnL.
        
        Args:
            symbol: Trading symbol
            exit_price: Exit price
            
        Returns:
            Position result dictionary or None if position not found
        """
        if symbol not in self.open_positions:
            return None
        
        position = self.open_positions[symbol]
        
        # Calculate PnL
        if position['side'] == 'long':
            pnl = (exit_price - position['entry_price']) * position['size']
            pnl_percentage = ((exit_price - position['entry_price']) / position['entry_price']) * 100
        else:
            pnl = (position['entry_price'] - exit_price) * position['size']
            pnl_percentage = ((position['entry_price'] - exit_price) / position['entry_price']) * 100
        
        # Calculate leveraged PnL
        leveraged_pnl = pnl * position['leverage']
        leveraged_pnl_percentage = pnl_percentage * position['leverage']
        
        # Update margin
        self.total_margin_used -= position['margin_used']
        self.available_margin += position['margin_used']
        
        # Remove position
        del self.open_positions[symbol]
        
        result = {
            'symbol': symbol,
            'side': position['side'],
            'entry_price': position['entry_price'],
            'exit_price': exit_price,
            'size': position['size'],
            'leverage': position['leverage'],
            'pnl': pnl,
            'pnl_percentage': pnl_percentage,
            'leveraged_pnl': leveraged_pnl,
            'leveraged_pnl_percentage': leveraged_pnl_percentage,
            'duration': (datetime.now() - position['entry_time']).total_seconds(),
        }
        
        self.logger.info(f"Closed position for {symbol}: {leveraged_pnl:.2f} USDC ({leveraged_pnl_percentage:.2f}%)")
        
        return result
    
    def get_margin_summary(self) -> Dict[str, Any]:
        """
        Get margin usage summary.
        
        Returns:
            Margin summary dictionary
        """
        return {
            'total_margin_used': self.total_margin_used,
            'available_margin': self.available_margin,
            'margin_utilization': (self.total_margin_used / (self.total_margin_used + self.available_margin)) * 100 if (self.total_margin_used + self.available_margin) > 0 else 0,
            'open_positions': len(self.open_positions),
            'positions': list(self.open_positions.keys()),
        }
    
    def update_available_margin(self, available_capital: float):
        """
        Update available margin.
        
        Args:
            available_capital: Available capital
        """
        self.available_margin = available_capital - self.total_margin_used
    
    def get_risk_summary(self) -> Dict[str, Any]:
        """
        Get risk summary for all open positions.
        
        Returns:
            Risk summary dictionary
        """
        risk_summary = {
            'high_risk_positions': [],
            'total_risk_score': 0,
            'positions_at_risk': 0,
        }
        
        for symbol, position in self.open_positions.items():
            # This would need current price from market data
            # For now, return basic position info
            risk_summary['total_risk_score'] += position['leverage']
            
            if position['leverage'] > 15:  # High leverage positions
                risk_summary['high_risk_positions'].append(symbol)
                risk_summary['positions_at_risk'] += 1
        
        return risk_summary 