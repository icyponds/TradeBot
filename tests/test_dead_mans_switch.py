"""Tests for the dead man's switch gating.

The scheduleCancel dead man's switch auto-cancels ALL resting orders ~timeout
after the bot stops heart-beating. Native protective stops are now the only
resting orders and MUST survive a crash to keep protecting open positions, so
the switch defaults OFF — it would cancel exactly the protection we want.
Re-enable only if the bot starts resting non-protective orders.
"""

import logging
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.strategies.strategy_manager import StrategyManager


def make_manager(enabled=False, timeout=30):
    mgr = StrategyManager.__new__(StrategyManager)
    mgr.logger = logging.getLogger('test_dms')
    mgr.config = {'risk_management': {'dead_mans_switch': {
        'enabled': enabled, 'timeout_seconds': timeout}}}
    mgr.market_api = SimpleNamespace(
        refresh_dead_mans_switch=MagicMock(return_value=True),
        set_dead_mans_switch=MagicMock(return_value=True),
    )
    mgr.last_heartbeat_refresh = 0.0
    return mgr


class TestDeadMansSwitchEnabled:
    def test_defaults_off(self):
        # Missing config block -> off (protective stops survive a crash).
        mgr = StrategyManager.__new__(StrategyManager)
        mgr.config = {'risk_management': {}}
        assert mgr._dead_mans_switch_enabled() is False

    def test_reads_flag(self):
        assert make_manager(enabled=True)._dead_mans_switch_enabled() is True
        assert make_manager(enabled=False)._dead_mans_switch_enabled() is False


class TestRefreshGating:
    def test_disabled_never_refreshes(self):
        mgr = make_manager(enabled=False)
        mgr._refresh_dead_mans_switch_periodic()
        mgr.market_api.refresh_dead_mans_switch.assert_not_called()

    def test_enabled_refreshes_with_configured_timeout(self):
        mgr = make_manager(enabled=True, timeout=45)
        # last_heartbeat_refresh=0 (long ago) so the 15s throttle lets it run.
        mgr._refresh_dead_mans_switch_periodic()
        mgr.market_api.refresh_dead_mans_switch.assert_called_once_with(45)

    def test_enabled_throttles_within_15s(self):
        mgr = make_manager(enabled=True)
        mgr.last_heartbeat_refresh = time.time()  # just refreshed
        mgr._refresh_dead_mans_switch_periodic()
        mgr.market_api.refresh_dead_mans_switch.assert_not_called()
