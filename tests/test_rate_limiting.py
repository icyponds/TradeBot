"""
Tests for rate limiting behavior in HyperliquidAPI.
"""

import pytest
from unittest.mock import MagicMock, patch


class TestRateLimiting:
    """Tests for rate limiting behavior."""

    @pytest.fixture
    def api_client(self, shared_api_client):
        """Use shared module-scoped client, reset mocks before each test."""
        shared_api_client.exchange.reset_mock()
        shared_api_client.info.reset_mock()
        return shared_api_client

    def test_append_current_candle_uses_rate_limiter(self, api_client):
        """Verify _append_current_candle routes through _rate_limited_call."""
        # Mock the rate limited call to return candle data
        mock_candles = [{'t': 123000, 'o': 1, 'h': 2, 'l': 1, 'c': 2, 'v': 100}]
        api_client._rate_limited_call = MagicMock(return_value=mock_candles)
        
        # Clear any existing cache
        api_client.ohlcv_cache.cache['BTC']['1h'] = MagicMock()
        api_client.ohlcv_cache.cache['BTC']['1h'].__bool__ = MagicMock(return_value=False)
        
        # Call the method
        api_client._append_current_candle("BTC", "1h", "BTC")
        
        # Verify _rate_limited_call was invoked (not direct SDK call)
        assert api_client._rate_limited_call.called, "_rate_limited_call should have been called"
        
    def test_executor_has_single_worker(self, api_client):
        """Verify persistence executor uses single worker to serialize requests."""
        # Check the executor's max_workers setting
        assert api_client._persistence_executor._max_workers == 1, \
            "Executor should have max_workers=1 to serialize API calls"
    
    def test_rate_limiter_burst_size(self, api_client):
        """Verify rate limiter has reduced burst size."""
        # Default burst size should be 10 (lowered from 50)
        assert api_client.rate_limiter.burst_size <= 20, \
            f"Burst size should be low to prevent 429s, got {api_client.rate_limiter.burst_size}"

    def test_spot_meta_uses_rate_limiter(self, api_client):
        """Verify get_spot_meta routes through _rate_limited_call."""
        # Setup mock
        api_client._rate_limited_call = MagicMock(return_value={})
        
        # Call method
        api_client.get_spot_meta()
        
        # Verify
        assert api_client._rate_limited_call.called, "get_spot_meta should use _rate_limited_call"

    def test_spot_meta_and_ctx_uses_rate_limiter(self, api_client):
        """Verify get_spot_meta_and_asset_ctxs routes through _rate_limited_call."""
        # Setup mock to return tuple as expected by method
        api_client._rate_limited_call = MagicMock(return_value=({}, []))
        
        # Call method
        api_client.get_spot_meta_and_asset_ctxs()
        
        # Verify
        assert api_client._rate_limited_call.called, "get_spot_meta_and_asset_ctxs should use _rate_limited_call"
