
import pytest
from src.api.interface import MarketInterface
from typing import Dict, Any

class MarketApiContract:
    """
    Contract test suite for MarketInterface.
    Any class implementing MarketInterface should pass these tests.
    
    Subclasses must define a `api` fixture.
    """

    def test_implements_interface(self, api: MarketInterface):
        """Verify the object explicitly inherits from MarketInterface."""
        assert isinstance(api, MarketInterface)

    def test_get_current_price_signature(self, api: MarketInterface):
        """Test get_current_price returns float or None."""
        # We assume 'BTC_SPOT' or 'ETH' are valid symbols in the test context
        price = api.get_current_price("BTC_SPOT")
        assert price is None or isinstance(price, float)

    def test_get_spot_token_for_perp(self, api: MarketInterface):
        """Test conversion of perp symbol to spot token."""
        token = api.get_spot_token_for_perp("BTC")
        assert token is None or isinstance(token, str)

    def test_get_spot_price(self, api: MarketInterface):
        """Test getting spot price."""
        price = api.get_spot_price("BTC")
        assert price is None or isinstance(price, float)

    def test_get_spot_balance(self, api: MarketInterface):
        """Test getting spot balance."""
        balance = api.get_spot_balance("USDC")
        assert isinstance(balance, float)

    def test_get_perp_balance(self, api: MarketInterface):
        """Test getting perp balance dict."""
        balance = api.get_perp_balance()
        assert isinstance(balance, dict)
        assert 'withdrawable' in balance
        # Accept either 'margin_used' (Mock) or 'total_margin_used' (Real API)
        assert 'margin_used' in balance or 'total_margin_used' in balance

    def test_ensure_funds_signatures(self, api: MarketInterface):
        """Test funds transfer boolean returns."""
        # Using 0.0 amount to be safe/no-op if possible, or expect bool
        result_spot = api.ensure_spot_funds(0.0)
        assert isinstance(result_spot, bool)
        
        result_perp = api.ensure_perp_funds(0.0)
        assert isinstance(result_perp, bool)

    def test_get_positions(self, api: MarketInterface):
        """Test list of dicts return."""
        positions = api.get_positions()
        assert isinstance(positions, list)
        for p in positions:
            assert isinstance(p, dict)
            assert 'symbol' in p
            assert 'size' in p
            assert 'side' in p

    def test_get_execution_fee(self, api: MarketInterface):
        """Test get_execution_fee returns float."""
        fee = api.get_execution_fee("non_existent_id")
        assert isinstance(fee, float)
