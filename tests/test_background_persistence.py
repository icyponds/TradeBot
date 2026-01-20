
import pytest
from unittest.mock import MagicMock, call
from collections import deque
import time

from src.api.hyperliquid_api import HyperliquidAPI

class TestBackgroundPersistence:
    """Test suite for verifying background asset persistence."""

    @pytest.fixture
    def mock_api(self):
        """Create a HyperliquidAPI with mocked dependencies."""
        mock_config = {
            'exchange': {'account_address': '0x123', 'is_testnet': False},
            'api': {'base_url': 'https://api.hyperliquid.xyz', 'private_key': '0x123', 'wallet_address': '0x123'},
            'api_latency_test': False,
            'rate_limit': {'max_requests': 1200, 'time_window': 60}
        }
        api = HyperliquidAPI(mock_config)
        api.info = MagicMock()
        api.exchange = MagicMock()
        api.market_db = MagicMock()
        api._persistence_executor = MagicMock()
        
        # Ensure cache is fresh
        from collections import defaultdict, deque
        api.ohlcv_cache.cache = defaultdict(lambda: defaultdict(lambda: deque()))
        
        return api

    def test_ensure_timeframe_updates_maxlen(self, mock_api):
        """
        Verify that ensure_timeframe correctly updates deque maxlen.
        """
        symbol = "BTC"
        timeframe = "5m"
        
        # Seed with initial maxlen
        mock_api.ohlcv_cache.seed(symbol, timeframe, [], maxlen=300)
        d = mock_api.ohlcv_cache.cache[symbol][timeframe]
        assert d.maxlen == 300
        
        # Update maxlen
        mock_api.ohlcv_cache.ensure_timeframe(symbol, timeframe, 1000)
        
        # Verify it updated
        d_new = mock_api.ohlcv_cache.cache[symbol][timeframe]
        assert d_new.maxlen == 1000
        assert mock_api.ohlcv_cache.maxlen[symbol][timeframe] == 1000

    def test_subscribe_init_timeframes(self, mock_api):
        """
        Test that subscribe_symbol initializes standard timeframes.
        """
        symbol = "BTC"
        
        # Initially empty
        assert len(mock_api.ohlcv_cache.cache[symbol]) == 0
        
        # Mock candles snapshot to return empty list (for _initialize_live_data)
        mock_api.info.candles_snapshot.return_value = []
        
        # Subscribe with NO explicit timeframes -> should default to empty
        mock_api.subscribe_symbol(symbol)
        
        # SIMULATE ASYNC WORKER:
        mock_api._persistence_executor.submit.assert_called()
        args, _ = mock_api._persistence_executor.submit.call_args
        worker_func = args[0]
        worker_args = args[1:]
        
        # Run synchronous
        worker_func(*worker_args)
        
        # Should have initialized NOTHING (strictly opt-in)
        cache = mock_api.ohlcv_cache.cache[symbol]
        assert len(cache) == 0, f"Should verify no persistence by default. Got: {list(cache.keys())}"
            
    def test_tick_updates_background_timeframes(self, mock_api):
        """
        Test that ticks update the initialized background timeframes.
        """
        symbol = "ETH"
        
        # Mock candles snapshot
        mock_api.info.candles_snapshot.return_value = []
        
        # Subscribe with explicit timeframe
        explicit_tfs = ['15m']
        mock_api.subscribe_symbol(symbol, required_timeframes=explicit_tfs)
        args, _ = mock_api._persistence_executor.submit.call_args
        args[0](*args[1:]) # Run worker
        
        # Simulate a tick
        price = 2000.0
        mock_api.update_ohlcv_from_tick(symbol, price=price, volume=1.0, ts=time.time())
        
        # Check explicit cache exists
        cache_15m = mock_api.ohlcv_cache.get(symbol, '15m')
        assert cache_15m is not None, "15m cache should exist"
        assert len(cache_15m) > 0, "15m cache should have received the tick data"
        assert cache_15m[-1]['close'] == price
