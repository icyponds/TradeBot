"""
Tests for order handling improvements:
- Dead man's switch functionality
- Cancel all orders on startup
- Heartbeat refresh
"""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime


class TestDeadMansSwitch:
    """Test dead man's switch functionality in HyperliquidAPI."""
    
    @pytest.fixture
    def mock_api(self):
        """Create a mocked HyperliquidAPI."""
        with patch('src.api.hyperliquid_api.HyperliquidAPI.__init__', return_value=None):
            from src.api.hyperliquid_api import HyperliquidAPI
            api = HyperliquidAPI.__new__(HyperliquidAPI)
            api.logger = MagicMock()
            api.exchange = MagicMock()
            api._rate_limited_call = MagicMock(return_value={'status': 'ok'})
            return api
    
    def test_set_dead_mans_switch_success(self, mock_api):
        """Test setting dead man's switch with valid timeout."""
        from src.api.hyperliquid_api import HyperliquidAPI
        # Bind the method
        mock_api.set_dead_mans_switch = HyperliquidAPI.set_dead_mans_switch.__get__(mock_api, HyperliquidAPI)
        
        result = mock_api.set_dead_mans_switch(30)
        
        assert result is True
        mock_api._rate_limited_call.assert_called_once()
        mock_api.logger.info.assert_called()
    
    def test_set_dead_mans_switch_disable(self, mock_api):
        """Test disabling dead man's switch with timeout=0."""
        from src.api.hyperliquid_api import HyperliquidAPI
        mock_api.set_dead_mans_switch = HyperliquidAPI.set_dead_mans_switch.__get__(mock_api, HyperliquidAPI)
        
        result = mock_api.set_dead_mans_switch(0)
        
        assert result is True
        # Action should have time=None for disable
        call_args = mock_api._rate_limited_call.call_args
        action = call_args[0][2]  # Third positional arg is the action
        assert action['time'] is None
    
    def test_set_dead_mans_switch_no_exchange(self, mock_api):
        """Test dead man's switch fails gracefully without exchange."""
        from src.api.hyperliquid_api import HyperliquidAPI
        mock_api.exchange = None
        mock_api.set_dead_mans_switch = HyperliquidAPI.set_dead_mans_switch.__get__(mock_api, HyperliquidAPI)
        
        result = mock_api.set_dead_mans_switch(30)
        
        assert result is False
        mock_api.logger.error.assert_called()


class TestCancelAllOnStartup:
    """Test cancel all orders on startup in StrategyManager."""
    
    @pytest.fixture
    def mock_manager(self, mock_config, mock_market_api):
        """Create a mocked StrategyManager."""
        from src.strategies.strategy_manager import StrategyManager
        
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
            manager.market_api = mock_market_api
            return manager
    
    def test_startup_cancels_all_orders(self, mock_manager):
        """Test that start() calls cancel_all_orders before sync."""
        mock_manager.market_api.cancel_all_orders = MagicMock(return_value=3)
        mock_manager._check_startup_orphans = MagicMock()
        mock_manager.sync_positions_with_exchange = MagicMock()
        mock_manager.check_startup_exits = MagicMock()
        mock_manager._run_trading_loop = MagicMock()
        mock_manager.portfolio_manager = MagicMock()
        mock_manager.portfolio_manager.calculate_available_capital_for_trading.return_value = 10000.0
        mock_manager.leverage_manager = MagicMock()
        mock_manager.performance_tracker = MagicMock()
        
        mock_manager.start()
        
        # Verify cancel_all_orders was called
        mock_manager.market_api.cancel_all_orders.assert_called_once()
    
    def test_startup_sets_dead_mans_switch(self, mock_manager):
        """Test that start() enables dead man's switch."""
        mock_manager.market_api.cancel_all_orders = MagicMock(return_value=0)
        mock_manager.market_api.set_dead_mans_switch = MagicMock(return_value=True)
        mock_manager._check_startup_orphans = MagicMock()
        mock_manager.sync_positions_with_exchange = MagicMock()
        mock_manager.check_startup_exits = MagicMock()
        mock_manager._run_trading_loop = MagicMock()
        mock_manager.portfolio_manager = MagicMock()
        mock_manager.portfolio_manager.calculate_available_capital_for_trading.return_value = 10000.0
        mock_manager.leverage_manager = MagicMock()
        mock_manager.performance_tracker = MagicMock()
        
        mock_manager.start()
        
        # Verify dead man's switch was set with 30s timeout
        mock_manager.market_api.set_dead_mans_switch.assert_called_once_with(30)


class TestHeartbeatRefresh:
    """Test heartbeat refresh in trading loop."""
    
    @pytest.fixture
    def mock_manager(self, mock_config, mock_market_api):
        """Create a mocked StrategyManager."""
        from src.strategies.strategy_manager import StrategyManager
        
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
            manager.market_api = mock_market_api
            return manager
    
    def test_heartbeat_respects_interval(self, mock_manager):
        """Test that heartbeat only refreshes every 15 seconds."""
        import time
        mock_manager.market_api.refresh_dead_mans_switch = MagicMock(return_value=True)
        
        # First call should refresh (no last_heartbeat_refresh set)
        mock_manager._refresh_dead_mans_switch_periodic()
        assert mock_manager.market_api.refresh_dead_mans_switch.call_count == 1
        
        # Immediate second call should NOT refresh (within 15s)
        mock_manager._refresh_dead_mans_switch_periodic()
        assert mock_manager.market_api.refresh_dead_mans_switch.call_count == 1
        
        # Simulate 16 seconds passing
        mock_manager.last_heartbeat_refresh = time.time() - 16
        mock_manager._refresh_dead_mans_switch_periodic()
        assert mock_manager.market_api.refresh_dead_mans_switch.call_count == 2
    
    def test_stop_disables_dead_mans_switch(self, mock_manager):
        """Test that stop() disables dead man's switch."""
        mock_manager.is_running = True
        mock_manager.market_api.disable_dead_mans_switch = MagicMock(return_value=True)
        mock_manager.market_api.stop = MagicMock()
        
        mock_manager.stop()
        
        mock_manager.market_api.disable_dead_mans_switch.assert_called_once()


class TestSlippageConfig:
    """Test slippage configuration."""
    
    def test_initial_slippage_is_50_bps(self):
        """Verify initial slippage is configured to 50 bps."""
        from src.api.hyperliquid_api import HyperliquidAPI
        assert HyperliquidAPI._INITIAL_SLIPPAGE_BPS == 50
