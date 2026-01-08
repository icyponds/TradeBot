
import pytest
from unittest.mock import MagicMock


class TestFundTransfer:
    
    @pytest.fixture
    def api_client(self, shared_api_client):
        """Use shared module-scoped client, reset mocks before each test."""
        shared_api_client.exchange.reset_mock()
        shared_api_client.logger = MagicMock()
        return shared_api_client

    def test_transfer_usd_to_perp_rounding(self, api_client):
        """Test that transfer_usd_to_perp rounds to 6 decimals."""
        # Setup specific return value for the mock
        api_client.exchange.usd_class_transfer.return_value = {'status': 'ok'}
        
        # High precision input
        amount_in = 10.123456789
        expected_call_amount = 10.123457  # round(10.123456789, 6)
        
        # Action
        result = api_client.transfer_usd_to_perp(amount_in)
        
        # Assert
        assert result is True
        api_client.exchange.usd_class_transfer.assert_called_once_with(expected_call_amount, to_perp=True)

    def test_transfer_usd_to_spot_rounding(self, api_client):
        """Test that transfer_usd_to_spot rounds to 6 decimals."""
        api_client.exchange.usd_class_transfer.return_value = {'status': 'ok'}
        
        amount_in = 5.987654321
        expected_call_amount = 5.987654  # round(5.987654321, 6)
        
        result = api_client.transfer_usd_to_spot(amount_in)
        
        assert result is True
        api_client.exchange.usd_class_transfer.assert_called_once_with(expected_call_amount, to_perp=False)
