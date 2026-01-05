
import unittest
from unittest.mock import MagicMock, patch
from src.api.hyperliquid_api import HyperliquidAPI

class TestFeeRetrieval(unittest.TestCase):
    def setUp(self):
        self.config = {
            'api': {
                'base_url': 'https://api.hyperliquid.xyz',
                'private_key': '00'*32,
                'wallet_address': '0x'+'00'*20,
            }
        }
        self.api = HyperliquidAPI(self.config)
        self.api.logger = MagicMock() # Suppress logging
        # Mock Info client
        self.api.info = MagicMock()

    def test_get_execution_fee_found(self):
        """Test retrieving fee when order ID matches."""
        # Mock user_fills response
        self.api.info.user_fills.return_value = [
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
        
        fee = self.api.get_execution_fee(12345)
        self.assertEqual(fee, 0.123)
        self.api.info.user_fills.assert_called_with(self.api.wallet_address)

    def test_get_execution_fee_not_found(self):
        """Test retrieving fee when order ID is not in fills."""
        self.api.info.user_fills.return_value = [
            {'oid': 67890, 'fee': '0.456'}
        ]
        
        fee = self.api.get_execution_fee(12345)
        self.assertEqual(fee, 0.0)

    def test_get_execution_fee_api_error(self):
        """Test behavior when API call fails."""
        self.api.info.user_fills.side_effect = Exception("API Error")
        
        fee = self.api.get_execution_fee(12345)
        self.assertEqual(fee, 0.0)

if __name__ == '__main__':
    unittest.main()
