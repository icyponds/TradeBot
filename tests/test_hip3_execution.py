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
            
    def test_execute_order_strips_prefix(self, mock_api):
        """Test that execute_order strips '1:' prefix from symbol before calling SDK."""
        
        # Setup Mocks
        
        # 1. get_asset_info universe (returns clean names)
        # Meta is a dict with 'universe' key
        mock_api.info.meta_and_asset_ctxs.return_value = (
            {'universe': [{'name': 'BTC', 'szDecimals': 4}, {'name': 'TSLA', 'szDecimals': 2}]},
            [{}, {}]
        )
        
        # 2. Price lookup (all_mids returns clean names)
        mock_api.info.all_mids.return_value = {'TSLA': '200.50'}
        
        # Execute Order with PREFIX
        # 1:TSLA -> should be converted to TSLA for SDK
        result = mock_api.execute_order(
            symbol="1:TSLA",
            side="buy",
            size=1.0,
            market_type="hip3" # or perp, should work same
        )
        
        # Verify SDK call
        # The first arg to exchange.order should be the coin name
        assert mock_api.exchange.order.called
        call_args = mock_api.exchange.order.call_args
        
        # Args: coin, is_buy, sz, limit_px, order_type_dict, reduce_only
        coin_arg = call_args[0][0]
        assert coin_arg == "TSLA", f"Expected 'TSLA', got '{coin_arg}'"
        
        # Verify other args
        assert call_args[0][1] is True # is_buy
        
    def test_get_current_price_strips_prefix(self, mock_api):
        """Test get_current_price handles prefixed symbol."""
        mock_api.info.all_mids.return_value = {'TSLA': '200.50'}
        
        price = mock_api.get_current_price("1:TSLA")
        assert price == 200.50
        
    def test_get_asset_info_for_symbol_strips_prefix(self, mock_api):
        """Test asset info lookup handles prefixed symbol."""
        mock_api.info.meta_and_asset_ctxs.return_value = (
            {'universe': [{'name': 'TSLA', 'szDecimals': 2}]},
            [{}]
        )
        
        info = mock_api._get_asset_info_for_symbol("1:TSLA")
        assert info is not None
        assert info['name'] == 'TSLA'
