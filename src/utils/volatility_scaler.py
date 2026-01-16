"""
Volatility Scaler: Computes per-asset relative volatility (ATR ratio) for threshold scaling.

This module provides dynamic volatility-based scaling factors for strategy parameters,
allowing z-score thresholds to adapt to each asset's volatility characteristics.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import numpy as np
import pandas as pd


logger = logging.getLogger(__name__)


class VolatilityScaler:
    """
    Computes relative volatility (ATR ratio) for each asset.
    
    The ATR ratio is calculated as:
        vol_multiplier(asset) = asset_ATR / median_ATR_all_assets
    
    This allows strategies to scale their z-score thresholds based on
    each asset's volatility relative to the universe.
    
    Example:
        - SOL's ATR is 1.5× the median → vol_multiplier = 1.5
        - Effective z_entry for SOL = base_z × 1.5 = 3.0 (if base is 2.0)
    """
    
    def __init__(
        self,
        lookback: int = 14,
        min_multiplier: float = 0.8,
        max_multiplier: float = 1.5,
    ):
        """
        Initialize the VolatilityScaler.
        
        Args:
            lookback: Number of bars for ATR calculation
            min_multiplier: Minimum allowed multiplier (floor)
            max_multiplier: Maximum allowed multiplier (ceiling)
        """
        self.lookback = lookback
        self.min_multiplier = min_multiplier
        self.max_multiplier = max_multiplier
        
        # Per-symbol ATR values
        self._atr_values: Dict[str, float] = {}
        
        # Cached median ATR
        self._median_atr: float = 0.0
        
        # Per-symbol multipliers (cached)
        self._multipliers: Dict[str, float] = {}
    
    def _calculate_atr(self, ohlcv: pd.DataFrame) -> float:
        """
        Calculate Average True Range for a single asset.
        
        Args:
            ohlcv: DataFrame with 'high', 'low', 'close' columns
            
        Returns:
            ATR value or 0.0 if insufficient data
        """
        if ohlcv is None or len(ohlcv) < self.lookback + 1:
            return 0.0
        
        try:
            high = ohlcv['high'].astype(float)
            low = ohlcv['low'].astype(float)
            close = ohlcv['close'].astype(float)
            
            # True Range = max(high - low, |high - prev_close|, |low - prev_close|)
            prev_close = close.shift(1)
            
            tr1 = high - low
            tr2 = (high - prev_close).abs()
            tr3 = (low - prev_close).abs()
            
            true_range = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            
            # Average True Range over lookback period
            atr = true_range.iloc[-self.lookback:].mean()
            
            return float(atr) if not np.isnan(atr) else 0.0
            
        except Exception as e:
            logger.debug(f"Error calculating ATR: {e}")
            return 0.0
    
    def update(
        self,
        symbols: List[str],
        ohlcv_getter,
        timeframe: str = "15m",
    ) -> None:
        """
        Update ATR values for all symbols and recalculate multipliers.
        
        Args:
            symbols: List of symbols to update
            ohlcv_getter: Callable that takes (symbol, timeframe, limit) and returns DataFrame
            timeframe: Timeframe for OHLCV data
        """
        self._atr_values.clear()
        self._multipliers.clear()
        
        for symbol in symbols:
            try:
                ohlcv = ohlcv_getter(symbol, timeframe, limit=self.lookback + 5)
                atr = self._calculate_atr(ohlcv)
                if atr > 0:
                    self._atr_values[symbol] = atr
            except Exception as e:
                logger.debug(f"Failed to calculate ATR for {symbol}: {e}")
        
        # Calculate median ATR
        if self._atr_values:
            self._median_atr = float(np.median(list(self._atr_values.values())))
        else:
            self._median_atr = 0.0
        
        # Calculate multipliers
        if self._median_atr > 0:
            for symbol, atr in self._atr_values.items():
                raw_ratio = atr / self._median_atr
                # Clamp to [min, max]
                clamped = max(self.min_multiplier, min(self.max_multiplier, raw_ratio))
                self._multipliers[symbol] = clamped
        
        logger.debug(
            f"VolatilityScaler updated: {len(self._multipliers)} symbols, "
            f"median_ATR={self._median_atr:.6f}"
        )
    
    def get_ratio(self, symbol: str) -> float:
        """
        Get the volatility multiplier for a symbol.
        
        Args:
            symbol: Trading symbol
            
        Returns:
            Volatility multiplier (1.0 if not calculated)
        """
        return self._multipliers.get(symbol, 1.0)
    
    def get_atr(self, symbol: str) -> float:
        """Get raw ATR value for a symbol."""
        return self._atr_values.get(symbol, 0.0)
    
    def get_median_atr(self) -> float:
        """Get the current median ATR across all symbols."""
        return self._median_atr
    
    def get_all_ratios(self) -> Dict[str, float]:
        """Get all calculated volatility ratios."""
        return self._multipliers.copy()
    
    def get_status_summary(self) -> Dict:
        """Get a summary of the scaler status."""
        ratios = list(self._multipliers.values())
        return {
            "symbol_count": len(self._multipliers),
            "median_atr": self._median_atr,
            "ratio_min": min(ratios) if ratios else 0.0,
            "ratio_max": max(ratios) if ratios else 0.0,
            "ratio_mean": float(np.mean(ratios)) if ratios else 1.0,
        }
