
import pytest
from unittest.mock import MagicMock, patch
from src.api.hyperliquid_api import HyperliquidAPI
from src.backtesting.mock_market_api import MockMarketAPI

class TestSpotNormalization:

    @pytest.fixture
    def api_config(self):
        return {
            'api': {
                'base_url': 'https://api.hyperliquid.xyz',
                'private_key': "00"*32,
                'wallet_address': "0x"*20,
            }
        }
    
    @pytest.fixture
    def api(self, api_config):
        # Create API instance
        api = HyperliquidAPI(api_config)
        
        # Mock get_spot_meta to return realistic structure with UBTC
        mock_meta = {
            'tokens': [{'name': 'UBTC', 'tokenId': 1}, {'name': 'USDC', 'tokenId': 2}, {'name': 'PURR', 'tokenId': 3}, {'name': 'UETH', 'tokenId': 4}],
            'universe': [{'name': '@109', 'tokens': [0, 1]}, {'name': '@2', 'tokens': [2, 1]}] # USDC is at index 1 in dummy tokens list above? No, wait.
        }
        # tokens: 0: UBTC, 1: USDC, 2: PURR, 3: UETH
        # universe: 
        #   First pair: [0, 1] -> UBTC/USDC
        #   Second pair: [2, 1] -> PURR/USDC
        #   Third pair (Virtual): [3, 1] -> UETH/USDC
        # Actually I need to add UETH to the universe for verification to pass
        mock_meta['universe'].append({'name': '@4', 'tokens': [3, 1]})
        
        api.get_spot_meta = MagicMock(return_value=mock_meta)
        return api

    def test_normalize_symbol_noop(self, api):
        """Test that perps and already normalized symbols are unchanged."""
        assert api.normalize_symbol("BTC") == "BTC"
        assert api.normalize_symbol("BTC_SPOT") == "BTC_SPOT"
        assert api.normalize_symbol("ETH_SPOT") == "ETH_SPOT"
        assert api.normalize_symbol("RANDOM_TOKEN") == "RANDOM_TOKEN"

    def test_normalize_symbol_conversion(self, api):
        """Test that API tokens are converted to internal spot convention."""
        assert api.normalize_symbol("UBTC") == "BTC_SPOT"
        assert api.normalize_symbol("UETH") == "ETH_SPOT"
        assert api.normalize_symbol("USOL") == "SOL_SPOT"
        # Test direct match types
        assert api.normalize_symbol("PURR") == "PURR_SPOT"
        assert api.normalize_symbol("HYPE") == "HYPE_SPOT"

    def test_is_spot_symbol(self, api):
        """Test spot symbol detection."""
        assert api._is_spot_symbol("BTC_SPOT") is True
        assert api._is_spot_symbol("ETH_SPOT") is True
        assert api._is_spot_symbol("BTC") is False
        assert api._is_spot_symbol("UBTC") is False # It maps to True, but strictly checks internal storage format checking logic

    def test_perp_to_spot_mapping(self, api):
        """Test that the mapping returns the correct internal format."""
        assert api.get_spot_token_for_perp("BTC") == "BTC_SPOT"
        assert api.get_spot_token_for_perp("ETH") == "ETH_SPOT"
        assert api.get_spot_token_for_perp("PURR") == "PURR_SPOT"
        assert api.get_spot_token_for_perp("UNKNOWN") is None

    def test_spot_internal_to_api_consistency(self, api):
        """Verify internal->api mapping is consistent."""
        assert api.SPOT_INTERNAL_TO_API['BTC_SPOT'] == 'UBTC'
        assert api.SPOT_INTERNAL_TO_API['ETH_SPOT'] == 'UETH'
        assert api.SPOT_INTERNAL_TO_API['PURR_SPOT'] == 'PURR'
        
    def test_mock_api_mapping(self, api_config):
        """Verify MockMarketAPI parity."""
        mock = MockMarketAPI(api_config, {})
        assert mock.get_spot_token_for_perp("BTC") == "BTC_SPOT"
        assert mock.get_spot_token_for_perp("ETH") == "ETH_SPOT"
