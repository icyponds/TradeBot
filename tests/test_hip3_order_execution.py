
import unittest
from unittest.mock import MagicMock, patch
from src.api.hyperliquid_api import HyperliquidAPI

class TestHIP3OrderExecution(unittest.TestCase):
    def setUp(self):
        self.config = {
            'api': {
                'base_url': 'https://api.hyperliquid.xyz',
                'private_key': '00' * 32,
                'wallet_address': '0x' + '00' * 20,
            },
            'hip3': {
                'enabled': True
            }
        }

    @patch('src.api.hyperliquid_api.Info')
    @patch('src.api.hyperliquid_api.Exchange')
    @patch('src.api.hyperliquid_api.Account')
    def test_hip3_asset_map_update(self, mock_account, mock_exchange_cls, mock_info_cls):
        # Setup mocks
        mock_info = MagicMock()
        mock_exchange = MagicMock()
        mock_info_cls.return_value = mock_info
        mock_exchange_cls.return_value = mock_exchange
        
        # Mock initial discovery (just native)
        mock_info.perp_dexs.return_value = [{"name": "native"}]
        
        # Mock meta_and_asset_ctxs for native
        mock_info.meta_and_asset_ctxs.return_value = (
            {'universe': [{'name': 'BTC', 'szDecimals': 2}]},
            [{'markPx': '1000'}]
        )

        # Initialize API
        api = HyperliquidAPI(self.config)
        
        # Mock exchange internal maps (via info)
        # The SDK Exchange client relies on its internal Info object for mappings.
        api.exchange.info = MagicMock()
        api.exchange.info.name_to_coin = {'BTC': {'name': 'BTC', 'assetId': 0}}
        api.exchange.info.coin_to_asset = {'BTC': 0}
        
        # --- PHASE 2: Discover HIP-3 Asset ---
        
        # Mock finding a HIP-3 Dex
        mock_info.perp_dexs.return_value = [{"name": "native"}, {"name": "HyperLiquidity"}]
        
        # Mock post response for the HIP-3 Dex
        # Note: This is what get_all_perp_assets calls
        mock_info.post.return_value = (
            {'universe': [{'name': 'HYPE', 'assetId': 12345, 'szDecimals': 2, 'maxLeverage': 5}]},
            [{'markPx': '10', 'openInterest': '1000'}]
        )
        
        # Run discovery
        assets = api.get_all_perp_assets(include_hip3=True)
        
        # Verify we found the asset
        hype_asset = next((a for a in assets if a['name'] == 'HYPE'), None)
        self.assertIsNotNone(hype_asset, "Should have discovered HYPE asset")
        self.assertTrue(hype_asset['is_hip3'], "Should be marked as HIP-3")
        
        # --- VERIFICATION ---
        # The Exchange.info object should now know about 'HYPE'
        self.assertIn('HYPE', api.exchange.info.name_to_coin, "Exchange.info should know about HYPE")
        self.assertEqual(api.exchange.info.coin_to_asset['HYPE'], 12345, "Exchange.info should map HYPE to AssetID")
        
        # Also simulate execute_order checking for it
        # We'll mock _resolve_market_info which calls get_current_price -> needs asset info
        # But specifically, if we called api.exchange.order('HYPE', ...), it needs the map.
        
        # Verify that we can resolve asset info provided the map is updated
        # (This part tests our logic relies on the map)
        
if __name__ == '__main__':
    unittest.main()
