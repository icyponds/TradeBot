"""
Per-strategy capital sleeves (risk_management.capital_sleeves).

Research round 6 (2026-07-02, reports/oos_matrix3): csm_4h and
sentiment_ml_1h each pass the validation bar alone but lose -$10.1k
together — one shared capital pool lets strategies evict each other's
positions (capital rotation churn). Sleeves budget each strategy a fixed
fraction of the allocation cap and restrict rotation to same-strategy
positions. Default OFF.
"""
import pytest
from unittest.mock import MagicMock, patch

from src.strategies.strategy_manager import StrategyManager


def _make_position(strategy: str, capital_at_risk: float = 1000.0):
    pos = MagicMock()
    pos.strategy = strategy
    pos.capital_at_risk = capital_at_risk
    pos.size = 1.0
    pos.entry_price = capital_at_risk
    pos.leverage = 1.0
    return pos


def _make_multi_leg(strategy: str, capital_at_risk: float = 1000.0):
    pos = MagicMock()
    pos.strategy = strategy
    pos.capital_at_risk = capital_at_risk
    pos.total_notional = capital_at_risk
    return pos


@pytest.fixture
def manager_factory(mock_config, mock_market_api):
    """Build a StrategyManager with two strategy instances and given sleeve config."""
    def build(sleeves_cfg=None):
        config = dict(mock_config)
        config['strategies'] = dict(mock_config['strategies'])
        config['strategies']['instances'] = [
            {'type': 'cross_sectional_momentum', 'name': 'csm_4h', 'timeframe': '4h'},
            {'type': 'sentiment_ml', 'name': 'sentiment_ml_1h', 'timeframe': '1h'},
        ]
        config['strategies']['ohlcv_limit'] = 100
        config['trading'] = dict(mock_config['trading'])
        config['trading']['position_monitoring_interval'] = 10
        config['trading']['enable_stale_order_cleanup'] = True
        config['trading']['position_sync_interval'] = 300
        config['trading']['enable_position_validation'] = True
        config['trading']['order_timeout_minutes'] = 5
        config['risk_management'] = dict(mock_config['risk_management'])
        if sleeves_cfg is not None:
            config['risk_management']['capital_sleeves'] = sleeves_cfg

        with patch('src.strategies.strategy_manager.StrategySelector'), \
             patch('src.strategies.strategy_manager.ExecutionEngine'), \
             patch('src.strategies.strategy_manager.DynamicPairSelector'), \
             patch('src.strategies.strategy_manager.PerformanceTracker'):
            manager = StrategyManager(config, mock_market_api)

        manager.execution_engine.positions = {}
        manager.execution_engine.multi_leg_positions = {}
        return manager

    return build


# ---------------------------------------------------------------------------
# Disabled / default path: behavior unchanged
# ---------------------------------------------------------------------------

class TestSleevesDisabled:
    def test_default_off(self, manager_factory):
        manager = manager_factory(sleeves_cfg=None)
        assert manager.capital_sleeves_enabled is False
        assert manager._sleeve_fraction('csm_4h') is None
        assert manager._sleeve_headroom('csm_4h') is None

    def test_rotation_unrestricted_when_disabled(self, manager_factory):
        """Without sleeves, rotation may displace any strategy's position."""
        manager = manager_factory(sleeves_cfg={'enabled': False, 'weights': {}})
        manager.execution_engine.positions = {
            'ETH': _make_position('sentiment_ml_1h'),
        }
        with patch.object(manager, '_get_position_profitability_score', return_value=0.1), \
             patch.object(manager, '_check_position_limit', return_value=False), \
             patch.object(manager, 'close_position') as mock_close:
            closed = manager._close_least_profitable_position(0.9)
        assert closed is True
        mock_close.assert_called_once()
        assert mock_close.call_args[0][0] == 'ETH'


# ---------------------------------------------------------------------------
# Enabled path: fractions, budgets, isolation
# ---------------------------------------------------------------------------

class TestSleeveFractions:
    def test_equal_split_with_empty_weights(self, manager_factory):
        manager = manager_factory(sleeves_cfg={'enabled': True, 'weights': {}})
        assert manager._sleeve_fraction('csm_4h') == pytest.approx(0.5)
        assert manager._sleeve_fraction('sentiment_ml_1h') == pytest.approx(0.5)

    def test_relative_weights(self, manager_factory):
        """Unlisted instances default to weight 1.0."""
        manager = manager_factory(sleeves_cfg={'enabled': True, 'weights': {'csm_4h': 2.0}})
        assert manager._sleeve_fraction('csm_4h') == pytest.approx(2.0 / 3.0)
        assert manager._sleeve_fraction('sentiment_ml_1h') == pytest.approx(1.0 / 3.0)

    def test_invalid_and_zero_weights_count_as_one(self, manager_factory):
        manager = manager_factory(sleeves_cfg={
            'enabled': True,
            'weights': {'csm_4h': 0.0, 'sentiment_ml_1h': 'garbage'},
        })
        assert manager._sleeve_fraction('csm_4h') == pytest.approx(0.5)
        assert manager._sleeve_fraction('sentiment_ml_1h') == pytest.approx(0.5)

    def test_unknown_strategy_gets_equal_share(self, manager_factory):
        manager = manager_factory(sleeves_cfg={'enabled': True, 'weights': {}})
        assert manager._sleeve_fraction('legacy_name') == pytest.approx(0.5)


class TestStrategyCapitalAtRisk:
    def test_counts_single_and_multi_leg_for_owner_only(self, manager_factory):
        """Parity rule: multi-leg positions consume the owner's sleeve too."""
        manager = manager_factory(sleeves_cfg={'enabled': True, 'weights': {}})
        manager.execution_engine.positions = {
            'BTC': _make_position('csm_4h', capital_at_risk=1500.0),
            'ETH': _make_position('sentiment_ml_1h', capital_at_risk=999.0),
        }
        manager.execution_engine.multi_leg_positions = {
            'ml_1': _make_multi_leg('csm_4h', capital_at_risk=500.0),
            'ml_2': _make_multi_leg('sentiment_ml_1h', capital_at_risk=777.0),
        }
        assert manager._strategy_capital_at_risk('csm_4h') == pytest.approx(2000.0)
        assert manager._strategy_capital_at_risk('sentiment_ml_1h') == pytest.approx(1776.0)

    def test_fallback_when_capital_at_risk_missing(self, manager_factory):
        manager = manager_factory(sleeves_cfg={'enabled': True, 'weights': {}})
        pos = _make_position('csm_4h')
        pos.capital_at_risk = None
        pos.size = 2.0
        pos.entry_price = 1000.0
        pos.leverage = 4.0
        manager.execution_engine.positions = {'BTC': pos}
        # notional 2000 / leverage 4 = 500
        assert manager._strategy_capital_at_risk('csm_4h') == pytest.approx(500.0)


class TestSleeveHeadroom:
    def test_headroom_math(self, manager_factory):
        manager = manager_factory(sleeves_cfg={'enabled': True, 'weights': {}})
        manager.portfolio_manager = MagicMock()
        manager.portfolio_manager.total_equity = 50000.0
        manager.max_positions_percentage = 100.0
        manager.execution_engine.positions = {
            'BTC': _make_position('csm_4h', capital_at_risk=10000.0),
        }
        # budget = 50k * 100% * 0.5 = 25k; headroom = 25k - 10k = 15k
        assert manager._sleeve_headroom('csm_4h') == pytest.approx(15000.0)
        # sentiment has no positions: full 25k
        assert manager._sleeve_headroom('sentiment_ml_1h') == pytest.approx(25000.0)

    def test_headroom_floor_zero(self, manager_factory):
        manager = manager_factory(sleeves_cfg={'enabled': True, 'weights': {}})
        manager.portfolio_manager = MagicMock()
        manager.portfolio_manager.total_equity = 10000.0
        manager.max_positions_percentage = 100.0
        manager.execution_engine.positions = {
            'BTC': _make_position('csm_4h', capital_at_risk=99999.0),
        }
        assert manager._sleeve_headroom('csm_4h') == 0.0

    def test_unknown_equity_returns_none(self, manager_factory):
        """API failure (equity 0): fall back to global behavior, don't block."""
        manager = manager_factory(sleeves_cfg={'enabled': True, 'weights': {}})
        manager.portfolio_manager = MagicMock()
        manager.portfolio_manager.total_equity = 0.0
        assert manager._sleeve_headroom('csm_4h') is None


class TestSleeveGate:
    def _allocation(self, pct, equity=50000.0):
        return {
            'allocation_percentage': pct,
            'total_equity': equity,
            'total_capital_at_risk': equity * pct / 100.0,
            'max_allocation': 100.0,
        }

    def test_blocks_strategy_over_its_sleeve(self, manager_factory):
        """csm fills its half; a new csm entry is blocked even though the
        OTHER strategy's half of the book is empty — and sentiment's
        positions are never eligible for displacement."""
        manager = manager_factory(sleeves_cfg={'enabled': True, 'weights': {}})
        manager.max_positions_percentage = 100.0
        # reserve 10% -> effective_max 90, sleeve_max 45% of equity
        manager.execution_engine.positions = {
            'BTC': _make_position('csm_4h', capital_at_risk=23000.0),  # 46% of 50k
            'ETH': _make_position('sentiment_ml_1h', capital_at_risk=1000.0),
        }
        with patch.object(manager, '_check_portfolio_allocation',
                          return_value=self._allocation(48.0)), \
             patch.object(manager, '_get_position_profitability_score', return_value=0.0), \
             patch.object(manager, 'close_position') as mock_close:
            # csm over sleeve: rotation considers only csm positions; BTC is
            # its only one and scores 0.0 < strength - threshold -> closes BTC
            result = manager._should_execute_with_position_limit(
                'SOL', {'signal': 'buy'}, 0.9, strategy_name='csm_4h')
        assert result is True
        mock_close.assert_called_once()
        assert mock_close.call_args[0][0] == 'BTC'  # never ETH (sentiment's)

    def test_blocked_when_no_own_position_to_rotate(self, manager_factory):
        """Over-sleeve strategy with nothing of its own to close is refused —
        it cannot evict the other strategy's positions."""
        manager = manager_factory(sleeves_cfg={'enabled': True, 'weights': {}})
        manager.max_positions_percentage = 100.0
        manager.execution_engine.positions = {
            'ETH': _make_position('sentiment_ml_1h', capital_at_risk=1000.0),
        }
        # csm's multi-leg position fills its sleeve
        manager.execution_engine.multi_leg_positions = {
            'ml_1': _make_multi_leg('csm_4h', capital_at_risk=23000.0),
        }
        with patch.object(manager, '_check_portfolio_allocation',
                          return_value=self._allocation(48.0)), \
             patch.object(manager, 'close_position') as mock_close:
            result = manager._should_execute_with_position_limit(
                'SOL', {'signal': 'buy'}, 0.9, strategy_name='csm_4h')
        assert result is False
        mock_close.assert_not_called()

    def test_under_sleeve_strategy_allowed(self, manager_factory):
        """The other strategy still has sleeve headroom and may trade."""
        manager = manager_factory(sleeves_cfg={'enabled': True, 'weights': {}})
        manager.max_positions_percentage = 100.0
        manager.execution_engine.positions = {
            'BTC': _make_position('csm_4h', capital_at_risk=23000.0),
        }
        with patch.object(manager, '_check_portfolio_allocation',
                          return_value=self._allocation(46.0)), \
             patch.object(manager, '_check_position_limit', return_value=False):
            result = manager._should_execute_with_position_limit(
                'SOL', {'signal': 'buy'}, 0.9, strategy_name='sentiment_ml_1h')
        assert result is True

    def test_no_strategy_name_keeps_global_behavior(self, manager_factory):
        """Callers that don't pass strategy_name see pre-sleeve behavior."""
        manager = manager_factory(sleeves_cfg={'enabled': True, 'weights': {}})
        with patch.object(manager, '_check_portfolio_allocation',
                          return_value=self._allocation(10.0)), \
             patch.object(manager, '_check_position_limit', return_value=False):
            result = manager._should_execute_with_position_limit(
                'SOL', {'signal': 'buy'}, 0.9)
        assert result is True


class TestRotationIsolation:
    def test_rotation_filters_by_owner(self, manager_factory):
        manager = manager_factory(sleeves_cfg={'enabled': True, 'weights': {}})
        manager.execution_engine.positions = {
            'BTC': _make_position('csm_4h'),
            'ETH': _make_position('sentiment_ml_1h'),
        }
        with patch.object(manager, '_get_position_profitability_score', return_value=0.0), \
             patch.object(manager, '_check_position_limit', return_value=False), \
             patch.object(manager, 'close_position') as mock_close:
            closed = manager._close_least_profitable_position(0.9, strategy_name='sentiment_ml_1h')
        assert closed is True
        assert mock_close.call_args[0][0] == 'ETH'

    def test_rotation_refuses_when_owner_has_no_positions(self, manager_factory):
        manager = manager_factory(sleeves_cfg={'enabled': True, 'weights': {}})
        manager.execution_engine.positions = {
            'ETH': _make_position('sentiment_ml_1h'),
        }
        with patch.object(manager, 'close_position') as mock_close:
            closed = manager._close_least_profitable_position(0.9, strategy_name='csm_4h')
        assert closed is False
        mock_close.assert_not_called()
