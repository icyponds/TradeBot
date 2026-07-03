"""
Leverage update hardening (live failures 2026-07-03).

1. Dynamic sizing can produce sub-1x leverage on small equity (ZEC at
   0.7x); int() truncated it to 0 and the exchange rejected the request
   with an opaque 422. The exchange setting must be an integer >= 1.
2. Some HIP-3 assets (xyz:INTC) are isolated-only without carrying the
   onlyIsolated flag in cached metadata — a cross rejection must retry
   as isolated instead of failing.
"""
import pytest
from unittest.mock import MagicMock, patch

from src.api.hyperliquid_api import HyperliquidAPI


@pytest.fixture
def api():
    config = {
        'api': {'base_url': 'https://api.hyperliquid.xyz',
                'private_key': '0xabc', 'wallet_address': '0x123'},
        'hip3': {'enabled': False, 'perp_dexs': []},
    }
    with patch('hyperliquid.info.Info'):
        with patch.object(HyperliquidAPI, '_discover_perp_dexs', return_value=['']):
            api = HyperliquidAPI(config)
    api.exchange = MagicMock()
    api.exchange.update_leverage.return_value = {'status': 'ok'}
    api._rate_limited_call = lambda fn, *a, **kw: fn(*a)
    api._get_asset_info_for_symbol = MagicMock(return_value={})
    api.cache = MagicMock()
    return api


class TestLeverageClamp:
    def test_zero_clamped_to_one(self, api):
        """Live failure 2026-07-03: int(0.7) -> 0 -> exchange 422."""
        assert api.update_leverage('ZEC', 0) is True
        api.exchange.update_leverage.assert_called_once_with(1, 'ZEC', True)

    def test_fractional_clamped_to_one(self, api):
        assert api.update_leverage('ZEC', 0.7) is True
        api.exchange.update_leverage.assert_called_once_with(1, 'ZEC', True)

    def test_normal_leverage_truncates_as_before(self, api):
        assert api.update_leverage('BTC', 2.7) is True
        api.exchange.update_leverage.assert_called_once_with(2, 'BTC', True)

    def test_negative_clamped_to_one(self, api):
        assert api.update_leverage('BTC', -3) is True
        api.exchange.update_leverage.assert_called_once_with(1, 'BTC', True)


class TestCrossMarginFallback:
    def test_cross_rejection_retries_isolated(self, api):
        """Live failure 2026-07-03: xyz:INTC rejects cross margin but has
        no onlyIsolated flag in cached metadata."""
        api.exchange.update_leverage.side_effect = [
            {'status': 'err', 'response': 'Cross margin is not allowed for this asset.'},
            {'status': 'ok'},
        ]

        assert api.update_leverage('xyz:INTC', 1, is_cross=True) is True

        calls = api.exchange.update_leverage.call_args_list
        assert len(calls) == 2
        assert calls[0][0] == (1, 'xyz:INTC', True)
        assert calls[1][0] == (1, 'xyz:INTC', False)

    def test_other_errors_do_not_retry(self, api):
        api.exchange.update_leverage.return_value = {
            'status': 'err', 'response': 'Some other failure'}

        assert api.update_leverage('BTC', 2) is False
        assert api.exchange.update_leverage.call_count == 1

    def test_isolated_request_failing_isolated_does_not_loop(self, api):
        """If the caller already asked for isolated, a cross-margin error
        (nonsensical but defensive) must not trigger the retry path."""
        api.exchange.update_leverage.return_value = {
            'status': 'err', 'response': 'Cross margin is not allowed for this asset.'}

        assert api.update_leverage('xyz:INTC', 1, is_cross=False) is False
        assert api.exchange.update_leverage.call_count == 1
