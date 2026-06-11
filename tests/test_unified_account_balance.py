"""
Tests for unified-account balance accounting (Hyperliquid Dec-2025 account
abstraction): for unifiedAccount/portfolioMargin modes, collateral comes
from the SPOT clearinghouse state and the perp marginSummary is ignored.
"""

import logging
from types import SimpleNamespace

import pytest

from src.api.hyperliquid_api import HyperliquidAPI


SPOT_STATE = {
    'balances': [
        {'coin': 'USDC', 'token': 0, 'total': '63.22', 'hold': '0.0'},
        {'coin': 'HYPE', 'token': 150, 'total': '0.0007', 'hold': '0.0'},
    ],
    'tokenToAvailableAfterMaintenance': [[0, '53.22'], [360, '0.0']],
}

PERP_STATE_WITH_POSITION = {
    'marginSummary': {'accountValue': '0', 'totalMarginUsed': '0'},
    'assetPositions': [{'position': {'coin': 'BTC', 'szi': '0.001',
                                     'unrealizedPnl': '2.50'}}],
}


def make_api(mode='unifiedAccount', spot_state=SPOT_STATE,
             user_state=PERP_STATE_WITH_POSITION):
    api = HyperliquidAPI.__new__(HyperliquidAPI)
    api.logger = logging.getLogger('test_unified')
    api.public_account_address = '0xabc'
    api.info = SimpleNamespace(
        query_user_abstraction_state=lambda addr: mode,
        spot_user_state=lambda addr: spot_state,
        user_state=lambda addr, dex="": user_state,
    )
    api._rate_limited_call = lambda fn, *a, **k: fn(*a)
    api.cache = SimpleNamespace(get=lambda k: None, set=lambda *a, **k: None)
    api.cache_ttl_positions = 5
    return api


class TestAbstractionMode:
    def test_unified_mode_detected_and_cached(self):
        api = make_api('unifiedAccount')
        assert api._get_account_abstraction_mode() == 'unifiedAccount'
        api.info.query_user_abstraction_state = lambda addr: (_ for _ in ()).throw(RuntimeError)
        assert api._get_account_abstraction_mode() == 'unifiedAccount'  # cached

    def test_portfolio_margin_treated_as_unified(self):
        assert make_api('portfolioMargin')._get_account_abstraction_mode() == 'portfolioMargin'

    def test_unknown_or_failed_query_falls_back_to_legacy(self):
        assert make_api('disabled')._get_account_abstraction_mode() == 'disabled'
        api = make_api()
        api.info.query_user_abstraction_state = lambda addr: (_ for _ in ()).throw(RuntimeError("down"))
        assert api._get_account_abstraction_mode() == 'disabled'


class TestUnifiedBalance:
    def test_balance_from_spot_state(self):
        bal = make_api().get_account_balance()
        assert bal['total_equity'] == pytest.approx(63.22 + 2.50)
        assert bal['free_margin'] == pytest.approx(53.22)  # after maintenance
        assert bal['used_margin'] == pytest.approx(63.22 + 2.50 - 53.22)
        assert bal['unrealized_pnl'] == pytest.approx(2.50)

    def test_perp_balance_maps_unified_fields(self):
        pb = make_api().get_perp_balance()
        assert pb['account_value'] == pytest.approx(65.72)
        assert pb['withdrawable'] == pytest.approx(53.22)

    def test_no_positions_pnl_zero(self):
        api = make_api(user_state={'marginSummary': {}, 'assetPositions': []})
        bal = api.get_account_balance()
        assert bal['total_equity'] == pytest.approx(63.22)
        assert bal['unrealized_pnl'] == 0.0
