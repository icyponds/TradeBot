"""
Dynamic strategy selection based on performance metrics.

This module provides functionality to:
- Rank strategies by performance (win rate, profit factor, expectancy, etc.)
- Dynamically enable/disable strategies based on performance
- Weight strategy signals based on confidence
- Rotate strategies based on market conditions
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple
from enum import Enum


class SelectionMode(Enum):
    """Strategy selection modes."""
    ALL = "all"                      # Use all strategies
    TOP_N = "top_n"                  # Use top N performing strategies
    THRESHOLD = "threshold"          # Use strategies above performance threshold
    WEIGHTED = "weighted"            # Weight signals by strategy performance
    ROTATING = "rotating"            # Rotate between strategies periodically
    ADAPTIVE = "adaptive"            # Adapt based on market conditions


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
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'strategy_name': self.strategy_name,
            'rank': self.rank,
            'score': round(self.score, 4),
            'is_enabled': self.is_enabled,
            'weight': round(self.weight, 4),
            'metrics': {k: round(v, 4) if isinstance(v, float) else v for k, v in self.metrics.items()},
            'last_updated': self.last_updated.isoformat(),
        }


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
        
        # Automatic selection - use composite ranking with threshold filtering
        self.selection_mode = SelectionMode.THRESHOLD
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
            'win_rate': 0.20,
            'profit_factor': 0.30,
            'expectancy': 0.25,
            'sharpe_ratio': 0.10,
            'risk_reward_ratio': 0.15,
        }
        
        self.logger.info("StrategySelector initialized - automatic performance-based selection enabled")
    
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
                score = self._calculate_score(metrics_dict)
            
            strategy_scores.append((strategy_name, score, metrics_dict))
        
        # Sort by score (descending)
        strategy_scores.sort(key=lambda x: x[1], reverse=True)
        
        # Create rankings
        self.strategy_rankings = {}
        for rank, (strategy_name, score, metrics_dict) in enumerate(strategy_scores, 1):
            # Determine if strategy is enabled based on selection mode
            is_enabled = self._should_enable_strategy(strategy_name, rank, score, metrics_dict)
            
            # Calculate weight based on score
            weight = self._calculate_weight(score, rank, len(strategy_scores))
            
            # Check cooling-off period
            if strategy_name in self.cooling_off_until:
                if now < self.cooling_off_until[strategy_name]:
                    is_enabled = False
                    self.logger.info(f"Strategy {strategy_name} is in cooling-off until {self.cooling_off_until[strategy_name]}")
                else:
                    del self.cooling_off_until[strategy_name]
            
            self.strategy_rankings[strategy_name] = StrategyRanking(
                strategy_name=strategy_name,
                rank=rank,
                score=score,
                is_enabled=is_enabled,
                weight=weight,
                metrics=metrics_dict,
                last_updated=now,
            )
        
        self.last_rerank_time = now
        self._log_rankings()
        
        return self.strategy_rankings
    
    def _calculate_score(self, metrics: Dict[str, float]) -> float:
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
            return self._calculate_composite_score(metrics)
        
        return 0.5
    
    def _calculate_composite_score(self, metrics: Dict[str, float]) -> float:
        """Calculate composite score from multiple metrics."""
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
        
        # Normalize by total weight
        if total_weight > 0:
            score = score / total_weight
        
        return score
    
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
        
        elif self.selection_mode == SelectionMode.WEIGHTED:
            # In weighted mode, all strategies are enabled but with different weights
            return True
        
        elif self.selection_mode == SelectionMode.ROTATING:
            # TODO: Implement rotation logic
            return rank <= self.top_n
        
        elif self.selection_mode == SelectionMode.ADAPTIVE:
            # TODO: Implement adaptive logic based on market conditions
            return score >= 0.4
        
        return True
    
    def _calculate_weight(self, score: float, rank: int, total: int) -> float:
        """Calculate strategy weight based on score and rank."""
        
        if self.selection_mode == SelectionMode.WEIGHTED:
            # Linear weight based on score
            weight = self.min_weight + (score * (self.max_weight - self.min_weight))
            return max(self.min_weight, min(self.max_weight, weight))
        
        elif self.selection_mode in [SelectionMode.TOP_N, SelectionMode.THRESHOLD]:
            # Binary weight based on enabled status
            return 1.0
        
        else:
            # Default equal weight
            return 1.0
    
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

