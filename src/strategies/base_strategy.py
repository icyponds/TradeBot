"""
Base strategy class for all trading strategies.
"""

import logging
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
import pandas as pd


class BaseStrategy(ABC):
    """Base class for all trading strategies."""
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the base strategy.
        
        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.logger = logging.getLogger(self.__class__.__name__)
        
        # Strategy state
        self.is_active = True
        self.trades = []
        
        # Configuration
        self.timeframe = config['strategies']['timeframe']
        self.ohlcv_limit = config['strategies']['ohlcv_limit']
    
    @abstractmethod
    def generate_signal(self, ohlcv: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """
        Generate trading signal based on market data.
        
        Args:
            ohlcv: OHLCV data DataFrame
            
        Returns:
            Signal dictionary or None if no signal
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
        # Get maximum position size from config
        max_position_size_usd = self.config['trading']['max_position_size_usd']
        max_position_size_percentage = self.config['trading']['max_position_size_percentage']
        
        # Calculate position size based on risk
        position_size = min(risk_amount / (self.config['trading']['risk_percentage'] / 100), max_position_size_usd)
        
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
    
    def calculate_take_profit(self, entry_price: float, side: str, ohlcv: pd.DataFrame = None, 
                            signal_strength: float = 1.0, market_volatility: float = 1.0) -> float:
        """
        Calculate take profit price based on strategy-specific logic.
        
        Args:
            entry_price: Entry price
            side: 'buy' or 'sell'
            ohlcv: OHLCV data for strategy-specific calculations
            signal_strength: Signal strength (0.0 to 1.0)
            market_volatility: Market volatility factor
            
        Returns:
            Take profit price
        """
        # Default implementation - can be overridden by specific strategies
        base_take_profit_percentage = 0.06  # 6% default
        
        # Adjust based on signal strength and volatility
        adjusted_percentage = base_take_profit_percentage * signal_strength * market_volatility
        
        if side == 'buy':
            return entry_price * (1 + adjusted_percentage)
        else:
            return entry_price * (1 - adjusted_percentage)
    
    def record_trade(self, symbol: str, side: str, price: float, size: float, timestamp):
        """
        Record a completed trade.
        
        Args:
            symbol: Trading symbol
            side: Trade side
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
        }
        
        self.trades.append(trade)
        self.logger.info(f"Recorded {side} trade for {symbol} at {price}")
    
    def get_performance_metrics(self) -> Dict[str, Any]:
        """
        Get performance metrics for the strategy.
        
        Returns:
            Performance metrics dictionary
        """
        if not self.trades:
            return {
                'total_trades': 0,
                'win_rate': 0,
                'total_pnl': 0,
                'average_trade_size': 0,
            }
        
        total_trades = len(self.trades)
        winning_trades = sum(1 for trade in self.trades if trade.get('pnl', 0) > 0)
        win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
        total_pnl = sum(trade.get('pnl', 0) for trade in self.trades)
        avg_trade_size = sum(trade['size'] for trade in self.trades) / total_trades
        
        return {
            'total_trades': total_trades,
            'winning_trades': winning_trades,
            'win_rate': win_rate,
            'total_pnl': total_pnl,
            'average_trade_size': avg_trade_size,
        }
    
    def reset(self):
        """Reset the strategy state."""
        self.trades = []
        self.logger.info("Strategy reset")
    
    def stop(self):
        """Stop the strategy."""
        self.is_active = False
        self.logger.info("Strategy stopped")
    
    def start(self):
        """Start the strategy."""
        self.is_active = True
        self.logger.info("Strategy started") 