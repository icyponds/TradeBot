import pytest
from unittest.mock import MagicMock, patch
from src.api.hyperliquid_api import HyperliquidAPI

class TestVerifyAllAssetTypes:
    
    @pytest.fixture
    def mock_api(self):
        # Create API with mocks and full config
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
            
            # Setup Metadata (Native + HIP-3)
            # Info.meta_and_asset_ctxs returns universe dict and ctxs list
            api.info.meta_and_asset_ctxs.return_value = (
                {'universe': [
                    {'name': 'BTC', 'szDecimals': 4},   # Native Perp
                    {'name': 'TSLA', 'szDecimals': 2}   # HIP-3 (prefixed in dashboard, clean here)
                ]},
                [{}, {}]
            )
            
            # Setup Prices
            api.info.all_mids.return_value = {
                'BTC': '100000.0', 
                'TSLA': '200.0',
                'PURR/USDC': '0.5' # Spot prices sometimes appear here or via spot info
            }
            
            # Mock Spot Info for PURR
            # get_spot_meta -> {'tokens': [...], 'universe': [...]}
            # We need to ensure get_spot_token_for_perp works or we manually add mapping if needed
            # For this test, let's assume PURR is in the spot universe
            api.info.spot_meta.return_value = {
                'tokens': [
                    {'name': 'PURR', 'szDecimals': 0},
                    {'name': 'USDC', 'szDecimals': 6}
                ],
                'universe': [
                    {'name': 'PURR/USDC', 'tokens': [0, 1], 'index': 0}
                ]
            }
            
            # Mock Spot Price
            # get_spot_px -> float
            def side_effect_spot_px(base, quote):
                if base == 'PURR' and quote == 'USDC':
                    return 0.5
                return None
            
            # We'll mock the internal helper get_spot_price to avoid complex response structure mocking
            api.get_spot_price = MagicMock(side_effect=side_effect_spot_px)

            return api

    def test_native_perp_execution(self, mock_api):
        """Verify Native Perp (BTC) execution."""
        # execute_order("BTC")
        result = mock_api.execute_order(
            symbol="BTC",
            side="buy",
            size=0.1,
            market_type="perp"
        )
        
        # Expectation: SDK called with "BTC"
        assert mock_api.exchange.order.called
        call_args = mock_api.exchange.order.call_args
        coin_arg = call_args[0][0]
        assert coin_arg == "BTC"
        
    def test_hip3_execution(self, mock_api):
        """Verify HIP-3 (1:TSLA) execution."""
        # execute_order("1:TSLA")
        result = mock_api.execute_order(
            symbol="1:TSLA",
            side="buy",
            size=1.0,
            market_type="perp" # or hip3
        )
        
        # Expectation: SDK called with "TSLA" (stripped)
        assert mock_api.exchange.order.called
        call_args = mock_api.exchange.order.call_args
        coin_arg = call_args[0][0]
        assert coin_arg == "TSLA"

    def test_spot_execution(self, mock_api):
        """Verify Spot (PURR/USDC) execution."""
        # Spot execution typically uses "Base/Quote" format
        # User/Dashboard might pass "PURR/USDC"
        
        # Need to ensure get_spot_token_for_perp has mapping if strictly required
        # But if we pass valid spot pair "PURR/USDC", it should resolve
        
        # Mock the mapping helper just in case logic depends on it
        mock_api.get_spot_token_for_perp = MagicMock(return_value="PURR")
        
        # execute_order("PURR/USDC", market_type='spot')
        result = mock_api.execute_order(
            symbol="PURR/USDC",
            side="buy",
            size=100.0,
            market_type="spot"
        )
        
        # Expectation: SDK called with "PURR/USDC" (or specific spot symbol format)
        assert mock_api.exchange.order.called
        call_args = mock_api.exchange.order.call_args
        coin_arg = call_args[0][0]
        assert coin_arg == "PURR/USDC"
        # Spot execution has extra kwargs usually
        kwargs = call_args[0][4]
        assert kwargs['limit']['tif'] == 'Ioc'
