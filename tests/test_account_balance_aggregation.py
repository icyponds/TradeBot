"""
Test that get_account_balance returns native DEX balance correctly.
(Aggregation was removed to prevent rate limit exhaustion)
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
        'hip3': {'enabled': True, 'perp_dexs': ['native', 'xyz']}
    }

def test_get_account_balance_returns_native_only(mock_config):
    """
    Verify that get_account_balance() only queries native DEX (single call).
    This is intentional to prevent rate limit exhaustion.
    """
    # Setup Mock Info
    with patch('hyperliquid.info.Info') as MockInfoClass:
        mock_info = MockInfoClass.return_value
        
        # Setup user_state to return native balance
        mock_info.user_state.return_value = {
            'marginSummary': {
                'accountValue': '1000.0',
                'totalMarginUsed': '100.0',
                'totalUnrealizedPnl': '50.0'
            }
        }
        
        # Initialize API
        with patch.object(HyperliquidAPI, '_discover_perp_dexs', return_value=['', 'xyz']):
             api = HyperliquidAPI(mock_config)
             api.info = mock_info
             api.perp_dexs = ['', 'xyz']
             api.public_account_address = '0x123'
             api.hip3_enabled = True
        
        # Call get_account_balance
        balance = api.get_account_balance()
        
        # Verify single call was made (native only)
        assert mock_info.user_state.call_count == 1
        
        # Verify correct values
        assert balance['total_equity'] == 1000.0
        assert balance['used_margin'] == 100.0
        assert balance['free_margin'] == 900.0
        assert balance['unrealized_pnl'] == 50.0
