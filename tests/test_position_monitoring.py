"""
Tests for position monitoring fixes:
1. Force WebSocket subscriptions for open positions
2. Never-skip monitoring with _force_fetch_price_with_retry
3. Mandatory retry logic for close_position
"""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime
from src.strategies.strategy_manager import StrategyManager
from src.strategies.execution_engine import ExecutionEngine
from src.models.trade import Position


class TestPositionMonitoringFixes:
    """Test position monitoring retry and never-skip logic."""
    
    @pytest.fixture
    def strategy_manager(self, mock_config, mock_market_api):
        """Creates a StrategyManager instance with mocked dependencies."""
        if 'strategies' not in mock_config:
            mock_config['strategies'] = {}
            
        mock_config['strategies']['instances'] = [
            {'type': 'cross_sectional_momentum', 'name': 'csm_1h', 'timeframe': '1h'}
        ]
        mock_config['strategies']['ohlcv_limit'] = 100
        mock_config['trading']['position_monitoring_interval'] = 10
        mock_config['trading']['enable_stale_order_cleanup'] = True
        mock_config['trading']['position_sync_interval'] = 300
        mock_config['trading']['enable_position_validation'] = True
        mock_config['trading']['order_timeout_minutes'] = 5

        with patch('src.strategies.strategy_manager.StrategySelector'), \
             patch('src.strategies.strategy_manager.ExecutionEngine'), \
             patch('src.strategies.strategy_manager.DynamicPairSelector'), \
             patch('src.strategies.strategy_manager.PerformanceTracker'):
             
             manager = StrategyManager(mock_config, mock_market_api)
             return manager

    # ========== Force Fetch Price Tests ==========
    
    def test_force_fetch_price_with_retry_success(self, strategy_manager):
        """Test _force_fetch_price_with_retry returns price on success."""
        strategy_manager.market_api.get_current_price = MagicMock(return_value=225.0)
        
        price = strategy_manager._force_fetch_price_with_retry('TAO')
        
        assert price == 225.0
        strategy_manager.market_api.get_current_price.assert_called_once_with('TAO')
    
    def test_force_fetch_price_with_retry_retries_on_failure(self, strategy_manager):
        """Test that retry logic works on failures."""
        # Fail twice, succeed on third
        strategy_manager.market_api.get_current_price = MagicMock(
            side_effect=[None, None, 225.0]
        )
        
        with patch('time.sleep'):  # Skip actual sleep
            price = strategy_manager._force_fetch_price_with_retry('TAO', max_retries=3)
        
        assert price == 225.0
        assert strategy_manager.market_api.get_current_price.call_count == 3
    
    def test_force_fetch_price_with_retry_returns_none_after_max_retries(self, strategy_manager):
        """Test that None is returned after all retries exhausted."""
        strategy_manager.market_api.get_current_price = MagicMock(return_value=None)
        
        with patch('time.sleep'):
            price = strategy_manager._force_fetch_price_with_retry('TAO', max_retries=3)
        
        assert price is None
        assert strategy_manager.market_api.get_current_price.call_count == 3

    # ========== Never Skip Monitoring Tests ==========
    
    def test_monitor_fetches_price_when_none(self, strategy_manager):
        """Test that monitoring fetches price when current_price is None."""
        # Position with no current price
        position = Position(
            symbol='TAO',
            side='short',
            entry_price=240.0,
            size=1.5,
            entry_time=datetime.now(),
            strategy='ou_mean_reversion_15m',
            take_profit=228.0
        )
        position.current_price = None
        strategy_manager.execution_engine.positions = {'TAO': position}
        
        # Mock the force fetch to return a price
        strategy_manager._force_fetch_price_with_retry = MagicMock(return_value=225.0)
        strategy_manager._should_close_position = MagicMock(return_value='take_profit')
        strategy_manager.close_position = MagicMock(return_value=True)
        strategy_manager._check_emergency_stop = MagicMock(return_value=False)
        strategy_manager.total_positions_closed = 0
        strategy_manager.last_emergency_check = 0
        
        strategy_manager._monitor_and_close_positions(emergency_portfolio_loss_pct=10.0)
        
        # Should have called force fetch
        strategy_manager._force_fetch_price_with_retry.assert_called_with('TAO')
        # Position should now have price
        assert position.current_price == 225.0


class TestClosePositionRetry:
    """Test that close_position retries on failure."""
    
    @pytest.fixture
    def execution_engine(self, mock_config, mock_market_api):
        """Create ExecutionEngine with mocked dependencies."""
        # Mock all required dependencies
        mock_leverage_manager = MagicMock()
        mock_portfolio_manager = MagicMock()
        mock_performance_tracker = MagicMock()
        mock_performance_tracker.db = MagicMock()
        mock_performance_tracker.db.get_all_active_positions.return_value = []
        mock_pair_selector = MagicMock()
        
        ee = ExecutionEngine(
            config=mock_config,
            market_api=mock_market_api,
            leverage_manager=mock_leverage_manager,
            portfolio_manager=mock_portfolio_manager,
            performance_tracker=mock_performance_tracker,
            pair_selector=mock_pair_selector
        )
        return ee
    
    def test_close_position_retries_on_failure(self, execution_engine):
        """Test that close_position retries when execute_order fails."""
        # Add position
        position = Position(
            symbol='TAO',
            side='short',
            entry_price=240.0,
            size=1.5,
            entry_time=datetime.now(),
            strategy='test'
        )
        execution_engine.positions['TAO'] = position
        
        # Fail twice, succeed on third
        execution_engine.market_api.execute_order = MagicMock(
            side_effect=[
                None,  # First fail
                {},    # Second fail (empty result)
                {'filled_size': 1.5, 'avg_fill_price': 225.0, 'fills': []}  # Success
            ]
        )
        
        with patch('time.sleep'):
            result = execution_engine.close_position('TAO', 'take_profit')
        
        # close_position returns (bool, str) tuple
        assert result[0] is True
        assert execution_engine.market_api.execute_order.call_count == 3
    
    def test_close_position_fails_after_max_retries(self, execution_engine):
        """Test that close_position returns False after all retries fail."""
        position = Position(
            symbol='TAO',
            side='short',
            entry_price=240.0,
            size=1.5,
            entry_time=datetime.now(),
            strategy='test'
        )
        execution_engine.positions['TAO'] = position
        
        # Always fail
        execution_engine.market_api.execute_order = MagicMock(return_value=None)
        
        with patch('time.sleep'):
            result = execution_engine.close_position('TAO', 'stop_loss')
        
        # close_position returns (bool, str) tuple
        assert result[0] is False
        assert execution_engine.market_api.execute_order.call_count == 5  # max_attempts


class TestGhostPositionFixes:
    """Test fixes for ghost position re-tracking issues."""
    
    @pytest.fixture
    def execution_engine(self, mock_config, mock_market_api):
        """Create ExecutionEngine with mocked dependencies."""
        mock_leverage_manager = MagicMock()
        mock_portfolio_manager = MagicMock()
        mock_performance_tracker = MagicMock()
        mock_performance_tracker.db = MagicMock()
        mock_performance_tracker.db.get_all_active_positions.return_value = []
        mock_pair_selector = MagicMock()
        
        ee = ExecutionEngine(
            config=mock_config,
            market_api=mock_market_api,
            leverage_manager=mock_leverage_manager,
            portfolio_manager=mock_portfolio_manager,
            performance_tracker=mock_performance_tracker,
            pair_selector=mock_pair_selector
        )
        return ee
    
    def test_load_positions_normalizes_negative_size(self, execution_engine):
        """Test that negative size is normalized to positive on load."""
        # Mock DB returning position with negative size (as exchange API returns for shorts)
        execution_engine.performance_tracker.db.get_all_active_positions.return_value = [{
            'position_id': 'test_TAO',
            'strategy': 'ou_mean_reversion_15m',
            'symbol': 'TAO',
            'side': 'short',
            'size': -1.447,  # Negative from exchange
            'entry_price': 238.923,
            'entry_time': datetime.now().isoformat(),
            'metadata': {'capital_at_risk': -115.24},  # Also negative
            'legs': []
        }]
        
        # Clear positions and reload
        execution_engine.positions.clear()
        execution_engine.load_positions_from_db()
        
        # Verify size and capital_at_risk are normalized to positive
        assert 'TAO' in execution_engine.positions
        pos = execution_engine.positions['TAO']
        assert pos.size == 1.447, f"Size should be positive, got {pos.size}"
        assert pos.capital_at_risk == 115.24, f"Capital should be positive, got {pos.capital_at_risk}"
        assert pos.side == 'short', "Side should remain 'short'"
    
    def test_load_positions_preserves_positive_size(self, execution_engine):
        """Test that positive size remains positive on load."""
        execution_engine.performance_tracker.db.get_all_active_positions.return_value = [{
            'position_id': 'test_BTC',
            'strategy': 'csm_1h',
            'symbol': 'BTC',
            'side': 'long',
            'size': 0.5,  # Already positive
            'entry_price': 50000.0,
            'entry_time': datetime.now().isoformat(),
            'metadata': {'capital_at_risk': 250.0},
            'legs': []
        }]
        
        execution_engine.positions.clear()
        execution_engine.load_positions_from_db()
        
        assert 'BTC' in execution_engine.positions
        pos = execution_engine.positions['BTC']
        assert pos.size == 0.5
        assert pos.capital_at_risk == 250.0


class TestStartupExitCheck:
    """Test startup TP/SL exit check."""
    
    @pytest.fixture
    def strategy_manager(self, mock_config, mock_market_api):
        """Creates a StrategyManager instance with mocked dependencies."""
        if 'strategies' not in mock_config:
            mock_config['strategies'] = {}
        mock_config['strategies']['instances'] = []
        mock_config['strategies']['ohlcv_limit'] = 100
        mock_config['trading']['position_monitoring_interval'] = 10
        mock_config['trading']['enable_stale_order_cleanup'] = True
        mock_config['trading']['position_sync_interval'] = 300
        mock_config['trading']['enable_position_validation'] = True
        mock_config['trading']['order_timeout_minutes'] = 5

        with patch('src.strategies.strategy_manager.StrategySelector'), \
             patch('src.strategies.strategy_manager.ExecutionEngine'), \
             patch('src.strategies.strategy_manager.DynamicPairSelector'), \
             patch('src.strategies.strategy_manager.PerformanceTracker'):
             
             manager = StrategyManager(mock_config, mock_market_api)
             return manager
    
    def test_check_startup_exits_closes_tp_positions(self, strategy_manager):
        """Test that positions past TP are closed on startup."""
        # Position with TP already met
        position = Position(
            symbol='TAO',
            side='short',
            entry_price=240.0,
            size=1.5,
            entry_time=datetime.now(),
            strategy='test',
            take_profit=228.0
        )
        strategy_manager.execution_engine.positions = {'TAO': position}
        
        # Mock: current price is below TP (should trigger for short)
        strategy_manager.market_api.get_current_price = MagicMock(return_value=225.0)
        strategy_manager.close_position = MagicMock(return_value=True)
        
        strategy_manager.check_startup_exits()
        
        # Should have closed the position
        strategy_manager.close_position.assert_called_with('TAO', 'take_profit')
        assert position.current_price == 225.0
    
    def test_check_startup_exits_skips_healthy_positions(self, strategy_manager):
        """Test that positions not meeting exit conditions are not closed."""
        position = Position(
            symbol='BTC',
            side='long',
            entry_price=50000.0,
            size=0.1,
            entry_time=datetime.now(),
            strategy='test',
            take_profit=55000.0,
            stop_loss=48000.0
        )
        strategy_manager.execution_engine.positions = {'BTC': position}
        
        # Price is between SL and TP - no exit
        strategy_manager.market_api.get_current_price = MagicMock(return_value=52000.0)
        strategy_manager.close_position = MagicMock()
        
        strategy_manager.check_startup_exits()
        
        # Should NOT close
        strategy_manager.close_position.assert_not_called()
