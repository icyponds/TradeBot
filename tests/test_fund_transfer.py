
import unittest
from unittest.mock import MagicMock, patch
from src.api.hyperliquid_api import HyperliquidAPI

class TestFundTransfer(unittest.TestCase):
    def setUp(self):
        self.config = {
            'api': {
                'base_url': 'https://api.hyperliquid.xyz',
                'private_key': '00'*32,
                'wallet_address': '0x'+'00'*20,
            }
        }
        self.api = HyperliquidAPI(self.config)
        self.api.logger = MagicMock()
        # Mock the exchange object
        self.api.exchange = MagicMock()

    def test_transfer_usd_to_perp_rounding(self):
        """Test that transfer_usd_to_perp rounds to 6 decimals."""
        # Setup specific return value for the mock
        self.api.exchange.usd_class_transfer.return_value = {'status': 'ok'}
        
        # High precision input
        amount_in = 10.123456789
        expected_call_amount = 10.123457  # round(10.123456789, 6)
        
        # Action
        result = self.api.transfer_usd_to_perp(amount_in)
        
        # Assert
        self.assertTrue(result)
        self.api.exchange.usd_class_transfer.assert_called_once_with(expected_call_amount, to_perp=True)

    def test_transfer_usd_to_spot_rounding(self):
        """Test that transfer_usd_to_spot rounds to 6 decimals."""
        self.api.exchange.usd_class_transfer.return_value = {'status': 'ok'}
        
        amount_in = 5.987654321
        expected_call_amount = 5.987654  # round(5.987654321, 6)
        
        result = self.api.transfer_usd_to_spot(amount_in)
        
        self.assertTrue(result)
        self.api.exchange.usd_class_transfer.assert_called_once_with(expected_call_amount, to_perp=False)

if __name__ == '__main__':
    unittest.main()
