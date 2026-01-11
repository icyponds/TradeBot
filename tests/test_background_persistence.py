
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
        
        # Subscribe
        mock_api.subscribe_symbol(symbol)
        
        # Should have initialized 5m, 15m, 1h, 4h, 1d
        expected_tfs = ['5m', '15m', '1h', '4h', '1d']
        cache = mock_api.ohlcv_cache.cache[symbol]
        
        for tf in expected_tfs:
            assert tf in cache, f"Timeframe {tf} was not initialized upon subscription"
            
    def test_tick_updates_background_timeframes(self, mock_api):
        """
        Test that ticks update the initialized background timeframes.
        """
        symbol = "ETH"
        
        # Manually initialize for now if subscribe fix isn't applied yet
        # (This test validates the end-to-end flow assuming init works)
        # But for TDD, we want this to fail if subscribe doesn't work.
        mock_api.subscribe_symbol(symbol)
        
        # Simulate a tick
        price = 2000.0
        mock_api.update_ohlcv_from_tick(symbol, price=price, volume=1.0, ts=time.time())
        
        # Check 5m cache
        cache_5m = mock_api.ohlcv_cache.get(symbol, '5m')
        assert cache_5m is not None, "5m cache should exist"
        assert len(cache_5m) > 0, "5m cache should have received the tick data"
        assert cache_5m[-1]['close'] == price
