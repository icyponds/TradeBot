"""
Resilience tests for balance fetching on UNIFIED accounts (Hyperliquid
Dec-2025 account abstraction — the current production mode):
- total failure of the spot clearinghouse fetch -> None (safe fallback,
  never an unhandled exception mid-cycle)
- perp-positions (PnL) fetch failure is tolerated: balance still returned
- abstraction-state query failure -> legacy path engaged (its own contract)
- cache served without re-fetching
"""

import pytest
from unittest.mock import MagicMock, patch
from src.api.hyperliquid_api import HyperliquidAPI


SPOT_STATE = {
    'balances': [{'coin': 'USDC', 'token': 0, 'total': '63.22', 'hold': '0.0'}],
    'tokenToAvailableAfterMaintenance': [[0, '53.22']],
}


@pytest.fixture
def unified_api():
    config = {
        'wallet': {'address': '0x123', 'private_key': '0xabc'},
        'api': {
            'base_url': 'https://api.hyperliquid.xyz',
            'private_key': '0xabc',
            'wallet_address': '0x123'
        },
        'hip3': {'perp_dexs': ['flx', 'cash']}
    }
    with patch('hyperliquid.exchange.Exchange'), \
         patch('hyperliquid.info.Info'):
        api = HyperliquidAPI(config)
        api.info = MagicMock()
        api.info.query_user_abstraction_state.return_value = 'unifiedAccount'
        api.info.spot_user_state.return_value = SPOT_STATE
        api.info.user_state.return_value = {'assetPositions': []}
        # No retry sleeps in tests
        api._rate_limited_call = lambda fn, *a, **k: fn(*a)
        return api


def test_unified_balance_happy_path(unified_api):
    bal = unified_api.get_account_balance()
    assert bal['total_equity'] == pytest.approx(63.22)
    assert bal['free_margin'] == pytest.approx(53.22)


def test_spot_state_failure_returns_none(unified_api):
    """Total failure -> None, the safe fallback PortfolioManager expects."""
    unified_api.info.spot_user_state.side_effect = RuntimeError("API down")
    assert unified_api.get_account_balance() is None


def test_pnl_fetch_failure_tolerated(unified_api):
    """Perp positions unavailable -> balance still returned, PnL = 0."""
    unified_api.info.user_state.side_effect = RuntimeError("API down")
    bal = unified_api.get_account_balance()
    assert bal is not None
    assert bal['total_equity'] == pytest.approx(63.22)
    assert bal['unrealized_pnl'] == 0.0


def test_abstraction_query_failure_falls_back_to_legacy(unified_api):
    """Mode unknown -> legacy path engaged (which has its own None contract)."""
    unified_api._abstraction_mode = None  # clear anything cached
    unified_api.info.query_user_abstraction_state.side_effect = RuntimeError("down")
    unified_api.perp_dexs = [""]
    unified_api.info.user_state.side_effect = RuntimeError("API down")
    # Legacy path with all dex fetches failing -> None
    assert unified_api.get_account_balance() is None
    assert unified_api._get_account_abstraction_mode() == 'disabled'


def test_balance_served_from_cache(unified_api):
    first = unified_api.get_account_balance()
    unified_api.info.spot_user_state.side_effect = RuntimeError("should not be called")
    assert unified_api.get_account_balance() == first
    assert unified_api.info.spot_user_state.call_count == 1


def test_perp_balance_failure_returns_zeroes(unified_api):
    """get_perp_balance: total failure -> zeroed dict (legacy contract)."""
    unified_api.info.spot_user_state.side_effect = RuntimeError("API down")
    pb = unified_api.get_perp_balance()
    assert pb == {'account_value': 0, 'total_margin_used': 0, 'withdrawable': 0}
