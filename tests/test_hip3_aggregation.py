
import pytest
from unittest.mock import MagicMock, patch, call
from src.api.hyperliquid_api import HyperliquidAPI

@pytest.fixture
def mock_config():
    return {
        'api': {
            'base_url': 'https://api.hyperliquid.xyz',
            'private_key': '0xabc',
            'wallet_address': '0x123'
        },
        'hip3': {'enabled': True, 'perp_dexs': ['native', 'xyz', 'abc']}
    }

def test_get_positions_aggregates_hip3(mock_config):
    """
    Verify that get_positions() aggregates positions from multiple DEXs
    and correctly formats the symbols with prefixes.
    """
    # 1. Setup Mock Info
    with patch('hyperliquid.info.Info') as MockInfoClass:
        mock_info = MockInfoClass.return_value
        
        # Setup user_state to return different data based on 'dex' arg
        def side_effect_user_state(address, dex=''):
            if dex == '':
                # Native position
                return {'assetPositions': [{'position': {'coin': 'BTC', 'szi': '1.0', 'entryPx': '50000'}}]}
            elif dex == 'xyz':
                # HIP-3 position (xyz)
                return {'assetPositions': [{'position': {'coin': 'PLTR', 'szi': '10.0', 'entryPx': '20'}}]}
            elif dex == 'abc':
                # HIP-3 position (abc) with prefix in coin already? 
                # SDK documentation is unclear, but let's assume it returns RAW coin name "AMZN"
                # And we must PREPEND the dex name.
                return {'assetPositions': [{'position': {'coin': 'AMZN', 'szi': '-5.0', 'entryPx': '100'}}]}
            return {'assetPositions': []}
        
        mock_info.user_state.side_effect = side_effect_user_state

        # Initialize API
        # We mock _discover_perp_dexs to avoid network call
        with patch.object(HyperliquidAPI, '_discover_perp_dexs', return_value=['', 'xyz', 'abc']):
             api = HyperliquidAPI(mock_config)
             api.info = mock_info # Inject mock info
             api.perp_dexs = ['', 'xyz', 'abc']
             api.public_account_address = '0x123'
             api.hip3_enabled = True

        # 2. Call get_positions (THIS WILL FAIL until we implement the fix)
        # We expect the implementation loop through per_dexs
        positions = api.get_positions()
        
        # 3. Verify Aggregation
        assert len(positions) == 3, f"Expected 3 positions, got {len(positions)}"
        
        symbols = {p['symbol'] for p in positions}
        print(f"DEBUG: Retrieved symbols: {symbols}")
        
        # 4. Verify formatting
        assert 'BTC' in symbols, "Native position missing"
        assert 'xyz:PLTR' in symbols, "xyz position missing or prefix incorrect"
        assert 'abc:AMZN' in symbols, "abc position missing or prefix incorrect"
