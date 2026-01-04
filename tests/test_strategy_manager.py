import pytest
from unittest.mock import MagicMock, patch, ANY
from src.strategies.strategy_manager import StrategyManager
from src.models.trade import Position
from datetime import datetime

class TestStrategyManager:
    
    @pytest.fixture
    def strategy_manager(self, mock_config, mock_market_api):
        """Creates a StrategyManager instance with mocked dependencies."""
        # Ensure strategies key exists
        if 'strategies' not in mock_config:
            mock_config['strategies'] = {}
            
        # Add required strategy config keys if missing
        mock_config['strategies']['enabled'] = ['cross_sectional_momentum']
        mock_config['strategies']['ohlcv_limit'] = 100
        mock_config['trading']['position_monitoring_interval'] = 10
        mock_config['trading']['enable_stale_order_cleanup'] = True
        mock_config['trading']['position_sync_interval'] = 300
        mock_config['trading']['enable_position_validation'] = True
        mock_config['trading']['order_timeout_minutes'] = 5

        # Mock StrategySelector and ExecutionEngine to avoid complex dependencies
        with patch('src.strategies.strategy_manager.StrategySelector'), \
             patch('src.strategies.strategy_manager.ExecutionEngine'), \
             patch('src.strategies.strategy_manager.DynamicPairSelector'), \
             patch('src.strategies.strategy_manager.PerformanceTracker'):
             
             manager = StrategyManager(mock_config, mock_market_api)
             return manager

    def test_initialization(self, strategy_manager):
        """Test that StrategyManager initializes correctly."""
        assert strategy_manager.is_running is False
        assert isinstance(strategy_manager.strategies, dict)
        # Check if CSM strategy was initialized (legacy path simulation)
        # Note: In real app, it loads from settings.py, here we use mock_config
        # We need to verify if _initialize_strategies picked it up
        assert strategy_manager.market_api is not None

    def test_should_execute_signal_basic(self, strategy_manager):
        """Test basic signal execution logic."""
        symbol = "BTC"
        signal = {
            'signal': 'buy',
            'confidence': 0.9,
            'price': 50000
        }
        current_price = 50000
        ohlcv = {} # Mock OHLCV
        
        # Mock checks
        strategy_manager.max_positions_percentage = 100.0
        strategy_manager.execution_engine.positions = {} # No positions
        
        # Assuming _should_execute_signal returns True for valid signal in simple case
        # We need to mock internal checks called by _should_execute_signal 
        # (like _check_position_limit, etc.) if they are complex.
        
        # For now, let's test a direct method if possible or mock the heavy lifters
        with patch.object(strategy_manager, '_check_position_limit', return_value=False):
             result = strategy_manager._should_execute_with_position_limit(symbol, signal, 0.9)
             assert result is True

    def test_position_limit_check(self, strategy_manager):
        """Test position limit enforcement."""
        strategy_manager.max_positions_percentage = 50.0
        
        # Mock portfolio allocation return
        # Case 1: Under limit
        with patch.object(strategy_manager, '_check_portfolio_allocation', 
                          return_value={'allocation_percentage': 10.0, 'max_allocation': 50.0}):
            assert strategy_manager._check_position_limit() is False
            
        # Case 2: Over limit
        with patch.object(strategy_manager, '_check_portfolio_allocation', 
                          return_value={'allocation_percentage': 60.0, 'max_allocation': 50.0}):
            assert strategy_manager._check_position_limit() is True

    def test_monitor_and_close_positions_unbound_local_fix(self, strategy_manager):
        """Verify the fix for UnboundLocalError in _monitor_and_close_positions."""
        # Setup state
        strategy_manager.execution_engine.positions = {
            'BTC': Position(symbol='BTC', side='long', size=1.0, entry_price=50000, 
                           current_price=51000, entry_time=datetime.now(), strategy='test')
        }
        strategy_manager.total_positions_closed = 0
        strategy_manager.last_emergency_check = 0 # force check
        
        # Mock dependencies
        strategy_manager._should_close_position = MagicMock(return_value="take_profit")
        strategy_manager.close_position = MagicMock(return_value=True)
        strategy_manager._check_emergency_stop = MagicMock(return_value=False)
        
        # Run method
        strategy_manager._monitor_and_close_positions(
            emergency_portfolio_loss_pct=10.0,
            timestamp=datetime.now()
        )
        
        # Verify side effects
        assert strategy_manager.total_positions_closed == 1
        strategy_manager.close_position.assert_called_with('BTC', 'take_profit', timestamp=ANY)
