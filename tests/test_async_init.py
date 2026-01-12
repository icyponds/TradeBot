
import pytest
from unittest.mock import MagicMock, patch
import time
from concurrent.futures import Future

# Import the class under test
# Assuming it's in src/api/hyperliquid_api.py
# We might need to mock imports if they have side effects
from src.api.hyperliquid_api import HyperliquidAPI

class TestAsyncInit:
    
    @pytest.fixture
    def api_client(self):
        """Create a stripped-down API client with mocked internals."""
        # Mock dependencies to avoid real network calls or extensive init
        config = {
             'api': {
                 'base_url': 'https://api.hyperliquid.xyz',
                 'private_key': '00'*32,
                 'wallet_address': '0x'*20,
                 'rate_limit': {},
                 'circuit_breaker': {},
                 'cache': {},
                 'health_monitor': {}
             },
             'hip3': {'enabled': False}
        }
        
        # Patch init to avoid SDK client creation
        with patch('src.api.hyperliquid_api.HyperliquidAPI._init_sdk_clients'):
            client = HyperliquidAPI(config)
            
            # Additional mocks needed for the async logic
            client._persistence_executor = MagicMock()
            client._initialize_live_data = MagicMock(return_value=True)
            client._get_asset_info_for_symbol = MagicMock(return_value={'name': 'BTC'})
            client.get_spot_api_name = MagicMock(return_value=None)
            client.ohlcv_cache = MagicMock()
            
            return client

    def test_subscribe_symbol_is_async(self, api_client):
        """Test that subscribe_symbol submits task and returns immediately."""
        symbol = "BTC"
        
        # Act
        api_client.subscribe_symbol(symbol)
        
        # Assert
        # 1. State should be pending initially (until worker runs)
        assert symbol in api_client._pending_init_symbols
        assert symbol not in api_client._subscribed_symbols
        assert symbol not in api_client._initializing_symbols # Added by worker, not main thread
        
        # 2. Executor should receive submission
        api_client._persistence_executor.submit.assert_called_once()
        args, _ = api_client._persistence_executor.submit.call_args
        assert args[0] == api_client._async_init_worker
        assert args[1] == symbol

    def test_async_worker_success_flow(self, api_client):
        """Test the lifecycle of a successful async init."""
        symbol = "ETH"
        api_symbol = "ETH"
        
        # Setup: Symbol is pending
        api_client._pending_init_symbols.add(symbol)
        
        # Mock successful fetch
        api_client._initialize_live_data.return_value = True
        
        # Act: Run worker directly (bypass executor for test)
        api_client._async_init_worker(symbol, api_symbol)
        
        # Assert
        # 1. Should call init
        api_client._initialize_live_data.assert_called_with(symbol, api_symbol)
        
        # 2. Should NOT be in initializing set (cleaned up in finally)
        assert symbol not in api_client._initializing_symbols
        
        # Note: _initialize_live_data handles removing from pending and adding to subscribed
        # But since we MOCKED _initialize_live_data, that side effect won't happen unless we define it.
        # However, _async_init_worker itself doesn't update the sets on success, 
        # it relies on _initialize_live_data doing it (lines 1800-1803 in implementation).
        # Wait, my implementation of _async_init_worker ONLY calls _initialize_live_data.
        # It relies on _initialize_live_data to call _finalize_subscription.
        # Let's verify _async_init_worker LOGIC is safe.
        
    def test_async_worker_failure_flow(self, api_client):
        """Test behavior when async init fails (exception or return False)."""
        symbol = "FAIL_COIN"
        
        # Setup
        # api_client._pending_init_symbols.add(symbol) # Worker assumes passed args
        
        # Mock failure (raise exception)
        api_client._initialize_live_data.side_effect = Exception("API Error")
        
        # Act
        api_client._async_init_worker(symbol, symbol)
        
        # Assert
        # 1. Should catch exception
        # 2. Should add back to pending (if it wasn't there, or keep it)
        assert symbol in api_client._pending_init_symbols
        # 3. Should clear initializing
        assert symbol not in api_client._initializing_symbols

    def test_retry_offload_to_executor(self, api_client):
        """Test that retry_pending_subscriptions uses executor."""
        # Setup
        api_client._pending_init_symbols = {"SOL", "AVAX"}
        api_client._initializing_symbols = set() # None currently running
        
        # Act
        api_client.retry_pending_subscriptions()
        
        # Assert
        # Should submit 2 tasks
        assert api_client._persistence_executor.submit.call_count == 2
