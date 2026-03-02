"""
Unit tests for multi-leg position monitoring exit conditions.

Tests the defensive checks in _should_close_multi_leg_position including:
- max_holding_hours enforcement
- Orphan strategy detection
- Strategy-specific exit delegation
"""
import unittest
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime, timedelta


class MockMultiLegPosition:
    """Mock multi-leg position for testing."""
    _SENTINEL = object()
    def __init__(self, position_id='test_pos_1', strategy='stat_arb_4h',
                 entry_time=_SENTINEL, capital_at_risk=100.0, **kwargs):
        self.position_id = position_id
        self.strategy = strategy
        self.entry_time = datetime(2026, 1, 15, 12, 0, 0) if entry_time is MockMultiLegPosition._SENTINEL else entry_time
        self.capital_at_risk = capital_at_risk
        self.metadata = kwargs.get('metadata', {})
        self.primary_symbol = kwargs.get('primary_symbol', 'BTC')
        self.legs = kwargs.get('legs', [])


class MockStrategy:
    """Mock strategy with configurable should_exit and max_holding_hours."""
    def __init__(self, max_holding_hours=120):
        self.max_holding_hours = max_holding_hours

    def should_exit(self, position, current_price=None, current_data=None):
        return False, None

    def get_spread_status(self, symbol):
        return None


class TestShouldCloseMultiLegPosition(unittest.TestCase):
    """Tests for _should_close_multi_leg_position in StrategyManager."""

    def _create_strategy_manager(self, strategies=None):
        """Create a minimal mock StrategyManager with the real method."""
        from src.strategies.strategy_manager import StrategyManager
        
        # Create a mock that has the real _should_close_multi_leg_position
        sm = MagicMock(spec=StrategyManager)
        sm.strategies = strategies or {}
        sm.logger = MagicMock()
        
        # Bind the real method
        sm._should_close_multi_leg_position = StrategyManager._should_close_multi_leg_position.__get__(sm)
        return sm

    def test_max_holding_hours_triggers_exit(self):
        """Position held beyond max_holding_hours should be closed."""
        strategy = MockStrategy(max_holding_hours=48)
        sm = self._create_strategy_manager(strategies={'stat_arb_4h': strategy})
        
        # Position entered 72 hours ago
        entry_time = datetime(2026, 2, 1, 12, 0, 0)
        now = datetime(2026, 2, 4, 12, 0, 0)  # 72 hours later
        
        position = MockMultiLegPosition(
            strategy='stat_arb_4h',
            entry_time=entry_time
        )
        
        result = sm._should_close_multi_leg_position(position, timestamp=now)
        
        self.assertIsNotNone(result)
        self.assertIn('max_holding_time_exceeded', result)

    def test_max_holding_hours_does_not_trigger_early(self):
        """Position within max_holding_hours should NOT be closed by time check."""
        strategy = MockStrategy(max_holding_hours=120)
        sm = self._create_strategy_manager(strategies={'stat_arb_4h': strategy})
        
        # Position entered 24 hours ago (well within 120h limit)
        entry_time = datetime(2026, 2, 1, 12, 0, 0)
        now = datetime(2026, 2, 2, 12, 0, 0)  # 24 hours later
        
        position = MockMultiLegPosition(
            strategy='stat_arb_4h',
            entry_time=entry_time
        )
        
        result = sm._should_close_multi_leg_position(position, timestamp=now)
        
        # Should be None (no close reason) — strategy.should_exit also returns (False, None)
        self.assertIsNone(result)

    def test_orphan_strategy_detected(self):
        """Position with non-existent strategy should be closed as orphan."""
        sm = self._create_strategy_manager(strategies={'vol_breakout_4h': MockStrategy()})
        
        # Position belongs to non-existent strategy
        position = MockMultiLegPosition(
            strategy='deleted_strategy_1h',
            entry_time=datetime(2026, 2, 1, 12, 0, 0)
        )
        
        now = datetime(2026, 2, 1, 13, 0, 0)  # 1 hour later (within max hold)
        result = sm._should_close_multi_leg_position(position, timestamp=now)
        
        self.assertIsNotNone(result)
        self.assertIn('orphaned_strategy', result)

    def test_strategy_exit_delegated(self):
        """Strategy-specific should_exit is called and respected."""
        strategy = MockStrategy(max_holding_hours=120)
        strategy.should_exit = MagicMock(return_value=(True, "z_score_converged"))
        
        sm = self._create_strategy_manager(strategies={'stat_arb_4h': strategy})
        
        entry_time = datetime(2026, 2, 1, 12, 0, 0)
        now = datetime(2026, 2, 1, 18, 0, 0)  # 6 hours
        
        position = MockMultiLegPosition(
            strategy='stat_arb_4h',
            entry_time=entry_time
        )
        
        result = sm._should_close_multi_leg_position(position, timestamp=now)
        
        self.assertIsNotNone(result)
        self.assertIn('z_score_converged', result)
        strategy.should_exit.assert_called_once()

    def test_timezone_naive_vs_aware_handled(self):
        """Timezone mismatch between entry_time and timestamp should not crash."""
        import pytz
        strategy = MockStrategy(max_holding_hours=48)
        sm = self._create_strategy_manager(strategies={'stat_arb_4h': strategy})
        
        # Timezone-aware entry, naive timestamp
        try:
            entry_time_aware = datetime(2026, 2, 1, 12, 0, 0, tzinfo=pytz.UTC)
        except ImportError:
            # Skip if pytz not available
            entry_time_aware = datetime(2026, 2, 1, 12, 0, 0)
        
        now_naive = datetime(2026, 2, 4, 12, 0, 0)  # 72 hours later, naive
        
        position = MockMultiLegPosition(
            strategy='stat_arb_4h',
            entry_time=entry_time_aware
        )
        
        # Should not raise an exception
        result = sm._should_close_multi_leg_position(position, timestamp=now_naive)
        
        # Should trigger max_holding (72h > 48h)
        self.assertIsNotNone(result)
        self.assertIn('max_holding_time_exceeded', result)

    def test_string_entry_time_handled(self):
        """String entry_time should be properly parsed."""
        strategy = MockStrategy(max_holding_hours=48)
        sm = self._create_strategy_manager(strategies={'stat_arb_4h': strategy})
        
        position = MockMultiLegPosition(
            strategy='stat_arb_4h',
            entry_time='2026-02-01T12:00:00'  # String format
        )
        
        now = datetime(2026, 2, 4, 12, 0, 0)  # 72 hours later
        
        result = sm._should_close_multi_leg_position(position, timestamp=now)
        
        self.assertIsNotNone(result)
        self.assertIn('max_holding_time_exceeded', result)

    def test_no_entry_time_skips_max_hold_check(self):
        """Position without entry_time should skip max_hold check gracefully."""
        strategy = MockStrategy(max_holding_hours=48)
        sm = self._create_strategy_manager(strategies={'stat_arb_4h': strategy})
        
        position = MockMultiLegPosition(
            strategy='stat_arb_4h',
            entry_time=None
        )
        
        now = datetime(2026, 2, 4, 12, 0, 0)
        
        # Should not crash, just skip the max_hold check
        result = sm._should_close_multi_leg_position(position, timestamp=now)
        
        # No close reason (strategy.should_exit returns False, None)
        self.assertIsNone(result)


if __name__ == '__main__':
    unittest.main()
