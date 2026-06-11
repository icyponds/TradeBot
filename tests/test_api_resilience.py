
import pytest
from unittest.mock import MagicMock, patch
from src.api.hyperliquid_api import HyperliquidAPI

@pytest.fixture
def mock_api():
    config = {
        'wallet': {'address': '0x123', 'private_key': '0xabc'},
        'api': {
            'base_url': 'https://api.hyperliquid.xyz',
            'private_key': '0xabc',
            'wallet_address': '0x123'
        },
        'hip3': {'perp_dexs': ['flx', 'cash']} # Test with multiple contexts
    }
    with patch('hyperliquid.exchange.Exchange'), \
         patch('hyperliquid.info.Info'):
        api = HyperliquidAPI(config)
        # These tests exercise the LEGACY (split spot/perp) aggregation path;
        # pin the mode so they don't depend on MagicMock coercion of the
        # unified-account abstraction query.
        api._abstraction_mode = 'disabled'
        return api

def test_get_account_balance_all_fail_returns_none(mock_api):
    """
    Test that get_account_balance returns None (safe fallback) 
    if ALL fetch attempts fail (e.g., due to strict rate limits).
    """
    # Mock info.user_state to raise Exception for every call
    mock_api.info.user_state.side_effect = Exception("API Error 429: Too Many Requests")
    
    result = mock_api.get_account_balance()
    
    # Crucial assertion: Must return None, NOT a zero-ed out dictionary
    assert result is None
    # Verify it tried all 3 contexts ("" + flx + cash)
    assert mock_api.info.user_state.call_count == 3

def test_get_account_balance_partial_success(mock_api):
    """Test that it returns a valid aggregated result if at least ONE fetch succeeds."""
    
    # Success response structure
    success_response = {
        'marginSummary': {
            'accountValue': '100.0',
            'totalMarginUsed': '10.0',
            'totalUnrealizedPnl': '5.0',
            'withdrawable': '90.0'
        }
    }
    
    # Mock side_effect: 
    # 1. Native Context ("") -> Fails
    # 2. FLX Context ("flx") -> Succeeds
    # 3. CASH Context ("cash") -> Fails
    mock_api.info.user_state.side_effect = [
        Exception("Fail 1"),
        success_response,
        Exception("Fail 2")
    ]
    
    result = mock_api.get_account_balance()
    
    assert result is not None
    # Equity should reflect the successful fetch
    assert result['total_equity'] == 100.0
    # Free margin calculation: Equity - Used (100 - 10 = 90)
    assert result['free_margin'] == 90.0
    assert result['used_margin'] == 10.0
    assert result['unrealized_pnl'] == 5.0

def test_get_account_balance_all_success_aggregation(mock_api):
    """Test that values are correctly aggregated when multiple contexts succeed."""
    
    # 1. Native: $1000 equity, $100 used
    native_response = {
        'marginSummary': {
            'accountValue': '1000.0',
            'totalMarginUsed': '100.0',
            'totalUnrealizedPnl': '0.0',
            'withdrawable': '900.0'
        }
    }
    
    # 2. Spot/FLX: $500 equity, $0 used (Segregated)
    flx_response = {
        'marginSummary': {
            'accountValue': '500.0',
            'totalMarginUsed': '0.0',
            'totalUnrealizedPnl': '0.0',
            'withdrawable': '500.0'
        }
    }

    # 3. Cash: Shared collateral (same withdrawable as native) -> Should NOT add equity
    cash_response = {
        'marginSummary': {
            'accountValue': '900.0', # Just cash
            'totalMarginUsed': '0.0',
            'totalUnrealizedPnl': '0.0',
            'withdrawable': '900.0' # Matches native withdrawable -> Shared
        }
    }
    
    mock_api.info.user_state.side_effect = [native_response, flx_response, cash_response]
    
    result = mock_api.get_account_balance()
    
    assert result is not None
    # Expected Equity: 
    # Native ($1000) + FLX ($500) + Cash (Shared, so +0) = $1500
    # Logic verification:
    # Native: equity += 1000. main_withdrawable = 900.
    # FLX: withdrawable (500) != 900. equity += 500.
    # Cash: withdrawable (900) == 900. equity += (900 - 900) = 0.
    
    assert result['total_equity'] == 1500.0
    assert result['used_margin'] == 100.0
    assert result['free_margin'] == 1400.0
