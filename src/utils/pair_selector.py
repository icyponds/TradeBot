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
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timedelta
from collections import defaultdict
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from enum import Enum


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
        
        # Volatility parameters
        volatility_config = selection_config.get('volatility', {})
        self.optimal_volatility_min = volatility_config.get('optimal_min', 0.02)  # 2% daily
        self.optimal_volatility_max = volatility_config.get('optimal_max', 0.08)  # 8% daily
        self.volatility_lookback_days = volatility_config.get('lookback_days', 14)
        
        # Diversification parameters
        diversification_config = selection_config.get('diversification', {})
        self.max_correlation = diversification_config.get('max_correlation', 0.7)
        self.correlation_penalty_factor = diversification_config.get('penalty_factor', 0.5)
        
        # State tracking
        self.selected_pairs = []
        self.selected_pairs_metadata = {}
        self.asset_metrics: Dict[str, AssetMetrics] = {}
        self.price_history: Dict[str, pd.Series] = {}
        self.correlation_matrix: Optional[pd.DataFrame] = None
        self.last_scan_time = None
        self.pair_history = {}
        self.backfill_queue: List[Dict[str, Any]] = []
        self.backfill_symbols_in_queue: set = set()
        
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
            return self.config['trading'].get('max_pairs_to_trade', 20)
    
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
                # Calculate actual volatility from returns
                returns = prices.pct_change().dropna()
                if len(returns) < 3:
                    return 0.5, 0.0
                
                # Daily volatility (standard deviation of returns)
                volatility = returns.std()
            
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
                                       volatility: float) -> Tuple[float, float, float]:
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
            
            # Get active strategies and their types
            active_strategies = self._get_active_strategy_types()
            
            # Weight by active strategy types
            if not active_strategies:
                # No strategy info - use balanced score
                strategy_fit_score = (momentum_score + mean_reversion_score) / 2
            else:
                momentum_weight = 0.0
                mean_rev_weight = 0.0
                arb_weight = 0.0
                
                for strategy_type in active_strategies:
                    if strategy_type in ['momentum_factor', 'supertrend', 'moving_average']:
                        momentum_weight += 1
                    elif strategy_type in ['ou_mean_reversion', 'vwap', 'rsi', 'bollinger_band']:
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
                
                # For arb strategies, prefer liquid assets with reasonable volatility
                arb_score = self._calculate_liquidity_score(asset) * 0.7 + (1 - volatility / 0.1) * 0.3
                
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
                    pass
            
            # No history - neutral score
            return 0.5, 0.0
            
        except Exception as e:
            self.logger.debug(f"Error calculating historical performance for {symbol}: {e}")
            return 0.5, 0.0
    
    def _get_price_history(self, symbol: str) -> Optional[pd.Series]:
        """
        Get price history for a symbol.
        
        Caches results for efficiency.
        """
        if symbol in self.price_history:
            return self.price_history[symbol]
        
        try:
            # Try to get OHLCV data from market API
            if hasattr(self.market_api, 'get_ohlcv'):
                ohlcv = self.market_api.get_ohlcv(
                    symbol, 
                    timeframe='1d',
                    limit=self.volatility_lookback_days + 5
                )
                if ohlcv and len(ohlcv) > 0:
                    df = pd.DataFrame(ohlcv)
                    if 'close' in df.columns:
                        prices = df['close'].astype(float)
                        self.price_history[symbol] = prices
                        return prices
            
            # Fallback: try to get from WebSocket price history
            if hasattr(self.market_api, 'get_price_history'):
                prices = self.market_api.get_price_history(symbol)
                if prices and len(prices) > 0:
                    price_series = pd.Series(prices)
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
            pass
        
        return []
    
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
            
            # Filter and rank assets
            eligible_pairs = self._filter_assets(all_assets)
            selected_pairs, pairs_metadata = self._rank_and_select_pairs(eligible_pairs)
            
            # Update state
            self.selected_pairs = selected_pairs
            self.selected_pairs_metadata = pairs_metadata
            self.last_scan_time = datetime.now()
            
            self.logger.info(f"Selected {len(selected_pairs)} pairs for trading: {selected_pairs}")
            
            # Subscribe to WebSocket feeds for selected pairs
            for symbol in selected_pairs:
                if hasattr(self.market_api, 'subscribe_symbol'):
                    self.market_api.subscribe_symbol(symbol)
            
            return selected_pairs
            
        except Exception as e:
            self.logger.error(f"Error scanning and selecting pairs: {e}")
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
                
                # Get asset context for market data
                ctx = asset_contexts[i] if i < len(asset_contexts) else {}
                
                volume_24h = float(ctx.get('dayNtlVlm', 0))
                
                # Apply spot minimum volume filter
                if volume_24h < self.spot_min_volume:
                    continue
                
                # Use base token name for spot pairs to enable correlation with perps
                # e.g., "BTC" instead of "@1" - the API can resolve "BTC/USDC" to "@1" for orders
                assets.append({
                    'name': base_name,  # Human-readable name (matches perp naming)
                    'display_name': f"{base_name}/{quote_name}",  # Full pair display
                    'api_name': pair.get('name', ''),  # Internal API name (@1, @109, etc.)
                    'base': base_name,
                    'quote': quote_name,
                    'market_type': 'spot',
                    'dex': '',
                    'is_hip3': False,
                    'markPrice': float(ctx.get('midPx', 0)),
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
                self.logger.info(f"Adding {asset_name} ({type_label}) to eligible assets")
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
    
    def _rank_and_select_pairs(self, eligible_assets: List[Dict[str, Any]]) -> tuple:
        """
        Rank eligible assets using sophisticated multi-factor scoring.
        
        Scoring factors (configurable weights):
        - Liquidity (25%): OI + Volume + Spread
        - Volatility (20%): Optimal volatility range
        - Strategy Fit (25%): Match to active strategies
        - Diversification (15%): Correlation penalty
        - Historical Performance (15%): Past trading results
        
        Args:
            eligible_assets: List of eligible assets
            
        Returns:
            Tuple of (list of selected pair symbols, dict of pair metadata)
        """
        if not eligible_assets:
            return [], {}
        
        self.logger.info(f"Ranking {len(eligible_assets)} eligible assets (mode: {self.selection_mode.value})")
        
        # Clear caches for fresh calculation
        self.price_history.clear()
        self.asset_metrics.clear()
        
        # Pre-filter to top N by liquidity to avoid excessive API calls
        # Sort by a quick liquidity proxy (OI + volume) without needing OHLCV
        max_to_analyze = min(30, len(eligible_assets))  # Analyze at most 30 assets up front
        
        def quick_liquidity_score(asset):
            oi = float(asset.get('openInterest', 0))
            vol = float(asset.get('volume24h', 0))
            return oi + vol
        
        sorted_by_liquidity = sorted(eligible_assets, key=quick_liquidity_score, reverse=True)
        assets_to_analyze = sorted_by_liquidity[:max_to_analyze]
        analyze_symbols = {a.get('name', '') for a in assets_to_analyze}
        remaining_assets = [a for a in eligible_assets if a.get('name', '') not in analyze_symbols]
        
        self.logger.info(f"Pre-filtered to top {len(assets_to_analyze)} assets by liquidity")
        
        # Pre-fetch price histories with rate limiting (batching to avoid 429/circuit breaker)
        self.logger.debug("Pre-fetching price histories (with rate limiting)...")
        batch_size = 5
        delay_between_batches = 1.0  # seconds
        for i, asset in enumerate(assets_to_analyze):
            symbol = asset.get('name', '')
            self._get_price_history(symbol)
            # Throttle aggressively to stay under rate limits
            if (i + 1) % batch_size == 0:
                time.sleep(delay_between_batches)

        # Queue remaining assets for staged backfill (processed gradually each scan)
        if remaining_assets:
            queued = 0
            for asset in remaining_assets:
                sym = asset.get('name', '')
                if sym and sym not in self.backfill_symbols_in_queue and sym not in self.price_history:
                    self.backfill_queue.append(asset)
                    self.backfill_symbols_in_queue.add(sym)
                    queued += 1
            if queued > 0:
                self.logger.info(f\"Queued {queued} assets for staged backfill\")
        
        # Build correlation matrix
        self._build_correlation_matrix()
        
        # Use pre-filtered assets for remaining analysis
        eligible_assets = assets_to_analyze
        
        # Calculate metrics for each asset
        all_metrics: List[AssetMetrics] = []
        
        for asset in eligible_assets:
            symbol = asset.get('name', '')
            market_type = asset.get('market_type', 'perp')
            is_hip3 = asset.get('is_hip3', False)
            
            # Create metrics object
            metrics = AssetMetrics(
                symbol=symbol,
                market_type=market_type,
                is_hip3=is_hip3,
                open_interest=float(asset.get('openInterest', 0)),
                volume_24h=float(asset.get('volume24h', 0)),
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
             metrics.mean_reversion_score) = self._calculate_strategy_fit_score(symbol, asset, metrics.volatility)
            metrics.historical_perf_score, metrics.sharpe_ratio = self._calculate_historical_performance_score(symbol)
            
            # Store for later reference
            self.asset_metrics[symbol] = metrics
            all_metrics.append(metrics)
        
        # Sort by initial composite score (without diversification) for greedy selection
        for metrics in all_metrics:
            metrics.composite_score = self._calculate_composite_score(metrics, [])
        
        all_metrics.sort(key=lambda m: m.composite_score, reverse=True)
        
        # Greedy selection with diversification penalty
        max_pairs = self._get_max_pairs_to_trade()
        selected_pairs = []
        pairs_metadata = {}
        
        self.logger.info("=" * 60)
        self.logger.info("PAIR SELECTION RANKINGS")
        self.logger.info("=" * 60)
        
        for metrics in all_metrics:
            if len(selected_pairs) >= max_pairs:
                break
            
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
                f"Score: {metrics.composite_score:.3f}"
            )
            self.logger.info(
                f"        Liq: {metrics.liquidity_score:.2f} | "
                f"Vol: {metrics.volatility_score:.2f} ({metrics.volatility*100:.1f}%) | "
                f"Strat: {metrics.strategy_fit_score:.2f} | "
                f"Div: {metrics.diversification_score:.2f} | "
                f"Hist: {metrics.historical_perf_score:.2f}"
            )
            self.logger.info(
                f"        OI: ${metrics.open_interest:,.0f} | "
                f"Vol24h: ${metrics.volume_24h:,.0f} | "
                f"Momentum: {metrics.momentum_score:.2f} | "
                f"MeanRev: {metrics.mean_reversion_score:.2f}"
            )
        
        self.logger.info("=" * 60)
        self.logger.info(f"Selected {len(selected_pairs)} pairs for trading")
        
        # Process staged backfill queue in small batches to avoid rate limits
        self._process_backfill_queue()
        
        return selected_pairs, pairs_metadata
    
    def should_rescan(self) -> bool:
        """
        Check if it's time to rescan for new pairs.
        
        Returns:
            True if should rescan, False otherwise
        """
        if not self.last_scan_time:
            return True
        
        time_since_scan = datetime.now() - self.last_scan_time
        scan_interval = timedelta(minutes=self.scan_interval_minutes)
        
        return time_since_scan >= scan_interval
    
    def get_current_pairs(self) -> List[str]:
        """
        Get currently selected trading pairs.
        
        Returns:
            List of current trading pairs
        """
        if self.should_rescan():
            return self.scan_and_select_pairs()
        return self.selected_pairs
    
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

    def _process_backfill_queue(self, batch_size: int = 5, delay_between_batches: float = 1.0):
        """
        Gradually backfill historical data for queued assets to avoid rate limits.
        
        Args:
            batch_size: How many assets to process per invocation
            delay_between_batches: Delay in seconds after each batch
        """
        if not self.backfill_queue:
            return
        
        to_process = min(batch_size, len(self.backfill_queue))
        processed = 0
        for i in range(to_process):
            asset = self.backfill_queue.pop(0)
            sym = asset.get('name', '')
            if not sym:
                continue
            self._get_price_history(sym)
            processed += 1
            if processed % batch_size == 0:
                time.sleep(delay_between_batches)
        
        # Rebuild queue symbol set
        self.backfill_symbols_in_queue = {a.get('name', '') for a in self.backfill_queue if a.get('name', '')}
        
        self.logger.info(f"Backfill processed {processed} assets; {len(self.backfill_queue)} remaining in queue")
    
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