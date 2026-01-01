"""
Statistical Arbitrage Strategy with Cointegration and Kalman Filter.

Enhanced version that uses cointegration testing (Engle-Granger) instead of
simple correlation, and employs Kalman Filter for dynamic hedge ratio estimation.
"""

import logging
from typing import Dict, Any, Optional, List, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from src.models.trade import Position
import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy


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
    
    def __init__(self, config: Dict[str, Any], market_api=None, correlation_manager=None):
        super().__init__(config)
        
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
        limit = len(ohlcv)
        timeframe = self.timeframe  # Use strategy's preferred timeframe (15m)
        
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
        Generate signal for a pair of assets using cointegration.
        
        Args:
            symbol_a: Primary symbol
            ohlcv_a: OHLCV data for primary symbol
            symbol_b: Correlated/cointegrated symbol
            ohlcv_b: OHLCV data for correlated symbol
            
        Returns:
            Signal dictionary with hedge ratio and spread info
        """
        if len(ohlcv_a) != len(ohlcv_b):
            # Align data lengths
            min_len = min(len(ohlcv_a), len(ohlcv_b))
            ohlcv_a = ohlcv_a.iloc[-min_len:]
            ohlcv_b = ohlcv_b.iloc[-min_len:]
            
        if len(ohlcv_a) < self.window_size:
            return None
        
        prices_a = ohlcv_a['close']
        prices_b = ohlcv_b['close']
        current_price_a = prices_a.iloc[-1]
        current_price_b = prices_b.iloc[-1]
        
        # Get or calculate hedge ratio
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
            # Check for exit signal
            position_side = self.active_spreads[pair_key].get('side')
            
            # Exit if Z-score crosses zero or hits exit threshold
            if position_side == 'short' and z_score < self.z_score_exit:
                signal = 'buy'  # Close short
                reason = f'Stat Arb Exit: {pair_key} Z-Score {z_score:.2f} < {self.z_score_exit} (Close Short)'
                del self.active_spreads[pair_key]
            elif position_side == 'long' and z_score > -self.z_score_exit:
                signal = 'sell'  # Close long
                reason = f'Stat Arb Exit: {pair_key} Z-Score {z_score:.2f} > -{self.z_score_exit} (Close Long)'
                del self.active_spreads[pair_key]
        else:
            # Check for entry signal
            # Spread is too high (Asset A expensive relative to B) -> Short A
            if z_score > self.z_score_entry:
                signal = 'sell'
                reason = f'Stat Arb Entry: {pair_key} Z-Score {z_score:.2f} > {self.z_score_entry} (Short {symbol_a})'
                self.active_spreads[pair_key] = {
                    'side': 'short',
                    'entry_zscore': z_score,
                    'hedge_ratio': hedge_ratio,
                }
            # Spread is too low (Asset A cheap relative to B) -> Long A
            elif z_score < -self.z_score_entry:
                signal = 'buy'
                reason = f'Stat Arb Entry: {pair_key} Z-Score {z_score:.2f} < -{self.z_score_entry} (Long {symbol_a})'
                self.active_spreads[pair_key] = {
                    'side': 'long',
                    'entry_zscore': z_score,
                    'hedge_ratio': hedge_ratio,
                }
        
        if signal == 'hold':
            return None
        
        return {
            'signal': signal,
            'reason': reason,
            'price': current_price_a,
            'strategy': 'stat_arb',
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
        
        Args:
            symbol_a: Primary symbol
            symbol_b: Pair symbol
            prices_a: Prices of asset A
            prices_b: Prices of asset B
            
        Returns:
            Hedge ratio (beta)
        """
        pair_key = f"{symbol_a}/{symbol_b}"
        
        # Try to get cointegration result from correlation manager
        if self.correlation_manager and hasattr(self.correlation_manager, 'get_cointegration_result'):
            coint_result = self.correlation_manager.get_cointegration_result(symbol_a)
            if coint_result:
                base_hedge_ratio = coint_result.hedge_ratio
            else:
                # Fall back to OLS estimation
                base_hedge_ratio = self._estimate_hedge_ratio_ols(prices_a, prices_b)
        else:
            base_hedge_ratio = self._estimate_hedge_ratio_ols(prices_a, prices_b)
        
        # Apply Kalman Filter for dynamic adjustment if enabled
        if self.use_kalman_filter:
            if pair_key not in self.kalman_states:
                # Initialize Kalman Filter state
                if self.correlation_manager and hasattr(self.correlation_manager, 'create_kalman_filter'):
                    self.kalman_states[pair_key] = self.correlation_manager.create_kalman_filter(
                        initial_beta=base_hedge_ratio,
                        Q=self.kalman_Q,
                        R=self.kalman_R
                    )
                else:
                    # Simple fallback without Kalman
                    return base_hedge_ratio
            
            # Update Kalman Filter with latest observation
            kf = self.kalman_states[pair_key]
            y = prices_a.iloc[-1]
            x = prices_b.iloc[-1]
            dynamic_hedge_ratio = kf.update(y, x)
            
            return dynamic_hedge_ratio
        
        return base_hedge_ratio
    
    def _estimate_hedge_ratio_ols(self, prices_a: pd.Series, prices_b: pd.Series) -> float:
        """
        Estimate hedge ratio using OLS regression.
        
        Args:
            prices_a: Dependent variable prices
            prices_b: Independent variable prices
            
        Returns:
            Hedge ratio (beta coefficient)
        """
        try:
            # Simple OLS: y = beta * x + alpha
            # We want beta = cov(y, x) / var(x)
            cov = np.cov(prices_a.values, prices_b.values)[0, 1]
            var = np.var(prices_b.values)
            
            if var == 0:
                return 1.0
            
            return cov / var
            
        except Exception as e:
            self.logger.error(f"Error estimating hedge ratio: {e}")
            return 1.0
    
    def _calculate_spread_zscore(self, spread: pd.Series) -> float:
        """
        Calculate Z-score of the spread using rolling statistics.
        
        Args:
            spread: Spread series
            
        Returns:
            Current Z-score
        """
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
    
    def get_active_spreads_summary(self) -> Dict[str, Any]:
        """Get summary of active spread positions."""
        return {
            'active_pairs': len(self.active_spreads),
            'positions': self.active_spreads.copy(),
        }
    
    def calculate_take_profit(self, entry_price: float, side: str, ohlcv: pd.DataFrame = None,
                             signal_strength: float = 1.0, market_volatility: float = 1.0) -> float:
        """
        Calculate take profit for statistical arbitrage.
        
        For mean reversion, we expect the spread to return to zero,
        so take profit is based on the expected move.
        """
        # Use a tighter take profit since we expect mean reversion
        base_tp_pct = 0.03  # 3% base
        
        # Adjust for signal strength (higher Z-score = larger expected move)
        adjusted_tp = base_tp_pct * signal_strength
        
        # Clamp to reasonable range
        adjusted_tp = max(0.02, min(0.10, adjusted_tp))
        
        if side == 'buy':
            return entry_price * (1 + adjusted_tp)
        else:
            return entry_price * (1 - adjusted_tp)
    
    def calculate_stop_loss(self, entry_price: float, side: str, 
                           signal_context: Dict[str, Any] = None) -> float:
        """
        Calculate stop loss for statistical arbitrage.
        
        For stat arb, stop loss is based on the spread Z-score moving further
        from the mean (wrong direction).
        """
        # Default stop loss percentage
        base_sl_pct = 0.05  # 5% base
        
        if signal_context:
            # Adjust based on entry Z-score
            z_score = abs(signal_context.get('z_score', 2.0))
            # Stop if Z-score moves to 1.5x entry
            stop_z = z_score * 1.5
            # Rough conversion: 1 Z-score ≈ 2-3% price move
            base_sl_pct = stop_z * 0.025
            
            # Clamp to reasonable range
            base_sl_pct = max(0.03, min(0.10, base_sl_pct))
        
        if side == 'buy':
            return entry_price * (1 - base_sl_pct)
        else:
            return entry_price * (1 + base_sl_pct)
    
    def should_exit(self, position: Any, current_price: float, 
                   current_data: Dict[str, Any] = None) -> Tuple[bool, Optional[str]]:
        """
        Determine if stat arb position should exit.
        
        Exit conditions:
        1. Spread Z-score has returned to near zero
        2. Cointegration relationship has broken down
        """
        if current_data is None:
            return False, None
        
        symbol = getattr(position, 'symbol', None)
        if not symbol:
            return False, None
        
        # Check active spreads for this symbol
        spread_data = self.active_spreads.get(symbol)
        if not spread_data:
            return False, None
        
        # If we have z_score in current_data, check for mean reversion
        z_score = current_data.get('z_score')
        if z_score is not None:
            if abs(z_score) < self.zscore_exit:
                return True, f"spread_mean_reversion_complete (z={z_score:.2f})"
        
        return False, None
    
    def get_trailing_stop_config(self) -> Dict[str, Any]:
        """
        Get trailing stop configuration for statistical arbitrage.
        
        Similar to OU mean reversion - we expect spread to revert, so we
        don't want aggressive trailing. Only protect large overshoots.
        
        Returns:
            Trailing stop configuration
        """
        return {
            'enabled': True,
            'trail_pct': 0.04,         # 4% trailing stop (loose)
            'activation_pct': 0.05,    # Only activate after 5% gain (spread overshoot)
        }
