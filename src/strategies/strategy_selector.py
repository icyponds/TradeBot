"""
Dynamic strategy selection based on performance metrics.

This module provides functionality to:
- Rank strategies by performance (win rate, profit factor, expectancy, etc.)
- Dynamically enable/disable strategies based on performance
- Weight strategy signals based on confidence
- Rotate strategies based on market conditions
- Regime-aware strategy selection
- Kelly Criterion for optimal allocation
- Strategy correlation/diversification analysis
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
from enum import Enum
import numpy as np
from collections import deque


class SelectionMode(Enum):
    """Strategy selection modes."""
    ALL = "all"                      # Use all strategies
    TOP_N = "top_n"                  # Use top N performing strategies
    THRESHOLD = "threshold"          # Use strategies above performance threshold
    WEIGHTED = "weighted"            # Weight signals by strategy performance
    ROTATING = "rotating"            # Rotate between strategies periodically
    ADAPTIVE = "adaptive"            # Adapt based on market regime
    KELLY = "kelly"                  # Kelly Criterion optimal allocation
    DIVERSIFIED = "diversified"      # Maximize diversification across strategies
    AUTO = "auto"                    # Automatically combine all methods intelligently


class RankingMetric(Enum):
    """Metrics used to rank strategies."""
    WIN_RATE = "win_rate"
    PROFIT_FACTOR = "profit_factor"
    EXPECTANCY = "expectancy"
    SHARPE_RATIO = "sharpe_ratio"
    TOTAL_PNL = "total_pnl"
    RISK_REWARD = "risk_reward_ratio"
    CALMAR_RATIO = "calmar_ratio"
    COMPOSITE = "composite"          # Combined score of multiple metrics
    ROLLING_SHARPE = "rolling_sharpe"  # Recent risk-adjusted performance
    INFORMATION_RATIO = "information_ratio"  # Alpha relative to benchmark


class MarketRegime(Enum):
    """Market regime classification."""
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    MEAN_REVERTING = "mean_reverting"
    HIGH_VOLATILITY = "high_volatility"
    LOW_VOLATILITY = "low_volatility"
    UNKNOWN = "unknown"


@dataclass
class StrategyRanking:
    """Ranking data for a strategy."""
    strategy_name: str
    rank: int
    score: float
    is_enabled: bool
    weight: float
    metrics: Dict[str, float] = field(default_factory=dict)
    last_updated: datetime = field(default_factory=datetime.now)
    kelly_fraction: float = 0.0  # Optimal Kelly bet size
    regime_affinity: Dict[str, float] = field(default_factory=dict)  # Performance by regime
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'strategy_name': self.strategy_name,
            'rank': self.rank,
            'score': round(self.score, 4),
            'is_enabled': self.is_enabled,
            'weight': round(self.weight, 4),
            'kelly_fraction': round(self.kelly_fraction, 4),
            'metrics': {k: round(v, 4) if isinstance(v, float) else v for k, v in self.metrics.items()},
            'regime_affinity': {k: round(v, 4) for k, v in self.regime_affinity.items()},
            'last_updated': self.last_updated.isoformat(),
        }


@dataclass
class StrategyPerformanceWindow:
    """Rolling window of strategy performance for recency-weighted analysis."""
    returns: deque = field(default_factory=lambda: deque(maxlen=50))
    timestamps: deque = field(default_factory=lambda: deque(maxlen=50))
    regimes: deque = field(default_factory=lambda: deque(maxlen=50))
    
    def add_return(self, ret: float, timestamp: datetime, regime: MarketRegime):
        self.returns.append(ret)
        self.timestamps.append(timestamp)
        self.regimes.append(regime)
    
    def get_rolling_sharpe(self, lookback: int = 20) -> float:
        """Calculate rolling Sharpe ratio."""
        if len(self.returns) < lookback:
            return 0.0
        recent = list(self.returns)[-lookback:]
        if len(recent) < 2:
            return 0.0
        mean_ret = np.mean(recent)
        std_ret = np.std(recent)
        if std_ret == 0:
            return 0.0
        # Annualize assuming daily returns
        return (mean_ret / std_ret) * np.sqrt(252)
    
    def get_regime_performance(self, regime: MarketRegime) -> Tuple[float, int]:
        """Get average return and count for a specific regime."""
        regime_returns = [r for r, reg in zip(self.returns, self.regimes) if reg == regime]
        if not regime_returns:
            return 0.0, 0
        return np.mean(regime_returns), len(regime_returns)


class StrategySelector:
    """
    Automatically selects strategies based on performance.
    
    Strategies are ranked by a composite score of win rate, profit factor,
    expectancy, and risk-reward ratio. Poorly performing strategies are
    automatically disabled.
    
    Features:
    - Automatic ranking by composite performance score
    - Strategies with insufficient data stay enabled (learning period)
    - Underperforming strategies (negative expectancy, <1.0 profit factor) are disabled
    - Cooling-off period for strategies with losing streaks
    """
    
    def __init__(self, performance_tracker, config: Dict[str, Any]):
        """
        Initialize the strategy selector.
        
        Args:
            performance_tracker: PerformanceTracker instance for getting metrics
            config: Configuration dictionary
        """
        self.performance_tracker = performance_tracker
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # Automatic selection - AUTO mode combines all methods intelligently
        self.selection_mode = SelectionMode.AUTO
        self.ranking_metric = RankingMetric.COMPOSITE
        
        # Automatic thresholds - strategies must be profitable to stay enabled
        self.min_win_rate = 35.0           # At least 35% wins
        self.min_profit_factor = 0.8       # Profit factor >= 0.8 (allow some margin)
        self.min_expectancy = -10.0        # Allow slightly negative during learning
        
        # Minimum trades before a strategy can be disabled (learning period)
        self.min_trades_for_ranking = 5
        
        # Cooling-off: disable strategies after consecutive losses
        self.enable_cooling_off = True
        self.cooling_off_hours = 12        # 12 hour timeout
        self.cooling_off_loss_streak = 4   # 4 consecutive losses triggers cooldown
        
        # Weight settings for signal strength adjustment
        self.min_weight = 0.3
        self.max_weight = 1.0
        
        # Re-rank every 30 minutes
        self.rerank_interval_minutes = 30
        self.last_rerank_time: Optional[datetime] = None
        
        # Strategy state
        self.strategy_rankings: Dict[str, StrategyRanking] = {}
        self.cooling_off_until: Dict[str, datetime] = {}
        
        # Composite score weights - balanced across key metrics
        self.composite_weights = {
            'win_rate': 0.15,
            'profit_factor': 0.25,
            'expectancy': 0.20,
            'sharpe_ratio': 0.15,
            'risk_reward_ratio': 0.10,
            'rolling_sharpe': 0.15,  # Recent performance matters
        }
        
        # Rolling performance windows for each strategy
        self.performance_windows: Dict[str, StrategyPerformanceWindow] = {}
        
        # Current market regime
        self.current_regime: MarketRegime = MarketRegime.UNKNOWN
        self.regime_history: deque = deque(maxlen=100)
        
        # Strategy correlation matrix (for diversification)
        self.strategy_correlations: Dict[Tuple[str, str], float] = {}
        
        # Kelly Criterion settings
        self.kelly_fraction_cap = 0.25  # Never bet more than 25% Kelly
        self.use_half_kelly = True      # Use half-Kelly for safety
        
        # Regime-strategy affinity (which strategies work in which regimes)
        # These can be learned over time or set based on strategy type
        self.regime_affinity_priors = {
            'stat_arb': {
                MarketRegime.MEAN_REVERTING: 1.5,
                MarketRegime.HIGH_VOLATILITY: 0.8,
                MarketRegime.TRENDING_UP: 0.7,
                MarketRegime.TRENDING_DOWN: 0.7,
            },
            'funding_rate_arbitrage': {
                MarketRegime.HIGH_VOLATILITY: 1.2,  # Higher funding in volatile markets
                MarketRegime.MEAN_REVERTING: 1.0,
                MarketRegime.TRENDING_UP: 1.0,
                MarketRegime.TRENDING_DOWN: 1.0,
            },
            'ou_mean_reversion': {
                MarketRegime.MEAN_REVERTING: 1.5,
                MarketRegime.LOW_VOLATILITY: 1.2,
                MarketRegime.TRENDING_UP: 0.5,
                MarketRegime.TRENDING_DOWN: 0.5,
            },
            'momentum_factor': {
                MarketRegime.TRENDING_UP: 1.5,
                MarketRegime.TRENDING_DOWN: 1.3,
                MarketRegime.MEAN_REVERTING: 0.5,
                MarketRegime.HIGH_VOLATILITY: 0.8,
            },
        }
        
        # Decay factor for recency weighting (higher = more weight on recent)
        self.recency_decay = 0.95
        
        self.logger.info("StrategySelector initialized - AUTO mode (combines all methods intelligently)")
    
    def update_rankings(self, force: bool = False) -> Dict[str, StrategyRanking]:
        """
        Update strategy rankings based on current performance.
        
        Args:
            force: Force update even if interval hasn't passed
            
        Returns:
            Dictionary of strategy rankings
        """
        # Check if we should re-rank
        now = datetime.now()
        if not force and self.last_rerank_time:
            time_since_rerank = (now - self.last_rerank_time).total_seconds() / 60
            if time_since_rerank < self.rerank_interval_minutes:
                return self.strategy_rankings
        
        self.logger.info("Updating strategy rankings...")
        
        # Get all strategy metrics
        all_metrics = self.performance_tracker.get_all_strategy_metrics()
        
        if not all_metrics:
            self.logger.warning("No strategy metrics available for ranking")
            return self.strategy_rankings
        
        # Calculate scores for each strategy
        strategy_scores: List[Tuple[str, float, Dict[str, float]]] = []
        
        for strategy_name, metrics in all_metrics.items():
            metrics_dict = metrics.to_dict()
            
            # Check minimum trades requirement
            total_trades = metrics_dict.get('total_trades', 0)
            if total_trades < self.min_trades_for_ranking:
                self.logger.debug(f"Strategy {strategy_name} has insufficient trades ({total_trades} < {self.min_trades_for_ranking})")
                # Give a neutral score for strategies with insufficient data
                score = 0.5
            else:
                score = self._calculate_score(metrics_dict, strategy_name)
            
            strategy_scores.append((strategy_name, score, metrics_dict))
        
        # Sort by score (descending)
        strategy_scores.sort(key=lambda x: x[1], reverse=True)
        
        # Create rankings
        self.strategy_rankings = {}
        for rank, (strategy_name, score, metrics_dict) in enumerate(strategy_scores, 1):
            # Determine if strategy is enabled based on selection mode
            is_enabled = self._should_enable_strategy(strategy_name, rank, score, metrics_dict)
            
            # Calculate weight based on score, regime, and other factors
            weight = self._calculate_weight(score, rank, len(strategy_scores), strategy_name, metrics_dict)
            
            # Check cooling-off period
            if strategy_name in self.cooling_off_until:
                if now < self.cooling_off_until[strategy_name]:
                    is_enabled = False
                    self.logger.info(f"Strategy {strategy_name} is in cooling-off until {self.cooling_off_until[strategy_name]}")
                else:
                    del self.cooling_off_until[strategy_name]
            
            # Calculate Kelly fraction
            kelly = self._calculate_kelly_fraction(metrics_dict) if metrics_dict.get('total_trades', 0) >= self.min_trades_for_ranking else 0.0
            
            # Calculate regime affinity from historical performance
            regime_affinity = self._calculate_regime_affinity(strategy_name)
            
            self.strategy_rankings[strategy_name] = StrategyRanking(
                strategy_name=strategy_name,
                rank=rank,
                score=score,
                is_enabled=is_enabled,
                weight=weight,
                metrics=metrics_dict,
                last_updated=now,
                kelly_fraction=kelly,
                regime_affinity=regime_affinity,
            )
        
        self.last_rerank_time = now
        self._log_rankings()
        
        return self.strategy_rankings
    
    def _calculate_score(self, metrics: Dict[str, float], strategy_name: str = None) -> float:
        """Calculate strategy score based on ranking metric."""
        
        if self.ranking_metric == RankingMetric.WIN_RATE:
            return metrics.get('win_rate', 0) / 100
        
        elif self.ranking_metric == RankingMetric.PROFIT_FACTOR:
            pf = metrics.get('profit_factor', 0)
            # Normalize: PF of 2.0 = score of 1.0
            return min(pf / 2.0, 2.0)
        
        elif self.ranking_metric == RankingMetric.EXPECTANCY:
            exp = metrics.get('expectancy', 0)
            # Normalize based on typical expectancy range
            return (exp + 100) / 200  # Assumes expectancy typically -100 to +100
        
        elif self.ranking_metric == RankingMetric.SHARPE_RATIO:
            sharpe = metrics.get('sharpe_ratio', 0)
            # Normalize: Sharpe of 2.0 = score of 1.0
            return min(max(sharpe / 2.0, -1), 2.0)
        
        elif self.ranking_metric == RankingMetric.TOTAL_PNL:
            pnl = metrics.get('total_pnl', 0)
            # Normalized by sign, magnitude doesn't matter as much for ranking
            return 0.5 + (0.5 if pnl > 0 else -0.5 if pnl < 0 else 0)
        
        elif self.ranking_metric == RankingMetric.RISK_REWARD:
            rr = metrics.get('risk_reward_ratio', 0)
            # Normalize: RR of 2.0 = score of 1.0
            return min(rr / 2.0, 2.0)
        
        elif self.ranking_metric == RankingMetric.CALMAR_RATIO:
            calmar = metrics.get('calmar_ratio', 0)
            return min(max(calmar / 2.0, -1), 2.0)
        
        elif self.ranking_metric == RankingMetric.COMPOSITE:
            return self._calculate_composite_score(metrics, strategy_name)
        
        elif self.ranking_metric == RankingMetric.ROLLING_SHARPE:
            if strategy_name:
                return max(0, min(1, (self._get_rolling_sharpe(strategy_name) + 1) / 3))
            return 0.5
        
        elif self.ranking_metric == RankingMetric.INFORMATION_RATIO:
            # Information ratio = (strategy return - benchmark) / tracking error
            # For now, use Sharpe as proxy
            sharpe = metrics.get('sharpe_ratio', 0)
            return min(max(sharpe / 2.0, -1), 2.0)
        
        return 0.5
    
    def _calculate_composite_score(self, metrics: Dict[str, float], strategy_name: str = None) -> float:
        """Calculate composite score from multiple metrics with recency weighting."""
        score = 0.0
        total_weight = 0.0
        
        # Win rate component (0-100 -> 0-1)
        if 'win_rate' in self.composite_weights:
            wr = metrics.get('win_rate', 50) / 100
            score += wr * self.composite_weights['win_rate']
            total_weight += self.composite_weights['win_rate']
        
        # Profit factor component (normalize around 1.0)
        if 'profit_factor' in self.composite_weights:
            pf = metrics.get('profit_factor', 1.0)
            # PF > 1 is good, PF of 2 = score of 1.0
            pf_score = min(pf / 2.0, 1.0) if pf >= 0 else 0
            score += pf_score * self.composite_weights['profit_factor']
            total_weight += self.composite_weights['profit_factor']
        
        # Expectancy component
        if 'expectancy' in self.composite_weights:
            exp = metrics.get('expectancy', 0)
            # Normalize to 0-1 range
            exp_score = max(0, min(1, (exp + 50) / 100))
            score += exp_score * self.composite_weights['expectancy']
            total_weight += self.composite_weights['expectancy']
        
        # Sharpe ratio component
        if 'sharpe_ratio' in self.composite_weights:
            sharpe = metrics.get('sharpe_ratio', 0)
            sharpe_score = max(0, min(1, (sharpe + 1) / 3))
            score += sharpe_score * self.composite_weights['sharpe_ratio']
            total_weight += self.composite_weights['sharpe_ratio']
        
        # Risk-reward component
        if 'risk_reward_ratio' in self.composite_weights:
            rr = metrics.get('risk_reward_ratio', 1.0)
            rr_score = min(rr / 2.0, 1.0) if rr >= 0 else 0
            score += rr_score * self.composite_weights['risk_reward_ratio']
            total_weight += self.composite_weights['risk_reward_ratio']
        
        # Rolling Sharpe (recent performance) - gives more weight to recent results
        if 'rolling_sharpe' in self.composite_weights and strategy_name:
            rolling_sharpe = self._get_rolling_sharpe(strategy_name)
            rolling_score = max(0, min(1, (rolling_sharpe + 1) / 3))
            score += rolling_score * self.composite_weights['rolling_sharpe']
            total_weight += self.composite_weights['rolling_sharpe']
        
        # Normalize by total weight
        if total_weight > 0:
            score = score / total_weight
        
        return score
    
    def _calculate_regime_affinity(self, strategy_name: str) -> Dict[str, float]:
        """
        Calculate strategy's performance affinity for each regime from historical data.
        
        Returns dict mapping regime name to performance multiplier.
        """
        affinity = {}
        
        if strategy_name not in self.performance_windows:
            # Use priors if no data
            if strategy_name in self.regime_affinity_priors:
                return {r.value: m for r, m in self.regime_affinity_priors[strategy_name].items()}
            return {}
        
        window = self.performance_windows[strategy_name]
        
        # Calculate performance in each regime
        overall_mean = np.mean(list(window.returns)) if window.returns else 0
        
        for regime in MarketRegime:
            if regime == MarketRegime.UNKNOWN:
                continue
            
            regime_mean, count = window.get_regime_performance(regime)
            
            if count >= 5:  # Minimum samples
                # Affinity = regime performance / overall performance
                if overall_mean != 0:
                    affinity[regime.value] = max(0.3, min(2.0, regime_mean / overall_mean))
                else:
                    affinity[regime.value] = 1.0
            elif strategy_name in self.regime_affinity_priors:
                # Use prior
                affinity[regime.value] = self.regime_affinity_priors[strategy_name].get(regime, 1.0)
        
        return affinity
    
    def _should_enable_strategy(
        self,
        strategy_name: str,
        rank: int,
        score: float,
        metrics: Dict[str, float],
    ) -> bool:
        """Determine if a strategy should be enabled based on selection mode."""
        
        if self.selection_mode == SelectionMode.ALL:
            return True
        
        elif self.selection_mode == SelectionMode.TOP_N:
            return rank <= self.top_n
        
        elif self.selection_mode == SelectionMode.THRESHOLD:
            # Check all threshold conditions
            win_rate = metrics.get('win_rate', 0)
            profit_factor = metrics.get('profit_factor', 0)
            expectancy = metrics.get('expectancy', float('-inf'))
            
            return (
                win_rate >= self.min_win_rate and
                profit_factor >= self.min_profit_factor and
                expectancy >= self.min_expectancy
            )
        
        elif self.selection_mode in [SelectionMode.WEIGHTED, SelectionMode.KELLY, 
                                      SelectionMode.DIVERSIFIED]:
            # In these modes, all strategies are enabled but with different weights
            return True
        
        elif self.selection_mode == SelectionMode.ROTATING:
            # TODO: Implement rotation logic
            return rank <= self.top_n
        
        elif self.selection_mode == SelectionMode.ADAPTIVE:
            # Enable if score is reasonable and regime affinity is positive
            regime_mult = self._get_regime_multiplier(strategy_name) if strategy_name else 1.0
            return score >= 0.3 and regime_mult >= 0.5
        
        elif self.selection_mode == SelectionMode.AUTO:
            # AUTO mode: Enable all strategies but with intelligent weighting
            # Only disable if demonstrably losing (negative Kelly edge)
            if metrics.get('total_trades', 0) < self.min_trades_for_ranking:
                return True  # Keep enabled during learning period
            
            kelly = self._calculate_kelly_fraction(metrics)
            
            # Disable only if:
            # 1. Kelly is negative (no edge)
            # 2. AND score is very low
            # 3. AND not performing well in current regime
            if kelly <= 0 and score < 0.3:
                regime_mult = self._get_regime_multiplier(strategy_name) if strategy_name else 1.0
                if regime_mult < 0.7:
                    return False
            
            return True
        
        return True
    
    def _calculate_weight(self, score: float, rank: int, total: int, 
                         strategy_name: str = None, metrics: Dict = None) -> float:
        """Calculate strategy weight based on score, rank, and regime."""
        
        base_weight = self.min_weight + (score * (self.max_weight - self.min_weight))
        
        if self.selection_mode == SelectionMode.AUTO:
            # AUTO mode: Intelligently combine all methods
            return self._calculate_auto_weight(score, rank, total, strategy_name, metrics)
        
        elif self.selection_mode == SelectionMode.WEIGHTED:
            # Apply regime adjustment if available
            if strategy_name and self.current_regime != MarketRegime.UNKNOWN:
                regime_mult = self._get_regime_multiplier(strategy_name)
                base_weight *= regime_mult
            
            return max(self.min_weight, min(self.max_weight, base_weight))
        
        elif self.selection_mode == SelectionMode.KELLY:
            # Use Kelly fraction as weight
            if metrics:
                kelly = self._calculate_kelly_fraction(metrics)
                return max(self.min_weight, min(self.kelly_fraction_cap, kelly))
            return base_weight
        
        elif self.selection_mode == SelectionMode.ADAPTIVE:
            # Combine score with regime affinity
            if strategy_name:
                regime_mult = self._get_regime_multiplier(strategy_name)
                # Also consider rolling performance
                rolling_sharpe = self._get_rolling_sharpe(strategy_name)
                sharpe_mult = 1.0 + (rolling_sharpe * 0.1)  # +10% per Sharpe point
                base_weight *= regime_mult * max(0.5, min(1.5, sharpe_mult))
            return max(self.min_weight, min(self.max_weight, base_weight))
        
        elif self.selection_mode == SelectionMode.DIVERSIFIED:
            # Penalize strategies correlated with higher-ranked ones
            correlation_penalty = self._calculate_correlation_penalty(strategy_name, rank)
            return max(self.min_weight, min(self.max_weight, base_weight * correlation_penalty))
        
        elif self.selection_mode in [SelectionMode.TOP_N, SelectionMode.THRESHOLD]:
            return 1.0
        
        return 1.0
    
    def _calculate_auto_weight(self, score: float, rank: int, total: int,
                               strategy_name: str = None, metrics: Dict = None) -> float:
        """
        AUTO mode: Combine multiple selection methods intelligently.
        
        The final weight is a blend of:
        1. Composite score (base quality)
        2. Kelly fraction (optimal sizing based on edge)
        3. Regime affinity (market condition fit)
        4. Rolling performance (recent results)
        5. Diversification (correlation penalty)
        
        Weights are combined using a weighted geometric mean to ensure
        that poor performance in any dimension significantly reduces weight.
        """
        components = []
        component_weights = []
        
        # 1. Base score component (25%)
        base_weight = self.min_weight + (score * (self.max_weight - self.min_weight))
        components.append(max(0.1, base_weight))
        component_weights.append(0.25)
        
        # 2. Kelly fraction component (20%)
        if metrics:
            kelly = self._calculate_kelly_fraction(metrics)
            # Normalize Kelly to 0-1 scale
            kelly_normalized = kelly / self.kelly_fraction_cap if self.kelly_fraction_cap > 0 else 0
            components.append(max(0.1, kelly_normalized))
            component_weights.append(0.20)
        
        # 3. Regime affinity component (20%)
        if strategy_name and self.current_regime != MarketRegime.UNKNOWN:
            regime_mult = self._get_regime_multiplier(strategy_name)
            # Normalize regime multiplier (typically 0.5-1.5) to 0-1
            regime_normalized = (regime_mult - 0.3) / 1.4  # Maps 0.3-1.7 to 0-1
            components.append(max(0.1, min(1.0, regime_normalized)))
            component_weights.append(0.20)
        
        # 4. Rolling performance component (20%)
        if strategy_name:
            rolling_sharpe = self._get_rolling_sharpe(strategy_name)
            # Normalize Sharpe: -1 to 3 maps to 0 to 1
            sharpe_normalized = (rolling_sharpe + 1) / 4
            components.append(max(0.1, min(1.0, sharpe_normalized)))
            component_weights.append(0.20)
        
        # 5. Diversification component (15%)
        if strategy_name:
            correlation_penalty = self._calculate_correlation_penalty(strategy_name, rank)
            components.append(max(0.1, correlation_penalty))
            component_weights.append(0.15)
        
        # Calculate weighted geometric mean
        # This ensures poor performance in any dimension pulls down the overall weight
        if not components:
            return base_weight
        
        # Normalize component weights
        total_weight = sum(component_weights)
        component_weights = [w / total_weight for w in component_weights]
        
        # Geometric mean: (x1^w1 * x2^w2 * ... * xn^wn)
        log_sum = sum(w * np.log(c) for c, w in zip(components, component_weights))
        combined_weight = np.exp(log_sum)
        
        # Scale to final weight range
        final_weight = self.min_weight + combined_weight * (self.max_weight - self.min_weight)
        
        return max(self.min_weight, min(self.max_weight, final_weight))
    
    def _calculate_kelly_fraction(self, metrics: Dict[str, float]) -> float:
        """
        Calculate Kelly Criterion optimal bet fraction.
        
        Kelly formula: f* = (p * b - q) / b
        Where:
            p = win probability
            b = win/loss ratio (odds)
            q = 1 - p (loss probability)
        
        For trading: f* = (win_rate * avg_win - loss_rate * avg_loss) / avg_win
        Simplified: f* = win_rate - (loss_rate / risk_reward)
        """
        win_rate = metrics.get('win_rate', 50) / 100
        loss_rate = 1 - win_rate
        risk_reward = metrics.get('risk_reward_ratio', 1.0)
        
        if risk_reward <= 0:
            return 0.0
        
        # Kelly fraction
        kelly = win_rate - (loss_rate / risk_reward)
        
        # Apply half-Kelly for safety
        if self.use_half_kelly:
            kelly *= 0.5
        
        # Cap the Kelly fraction
        kelly = max(0, min(self.kelly_fraction_cap, kelly))
        
        return kelly
    
    def _get_regime_multiplier(self, strategy_name: str) -> float:
        """Get weight multiplier based on current regime and strategy affinity."""
        if self.current_regime == MarketRegime.UNKNOWN:
            return 1.0
        
        # Check learned affinity first
        if strategy_name in self.strategy_rankings:
            ranking = self.strategy_rankings[strategy_name]
            if self.current_regime.value in ranking.regime_affinity:
                return ranking.regime_affinity[self.current_regime.value]
        
        # Fall back to priors
        if strategy_name in self.regime_affinity_priors:
            return self.regime_affinity_priors[strategy_name].get(self.current_regime, 1.0)
        
        return 1.0
    
    def _get_rolling_sharpe(self, strategy_name: str) -> float:
        """Get rolling Sharpe ratio for a strategy."""
        if strategy_name in self.performance_windows:
            return self.performance_windows[strategy_name].get_rolling_sharpe()
        return 0.0
    
    def _calculate_correlation_penalty(self, strategy_name: str, rank: int) -> float:
        """
        Calculate penalty for strategies correlated with higher-ranked strategies.
        
        This promotes diversification by reducing weight on redundant strategies.
        """
        if rank == 1:
            return 1.0  # No penalty for top strategy
        
        penalty = 1.0
        
        # Check correlation with all higher-ranked strategies
        for other_name, other_ranking in self.strategy_rankings.items():
            if other_ranking.rank < rank:  # Higher ranked (lower number)
                corr_key = tuple(sorted([strategy_name, other_name]))
                correlation = self.strategy_correlations.get(corr_key, 0.0)
                
                # Penalize based on correlation (high correlation = high penalty)
                # Correlation of 0.8 -> penalty of 0.6
                if correlation > 0.5:
                    penalty *= (1 - correlation * 0.5)
        
        return max(0.3, penalty)  # Minimum 30% of original weight
    
    def detect_market_regime(self, prices: np.ndarray, lookback: int = 50) -> MarketRegime:
        """
        Detect current market regime based on price action.
        
        Uses:
        - Trend detection (SMA slope)
        - Volatility regime (ATR or std dev)
        - Mean reversion tendency (Hurst exponent approximation)
        """
        if len(prices) < lookback:
            return MarketRegime.UNKNOWN
        
        recent_prices = prices[-lookback:]
        
        # Calculate returns
        returns = np.diff(recent_prices) / recent_prices[:-1]
        
        # Trend detection using linear regression slope
        x = np.arange(len(recent_prices))
        slope = np.polyfit(x, recent_prices, 1)[0]
        normalized_slope = slope / np.mean(recent_prices)
        
        # Volatility (annualized)
        volatility = np.std(returns) * np.sqrt(252)
        
        # Simple Hurst exponent approximation (R/S method simplified)
        # H < 0.5 = mean reverting, H > 0.5 = trending
        hurst = self._estimate_hurst(returns)
        
        # Classify regime
        high_vol_threshold = 0.6  # 60% annualized vol
        trend_threshold = 0.001  # 0.1% per period
        
        if volatility > high_vol_threshold:
            regime = MarketRegime.HIGH_VOLATILITY
        elif volatility < 0.2:
            regime = MarketRegime.LOW_VOLATILITY
        elif hurst < 0.45:
            regime = MarketRegime.MEAN_REVERTING
        elif normalized_slope > trend_threshold:
            regime = MarketRegime.TRENDING_UP
        elif normalized_slope < -trend_threshold:
            regime = MarketRegime.TRENDING_DOWN
        else:
            regime = MarketRegime.MEAN_REVERTING
        
        self.current_regime = regime
        self.regime_history.append((datetime.now(), regime))
        
        return regime
    
    def _estimate_hurst(self, returns: np.ndarray) -> float:
        """
        Estimate Hurst exponent using simplified R/S analysis.
        
        H < 0.5: Mean reverting
        H = 0.5: Random walk
        H > 0.5: Trending
        """
        if len(returns) < 20:
            return 0.5
        
        try:
            # Simplified variance ratio test
            # Compare variance of returns at different lags
            var_1 = np.var(returns)
            
            # 2-period returns
            returns_2 = returns[::2]
            if len(returns_2) < 2:
                return 0.5
            var_2 = np.var(returns_2)
            
            # For random walk: var_2 ≈ 2 * var_1
            # Hurst ≈ 0.5 * log2(var_2 / var_1)
            if var_1 <= 0:
                return 0.5
            
            ratio = var_2 / var_1
            if ratio <= 0:
                return 0.5
            
            hurst = 0.5 * np.log2(ratio)
            return max(0.0, min(1.0, hurst))
            
        except Exception:
            return 0.5
    
    def record_trade_result(self, strategy_name: str, return_pct: float, timestamp: datetime = None):
        """
        Record a trade result for rolling performance tracking.
        
        Args:
            strategy_name: Name of the strategy
            return_pct: Return percentage of the trade
            timestamp: Trade timestamp (defaults to now)
        """
        if timestamp is None:
            timestamp = datetime.now()
        
        if strategy_name not in self.performance_windows:
            self.performance_windows[strategy_name] = StrategyPerformanceWindow()
        
        self.performance_windows[strategy_name].add_return(
            return_pct, 
            timestamp, 
            self.current_regime
        )
    
    def update_strategy_correlation(self, strategy_a: str, strategy_b: str, correlation: float):
        """Update correlation between two strategies."""
        key = tuple(sorted([strategy_a, strategy_b]))
        self.strategy_correlations[key] = correlation
    
    def calculate_strategy_correlations(self):
        """Calculate correlations between all strategy returns."""
        strategies = list(self.performance_windows.keys())
        
        for i, strat_a in enumerate(strategies):
            for strat_b in strategies[i+1:]:
                returns_a = list(self.performance_windows[strat_a].returns)
                returns_b = list(self.performance_windows[strat_b].returns)
                
                # Align by length
                min_len = min(len(returns_a), len(returns_b))
                if min_len < 10:
                    continue
                
                returns_a = returns_a[-min_len:]
                returns_b = returns_b[-min_len:]
                
                correlation = np.corrcoef(returns_a, returns_b)[0, 1]
                self.update_strategy_correlation(strat_a, strat_b, correlation)
    
    def get_enabled_strategies(self) -> List[str]:
        """Get list of currently enabled strategy names."""
        self.update_rankings()
        return [
            name for name, ranking in self.strategy_rankings.items()
            if ranking.is_enabled
        ]
    
    def get_strategy_weight(self, strategy_name: str) -> float:
        """
        Get the weight for a strategy's signals.
        
        Args:
            strategy_name: Name of the strategy
            
        Returns:
            Weight (0.0 to 1.0) for the strategy's signals
        """
        if strategy_name not in self.strategy_rankings:
            self.update_rankings()
        
        ranking = self.strategy_rankings.get(strategy_name)
        if ranking and ranking.is_enabled:
            return ranking.weight
        return 0.0
    
    def is_strategy_enabled(self, strategy_name: str) -> bool:
        """Check if a strategy is currently enabled."""
        if strategy_name not in self.strategy_rankings:
            self.update_rankings()
        
        ranking = self.strategy_rankings.get(strategy_name)
        return ranking.is_enabled if ranking else False
    
    def should_execute_signal(
        self,
        strategy_name: str,
        signal_strength: float,
    ) -> Tuple[bool, float]:
        """
        Determine if a strategy signal should be executed.
        
        Args:
            strategy_name: Name of the strategy
            signal_strength: Original signal strength
            
        Returns:
            Tuple of (should_execute, adjusted_signal_strength)
        """
        if strategy_name not in self.strategy_rankings:
            self.update_rankings()
        
        ranking = self.strategy_rankings.get(strategy_name)
        
        if not ranking or not ranking.is_enabled:
            return False, 0.0
        
        # Adjust signal strength by strategy weight
        adjusted_strength = signal_strength * ranking.weight
        
        return True, adjusted_strength
    
    def put_strategy_in_cooling_off(self, strategy_name: str, reason: str = ""):
        """
        Put a strategy in cooling-off period.
        
        Args:
            strategy_name: Name of the strategy
            reason: Reason for cooling off
        """
        if not self.enable_cooling_off:
            return
        
        cooling_off_until = datetime.now() + timedelta(hours=self.cooling_off_hours)
        self.cooling_off_until[strategy_name] = cooling_off_until
        
        if strategy_name in self.strategy_rankings:
            self.strategy_rankings[strategy_name].is_enabled = False
        
        self.logger.warning(f"Strategy {strategy_name} put in cooling-off until {cooling_off_until} - Reason: {reason}")
    
    def check_for_cooling_off(self, strategy_name: str) -> bool:
        """
        Check if a strategy should be put in cooling-off based on recent performance.
        
        Args:
            strategy_name: Name of the strategy
            
        Returns:
            True if strategy was put in cooling-off
        """
        if not self.enable_cooling_off:
            return False
        
        ranking = self.strategy_rankings.get(strategy_name)
        if not ranking:
            return False
        
        metrics = ranking.metrics
        
        # Check for losing streak
        current_lose_streak = metrics.get('current_lose_streak', 0)
        if current_lose_streak >= self.cooling_off_loss_streak:
            self.put_strategy_in_cooling_off(
                strategy_name,
                f"Losing streak of {current_lose_streak}"
            )
            return True
        
        return False
    
    def get_rankings_summary(self) -> Dict[str, Any]:
        """Get a summary of current strategy rankings."""
        self.update_rankings()
        
        return {
            'selection_mode': self.selection_mode.value,
            'ranking_metric': self.ranking_metric.value,
            'last_updated': self.last_rerank_time.isoformat() if self.last_rerank_time else None,
            'total_strategies': len(self.strategy_rankings),
            'enabled_strategies': len(self.get_enabled_strategies()),
            'strategies_in_cooling_off': len(self.cooling_off_until),
            'rankings': [
                ranking.to_dict()
                for ranking in sorted(
                    self.strategy_rankings.values(),
                    key=lambda r: r.rank
                )
            ],
        }
    
    def _log_rankings(self):
        """Log current strategy rankings."""
        self.logger.info("=" * 60)
        self.logger.info("📊 STRATEGY RANKINGS")
        self.logger.info(f"Mode: {self.selection_mode.value} | Metric: {self.ranking_metric.value}")
        self.logger.info("-" * 60)
        
        for ranking in sorted(self.strategy_rankings.values(), key=lambda r: r.rank):
            status = "✅" if ranking.is_enabled else "❌"
            metrics = ranking.metrics
            
            self.logger.info(
                f"{status} #{ranking.rank} {ranking.strategy_name:<15} | "
                f"Score: {ranking.score:.3f} | Weight: {ranking.weight:.2f} | "
                f"WR: {metrics.get('win_rate', 0):.1f}% | "
                f"PF: {metrics.get('profit_factor', 0):.2f} | "
                f"Exp: ${metrics.get('expectancy', 0):.2f}"
            )
        
        self.logger.info("=" * 60)
    
    def set_selection_mode(self, mode: SelectionMode):
        """Change the selection mode."""
        self.selection_mode = mode
        self.update_rankings(force=True)
        self.logger.info(f"Selection mode changed to: {mode.value}")
    
    def set_ranking_metric(self, metric: RankingMetric):
        """Change the ranking metric."""
        self.ranking_metric = metric
        self.update_rankings(force=True)
        self.logger.info(f"Ranking metric changed to: {metric.value}")

