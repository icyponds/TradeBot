"""
Test that get_positions properly bubbles up exceptions for retry logic.
"""
import pytest
from unittest.mock import MagicMock, patch
from src.api.hyperliquid_api import HyperliquidAPI


@pytest.fixture
def mock_config():
    return {
        'api': {
            'base_url': 'https://api.hyperliquid.xyz',
            'private_key': '0xabc',
            'wallet_address': '0x123'
        },
        'hip3': {'enabled': True, 'perp_dexs': ['', 'xyz']}
    }


def test_get_positions_bubbles_up_429_error(mock_config):
    """
    Verify that get_positions() raises 429 errors instead of swallowing them.
    This allows _rate_limited_call to retry properly.
    """
    with patch('hyperliquid.info.Info') as MockInfoClass:
        mock_info = MockInfoClass.return_value
        
        # First call succeeds, second call (HIP-3 DEX) fails with 429
        mock_info.user_state.side_effect = [
            {'assetPositions': []},  # Native DEX OK
            Exception("(429, None, 'null', None, ...)")  # HIP-3 DEX fails
        ]
        
        with patch.object(HyperliquidAPI, '_discover_perp_dexs', return_value=['', 'xyz']):
            api = HyperliquidAPI(mock_config)
            api.info = mock_info
            api.perp_dexs = ['', 'xyz']
            api.hip3_enabled = True
            api.public_account_address = '0x123'
            
            # Bypass rate limiter wrapper for this test
            api._rate_limited_call = lambda fn, *args, **kwargs: fn(*args)
        
        # Should raise exception, NOT return partial data
        with pytest.raises(Exception) as exc_info:
            api.get_positions()
        
        assert "429" in str(exc_info.value)


def test_get_positions_returns_all_dex_positions(mock_config):
    """
    Verify that get_positions() aggregates positions from all DEXs.
    """
    with patch('hyperliquid.info.Info') as MockInfoClass:
        mock_info = MockInfoClass.return_value
        
        # Native DEX has BTC, HIP-3 has PLTR
        mock_info.user_state.side_effect = [
            {'assetPositions': [{'position': {'coin': 'BTC', 'szi': '1.0', 'entryPx': '50000'}}]},
            {'assetPositions': [{'position': {'coin': 'PLTR', 'szi': '100', 'entryPx': '25'}}]}
        ]
        
        with patch.object(HyperliquidAPI, '_discover_perp_dexs', return_value=['', 'xyz']):
            api = HyperliquidAPI(mock_config)
            api.info = mock_info
            api.perp_dexs = ['', 'xyz']
            api.hip3_enabled = True
            api.public_account_address = '0x123'
            api._rate_limited_call = lambda fn, *args, **kwargs: fn(*args)
        
        positions = api.get_positions()
        
        assert len(positions) == 2
        symbols = [p['symbol'] for p in positions]
        assert 'BTC' in symbols
        assert 'xyz:PLTR' in symbols
