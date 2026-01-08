import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

import pytest
from unittest.mock import MagicMock, patch
from src.api.hyperliquid_api import HyperliquidAPI
import pandas as pd

class TestHIP3Discovery:
    
    @pytest.fixture
    def mock_info(self):
        with patch('hyperliquid.info.Info') as MockInfo:
            mock_info_instance = MockInfo.return_value
            # Mock default native response
            mock_info_instance.meta_and_asset_ctxs.return_value = (
                {'universe': [{'name': 'BTC', 'maxLeverage': 50}]}, 
                [{'markPx': '50000', 'dayNtlVlm': '1000'}]
            )
            # Mock dex list
            mock_info_instance.perp_dexs.return_value = [
                {'name': ''}, # Native
                {'name': 'hyna'},
                {'name': 'xyz'}
            ]
            # Mock post method for Dex metadata calls
            def mock_post_side_effect(endpoint, payload):
                print(f"DEBUG: Mock Post called: {endpoint}, {payload}")
                if payload.get('type') == 'metaAndAssetCtxs':
                    dex_name = payload.get('dex')
                    if dex_name == 'hyna':
                        return (
                            {'universe': [{'name': 'hyna:BTC', 'maxLeverage': 20}]}, 
                            [{'markPx': '50100', 'dayNtlVlm': '500'}]
                        )
                    if dex_name == 'xyz':
                        return (
                             {'universe': [{'name': 'xyz:ETH', 'maxLeverage': 10}]}, 
                             [{'markPx': '3000', 'dayNtlVlm': '200'}]
                        )
                if payload.get('type') == 'candleSnapshot':
                    req = payload.get('req')
                    coin = req.get('coin')
                    print(f"DEBUG: Candle Req Coin: {coin}")
                    if coin == 'UnknownHIP3':
                        return [
                            {'t': 1000000, 'o': 100, 'h': 110, 'l': 90, 'c': 105, 'v': 1000}
                        ]
                return []
            
            mock_info_instance.post.side_effect = mock_post_side_effect
            
            # Mock candles_snapshot for known symbols
            mock_info_instance.candles_snapshot.side_effect = lambda s, t, st, et: (
                [{'t': 1000000, 'o': 50000, 'h': 51000, 'l': 49000, 'c': 50500, 'v': 100}] 
                if s == 'BTC' else (_ for _ in ()).throw(KeyError("Symbol not found"))
            )
            
            yield mock_info_instance

    @pytest.fixture
    def api(self, mock_info):
        config = {
            'api': {
                'base_url': 'https://api.hyperliquid.xyz',
                'private_key': '0000000000000000000000000000000000000000000000000000000000000000',
                'account': '0x0000000000000000000000000000000000000000',
                'wallet_address': '0x0000000000000000000000000000000000000000'
            },
            'hip3': {'enabled': True, 'include_in_pair_selection': True}
        }
        api = HyperliquidAPI(config)
        api.info = mock_info # Inject mock
        
        # Bypass rate limiter for tests to ensure logic runs
        api._rate_limited_call = lambda func, *args, **kwargs: func(*args, **kwargs)
        
        return api

    def test_hip3_discovery_iterates_dexes(self, api, mock_info):
        """Verify get_all_perp_assets calls API for each non-native Dex."""
        assets = api.get_all_perp_assets(include_hip3=True)
        
        # Should find: BTC (Native), hyna:BTC (HIP-3), xyz:ETH (HIP-3)
        assert len(assets) == 3
        
        btc_native = next(a for a in assets if a['name'] == 'BTC')
        assert btc_native['is_hip3'] is False
        assert btc_native['dex'] == ''
        
        btc_hyna = next(a for a in assets if a['name'] == 'hyna:BTC')
        assert btc_hyna['is_hip3'] is True
        assert btc_hyna['dex'] == 'hyna'
        
        eth_xyz = next(a for a in assets if a['name'] == 'xyz:ETH')
        assert eth_xyz['is_hip3'] is True
        assert eth_xyz['dex'] == 'xyz'
        
        # START UPDATE: Check that post was called correctly
        # We expect calls for 'hyna' and 'xyz'
        # Filter calls to post with metaAndAssetCtxs
        calls = [c for c in mock_info.post.call_args_list if c[0][1].get('type') == 'metaAndAssetCtxs']
        dexes_called = [c[0][1].get('dex') for c in calls]
        assert 'hyna' in dexes_called
        assert 'xyz' in dexes_called

    def test_get_ohlcv_known_symbol(self, api, mock_info):
        """Verify normal behavior for known symbols (SDK wrapper)."""
        df = api.get_ohlcv('BTC', '1h', limit=5)
        
        assert df is not None
        assert not df.empty
        # Verify SDK wrapper was called
        mock_info.candles_snapshot.assert_called()

    def test_get_market_data_hip3_fallback(self, api, mock_info):
        """Verify get_market_data correctly falls back to checking other dexes for HIP-3 assets."""
        # 1. Setup mocks
        # partial return for meta_and_asset_ctxs (Native) -> Asset NOT found
        mock_info.meta_and_asset_ctxs.return_value = (
            {'universe': [{'name': 'BTC'}]}, 
            [{'markPx': '100.0'}]
        )
        
        # Mock perp_dexs to return native + hyna
        mock_info.perp_dexs.return_value = [{'name': 'native'}, {'name': 'hyna'}]
        
        # Mock post to intercept "metaAndAssetCtxs" call for hyna
        def mock_post_side_effect(endpoint, payload):
            if payload.get("type") == "metaAndAssetCtxs" and payload.get("dex") == "hyna":
                # Return hyna universe containing our target
                # Structure: [ {'universe': [...]}, [ctxs...] ]
                # wait, checking api code:
                # res = self.info.post...
                # if res and len(res) >= 2:
                #   u_hip3 = res[0]['universe']
                return [
                    {'universe': [{'name': 'hyna:BTC', 'maxLeverage': 20, 'szDecimals': 4}]},
                    [{'markPx': '105.5', 'dayNtlVlm': '5000', 'funding': '0.0001', 'impactPxs': ['105.4', '105.6']}]
                ]
            return []

        mock_info.post.side_effect = mock_post_side_effect
        
        # 2. Call get_market_data for a HIP-3 asset
        # Need to ensure hip3_enabled is True in fixture or config
        api.hip3_enabled = True
        
        data = api.get_market_data('hyna:BTC')
        
        # 3. Verify
        assert data is not None
        assert data['symbol'] == 'hyna:BTC'
        assert data['current_price'] == 105.5
        assert data['volume_24h'] == 5000.0
        
        # Verify perp_dexs was called (to get the list)
        mock_info.perp_dexs.assert_called()
        
        # Verify post was called for hyna
        calls = [c for c in mock_info.post.call_args_list if c[0][1].get('dex') == 'hyna']
        assert len(calls) > 0
