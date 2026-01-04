"""
Ornstein-Uhlenbeck Mean Reversion Strategy.

This strategy models asset prices as mean-reverting processes using the
Ornstein-Uhlenbeck (OU) stochastic differential equation and trades
when prices deviate significantly from their estimated equilibrium.
"""

import logging
from typing import Dict, Any, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from src.models.trade import Position
from dataclasses import dataclass
from datetime import datetime
import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy

# Try to import scipy for MLE optimization
try:
    from scipy.optimize import minimize
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False


@dataclass
class OUParameters:
    """Ornstein-Uhlenbeck process parameters."""
    theta: float      # Speed of mean reversion
    mu: float         # Long-term mean
    sigma: float      # Volatility
    half_life: float  # Half-life in periods (hours if hourly data)
    
    def to_dict(self) -> Dict[str, float]:
        return {
            'theta': round(self.theta, 6),
            'mu': round(self.mu, 4),
            'sigma': round(self.sigma, 6),
            'half_life': round(self.half_life, 2),
        }


class OUMeanReversionStrategy(BaseStrategy):
    """
    Ornstein-Uhlenbeck Mean Reversion Strategy.
    
    The OU process is defined by the stochastic differential equation:
        dX = θ(μ - X)dt + σdW
    
    Where:
        - θ (theta): Speed of mean reversion
        - μ (mu): Long-term equilibrium mean
        - σ (sigma): Volatility of the process
        - W: Wiener process (Brownian motion)
    
    The half-life of mean reversion is: t_half = ln(2) / θ
    
    Strategy Logic:
        1. Estimate OU parameters from historical price data
        2. Calculate deviation from equilibrium mean
        3. Enter when deviation exceeds threshold (in std devs)
        4. Exit when price reverts toward mean
        5. Only trade assets with reasonable half-life (not too slow)
    """
    
    # OU mean reversion needs granular data to detect short-term reversions
    PREFERRED_TIMEFRAME = '15m'
    
    def __init__(self, config: Dict[str, Any], timeframe: str = None):
        super().__init__(config, timeframe)
        
        # Strategy parameters from config
        ou_config = config.get('strategies', {}).get('ou_mean_reversion', {})
        
        # Entry/exit thresholds (in standard deviations)
        self.zscore_entry = ou_config.get('zscore_entry', 2.0)
        self.zscore_exit = ou_config.get('zscore_exit', 0.5)
        
        # Half-life constraints
        self.half_life_max_hours = ou_config.get('half_life_max_hours', 24)
        self.half_life_min_hours = ou_config.get('half_life_min_hours', 1)
        
        # Minimum mean reversion speed (theta)
        self.min_theta = ou_config.get('min_mean_reversion_speed', 0.1)
        
        # Lookback period for parameter estimation
        self.estimation_lookback = ou_config.get('estimation_lookback', 100)
        
        # Minimum data points for estimation
        self.min_data_points = ou_config.get('min_data_points', 50)
        
        # Cache for OU parameters
        self.ou_params_cache: Dict[str, Tuple[OUParameters, datetime]] = {}
        self.cache_ttl_hours = ou_config.get('cache_ttl_hours', 4)
        
        # Active positions tracking
        self.active_positions: Dict[str, Dict[str, Any]] = {}
        
        self.logger.info(f"Initialized OU Mean Reversion Strategy: "
                        f"z_entry={self.zscore_entry}, z_exit={self.zscore_exit}, "
                        f"half_life_max={self.half_life_max_hours}h")
    
    def generate_signal(self, symbol: str, ohlcv: Dict[str, pd.DataFrame]) -> Optional[Dict[str, Any]]:
        """
        Generate trading signal based on OU mean reversion.
        
        Args:
            symbol: Symbol being analyzed
            ohlcv: Dictionary of OHLCV DataFrames
            
        Returns:
            Signal dictionary or None
        """
        # Get preferred timeframe data
        tf_data = ohlcv.get(self.timeframe)
        if tf_data is None:
            # Fallback to first available if preferred not found
            if ohlcv:
                tf_data = next(iter(ohlcv.values()))
            else:
                return None

        return self._generate_signal_internal(tf_data, symbol)
    
    def generate_signal_for_symbol(self, symbol: str, ohlcv: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """
        Generate signal with symbol context for caching.
        
        Args:
            symbol: Trading symbol
            ohlcv: OHLCV data DataFrame
            
        Returns:
            Signal dictionary or None
        """
        return self._generate_signal_internal(ohlcv, symbol)
    
    def _generate_signal_internal(self, ohlcv: pd.DataFrame, symbol: str) -> Optional[Dict[str, Any]]:
        """Internal signal generation with symbol context."""
        
        if ohlcv is None or len(ohlcv) < self.min_data_points:
            return None
        
        prices = ohlcv['close']
        current_price = prices.iloc[-1]
        
        # Get or estimate OU parameters
        ou_params = self._get_ou_parameters(symbol, prices)
        
        if ou_params is None:
            return None
        
        # Check if asset is suitable for mean reversion trading
        if not self._is_tradeable(ou_params):
            self.logger.debug(f"{symbol}: Not suitable for OU trading "
                            f"(half_life={ou_params.half_life:.1f}h, theta={ou_params.theta:.4f})")
            return None
        
        # Calculate Z-score in LOG space (since OU params are estimated on log prices)
        # log(current_price) - log(mu) = log(current_price / mu)
        log_current = np.log(current_price)
        log_mu = np.log(ou_params.mu)
        log_deviation = log_current - log_mu
        
        # For OU process, stationary std = sigma / sqrt(2 * theta)
        # This is in log space since sigma was estimated on log returns
        stationary_std = ou_params.sigma / np.sqrt(2 * ou_params.theta) if ou_params.theta > 0 else ou_params.sigma
        
        if stationary_std == 0:
            return None
        
        zscore = log_deviation / stationary_std
        
        # Check for existing position
        has_position = symbol in self.active_positions
        
        signal = 'hold'
        reason = ''
        
        if has_position:
            # Check for exit signal
            position = self.active_positions[symbol]
            position_side = position.get('side')
            
            # Exit if price has reverted toward mean
            if position_side == 'long' and zscore > -self.zscore_exit:
                signal = 'sell'
                reason = f'OU Exit: {symbol} Z-score {zscore:.2f} > -{self.zscore_exit} (Close Long)'
                del self.active_positions[symbol]
            elif position_side == 'short' and zscore < self.zscore_exit:
                signal = 'buy'
                reason = f'OU Exit: {symbol} Z-score {zscore:.2f} < {self.zscore_exit} (Close Short)'
                del self.active_positions[symbol]
        else:
            # Check for entry signal
            # Price significantly below mean -> buy (expect reversion up)
            if zscore < -self.zscore_entry:
                signal = 'buy'
                reason = f'OU Entry: {symbol} Z-score {zscore:.2f} < -{self.zscore_entry} (Price below mean)'
                self.active_positions[symbol] = {
                    'side': 'long',
                    'entry_zscore': zscore,
                    'entry_price': current_price,
                    'mu': ou_params.mu,
                }
            # Price significantly above mean -> sell (expect reversion down)
            elif zscore > self.zscore_entry:
                signal = 'sell'
                reason = f'OU Entry: {symbol} Z-score {zscore:.2f} > {self.zscore_entry} (Price above mean)'
                self.active_positions[symbol] = {
                    'side': 'short',
                    'entry_zscore': zscore,
                    'entry_price': current_price,
                    'mu': ou_params.mu,
                }
        
        if signal == 'hold':
            return None
        
        return {
            'signal': signal,
            'reason': reason,
            'price': current_price,
            'strategy': 'ou_mean_reversion',
            'zscore': zscore,
            'mu': ou_params.mu,
            'theta': ou_params.theta,
            'half_life': ou_params.half_life,
            'stationary_std': stationary_std,
        }
    
    def _get_ou_parameters(self, symbol: str, prices: pd.Series) -> Optional[OUParameters]:
        """
        Get OU parameters from cache or estimate them.
        
        Args:
            symbol: Trading symbol
            prices: Price series
            
        Returns:
            OUParameters or None if estimation fails
        """
        # Check cache
        if symbol in self.ou_params_cache:
            cached_params, cache_time = self.ou_params_cache[symbol]
            age_hours = (datetime.now() - cache_time).total_seconds() / 3600
            
            if age_hours < self.cache_ttl_hours:
                return cached_params
        
        # Adaptive Lookback (Phase 7 Refinement)
        # Try multiple lookback windows and pick the one with best fit (lowest residuals)
        candidate_lookbacks = [50, 100, 200]
        # Ensure we don't exceed available history
        available_points = len(prices)
        candidate_lookbacks = [L for L in candidate_lookbacks if L <= available_points]
        
        # If explicitly configured lookback is not in list, add it
        if self.estimation_lookback not in candidate_lookbacks and self.estimation_lookback <= available_points:
            candidate_lookbacks.append(self.estimation_lookback)
            
        best_params = None
        best_error = float('inf')
        
        for lookback in candidate_lookbacks:
            # Slice prices to this lookback
            slice_prices = prices.iloc[-lookback:]
            
            # Estimate parameters
            params, error = self._estimate_ou_parameters_with_error(slice_prices)
            
            if params and error < best_error:
                best_params = params
                best_error = error
                # Store the optimal lookback in the params object for debugging
                best_params.optimal_lookback = lookback
        
        if best_params:
            self.ou_params_cache[symbol] = (best_params, datetime.now())
            if hasattr(best_params, 'optimal_lookback') and best_params.optimal_lookback != self.estimation_lookback:
                 self.logger.debug(f"{symbol}: Adaptive lookback selected {best_params.optimal_lookback} periods (default {self.estimation_lookback})")
        
        return best_params
    
    def _estimate_ou_parameters_with_error(self, prices: pd.Series) -> Tuple[Optional[OUParameters], float]:
        """
        Estimate OU parameters and return fitting error (residual variance).
        """
        try:
            if len(prices) < self.min_data_points:
                return None, float('inf')
            
            # Use log prices
            log_prices = np.log(prices.values)
            
            # Calculate price changes
            X = log_prices[:-1]
            dX = log_prices[1:] - log_prices[:-1]
            
            # OLS regression: dX = α + β*X + ε
            n = len(X)
            X_mean = np.mean(X)
            dX_mean = np.mean(dX)
            
            # Calculate beta
            numerator = np.sum((X - X_mean) * (dX - dX_mean))
            denominator = np.sum((X - X_mean) ** 2)
            
            if denominator == 0:
                return None, float('inf')
            
            beta = numerator / denominator
            alpha = dX_mean - beta * X_mean
            
            # OU parameters
            theta = -beta
            
            if theta <= 0:
                # Not mean-reverting (error is high)
                return None, float('inf')
            
            mu = -alpha / beta if beta != 0 else X_mean
            
            # Estimate residuals
            residuals = dX - alpha - beta * X
            # Error metric: Standard deviation of residuals (sigma)
            # Smaller sigma means tighter fit to the mean-reverting process
            sigma = np.std(residuals)
            
            # Calculate half-life
            half_life = np.log(2) / theta if theta > 0 else float('inf')
            mu_price = np.exp(mu)
            
            params = OUParameters(
                theta=theta,
                mu=mu_price,
                sigma=sigma,
                half_life=half_life,
            )
            
            return params, sigma
            
        except Exception as e:
            # self.logger.error(f"Error estimating OU parameters: {e}")
            return None, float('inf')

    def _estimate_ou_parameters(self, prices: pd.Series) -> Optional[OUParameters]:
        """Legacy wrapper for backward compatibility."""
        params, _ = self._estimate_ou_parameters_with_error(prices)
        return params
    
    def _estimate_ou_parameters_mle(self, prices: pd.Series) -> Optional[OUParameters]:
        """
        Estimate OU parameters using Maximum Likelihood Estimation.
        
        This is more accurate but requires scipy.
        
        Args:
            prices: Price series
            
        Returns:
            OUParameters or None
        """
        if not SCIPY_AVAILABLE:
            return self._estimate_ou_parameters(prices)
        
        try:
            log_prices = np.log(prices.values)
            n = len(log_prices)
            dt = 1.0  # Assuming unit time steps
            
            def neg_log_likelihood(params):
                theta, mu, sigma = params
                if theta <= 0 or sigma <= 0:
                    return 1e10
                
                # Expected value and variance for OU process
                X = log_prices[:-1]
                X_next = log_prices[1:]
                
                expected = X * np.exp(-theta * dt) + mu * (1 - np.exp(-theta * dt))
                variance = (sigma ** 2) / (2 * theta) * (1 - np.exp(-2 * theta * dt))
                
                if variance <= 0:
                    return 1e10
                
                # Negative log likelihood (Gaussian)
                residuals = X_next - expected
                nll = 0.5 * n * np.log(2 * np.pi * variance) + 0.5 * np.sum(residuals ** 2) / variance
                
                return nll
            
            # Initial guess from OLS
            ols_params = self._estimate_ou_parameters(prices)
            if ols_params:
                x0 = [ols_params.theta, np.log(ols_params.mu), ols_params.sigma]
            else:
                x0 = [0.1, np.mean(log_prices), np.std(np.diff(log_prices))]
            
            # Optimize
            result = minimize(neg_log_likelihood, x0, method='Nelder-Mead',
                            options={'maxiter': 1000})
            
            if result.success:
                theta, mu_log, sigma = result.x
                mu_price = np.exp(mu_log)
                half_life = np.log(2) / theta if theta > 0 else float('inf')
                
                return OUParameters(
                    theta=theta,
                    mu=mu_price,
                    sigma=sigma,
                    half_life=half_life,
                )
            
            return self._estimate_ou_parameters(prices)
            
        except Exception as e:
            self.logger.error(f"MLE estimation failed: {e}")
            return self._estimate_ou_parameters(prices)
    
    def _is_tradeable(self, params: OUParameters) -> bool:
        """
        Check if the asset is suitable for mean reversion trading.
        
        Args:
            params: OU parameters
            
        Returns:
            True if tradeable
        """
        # Check mean reversion speed
        if params.theta < self.min_theta:
            return False
        
        # Check half-life bounds
        if params.half_life > self.half_life_max_hours:
            return False
        
        if params.half_life < self.half_life_min_hours:
            return False
        
        return True
    
    def calculate_take_profit(self, entry_price: float, side: str, ohlcv: Dict[str, pd.DataFrame] = None,
                             signal_strength: float = 1.0, market_volatility: float = 1.0) -> float:
        """
        Calculate take profit for OU mean reversion.
        
        Target is the estimated mean (mu) for mean reversion.
        """
        # For mean reversion, target is approximately the mean
        # We use a percentage based on expected reversion
        base_tp_pct = 0.03  # 3% base target
        
        # Adjust for signal strength (higher Z-score = more deviation = larger target)
        adjusted_tp = base_tp_pct * signal_strength
        
        # Clamp to reasonable range
        adjusted_tp = max(0.02, min(0.08, adjusted_tp))
        
        if side == 'buy':
            return entry_price * (1 + adjusted_tp)
        else:
            return entry_price * (1 - adjusted_tp)
    
    def calculate_stop_loss(self, entry_price: float, side: str, 
                           signal_context: Dict[str, Any] = None) -> float:
        """
        Calculate stop loss for OU mean reversion.
        
        For mean reversion, stop loss is based on the Z-score moving further
        from the mean (wrong direction), typically 1.5-2x the entry Z-score.
        """
        # Default stop loss if no context available
        if signal_context is None:
            # Use 5% as default
            sl_pct = 0.05
        else:
            # Get the entry Z-score and OU sigma (handle None values explicitly)
            z_score = signal_context.get('z_score')
            z_score = abs(z_score) if z_score is not None else 2.0
            sigma = signal_context.get('sigma') or 0.02
            mu = signal_context.get('mu') or entry_price
            
            # Stop loss if Z-score moves to 1.5x entry (e.g., entered at 2, stop at 3)
            stop_z = z_score * 1.5
            
            # Convert back to price deviation
            # For OU: z = (ln(price) - ln(mu)) / sigma
            # So: ln(price) = ln(mu) + z * sigma
            # And: price = mu * exp(z * sigma)
            if side == 'buy':
                # Long position - price went down to enter, stop if it goes even lower
                sl_pct = 1 - np.exp(-stop_z * sigma)
            else:
                # Short position - price went up to enter, stop if it goes even higher
                sl_pct = np.exp(stop_z * sigma) - 1
            
            # Clamp to reasonable range (3% to 10%)
            sl_pct = max(0.03, min(0.10, sl_pct))
        
        if side == 'buy':
            return entry_price * (1 - sl_pct)
        else:
            return entry_price * (1 + sl_pct)
    
    def should_exit(self, position: Any, current_price: float, 
                   current_data: Dict[str, Any] = None) -> Tuple[bool, Optional[str]]:
        """
        Determine if OU position should exit based on Z-score normalization.
        
        Exit conditions:
        1. Z-score has returned to near zero (mean reversion complete)
        2. Z-score has reversed direction significantly
        """
        if current_data is None:
            return False, None
        
        symbol = getattr(position, 'symbol', None)
        if not symbol:
            return False, None
        
        # Get OHLCV data for Z-score calculation
        ohlcv = current_data.get('ohlcv')
        if ohlcv is None or len(ohlcv) < self.min_data_points:
            return False, None
        
        # Get or estimate OU parameters
        params = self._estimate_ou_parameters(ohlcv['close'])
        if params is None:
            return False, None
        
        # Calculate current Z-score
        log_price = np.log(current_price)
        log_mu = np.log(params.mu)
        
        if params.sigma <= 0:
            return False, None
        
        z_score = (log_price - log_mu) / params.sigma
        
        # Exit if Z-score has reverted past exit threshold
        if abs(z_score) < self.zscore_exit:
            return True, f"mean_reversion_complete (z={z_score:.2f})"
        
        # Exit if Z-score has crossed zero (overshot the mean)
        position_side = getattr(position, 'side', None)
        if position_side == 'long' and z_score > 0.5:
            return True, f"mean_reversion_overshot (z={z_score:.2f})"
        elif position_side == 'short' and z_score < -0.5:
            return True, f"mean_reversion_overshot (z={z_score:.2f})"
        
        return False, None
    
    def get_trailing_stop_config(self) -> Dict[str, Any]:
        """
        Get trailing stop configuration for OU mean reversion.
        
        Mean reversion expects price to return to the mean, so we DON'T want
        aggressive trailing that cuts profits short before full reversion.
        
        However, if price significantly overshoots the mean (big gain), we use
        a loose trailing stop to protect those gains.
        
        Returns:
            Trailing stop configuration
        """
        return {
            'enabled': True,
            'trail_pct': 0.04,         # 4% trailing stop (loose)
            'activation_pct': 0.05,    # Only activate after 5% gain (overshoot)
        }
    
    def get_ou_parameters(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get cached OU parameters for a symbol."""
        if symbol in self.ou_params_cache:
            params, cache_time = self.ou_params_cache[symbol]
            return {
                **params.to_dict(),
                'cached_at': cache_time.isoformat(),
            }
        return None
    
    def get_active_positions_summary(self) -> Dict[str, Any]:
        """Get summary of active positions."""
        return {
            'active_positions': len(self.active_positions),
            'positions': self.active_positions.copy(),
        }
    
    def scan_tradeable_assets(self, symbols: list, market_api) -> Dict[str, OUParameters]:
        """
        Scan multiple assets to find those suitable for OU mean reversion.
        
        Args:
            symbols: List of symbols to scan
            market_api: Market API for fetching data
            
        Returns:
            Dictionary of tradeable symbols and their OU parameters
        """
        tradeable = {}
        
        for symbol in symbols:
            try:
                ohlcv = market_api.get_ohlcv(symbol, '1h', self.estimation_lookback)
                if ohlcv is not None and len(ohlcv) >= self.min_data_points:
                    params = self._estimate_ou_parameters(ohlcv['close'])
                    
                    if params and self._is_tradeable(params):
                        tradeable[symbol] = params
                        self.logger.info(f"{symbol}: Tradeable (half_life={params.half_life:.1f}h)")
                        
            except Exception as e:
                self.logger.error(f"Error scanning {symbol}: {e}")
        
        self.logger.info(f"Found {len(tradeable)} tradeable assets out of {len(symbols)}")
        return tradeable

