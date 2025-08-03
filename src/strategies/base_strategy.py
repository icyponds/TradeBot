"""
Base strategy class for trading strategies.
"""

import logging
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List
import pandas as pd


class BaseStrategy(ABC):
    """Base class for all trading strategies."""
    
    def __init__(self, config: Dict[str, Any], market_api):
        """
        Initialize the base strategy.
        
        Args:
            config: Configuration dictionary
            market_api: Market data API instance
        """
        self.config = config
        self.market_api = market_api
        self.logger = logging.getLogger(self.__class__.__name__)
        self.name = self.__class__.__name__
        
        # Strategy state
        self.positions = {}
        self.trades = []
        self.is_active = True
    
    @abstractmethod
    def analyze(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Analyze market data and generate trading signals.
        
        Args:
            market_data: Market data dictionary
            
        Returns:
            Dictionary containing analysis results and signals
        """
        pass
    
    @abstractmethod
    def should_buy(self, analysis: Dict[str, Any]) -> bool:
        """
        Determine if we should buy based on analysis.
        
        Args:
            analysis: Analysis results from analyze() method
            
        Returns:
            True if should buy, False otherwise
        """
        pass
    
    @abstractmethod
    def should_sell(self, analysis: Dict[str, Any]) -> bool:
        """
        Determine if we should sell based on analysis.
        
        Args:
            analysis: Analysis results from analyze() method
            
        Returns:
            True if should sell, False otherwise
        """
        pass
    
    def calculate_position_size(self, price: float, risk_amount: float) -> float:
        """
        Calculate position size based on risk management rules.
        
        Args:
            price: Current price
            risk_amount: Amount willing to risk
            
        Returns:
            Position size in base currency
        """
        max_position = self.config['trading']['max_position_size']
        risk_percentage = self.config['trading']['risk_percentage'] / 100
        
        # Calculate position size based on risk
        position_size = min(risk_amount / risk_percentage, max_position)
        
        return position_size
    
    def calculate_stop_loss(self, entry_price: float, side: str) -> float:
        """
        Calculate stop loss price.
        
        Args:
            entry_price: Entry price
            side: 'buy' or 'sell'
            
        Returns:
            Stop loss price
        """
        stop_loss_percentage = self.config['trading']['stop_loss_percentage'] / 100
        
        if side == 'buy':
            return entry_price * (1 - stop_loss_percentage)
        else:
            return entry_price * (1 + stop_loss_percentage)
    
    def calculate_take_profit(self, entry_price: float, side: str) -> float:
        """
        Calculate take profit price.
        
        Args:
            entry_price: Entry price
            side: 'buy' or 'sell'
            
        Returns:
            Take profit price
        """
        take_profit_percentage = self.config['trading']['take_profit_percentage'] / 100
        
        if side == 'buy':
            return entry_price * (1 + take_profit_percentage)
        else:
            return entry_price * (1 - take_profit_percentage)
    
    def record_trade(self, symbol: str, side: str, price: float, size: float, timestamp):
        """
        Record a trade for tracking.
        
        Args:
            symbol: Trading symbol
            side: 'buy' or 'sell'
            price: Trade price
            size: Trade size
            timestamp: Trade timestamp
        """
        trade = {
            'symbol': symbol,
            'side': side,
            'price': price,
            'size': size,
            'timestamp': timestamp,
            'strategy': self.name,
        }
        
        self.trades.append(trade)
        self.logger.info(f"Recorded {side} trade: {symbol} at {price}")
    
    def get_performance_metrics(self) -> Dict[str, Any]:
        """
        Calculate performance metrics for the strategy.
        
        Returns:
            Dictionary with performance metrics
        """
        if not self.trades:
            return {
                'total_trades': 0,
                'win_rate': 0,
                'total_pnl': 0,
                'avg_trade_pnl': 0,
            }
        
        # Calculate basic metrics
        total_trades = len(self.trades)
        winning_trades = 0
        total_pnl = 0
        
        for trade in self.trades:
            # Simple PnL calculation (can be enhanced)
            if trade['side'] == 'buy':
                # For buy trades, assume we sold at a profit
                pnl = trade['price'] * 0.01  # Simplified
                total_pnl += pnl
                winning_trades += 1
        
        win_rate = (winning_trades / total_trades) * 100 if total_trades > 0 else 0
        avg_trade_pnl = total_pnl / total_trades if total_trades > 0 else 0
        
        return {
            'total_trades': total_trades,
            'win_rate': win_rate,
            'total_pnl': total_pnl,
            'avg_trade_pnl': avg_trade_pnl,
        }
    
    def stop(self):
        """Stop the strategy."""
        self.is_active = False
        self.logger.info(f"Strategy {self.name} stopped")
    
    def reset(self):
        """Reset strategy state."""
        self.positions = {}
        self.trades = []
        self.is_active = True
        self.logger.info(f"Strategy {self.name} reset") 