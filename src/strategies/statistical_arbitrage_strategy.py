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
    
    def __init__(self, config: Dict[str, Any], market_api=None, correlation_manager=None):
        super().__init__(config)
        
        # Strategy parameters
        self.z_score_threshold = config['strategies'].get('stat_arb', {}).get('z_score_threshold', 2.0)
        self.window_size = config['strategies'].get('stat_arb', {}).get('window_size', 100)
        
        # Dependencies
        self.market_api = market_api
        self.correlation_manager = correlation_manager
        
        self.logger.info(f"Initialized Stat Arb Strategy: z_threshold={self.z_score_threshold}, window={self.window_size}")
    
    def set_dependencies(self, market_api, correlation_manager):
        """Set external dependencies."""
        self.market_api = market_api
        self.correlation_manager = correlation_manager
        
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
        
        Args:
            ohlcv: OHLCV data DataFrame for the primary asset
            
        Returns:
            Signal dictionary or None
        """
        if not self.market_api or not self.correlation_manager:
            self.logger.warning("Stat Arb strategy missing dependencies (market_api or correlation_manager)")
            return None
            
        # Get the symbol from the OHLCV data (assuming it's passed or we can infer it)
        # Since we don't have the symbol in the args, we need to rely on the caller
        # But wait, the caller (StrategyManager) calls this method.
        # We need to change the signature or use a workaround.
        # Actually, StrategyManager calls generate_signal(ohlcv).
        # We can't easily get the symbol from just the dataframe unless it's in the columns.
        
        # WORKAROUND: We will return None here and rely on a specialized method 
        # that StrategyManager should call for this strategy, OR we update StrategyManager
        # to pass the symbol.
        
        # For now, let's assume StrategyManager will be updated to call a specific method
        # or we can't proceed.
        
        # However, looking at StrategyManager._execute_strategy:
        # signal = strategy.generate_signal(ohlcv)
        
        # We need to update StrategyManager to pass the symbol to generate_signal
        # or use a different method.
        
        return None

    def generate_signal_with_symbol(self, symbol: str, ohlcv: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """
        Generate signal with symbol context.
        
        Args:
            symbol: Current symbol being analyzed
            ohlcv: OHLCV data for the symbol
            
        Returns:
            Signal dictionary or None
        """
        if not self.market_api or not self.correlation_manager:
            return None
            
        # Get correlated pair
        correlated_symbol = self.correlation_manager.get_correlated_symbol(symbol)
        
        if not correlated_symbol:
            # No correlation found for this symbol
            return None
            
        # Fetch data for correlated symbol
        # Use the same timeframe and limit as the primary symbol
        # We can infer timeframe/limit from the config or the ohlcv length
        limit = len(ohlcv)
        timeframe = self.config['strategies']['timeframe']
        
        try:
            ohlcv_pair = self.market_api.get_ohlcv(correlated_symbol, timeframe, limit)
            
            if ohlcv_pair is None or len(ohlcv_pair) < self.window_size:
                return None
                
            return self.generate_pair_signal(symbol, ohlcv, correlated_symbol, ohlcv_pair)
            
        except Exception as e:
            self.logger.error(f"Error fetching correlated data for {correlated_symbol}: {e}")
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
        # We use Close prices
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
