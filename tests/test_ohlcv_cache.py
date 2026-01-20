"""Tests for OhlcvCache and data management functionality."""
import pytest
import time
from unittest.mock import MagicMock, patch
from collections import deque

from src.api.hyperliquid_api import OhlcvCache


class TestOhlcvCache:
    """Tests for OhlcvCache class."""
    
    @pytest.fixture
    def cache(self):
        """Create a fresh OhlcvCache instance."""
        return OhlcvCache()
    
    def test_seed_creates_cache(self, cache):
        """Test that seed creates a cache for a symbol/timeframe."""
        bars = [
            {'time': 1000, 'open': 100, 'high': 110, 'low': 90, 'close': 105, 'volume': 1000},
            {'time': 1060, 'open': 105, 'high': 115, 'low': 95, 'close': 110, 'volume': 1100},
        ]
        cache.seed('BTC', '1m', bars)
        
        result = cache.get('BTC', '1m')
        assert result is not None
        assert len(result) == 2
        assert result[0]['time'] == 1000
        assert result[1]['close'] == 110
    
    def test_get_bar_key_floors_correctly(self, cache):
        """Test that _get_bar_key floors timestamps to timeframe boundaries."""
        # 1m = 60 seconds
        assert cache._get_bar_key(125, '1m') == 120  # Floor to 120
        assert cache._get_bar_key(179, '1m') == 120
        assert cache._get_bar_key(180, '1m') == 180
        
        # 5m = 300 seconds
        assert cache._get_bar_key(350, '5m') == 300
        assert cache._get_bar_key(600, '5m') == 600
        
        # 1h = 3600 seconds
        assert cache._get_bar_key(3700, '1h') == 3600
    
    def test_update_from_tick_creates_new_bar_on_boundary(self, cache):
        """Test that update_from_tick creates a new bar when timeframe boundary is crossed."""
        # Seed initial data
        bars = [{'time': 0, 'open': 100, 'high': 110, 'low': 90, 'close': 105, 'volume': 1000}]
        cache.seed('BTC', '1m', bars)
        
        # Update with tick in SAME minute (should update existing bar)
        cache.update_from_tick('BTC', 107, 50, 30)  # ts=30 -> bar_key=0
        result = cache.get('BTC', '1m')
        assert len(result) == 1
        assert result[-1]['close'] == 107
        
        # Update with tick in NEXT minute (should create new bar)
        cache.update_from_tick('BTC', 115, 60, 90)  # ts=90 -> bar_key=60
        result = cache.get('BTC', '1m')
        assert len(result) == 2
        assert result[-1]['time'] == 60
        assert result[-1]['open'] == 115  # First tick becomes open
        assert result[-1]['close'] == 115
    
    def test_boundary_callback_fired_on_new_bar(self, cache):
        """Test that on_bar_complete_callback is fired when a new bar is created."""
        callback_calls = []
        
        def callback(symbol, timeframe, bar):
            callback_calls.append({'symbol': symbol, 'timeframe': timeframe, 'bar': bar.copy()})
        
        cache.on_bar_complete_callback = callback
        
        # Seed initial data
        bars = [{'time': 0, 'open': 100, 'high': 110, 'low': 90, 'close': 105, 'volume': 1000}]
        cache.seed('BTC', '1m', bars)
        
        # Update with tick that triggers new bar at T=60 (completing T=0)
        # Sinc T=0 was seeded, its callback should be SUPPRESSED.
        cache.update_from_tick('BTC', 115, 60, 90)  # ts=90 -> bar_key=60
        assert len(callback_calls) == 0
        
        # Now update with tick that triggers ANOTHER new bar at T=120 (completing T=60)
        # T=60 was live-created (not seeded), so callback SHOULD fire.
        cache.update_from_tick('BTC', 120, 60, 150) # ts=150 -> bar_key=120
        
        # Callback should have been called with the COMPLETED bar (time=60)
        assert len(callback_calls) == 1
        assert callback_calls[0]['symbol'] == 'BTC'
        assert callback_calls[0]['timeframe'] == '1m'
        assert callback_calls[0]['bar']['time'] == 60
    

    def test_reseeding_does_not_suppress_live_bar(self, cache):
        """
        Regression test for Phase 15:
        Verify that re-seeding the cache (e.g. by Repairer or PairSelector)
        does NOT update the 'last_seeded_keys' suppression timestamp if it was already set.
        This prevents background fetches from blocking live callbacks.
        """
        callback_calls = []
        def on_bar(symbol, timeframe, bar):
            callback_calls.append({'symbol': symbol, 'timeframe': timeframe, 'bar': bar})
        
        cache.on_bar_complete_callback = on_bar
        
        # 1. First Seed (Startup) -> Supression Timestamp Set
        # Seed T=0..60. last_seeded_key SHOULD be 60.
        bars_1 = [
            {'time': 0, 'open': 100, 'high': 100, 'low': 100, 'close': 100, 'volume': 10},
            {'time': 60, 'open': 100, 'high': 100, 'low': 100, 'close': 100, 'volume': 10}
        ]
        cache.seed('BTC', '1m', bars_1)
        assert cache.last_seeded_keys['BTC']['1m'] == 60
        
        # 2. Tick T=70 implies T=60 completed? 
        # Wait, if T=60 is last in deque, key=60.
        # Tick T=70 -> key=60. No change.
        # Tick T=120 -> key=120. Boundary cross 60.
        # Completed Bar T=60. Matches seed key 60. SUPPRESSED.
        
        # 3. Simulate Repairer fetching T=0..120 and calling seed() AGAIN
        # This happens before T=120 completes live.
        bars_2 = [
            {'time': 0, 'open': 100, 'high': 100, 'low': 100, 'close': 100, 'volume': 10},
            {'time': 60, 'open': 100, 'high': 100, 'low': 100, 'close': 100, 'volume': 10},
            {'time': 120, 'open': 101, 'high': 101, 'low': 101, 'close': 101, 'volume': 10}
        ]
        cache.seed('BTC', '1m', bars_2)
        
        # CRITICAL ASSERTION: The bug was that this updated seed key to 120.
        # The fix should keep it as 60.
        assert cache.last_seeded_keys['BTC']['1m'] == 60
        
        # 4. Now process live ticks to complete T=120
        # Tick T=180 -> key=180. Boundary cross 120.
        # Completed bar T=120.
        # Should compare 120 vs seed_key (60).
        # 120 != 60. Callback SHOULD FIRE.
        
        # Note: update_from_tick expects creation_time.
        # If we send tick at T=180, it completes T=120.
        cache.update_from_tick('BTC', 102, 10, 180)
        
        assert len(callback_calls) == 1
        assert callback_calls[0]['bar']['time'] == 120

    def test_callback_exception_does_not_break_tick_processing(self, cache):

        """Test that callback exceptions don't break tick processing."""
        def bad_callback(symbol, timeframe, bar):
            raise Exception("Callback error!")
        
        cache.on_bar_complete_callback = bad_callback
        
        # Seed initial data
        bars = [{'time': 0, 'open': 100, 'high': 110, 'low': 90, 'close': 105, 'volume': 1000}]
        cache.seed('BTC', '1m', bars)
        
        # This should NOT raise an exception despite callback failure
        cache.update_from_tick('BTC', 115, 60, 90)
        
        # New bar should still be created
        result = cache.get('BTC', '1m')
        assert len(result) == 2
        assert result[-1]['time'] == 60
    
    def test_update_bar_updates_ohlcv_correctly(self, cache):
        """Test that tick updates correctly modify OHLCV values."""
        # Seed initial data
        bars = [{'time': 0, 'open': 100, 'high': 100, 'low': 100, 'close': 100, 'volume': 0}]
        cache.seed('BTC', '1m', bars)
        
        # Update with higher price -> should update high
        cache.update_from_tick('BTC', 110, 50, 30)
        result = cache.get('BTC', '1m')
        assert result[-1]['high'] == 110
        assert result[-1]['close'] == 110
        
        # Update with lower price -> should update low
        cache.update_from_tick('BTC', 95, 50, 45)
        result = cache.get('BTC', '1m')
        assert result[-1]['low'] == 95
        assert result[-1]['close'] == 95
        
        # Volume should accumulate
        assert result[-1]['volume'] == 100


class TestHyperliquidAPIDataMethods:
    """Tests for HyperliquidAPI data management methods."""
    
    @pytest.fixture
    def api_client(self, shared_api_client):
        """Reuses the module-scoped shared client."""
        return shared_api_client
    
    def test_pending_init_symbols_initialized(self, api_client):
        """Test that _pending_init_symbols is initialized as empty set."""
        assert hasattr(api_client, '_pending_init_symbols')
        assert isinstance(api_client._pending_init_symbols, set)
        assert len(api_client._pending_init_symbols) == 0
    
    def test_on_bar_complete_callback_wired(self, api_client):
        """Test that OhlcvCache callback is wired to HyperliquidAPI."""
        assert api_client.ohlcv_cache.on_bar_complete_callback is not None
        assert api_client.ohlcv_cache.on_bar_complete_callback == api_client._on_bar_complete
    
    def test_on_bar_complete_persists_to_db(self, api_client):
        """Test that _on_bar_complete submits task to persistence executor."""
        # Setup mock database and executor
        api_client.market_db = MagicMock()
        api_client._persistence_executor = MagicMock()
        
        bar = {'time': 1000, 'open': 100, 'high': 110, 'low': 90, 'close': 105, 'volume': 1000}
        
        # Call the callback
        api_client._on_bar_complete('BTC', '1h', bar)
        
        # Verify executor submit was called
        api_client._persistence_executor.submit.assert_called_once()
        
        # Verify DB insert was NOT called directly (it's handled by worker)
        api_client.market_db.insert_market_data.assert_not_called()
    
    def test_on_bar_complete_skips_if_no_db(self, api_client):
        """Test that _on_bar_complete does nothing if market_db is None."""
        api_client.market_db = None
        
        bar = {'time': 1000, 'open': 100, 'high': 110, 'low': 90, 'close': 105, 'volume': 1000}
        
        # Should not raise any exception
        api_client._on_bar_complete('BTC', '1h', bar)
    
    def test_get_interval_ms(self, api_client):
        """Test _get_interval_ms returns correct values."""
        assert api_client._get_interval_ms('1m') == 60 * 1000
        assert api_client._get_interval_ms('5m') == 5 * 60 * 1000
        assert api_client._get_interval_ms('15m') == 15 * 60 * 1000
        assert api_client._get_interval_ms('1h') == 60 * 60 * 1000
        assert api_client._get_interval_ms('4h') == 4 * 60 * 60 * 1000
        assert api_client._get_interval_ms('1d') == 24 * 60 * 60 * 1000
        # Unknown timeframe defaults to 1h
        assert api_client._get_interval_ms('unknown') == 60 * 60 * 1000
    
    def test_retry_pending_subscriptions_empty_queue(self, api_client):
        """Test that retry_pending_subscriptions does nothing when queue is empty."""
        api_client._pending_init_symbols = set()
        
        # Should not raise and should not log
        api_client.retry_pending_subscriptions()
