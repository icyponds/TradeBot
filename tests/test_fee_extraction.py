
import pytest
from unittest.mock import MagicMock


class TestFeeExtraction:
    
    @pytest.fixture
    def api_client(self, shared_api_client):
        """Use shared module-scoped client, reset mocks before each test."""
        shared_api_client.info.reset_mock()
        shared_api_client.exchange.reset_mock()
        shared_api_client.logger = MagicMock()
        return shared_api_client

    def test_parse_fee_standard(self, api_client):
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
        result = api_client._parse_order_response(response, "BTC", "buy", 1.0, 100.0)
        assert result['fee'] == 0.05

    def test_parse_fee_totalFee(self, api_client):
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
        result = api_client._parse_order_response(response, "BTC", "buy", 1.0, 100.0)
        assert result['fee'] == 0.06

    def test_parse_fee_missing(self, api_client):
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
        result = api_client._parse_order_response(response, "BTC", "buy", 1.0, 100.0)
        assert result['fee'] == 0.0
