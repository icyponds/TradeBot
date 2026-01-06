"""
Sentiment ML Strategy.

This strategy is designed to ingest external sentiment data (e.g., social volume,
NLP sentiment scores) and trade big shifts in sentiment.

Current Implementation:
- Uses a "Volume * Price Momentum" proxy to simulate "Social Hype" in the absence
  of an external API.
- Structure is ready for integration with LunarCrush / Santiment APIs.

Logic:
1. Calculate "Sentiment Score" (Proxy or API).
2. Detect extreme positive sentiment (Hype) -> Long.
3. Detect extreme negative sentiment (FUD) -> Short (or exit Long).
"""

import logging
from typing import Dict, Any, Optional, Tuple
import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy

class SentimentMLStrategy(BaseStrategy):
    """
    Sentiment ML Strategy.
    
    Trades based on Sentiment Analysis.
    """
    
    # 1h timeframe matches typical social sentiment aggregation
    PREFERRED_TIMEFRAME = '1h'
    
    def __init__(self, config: Dict[str, Any], timeframe: str = None):
        super().__init__(config, timeframe)
        
        # Strategy parameters from config
        sent_config = config.get('strategies', {}).get('sentiment_ml', {})
        
        # Thresholds (Z-Score of sentiment)
        # Entry when sentiment is > 2 std devs above mean
        self.sentiment_threshold = sent_config.get('sentiment_threshold', 2.0)
        
        # Lookback for Z-Score normalization
        self.normalization_lookback = sent_config.get('normalization_lookback', 24 * 7) # 1 week
        
        self.logger.info(f"Initialized Sentiment ML Strategy: Threshold={self.sentiment_threshold}")
    
    def generate_signal(self, symbol: str, ohlcv: Dict[str, pd.DataFrame]) -> Optional[Dict[str, Any]]:
        """
        Generate sentiment signal.
        """
        # Get preferred timeframe data
        tf_data = ohlcv.get(self.timeframe)
        if tf_data is None:
            if ohlcv:
                tf_data = next(iter(ohlcv.values()))
            else:
                return None
        
        return self._generate_signal_internal(tf_data, symbol)
    
    def _generate_signal_internal(self, ohlcv: pd.DataFrame, symbol: str) -> Optional[Dict[str, Any]]:
        """Internal signal generation logic."""
        
        if len(ohlcv) < self.normalization_lookback:
            return None
        
        # 1. Fetch Sentiment Score
        # TODO: Replace this call with fetching real data from External API
        sentiment_series = self._get_sentiment_proxy(ohlcv)
        
        if sentiment_series is None or len(sentiment_series) == 0:
            return None
            
        current_sentiment = sentiment_series.iloc[-1]
        
        # 2. Normalize Sentiment (Z-Score)
        # How unusual is this sentiment relative to the last week?
        rolling_mean = sentiment_series.rolling(window=self.normalization_lookback).mean()
        rolling_std = sentiment_series.rolling(window=self.normalization_lookback).std()
        
        mean_val = rolling_mean.iloc[-1]
        std_val = rolling_std.iloc[-1]
        
        if std_val == 0 or np.isnan(std_val):
            return None
            
        z_score = (current_sentiment - mean_val) / std_val
        
        signal = 'hold'
        reason = ''
        
        # 3. Generate Signals
        # High Positive Sentiment -> Long
        if z_score > self.sentiment_threshold:
            signal = 'buy'
            reason = f"Sentiment ML: Hype detected (Z-Score {z_score:.2f} > {self.sentiment_threshold})"
            
        # Extreme Negative Sentiment -> Short (FUD)
        elif z_score < -self.sentiment_threshold:
            signal = 'sell'
            reason = f"Sentiment ML: FUD detected (Z-Score {z_score:.2f} < -{self.sentiment_threshold})"
        
        if signal == 'hold':
            return None
            
        return {
            'signal': signal,
            'reason': reason,
            'price': ohlcv['close'].iloc[-1],
            'strategy': 'sentiment_ml',
            'sentiment_score': current_sentiment,
            'sentiment_zscore': z_score
        }

    def _get_sentiment_proxy(self, ohlcv: pd.DataFrame) -> pd.Series:
        """
        Generate a proxy for sentiment using Price and Volume.
        Assumption: High Volume + High Positive Return = Hype (High Sentiment).
                   High Volume + High Negative Return = FUD (Low Sentiment).
        """
        if 'volume' not in ohlcv.columns:
            return None
            
        closes = ohlcv['close']
        volumes = ohlcv['volume']
        
        # Calculate returns
        returns = closes.pct_change()
        
        # Normalize volume (Relative Volume)
        # vol_ma = volumes.rolling(window=24).mean()
        # rvol = volumes / vol_ma
        
        # Sentiment Proxy = Return * Volume
        # This gives very large positive numbers for high volume pumps
        # and very large negative numbers for high volume dumps.
        sentiment = returns * volumes
        
        # Smooth slighty
        sentiment = sentiment.rolling(window=3).mean()
        
        return sentiment

    def calculate_take_profit(self, entry_price: float, side: str, ohlcv: Dict[str, pd.DataFrame] = None,
                             signal_strength: float = 1.0, market_volatility: float = 1.0) -> float:
        """
        Sentiment moves are often short-lived hype spikes.
        Take profit relatively quickly.
        """
        tp_dist = entry_price * 0.05  # 5% pump
        
        if side == 'buy':
            return entry_price + tp_dist
        else:
            return entry_price - tp_dist

    def calculate_stop_loss(self, entry_price: float, side: str, 
                           signal_context: Dict[str, Any] = None) -> float:
        """
        If sentiment was wrong (price drops), get out fast.
        """
        sl_dist = entry_price * 0.03
        
        if side == 'buy':
            return entry_price - sl_dist
        else:
            return entry_price + sl_dist
            
    def get_trailing_stop_config(self) -> Dict[str, Any]:
        """
        Aggressive trailing stop to catch pumps and dump.
        """
        return {
            'enabled': True,
            'trail_pct': 0.02,         # 2% trail
            'activation_pct': 0.03,    # Activate after 3% gain
        }

    def should_exit(self, position: Any, current_price: float, 
                   current_data: Dict[str, Any] = None) -> Tuple[bool, Optional[str]]:
        """
        Exit if sentiment flips?
        """
        return False, None
    def calculate_signal_strength(self, ohlcv: Dict[str, pd.DataFrame], symbol: str = None, signal_context: Dict[str, Any] = None) -> float:
        """
        Calculate signal strength based on Sentiment Z-Score.
        
        Mapping:
        - Z-Score 2.0 (Entry) -> 0.5
        - Z-Score 4.0 (Max) -> 1.0
        """
        z_score = 0.0
        if signal_context and 'sentiment_zscore' in signal_context:
            z_score = abs(signal_context['sentiment_zscore'])
            
        if z_score < self.sentiment_threshold:
            return 0.5
            
        z_max = 4.0
        if z_score >= z_max:
            return 1.0
            
        return 0.5 + 0.5 * (z_score - self.sentiment_threshold) / (z_max - self.sentiment_threshold)
