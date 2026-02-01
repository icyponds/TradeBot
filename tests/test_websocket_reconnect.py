
import pytest
from unittest.mock import MagicMock, patch
import threading
import time
from src.api.hyperliquid_api import HyperliquidAPI
from src.strategies.strategy_manager import StrategyManager

@pytest.fixture
def mock_api():
    config = {
        'wallet': {'address': '0x123', 'private_key': '0xabc'},
        'api': {
            'base_url': 'https://api.hyperliquid.xyz',
            'private_key': '0xabc',
            'wallet_address': '0x123'
        },
        'trading': {'position_monitoring_interval': 1},
        'risk_management': {'margin_buffer_percentage': 0.05} # Added required key
    }
    # Patch the classes where they are DEFINED, since they are lazy imported
    with patch('hyperliquid.exchange.Exchange'), \
         patch('hyperliquid.info.Info'):
        api = HyperliquidAPI(config)
        api.health_monitor = MagicMock()
        return api

def test_check_connection_status_healthy(mock_api):
    """Test that check_connection_status returns True when healthy."""
    mock_api.health_monitor.check_and_request_reconnect.return_value = False
    
    result = mock_api.check_connection_status()
    
    assert result is True
    assert not hasattr(mock_api, '_reconnect_thread') or not mock_api._reconnect_thread.is_alive()

def test_check_connection_status_triggers_thread(mock_api):
    """Test that it spawns a thread when reconnection is requested."""
    mock_api.health_monitor.check_and_request_reconnect.return_value = True
    
    # Mock attempt_ws_reconnect to verify it was called
    mock_api.attempt_ws_reconnect = MagicMock()
    
    # Mock the thread start so we don't actually spawn threads but verify the intent
    with patch('threading.Thread') as MockThread:
        mock_thread_instance = MockThread.return_value
        
        result = mock_api.check_connection_status()
        
        assert result is True
        MockThread.assert_called_once()
        # Verify daemon=True
        call_kwargs = MockThread.call_args[1]
        assert call_kwargs.get('daemon') is True
        assert call_kwargs.get('name') == "BackgroundWSReconnect"
        
        mock_thread_instance.start.assert_called_once()

def test_reconnect_thread_is_non_blocking_simulation():
    """
    Simulate the non-blocking behavior using Events for reliability.
    Verification that the 'main loop' continues while 'reconnect' is blocked.
    """
    event = threading.Event()
    thread_started = threading.Event()
    
    def blocked_reconnect():
        thread_started.set()
        event.wait(timeout=5.0) # Block until main thread releases
        
    # Start thread
    t = threading.Thread(target=blocked_reconnect)
    t.start()
    
    # Wait for thread to actually start running
    thread_started.wait(timeout=1.0)
    
    # Verification: Main thread is running and can execute code while t is blocked
    # If spawn was blocking, we wouldn't reach here until event set (which handles timeout)
    assert t.is_alive()
    
    # Cleanup
    event.set()
    t.join(timeout=1.0)
    assert not t.is_alive()

@patch('src.strategies.strategy_manager.StrategyManager.run_trading_cycle')
@patch('src.strategies.strategy_manager.StrategyManager._reconcile_strategies_periodic')
@patch('src.strategies.strategy_manager.StrategyManager._update_account_balance_periodic')
@patch('src.strategies.strategy_manager.StrategyManager._sync_positions_periodic')
@patch('src.strategies.strategy_manager.StrategyManager._refresh_dead_mans_switch_periodic')
def test_strategy_manager_calls_check(mock_refresh, mock_sync, mock_update, mock_reconcile, mock_cycle):
    """Verify StrategyManager calls the check in its loop."""
    config = {
        'api': {
            'base_url': 'https://api.hyperliquid.xyz',
            'private_key': '0xabc',
            'wallet_address': '0x123'
        },
        'wallet': {'address': '0x123', 'private_key': '0xabc'},
        'trading': {
            'position_monitoring_interval': 0.1, 
            'max_pairs_to_trade': 1, 
            'max_positions_percentage': 0.5, 
            'base_currency': 'USDC', 
            'order_timeout_minutes': 5,
            'enable_stale_order_cleanup': True,
            'position_sync_interval': 10,
            'enable_position_validation': True
        },
        'system': {'close_on_shutdown': False},
        'risk_management': {'margin_buffer_percentage': 0.05},
        'strategies': {'instances': [], 'ohlcv_limit': 100} # Added required key
    }
    
    # Patch PerformanceTracker instead of TradeDatabase
    with patch('src.strategies.strategy_manager.HyperliquidAPI') as MockAPI, \
         patch('src.strategies.strategy_manager.PortfolioManager'), \
         patch('src.strategies.strategy_manager.PerformanceTracker'), \
         patch('src.strategies.strategy_manager.DynamicPairSelector'), \
         patch('src.strategies.strategy_manager.LeverageManager'): # Added mocks
             
        manager = StrategyManager(config)
        # Mock strategy selector
        manager.strategy_selector = MagicMock()
        
        manager.market_api = MockAPI.return_value # Ensure we use the mock
        
        # Setup manager to run loop ONCE then stop
        manager.is_running = True
        
        # Raise exception to break loop after one iteration
        # This is a common pattern to test "one loop iteration"
        mock_cycle.side_effect = KeyboardInterrupt()
        
        manager._run_trading_loop()
        
        # Verify check_connection_status was called
        manager.market_api.check_connection_status.assert_called()
