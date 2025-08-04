"""
Leverage manager for dynamic leverage calculation and position sizing.
"""

import logging
import math
from typing import Dict, Any, Optional, Tuple
from .portfolio_manager import PortfolioManager


class LeverageManager:
    """
    Manages dynamic leverage calculation and position sizing based on market conditions and portfolio.
    """
    
    def __init__(self, config: Dict[str, Any], portfolio_manager: PortfolioManager = None):
        """
        Initialize the leverage manager.
        
        Args:
            config: Configuration dictionary
            portfolio_manager: Portfolio manager instance for position sizing
        """
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.portfolio_manager = portfolio_manager
        
        # Risk management configuration
        self.risk_percentage = config['trading']['risk_percentage']
        self.stop_loss_percentage = config['trading']['stop_loss_percentage']
        self.margin_buffer_percentage = config['risk_management']['margin_buffer_percentage']
        self.liquidation_risk_threshold = config['risk_management']['liquidation_risk_threshold']
        
        # Position tracking
        self.positions = {}
        self.total_margin_used = 0.0
        self.available_margin = 0.0
        
        self.logger.info("Leverage manager initialized")
    
    def set_portfolio_manager(self, portfolio_manager: PortfolioManager):
        """
        Set the portfolio manager for position sizing calculations.
        
        Args:
            portfolio_manager: Portfolio manager instance
        """
        self.portfolio_manager = portfolio_manager
        self.logger.info("Portfolio manager set for leverage manager")
    
    def calculate_dynamic_leverage(self, symbol: str, strategy_name: str, signal_strength: float, 
                                 market_volatility: float, current_price: float) -> float:
        """
        Calculate dynamic leverage based on market conditions and signal strength.
        
        Args:
            symbol: Trading symbol
            strategy_name: Name of the strategy
            signal_strength: Signal strength (0-1)
            market_volatility: Market volatility measure
            current_price: Current asset price
            
        Returns:
            Dynamic leverage value
        """
        # Base leverage calculation
        base_leverage = 2.0  # Conservative base leverage
        
        # Adjust leverage based on signal strength
        signal_adjustment = 1.0 + (signal_strength * 0.5)  # Up to 50% increase for strong signals
        
        # Adjust leverage based on market volatility (inverse relationship)
        # Handle edge case where market_volatility is -1.0 (which would cause division by zero)
        if market_volatility <= -1.0:
            volatility_adjustment = 0.1  # Very low leverage for extreme volatility
        else:
            volatility_adjustment = 1.0 / (1.0 + market_volatility)  # Lower leverage for high volatility
        
        # Strategy-specific adjustments
        strategy_adjustment = 1.0
        if strategy_name == 'moving_average':
            strategy_adjustment = 1.1  # Slightly higher leverage for trend-following
        elif strategy_name == 'rsi':
            strategy_adjustment = 0.9  # Lower leverage for mean-reversion
        
        # Calculate final leverage
        dynamic_leverage = base_leverage * signal_adjustment * volatility_adjustment * strategy_adjustment
        
        # Apply limits
        min_leverage = 1.0
        max_leverage = 10.0  # Conservative maximum leverage
        
        dynamic_leverage = max(min_leverage, min(max_leverage, dynamic_leverage))
        
        self.logger.debug(f"Dynamic leverage for {symbol} ({strategy_name}): {dynamic_leverage:.1f}x")
        
        return dynamic_leverage
    
    def calculate_leveraged_position_size(self, symbol: str, current_price: float, available_capital: float,
                                        strategy_name: str, signal_strength: float, market_volatility: float) -> Tuple[float, float, float]:
        """
        Calculate leveraged position size with dynamic leverage and portfolio-based sizing.
        
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
        
        # Get maximum position size from portfolio manager if available
        if self.portfolio_manager:
            max_position_size = self.portfolio_manager.calculate_max_position_size(symbol)
        else:
            # Fallback to fixed amount from config
            max_position_size = self.config['trading']['max_position_size_usd']
        
        # Calculate maximum capital at risk per trade
        max_risk_per_trade = max_position_size
        
        # Calculate position value based on maximum risk
        # With leverage, the position value = risk_amount * leverage
        position_value = max_risk_per_trade * leverage
        
        # Calculate position size in units
        if current_price <= 0:
            self.logger.warning(f"Invalid current price for {symbol}: {current_price}")
            return 0.0, 0.0, leverage
        
        position_size = position_value / current_price
        
        # Calculate margin required (this is the actual capital at risk)
        margin_required = max_risk_per_trade
        
        # Ensure we don't exceed the maximum risk per trade
        if margin_required > max_position_size:
            margin_required = max_position_size
            position_value = margin_required * leverage
            position_size = position_value / current_price
        
        # Ensure we don't exceed maximum position size
        max_position_value = max_position_size * leverage
        max_position_size_units = max_position_value / current_price if current_price > 0 else 0.0
        
        if position_size > max_position_size_units:
            position_size = max_position_size_units
            position_value = position_size * current_price
            margin_required = position_value / leverage
        
        self.logger.debug(f"Position size calculation for {symbol}: {position_size:.4f} units, "
                         f"${margin_required:.2f} margin, {leverage:.1f}x leverage")
        
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
        
        # Handle division by zero
        if leverage <= 0:
            self.logger.warning(f"Invalid leverage value: {leverage}, using default stop loss")
            leverage_adjusted_pct = base_stop_loss_pct
        else:
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
        
        # Handle division by zero
        if leverage <= 0:
            self.logger.warning(f"Invalid leverage value: {leverage}, using default take profit")
            leverage_adjusted_pct = base_take_profit_pct
        else:
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
        if capital_at_risk <= 0:
            self.logger.warning(f"Invalid capital at risk: {capital_at_risk}, using default stop loss")
            price_change_pct = 0.02  # Default 2% stop loss
        else:
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
        if capital_at_risk <= 0:
            self.logger.warning(f"Invalid capital at risk: {capital_at_risk}, using default take profit")
            price_change_pct = 0.06  # Default 6% take profit
        else:
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
        if margin_required > available_capital * (1 - self.margin_buffer_percentage / 100):
            return False
        
        # Check if we're not over-leveraged
        total_margin_after = self.total_margin_used + margin_required
        if total_margin_after > available_capital * self.liquidation_risk_threshold:
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
        self.positions[symbol] = {
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
        if symbol not in self.positions:
            return None
        
        position = self.positions[symbol]
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
        del self.positions[symbol]
        
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
            'open_positions': len(self.positions)
        }
    
    def get_risk_summary(self) -> Dict[str, Any]:
        """
        Get risk summary.
        
        Returns:
            Risk summary dictionary
        """
        return {
            'margin_buffer': self.margin_buffer_percentage,
            'liquidation_threshold': self.liquidation_risk_threshold * 100,
            'risk_percentage': self.risk_percentage * 100,
            'stop_loss_percentage': self.stop_loss_percentage * 100
        } 