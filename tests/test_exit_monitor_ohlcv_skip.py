"""
Exit monitor cost regression (live 2026-07-03): the per-cycle OHLCV fetch
in _should_close_position ran for EVERY position even when the strategy's
should_exit was the BaseStrategy no-op — pure wasted rate-limiter budget.
Under background-scout saturation those queued fetches stalled the exit
monitor past the 45s watchdog threshold twice in 10 minutes.
"""
import pytest
from unittest.mock import MagicMock, patch

from src.strategies.strategy_manager import StrategyManager
from src.strategies.base_strategy import BaseStrategy


@pytest.fixture
def manager(mock_config, mock_market_api):
    mock_config['strategies']['instances'] = [
        {'type': 'cross_sectional_momentum', 'name': 'csm_4h', 'timeframe': '4h'}
    ]
    mock_config['strategies']['ohlcv_limit'] = 100
    mock_config['trading'].update({
        'position_monitoring_interval': 10,
        'enable_stale_order_cleanup': True,
        'position_sync_interval': 300,
        'enable_position_validation': True,
        'order_timeout_minutes': 5,
    })
    with patch('src.strategies.strategy_manager.StrategySelector'), \
         patch('src.strategies.strategy_manager.ExecutionEngine'), \
         patch('src.strategies.strategy_manager.DynamicPairSelector'), \
         patch('src.strategies.strategy_manager.PerformanceTracker'):
        return StrategyManager(mock_config, mock_market_api)


def _position(strategy='csm_4h'):
    pos = MagicMock()
    pos.symbol = 'BTC'
    pos.strategy = strategy
    pos.current_price = 50000.0
    pos.stop_loss = None
    pos.take_profit = None
    pos.side = 'long'
    pos.entry_price = 50000.0
    pos.size = 0.001
    pos.trailing_stop_enabled = False
    pos.unrealized_pnl = 0.0
    pos.unrealized_pnl_percentage = 0.0
    pos.capital_at_risk = 10.0
    return pos


def test_no_ohlcv_fetch_for_default_should_exit(manager):
    """csm_4h does not override should_exit -> the fetch must be skipped."""
    strategy = manager.strategies['csm_4h']
    assert type(strategy).should_exit is BaseStrategy.should_exit, \
        "precondition: csm uses the base no-op should_exit"

    manager.market_api.get_ohlcv = MagicMock()
    manager._should_close_position(_position())

    manager.market_api.get_ohlcv.assert_not_called()


def test_ohlcv_fetched_for_overriding_strategy(manager):
    """Strategies with a real should_exit still get their data."""
    class ExitingStrategy(BaseStrategy):
        PREFERRED_TIMEFRAME = '1h'

        def generate_signal(self, symbol, ohlcv):
            return None

        def should_exit(self, position, current_price, current_data=None):
            return False, None

    manager.strategies['custom'] = ExitingStrategy(
        {'strategies': {'ohlcv_limit': 100}}, timeframe='1h')
    manager.market_api.get_ohlcv = MagicMock(return_value=None)

    manager._should_close_position(_position(strategy='custom'))

    manager.market_api.get_ohlcv.assert_called_once()
