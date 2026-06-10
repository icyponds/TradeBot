"""
Tests for the per-strategy equity circuit breaker in StrategyManager:
- blocks when rolling realized loss breaches the threshold
- ignores losses outside the lookback window (stateless self-healing)
- per-strategy isolation (one strategy's losses don't halt another)
- disabled by default / no tracker -> never blocks
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.strategies.strategy_manager import StrategyManager


def make_manager(trades, enabled=True, threshold_pct=5.0, lookback_days=7.0,
                 initial_equity=50000.0, now=None):
    """Bare StrategyManager with only the attributes the breaker reads."""
    mgr = StrategyManager.__new__(StrategyManager)
    mgr.config = {"risk_management": {"circuit_breaker": {
        "enabled": enabled,
        "loss_threshold_pct": threshold_pct,
        "lookback_days": lookback_days,
    }}}
    mgr.performance_tracker = SimpleNamespace(
        completed_trades=trades, initial_equity=initial_equity)
    mgr._cycle_timestamp = now or datetime(2026, 1, 15)
    import logging
    mgr.logger = logging.getLogger("test_cb")
    return mgr


def trade(strategy, pnl, days_ago, now=None):
    now = now or datetime(2026, 1, 15)
    return SimpleNamespace(strategy=strategy, pnl=pnl,
                           exit_time=now - timedelta(days=days_ago))


class TestCircuitBreaker:
    def test_blocks_on_breach(self):
        # -3000 in window vs threshold -5% of (50000 - 3000) = -2350
        mgr = make_manager([trade('csm_4h', -1500, 1), trade('csm_4h', -1500, 3)])
        assert mgr._circuit_breaker_active('csm_4h')

    def test_no_block_below_threshold(self):
        mgr = make_manager([trade('csm_4h', -1000, 1)])
        assert not mgr._circuit_breaker_active('csm_4h')

    def test_old_losses_age_out(self):
        # Same losses but outside the 7d lookback -> strategy re-enabled
        mgr = make_manager([trade('csm_4h', -1500, 8), trade('csm_4h', -1500, 10)])
        assert not mgr._circuit_breaker_active('csm_4h')

    def test_per_strategy_isolation(self):
        mgr = make_manager([trade('csm_4h', -3000, 1)])
        assert mgr._circuit_breaker_active('csm_4h')
        assert not mgr._circuit_breaker_active('vol_breakout_4h')

    def test_wins_offset_losses(self):
        mgr = make_manager([trade('csm_4h', -3000, 1), trade('csm_4h', +2000, 2)])
        assert not mgr._circuit_breaker_active('csm_4h')

    def test_disabled_never_blocks(self):
        mgr = make_manager([trade('csm_4h', -30000, 1)], enabled=False)
        assert not mgr._circuit_breaker_active('csm_4h')

    def test_no_tracker_never_blocks(self):
        mgr = make_manager([])
        mgr.performance_tracker = None
        assert not mgr._circuit_breaker_active('csm_4h')

    def test_string_exit_times_handled(self):
        t = SimpleNamespace(strategy='csm_4h', pnl=-3000.0,
                            exit_time='2026-01-14 12:00:00')
        mgr = make_manager([t])
        assert mgr._circuit_breaker_active('csm_4h')

    def test_equity_scales_threshold(self):
        # -3000 rolling loss: breaches at 50k equity (-5% = -2500+pnl adj),
        # not at 200k (-5% = -9850)
        mgr = make_manager([trade('csm_4h', -3000, 1)], initial_equity=200000.0)
        assert not mgr._circuit_breaker_active('csm_4h')
