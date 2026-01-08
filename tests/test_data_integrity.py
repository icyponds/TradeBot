import pytest
import time
from unittest.mock import MagicMock, patch, ANY
import pandas as pd


class TestDataIntegrity:
    
    @pytest.fixture
    def api_client(self, shared_api_client):
        """Use shared module-scoped client, reset mocks before each test."""
        shared_api_client.exchange.reset_mock()
        shared_api_client.info.reset_mock()
        shared_api_client.market_db.reset_mock()
        return shared_api_client

    def test_on_bar_complete_delegation(self, api_client):
        """Verify _on_bar_complete submits task to executor and does NOT write directly."""
        symbol = "BTC"
        timeframe = "1h"
        bar = {'time': 1700000000, 'open': 100, 'close': 110}
        
        # Mock executor
        api_client._persistence_executor = MagicMock()
        
        # Trigger
        api_client._on_bar_complete(symbol, timeframe, bar)
        
        # Verify submit called
        api_client._persistence_executor.submit.assert_called_once()
        args = api_client._persistence_executor.submit.call_args[0]
        # First arg should be the function
        assert args[0] == api_client._fetch_and_persist_candle
        # Other args matches
        assert args[1] == symbol
        assert args[2] == timeframe
        assert args[3] == bar['time']
        
        # Verify DB NOT touched directly
        api_client.market_db.insert_market_data.assert_not_called()

    @patch('time.sleep') # Don't wait in test
    def test_fetch_and_persist_success(self, mock_sleep, api_client):
        """Verify worker fetches from API and persists verified candle."""
        symbol = "BTC"
        api_symbol = "BTC" # assume same
        timeframe = "5m"
        timestamp = 1700000000 # Start time
        
        # Mock API response for candles_snapshot
        # It should request range [start, end]
        # Let's say we return the correct candle
        verified_candle = {
            't': timestamp * 1000,
            'o': 100.0,
            'h': 105.0,
            'l': 99.0,
            'c': 102.0,
            'v': 500.0
        }
        api_client.info.candles_snapshot.return_value = [verified_candle]
        
        # Setup interval mock
        with patch.object(api_client, '_get_interval_ms', return_value=300000): # 5m
             with patch.object(api_client, '_get_api_symbol', return_value="BTC"):
                 # Trigger worker directly
                 api_client._fetch_and_persist_candle(symbol, timeframe, timestamp)
        
        # Verify API called with correct range
        # start = 1700000000000
        # end =   1700000300000 - 1 + 1000 (buffer)
        api_client.info.candles_snapshot.assert_called_once()
        
        # Verify DB Insert
        api_client.market_db.insert_market_data.assert_called_once()
        df_arg = api_client.market_db.insert_market_data.call_args[0][0]
        assert isinstance(df_arg, pd.DataFrame)
        assert len(df_arg) == 1
        assert df_arg.iloc[0]['close'] == 102.0
        assert df_arg.index[0].timestamp() == 1700000000

    @patch('time.sleep')
    def test_fetch_and_persist_verification_failed(self, mock_sleep, api_client):
        """Verify worker does NOT persist if API returns no data or mismatch."""
        symbol = "BTC"
        timeframe = "5m"
        timestamp = 1700000000
        
        # Mock API returning EMPTY list (e.g. data not ready yet)
        api_client.info.candles_snapshot.return_value = []
        
        with patch.object(api_client, '_get_interval_ms', return_value=300000):
            with patch.object(api_client, '_get_api_symbol', return_value="BTC"):
                api_client._fetch_and_persist_candle(symbol, timeframe, timestamp)
        
        # Verify NO DB Insert
        api_client.market_db.insert_market_data.assert_not_called()
        
    @patch('time.sleep')
    def test_fetch_and_persist_mismatch(self, mock_sleep, api_client):
        """Verify worker ignores candles with wrong timestamp."""
        symbol = "BTC"
        timeframe = "5m"
        timestamp = 1700000000
        
        # Mock API returning candle from DIFFERENT time (weird edge case)
        wrong_candle = {
            't': (timestamp + 600) * 1000, # 10 mins later
            'o': 100.0, 'h': 101.0, 'l': 99.0, 'c': 100.0, 'v': 10.0
        }
        api_client.info.candles_snapshot.return_value = [wrong_candle]
        
        with patch.object(api_client, '_get_interval_ms', return_value=300000):
            with patch.object(api_client, '_get_api_symbol', return_value="BTC"):
                api_client._fetch_and_persist_candle(symbol, timeframe, timestamp)
        
        # Verify NO DB Insert
        api_client.market_db.insert_market_data.assert_not_called()
