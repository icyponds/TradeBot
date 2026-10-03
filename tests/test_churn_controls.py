"""Churn controls: capital_rotation / same_strategy_upgrade switches (research round 8).

With the backtest selector no longer seeded from live trades, csm_4h solo
spent ~half its June-2026 trades on capital rotation / position-limit closes
and same-side "upgrades" (close + reopen of the very position it held).
Defaults keep the current behavior; the switches make both testable.
"""
from unittest.mock import MagicMock, patch

import pytest

from src.strategies.strategy_manager import StrategyManager


class Pos:
    def __init__(self, side, strength, strategy):
        self.side = side
        self.entry_signal_strength = strength
        self.strategy = strategy


def _manager(mock_config, mock_market_api, **rm):
    mock_config['strategies']['instances'] = []
    mock_config['trading'].update({
        'position_monitoring_interval': 10, 'enable_stale_order_cleanup': True,
        'position_sync_interval': 300, 'enable_position_validation': True,
        'order_timeout_minutes': 5,
    })
    mock_config.setdefault('risk_management', {}).update(rm)
    with patch('src.strategies.strategy_manager.StrategySelector'), \
         patch('src.strategies.strategy_manager.ExecutionEngine') as ee, \
         patch('src.strategies.strategy_manager.DynamicPairSelector'), \
         patch('src.strategies.strategy_manager.PerformanceTracker'):
        ee.return_value.positions = {}
        ee.return_value.get_multi_leg_position_by_leg_symbol.return_value = None
        mgr = StrategyManager(mock_config, mock_market_api)
    return mgr


def test_defaults_preserve_current_behavior(mock_config, mock_market_api):
    mgr = _manager(mock_config, mock_market_api)
    assert mgr.capital_rotation_enabled is True
    assert mgr.same_strategy_upgrade_enabled is True


def test_same_strategy_upgrade_allowed_by_default(mock_config, mock_market_api):
    mgr = _manager(mock_config, mock_market_api)
    mgr.execution_engine.positions = {'BTC': Pos('long', 0.3, 'csm_4h')}
    assert mgr._resolve_conflict('BTC', {'signal': 'buy'}, 0.9, strategy_name='csm_4h') == 'upgrade'


def test_same_strategy_upgrade_blocked_when_disabled(mock_config, mock_market_api):
    mgr = _manager(mock_config, mock_market_api, same_strategy_upgrade={'enabled': False})
    mgr.execution_engine.positions = {'BTC': Pos('long', 0.3, 'csm_4h')}
    assert mgr._resolve_conflict('BTC', {'signal': 'buy'}, 0.9, strategy_name='csm_4h') == 'block'


def test_cross_strategy_upgrade_unaffected_by_switch(mock_config, mock_market_api):
    mgr = _manager(mock_config, mock_market_api, same_strategy_upgrade={'enabled': False})
    mgr.execution_engine.positions = {'BTC': Pos('long', 0.3, 'other_strat')}
    assert mgr._resolve_conflict('BTC', {'signal': 'buy'}, 0.9, strategy_name='csm_4h') == 'upgrade'


def test_flips_unaffected_by_upgrade_switch(mock_config, mock_market_api):
    mgr = _manager(mock_config, mock_market_api, same_strategy_upgrade={'enabled': False})
    mgr.execution_engine.positions = {'BTC': Pos('long', 0.3, 'csm_4h')}
    assert mgr._resolve_conflict('BTC', {'signal': 'sell'}, 0.9, strategy_name='csm_4h') == 'flip'


@pytest.mark.parametrize("enabled,expect_close", [(True, True), (False, False)])
def test_capital_rotation_switch(mock_config, mock_market_api, enabled, expect_close):
    mgr = _manager(mock_config, mock_market_api, capital_rotation={'enabled': enabled})
    mgr.execution_engine.positions = {}
    mgr._check_portfolio_allocation = MagicMock(return_value={'allocation_percentage': 95.0, 'total_equity': 50000})
    mgr._close_least_profitable_position = MagicMock(return_value=True)

    result = mgr._should_execute_with_position_limit('ETH', {'signal': 'buy'}, 0.9, strategy_name='csm_4h')

    assert result is expect_close
    assert mgr._close_least_profitable_position.called is expect_close
