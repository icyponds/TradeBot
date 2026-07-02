"""
Regression tests: read-path API methods MUST route through _rate_limited_call.

Codebase review 2026-07-02 found seven call sites invoking self.info.* directly,
bypassing 429/transient retry handling (CLAUDE.md rule 4):
get_all_prices, get_order_book, get_asset_meta, _get_unified_balance (x2),
get_account_balance (multi-dex loop), get_positions (multi-dex loop),
get_execution_fee.

Each test replaces _rate_limited_call with a spy; if the method calls the SDK
directly instead of through the wrapper, the spy count stays 0 and the test
fails (as it did on the pre-fix code).
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
        'hip3': {'enabled': False, 'perp_dexs': []}
    }


@pytest.fixture
def api(mock_config):
    with patch('hyperliquid.info.Info') as MockInfoClass:
        with patch.object(HyperliquidAPI, '_discover_perp_dexs', return_value=['']):
            api = HyperliquidAPI(mock_config)
            api.info = MockInfoClass.return_value
            api.perp_dexs = ['']
            api.public_account_address = '0x123'
            api.wallet_address = '0x123'
    return api


def _install_spy(api):
    """Replace _rate_limited_call with a pass-through spy."""
    calls = []

    def spy(fn, *args, **kwargs):
        calls.append(fn)
        return fn(*args)

    api._rate_limited_call = spy
    return calls


def test_get_all_prices_uses_wrapper(api):
    calls = _install_spy(api)
    api.info.all_mids.return_value = {'BTC': '50000'}

    prices = api.get_all_prices()

    assert prices == {'BTC': 50000.0}
    assert len(calls) == 1


def test_get_order_book_uses_wrapper(api):
    calls = _install_spy(api)
    api.info.l2_snapshot.return_value = {'levels': [[{'px': '1'}], [{'px': '2'}]]}

    book = api.get_order_book('BTC')

    assert book['symbol'] == 'BTC'
    assert len(calls) == 1


def test_get_asset_meta_uses_wrapper(api):
    calls = _install_spy(api)
    api.info.meta.return_value = {'universe': [{'name': 'BTC', 'maxLeverage': 50}]}

    meta = api.get_asset_meta('BTC')

    assert meta == {'name': 'BTC', 'maxLeverage': 50}
    assert len(calls) == 1


def test_get_unified_balance_uses_wrapper(api):
    calls = _install_spy(api)
    api.info.spot_user_state.return_value = {
        'balances': [{'coin': 'USDC', 'total': '1000'}],
        'tokenToAvailableAfterMaintenance': [[0, '900']],
    }
    api.info.user_state.return_value = {
        'assetPositions': [{'position': {'unrealizedPnl': '25'}}]
    }

    bal = api._get_unified_balance()

    assert bal['total_equity'] == pytest.approx(1025.0)
    assert bal['free_margin'] == pytest.approx(900.0)
    # Both the spot state and the perp state fetch must go through the wrapper
    assert len(calls) == 2


def test_get_account_balance_multi_dex_uses_wrapper(api):
    calls = _install_spy(api)
    api.cache = MagicMock()
    api.cache.get.return_value = None
    api._abstraction_mode = 'disabled'
    api.info.user_state.return_value = {
        'marginSummary': {
            'accountValue': '1000', 'totalMarginUsed': '100',
            'totalUnrealizedPnl': '0', 'withdrawable': '900',
        }
    }

    bal = api.get_account_balance()

    assert bal is not None
    assert api.info.user_state.call_count >= 1
    assert len(calls) >= api.info.user_state.call_count


def test_get_positions_multi_dex_uses_wrapper(api):
    calls = _install_spy(api)
    api.info.user_state.return_value = {
        'assetPositions': [{'position': {'coin': 'BTC', 'szi': '1.0',
                                         'entryPx': '50000'}}]
    }

    positions = api.get_positions()

    assert len(positions) == 1
    assert api.info.user_state.call_count >= 1
    assert len(calls) >= api.info.user_state.call_count


def test_get_execution_fee_uses_wrapper(api):
    calls = _install_spy(api)
    api.info.user_fills.return_value = [
        {'oid': 42, 'fee': '0.5'},
        {'oid': 43, 'fee': '0.9'},
    ]

    fee = api.get_execution_fee(42)

    assert fee == pytest.approx(0.5)
    assert len(calls) == 1


def test_wrapper_retries_on_429_for_get_all_prices(api):
    """End-to-end: a 429 on the first attempt is retried, not surfaced."""
    attempts = {'n': 0}

    def flaky_all_mids():
        attempts['n'] += 1
        if attempts['n'] == 1:
            raise Exception("(429, None, 'null', None)")
        return {'ETH': '3000'}

    api.info.all_mids = flaky_all_mids

    with patch('time.sleep'):
        prices = api.get_all_prices()

    assert prices == {'ETH': 3000.0}
    assert attempts['n'] == 2
