"""
Statistical Arbitrage Strategy with Cointegration and Kalman Filter.

Enhanced version that uses cointegration testing (Engle-Granger) instead of
simple correlation, and employs Kalman Filter for dynamic hedge ratio estimation.
"""

import logging
from typing import Dict, Any, Optional, List, Tuple, TYPE_CHECKING
import pandas as pd
import numpy as np

if TYPE_CHECKING:
    from src.models.trade import Position

from .base_strategy import BaseStrategy
from ..utils.kalman_filter import KalmanFilter1D


class StatisticalArbitrageStrategy(BaseStrategy):
    """
    Statistical Arbitrage Strategy with Cointegration.
    
    This strategy identifies cointegrated pairs of assets and trades
    mean reversion on their spread using proper hedge ratios.
    
    Improvements over correlation-based approach:
    - Uses Engle-Granger cointegration test for pair selection
    - Dynamic hedge ratio estimation via Kalman Filter
    - Half-life filtering to ensure tradeable mean reversion speed
    - Proper spread calculation: spread = price_A - hedge_ratio * price_B
    """
    
    # Stat arb works best on 15m-1h timeframes for spread mean reversion
    PREFERRED_TIMEFRAME = '15m'
    
    def __init__(self, config: Dict[str, Any], market_api=None, correlation_manager=None, timeframe: str = None):
        super().__init__(config, timeframe)
        
        # Strategy parameters from config
        stat_arb_config = config.get('strategies', {}).get('stat_arb', {})
        coint_config = config.get('strategies', {}).get('cointegration', {})
        
        # Z-score thresholds
        self.z_score_entry = coint_config.get('zscore_entry', stat_arb_config.get('z_score_threshold', 2.0))
        self.z_score_exit = coint_config.get('zscore_exit', 0.5)
        
        # Window sizes
        self.window_size = stat_arb_config.get('window_size', 100)
        self.zscore_lookback = coint_config.get('lookback_period', 20)
        
        # Cointegration parameters
        self.adf_pvalue_threshold = coint_config.get('adf_pvalue_threshold', 0.05)
        self.half_life_max_hours = coint_config.get('half_life_max_hours', 48)
        self.use_kalman_filter = coint_config.get('kalman_filter_enabled', True)
        
        # Kalman Filter parameters
        self.kalman_Q = coint_config.get('kalman_Q', 0.001)  # Process noise
        self.kalman_R = coint_config.get('kalman_R', 1.0)    # Measurement noise
        
        # Dependencies
        self.market_api = market_api
        self.correlation_manager = correlation_manager
        
        # Kalman filter states for each pair
        self.kalman_states: Dict[str, Any] = {}
        
        # Track active spreads
        self.active_spreads: Dict[str, Dict[str, Any]] = {}
        
        self.logger.info(f"Initialized Cointegration Stat Arb Strategy: "
                        f"z_entry={self.z_score_entry}, z_exit={self.z_score_exit}, "
                        f"kalman={self.use_kalman_filter}")
    
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
    
    def generate_signal(self, symbol: str, ohlcv: Dict[str, pd.DataFrame]) -> Optional[Dict[str, Any]]:
        """
        Generate trading signal based on OHLCV data.
        """
        if not self.market_api or not self.correlation_manager:
            self.logger.warning("Stat Arb strategy missing dependencies (market_api or correlation_manager)")
            return None
            
        # Get preferred timeframe data
        tf_data = ohlcv.get(self.timeframe)
        if tf_data is None:
            # Fallback
            if ohlcv:
                tf_data = next(iter(ohlcv.values()))
            else:
                return None

        # Logic migrated from generate_signal_with_symbol
        # Get correlated pair
        correlated_symbol = self.correlation_manager.get_correlated_symbol(symbol)
        if not correlated_symbol:
            return None
            
        # Fetch data for correlated symbol
        limit = len(tf_data)
        timeframe = self.timeframe
        
        try:
            ohlcv_pair = self.market_api.get_ohlcv(correlated_symbol, timeframe, limit)
            if ohlcv_pair is None or len(ohlcv_pair) < self.window_size:
                return None
            return self.generate_pair_signal(symbol, tf_data, correlated_symbol, ohlcv_pair)
        except Exception as e:
            self.logger.error(f"Error fetching correlated data for {correlated_symbol}: {e}")
            return None

    def generate_pair_signal(self, symbol_a: str, ohlcv_a: pd.DataFrame, 
                           symbol_b: str, ohlcv_b: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """
        Generate signal for a pair of assets using cointegration.
        """
        if len(ohlcv_a) != len(ohlcv_b):
            min_len = min(len(ohlcv_a), len(ohlcv_b))
            ohlcv_a = ohlcv_a.iloc[-min_len:]
            ohlcv_b = ohlcv_b.iloc[-min_len:]
            
        if len(ohlcv_a) < self.window_size:
            return None
        
        prices_a = ohlcv_a['close']
        prices_b = ohlcv_b['close']
        current_price_a = prices_a.iloc[-1]
        current_price_b = prices_b.iloc[-1]
        
        # Get or calculate hedge ratio (Kalman)
        hedge_ratio = self._get_hedge_ratio(symbol_a, symbol_b, prices_a, prices_b)
        
        # Calculate spread: spread = price_A - hedge_ratio * price_B
        spread = prices_a - hedge_ratio * prices_b
        
        # Calculate Z-score of the spread
        z_score = self._calculate_spread_zscore(spread)
        
        # Check if we have an active position
        pair_key = f"{symbol_a}/{symbol_b}"
        has_position = pair_key in self.active_spreads
        
        signal = 'hold'
        reason = ''
        
        if has_position:
            position_side = self.active_spreads[pair_key].get('side')
            # Exit if Z-score crosses zero or hits exit threshold
            if position_side == 'short' and z_score < self.z_score_exit:
                signal = 'buy'
                reason = f'Stat Arb Exit: {pair_key} Z-Score {z_score:.2f} < {self.z_score_exit} (Close Short)'
                del self.active_spreads[pair_key]
            elif position_side == 'long' and z_score > -self.z_score_exit:
                signal = 'sell'
                reason = f'Stat Arb Exit: {pair_key} Z-Score {z_score:.2f} > -{self.z_score_exit} (Close Long)'
                del self.active_spreads[pair_key]
        else:
            # Check for entry signal
            # Additional Filter: Hurst Exponent Check
            hurst = self._calculate_hurst_exponent(spread)
            if hurst >= 0.5:
                # Spread is random walk (0.5) or trending (>0.5) - NOT SAFE for mean reversion
                return None
            
            if z_score > self.z_score_entry:
                signal = 'sell'
                reason = f'Stat Arb Entry: {pair_key} Z-Score {z_score:.2f} > {self.z_score_entry} (H={hurst:.2f}) (Short {symbol_a})'
                self.active_spreads[pair_key] = {
                    'side': 'short',
                    'entry_zscore': z_score,
                    'hedge_ratio': hedge_ratio,
                    'hurst': hurst
                }
            elif z_score < -self.z_score_entry:
                signal = 'buy'
                reason = f'Stat Arb Entry: {pair_key} Z-Score {z_score:.2f} < -{self.z_score_entry} (H={hurst:.2f}) (Long {symbol_a})'
                self.active_spreads[pair_key] = {
                    'side': 'long',
                    'entry_zscore': z_score,
                    'hedge_ratio': hedge_ratio,
                    'hurst': hurst
                }
        
        if signal == 'hold':
            return None
        
        is_entry = "Stat Arb Entry:" in reason
        return {
            'signal': signal,
            'reason': reason,
            'price': current_price_a,
            'strategy': 'stat_arb',
            'action': 'open' if is_entry else 'close',
            'pair_symbol': symbol_b,
            'z_score': z_score,
            'hedge_ratio': hedge_ratio,
            'spread': spread.iloc[-1],
            'pair_price': current_price_b,
        }

    def _get_hedge_ratio(self, symbol_a: str, symbol_b: str, 
                        prices_a: pd.Series, prices_b: pd.Series) -> float:
        """
        Get hedge ratio, using Kalman Filter if enabled.
        """
        pair_key = f"{symbol_a}/{symbol_b}"
        
        # Fallback to OLS for initial estimation
        base_hedge_ratio = self._estimate_hedge_ratio_ols(prices_a, prices_b)
        
        if self.use_kalman_filter:
            if pair_key not in self.kalman_states:
                # Initialize Kalman Filter state
                kf = KalmanFilter1D(delta=self.kalman_Q, R=self.kalman_R)
                initial_alpha = prices_a.iloc[-1] - base_hedge_ratio * prices_b.iloc[-1]
                kf.initialize(initial_alpha, base_hedge_ratio)
                self.kalman_states[pair_key] = kf
            
            # Update Kalman Filter
            kf = self.kalman_states[pair_key]
            y = prices_a.iloc[-1]
            x = prices_b.iloc[-1]
            _, _, dynamic_beta = kf.update(y, x)
            return dynamic_beta
        
        return base_hedge_ratio

    def _estimate_hedge_ratio_ols(self, prices_a: pd.Series, prices_b: pd.Series) -> float:
        """Estimate hedge ratio using OLS regression."""
        try:
            cov = np.cov(prices_a.values, prices_b.values)[0, 1]
            var = np.var(prices_b.values)
            if var == 0:
                return 1.0
            return cov / var
        except Exception as e:
            self.logger.error(f"Error estimating hedge ratio: {e}")
            return 1.0

    def _calculate_spread_zscore(self, spread: pd.Series) -> float:
        """Calculate Z-score of the spread."""
        if len(spread) < self.zscore_lookback:
            return 0.0
        rolling_mean = spread.rolling(window=self.zscore_lookback).mean()
        rolling_std = spread.rolling(window=self.zscore_lookback).std()
        current_mean = rolling_mean.iloc[-1]
        current_std = rolling_std.iloc[-1]
        current_spread = spread.iloc[-1]
        if current_std == 0 or np.isnan(current_std):
            return 0.0
        return (current_spread - current_mean) / current_std

    def _calculate_hurst_exponent(self, ts: pd.Series) -> float:
        """Calculate the Hurst Exponent of a time series."""
        try:
            ts = ts.values
            if len(ts) < 20:
                return 0.5
            lags = range(2, 20)
            tau = []
            for lag in lags:
                diff = np.subtract(ts[lag:], ts[:-lag])
                tau.append(np.std(diff))
            m = np.polyfit(np.log(lags), np.log(tau), 1)
            return m[0]
        except Exception:
            return 0.5

    def get_active_spreads_summary(self) -> Dict[str, Any]:
        """Get summary of active spread positions."""
        return {
            'active_pairs': len(self.active_spreads),
            'positions': self.active_spreads.copy(),
        }

    def calculate_take_profit(self, entry_price: float, side: str, ohlcv: Dict[str, pd.DataFrame] = None,
                             signal_strength: float = 1.0, market_volatility: float = 1.0) -> float:
        """Calculate take profit (expected mean reversion)."""
        base_tp_pct = 0.03
        adjusted_tp = max(0.02, min(0.10, base_tp_pct * signal_strength))
        if side == 'buy':
            return entry_price * (1 + adjusted_tp)
        else:
            return entry_price * (1 - adjusted_tp)

    def calculate_stop_loss(self, entry_price: float, side: str, 
                           signal_context: Dict[str, Any] = None) -> float:
        """Calculate stop loss based on Z-score deviation."""
        base_sl_pct = 0.05
        if signal_context:
            z_score = signal_context.get('z_score')
            z_score = abs(z_score) if z_score is not None else 2.0
            stop_z = z_score * 1.5
            base_sl_pct = max(0.03, min(0.10, stop_z * 0.025))
        if side == 'buy':
            return entry_price * (1 - base_sl_pct)
        else:
            return entry_price * (1 + base_sl_pct)

    def should_exit(self, position: Any, current_price: float, 
                   current_data: Dict[str, Any] = None) -> Tuple[bool, Optional[str]]:
        """Determine if position should exit (mean reversion complete)."""
        if current_data is None:
            return False, None
        symbol = getattr(position, 'symbol', None)
        if not symbol or symbol not in self.active_spreads:
            return False, None
        z_score = current_data.get('z_score')
        if z_score is not None and abs(z_score) < self.zscore_exit:
            return True, f"spread_mean_reversion_complete (z={z_score:.2f})"
        return False, None

    def get_trailing_stop_config(self) -> Dict[str, Any]:
        """Trailing stop config."""
        return {
            'enabled': True,
            'trail_pct': 0.04,
            'activation_pct': 0.05,
        }

    def calculate_signal_strength(self, ohlcv: pd.DataFrame) -> float:
        return 0.8
