"""
Tests for stale-data force-close handling (live failure 2026-06-11 01:35:
a 74s WebSocket blip left xyz:GOLD with no _symbol_last_tick entry; the
check treated None as inf staleness and force-closed the position, then
re-entered a minute later — fee churn for a phantom outage).

Contract: a missing timestamp means UNKNOWN — seed the clock, probe REST,
manage normally; force-close only after a MEASURED outage >= threshold.
"""

import time
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.strategies.strategy_manager import StrategyManager


def make_manager(tick_ages=None, rest_price=100.0):
    """tick_ages: {symbol: seconds_ago} — None entry means no tick recorded."""
    mgr = StrategyManager.__new__(StrategyManager)
    mgr.logger = logging.getLogger('test_stale')
    mgr.config = {}
    now = time.time()
    tick_map = {}
    for sym, age in (tick_ages or {}).items():
        if age is not None:
            tick_map[sym] = now - age
    mgr.market_api = SimpleNamespace(
        _symbol_last_tick=tick_map,
        get_current_price=lambda s: rest_price,
        _price_data={},
    )
    mgr.execution_engine = SimpleNamespace(
        positions={'xyz:GOLD': SimpleNamespace(symbol='xyz:GOLD', side='short')},
        multi_leg_positions={},
        close_position=MagicMock(),
    )
    mgr._get_position_timeframe = lambda s: '4h'
    mgr._check_exit_conditions_with_price = MagicMock()
    return mgr


class TestStaleDataHandling:
    def test_missing_timestamp_does_not_force_close(self):
        mgr = make_manager(tick_ages={})  # no tick ever recorded
        mgr._handle_stale_data_for_symbol('xyz:GOLD')
        mgr.execution_engine.close_position.assert_not_called()
        # Clock seeded so the NEXT check measures a real duration
        assert 'xyz:GOLD' in mgr.market_api._symbol_last_tick

    def test_missing_timestamp_with_rest_price_runs_exit_checks(self):
        mgr = make_manager(tick_ages={}, rest_price=4000.0)
        mgr._handle_stale_data_for_symbol('xyz:GOLD')
        mgr._check_exit_conditions_with_price.assert_called_once()

    def test_missing_timestamp_rest_down_still_no_close(self):
        mgr = make_manager(tick_ages={}, rest_price=None)
        mgr._handle_stale_data_for_symbol('xyz:GOLD')
        mgr.execution_engine.close_position.assert_not_called()
        assert 'xyz:GOLD' in mgr.market_api._symbol_last_tick

    def test_measured_outage_over_threshold_closes(self):
        threshold = StrategyManager.STALE_DATA_FORCE_CLOSE_THRESHOLDS.get('4h', 600)
        mgr = make_manager(tick_ages={'xyz:GOLD': threshold + 60})
        mgr._handle_stale_data_for_symbol('xyz:GOLD')
        mgr.execution_engine.close_position.assert_called_once()

    def test_short_measured_staleness_does_not_close(self):
        mgr = make_manager(tick_ages={'xyz:GOLD': 60})
        mgr._handle_stale_data_for_symbol('xyz:GOLD')
        mgr.execution_engine.close_position.assert_not_called()

    def test_no_position_is_noop(self):
        mgr = make_manager(tick_ages={})
        mgr.execution_engine.positions = {}
        mgr._handle_stale_data_for_symbol('xyz:GOLD')
        mgr.execution_engine.close_position.assert_not_called()
