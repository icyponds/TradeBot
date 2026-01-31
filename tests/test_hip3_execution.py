import pytest
from unittest.mock import MagicMock, patch
from src.api.hyperliquid_api import HyperliquidAPI

class TestHip3Execution:
    
    @pytest.fixture
    def mock_api(self):
        # Create API with mocks
        with patch('eth_account.Account'), \
             patch('hyperliquid.exchange.Exchange'), \
             patch('hyperliquid.info.Info'):
             
            config = {
                'wallet': {'private_key': '0x123', 'address': '0x123'},
                'api': {
                    'base_url': 'https://api.hyperliquid.xyz',
                    'private_key': '0x123',
                    'wallet_address': '0x123'
                },
                'hip3': {'enabled': True, 'perp_dexs': [0, 1]}
            }
            api = HyperliquidAPI(config)
            api.exchange = MagicMock()
            api.info = MagicMock()
            return api
            
    def test_execute_order_keeps_hip3_prefix(self, mock_api):
        """Test that execute_order KEEPS the HIP-3 prefix (e.g., 'xyz:TSLA') for SDK calls.
        
        The SDK's name_to_coin mapping expects the full prefixed format for HIP-3 symbols.
        Only numeric prefixes (legacy format) are stripped.
        """
        
        # Setup Mocks
        
        # 1. get_asset_info universe
        mock_api.info.meta_and_asset_ctxs.return_value = (
            {'universe': [{'name': 'BTC', 'szDecimals': 4}, {'name': 'xyz:TSLA', 'szDecimals': 2}]},
            [{}, {}]
        )
        
        # 2. Price lookup - HIP-3 symbols use their full prefixed name
        mock_api.info.all_mids.return_value = {'xyz:TSLA': '200.50'}
        
        # Execute Order with HIP-3 PREFIX
        # xyz:TSLA -> should be KEPT for SDK (SDK expects this format)
        result = mock_api.execute_order(
            symbol="xyz:TSLA",
            side="buy",
            size=1.0,
            market_type="hip3"
        )
        
        # Verify SDK call
        assert mock_api.exchange.order.called
        call_args = mock_api.exchange.order.call_args
        
        # Args: coin, is_buy, sz, limit_px, order_type_dict, reduce_only
        coin_arg = call_args[0][0]
        # HIP-3 prefix should be KEPT for SDK calls
        assert coin_arg == "xyz:TSLA", f"Expected 'xyz:TSLA', got '{coin_arg}'"
        
        # Verify other args
        assert call_args[0][1] is True # is_buy
        
    def test_get_current_price_handles_hip3_prefix(self, mock_api):
        """Test get_current_price handles HIP-3 prefixed symbol."""
        # HIP-3 symbols use their full prefixed name in all_mids
        mock_api.info.all_mids.return_value = {'xyz:TSLA': '200.50'}
        
        price = mock_api.get_current_price("xyz:TSLA")
        assert price == 200.50
        
    def test_get_asset_info_for_symbol_with_hip3_prefix(self, mock_api):
        """Test asset info lookup with HIP-3 prefixed symbols.
        
        HIP-3 DEXs have their own metadata where assets are named with the DEX prefix
        (e.g., 'xyz:TSLA' not just 'TSLA').
        """
        # Mock get_asset_info directly to avoid caching complexity
        mock_api.get_asset_info = MagicMock(return_value={
            'universe': [{'name': 'xyz:TSLA', 'szDecimals': 2}]
        })
        
        # Looking up "xyz:TSLA" should find it directly in universe
        info = mock_api._get_asset_info_for_symbol("xyz:TSLA")
        assert info is not None
        assert info['name'] == 'xyz:TSLA'
