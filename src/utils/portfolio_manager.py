"""
Portfolio manager for dynamic position sizing based on account balance.
"""

import logging
from typing import Dict, Any, Optional, Tuple
from datetime import datetime, timedelta


class PortfolioManager:
    """Manages portfolio information and position sizing calculations."""
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the portfolio manager.
        
        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # Portfolio configuration
        self.use_portfolio_based_sizing = config['trading']['use_portfolio_based_sizing']
        self.max_position_size_percentage = config['trading']['max_position_size_percentage']
        self.max_positions_percentage = config['trading']['max_positions_percentage']
        
        # Portfolio state
        self.total_equity = 0.0
        self.free_margin = 0.0
        self.used_margin = 0.0
        self.last_update = None
        self.update_interval = timedelta(minutes=5)  # Update every 5 minutes
        
        self.logger.info(f"Portfolio manager initialized with portfolio-based sizing: {self.use_portfolio_based_sizing}")
    
    def update_portfolio_info(self, market_api) -> bool:
        """
        Update portfolio information from the market API.
        
        Args:
            market_api: Market API instance
            
        Returns:
            True if update successful, False otherwise
        """
        try:
            balance_info = market_api.get_account_balance()
            if not balance_info:
                self.logger.warning("Failed to get account balance, using fallback values")
                return False
            
            self.total_equity = balance_info.get('total_equity', 0.0)
            self.free_margin = balance_info.get('free_margin', 0.0)
            self.used_margin = balance_info.get('used_margin', 0.0)
            self.last_update = datetime.now()
            
            self.logger.info(f"Portfolio updated: ${self.total_equity:.2f} total equity, ${self.free_margin:.2f} free margin")
            self.logger.debug(f"Raw balance info: {balance_info}")
            return True
            
        except Exception as e:
            self.logger.error(f"Error updating portfolio info: {e}")
            return False
    
    def should_update_portfolio(self) -> bool:
        """
        Check if portfolio information should be updated.
        
        Returns:
            True if update is needed, False otherwise
        """
        if not self.last_update:
            return True
        
        return datetime.now() - self.last_update > self.update_interval
    
    def calculate_max_position_size(self, symbol: str = None) -> float:
        """
        Calculate maximum position size based on portfolio and configuration.
        
        Args:
            symbol: Trading symbol (optional, for future symbol-specific logic)
            
        Returns:
            Maximum position size in USD
        """
        if not self.use_portfolio_based_sizing or self.total_equity <= 0:
            # If portfolio sizing is disabled or equity is unknown, we cannot safely size.
            # Callers should ensure portfolio info is updated.
            return 0.0
        
        # Calculate percentage-based position size
        percentage_based_size = self.total_equity * (self.max_position_size_percentage / 100)
        
        max_position_size = percentage_based_size
        
        self.logger.debug(f"Max position size for {symbol}: ${max_position_size:.2f} "
                         f"(portfolio: ${self.total_equity:.2f}, {self.max_position_size_percentage}%)")
        
        return max_position_size
    
    def calculate_available_capital_for_trading(self) -> float:
        """
        Calculate available capital for new positions.
        
        Returns:
            Available capital in USD
        """
        if not self.use_portfolio_based_sizing:
            return self.free_margin
        
        # Calculate maximum capital that can be used for positions
        max_capital_for_positions = self.total_equity * (self.max_positions_percentage / 100)
        
        # Available capital is the minimum of free margin and max capital for positions
        available_capital = min(self.free_margin, max_capital_for_positions)
        
        self.logger.debug(f"Available capital: ${available_capital:.2f} "
                         f"(free margin: ${self.free_margin:.2f}, max positions: ${max_capital_for_positions:.2f})")
        
        return available_capital
    
    def can_open_position(self, required_margin: float) -> bool:
        """
        Check if a new position can be opened with the required margin.
        
        Args:
            required_margin: Required margin for the position
            
        Returns:
            True if position can be opened, False otherwise
        """
        available_capital = self.calculate_available_capital_for_trading()
        return required_margin <= available_capital
    
    def get_portfolio_summary(self) -> Dict[str, Any]:
        """
        Get portfolio summary information.
        
        Returns:
            Portfolio summary dictionary
        """
        # Calculate margin usage percentage
        margin_usage_percentage = 0.0
        if self.total_equity > 0:
            margin_usage_percentage = (self.used_margin / self.total_equity) * 100

        return {
            'total_equity': self.total_equity,
            'free_margin': self.free_margin,
            'available_margin': self.free_margin, # Alias for dashboard compatibility
            'used_margin': self.used_margin,
            'margin_usage_percentage': margin_usage_percentage,
            'max_position_size': self.calculate_max_position_size(),
            'available_capital': self.calculate_available_capital_for_trading(),
            'use_portfolio_based_sizing': self.use_portfolio_based_sizing,
            'last_update': self.last_update.isoformat() if self.last_update else None,
        }
    
    def get_position_size_limits(self) -> Dict[str, float]:
        """
        Get current position size limits.
        
        Returns:
            Dictionary with position size limits
        """
        return {
            'max_position_size_percentage': self.max_position_size_percentage,
            'current_max_position_size': self.calculate_max_position_size(),
            'max_positions_percentage': self.max_positions_percentage,
            'available_capital': self.calculate_available_capital_for_trading(),
        } 