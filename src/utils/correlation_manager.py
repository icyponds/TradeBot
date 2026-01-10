"""
Correlation and Cointegration Manager for analyzing asset pairs.

Provides both correlation-based and cointegration-based pair identification
for statistical arbitrage strategies.
"""

import logging
import pandas as pd
import numpy as np
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass, field

# Try to import statsmodels for cointegration tests
try:
    from statsmodels.tsa.stattools import adfuller, coint
    from statsmodels.regression.linear_model import OLS
    from statsmodels.tools import add_constant
    STATSMODELS_AVAILABLE = True
except ImportError:
    STATSMODELS_AVAILABLE = False

from src.utils.statistics import engle_granger as custom_engle_granger



@dataclass
class CointegrationResult:
    """Result of cointegration analysis between two assets."""
    symbol_a: str
    symbol_b: str
    is_cointegrated: bool
    p_value: float
    hedge_ratio: float
    half_life: Optional[float]  # In periods (hours if using hourly data)
    correlation: float
    spread_mean: float
    spread_std: float
    adf_statistic: Optional[float] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'symbol_a': self.symbol_a,
            'symbol_b': self.symbol_b,
            'is_cointegrated': self.is_cointegrated,
            'p_value': round(self.p_value, 4),
            'hedge_ratio': round(self.hedge_ratio, 4),
            'half_life': round(self.half_life, 2) if self.half_life else None,
            'correlation': round(self.correlation, 4),
            'spread_mean': round(self.spread_mean, 4),
            'spread_std': round(self.spread_std, 4),
        }


@dataclass
class KalmanFilterState:
    """State for Kalman Filter hedge ratio estimation."""
    beta: float  # Current hedge ratio estimate
    P: float     # Estimation error covariance
    Q: float     # Process noise covariance
    R: float     # Measurement noise covariance
    
    def update(self, y: float, x: float) -> float:
        """
        Update Kalman Filter with new observation.
        
        Args:
            y: Dependent variable (price of asset A)
            x: Independent variable (price of asset B)
            
        Returns:
            Updated hedge ratio
        """
        # Prediction step
        beta_pred = self.beta
        P_pred = self.P + self.Q
        
        # Update step
        y_hat = beta_pred * x
        error = y - y_hat
        
        # Kalman gain
        S = P_pred * x * x + self.R
        K = P_pred * x / S if S != 0 else 0
        
        # Update estimates
        self.beta = beta_pred + K * error
        self.P = (1 - K * x) * P_pred
        
        return self.beta


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
        self.cointegrated_pairs = {}  # {symbol: symbol_b}
        self.cointegration_results = {}  # {symbol: CointegrationResult}
        self.correlation_matrix = None
        self.last_update = None
        
    def update_correlations(self, symbols: List[str], current_time: datetime = None) -> Dict[str, str]:
        """
        Update correlation data for the given symbols.
        
        Args:
            symbols: List of symbols to analyze
            current_time: Current simulation time (optional)
            
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
                    self.logger.debug(f"Fetched {len(ohlcv)} points for {symbol} ({ohlcv.index[0]} to {ohlcv.index[-1]})")
                else:
                    self.logger.warning(f"No correlation data for {symbol} (TF={analysis_timeframe})")
            except Exception as e:
                self.logger.error(f"Error fetching data for {symbol}: {e}")
                
        if len(price_data) < 2:
            self.logger.warning("Insufficient data for correlation analysis")
            return {}
            
        # Create DataFrame with all prices
        # Align data by index (timestamp) to ensure valid comparisons
        df = pd.DataFrame(price_data)
        
        # [MODIFIED] Do NOT drop globally missing values, as this kills analysis if any single asset 
        # has a different time range (e.g. Spot vs Perp). df.corr() handles NaNs pairwise.
        # df = df.dropna()
        
        # Check if we have enough data (at least some rows)
        if df.empty:
            self.logger.warning(f"Combined dataframe is empty")
            return {}
            
        # Calculate correlation matrix with min_periods to ensure statistical significance
        # We require at least 50% of lookback period to overlap
        min_overlap = int(self.lookback_period * 0.5)
        self.correlation_matrix = df.corr(min_periods=min_overlap)
        
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
        self.last_update = current_time if current_time else datetime.now()
        
        self.logger.info(f"Correlation update complete. Found {len(self.correlated_pairs)} correlated pairs. (Time: {self.last_update})")
        return self.correlated_pairs
        
    def get_correlated_symbol(self, symbol: str) -> Optional[str]:
        """Get the correlated symbol for a given asset."""
        return self.correlated_pairs.get(symbol)
        
    def should_update(self, current_time: datetime = None) -> bool:
        """Check if correlations need to be updated."""
        if self.last_update is None:
            return True
            
        now = current_time if current_time else datetime.now()
        elapsed = now - self.last_update
        return elapsed > timedelta(hours=self.update_interval_hours)
    
    # ==================== COINTEGRATION METHODS ====================
    
    def test_cointegration(self, prices_a: pd.Series, prices_b: pd.Series,
                          symbol_a: str = "A", symbol_b: str = "B",
                          p_value_threshold: float = 0.05) -> CointegrationResult:
        """
        Test for cointegration between two price series using Engle-Granger method.
        
        Args:
            prices_a: Price series for asset A
            prices_b: Price series for asset B
            symbol_a: Symbol name for asset A
            symbol_b: Symbol name for asset B
            p_value_threshold: P-value threshold for cointegration (default 0.05)
            
        Returns:
            CointegrationResult with test results
        """
        if not STATSMODELS_AVAILABLE:
            self.logger.warning("statsmodels not available - using simple correlation")
            return self._fallback_cointegration_test(prices_a, prices_b, symbol_a, symbol_b)
        
        try:
            # Align the series
            aligned = pd.DataFrame({'a': prices_a, 'b': prices_b}).dropna()
            
            if len(aligned) < 30:
                self.logger.warning(f"Insufficient data for cointegration test: {len(aligned)} points")
                return CointegrationResult(
                    symbol_a=symbol_a, symbol_b=symbol_b,
                    is_cointegrated=False, p_value=1.0,
                    hedge_ratio=1.0, half_life=None,
                    correlation=0.0, spread_mean=0.0, spread_std=1.0
                )
            
            y = aligned['a'].values
            x = aligned['b'].values
            
            # Calculate correlation
            correlation = np.corrcoef(y, x)[0, 1]
            
            # Run cointegration test (Engle-Granger)
            score, p_value, _ = coint(y, x)
            
            # Estimate hedge ratio using OLS: y = beta * x + epsilon
            X = add_constant(x)
            model = OLS(y, X).fit()
            hedge_ratio = model.params[1]  # Beta coefficient
            
            # Calculate spread
            spread = y - hedge_ratio * x
            spread_mean = np.mean(spread)
            spread_std = np.std(spread)
            
            # Calculate half-life of mean reversion
            half_life = self._calculate_half_life(spread)
            
            # ADF test on the spread for additional validation
            adf_result = adfuller(spread, maxlag=1)
            adf_statistic = adf_result[0]
            adf_pvalue = adf_result[1]
            
            is_cointegrated = p_value < p_value_threshold
            
            result = CointegrationResult(
                symbol_a=symbol_a,
                symbol_b=symbol_b,
                is_cointegrated=is_cointegrated,
                p_value=p_value,
                hedge_ratio=hedge_ratio,
                half_life=half_life,
                correlation=correlation,
                spread_mean=spread_mean,
                spread_std=spread_std,
                adf_statistic=adf_statistic,
            )
            
            self.logger.info(f"Cointegration test {symbol_a}/{symbol_b}: "
                           f"p-value={p_value:.4f}, hedge_ratio={hedge_ratio:.4f}, "
                           f"half_life={half_life:.1f}h, cointegrated={is_cointegrated}")
            
            return result
            
        except Exception as e:
            self.logger.error(f"Error in cointegration test: {e}")
            return CointegrationResult(
                symbol_a=symbol_a, symbol_b=symbol_b,
                is_cointegrated=False, p_value=1.0,
                hedge_ratio=1.0, half_life=None,
                correlation=0.0, spread_mean=0.0, spread_std=1.0
            )
    
    def _fallback_cointegration_test(self, prices_a: pd.Series, prices_b: pd.Series,
                                    symbol_a: str, symbol_b: str) -> CointegrationResult:
        """Fallback method utilizing custom Engle-Granger implementation without statsmodels."""
        aligned = pd.DataFrame({'a': prices_a, 'b': prices_b}).dropna()
        
        if len(aligned) < 30:
            return CointegrationResult(
                symbol_a=symbol_a, symbol_b=symbol_b,
                is_cointegrated=False, p_value=1.0,
                hedge_ratio=1.0, half_life=None,
                correlation=0.0, spread_mean=0.0, spread_std=1.0
            )
        
        # Use custom implementation
        t_stat, p_value, beta = custom_engle_granger(aligned['a'], aligned['b'])
        
        y = aligned['a'].values
        x = aligned['b'].values
        
        # Simple correlation for metadata
        correlation = np.corrcoef(y, x)[0, 1]
        
        # Calculate spread
        spread = y - beta * x
        spread_mean = np.mean(spread)
        spread_std = np.std(spread)
        
        # Estimate half-life
        half_life = self._calculate_half_life(spread)
        
        is_cointegrated = p_value < 0.05
        
        return CointegrationResult(
            symbol_a=symbol_a, symbol_b=symbol_b,
            is_cointegrated=is_cointegrated,
            p_value=p_value,
            hedge_ratio=beta,
            half_life=half_life,
            correlation=correlation,
            spread_mean=spread_mean,
            spread_std=spread_std,
            adf_statistic=t_stat
        )
    
    def _calculate_half_life(self, spread: np.ndarray) -> float:
        """
        Calculate half-life of mean reversion using AR(1) model.
        
        Half-life = -ln(2) / ln(phi) where phi is AR(1) coefficient
        
        Args:
            spread: Spread series
            
        Returns:
            Half-life in periods
        """
        try:
            if len(spread) < 10:
                return float('inf')
            
            # Compute AR(1) coefficient using regression
            spread_lag = spread[:-1]
            spread_diff = spread[1:] - spread[:-1]
            
            # Regress spread_diff on spread_lag
            X = add_constant(spread_lag)
            model = OLS(spread_diff, X).fit()
            
            # AR(1) coefficient is 1 + regression coefficient
            lambda_coef = model.params[1]
            
            if lambda_coef >= 0:
                # Not mean-reverting
                return float('inf')
            
            # Half-life = -ln(2) / lambda
            half_life = -np.log(2) / lambda_coef
            
            return max(0.1, half_life)  # Minimum 0.1 periods
            
        except Exception as e:
            self.logger.error(f"Error calculating half-life: {e}")
            return float('inf')
    
    def find_cointegrated_pairs(self, symbols: List[str], 
                               p_value_threshold: float = 0.05,
                               max_half_life: float = 48) -> List[CointegrationResult]:
        """
        Find all cointegrated pairs among a list of symbols.
        
        Args:
            symbols: List of symbols to analyze
            p_value_threshold: P-value threshold for cointegration
            max_half_life: Maximum half-life in hours to consider
            
        Returns:
            List of CointegrationResult for cointegrated pairs
        """
        self.logger.info(f"Scanning {len(symbols)} symbols for cointegration...")
        
        # Fetch price data
        price_data = {}
        analysis_timeframe = '1h'
        
        for symbol in symbols:
            try:
                ohlcv = self.market_api.get_ohlcv(symbol, analysis_timeframe, self.lookback_period)
                if ohlcv is not None and not ohlcv.empty:
                    price_data[symbol] = ohlcv['close']
            except Exception as e:
                self.logger.error(f"Error fetching data for {symbol}: {e}")
        
        if len(price_data) < 2:
            self.logger.warning("Insufficient data for cointegration analysis")
            return []
        
        cointegrated_pairs = []
        tested_pairs = set()
        
        symbols_with_data = list(price_data.keys())
        
        for i, symbol_a in enumerate(symbols_with_data):
            for symbol_b in symbols_with_data[i+1:]:
                # Skip if already tested
                pair_key = tuple(sorted([symbol_a, symbol_b]))
                if pair_key in tested_pairs:
                    continue
                tested_pairs.add(pair_key)
                
                # Test cointegration
                result = self.test_cointegration(
                    price_data[symbol_a], 
                    price_data[symbol_b],
                    symbol_a, symbol_b,
                    p_value_threshold
                )
                
                # Filter by cointegration and half-life
                if result.is_cointegrated:
                    if result.half_life and result.half_life <= max_half_life:
                        cointegrated_pairs.append(result)
                        self.logger.info(f"Found cointegrated pair: {symbol_a}/{symbol_b} "
                                       f"(half-life: {result.half_life:.1f}h)")
        
        # Sort by half-life (prefer faster mean reversion)
        cointegrated_pairs.sort(key=lambda x: x.half_life if x.half_life else float('inf'))
        
        self.logger.info(f"Found {len(cointegrated_pairs)} cointegrated pairs")
        return cointegrated_pairs
    
    def create_kalman_filter(self, initial_beta: float = 1.0,
                            Q: float = 0.001, R: float = 1.0) -> KalmanFilterState:
        """
        Create a Kalman Filter for dynamic hedge ratio estimation.
        
        Args:
            initial_beta: Initial hedge ratio estimate
            Q: Process noise covariance (how fast beta can change)
            R: Measurement noise covariance
            
        Returns:
            KalmanFilterState instance
        """
        return KalmanFilterState(
            beta=initial_beta,
            P=1.0,  # Initial estimation error covariance
            Q=Q,
            R=R
        )
    
    def calculate_spread_zscore(self, prices_a: pd.Series, prices_b: pd.Series,
                               hedge_ratio: float, lookback: int = 20) -> pd.Series:
        """
        Calculate Z-score of the spread for trading signals.
        
        Args:
            prices_a: Price series for asset A
            prices_b: Price series for asset B
            hedge_ratio: Hedge ratio (beta)
            lookback: Lookback period for rolling mean/std
            
        Returns:
            Z-score series
        """
        spread = prices_a - hedge_ratio * prices_b
        spread_mean = spread.rolling(window=lookback).mean()
        spread_std = spread.rolling(window=lookback).std()
        
        zscore = (spread - spread_mean) / spread_std
        return zscore
    
    def get_cointegrated_pairs_dict(self) -> Dict[str, str]:
        """
        Get dictionary of cointegrated pairs.
        
        Returns:
            Dictionary mapping symbol_a to symbol_b
        """
        return self.cointegrated_pairs or self.correlated_pairs
    
    def update_cointegrated_pairs(self, symbols: List[str],
                                  p_value_threshold: float = 0.05,
                                  max_half_life: float = 48) -> Dict[str, CointegrationResult]:
        """
        Update the cointegrated pairs dictionary.
        
        Args:
            symbols: List of symbols to analyze
            p_value_threshold: P-value threshold
            max_half_life: Maximum half-life in hours
            
        Returns:
            Dictionary mapping symbol to CointegrationResult
        """
        pairs = self.find_cointegrated_pairs(symbols, p_value_threshold, max_half_life)
        
        self.cointegrated_pairs = {}
        self.cointegration_results = {}
        
        for result in pairs:
            self.cointegrated_pairs[result.symbol_a] = result.symbol_b
            self.cointegration_results[result.symbol_a] = result
        
        self.last_update = datetime.now()
        return self.cointegration_results
    
    def get_cointegration_result(self, symbol: str) -> Optional[CointegrationResult]:
        """Get cointegration result for a symbol."""
        return self.cointegration_results.get(symbol)
