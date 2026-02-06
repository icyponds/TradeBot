"""
Strategy Circuit Breaker: Auto-disable strategies exceeding drawdown limits.

Per trade performance analysis, this prevents runaway losses by automatically
disabling strategies that hit daily loss thresholds.
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, Set


class StrategyCircuitBreaker:
    """Automatically disable strategies that exceed drawdown limits."""
    
    def __init__(self, config: Dict):
        self.logger = logging.getLogger(__name__)
        
        # Configuration
        self.daily_loss_limit = config.get('risk_management', {}).get('strategy_daily_loss_limit', 50.0)
        self.reset_hour = config.get('risk_management', {}).get('strategy_reset_hour', 0)  # UTC midnight
        
        # State tracking
        self.strategy_pnl_today: Dict[str, float] = defaultdict(float)
        self.disabled_strategies: Set[str] = set()
        self.last_reset_date: str = ""
        
        # Trade history for daily tracking
        self.trades_today: Dict[str, list] = defaultdict(list)
        
        self.logger.info(
            f"Strategy Circuit Breaker initialized: "
            f"daily_loss_limit=${self.daily_loss_limit:.2f}"
        )
    
    def record_trade(self, strategy_name: str, pnl: float, timestamp: datetime = None) -> bool:
        """
        Record trade P&L and check if strategy should be disabled.
        
        Args:
            strategy_name: Name of the strategy
            pnl: Trade P&L (can be negative)
            timestamp: Trade timestamp (defaults to now)
        
        Returns:
            True if strategy should be disabled, False otherwise
        """
        if timestamp is None:
            timestamp = datetime.now()
        
        # Check if we need to reset daily counters
        self._check_daily_reset(timestamp)
        
        # Skip if already disabled
        if strategy_name in self.disabled_strategies:
            self.logger.debug(f"Skipping trade for disabled strategy {strategy_name}")
            return True
        
        # Record trade
        self.strategy_pnl_today[strategy_name] += pnl
        self.trades_today[strategy_name].append({
            'pnl': pnl,
            'timestamp': timestamp,
            'cumulative_pnl': self.strategy_pnl_today[strategy_name]
        })
        
        # Check if threshold breached
        if self.strategy_pnl_today[strategy_name] < -self.daily_loss_limit:
            self._disable_strategy(strategy_name)
            return True
        
        return False
    
    def _disable_strategy(self, strategy_name: str):
        """Disable a strategy due to excessive losses."""
        self.disabled_strategies.add(strategy_name)
        loss = abs(self.strategy_pnl_today[strategy_name])
        trade_count = len(self.trades_today[strategy_name])
        
        self.logger.critical(
            f"🚨 CIRCUIT BREAKER: {strategy_name} DISABLED after losing "
            f"${loss:.2f} today ({trade_count} trades). "
            f"Will reset at {self.reset_hour}:00 UTC."
        )
    
    def _check_daily_reset(self, current_time: datetime):
        """Reset daily counters if we've crossed the reset hour."""
        current_date = current_time.strftime("%Y-%m-%d")
        current_hour = current_time.hour
        
        # Check if it's a new day or we've crossed reset hour
        if self.last_reset_date != current_date:
            # Only reset if we're past reset hour
            if current_hour >= self.reset_hour or self.last_reset_date == "":
                self._reset_daily_state(current_date)
    
    def _reset_daily_state(self, date_str: str):
        """Reset all daily counters and re-enable strategies."""
        if self.disabled_strategies:
            self.logger.info(
                f"Circuit Breaker Daily Reset: Re-enabling {len(self.disabled_strategies)} strategies"
            )
        
        self.strategy_pnl_today.clear()
        self.trades_today.clear()
        self.disabled_strategies.clear()
        self.last_reset_date = date_str
    
    def is_disabled(self, strategy_name: str) -> bool:
        """Check if a strategy is currently disabled."""
        return strategy_name in self.disabled_strategies
    
    def get_status_summary(self) -> Dict:
        """Get current circuit breaker status for all strategies."""
        summary = {
            'daily_loss_limit': self.daily_loss_limit,
            'reset_hour_utc': self.reset_hour,
            'last_reset': self.last_reset_date,
            'strategies': {}
        }
        
        for strategy_name in set(list(self.strategy_pnl_today.keys()) + list(self.disabled_strategies)):
            pnl = self.strategy_pnl_today.get(strategy_name, 0.0)
            trade_count = len(self.trades_today.get(strategy_name, []))
            utilization = abs(pnl) / self.daily_loss_limit if pnl < 0 else 0.0
            
            summary['strategies'][strategy_name] = {
                'pnl_today': round(pnl, 2),
                'trades_today': trade_count,
                'disabled': strategy_name in self.disabled_strategies,
                'loss_utilization_pct': round(utilization * 100, 1)
            }
        
        return summary
    
    def force_disable(self, strategy_name: str, reason: str = "Manual disable"):
        """Manually disable a strategy."""
        self.disabled_strategies.add(strategy_name)
        self.logger.warning(f"Strategy {strategy_name} manually disabled: {reason}")
    
    def force_enable(self, strategy_name: str):
        """Manually re-enable a strategy."""
        if strategy_name in self.disabled_strategies:
            self.disabled_strategies.remove(strategy_name)
            self.logger.info(f"Strategy {strategy_name} manually re-enabled")
