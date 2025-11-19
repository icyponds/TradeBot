"""
Correlation Manager for analyzing and identifying correlated asset pairs.
"""

import logging
import pandas as pd
import numpy as np
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timedelta

class CorrelationManager:
    """
    Manages the calculation and tracking of asset correlations.
    Identifies highly correlated pairs for statistical arbitrage.
    """
    
    def __init__(self, market_api, config: Dict[str, Any]):
        """
        Initialize the Correlation Manager.
        
        Args:
            market_api: Market API instance for fetching data
            config: Configuration dictionary
        """
        self.market_api = market_api
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # Configuration
        self.min_correlation = config.get('strategies', {}).get('stat_arb', {}).get('min_correlation', 0.8)
        self.lookback_period = config.get('strategies', {}).get('stat_arb', {}).get('correlation_lookback', 100)
        self.update_interval_hours = config.get('strategies', {}).get('stat_arb', {}).get('update_interval_hours', 24)
        
        # State
        self.correlated_pairs = {}  # {symbol: correlated_symbol}
        self.correlation_matrix = None
        self.last_update = None
        
    def update_correlations(self, symbols: List[str]) -> Dict[str, str]:
        """
        Update correlation data for the given symbols.
        
        Args:
            symbols: List of symbols to analyze
            
        Returns:
            Dictionary of best correlated pairs {symbol: correlated_symbol}
        """
        self.logger.info(f"Updating correlations for {len(symbols)} symbols...")
        
        if len(symbols) < 2:
            self.logger.warning("Not enough symbols to calculate correlations")
            return {}
            
        # Fetch historical data for all symbols
        price_data = {}
        
        # Use a longer timeframe for correlation analysis (e.g., 1h or 4h) to reduce noise
        # regardless of the trading timeframe
        analysis_timeframe = '1h' 
        
        for symbol in symbols:
            try:
                # Fetch enough data for robust correlation
                ohlcv = self.market_api.get_ohlcv(symbol, analysis_timeframe, self.lookback_period)
                if ohlcv is not None and not ohlcv.empty:
                    # Use closing prices
                    price_data[symbol] = ohlcv['close']
            except Exception as e:
                self.logger.error(f"Error fetching data for {symbol}: {e}")
                
        if len(price_data) < 2:
            self.logger.warning("Insufficient data for correlation analysis")
            return {}
            
        # Create DataFrame with all prices
        # Align data by index (timestamp) to ensure valid comparisons
        df = pd.DataFrame(price_data)
        
        # Drop rows with missing values to ensure clean correlation
        df = df.dropna()
        
        if len(df) < self.lookback_period * 0.5:
            self.logger.warning(f"Insufficient overlapping data points: {len(df)}")
            return {}
            
        # Calculate correlation matrix
        self.correlation_matrix = df.corr()
        
        # Find best pairs
        new_correlations = {}
        used_symbols = set()
        
        # Iterate through the matrix to find high correlations
        # We want to find the single best partner for each asset
        for symbol in self.correlation_matrix.columns:
            if symbol in used_symbols:
                continue
                
            # Get correlations for this symbol, sort descending
            correlations = self.correlation_matrix[symbol].sort_values(ascending=False)
            
            # Skip self-correlation (1.0)
            correlations = correlations[correlations.index != symbol]
            
            if correlations.empty:
                continue
                
            best_match = correlations.index[0]
            best_score = correlations.iloc[0]
            
            if best_score >= self.min_correlation:
                self.logger.info(f"Found correlated pair: {symbol} - {best_match} (Correlation: {best_score:.2f})")
                new_correlations[symbol] = best_match
                # We don't mark them as 'used' so multiple assets can correlate to a major one like BTC
                # But for pure pair trading, maybe we want unique pairs? 
                # For now, allowing many-to-one (e.g. everything correlates to BTC) is safer.
            
        self.correlated_pairs = new_correlations
        self.last_update = datetime.now()
        
        self.logger.info(f"Correlation update complete. Found {len(self.correlated_pairs)} correlated pairs.")
        return self.correlated_pairs
        
    def get_correlated_symbol(self, symbol: str) -> Optional[str]:
        """Get the correlated symbol for a given asset."""
        return self.correlated_pairs.get(symbol)
        
    def should_update(self) -> bool:
        """Check if correlations need to be updated."""
        if self.last_update is None:
            return True
            
        elapsed = datetime.now() - self.last_update
        return elapsed > timedelta(hours=self.update_interval_hours)

