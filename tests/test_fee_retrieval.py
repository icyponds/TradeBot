
import pytest
from unittest.mock import MagicMock


class TestFeeRetrieval:
    
    @pytest.fixture
    def api_client(self, shared_api_client):
        """Use shared module-scoped client, reset mocks before each test."""
        shared_api_client.info.reset_mock()
        shared_api_client.logger = MagicMock()
        return shared_api_client

    def test_get_execution_fee_found(self, api_client):
        """Test retrieving fee when order ID matches."""
        # Mock user_fills response
        api_client.info.user_fills.return_value = [
            {
                'oid': 12345,
                'fee': '0.123',
                'feeToken': 'USDC'
            },
            {
                'oid': 67890,
                'fee': '0.456',
                'feeToken': 'USDC'
            }
        ]
        
        fee = api_client.get_execution_fee(12345)
        assert fee == 0.123
        api_client.info.user_fills.assert_called_with(api_client.wallet_address)

    def test_get_execution_fee_not_found(self, api_client):
        """Test retrieving fee when order ID is not in fills."""
        api_client.info.user_fills.return_value = [
            {'oid': 67890, 'fee': '0.456'}
        ]
        
        fee = api_client.get_execution_fee(12345)
        assert fee == 0.0

    def test_get_execution_fee_api_error(self, api_client):
        """Test behavior when API call fails."""
        api_client.info.user_fills.side_effect = Exception("API Error")
        
        fee = api_client.get_execution_fee(12345)
        assert fee == 0.0
