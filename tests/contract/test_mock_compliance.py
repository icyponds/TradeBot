
import pytest
import pandas as pd
from datetime import datetime
from src.backtesting.mock_market_api import MockMarketAPI
from tests.contract.test_market_api_interface import MarketApiContract

class TestMockMarketApi(MarketApiContract):
    """
    Run contract tests against MockMarketAPI.
    """
    
    @pytest.fixture
    def api(self):
        # Setup specific to Mock
        config = {'test': True}
        
        # Create minimal dummy historical data
        dates = pd.date_range(start='2024-01-01', periods=10, freq='1h')
        df = pd.DataFrame({
            'open': [100.0] * 10,
            'high': [105.0] * 10,
            'low': [95.0] * 10,
            'close': [101.0] * 10,
            'volume': [1000] * 10
        }, index=dates)
        
        historical_data = {
            'BTC_SPOT': {'1h': df},
            'BTC': {'1h': df}
        }
        
        mock_api = MockMarketAPI(config, historical_data)
        mock_api.set_time(dates[5]) # Set simulation time to middle of data
        return mock_api

    # Failure Modes / Specific Behavior Tests can be added here
    # e.g., verifying that get_current_price actually returns the value at 'current_time'
    
    def test_mock_specific_price_logic(self, api):
        # We set simulation time to index[5] (6th row)
        # Previous closed candle is index[4] or we logic in mock uses <= or <
        # Mock logic get_current_price uses .loc[mask][-1] where mask is df.index < current_time
        # So at current_time=dates[5], strictly before is dates[0]..dates[4]. 
        # Last is dates[4].
        
        # In our dummy data, all closes are 101.0
        price = api.get_current_price("BTC_SPOT")
        assert price == 101.0
