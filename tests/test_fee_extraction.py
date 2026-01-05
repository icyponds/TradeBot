
import unittest
from src.api.hyperliquid_api import HyperliquidAPI
from unittest.mock import MagicMock

class TestFeeExtraction(unittest.TestCase):
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

    def test_parse_fee_standard(self):
        """Test parsing fee from standard 'fee' field."""
        response = {
            "response": {
                "data": {
                    "statuses": [{
                        "filled": {
                            "oid": 123,
                            "totalSz": "1.0",
                            "avgPx": "100.0",
                            "fee": "0.05"
                        }
                    }]
                }
            }
        }
        result = self.api._parse_order_response(response, "BTC", "buy", 1.0, 100.0)
        self.assertEqual(result['fee'], 0.05)

    def test_parse_fee_totalFee(self):
        """Test parsing fee from 'totalFee' field."""
        response = {
            "response": {
                "data": {
                    "statuses": [{
                        "filled": {
                            "oid": 123,
                            "totalSz": "1.0",
                            "avgPx": "100.0",
                            "fee": "0.01",
                            "totalFee": "0.06"
                        }
                    }]
                }
            }
        }
        result = self.api._parse_order_response(response, "BTC", "buy", 1.0, 100.0)
        self.assertEqual(result['fee'], 0.06)

    def test_parse_fee_missing(self):
        """Test behavior when fee is missing (should be 0.0)."""
        response = {
            "response": {
                "data": {
                    "statuses": [{
                        "filled": {
                            "oid": 123,
                            "totalSz": "1.0",
                            "avgPx": "100.0"
                        }
                    }]
                }
            }
        }
        # With fallback removed, this should return 0.0
        result = self.api._parse_order_response(response, "BTC", "buy", 1.0, 100.0)
        self.assertEqual(result['fee'], 0.0)


if __name__ == '__main__':
    unittest.main()
