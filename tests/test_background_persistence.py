
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

    def test_seeded_bar_suppresses_callback(self, mock_api):
        """
        Test that completing a bar that was just seeded does NOT trigger a callback.
        """
        symbol = "SOL"
        timeframe = "1h"
        
        # 1. Seed with a bar ending at T=100
        # (Assuming timeframe=1h=3600s, T=3600 aligns)
        ts_bar = 3600
        bars = [{'time': ts_bar, 'open': 100, 'high': 100, 'low': 100, 'close': 100, 'volume': 10}]
        mock_api.ohlcv_cache.seed(symbol, timeframe, bars)
        
        # Verify seed key set
        assert mock_api.ohlcv_cache.last_seeded_keys[symbol][timeframe] == ts_bar
        
        # 2. Setup mock callback
        callback = MagicMock()
        mock_api.ohlcv_cache.on_bar_complete_callback = callback
        
        # 3. Process tick at T=3700 (next bar). 
        # This implies bar at T=3600 is complete.
        # It matches the seed key, so callback should be SKIPPED.
        # T=7201 -> Bar start 7200, so last bar was 3600.
        # update_from_tick logic:
        # key (7200) != dq[-1].time (3600) -> Boundary Crossed.
        # -> Call callback for dq[-1] (3600).
        # BUT 3600 == seed_key. Should SKIP.
        
        mock_api.ohlcv_cache.update_from_tick(symbol, 101, 1, 7201) 
        
        callback.assert_not_called()
        
        # 4. Now process tick for T=7300. 
        # Current bar is 7200. 
        # No boundary crossing yet.
        mock_api.ohlcv_cache.update_from_tick(symbol, 102, 1, 7300)
        callback.assert_not_called()
        
        # 5. Process tick for T=10801 (next bar starts 10800).
        # This completes bar 7200.
        # 7200 != seed_key (3600). Should CALL.
        mock_api.ohlcv_cache.update_from_tick(symbol, 103, 1, 10801)
        
        callback.assert_called_once()
        args = callback.call_args[0]
        assert args[0] == symbol
        assert args[1] == timeframe
        assert args[2]['time'] == 7200
