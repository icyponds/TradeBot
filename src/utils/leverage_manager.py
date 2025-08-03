"""
Simplified leverage management for trading strategies.
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
        
        self.logger.info("Initialized simplified leverage manager")
    
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
        signal_multiplier = 0.5 + (signal_strength * 0.5)  # 0.5 to 1.0
        
        # Adjust leverage based on volatility
        volatility_multiplier = max(0.3, 1.0 - market_volatility)  # 0.3 to 1.0
        
        # Calculate base leverage
        base_leverage = min_leverage + (max_leverage - min_leverage) * signal_multiplier
        
        # Apply volatility adjustment
        dynamic_leverage = base_leverage * volatility_multiplier
        
        # Ensure leverage is within reasonable bounds
        dynamic_leverage = max(5, min(25, dynamic_leverage))
        
        self.logger.info(f"Dynamic leverage for {symbol} ({strategy_name}): {dynamic_leverage:.1f}x")
        
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
            Tuple of (position_size, margin_required, leverage)
        """
        # Calculate dynamic leverage
        leverage = self.calculate_dynamic_leverage(symbol, strategy_name, signal_strength, market_volatility, current_price)
        
        # Calculate position size based on risk percentage
        risk_amount = available_capital * self.risk_percentage
        position_value = risk_amount * leverage
        position_size = position_value / current_price
        
        # Calculate margin required
        margin_required = position_value / leverage
        
        # Ensure position size doesn't exceed maximum
        max_position_value = self.max_position_size * leverage
        max_position_size = max_position_value / current_price
        
        if position_size > max_position_size:
            position_size = max_position_size
            position_value = position_size * current_price
            margin_required = position_value / leverage
        
        return position_size, margin_required, leverage
    
    def calculate_stop_loss_with_leverage(self, entry_price: float, side: str, leverage: float) -> float:
        """
        Calculate stop loss price with leverage consideration.
        
        Args:
            entry_price: Entry price
            side: Position side ('long' or 'short')
            leverage: Leverage used
            
        Returns:
            Stop loss price
        """
        # Calculate stop loss percentage based on leverage
        # Higher leverage = tighter stop loss
        base_stop_loss_pct = self.stop_loss_percentage / 100
        leverage_adjusted_pct = base_stop_loss_pct / leverage
        
        if side == 'long':
            stop_loss = entry_price * (1 - leverage_adjusted_pct)
        else:
            stop_loss = entry_price * (1 + leverage_adjusted_pct)
        
        return stop_loss
    
    def calculate_take_profit_with_leverage(self, entry_price: float, side: str, leverage: float, 
                                         strategy_take_profit: float = None) -> float:
        """
        Calculate take profit price with leverage consideration.
        
        Args:
            entry_price: Entry price
            side: Position side ('long' or 'short')
            leverage: Leverage used
            strategy_take_profit: Strategy-specific take profit (optional)
            
        Returns:
            Take profit price
        """
        if strategy_take_profit:
            return strategy_take_profit
        
        # Default take profit based on leverage
        base_take_profit_pct = (self.stop_loss_percentage * 2) / 100  # 2x the stop loss
        leverage_adjusted_pct = base_take_profit_pct / leverage
        
        if side == 'long':
            take_profit = entry_price * (1 + leverage_adjusted_pct)
        else:
            take_profit = entry_price * (1 - leverage_adjusted_pct)
        
        return take_profit
    
    def calculate_stop_loss_with_capital_at_risk(self, entry_price: float, side: str, capital_at_risk: float, max_loss_amount: float) -> float:
        """
        Calculate stop loss based on capital at risk.
        
        Args:
            entry_price: Entry price
            side: Position side ('long' or 'short')
            capital_at_risk: Capital at risk
            max_loss_amount: Maximum loss amount
            
        Returns:
            Stop loss price
        """
        # Calculate price change needed to achieve max loss
        price_change_pct = max_loss_amount / capital_at_risk
        
        if side == 'long':
            stop_loss = entry_price * (1 - price_change_pct)
        else:
            stop_loss = entry_price * (1 + price_change_pct)
        
        return stop_loss
    
    def calculate_take_profit_with_capital_at_risk(self, entry_price: float, side: str, capital_at_risk: float, target_profit_amount: float) -> float:
        """
        Calculate take profit based on capital at risk.
        
        Args:
            entry_price: Entry price
            side: Position side ('long' or 'short')
            capital_at_risk: Capital at risk
            target_profit_amount: Target profit amount
            
        Returns:
            Take profit price
        """
        # Calculate price change needed to achieve target profit
        price_change_pct = target_profit_amount / capital_at_risk
        
        if side == 'long':
            take_profit = entry_price * (1 + price_change_pct)
        else:
            take_profit = entry_price * (1 - price_change_pct)
        
        return take_profit
    
    def can_open_position(self, symbol: str, margin_required: float, available_capital: float) -> bool:
        """
        Check if we can open a new position.
        
        Args:
            symbol: Trading symbol
            margin_required: Margin required for position
            available_capital: Available capital
            
        Returns:
            True if position can be opened
        """
        # Check if we have enough capital
        if margin_required > available_capital * (1 - self.margin_buffer):
            return False
        
        # Check if we're not over-leveraged
        total_margin_after = self.total_margin_used + margin_required
        if total_margin_after > available_capital * self.liquidation_threshold:
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
            'entry_time': datetime.now()
        }
        
        self.total_margin_used += margin_used
    
    def close_position(self, symbol: str, exit_price: float) -> Optional[Dict[str, Any]]:
        """
        Close a position and calculate PnL.
        
        Args:
            symbol: Trading symbol
            exit_price: Exit price
            
        Returns:
            Position result dictionary or None
        """
        if symbol not in self.open_positions:
            return None
        
        position = self.open_positions[symbol]
        entry_price = position['entry_price']
        size = position['size']
        side = position['side']
        margin_used = position['margin_used']
        
        # Calculate PnL
        if side == 'long':
            pnl = (exit_price - entry_price) * size
        else:
            pnl = (entry_price - exit_price) * size
        
        # Calculate PnL percentage
        pnl_percentage = (pnl / margin_used) * 100
        
        # Update margin used
        self.total_margin_used -= margin_used
        
        # Remove position
        del self.open_positions[symbol]
        
        return {
            'symbol': symbol,
            'side': side,
            'entry_price': entry_price,
            'exit_price': exit_price,
            'size': size,
            'pnl': pnl,
            'pnl_percentage': pnl_percentage,
            'margin_used': margin_used
        }
    
    def update_available_margin(self, available_capital: float):
        """
        Update available margin.
        
        Args:
            available_capital: Available capital
        """
        self.available_margin = available_capital
    
    def get_margin_summary(self) -> Dict[str, Any]:
        """
        Get margin usage summary.
        
        Returns:
            Margin summary dictionary
        """
        return {
            'total_margin_used': self.total_margin_used,
            'available_margin': self.available_margin,
            'margin_utilization': (self.total_margin_used / self.available_margin * 100) if self.available_margin > 0 else 0,
            'open_positions': len(self.open_positions)
        }
    
    def get_risk_summary(self) -> Dict[str, Any]:
        """
        Get risk summary.
        
        Returns:
            Risk summary dictionary
        """
        return {
            'margin_buffer': self.margin_buffer * 100,
            'liquidation_threshold': self.liquidation_threshold * 100,
            'risk_percentage': self.risk_percentage * 100,
            'stop_loss_percentage': self.stop_loss_percentage * 100
        } 