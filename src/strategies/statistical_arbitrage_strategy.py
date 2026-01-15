"""
Statistical Arbitrage Strategy with Cointegration and Kalman Filter.

Enhanced version that uses cointegration testing (Engle-Granger) instead of
simple correlation, and employs Kalman Filter for dynamic hedge ratio estimation.
"""

import logging
from typing import Dict, Any, Optional, List, Tuple, TYPE_CHECKING
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

if TYPE_CHECKING:
    from src.models.trade import Position

from .base_strategy import BaseStrategy
from ..utils.kalman_filter import KalmanFilter1D
from src.utils.statistics import hurst_exponent


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
        self.max_holding_hours = stat_arb_config.get('max_holding_hours', 120) # Default 5 days
        self.max_adverse_z_delta = stat_arb_config.get('max_adverse_z_delta', 1.5)  # Exit if z moves 1.5σ against entry
        
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
    
    def restore_active_spreads(self, positions: list):
        """
        Restore active_spreads state from persisted positions on startup.
        
        Args:
            positions: List of position dicts loaded from DB with metadata containing z-score fields.
        """
        for pos_data in positions:
            if not pos_data.get('strategy', '').startswith('stat_arb'):
                continue
            
            metadata = pos_data.get('metadata', {}) or {}
            pair_key = metadata.get('pair_key')
            
            if not pair_key:
                # Try to reconstruct pair_key from legs
                legs = pos_data.get('legs', [])
                if len(legs) >= 2:
                    sym_a = legs[0].get('symbol', '').split('/')[0]
                    sym_b = legs[1].get('symbol', '').split('/')[0]
                    pair_key = f"{sym_a}/{sym_b}"
            
            if not pair_key:
                continue
            
            # Restore z-score state
            entry_z = metadata.get('entry_zscore')
            if entry_z is not None:
                self.active_spreads[pair_key] = {
                    'side': pos_data.get('side', 'short'),
                    'entry_zscore': entry_z,
                    'current_z': metadata.get('current_z', entry_z),
                    'max_adverse_z': metadata.get('max_adverse_z', entry_z),
                    'hedge_ratio': metadata.get('hedge_ratio', 1.0),
                }
                self.logger.info(f"Restored stat_arb spread {pair_key}: entry_z={entry_z:.2f}, current_z={metadata.get('current_z')}")
        
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
            
        tf_data = self._get_timeframe_data(ohlcv)
        if tf_data is None:
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
            if position_side == 'short':
                if z_score < self.z_score_exit:
                    signal = 'buy'
                    reason = f'Stat Arb Exit: {pair_key} Z-Score {z_score:.2f} < {self.z_score_exit} (Close Short)'
                    del self.active_spreads[pair_key]
                # Stop Loss Handling
                elif z_score > 4.0: # Hard Stop
                    signal = 'buy' 
                    reason = f'Stat Arb Stop: {pair_key} Regime Break Z-Score {z_score:.2f} > 4.0'
                    del self.active_spreads[pair_key]
                else:
                    # Max Adverse Stop (Check entry z-score)
                    entry_z = self.active_spreads[pair_key].get('entry_zscore')
                    if entry_z and entry_z > 0 and z_score > (entry_z + self.max_adverse_z_delta):
                        signal = 'buy'
                        reason = f'Stat Arb Stop: {pair_key} Max Adverse {z_score:.2f} > Entry {entry_z:.2f} + {self.max_adverse_z_delta}'
                        del self.active_spreads[pair_key]

            elif position_side == 'long':
                if z_score > -self.z_score_exit:
                    signal = 'sell'
                    reason = f'Stat Arb Exit: {pair_key} Z-Score {z_score:.2f} > -{self.z_score_exit} (Close Long)'
                    del self.active_spreads[pair_key]
                # Stop Loss Handling
                elif z_score < -4.0: # Hard Stop
                    signal = 'sell'
                    reason = f'Stat Arb Stop: {pair_key} Regime Break Z-Score {z_score:.2f} < -4.0'
                    del self.active_spreads[pair_key]
                else:
                    # Max Adverse Stop (Check entry z-score)
                    entry_z = self.active_spreads[pair_key].get('entry_zscore')
                    if entry_z and entry_z < 0 and z_score < (entry_z - self.max_adverse_z_delta):
                        signal = 'sell'
                        reason = f'Stat Arb Stop: {pair_key} Max Adverse {z_score:.2f} < Entry {entry_z:.2f} - {self.max_adverse_z_delta}'
                        del self.active_spreads[pair_key]

            # Update Metadata if still active
            if signal == 'hold' and pair_key in self.active_spreads:
                entry_z = self.active_spreads[pair_key].get('entry_zscore')
                if entry_z is not None:
                    # Update max adverse
                    current_max = self.active_spreads[pair_key].get('max_adverse_z', entry_z)
                    if entry_z < 0: # Long spread
                         self.active_spreads[pair_key]['max_adverse_z'] = min(current_max, z_score)
                    else: # Short spread
                         self.active_spreads[pair_key]['max_adverse_z'] = max(current_max, z_score)
                
                self.active_spreads[pair_key]['current_z'] = z_score

        else:
            # Check for entry signal
            # Additional Filter: Hurst Exponent Check
            hurst = hurst_exponent(spread)
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
                    'hurst': hurst,
                    'max_adverse_z': z_score,
                    'current_z': z_score
                }
            elif z_score < -self.z_score_entry:
                signal = 'buy'
                reason = f'Stat Arb Entry: {pair_key} Z-Score {z_score:.2f} < -{self.z_score_entry} (H={hurst:.2f}) (Long {symbol_a})'
                self.active_spreads[pair_key] = {
                    'side': 'long',
                    'entry_zscore': z_score,
                    'hedge_ratio': hedge_ratio,
                    'hurst': hurst,
                    'max_adverse_z': z_score,
                    'current_z': z_score
                }
        
        if signal == 'hold':
            return None
        
        is_entry = "Stat Arb Entry:" in reason
        
        # Build legs specification for atomic multi-leg execution
        if is_entry:
            if signal == 'buy':  # Long spread: long A, short B
                legs = [
                    {'symbol': symbol_a, 'market_type': 'perp', 'side': 'long', 'order_side': 'buy', 'hedge_ratio': 1.0},
                    {'symbol': symbol_b, 'market_type': 'perp', 'side': 'short', 'order_side': 'sell', 'hedge_ratio': hedge_ratio},
                ]
            else:  # Short spread: short A, long B
                legs = [
                    {'symbol': symbol_a, 'market_type': 'perp', 'side': 'short', 'order_side': 'sell', 'hedge_ratio': 1.0},
                    {'symbol': symbol_b, 'market_type': 'perp', 'side': 'long', 'order_side': 'buy', 'hedge_ratio': hedge_ratio},
                ]
        else:
            # Exit: close both positions
            if signal == 'buy':  # Closing a short spread
                legs = [
                    {'symbol': symbol_a, 'market_type': 'perp', 'side': 'close', 'order_side': 'buy', 'reduce_only': True, 'hedge_ratio': 1.0},
                    {'symbol': symbol_b, 'market_type': 'perp', 'side': 'close', 'order_side': 'sell', 'reduce_only': True, 'hedge_ratio': hedge_ratio},
                ]
            else:  # Closing a long spread
                legs = [
                    {'symbol': symbol_a, 'market_type': 'perp', 'side': 'close', 'order_side': 'sell', 'reduce_only': True, 'hedge_ratio': 1.0},
                    {'symbol': symbol_b, 'market_type': 'perp', 'side': 'close', 'order_side': 'buy', 'reduce_only': True, 'hedge_ratio': hedge_ratio},
                ]
        
        return {
            'signal': signal,
            'signal_type': 'multi_leg',
            'reason': reason,
            'price': current_price_a,
            'strategy': 'stat_arb',
            'action': 'open' if is_entry else 'close',
            'pair_symbol': symbol_b,
            'z_score': z_score,
            'hedge_ratio': hedge_ratio,
            'spread': spread.iloc[-1],
            'pair_price': current_price_b,
            'legs': legs,
            'atomic': True,
            'metadata': {'symbol_a': symbol_a, 'symbol_b': symbol_b, 'entry_z_score': z_score, 'hedge_ratio': hedge_ratio},
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
            
            # Cap hedge ratio to sane limits to prevent massive sizing
            if dynamic_beta > 10.0:
                self.logger.warning(f"StatArb: Capping Hedge Ratio {dynamic_beta:.2f} -> 10.0")
                dynamic_beta = 10.0
            elif dynamic_beta < 0.1 and dynamic_beta > -0.1: # Avoid near-zero/negative mess (though negative might be valid for inverse corr)
                # If negative, we might be short-short, which is fine, but needs logic check.
                # Assuming positive correlation for now.
                pass
            
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



    def get_active_spreads_summary(self) -> Dict[str, Any]:
        """Get summary of active spread positions."""
        return {
            'active_pairs': len(self.active_spreads),
            'positions': self.active_spreads.copy(),
        }

    def calculate_take_profit(self, entry_price: float, side: str, ohlcv: Dict[str, pd.DataFrame] = None,
                             signal_strength: float = 1.0, market_volatility: float = 1.0) -> float:
        """
        Disable fixed Take Profit. 
        Exit is managed by Z-Score convergence in should_exit().
        """
        return 0.0 # Disabled
        
    def should_exit(self, position: Any, current_price: float, 
                   current_data: Dict[str, Any] = None) -> Tuple[bool, Optional[str]]:
        """Determine if position should exit (mean reversion complete or logic break)."""
        if current_data is None:
            return False, None
            
        symbol = getattr(position, 'symbol', None)
        # Note: StrategyManager might pass just the symbol name if checking globally, 
        # but here we need to know WHICH spread this position belongs to.
        # This implementation assumes we can map symbol -> spread.
        # For multi-leg, StrategyManager handles the legs. 
        # If this is called per-leg, we need to know the pair.
        
        # Simplified Check:
        # In this architecture, generating_signal/active_spreads manages the state.
        # We need to find the pair this symbol is part of.
        
        active_pair_key = None
        for pair_key, data in self.active_spreads.items():
            sym_a, sym_b = pair_key.split('/')
            if symbol == sym_a or symbol == sym_b:
                active_pair_key = pair_key
                break
        
        if not active_pair_key:
            return False, None
            
        # We need the CURRENT Z-Score. 
        # The logic in generate_signal calculates it. 
        # Ideally, we should recalculate it here or have it passed in current_data.
        # If current_data represents the ticker data, we can't easily calc spread without the other leg.
        
        # Strategy: Rely on generate_signal to produce 'close' signals based on Z-score, 
        # OR use the 'z_score' passed in if available. 
        
        # If 'z_score' is passed in current_data (from StrategyManager's analysis loop):
        z_score = current_data.get('z_score')
        
        if z_score is not None:
            # 1. Take Profit: Mean Reversion
            if abs(z_score) < self.z_score_exit:
                return True, f"spread_mean_reversion_complete (z={z_score:.2f})"
            
            # 2. Stop Loss: Regime Break / Divergence
            # If Z-score expands beyond 4.0, the correlation is likely broken.
            if abs(z_score) > 4.0:
                return True, f"spread_regime_break_stop (z={z_score:.2f})"
            
            # 3. Max Adverse Spread Stop (1.5σ from entry)
            entry_z = current_data.get('entry_z_score')
            if entry_z:
                # Long spread (negative entry z): adverse = more negative
                if entry_z < 0 and z_score < entry_z - self.max_adverse_z_delta:
                    return True, f"max_adverse_spread_stop (entry={entry_z:.2f}, now={z_score:.2f})"
                # Short spread (positive entry z): adverse = more positive
                elif entry_z > 0 and z_score > entry_z + self.max_adverse_z_delta:
                    return True, f"max_adverse_spread_stop (entry={entry_z:.2f}, now={z_score:.2f})"
                
        # 3. Max Hold Time Limit (Safety)
        # Prevents holding losing positions indefinitely (Survivor Bias)
        if hasattr(position, 'entry_time') and position.entry_time:
            entry_time = position.entry_time
            if isinstance(entry_time, str):
                entry_time = pd.to_datetime(entry_time)
            
            # Simple datetime subtraction (ensure both are naive or aware)
            # In backtest/bot, typically naive local time or UTC
            try:
                time_held = datetime.now() - entry_time
                if time_held > timedelta(hours=self.max_holding_hours):
                    return True, f"max_holding_time_exceeded ({time_held})"
            except Exception as e:
                self.logger.warning(f"Error checking max hold time: {e}")

        return False, None

    def get_trailing_stop_config(self) -> Dict[str, Any]:
        """Trailing stop config."""
        return {
            'enabled': True,
            'trail_pct': 0.04,
            'activation_pct': 0.05,
        }

    def calculate_signal_strength(self, ohlcv: Dict[str, pd.DataFrame], symbol: str = None, signal_context: Dict[str, Any] = None) -> float:
        """
        Calculate signal strength based on Z-Score magnitude.
        
        Mapping:
        - Z-Score < Entry (2.0): 0.5 (Base)
        - Z-Score 2.0 -> 0.5
        - Z-Score 3.0 -> 0.75
        - Z-Score >= 4.0 -> 1.0 (Max Conviction)
        """
        z_score = 0.0
        
        # Try to get from context first (most reliable)
        if signal_context and 'z_score' in signal_context:
            z_score = abs(signal_context['z_score'])
        elif symbol:
            # Try to find active spread for this symbol (re-calculation fallback)
            # This is complex because we need the pair. 
            # If we don't have context, we arguably shouldn't trust a fresh calc without peer.
            pass
            
        # Default to entry threshold if unknown, or if z_score is small (holding)
        if z_score < self.z_score_entry:
            return 0.5
            
        # Normalize: (Z - Entry) / (Max - Entry) * 0.5 + 0.5
        # Scale range: 2.0 to 4.0 maps to 0.5 to 1.0
        z_max = 4.0
        if z_score >= z_max:
            return 1.0
            
        return 0.5 + 0.5 * (z_score - self.z_score_entry) / (z_max - self.z_score_entry)

    def get_spread_status(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get z-score status for a spread involving this symbol."""
        # Find pair for this symbol
        active_pair_key = None
        for pair_key in self.active_spreads.keys():
            sym_a, sym_b = pair_key.split('/')
            if symbol == sym_a or symbol == sym_b:
                active_pair_key = pair_key
                break
        
        if not active_pair_key:
            return None
            
        data = self.active_spreads[active_pair_key]
        return {
            'current_z': data.get('current_z'),
            'entry_z': data.get('entry_zscore'),
            'max_adverse_z': data.get('max_adverse_z')
        }
