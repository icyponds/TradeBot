import pytest
from unittest.mock import MagicMock, patch
from src.api.hyperliquid_api import HyperliquidAPI
import time

class TestHyperliquidAPI:

    @pytest.fixture
    def api_client(self, mock_config):
        """Creates an API client instance."""
        return HyperliquidAPI(mock_config)

    def test_initialization(self, api_client, mock_config):
        """Test API client initialization."""
        assert api_client.base_url == mock_config['api']['base_url']
        assert api_client.wallet_address == mock_config['api']['wallet_address']

    def test_get_ohlcv_retry_logic(self, api_client):
        """Test that get_ohlcv retries on failure."""
        symbol = "BTC"
        interval = "1h"
        
        # Mock the SDK Info client
        api_client.info = MagicMock()
        
        # Ensure it passes "is_valid_perp" check
        with patch.object(api_client, '_get_asset_info_for_symbol', return_value={'name': 'BTC', 'szDecimals': 2}):
            
            # Side effect: Fail twice, then succeed
            # Note: with_retry catches exceptions.
            api_client.info.candles_snapshot.side_effect = [
                Exception("Network Error 1"), 
                Exception("Network Error 2"), 
                [{"t": 123000, "o": 1, "h": 2, "l": 1, "c": 2, "v": 100}]
            ]
            
            # Patch sleep to speed up test
            with patch('time.sleep'): 
                result = api_client.get_ohlcv(symbol, interval, limit=1)
                
        assert api_client.info.candles_snapshot.call_count == 3
        assert result is not None
        assert len(result) == 1

    def test_get_ohlcv_exhausted_retries(self, api_client):
        """Test that get_ohlcv raises exception after exhausting retries."""
        # Note: The decorator raises the last exception if all retries fail.
        
        api_client.info = MagicMock()
        api_client.info.candles_snapshot.side_effect = Exception("Persistent Error")
        
        with patch.object(api_client, '_get_asset_info_for_symbol', return_value={'name': 'BTC'}):
            with patch('time.sleep'):
                with pytest.raises(Exception) as excinfo:
                    api_client.get_ohlcv("BTC", "1h", limit=1)
                assert "Persistent Error" in str(excinfo.value)
            
        # Default retries is 3 (initial + 3 retries? or 3 attempts total?)
        # @with_retry(max_attempts=3) usually means 3 total attempts.
        assert api_client.info.candles_snapshot.call_count == 3

    def test_check_health(self, api_client):
        """Test health check method via health monitor."""
        # Default should be unknown or healthy depending on init
        # We can force a check
        
        # Mock connection test
        with patch.object(api_client, 'test_connection', return_value=True):
             # Run a check manually or verify initial state
             # The monitor runs in a thread, but for unit test we can check state directly
             # or call _perform_health_check if accessible, but it's internal.
             
             # Let's test the public interface `is_healthy` which checks status
             api_client.health_monitor.status = api_client.health_monitor.status.HEALTHY
             assert api_client.health_monitor.is_healthy() is True
             
             api_client.health_monitor.status = api_client.health_monitor.status.UNHEALTHY
             assert api_client.health_monitor.is_healthy() is False
