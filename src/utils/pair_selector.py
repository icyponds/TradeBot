"""
Dynamic pair selector for trading based on sophisticated multi-factor scoring.

Supports:
- Native perpetuals (BTC, ETH, SOL, etc.)
- HIP-3 perpetuals (builder-deployed markets)
- Spot markets (optional)

Selection Factors:
- Liquidity Score: OI + Volume + Spread quality
- Volatility Score: Optimal volatility range (not too low, not too high)
- Strategy Fit Score: How well the asset fits active strategies
- Diversification Score: Correlation penalty for portfolio diversification
- Historical Performance Score: Past trading performance on the ticker
"""

import logging
import time
import threading
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timedelta
from collections import defaultdict
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from enum import Enum
from src.utils.statistics import adfuller, engle_granger, calculate_annualized_volatility



class SelectionMode(Enum):
    """Pair selection modes."""
    SIMPLE = "simple"              # Basic OI + Volume scoring
    SOPHISTICATED = "sophisticated" # Full multi-factor scoring
    MOMENTUM = "momentum"          # Favor trending assets
    MEAN_REVERSION = "mean_reversion"  # Favor mean-reverting assets
    BALANCED = "balanced"          # Equal weight all factors


@dataclass
class AssetMetrics:
    """Comprehensive metrics for an asset."""
    symbol: str
    market_type: str
    is_hip3: bool
    
    # Raw data
    open_interest: float = 0.0
    volume_24h: float = 0.0
    mark_price: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    funding_rate: float = 0.0
    
    # Calculated scores (0-1 scale)
    liquidity_score: float = 0.0
    volatility_score: float = 0.0
    strategy_fit_score: float = 0.0
    diversification_score: float = 1.0  # Start at 1 (no penalty)
    historical_perf_score: float = 0.5  # Neutral default
    momentum_score: float = 0.5
    mean_reversion_score: float = 0.5
    
    # Final composite
    composite_score: float = 0.0
    
    # Additional metadata
    volatility: float = 0.0
    returns_1d: float = 0.0
    returns_7d: float = 0.0
    sharpe_ratio: float = 0.0
    max_leverage: float = 1.0
    dex: str = ''


class DynamicPairSelector:
    """
    Sophisticated pair selector using multi-factor scoring.
    
    Selection factors and their weights (configurable):
    - Liquidity (25%): OI + Volume + Spread quality
    - Volatility (20%): Optimal volatility range
    - Strategy Fit (25%): Match to active strategies
    - Diversification (15%): Correlation penalty
    - Historical Performance (15%): Past trading results
    """
    
    def __init__(self, config: Dict[str, Any], market_api, strategy_manager=None,
                 performance_tracker=None, correlation_manager=None):
        """
        Initialize the dynamic pair selector.
        
        Args:
            config: Configuration dictionary
            market_api: Market data API instance
            strategy_manager: Strategy manager instance (optional)
            performance_tracker: Performance tracker for historical data (optional)
            correlation_manager: Correlation manager for diversification (optional)
        """
        self.config = config
        self.market_api = market_api
        self.strategy_manager = strategy_manager
        self.performance_tracker = performance_tracker
        self.correlation_manager = correlation_manager
        self.logger = logging.getLogger(__name__)
        
        # Concurrency control
        self._pairs_lock = threading.Lock()
        self._backfill_lock = threading.Lock()
        
        # Trading configuration
        self.dynamic_selection = config['trading']['dynamic_pair_selection']
        self.min_open_interest = config['trading']['min_open_interest']
        self.scan_interval_minutes = config['trading']['scan_interval_minutes']
        self.excluded_assets = config['trading']['excluded_assets']
        self.included_assets = config['trading']['included_assets']
        
        # HIP-3 configuration
        hip3_config = config.get('hip3', {})
        self.hip3_enabled = hip3_config.get('enabled', False)
        self.hip3_include_in_selection = hip3_config.get('include_in_pair_selection', True)
        self.hip3_min_volume = hip3_config.get('min_volume', 10000)
        self.hip3_max_leverage = hip3_config.get('max_leverage', 5)
        
        # Spot configuration
        spot_config = config.get('spot', {})
        self.spot_enabled = spot_config.get('enabled', False)
        self.spot_include_in_selection = spot_config.get('include_in_pair_selection', False)
        self.spot_min_volume = spot_config.get('min_volume', 50000)
        
        # Sophisticated selection configuration
        selection_config = config.get('pair_selection', {})
        self.selection_mode = SelectionMode(
            selection_config.get('mode', 'sophisticated')
        )
        
        # Score weights (must sum to 1.0)
        weights = selection_config.get('weights', {})
        self.weight_liquidity = weights.get('liquidity', 0.25)
        self.weight_volatility = weights.get('volatility', 0.20)
        self.weight_strategy_fit = weights.get('strategy_fit', 0.25)
        self.weight_diversification = weights.get('diversification', 0.15)
        self.weight_historical = weights.get('historical_performance', 0.15)
        
        # Volatility parameters (Annualized)
        volatility_config = selection_config.get('volatility', {})
        # Defaults ~ 40% - 150% Annualized Volatility
        self.optimal_volatility_min = volatility_config.get('optimal_min', 0.40)
        self.optimal_volatility_max = volatility_config.get('optimal_max', 1.50)
        self.volatility_lookback_days = volatility_config.get('lookback_days', 14)
        
        # Cluster parameters
        cluster_config = selection_config.get('cluster_selection', {})
        self.cluster_enabled = cluster_config.get('enabled', False)
        self.cluster_k = cluster_config.get('k_clusters', 7)
        self.cluster_lookback = cluster_config.get('lookback_days', 30)
        
        if self.cluster_enabled:
            from src.utils.cluster_manager import ClusterManager
            self.cluster_manager = ClusterManager(self.market_api)
        else:
            self.cluster_manager = None
            
        # Diversification parameters
        diversification_config = selection_config.get('diversification', {})
        self.max_correlation = diversification_config.get('max_correlation', 0.7)
        self.correlation_penalty_factor = diversification_config.get('penalty_factor', 0.5)
        
        # State tracking
        self.selected_pairs = []
        self.ready_pairs = set()  # Assets fully loaded and ready to trade
        self.selected_pairs_metadata = {}
        self.asset_metrics: Dict[str, AssetMetrics] = {}
        self.price_history: Dict[str, pd.Series] = {}
        self.correlation_matrix: Optional[pd.DataFrame] = None
        self.last_scan_time = None
        self.pair_history = {}
        self.backfill_queue: List[Dict[str, Any]] = []
        self.backfill_symbols_in_queue: set = set()
        self.last_backfill_time: Optional[datetime] = None
        
        # Threading for independent data fetching
        self._backfill_thread: Optional[threading.Thread] = None
        self._backfill_running = False
        self._backfill_lock = threading.Lock()  # Protects backfill_queue access
        self._data_lock = threading.Lock()  # Protects price_history access
        self._pairs_lock = threading.Lock()  # Protects selected_pairs access (prevents race with background fetcher)
        
        self.logger.info(f"Initialized DynamicPairSelector (Mode: {self.selection_mode.value})")
        self.logger.info(f"  Weights: Liquidity={self.weight_liquidity:.0%}, Volatility={self.weight_volatility:.0%}, "
                        f"Strategy={self.weight_strategy_fit:.0%}, Diversification={self.weight_diversification:.0%}, "
                        f"Historical={self.weight_historical:.0%}")
        self.logger.info(f"  Optimal volatility range: {self.optimal_volatility_min:.1%} - {self.optimal_volatility_max:.1%}")
        self.logger.info(f"  HIP-3 enabled: {self.hip3_enabled}, Spot enabled: {self.spot_enabled}")
    
    def _get_max_pairs_to_trade(self) -> int:
        """
        Get the maximum number of pairs to trade.
        
        Returns:
            Maximum number of pairs to trade
        """
        if self.strategy_manager:
            return self.strategy_manager.get_max_pairs_to_trade()
        else:
            # Fallback to config if strategy manager is not available
            return self.config['trading']['max_pairs_to_trade']
    
    # =========================================================================
    # SOPHISTICATED SCORING METHODS
    # =========================================================================
    
    def _calculate_liquidity_score(self, asset: Dict[str, Any]) -> float:
        """
        Calculate liquidity score based on OI, volume, and spread.
        
        Components:
        - Open Interest (normalized, 40% of liquidity score)
        - 24h Volume (normalized, 40% of liquidity score)
        - Bid-Ask Spread (inverse, 20% of liquidity score)
        
        Returns:
            Float between 0 and 1
        """
        try:
            oi = float(asset.get('openInterest', 0))
            volume = float(asset.get('volume24h', 0))
            bid = float(asset.get('bid', 0))
            ask = float(asset.get('ask', 0))
            
            # Normalize OI (sigmoid-like normalization)
            # $10M OI = 0.5 score, $100M = ~0.9
            oi_million = oi / 1_000_000
            oi_score = oi_million / (oi_million + 10)  # Asymptotic to 1
            
            # Normalize Volume similarly
            vol_million = volume / 1_000_000
            vol_score = vol_million / (vol_million + 5)
            
            # Spread score (lower spread = higher score)
            if bid > 0 and ask > 0:
                spread_bps = ((ask - bid) / bid) * 10000  # Basis points
                # 10 bps = 0.9 score, 100 bps = ~0.5 score
                spread_score = max(0, 1 - (spread_bps / 200))
            else:
                spread_score = 0.5  # Neutral if no bid/ask
            
            # Combine with weights
            liquidity_score = (
                oi_score * 0.4 +
                vol_score * 0.4 +
                spread_score * 0.2
            )
            
            return min(1.0, max(0.0, liquidity_score))
            
        except Exception as e:
            self.logger.debug(f"Error calculating liquidity score: {e}")
            return 0.0
    
    def _calculate_volatility_score(self, symbol: str, asset: Dict[str, Any]) -> Tuple[float, float]:
        """
        Calculate volatility score favoring optimal volatility range.
        
        Too low volatility = not enough profit opportunity
        Too high volatility = excessive risk
        Optimal range is configurable (default 2-8% daily)
        
        Returns:
            Tuple of (volatility_score, raw_volatility)
        """
        try:
            # Try to get historical prices
            prices = self._get_price_history(symbol)
            
            if prices is None or len(prices) < 5:
                # Use a proxy from 24h data if available
                price = float(asset.get('markPrice', 0))
                if price > 0:
                    # Estimate volatility from 24h high/low if available
                    high_24h = float(asset.get('high24h', price * 1.02))
                    low_24h = float(asset.get('low24h', price * 0.98))
                    estimated_vol = (high_24h - low_24h) / price
                    volatility = estimated_vol
                else:
                    return 0.5, 0.0  # Neutral if no data
            else:
                # Calculate actual annualized volatility
                prices_series = prices
                if isinstance(prices, pd.DataFrame):
                    prices_series = prices['close']
                    
                volatility = calculate_annualized_volatility(prices_series)
            
            # Score based on optimal range
            if self.optimal_volatility_min <= volatility <= self.optimal_volatility_max:
                # In optimal range - full score
                vol_score = 1.0
            elif volatility < self.optimal_volatility_min:
                # Too low - penalize proportionally
                vol_score = volatility / self.optimal_volatility_min
            else:
                # Too high - penalize proportionally
                excess = volatility - self.optimal_volatility_max
                vol_score = max(0, 1 - (excess / self.optimal_volatility_max))
            
            return min(1.0, max(0.0, vol_score)), volatility
            
        except Exception as e:
            self.logger.debug(f"Error calculating volatility score for {symbol}: {e}")
            return 0.5, 0.0
    
    def _calculate_strategy_fit_score(self, symbol: str, asset: Dict[str, Any],
                                       volatility: float, btc_history: Optional[pd.Series] = None) -> Tuple[float, float, float]:
        """
        Calculate how well an asset fits the active trading strategies.
        
        Returns:
            Tuple of (strategy_fit_score, momentum_score, mean_reversion_score)
        """
        try:
            momentum_score = 0.5
            mean_reversion_score = 0.5
            
            prices = self._get_price_history(symbol)
            
            if prices is not None and len(prices) >= 7:
                returns = prices.pct_change().dropna()
                
                # Calculate momentum (7-day return)
                if len(returns) >= 7:
                    momentum_7d = (prices.iloc[-1] / prices.iloc[-7]) - 1
                    # Strong momentum (>5%) = high momentum score
                    momentum_score = min(1.0, abs(momentum_7d) / 0.10)
                    
                    # Also store directional momentum
                    asset['returns_7d'] = momentum_7d
                
                # Calculate mean reversion potential (z-score from 20-day mean)
                if len(prices) >= 20:
                    mean_20 = prices.rolling(20).mean().iloc[-1]
                    std_20 = prices.rolling(20).std().iloc[-1]
                    if std_20 > 0:
                        z_score = abs((prices.iloc[-1] - mean_20) / std_20)
                        # Z-score > 2 = good mean reversion opportunity
                        mean_reversion_score = min(1.0, z_score / 3.0)
            
            # Calculate Cointegration/Stationarity
            cointegration_score = 0.5
            stationarity_score = 0.5
            
            # Check CorrelationManager for specific cointegrated pairs
            if self.correlation_manager:
                c_result = self.correlation_manager.get_cointegration_result(symbol)
                if c_result and c_result.is_cointegrated:
                    cointegration_score = 1.0
                    # self.logger.debug(f"Boosted cointegration score for {symbol} (paired with {c_result.symbol_b})")
            
            # Fallback/Supplemental: Check vs BTC (Market Leader Benchmark)
            # Only if not already found by CorrelationManager
            if cointegration_score < 1.0 and prices is not None and len(prices) > 30:
                try:
                    # 1. Stationarity (ADF Test)
                    ts_t, ts_p = adfuller(prices)
                    if ts_p < 0.05:
                        stationarity_score = 1.0
                    elif ts_p < 0.20:
                        stationarity_score = 0.8
                    
                    # 2. Cointegration vs BTC
                    if btc_history is not None and len(btc_history) > 30 and symbol != 'BTC':
                        # Align series
                        aligned_df = pd.DataFrame({'a': prices, 'b': btc_history}).dropna()
                        if len(aligned_df) > 30:
                            c_t, c_p, _ = engle_granger(aligned_df['a'], aligned_df['b'])
                            if c_p < 0.05:
                                cointegration_score = 1.0
                            elif c_p < 0.10:
                                cointegration_score = 0.8
                except Exception:
                    pass

            # Boost scores based on stats
            # If stationary, massive boost to mean reversion
            if stationarity_score > 0.5:
                mean_reversion_score = max(mean_reversion_score, stationarity_score)
            
            # Get active strategies and their types
            active_strategies = self._get_active_strategy_types()
            
            # Weight by active strategy types
            if not active_strategies:
                # No strategy info - use balanced score
                strategy_fit_score = (momentum_score + mean_reversion_score + cointegration_score) / 3
            else:
                momentum_weight = 0.0
                mean_rev_weight = 0.0
                arb_weight = 0.0
                
                for strategy_type in active_strategies:
                    if strategy_type in ['momentum_factor', 'supertrend', 'moving_average', 'cross_sectional_momentum']:
                        momentum_weight += 1
                    elif strategy_type in ['ou_mean_reversion', 'vwap', 'rsi', 'bollinger_band', 'adaptive_grid', 'liquidation_hunter']:
                        mean_rev_weight += 1
                    elif strategy_type in ['stat_arb', 'funding_rate_arbitrage']:
                        arb_weight += 1
                
                total = momentum_weight + mean_rev_weight + arb_weight
                if total > 0:
                    momentum_weight /= total
                    mean_rev_weight /= total
                    arb_weight /= total
                else:
                    momentum_weight = mean_rev_weight = arb_weight = 1/3
                
                # For arb strategies, prefer liquid assets with reasonable volatility AND Cointegration
                # Arb score = Liquidity(40%) + Volatility(20%) + Cointegration(40%)
                arb_score = self._calculate_liquidity_score(asset) * 0.4 + \
                           (1 - volatility / 0.1) * 0.2 + \
                           cointegration_score * 0.4
                
                strategy_fit_score = (
                    momentum_score * momentum_weight +
                    mean_reversion_score * mean_rev_weight +
                    arb_score * arb_weight
                )
            
            return min(1.0, max(0.0, strategy_fit_score)), momentum_score, mean_reversion_score
            
        except Exception as e:
            self.logger.debug(f"Error calculating strategy fit score for {symbol}: {e}")
            return 0.5, 0.5, 0.5
    
    def _calculate_diversification_score(self, symbol: str, 
                                          already_selected: List[str]) -> float:
        """
        Calculate diversification score with correlation penalty.
        
        Assets that are highly correlated with already-selected assets
        receive a penalty to encourage portfolio diversification.
        
        Returns:
            Float between 0 and 1 (1 = not correlated, good for diversification)
        """
        if not already_selected:
            return 1.0  # First asset gets full score
        
        try:
            # Get or build correlation matrix
            if self.correlation_matrix is None:
                self._build_correlation_matrix()
            
            if self.correlation_matrix is None or symbol not in self.correlation_matrix.columns:
                return 0.8  # Slight penalty for unknown correlation
            
            # Calculate average correlation with already selected assets
            correlations = []
            for selected in already_selected:
                if selected in self.correlation_matrix.columns:
                    corr = abs(self.correlation_matrix.loc[symbol, selected])
                    correlations.append(corr)
            
            if not correlations:
                return 0.8
            
            avg_correlation = np.mean(correlations)
            max_correlation = max(correlations)
            
            # Penalty based on max correlation (worst case)
            if max_correlation > self.max_correlation:
                # High correlation penalty
                penalty = (max_correlation - self.max_correlation) * self.correlation_penalty_factor
                diversification_score = max(0.2, 1 - penalty)
            else:
                # Low correlation bonus
                diversification_score = 1.0 - (avg_correlation * 0.3)
            
            return min(1.0, max(0.0, diversification_score))
            
        except Exception as e:
            self.logger.debug(f"Error calculating diversification score for {symbol}: {e}")
            return 0.7
    
    def _calculate_historical_performance_score(self, symbol: str) -> Tuple[float, float]:
        """
        Calculate score based on historical trading performance on this ticker.
        
        Uses performance tracker data if available.
        
        Returns:
            Tuple of (performance_score, sharpe_ratio)
        """
        try:
            # Check pair history (local tracking)
            if symbol in self.pair_history:
                history = self.pair_history[symbol]
                total_pnl = history.get('total_pnl', 0)
                trade_count = history.get('trade_count', 0)
                
                if trade_count >= 3:  # Need minimum trades for significance
                    avg_pnl = total_pnl / trade_count
                    # Normalize: +$100 avg = 0.75, +$500 avg = ~0.9
                    if avg_pnl > 0:
                        perf_score = 0.5 + min(0.5, avg_pnl / 1000)
                    else:
                        perf_score = 0.5 - min(0.5, abs(avg_pnl) / 500)
                    
                    return min(1.0, max(0.0, perf_score)), 0.0
            
            # Check performance tracker if available
            if self.performance_tracker:
                try:
                    metrics = self.performance_tracker.get_symbol_metrics(symbol)
                    if metrics:
                        sharpe = metrics.get('sharpe_ratio', 0)
                        win_rate = metrics.get('win_rate', 0.5)
                        
                        # Combine Sharpe and win rate
                        sharpe_score = min(1.0, max(0, (sharpe + 1) / 4))  # Sharpe -1 to 3 -> 0 to 1
                        win_score = win_rate
                        
                        perf_score = sharpe_score * 0.6 + win_score * 0.4
                        return perf_score, sharpe
                except Exception:
                    self.logger.debug(f"Error getting symbol metrics for {symbol}", exc_info=True)
            
            # No history - neutral score
            return 0.5, 0.0
            
        except Exception as e:
            self.logger.debug(f"Error calculating historical performance for {symbol}: {e}")
            return 0.5, 0.0
    
    def _get_price_history(self, symbol: str, market_type: str = None) -> Optional[pd.Series]:
        """
        Get price history for a symbol.
        
        Caches results for efficiency. Thread-safe.
        """
        # Check cache first (thread-safe read)
        with self._data_lock:
            if symbol in self.price_history:
                return self.price_history[symbol]
        
        try:
            # Try to get OHLCV data from market API
            if hasattr(self.market_api, 'get_ohlcv'):
                ohlcv = self.market_api.get_ohlcv(
                    symbol, 
                    timeframe='1d',
                    limit=self.volatility_lookback_days + 5,
                    market_type=market_type
                )
                if ohlcv and len(ohlcv) > 0:
                    df = pd.DataFrame(ohlcv)
                    if 'close' in df.columns:
                        prices = df['close'].astype(float)
                        with self._data_lock:
                            self.price_history[symbol] = prices
                        return prices
            
            # Fallback: try to get from WebSocket price history
            if hasattr(self.market_api, 'get_price_history'):
                prices = self.market_api.get_price_history(symbol)
                if prices and len(prices) > 0:
                    price_series = pd.Series(prices)
                    with self._data_lock:
                        self.price_history[symbol] = price_series
                    return price_series
            
            return None
            
        except Exception as e:
            self.logger.debug(f"Error getting price history for {symbol}: {e}")
            return None
    
    def _build_correlation_matrix(self):
        """Build correlation matrix from available price history."""
        try:
            if len(self.price_history) < 2:
                return
            
            # Build DataFrame from price histories
            price_data = {}
            min_length = float('inf')
            
            for symbol, prices in self.price_history.items():
                if len(prices) >= 5:
                    price_data[symbol] = prices.values[-30:]  # Last 30 data points
                    min_length = min(min_length, len(price_data[symbol]))
            
            if len(price_data) < 2:
                return
            
            # Align lengths
            aligned_data = {k: v[-int(min_length):] for k, v in price_data.items()}
            
            df = pd.DataFrame(aligned_data)
            returns = df.pct_change().dropna()
            
            if len(returns) >= 5:
                self.correlation_matrix = returns.corr()
                
        except Exception as e:
            self.logger.debug(f"Error building correlation matrix: {e}")
    
    def _get_active_strategy_types(self) -> List[str]:
        """Get list of active strategy types from strategy manager."""
        if not self.strategy_manager:
            return []
        
        try:
            if hasattr(self.strategy_manager, 'get_active_strategies'):
                strategies = self.strategy_manager.get_active_strategies()
                return [s.name if hasattr(s, 'name') else str(s) for s in strategies]
            elif hasattr(self.strategy_manager, 'strategies'):
                return list(self.strategy_manager.strategies.keys())
        except Exception:
            self.logger.debug("Error getting active strategy types", exc_info=True)
        
        return []
    
    def _calculate_asset_metrics(self, asset: Dict[str, Any], btc_history: Optional[pd.Series] = None) -> Optional[AssetMetrics]:
        """
        Calculate metrics for a single asset.
        
        Used by background fetcher to evaluate newly-loaded assets.
        """
        try:
            symbol = asset.get('name', '')
            market_type = asset.get('market_type', 'perp')
            is_hip3 = asset.get('is_hip3', False)
            
            if not symbol:
                return None
            
            # Create metrics object
            metrics = AssetMetrics(
                symbol=symbol,
                market_type=market_type,
                is_hip3=is_hip3,
                open_interest=float(asset.get('openInterest', 0)),
                volume_24h=float(asset.get('volume24h', asset.get('volume_24h', 0))),
                mark_price=float(asset.get('markPrice', 0)),
                bid=float(asset.get('bid', 0)),
                ask=float(asset.get('ask', 0)),
                funding_rate=float(asset.get('fundingRate', 0)),
                max_leverage=float(asset.get('maxLeverage', 1)),
                dex=asset.get('dex', ''),
            )
            
            # Calculate individual scores
            metrics.liquidity_score = self._calculate_liquidity_score(asset)
            metrics.volatility_score, metrics.volatility = self._calculate_volatility_score(symbol, asset)
            (metrics.strategy_fit_score, 
             metrics.momentum_score, 
             metrics.mean_reversion_score) = self._calculate_strategy_fit_score(symbol, asset, metrics.volatility, btc_history)
            metrics.historical_perf_score, metrics.sharpe_ratio = self._calculate_historical_performance_score(symbol)
            
            # Calculate composite score
            metrics.composite_score = self._calculate_composite_score(metrics, [])
            
            # Store for later reference
            self.asset_metrics[symbol] = metrics
            
            return metrics
            
        except Exception as e:
            self.logger.debug(f"Error calculating metrics for {asset.get('name', '?')}: {e}")
            return None
    
    def _calculate_composite_score(self, metrics: AssetMetrics, 
                                    already_selected: List[str]) -> float:
        """
        Calculate final composite score based on selection mode.
        """
        # Update diversification score based on current selection
        metrics.diversification_score = self._calculate_diversification_score(
            metrics.symbol, already_selected
        )
        
        if self.selection_mode == SelectionMode.SIMPLE:
            # Simple mode: just liquidity
            return metrics.liquidity_score
        
        elif self.selection_mode == SelectionMode.MOMENTUM:
            # Momentum mode: favor trending assets
            return (
                metrics.liquidity_score * 0.3 +
                metrics.momentum_score * 0.5 +
                metrics.volatility_score * 0.2
            )
        
        elif self.selection_mode == SelectionMode.MEAN_REVERSION:
            # Mean reversion mode: favor deviating assets
            return (
                metrics.liquidity_score * 0.3 +
                metrics.mean_reversion_score * 0.5 +
                metrics.volatility_score * 0.2
            )
        
        elif self.selection_mode == SelectionMode.BALANCED:
            # Equal weight all factors
            return (
                metrics.liquidity_score * 0.2 +
                metrics.volatility_score * 0.2 +
                metrics.strategy_fit_score * 0.2 +
                metrics.diversification_score * 0.2 +
                metrics.historical_perf_score * 0.2
            )
        
        else:  # SOPHISTICATED (default)
            return (
                metrics.liquidity_score * self.weight_liquidity +
                metrics.volatility_score * self.weight_volatility +
                metrics.strategy_fit_score * self.weight_strategy_fit +
                metrics.diversification_score * self.weight_diversification +
                metrics.historical_perf_score * self.weight_historical
            )
    
    def scan_and_select_pairs(self) -> List[str]:
        """
        Scan available assets and select trading pairs based on criteria.
        
        Includes:
        - Native perpetuals (always)
        - HIP-3 perpetuals (if enabled and include_in_pair_selection)
        - Spot markets (if enabled and include_in_pair_selection)
        
        Returns:
            List of selected trading pairs
        """
        if not self.dynamic_selection:
            self.logger.info("Dynamic pair selection is disabled")
            return []
            
        try:
            all_assets = []
            
            # 1. Get native perpetuals (always)
            self.logger.info("Scanning native perpetuals...")
            native_assets = self._get_native_perp_assets()
            all_assets.extend(native_assets)
            self.logger.info(f"  Found {len(native_assets)} native perps")
            
            # 2. Get HIP-3 perpetuals (if enabled)
            if self.hip3_enabled and self.hip3_include_in_selection:
                self.logger.info("Scanning HIP-3 perpetuals...")
                hip3_assets = self._get_hip3_perp_assets()
                all_assets.extend(hip3_assets)
                self.logger.info(f"  Found {len(hip3_assets)} HIP-3 perps")
            
            # 3. Get spot markets (if enabled)
            if self.spot_enabled and self.spot_include_in_selection:
                self.logger.info("Scanning spot markets...")
                spot_assets = self._get_spot_assets()
                all_assets.extend(spot_assets)
                self.logger.info(f"  Found {len(spot_assets)} spot pairs")
            
            self.logger.info(f"Total: Scanning {len(all_assets)} available assets")
            
            # Fetch BTC history once for cointegration checks
            btc_history = self._get_price_history('BTC')
            
            # Filter and rank assets
            eligible_pairs = self._filter_assets(all_assets)
            selected_pairs, pairs_metadata = self._rank_and_select_pairs(eligible_pairs, btc_history)
            
            # Update state (thread-safe)
            # IMPORTANT: In background mode, _rank_and_select_pairs returns ([], {})
            # and the actual pair selection happens incrementally in the background fetcher.
            # Only overwrite if we got actual results (synchronous mode, if ever used).
            if selected_pairs:
                with self._pairs_lock:
                    self.selected_pairs = selected_pairs
                    self.selected_pairs_metadata = pairs_metadata
                self.logger.info(f"Selected {len(selected_pairs)} pairs for trading: {selected_pairs}")
                
                # Subscribe to WebSocket feeds for selected pairs (sync mode)
                
                # Fetch required timeframes
                req_timeframes = ['15m', '1h'] # Safe default
                if hasattr(self, 'strategy_manager') and self.strategy_manager:
                    if hasattr(self.strategy_manager, 'get_required_timeframes'):
                        req_timeframes = self.strategy_manager.get_required_timeframes()
                
                for symbol in selected_pairs:
                    if hasattr(self.market_api, 'subscribe_symbol'):
                        self.market_api.subscribe_symbol(symbol, required_timeframes=req_timeframes)
                        time.sleep(0.1)
            else:
                self.logger.info("Pair selection queued for background processing")
            
            self.last_scan_time = datetime.now()
            
            # Start background data fetcher if there are queued assets
            if self.backfill_queue and not self._backfill_running:
                self.start_background_fetcher()
            
            return selected_pairs
            
        except Exception as e:
            self.logger.error(f"Error scanning and selecting pairs: {e}")
            # Ensure we don't retry immediately in a tight loop
            self.last_scan_time = datetime.now()
            return []
    
    def _get_native_perp_assets(self) -> List[Dict[str, Any]]:
        """Get native perpetual assets with metadata."""
        try:
            asset_info = self.market_api.get_asset_info()
            if not asset_info:
                return []
            
            assets = []
            for asset in asset_info.get('universe', []):
                asset['market_type'] = 'perp'
                asset['dex'] = ''  # Native dex
                asset['is_hip3'] = False
                assets.append(asset)
            
            return assets
        except Exception as e:
            self.logger.error(f"Error getting native perp assets: {e}")
            return []
    
    def _get_hip3_perp_assets(self) -> List[Dict[str, Any]]:
        """Get HIP-3 perpetual assets with metadata."""
        try:
            if not hasattr(self.market_api, 'get_all_perp_assets'):
                return []
            
            # get_all_perp_assets includes both native and HIP-3
            # We only want HIP-3 here (is_hip3 = True)
            all_perps = self.market_api.get_all_perp_assets(include_hip3=True)
            
            hip3_assets = []
            for asset in all_perps:
                if asset.get('is_hip3', False):
                    asset['market_type'] = 'perp'
                    # Apply HIP-3 specific filters
                    volume = float(asset.get('volume24h', 0))
                    if volume >= self.hip3_min_volume:
                        hip3_assets.append(asset)
            
            return hip3_assets
        except Exception as e:
            self.logger.error(f"Error getting HIP-3 perp assets: {e}")
            return []
    
    def _get_spot_assets(self) -> List[Dict[str, Any]]:
        """Get spot market assets with metadata."""
        try:
            if not hasattr(self.market_api, 'get_spot_meta_and_asset_ctxs'):
                return []
            
            result = self.market_api.get_spot_meta_and_asset_ctxs()
            if not result:
                return []
            
            spot_meta, asset_contexts = result
            
            assets = []
            for i, pair in enumerate(spot_meta.get('universe', [])):
                tokens = pair.get('tokens', [])
                if len(tokens) < 2:
                    continue
                
                token_list = spot_meta.get('tokens', [])
                base_idx, quote_idx = tokens[0], tokens[1]
                
                if base_idx >= len(token_list) or quote_idx >= len(token_list):
                    continue
                
                base_name = token_list[base_idx].get('name', '')
                quote_name = token_list[quote_idx].get('name', '')
                
                # Get asset context for market data using explicit index from metadata
                # (Universe index 'i' may not match context index if lists differ in length)
                ctx_idx = pair.get('index', i)
                ctx = asset_contexts[ctx_idx] if ctx_idx < len(asset_contexts) else {}
                
                volume_24h = float(ctx.get('dayNtlVlm', 0))
                
                # Apply spot minimum volume filter
                if volume_24h < self.spot_min_volume:
                    continue
                
                # Use base token name for spot pairs to enable correlation with perps
                # e.g., "BTC" instead of "@1" - the API can resolve "BTC/USDC" to "@1" for orders
                # Safely get midPx with fallback to 0 if None
                mid_px = ctx.get('midPx')
                mark_price = float(mid_px) if mid_px is not None else 0.0
                
                assets.append({
                    'name': base_name,  # Human-readable name (matches perp naming)
                    'display_name': f"{base_name}/{quote_name}",  # Full pair display
                    'api_name': pair.get('name', ''),  # Internal API name (@1, @109, etc.)
                    'base': base_name,
                    'quote': quote_name,
                    'market_type': 'spot',
                    'dex': '',
                    'is_hip3': False,
                    'markPrice': mark_price,
                    'volume24h': volume_24h,
                    'openInterest': 0,  # Spot doesn't have OI
                    'maxLeverage': 1,   # No leverage on spot
                })
                

            
            return assets
        except Exception as e:
            self.logger.error(f"Error getting spot assets: {e}")
            return []
    
    

    
    def _filter_assets(self, universe: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Filter assets based on criteria for each market type.
        
        Args:
            universe: List of all available assets with market data
            
        Returns:
            List of eligible assets
        """
        eligible_assets = []
        
        try:
            # Debug: Show first few assets
            self.logger.debug(f"Processing {len(universe)} assets")
            for i, asset in enumerate(universe[:5]):
                self.logger.debug(f"Sample asset {i}: {asset}")
            
            for asset in universe:
                asset_name = asset.get('name', '')
                market_type = asset.get('market_type', 'perp')
                is_hip3 = asset.get('is_hip3', False)
                
                # Skip if explicitly excluded
                if asset_name in self.excluded_assets:
                    continue
                
                # Skip if included assets are specified and this asset is not in the list
                if self.included_assets and asset_name not in self.included_assets:
                    continue
                
                # Extract market data from asset
                open_interest_dollars = float(asset.get('openInterest', 0))
                volume_24h = float(asset.get('volume24h', 0))
                mark_price = float(asset.get('markPrice', 0))
                
                # Debug logging for first few assets
                if len(eligible_assets) < 3:
                    type_label = f"HIP-3 {market_type}" if is_hip3 else market_type
                    self.logger.debug(f"Asset {asset_name} ({type_label}): OI=${open_interest_dollars:,.0f}, Vol=${volume_24h:,.0f}, Price=${mark_price:.2f}")
                
                # Apply market-type-specific filters
                if market_type == 'spot':
                    # Spot: filter by volume only (no OI)
                    if not self._is_spot_eligible(asset, volume_24h, mark_price):
                        continue
                elif is_hip3:
                    # HIP-3 perp: different thresholds
                    if not self._is_hip3_perp_eligible(asset, open_interest_dollars, volume_24h, mark_price):
                        continue
                else:
                    # Native perp: standard thresholds
                    if not self._is_native_perp_eligible(asset, open_interest_dollars, volume_24h, mark_price):
                        continue
                
                # Check additional eligibility criteria
                if not self._is_asset_eligible(asset):
                    if len(eligible_assets) < 3:
                        self.logger.debug(f"  Skipped {asset_name}: Failed additional eligibility checks")
                    continue
                
                type_label = f"HIP-3 {market_type}" if is_hip3 else market_type
                self.logger.info(f"Adding {asset_name} ({type_label}) to eligible assets | Vol: ${volume_24h:,.0f} | Price: ${mark_price:.4f}")
                eligible_assets.append(asset)
        
        except Exception as e:
            self.logger.error(f"Error filtering assets: {e}")
            return []
        
        self.logger.info(f"Found {len(eligible_assets)} eligible assets after filtering")
        return eligible_assets
    
    def _is_native_perp_eligible(self, asset: Dict[str, Any], oi: float, volume: float, price: float) -> bool:
        """Check if a native perp meets the eligibility criteria."""
        asset_name = asset.get('name', '')
        
        # Check open interest criteria
        if oi < self.min_open_interest:
            self.logger.debug(f"  Skipped {asset_name}: OI ${oi:,.0f} < min ${self.min_open_interest:,.0f}")
            return False
        
        # Check for minimum volume
        min_volume_threshold = self.config.get('pair_selection', {}).get('min_volume_threshold', 10000)
        if volume < min_volume_threshold:
            self.logger.debug(f"  Skipped {asset_name}: Vol ${volume:,.0f} < min ${min_volume_threshold:,.0f}")
            return False
        
        # Check for valid price
        if price <= 0:
            self.logger.debug(f"  Skipped {asset_name}: Price ${price:.2f} <= 0")
            return False
        
        return True
    
    def _is_hip3_perp_eligible(self, asset: Dict[str, Any], oi: float, volume: float, price: float) -> bool:
        """Check if a HIP-3 perp meets the eligibility criteria."""
        asset_name = asset.get('name', '')
        
        # HIP-3 perps may have lower OI, so use a lower threshold
        hip3_min_oi = self.config.get('hip3', {}).get('min_open_interest', self.min_open_interest / 10)
        if oi < hip3_min_oi:
            self.logger.debug(f"  Skipped HIP-3 {asset_name}: OI ${oi:,.0f} < min ${hip3_min_oi:,.0f}")
            return False
        
        # Use HIP-3 specific volume threshold
        if volume < self.hip3_min_volume:
            self.logger.debug(f"  Skipped HIP-3 {asset_name}: Vol ${volume:,.0f} < min ${self.hip3_min_volume:,.0f}")
            return False
        
        # Check for valid price
        if price <= 0:
            self.logger.debug(f"  Skipped HIP-3 {asset_name}: Price ${price:.2f} <= 0")
            return False
        
        # Check leverage constraint
        max_leverage = float(asset.get('maxLeverage', 1))
        if max_leverage > self.hip3_max_leverage:
            self.logger.debug(f"  Limiting HIP-3 {asset_name} leverage from {max_leverage}x to {self.hip3_max_leverage}x")
            asset['effectiveLeverage'] = self.hip3_max_leverage
        
        return True
    
    def _is_spot_eligible(self, asset: Dict[str, Any], volume: float, price: float) -> bool:
        """Check if a spot pair meets the eligibility criteria."""
        asset_name = asset.get('name', '')
        
        # Spot: only volume-based filtering (no OI)
        if volume < self.spot_min_volume:
            self.logger.debug(f"  Skipped spot {asset_name}: Vol ${volume:,.0f} < min ${self.spot_min_volume:,.0f}")
            return False
        
        # Check for valid price
        if price <= 0:
            self.logger.debug(f"  Skipped spot {asset_name}: Price ${price:.2f} <= 0")
            return False
        
        return True
    
    def _is_asset_eligible(self, asset: Dict[str, Any]) -> bool:
        """
        Check if an asset meets additional eligibility criteria.
        
        Args:
            asset: Asset information dictionary
            
        Returns:
            True if asset is eligible, False otherwise
        """
        try:
            # Check for minimum volume
            volume_24h = float(asset.get('volume24h', 0))
            min_volume_threshold_strict = self.config.get('pair_selection', {}).get('min_volume_threshold_strict', 100000)
            if volume_24h < min_volume_threshold_strict:  # $100k minimum daily volume
                return False
            
            # Check for valid price
            mark_price = float(asset.get('markPrice', 0))
            if mark_price <= 0:
                return False
            
            # Check for reasonable bid-ask spread
            bid = float(asset.get('bid', 0))
            ask = float(asset.get('ask', 0))
            if bid > 0 and ask > 0:
                spread_pct = (ask - bid) / bid * 100
                if spread_pct > 5:  # 5% maximum spread
                    return False
            
            return True
            
        except (ValueError, TypeError):
            return False
    
    def _rank_and_select_pairs(self, eligible_assets: List[Dict[str, Any]], btc_history: Optional[pd.Series] = None) -> tuple:
        """
        Rank eligible assets using volume-based ordering with sequential data loading.
        
        Process:
        1. Rank all perp/HIP-3 assets by 24h volume
        2. For each asset in order:
           - Fetch historical OHLCV data
           - Subscribe to WebSocket streaming
           - If asset has corresponding spot (or vice versa), also load that pair
        3. Respect rate limits (~20 OHLCV requests per minute)
        
        Args:
            eligible_assets: List of eligible assets
            
        Returns:
            Tuple of (list of selected pair symbols, dict of pair metadata)
        """
        if not eligible_assets:
            return [], {}
        
        self.logger.info(f"Ranking {len(eligible_assets)} eligible assets by volume")
        
        # Stop background fetcher if running (we're starting fresh)
        if self._backfill_running:
            self.stop_background_fetcher()
        
        # Only clear queue to prepare for fresh population
        # DO NOT clear price_history or asset_metrics - continuous loop updates incrementally
        with self._backfill_lock:
            self.backfill_queue.clear()
            self.backfill_symbols_in_queue.clear()
        
        # =====================================================================
        # STEP 1: Sort ALL assets by 24h volume (descending)
        # =====================================================================
        def get_volume(asset):
            return float(asset.get('volume24h', 0))
        
        sorted_by_volume = sorted(eligible_assets, key=get_volume, reverse=True)
        
        self.logger.info(f"Top 5 by volume: {[a.get('name', '') for a in sorted_by_volume[:5]]}")
        
        # =====================================================================
        # STEP 2: Unified Background Loading
        # Queue ALL assets for background loading to avoid API rate limits
        # =====================================================================
        
        max_active = self._get_max_pairs_to_trade()
        self.logger.info(f"Queueing {len(sorted_by_volume)} assets for background scouting (Active Set Cap: {max_active})...")
        
        # Add all selected assets to queue immediately
        with self._backfill_lock:
            for asset in sorted_by_volume:
                # User Request: Strictly filter zero volume assets
                if float(asset.get('volume24h', 0)) <= 0:
                    continue
                    
                # 1. Queue the main asset (Perp)
                sym = asset.get('name', '')
                market_type = asset.get('market_type', 'perp')
                queue_key = f"{market_type}:{sym}"
                
                if sym and queue_key not in self.backfill_symbols_in_queue:
                    self.backfill_queue.append(asset)
                    self.backfill_symbols_in_queue.add(queue_key)
                
                # 2. Queue corresponding Spot asset if applicable (ONLY if it passed eligibility filters)
                if market_type == 'perp':
                     if hasattr(self.market_api, 'get_spot_token_for_perp'):
                        spot_token = self.market_api.get_spot_token_for_perp(sym)
                        if spot_token:
                            # Use internal naming convention (e.g., "BTC_SPOT")
                            spot_internal = f"{spot_token}_SPOT" if not spot_token.endswith('_SPOT') else spot_token
                            spot_key = f"spot:{spot_internal}"
                            
                            # Validates that the spot asset itself passed _filter_assets (volume, price, etc.)
                            # We check if spot_internal is in the list of eligible assets
                            
                            # Phase 14: Linked spot assets are ALWAYS eligible if their Perp is subscribed.
                            # We bypass strict eligibility (volume/price) because the user explicitly wants this data for the Perp strategy.
                            # Check if already queued to avoid duplicates
                            if spot_key not in self.backfill_symbols_in_queue:
                                self.logger.info(f"  -> Queueing linked spot pair: {spot_internal} (Bypassing eligibility)")
                                spot_asset = {'name': spot_internal, 'market_type': 'spot'}
                                self.backfill_queue.append(spot_asset)
                                self.backfill_symbols_in_queue.add(spot_key)


        self.logger.info(f"Queued total {len(self.backfill_queue)} assets (Perp/HIP-3 + Spot) for background fetching")
        
        # =====================================================================
        # STEP 3: Start Background Fetcher (Scoring happens there, not here)
        # =====================================================================
        # The background fetcher will:
        # 1. Load data for each asset in priority order (BTC first)
        # 2. Score the asset after loading
        # 3. Add to selected_pairs / ready_pairs if it qualifies
        # 
        # This ensures NO synchronous API calls during scan.
        # The trading loop will use ready_pairs which gets populated by the fetcher.
        
        self.start_background_fetcher()
        
        # Return empty - actual selection happens in _background_data_fetcher
        # as assets are loaded and scored
        return [], {}

    def _select_clustered_pairs(self, scored_pairs: List[Dict[str, Any]], n: int) -> List[str]:
        """
        Select n pairs ensuring diversification across clusters.
        """
        symbols = [p['symbol'] for p in scored_pairs]
        symbol_to_score = {p['symbol']: p for p in scored_pairs}
        
        # 1. Cluster
        try:
            clusters = self.cluster_manager.cluster_assets(
                symbols, 
                n_clusters=self.cluster_k, 
                lookback_days=self.cluster_lookback
            )
        except Exception as e:
            self.logger.error(f"Clustering failed, falling back to top-N: {e}")
            return [p['symbol'] for p in scored_pairs[:n]]
        
        # 2. Organize by cluster
        cluster_queues = {}
        for cid, cluster_symbols in clusters.items():
            # Sort symbols in this cluster by score desc
            sorted_syms = sorted(cluster_symbols, key=lambda s: symbol_to_score[s]['score'], reverse=True)
            cluster_queues[cid] = sorted_syms
            
        # 3. Round Robin Selection
        selected = []
        cluster_ids = sorted(cluster_queues.keys())
        
        while len(selected) < n and any(cluster_queues.values()):
            progress_made = False
            for cid in cluster_ids:
                if len(selected) >= n:
                    break
                queue = cluster_queues[cid]
                if queue:
                    selected.append(queue.pop(0))
                    progress_made = True
            if not progress_made:
                break
                
        return selected
        
        self.logger.info("=" * 60)
        self.logger.info("PAIR SELECTION RANKINGS (by volume + score)")
        self.logger.info("=" * 60)
        
        for metrics in all_metrics:
            # No max_pairs limit - trade all assets that pass quality filters
            
            # Recalculate composite with diversification penalty
            final_score = self._calculate_composite_score(metrics, selected_pairs)
            metrics.composite_score = final_score
            
            # Add to selection
            selected_pairs.append(metrics.symbol)
            
            # Build metadata
            type_label = f"HIP-3 {metrics.market_type}" if metrics.is_hip3 else metrics.market_type
            pairs_metadata[metrics.symbol] = {
                'market_type': metrics.market_type,
                'is_hip3': metrics.is_hip3,
                'dex': metrics.dex,
                'open_interest': metrics.open_interest,
                'volume_24h': metrics.volume_24h,
                'max_leverage': metrics.max_leverage,
                'composite_score': metrics.composite_score,
                'scores': {
                    'liquidity': metrics.liquidity_score,
                    'volatility': metrics.volatility_score,
                    'strategy_fit': metrics.strategy_fit_score,
                    'diversification': metrics.diversification_score,
                    'historical_perf': metrics.historical_perf_score,
                    'momentum': metrics.momentum_score,
                    'mean_reversion': metrics.mean_reversion_score,
                },
                'volatility': metrics.volatility,
                'sharpe_ratio': metrics.sharpe_ratio,
            }
            
            # Log detailed selection info
            rank = len(selected_pairs)
            self.logger.info(
                f"Rank {rank}: {metrics.symbol} ({type_label}) | "
                f"Score: {metrics.composite_score:.3f} | Vol24h: ${metrics.volume_24h:,.0f}"
            )
        
        self.logger.info("=" * 60)
        self.logger.info(f"Selected {len(selected_pairs)} pairs for trading")
        
        if self.backfill_queue:
            self.logger.info(f"Backfill queue: {len(self.backfill_queue)} assets will load gradually while strategies run")
        
        return selected_pairs, pairs_metadata
    
    def get_current_pairs(self, trigger_rescan: bool = True) -> List[str]:
        """
        Get currently selected trading pairs.
        
        Args:
            trigger_rescan: Deprecated, kept for backwards compatibility.
                           Continuous scouting loop handles rotation now.
        
        Returns:
            List of current trading pairs (copy, safe to iterate)
        """
        # Return a copy to prevent race condition with background thread
        with self._pairs_lock:
            return self.selected_pairs.copy()
    
    def get_ready_pairs(self) -> List[str]:
        """
        Get list of pairs that are fully loaded and ready for trading.
        Thread-safe.
        """
        with self._pairs_lock:
            # Only return pairs that are both SELECTED and READY
            return [p for p in self.selected_pairs if p in self.ready_pairs]

    
    def start_background_fetcher(self):
        """Start the background data fetching thread."""
        if self._backfill_thread is not None and self._backfill_thread.is_alive():
            self.logger.debug("Background fetcher already running")
            return
        
        self._backfill_running = True
        self._backfill_thread = threading.Thread(
            target=self._background_data_fetcher,
            name="DataFetcher",
            daemon=True
        )
        self._backfill_thread.start()
        self.logger.info("Started background data fetcher thread")
    
    def stop_background_fetcher(self):
        """Stop the background data fetching thread."""
        self._backfill_running = False
        if self._backfill_thread is not None:
            self._backfill_thread.join(timeout=5.0)
            self._backfill_thread = None
        self.logger.info("Stopped background data fetcher thread")
    
    def _background_data_fetcher(self):
        """
        Background thread that continuously cycles through all assets.
        
        Each cycle:
        1. SCOUTING: Process all assets in queue (fetch 1d, score, rotate)
        2. MAINTENANCE: Refresh candles and run integrity for pool members
        3. WAIT: If cycle < 15 min, wait until 15 min mark, then repopulate and repeat
        """
        RATE_LIMIT_DELAY = 1.5  # ~40 requests per minute
        MIN_CYCLE_INTERVAL_SECONDS = 900  # 15 minutes minimum between cycle starts
        
        self.logger.info("Background data fetcher started (continuous scouting mode)")
        
        while self._backfill_running:
            try:
                cycle_start_time = datetime.now()
                assets_scouted = 0
                
                # ===== PHASE 1: SCOUTING - Process all assets in queue =====
                self.logger.info(f"[Scouting] Starting cycle with {len(self.backfill_queue)} queued assets")
                
                while self._backfill_running:
                    # Get next asset from queue
                    with self._backfill_lock:
                        if not self.backfill_queue:
                            break
                        asset = self.backfill_queue.pop(0)
                    
                    sym = asset.get('name', '')
                    market_type = asset.get('market_type', 'perp')
                    queue_key = f"{market_type}:{sym}"
                    
                    if not sym:
                        continue
                    
                    try:
                        assets_scouted += 1
                        if assets_scouted <= 3 or assets_scouted % 20 == 0:
                            self.logger.info(f"[Scouting] #{assets_scouted}: {sym}")
                        
                        # Fetch 1d data for scoring
                        self._get_price_history(sym, market_type=market_type)
                        
                        with self._backfill_lock:
                            self.backfill_symbols_in_queue.discard(queue_key)
                        
                        # Score and potentially rotate into pool (handles both new and existing)
                        self._try_add_to_trading_pairs(asset)
                        
                        # If newly added to pool, pre-warm all timeframes
                        with self._pairs_lock:
                            is_in_pool = sym in self.selected_pairs
                            needs_warmup = is_in_pool and sym not in self.ready_pairs
                        
                        if needs_warmup:
                            # Use dynamic timeframes or safe default
                            required_timeframes = ['15m', '1h']
                            if hasattr(self, 'strategy_manager') and self.strategy_manager:
                                if hasattr(self.strategy_manager, 'get_required_timeframes'):
                                    required_timeframes = self.strategy_manager.get_required_timeframes()
                            
                            self.logger.info(f"[Scouting] Pre-warming {sym} ({'/'.join(required_timeframes)})...")
                            try:
                                # Run integrity check
                                if hasattr(self.market_api, 'repairer') and self.market_api.repairer:
                                    self.market_api.repairer.process_asset(sym, timeframes=required_timeframes)
                                
                                # Fetch all timeframes (Asynchronously warming the cache)
                                if hasattr(self.market_api, 'warmup_cache'):
                                    self.market_api.warmup_cache([sym], required_timeframes)
                                else:
                                    for tf in required_timeframes:
                                        self.market_api.get_ohlcv(sym, tf, market_type=market_type)
                                
                                # Mark as ready
                                with self._pairs_lock:
                                    self.ready_pairs.add(sym)
                                
                                self.logger.info(f"[Scouting] {sym} now ready for trading")
                            except Exception as e:
                                self.logger.warning(f"[Scouting] Pre-warm failed for {sym}: {e}")
                        
                        # Rate limit delay
                        time.sleep(RATE_LIMIT_DELAY)
                        
                    except Exception as e:
                        msg = str(e)
                        if "Circuit breaker is open" in msg or "429" in msg:
                            self.logger.warning(f"[Scouting] Rate limit at {sym}, waiting 10s...")
                            with self._backfill_lock:
                                self.backfill_queue.append(asset)
                            time.sleep(10.0)
                        else:
                            self.logger.warning(f"[Scouting] Error for {sym}: {e}")
                            with self._backfill_lock:
                                self.backfill_symbols_in_queue.discard(queue_key)
                            time.sleep(RATE_LIMIT_DELAY)
                
                if not self._backfill_running:
                    break
                
                # ===== PHASE 2: MAINTENANCE - Refresh pool members =====
                with self._pairs_lock:
                    pool_members = list(self.selected_pairs)
                
                if pool_members:
                    self.logger.info(f"[Maintenance] Refreshing {len(pool_members)} pool members")
                
                # Get required timeframes from StrategyManager (dynamic, based on active strategies)
                required_timeframes = ['15m', '1h']  # Default fallback
                if hasattr(self, 'strategy_manager') and self.strategy_manager:
                    if hasattr(self.strategy_manager, 'get_required_timeframes'):
                        required_timeframes = self.strategy_manager.get_required_timeframes()
                        self.logger.debug(f"[Maintenance] Using dynamic timeframes: {required_timeframes}")
                
                for sym in pool_members:
                    if not self._backfill_running:
                        break
                    try:
                        # Run integrity check with dynamic timeframes
                        if hasattr(self.market_api, 'repairer') and self.market_api.repairer:
                            self.market_api.repairer.process_asset(sym, timeframes=required_timeframes)
                        
                        # Refresh all required timeframes (Asynchronously warming the cache)
                        if hasattr(self.market_api, 'warmup_cache'):
                            self.market_api.warmup_cache([sym], required_timeframes, limit=10)
                        else:
                            for tf in required_timeframes:
                                self.market_api.get_ohlcv(sym, tf, limit=10)
                        
                        # Ensure in ready_pairs
                        with self._pairs_lock:
                            if sym in self.selected_pairs and sym not in self.ready_pairs:
                                self.ready_pairs.add(sym)
                                self.logger.info(f"[Maintenance] Re-added {sym} to ready_pairs")
                        
                        time.sleep(RATE_LIMIT_DELAY)
                        
                    except Exception as e:
                        self.logger.warning(f"[Maintenance] Error for {sym}: {e}")
                
                if not self._backfill_running:
                    break
                
                # ===== PHASE 3: WAIT AND REPOPULATE =====
                cycle_duration = (datetime.now() - cycle_start_time).total_seconds()
                
                if cycle_duration < MIN_CYCLE_INTERVAL_SECONDS:
                    wait_time = MIN_CYCLE_INTERVAL_SECONDS - cycle_duration
                    self.logger.info(f"[Scouting] Cycle complete ({assets_scouted} assets in {cycle_duration:.0f}s), waiting {wait_time:.0f}s")
                    
                    # Interruptible sleep
                    for _ in range(int(wait_time)):
                        if not self._backfill_running:
                            break
                        time.sleep(1.0)
                else:
                    self.logger.info(f"[Scouting] Cycle took {cycle_duration:.0f}s (>{MIN_CYCLE_INTERVAL_SECONDS}s), starting next immediately")
                
                # Repopulate queue for next cycle
                if self._backfill_running:
                    self._repopulate_scouting_queue()
                
            except Exception as e:
                self.logger.error(f"[BackgroundFetcher] Unexpected error: {e}")
                import traceback
                self.logger.error(traceback.format_exc())
                time.sleep(5.0)
        
        self.logger.info("Background data fetcher stopped")
    
    def _repopulate_scouting_queue(self):
        """Re-populate queue with all eligible assets for a new scouting cycle."""
        try:
            all_assets = []
            all_assets.extend(self._get_native_perp_assets())
            
            if self.hip3_enabled and self.hip3_include_in_selection:
                all_assets.extend(self._get_hip3_perp_assets())
            
            eligible_assets = self._filter_assets(all_assets)
            
            # Sort by volume (highest first for priority)
            sorted_assets = sorted(
                eligible_assets, 
                key=lambda x: float(x.get('volume24h', 0)), 
                reverse=True
            )
            
            with self._backfill_lock:
                self.backfill_queue.clear()
                self.backfill_symbols_in_queue.clear()
                
                for asset in sorted_assets:
                    sym = asset.get('name', '')
                    market_type = asset.get('market_type', 'perp')
                    queue_key = f"{market_type}:{sym}"
                    
                    if sym:
                        self.backfill_queue.append(asset)
                        self.backfill_symbols_in_queue.add(queue_key)
            
            self.logger.info(f"[Scouting] Queued {len(self.backfill_queue)} assets for next cycle")
            
        except Exception as e:
            self.logger.error(f"[Scouting] Failed to repopulate queue: {e}")
    
    def get_backfill_status(self) -> Dict[str, Any]:
        """Get status of the background data fetcher."""
        with self._backfill_lock:
            queue_size = len(self.backfill_queue)
        with self._data_lock:
            loaded_count = len(self.price_history)
        
        return {
            'running': self._backfill_running,
            'queue_size': queue_size,
            'loaded_assets': loaded_count,
            'thread_alive': self._backfill_thread.is_alive() if self._backfill_thread else False
        }
    
    def _try_add_to_trading_pairs(self, asset: Dict[str, Any]):
        """
        Rotational scouting with dynamic rescoring.
        
        For NEW assets: Add if pool has room, or swap if score > lowest pool member
        For EXISTING pool members: Rescore and update stored score in metadata
        
        Thread-safe: Uses _pairs_lock to prevent race conditions with main thread.
        """
        try:
            symbol = asset.get('name', '')
            market_type = asset.get('market_type', 'perp')
            
            if not symbol:
                return
            
            # Skip spot-only assets for trading (they're just for hedging)
            if market_type == 'spot':
                return
            
            # Apply symbol blacklist filter (from trade performance analysis)
            symbol_blacklist = self.config.get('trading', {}).get('symbol_blacklist', {})
            global_blacklist = symbol_blacklist.get('global', [])
            
            # Check global blacklist
            if symbol in global_blacklist:
                self.logger.debug(f"[Blacklist] Skipped {symbol}: in global blacklist")
                return
            
            # Check strategy-specific blacklists
            # Get active strategy types to check relevant blacklists
            active_strategies = self._get_active_strategy_types()
            for strategy_prefix in ['stat_arb', 'ou_mean_reversion', 'vol_breakout']:
                if any(strategy_prefix in s for s in active_strategies):
                    strategy_blacklist = symbol_blacklist.get(strategy_prefix, [])
                    if symbol in strategy_blacklist:
                        self.logger.debug(f"[Blacklist] Skipped {symbol}: in {strategy_prefix} blacklist")
                        return

            
            # Calculate fresh metrics for this asset
            metrics = self._calculate_asset_metrics(asset)
            if not metrics or metrics.composite_score <= 0:
                self.logger.debug(f"[Scouting] Skipped {symbol}: insufficient metrics or score")
                return
            
            # Thread-safe check and update/add
            with self._pairs_lock:
                is_in_pool = symbol in self.selected_pairs
                
                # Calculate final score (with diversification penalty if applicable)
                final_score = self._calculate_composite_score(metrics, self.selected_pairs)
                
                if is_in_pool:
                    # UPDATE existing pool member's score
                    if symbol in self.selected_pairs_metadata:
                        old_score = self.selected_pairs_metadata[symbol].get('composite_score', 0)
                        self.selected_pairs_metadata[symbol]['composite_score'] = final_score
                        
                        # Log significant changes
                        if abs(final_score - old_score) > 0.05:
                            self.logger.info(f"[Rescore] {symbol}: {old_score:.3f} → {final_score:.3f}")
                else:
                    # NEW asset - apply rotation logic
                    current_count = len(self.selected_pairs)
                    max_pool_size = self._get_max_pairs_to_trade()
                    
                    if current_count < max_pool_size:
                        # Pool has room - add directly
                        self._add_to_pool(symbol, asset, market_type, final_score)
                        self.logger.info(f"[Pool] Added {symbol} (Score: {final_score:.3f}, Total: {current_count + 1})")
                    else:
                        # Pool is full - check if we should swap
                        lowest_symbol, lowest_score = self._get_lowest_scorer()
                        
                        if lowest_symbol and final_score > lowest_score:
                            # Swap: Evict lowest, add new
                            self._remove_from_pool(lowest_symbol)
                            self._add_to_pool(symbol, asset, market_type, final_score)
                            self.logger.info(f"[Pool] Swapped in {symbol} ({final_score:.3f}), evicted {lowest_symbol} ({lowest_score:.3f})")
                        else:
                            # Asset scouted but not qualified for pool
                            self.logger.debug(f"[Scouting] {symbol} ({final_score:.3f}) below cutoff {lowest_score:.3f}")
                
        except Exception as e:
            self.logger.debug(f"[Scouting] Error processing {asset.get('name', '?')}: {e}")
    
    def _add_to_pool(self, symbol: str, asset: Dict[str, Any], market_type: str, score: float):
        """Add a symbol to the trading pool. Must be called within _pairs_lock."""
        self.selected_pairs.append(symbol)
        self.selected_pairs_metadata[symbol] = {
            'market_type': market_type,
            'is_hip3': asset.get('is_hip3', False),
            'dex': asset.get('dex', ''),
            'open_interest': asset.get('openInterest', 0),
            'volume_24h': asset.get('volume_24h', 0),
            'max_leverage': asset.get('maxLeverage', 10),
            'composite_score': score,
        }
        # Subscribe to WebSocket for real-time data
        # Subscribe to WebSocket for real-time data
        if hasattr(self.market_api, 'subscribe_symbol'):
            # Fetch required timeframes
            req_timeframes = ['15m', '1h'] # Safe default
            if hasattr(self, 'strategy_manager') and self.strategy_manager:
                if hasattr(self.strategy_manager, 'get_required_timeframes'):
                    req_timeframes = self.strategy_manager.get_required_timeframes()
            
            # Pass to subscription to limit cache initialization
            # Check if method supports required_timeframes (to match changed signature)
            # Python methods handle new args fine if updated, but safe to just pass if we know it's our API
            self.market_api.subscribe_symbol(symbol, required_timeframes=req_timeframes)
    
    def _remove_from_pool(self, symbol: str):
        """Remove a symbol from the trading pool. Must be called within _pairs_lock."""
        if symbol in self.selected_pairs:
            self.selected_pairs.remove(symbol)
        if symbol in self.selected_pairs_metadata:
            del self.selected_pairs_metadata[symbol]
        
        # Remove from ready set
        self.ready_pairs.discard(symbol)
        
        # Unsubscribe from WebSocket
        if hasattr(self.market_api, 'unsubscribe_symbol'):
            self.market_api.unsubscribe_symbol(symbol)
    
    def _get_lowest_scorer(self) -> tuple:
        """Get the symbol with the lowest score in the pool. Must be called within _pairs_lock."""
        if not self.selected_pairs:
            return None, float('inf')
        
        lowest_symbol = None
        lowest_score = float('inf')
        
        for sym in self.selected_pairs:
            meta = self.selected_pairs_metadata.get(sym, {})
            score = meta.get('composite_score', 0.0)
            if score < lowest_score:
                lowest_score = score
                lowest_symbol = sym
        
        return lowest_symbol, lowest_score
    
    def update_pair_performance(self, symbol: str, pnl: float):
        """
        Update performance tracking for a pair.
        
        Args:
            symbol: Trading symbol
            pnl: Profit/loss for the pair
        """
        if symbol not in self.pair_history:
            self.pair_history[symbol] = {
                'total_pnl': 0,
                'trade_count': 0,
                'last_trade': None,
            }
        
        self.pair_history[symbol]['total_pnl'] += pnl
        self.pair_history[symbol]['trade_count'] += 1
        self.pair_history[symbol]['last_trade'] = datetime.now()
        
        self.logger.info(f"Updated performance for {symbol}: PnL={pnl:.2f}, Total={self.pair_history[symbol]['total_pnl']:.2f}")
    
    def get_pair_performance_summary(self) -> Dict[str, Any]:
        """
        Get performance summary for all pairs.
        
        Returns:
            Performance summary dictionary
        """
        if not self.pair_history:
            return {}
        
        summary = {}
        for symbol, data in self.pair_history.items():
            summary[symbol] = {
                'total_pnl': data['total_pnl'],
                'trade_count': data['trade_count'],
                'avg_pnl': data['total_pnl'] / data['trade_count'] if data['trade_count'] > 0 else 0,
                'last_trade': data['last_trade'].isoformat() if data['last_trade'] else None,
            }
        
        return summary
    
    def force_rescan(self):
        """Force a rescan of available pairs."""
        self.logger.info("Forcing pair rescan")
        self.last_scan_time = None
        self.scan_and_select_pairs()
    
    def get_pair_metadata(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get metadata for a specific pair.
        
        Args:
            symbol: The trading symbol
            
        Returns:
            Dict with market_type, is_hip3, dex, etc. or None if not found
        """
        with self._pairs_lock:
            return self.selected_pairs_metadata.get(symbol)
    
    def get_pairs_by_type(self, market_type: str = None, is_hip3: bool = None) -> List[str]:
        """
        Get selected pairs filtered by market type.
        
        Args:
            market_type: 'perp' or 'spot' (optional filter)
            is_hip3: True for HIP-3 only, False for native only (optional filter)
            
        Returns:
            List of matching pair symbols
        """
        matching = []
        with self._pairs_lock:
            for symbol, meta in self.selected_pairs_metadata.items():
                if market_type is not None and meta.get('market_type') != market_type:
                    continue
                if is_hip3 is not None and meta.get('is_hip3') != is_hip3:
                    continue
                matching.append(symbol)
        return matching
    
    def get_perp_pairs(self, include_hip3: bool = True) -> List[str]:
        """Get all perpetual pairs (native and/or HIP-3)."""
        result = self.get_pairs_by_type(market_type='perp', is_hip3=False)
        if include_hip3:
            result.extend(self.get_pairs_by_type(market_type='perp', is_hip3=True))
        return result
    
    def get_spot_pairs(self) -> List[str]:
        """Get all spot pairs."""
        return self.get_pairs_by_type(market_type='spot')
    
    def get_hip3_pairs(self) -> List[str]:
        """Get all HIP-3 perpetual pairs."""
        return self.get_pairs_by_type(is_hip3=True)
    
    def get_native_perp_pairs(self) -> List[str]:
        """Get only native (non-HIP-3) perpetual pairs."""
        return self.get_pairs_by_type(market_type='perp', is_hip3=False)

    
    def get_available_markets_summary(self) -> Dict[str, Any]:
        """
        Get a summary of all available markets the bot can trade.
        
        Returns:
            Dict with counts and lists by market type
        """
        native_perps = self.get_native_perp_pairs()
        hip3_perps = self.get_hip3_pairs()
        spot_pairs = self.get_spot_pairs()
        
        return {
            'total_selected': len(self.selected_pairs),
            'native_perps': {
                'count': len(native_perps),
                'pairs': native_perps,
            },
            'hip3_perps': {
                'count': len(hip3_perps),
                'pairs': hip3_perps,
                'enabled': self.hip3_enabled and self.hip3_include_in_selection,
            },
            'spot': {
                'count': len(spot_pairs),
                'pairs': spot_pairs,
                'enabled': self.spot_enabled and self.spot_include_in_selection,
            },
            'configuration': {
                'hip3_enabled': self.hip3_enabled,
                'hip3_include_in_selection': self.hip3_include_in_selection,
                'hip3_min_volume': self.hip3_min_volume,
                'spot_enabled': self.spot_enabled,
                'spot_include_in_selection': self.spot_include_in_selection,
                'spot_min_volume': self.spot_min_volume,
                'min_open_interest': self.min_open_interest,
            }
        }