"""
Leverage manager for dynamic leverage calculation and position sizing.
"""

import logging
import math
from typing import Dict, Any, Optional, Tuple
from .portfolio_manager import PortfolioManager
from datetime import datetime


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
        # Strategy-specific SL/TP and account-based risk limits are enforced in StrategyManager.
        # These are only heuristic defaults used when a strategy doesn't provide a TP/SL.
        self.fallback_stop_loss_pct = float(
            config.get('leverage_management', {}).get('fallback_stop_loss_pct', 0.05)
        )  # 5% default
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
                                 market_volatility: float, current_price: float, asset_max_leverage: float = 100.0,
                                 strategy_leverage: Optional[float] = None) -> float:
        """
        Calculate dynamic leverage based on market conditions, signal strength, and strategy preference.
        
        Args:
            symbol: Trading symbol
            strategy_name: Name of the strategy
            signal_strength: Signal strength (0-1)
            signal_strength: Signal strength (0-1)
            market_volatility: Market volatility measure
            current_price: Current asset price
            asset_max_leverage: Maximum leverage allowed for this asset (default: 100.0)
            strategy_leverage: Leverage requested by strategy signal (default: None -> 1.0)
            
        Returns:
            Dynamic leverage value
        """
        # Get configuration values
        leverage_config = self.config.get('leverage_management', {})
        
        # Use strategy-provided leverage or default to 1.0 (safe/unleveraged) if not provided
        base_leverage = strategy_leverage if strategy_leverage is not None else 1.0
        min_leverage = float(leverage_config.get('min_leverage', 1.0))
        
        # Volatility Targeting Logic
        # Target Annualized Volatility (default 40%)
        # Logic: Leverage = Target Vol / Asset Vol
        target_vol = float(leverage_config.get('target_annual_volatility', 0.40))
        
        # Ensure input volatility is sanitized. 
        # If vol < 0.0, it's invalid. If vol < 0.10, it effectively implies massive leverage 
        # (likely daily/hourly vol passed by mistake), so we clamp it safely.
        # Ideally, inputs are already annualized.
        safe_vol = max(0.10, market_volatility) # Clamp to min 10% vol to preventing infinite leverage bubbles
        
        # Calculate raw volatility scalar
        vol_scalar = target_vol / safe_vol
        
        # Apply Base Leverage (Strategy Preference)
        # If strategy asks for 1.0 (neutral), we use pure vol targeting.
        # If strategy asks for 2.0 (aggressive), we scale up.
        dynamic_leverage = vol_scalar * base_leverage
        
        # Signal Strength Multiplier (optional boost for high conviction)
        # We temper this to be subtle (e.g. 0.8x to 1.2x)
        signal_mod = 1.0 + (signal_strength - 0.5) * 0.4
        dynamic_leverage *= signal_mod
        
        # Absolute Safety Caps
        dynamic_leverage = max(min_leverage, min(asset_max_leverage, dynamic_leverage))
        
        # Round to 1 decimal for cleanliness
        dynamic_leverage = round(dynamic_leverage, 1)
        
        self.logger.debug(f"Dynamic Lev for {symbol}: {dynamic_leverage}x "
                         f"(TargetVol={target_vol}, AssetVol={market_volatility:.2f}, "
                         f"Scalar={vol_scalar:.2f}, SignalMod={signal_mod:.2f})")
        
        return dynamic_leverage
    
    def calculate_leveraged_position_size(self, symbol: str, current_price: float, available_capital: float,
                                        strategy_name: str, signal_strength: float, market_volatility: float, 
                                        asset_max_leverage: float = 100.0, stop_loss_pct: float = 0.05) -> Tuple[float, float, float]:
        """
        Calculate position size using Risk-Based Sizing logic:
        Size = (Equity * Risk%) / StopLoss%
        
        Args:
            symbol: Trading symbol
            current_price: Current asset price
            available_capital: Available capital for trading
            strategy_name: Name of the strategy
            signal_strength: Signal strength (0-1)
            market_volatility: Market volatility measure (Annualized)
            asset_max_leverage: Maximum leverage allowed
            stop_loss_pct: Expected stop loss percentage (decimal)
            
        Returns:
            Tuple of (position_size, margin_required, leverage)
        """
        # 1. Calculate Dynamic Leverage (Volatility Targeted)
        leverage = self.calculate_dynamic_leverage(
            symbol, strategy_name, signal_strength, market_volatility, current_price, asset_max_leverage
        )
        
        # Determine available capital (via PortfolioManager if possible)
        if self.portfolio_manager:
            available_capital = self.portfolio_manager.calculate_available_capital_for_trading()
            equity = self.portfolio_manager.total_equity
        else:
            # Fallback if no PM attached (e.g. unit tests or standalone usage)
            equity = available_capital
        
        # CRITICAL: Prevent trading if insolvent
        if available_capital <= 0:
            self.logger.warning(f"Sizing {symbol}: REFUSED due to non-positive capital (${available_capital:.2f})")
            return 0.0, 0.0, 1.0
        
        # 2. Determine Risk Parameters
        lm_cfg = self.config.get("leverage_management", {}) or {}
        # Default Risk Per Trade: 1.0%
        risk_per_trade_pct = float(lm_cfg.get("risk_per_trade_pct", 1.0)) / 100.0
        
        # 3. Calculate Risk Budget
        risk_budget = equity * risk_per_trade_pct
        
        # 4. Calculate Position Notional Size based on Risk
        if stop_loss_pct <= 0:
            safe_leverage = max(1.0, leverage)
            stop_loss_pct = self.fallback_stop_loss_pct / safe_leverage
            
        raw_position_notional = risk_budget / stop_loss_pct
        
        # 5. Apply Position Limits (Concentration Cap)
        # Use PortfolioManager to get the cap
        if self.portfolio_manager:
            max_allowed_notional = self.portfolio_manager.calculate_max_position_size(symbol)
        else:
            # Fallback manual calculation
            max_pos_pct = float(self.config['trading'].get('max_position_size_percentage', 10.0)) / 100.0
            max_allowed_notional = equity * max_pos_pct
        
        final_position_notional = min(raw_position_notional, max_allowed_notional)
        
        # 6. Apply Minimum Order Value
        MIN_ORDER_VALUE = 12.0
        if final_position_notional < MIN_ORDER_VALUE:
            if final_position_notional > 0:
                final_position_notional = MIN_ORDER_VALUE
        
        # 7. Calculate Position Size (Units) and Margin Required
        position_size = final_position_notional / current_price if current_price > 0 else 0.0
        margin_required = final_position_notional / leverage
        
        # 8. Check Capital Availability
        # Use PortfolioManager logic if available
        if self.portfolio_manager:
            if not self.portfolio_manager.can_open_position(margin_required):
                # Scale down to fit available capital
                available_cap = self.portfolio_manager.calculate_available_capital_for_trading()
                margin_required = available_cap * (1 - self.margin_buffer_percentage / 100)
                final_position_notional = margin_required * leverage
                position_size = final_position_notional / current_price
        else:
            # Fallback manual check
            if margin_required > available_capital * (1 - self.margin_buffer_percentage / 100):
                margin_required = available_capital * (1 - self.margin_buffer_percentage / 100)
                final_position_notional = margin_required * leverage
                position_size = final_position_notional / current_price
            
        self.logger.info(
            f"Sizing {symbol}: Risk=${risk_budget:.2f}, SL={stop_loss_pct*100:.1f}%, "
            f"Notional=${final_position_notional:.2f}, Lev={leverage}x"
        )
        
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
        base_stop_loss_pct = self.fallback_stop_loss_pct
        
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
        base_take_profit_pct = self.fallback_stop_loss_pct * 2  # 2x the stop loss
        
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
        # Use PortfolioManager if available
        if self.portfolio_manager:
            return self.portfolio_manager.can_open_position(margin_required)
            
        # Fallback manual check
        # Check if we have enough capital
        if margin_required > available_capital * (1 - self.margin_buffer_percentage / 100):
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
            'fallback_stop_loss_pct': self.fallback_stop_loss_pct * 100,
        } 