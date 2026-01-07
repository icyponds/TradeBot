"""
Base strategy class for all trading strategies.
"""

import logging
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, Tuple
import pandas as pd
from datetime import datetime


class BaseStrategy(ABC):
    """Base class for all trading strategies."""
    
    # Default timeframe - subclasses should override with optimal value
    PREFERRED_TIMEFRAME = '15m'
    
    # Timeframe to minutes mapping
    TIMEFRAME_MINUTES = {
        '1m': 1, '5m': 5, '15m': 15, '30m': 30,
        '1h': 60, '4h': 240, '1d': 1440
    }
    
    def __init__(self, config: Dict[str, Any], timeframe: str = None):
        """
        Initialize the base strategy.
        
        Args:
            config: Configuration dictionary
            timeframe: Optional timeframe override (e.g. '15m', '1h'). 
                       If None, uses PREFERRED_TIMEFRAME.
        """
        self.config = config
        self.logger = logging.getLogger(self.__class__.__name__)
        
        # Strategy state
        self.is_active = True
        self.trades = []
        
        # Timeframe - use override or class-defined preferred timeframe
        self.timeframe = timeframe if timeframe else self.PREFERRED_TIMEFRAME
        self.ohlcv_limit = config['strategies']['ohlcv_limit']
    
    @property
    def execution_interval_seconds(self) -> int:
        """Get execution interval in seconds based on timeframe."""
        minutes = self.TIMEFRAME_MINUTES.get(self.timeframe, 15)
        return minutes * 60
    
    @abstractmethod
    def generate_signal(self, symbol: str, ohlcv: Dict[str, pd.DataFrame]) -> Optional[Dict[str, Any]]:
        """
        Generate trading signal based on market data across multiple timeframes.
        
        Args:
            symbol: Symbol being analyzed
            ohlcv: Dictionary mapping timeframe (e.g., '1h', '4h') to OHLCV DataFrame
            
        Returns:
            Signal dictionary or None if no signal
        """
        pass

    def calculate_signal_strength(self, ohlcv: Dict[str, pd.DataFrame], symbol: str = None, signal_context: Dict[str, Any] = None) -> float:
        """
        Calculate the strength of the trading signal.
        
        Args:
            ohlcv: Dictionary mapping timeframe to OHLCV DataFrame
            symbol: Optional symbol being analyzed (required for stateful strategies)
            signal_context: Optional context from generated signal (e.g. z-score), prevents recalculation
            
        Returns:
            Signal strength between 0.0 and 1.0
        """
        # Default implementation returns 0.5 (neutral)
        return 0.5
    
    def calculate_position_size(self, price: float, risk_amount: float) -> float:
        """
        Calculate position size based on risk management rules.
        
        Args:
            price: Current price
            risk_amount: Amount willing to risk
            
        Returns:
            Position size in base currency
        """
        # NOTE: Global "risk_percentage" sizing has been removed in favor of portfolio-based sizing
        # managed by `PortfolioManager` + account-based max loss enforcement in `StrategyManager`.
        # Here, `risk_amount` is treated as a USD cap for this position.
        # Strategies should not impose USD-denominated hard caps; sizing is handled via
        # portfolio/percentage-based logic upstream (PortfolioManager + LeverageManager).
        return float(risk_amount)
    
    # NOTE: calculate_stop_loss has been removed from the strategy layer.
    # Stop-loss is now entirely managed by the centralized risk layer:
    #   - ExecutionEngine applies account-based and leverage-based stops
    #   - LeverageManager.calculate_stop_loss_with_leverage() computes the stop
    # Strategies that need custom stop-loss logic can implement calculate_stop_loss
    # and it will be checked via hasattr in ExecutionEngine.
    
    def should_exit(self, position: Any, current_price: float, 
                   current_data: Dict[str, Any] = None) -> Tuple[bool, Optional[str]]:
        """
        Determine if an existing position should be closed based on strategy logic.
        
        This method allows strategies to define custom exit conditions beyond
        simple price-based TP/SL. For example:
        - OU Mean Reversion: Exit when Z-score returns to zero
        - Momentum: Exit on rebalance when asset falls out of top/bottom N
        - Funding Arb: Exit when funding rate normalizes
        
        Args:
            position: The current position object
            current_price: Current market price
            current_data: Additional market data (OHLCV, indicators, etc.)
            
        Returns:
            Tuple of (should_exit: bool, reason: str or None)
        """
        # Default implementation - no strategy-specific exit
        # Subclasses should override for custom exit logic
        return False, None
    
    def calculate_take_profit(self, entry_price: float, side: str, ohlcv: Dict[str, pd.DataFrame] = None, 
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
    
    def get_trailing_stop_config(self) -> Dict[str, Any]:
        """
        Get trailing stop configuration for this strategy.
        
        Override this method in subclasses to enable trailing stops.
        
        Returns:
            Dictionary with:
                - enabled: bool - Whether trailing stop is enabled
                - trail_pct: float - Trailing percentage (e.g., 0.05 = 5%)
                - activation_pct: float - Minimum gain before trailing activates (e.g., 0.03 = 3%)
        """
        # Default: trailing stop disabled
        return {
            'enabled': False,
            'trail_pct': 0.0,
            'activation_pct': 0.0,
        }
    
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