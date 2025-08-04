"""
RSI (Relative Strength Index) Strategy.
"""

import logging
from typing import Dict, Any, Optional
import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy


class RSIStrategy(BaseStrategy):
    """RSI-based trading strategy."""
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        
        # Strategy parameters
        self.period = config['strategies']['rsi']['period']
        self.overbought = config['strategies']['rsi']['overbought']
        self.oversold = config['strategies']['rsi']['oversold']
        
        self.logger.info(f"Initialized RSI Strategy: period={self.period}, overbought={self.overbought}, oversold={self.oversold}")
    
    def calculate_take_profit(self, entry_price: float, side: str, ohlcv: pd.DataFrame = None, 
                            signal_strength: float = 1.0, market_volatility: float = 1.0) -> float:
        """
        Calculate take profit based on RSI momentum and extreme levels.
        
        Args:
            entry_price: Entry price
            side: 'buy' or 'sell'
            ohlcv: OHLCV data for RSI analysis
            signal_strength: Signal strength (0.0 to 1.0)
            market_volatility: Market volatility factor
            
        Returns:
            Take profit price
        """
        if ohlcv is None or len(ohlcv) < self.period + 1:
            # Fallback to base implementation
            return super().calculate_take_profit(entry_price, side, ohlcv, signal_strength, market_volatility)
        
        # Calculate RSI
        rsi = self.calculate_rsi(ohlcv['close'], self.period)
        
        # Calculate RSI momentum (rate of change)
        if len(ohlcv) >= self.period + 2:
            prev_rsi = self.calculate_rsi(ohlcv['close'].iloc[:-1], self.period)
            rsi_momentum = abs(rsi - prev_rsi)
        else:
            rsi_momentum = 0.0
        
        # Base take profit percentage
        base_percentage = 0.06  # 6% base
        
        # Get configuration values
        rsi_config = self.config.get('rsi_strategy', {})
        oversold_threshold = rsi_config.get('oversold_threshold', 30)
        overbought_threshold = rsi_config.get('overbought_threshold', 70)
        neutral_threshold = rsi_config.get('neutral_threshold', 60)
        factor_oversold = rsi_config.get('factor_oversold', 1.3)
        factor_overbought = rsi_config.get('factor_overbought', 0.7)
        volatility_adjustment_max = rsi_config.get('volatility_adjustment_max', 0.3)
        
        # RSI-based adjustments
        rsi_factor = 1.0
        
        if side == 'buy':
            # For buy signals, higher take profit if RSI is oversold
            if rsi < oversold_threshold:
                rsi_factor = 1.5  # 50% increase for extreme oversold
            elif rsi < 40:
                rsi_factor = factor_oversold  # Configurable increase for oversold
            elif rsi > overbought_threshold:
                rsi_factor = factor_overbought  # Configurable decrease for overbought (shorter target)
        else:
            # For sell signals, higher take profit if RSI is overbought
            if rsi > overbought_threshold:
                rsi_factor = 1.5  # 50% increase for extreme overbought
            elif rsi > neutral_threshold:
                rsi_factor = factor_oversold  # Configurable increase for overbought
            elif rsi < oversold_threshold:
                rsi_factor = factor_overbought  # Configurable decrease for oversold (shorter target)
        
        # Momentum-based adjustment
        momentum_factor = 1.0 + (rsi_momentum * 0.5)  # Up to 50% increase for high momentum
        
        # Volatility adjustment
        volatility_factor = 1.0 + (market_volatility * volatility_adjustment_max)  # Up to max increase for high volatility
        
        # Final take profit percentage
        take_profit_percentage = base_percentage * rsi_factor * momentum_factor * volatility_factor * signal_strength
        
        # Ensure reasonable bounds (2% to 20%)
        take_profit_percentage = max(0.02, min(0.20, take_profit_percentage))
        
        if side == 'buy':
            return entry_price * (1 + take_profit_percentage)
        else:
            return entry_price * (1 - take_profit_percentage)
    
    def generate_signal(self, ohlcv: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """
        Generate trading signal based on OHLCV data using RSI.
        
        Args:
            ohlcv: OHLCV data DataFrame
            
        Returns:
            Signal dictionary or None if no signal
        """
        if ohlcv is None or len(ohlcv) < self.period + 1:
            return None
        
        # Calculate RSI
        rsi = self.calculate_rsi(ohlcv['close'], self.period)
        current_price = ohlcv['close'].iloc[-1]
        
        # Get previous RSI for divergence detection
        if len(ohlcv) >= self.period + 2:
            prev_rsi = self.calculate_rsi(ohlcv['close'].iloc[:-1], self.period)
        else:
            prev_rsi = rsi
        
        # Determine signal
        signal = 'hold'
        reason = ''
        
        # Oversold condition (potential buy signal)
        if rsi < self.oversold and prev_rsi >= self.oversold:
            signal = 'buy'
            reason = f'RSI oversold: {rsi:.2f} < {self.oversold} (bounce expected)'
        
        # Overbought condition (potential sell signal)
        elif rsi > self.overbought and prev_rsi <= self.overbought:
            signal = 'sell'
            reason = f'RSI overbought: {rsi:.2f} > {self.overbought} (correction expected)'
        
        # Strong oversold (extreme buy signal)
        elif rsi < 20:
            signal = 'buy'
            reason = f'RSI extremely oversold: {rsi:.2f} (strong buy signal)'
        
        # Strong overbought (extreme sell signal)
        elif rsi > 80:
            signal = 'sell'
            reason = f'RSI extremely overbought: {rsi:.2f} (strong sell signal)'
        
        if signal == 'hold':
            return None
        
        return {
            'signal': signal,
            'reason': reason,
            'price': current_price,
            'rsi': rsi,
            'strategy': 'rsi',
        }
    
    def calculate_rsi(self, prices: pd.Series, period: int = 14) -> float:
        """
        Calculate RSI (Relative Strength Index).
        
        Args:
            prices: Series of prices
            period: RSI period
            
        Returns:
            RSI value
        """
        if len(prices) < period + 1:
            return 50.0  # Neutral RSI if insufficient data
        
        # Calculate price changes
        delta = prices.diff()
        
        # Separate gains and losses
        gains = delta.where(delta > 0, 0)
        losses = -delta.where(delta < 0, 0)
        
        # Calculate average gains and losses
        avg_gains = gains.rolling(window=period).mean()
        avg_losses = losses.rolling(window=period).mean()
        
        # Calculate RS and RSI
        # Handle division by zero
        if avg_losses.iloc[-1] == 0:
            if avg_gains.iloc[-1] > 0:
                rsi = 100.0  # All gains, no losses
            else:
                rsi = 50.0   # No gains, no losses (neutral)
        else:
            rs = avg_gains / avg_losses
            rsi = 100 - (100 / (1 + rs))
        
        return rsi.iloc[-1]
    
    def analyze(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Analyze market data using RSI.
        
        Args:
            market_data: Market data dictionary
            
        Returns:
            Analysis results with signals
        """
        symbol = market_data['symbol']
        ohlcv = market_data['ohlcv']
        current_price = market_data['current_price']
        
        if ohlcv is None or len(ohlcv) < self.period + 1:
            return {
                'signal': 'hold',
                'reason': 'Insufficient data for RSI calculation',
                'rsi': None,
                'current_price': current_price,
            }
        
        # Calculate RSI
        rsi = self.calculate_rsi(ohlcv['close'], self.period)
        
        # Get previous RSI for divergence detection
        if len(ohlcv) >= self.period + 2:
            prev_rsi = self.calculate_rsi(ohlcv['close'].iloc[:-1], self.period)
        else:
            prev_rsi = rsi
        
        # Determine signal
        signal = 'hold'
        reason = ''
        
        # Oversold condition (potential buy signal)
        if rsi < self.oversold and prev_rsi >= self.oversold:
            signal = 'buy'
            reason = f'RSI oversold: {rsi:.2f} < {self.oversold} (bounce expected)'
        
        # Overbought condition (potential sell signal)
        elif rsi > self.overbought and prev_rsi <= self.overbought:
            signal = 'sell'
            reason = f'RSI overbought: {rsi:.2f} > {self.overbought} (correction expected)'
        
        # Strong oversold (extreme buy signal)
        elif rsi < 20:
            signal = 'buy'
            reason = f'RSI extremely oversold: {rsi:.2f} (strong buy signal)'
        
        # Strong overbought (extreme sell signal)
        elif rsi > 80:
            signal = 'sell'
            reason = f'RSI extremely overbought: {rsi:.2f} (strong sell signal)'
        
        # RSI divergence detection (simplified)
        elif len(ohlcv) >= 20:
            # Check for bullish divergence (price making lower lows, RSI making higher lows)
            recent_prices = ohlcv['close'].iloc[-10:]
            recent_rsi = [self.calculate_rsi(ohlcv['close'].iloc[:i+self.period], self.period) 
                         for i in range(len(ohlcv)-10, len(ohlcv))]
            
            if (recent_prices.iloc[-1] < recent_prices.iloc[0] and 
                recent_rsi[-1] > recent_rsi[0] and rsi < 40):
                signal = 'buy'
                reason = f'Bullish RSI divergence detected: RSI={rsi:.2f}'
        
        return {
            'signal': signal,
            'reason': reason,
            'rsi': rsi,
            'current_price': current_price,
            'period': self.period,
            'overbought': self.overbought,
            'oversold': self.oversold,
        }
    
    def should_buy(self, analysis: Dict[str, Any]) -> bool:
        """
        Determine if we should buy based on analysis.
        
        Args:
            analysis: Analysis results from analyze() method
            
        Returns:
            True if should buy, False otherwise
        """
        return analysis['signal'] == 'buy'
    
    def should_sell(self, analysis: Dict[str, Any]) -> bool:
        """
        Determine if we should sell based on analysis.
        
        Args:
            analysis: Analysis results from analyze() method
            
        Returns:
            True if should sell, False otherwise
        """
        return analysis['signal'] == 'sell' 