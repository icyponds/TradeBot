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

    def test_on_bar_complete_optimistic_delegation(self, api_client):
        """Verify _on_bar_complete submits task to executor for optimistic persistence."""
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
        assert args[0] == api_client._persist_optimistic_candle
        # Other args matches
        assert args[1] == symbol
        assert args[2] == timeframe
        assert args[3] == bar
        
    def test_persist_optimistic_candle_success(self, api_client):
        """Verify _persist_optimistic_candle writes directly to DB without API calls."""
        symbol = "BTC"
        timeframe = "5m"
        bar = {
            'time': 1700000000,
            'open': 100.0,
            'high': 105.0,
            'low': 99.0,
            'close': 102.0,
            'volume': 500.0
        }
        
        # Trigger worker directly
        api_client._persist_optimistic_candle(symbol, timeframe, bar)
        
        # Verify DB Insert
        api_client.market_db.insert_market_data.assert_called_once()
        df_arg = api_client.market_db.insert_market_data.call_args[0][0]
        assert isinstance(df_arg, pd.DataFrame)
        assert len(df_arg) == 1
        assert df_arg.iloc[0]['close'] == 102.0
        assert df_arg.index[0].timestamp() == 1700000000
        
        # Verify NO API calls made (redundant check but good for integrity)
        api_client.info.candles_snapshot.assert_not_called()

    def test_persist_optimistic_candle_error_handling(self, api_client):
        """Verify exception handling during persistence."""
        symbol = "BTC"
        timeframe = "5m"
        bar = {'time': 1700000000}
        
        # Simulate DB error
        api_client.market_db.insert_market_data.side_effect = Exception("DB Disk Full")
        
        # Should catch exception and log error, NOT raise
        api_client._persist_optimistic_candle(symbol, timeframe, bar)
        
        # Verify DB attempted
        api_client.market_db.insert_market_data.assert_called_once()
