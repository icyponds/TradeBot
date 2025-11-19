"""
Statistical Arbitrage Strategy.
"""

import logging
from typing import Dict, Any, Optional, List
import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy


class StatisticalArbitrageStrategy(BaseStrategy):
    """
    Statistical Arbitrage Strategy.
    
    This strategy identifies pairs of assets that are historically correlated
    and trades mean reversion on their spread/ratio.
    """
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        
        # Strategy parameters
        self.z_score_threshold = config['strategies'].get('stat_arb', {}).get('z_score_threshold', 2.0)
        self.window_size = config['strategies'].get('stat_arb', {}).get('window_size', 100)
        
        # We need a way to access other pairs' data. 
        # In a real implementation, the StrategyManager would pass a context with all market data.
        # For now, we'll assume we can request it or it's passed in.
        
        self.logger.info(f"Initialized Stat Arb Strategy: z_threshold={self.z_score_threshold}, window={self.window_size}")
        
        # Hardcoded correlations for demo (in production, this would be dynamic)
        self.correlated_pairs = {
            'ETH': 'BTC',
            'SOL': 'ETH',
            'AVAX': 'SOL',
            'MATIC': 'AVAX'
        }
    
    def calculate_z_score(self, series: pd.Series) -> float:
        """Calculate Z-Score of the last element."""
        mean = series.mean()
        std = series.std()
        if std == 0:
            return 0.0
        return (series.iloc[-1] - mean) / std
    
    def generate_signal(self, ohlcv: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """
        Generate trading signal based on OHLCV data.
        
        Note: This strategy requires data from a second asset (the pair).
        Since the standard interface only provides one OHLCV dataframe,
        this is a placeholder for the logic. The actual implementation
        would need the StrategyManager to provide the correlated asset's data.
        
        Args:
            ohlcv: OHLCV data DataFrame for the primary asset
            
        Returns:
            Signal dictionary or None
        """
        # This strategy is unique because it needs two assets.
        # The standard generate_signal signature doesn't support this easily without
        # external data access.
        
        # For now, we will return None to prevent errors, as this requires
        # a more complex integration with the StrategyManager to fetch the second pair.
        # See the implementation note below.
        return None

    def generate_pair_signal(self, symbol_a: str, ohlcv_a: pd.DataFrame, 
                           symbol_b: str, ohlcv_b: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """
        Generate signal for a pair of assets.
        
        Args:
            symbol_a: Primary symbol
            ohlcv_a: OHLCV data for primary symbol
            symbol_b: Correlated symbol
            ohlcv_b: OHLCV data for correlated symbol
            
        Returns:
            Signal dictionary
        """
        if len(ohlcv_a) != len(ohlcv_b):
            # Align data lengths
            min_len = min(len(ohlcv_a), len(ohlcv_b))
            ohlcv_a = ohlcv_a.iloc[-min_len:]
            ohlcv_b = ohlcv_b.iloc[-min_len:]
            
        if len(ohlcv_a) < self.window_size:
            return None
            
        # Calculate Spread Ratio
        ratio = ohlcv_a['close'] / ohlcv_b['close']
        
        # Calculate Z-Score of the ratio
        z_score = self.calculate_z_score(ratio.rolling(window=self.window_size).apply(lambda x: x[-1]))
        
        current_price_a = ohlcv_a['close'].iloc[-1]
        
        signal = 'hold'
        reason = ''
        
        # Mean Reversion Logic
        # If Ratio is too high (Asset A is expensive relative to B), Short A
        if z_score > self.z_score_threshold:
            signal = 'sell'
            reason = f'Stat Arb: {symbol_a}/{symbol_b} Ratio Z-Score {z_score:.2f} > {self.z_score_threshold} (Short {symbol_a})'
            
        # If Ratio is too low (Asset A is cheap relative to B), Long A
        elif z_score < -self.z_score_threshold:
            signal = 'buy'
            reason = f'Stat Arb: {symbol_a}/{symbol_b} Ratio Z-Score {z_score:.2f} < -{self.z_score_threshold} (Long {symbol_a})'
            
        if signal == 'hold':
            return None
            
        return {
            'signal': signal,
            'reason': reason,
            'price': current_price_a,
            'strategy': 'stat_arb',
            'pair_symbol': symbol_b,
            'z_score': z_score
        }

