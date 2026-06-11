"""
Tests for fixes shipped 2026-06-10/11 that lacked dedicated coverage:

1. _get_asset_info_for_symbol: HIP-3 entries are stored dex-prefixed
   ('xyz:GOLD'); the stripped-name lookup missed them and callers fell back
   to sz_decimals=2, rounding a 0.0031-GOLD order to a rejected $0 order
   (live failure 2026-06-10 22:15).
2. _candles_snapshot_smart: route by name_to_coin membership — SDK wrapper
   for known symbols, raw POST for unknown (spot ids, new HIP-3 listings),
   with no KeyError flowing through the retry wrapper.
3. _rate_limited_call: Hyperliquid's transient "(500, 'null')" answer logs
   a WARNING and does NOT record a circuit-breaker failure.
"""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.api.hyperliquid_api import HyperliquidAPI


def make_api():
    api = HyperliquidAPI.__new__(HyperliquidAPI)
    api.logger = logging.getLogger('test_routing')
    api.info = MagicMock()
    api._rate_limited_call = lambda fn, *a, **k: fn(*a)
    return api


class TestAssetInfoLookup:
    UNIVERSE = {'universe': [
        {'name': 'BTC', 'szDecimals': 5},
        {'name': 'xyz:GOLD', 'szDecimals': 4},
    ]}

    def test_prefixed_hip3_name_found_exactly(self):
        api = make_api()
        api.get_asset_info = lambda: self.UNIVERSE
        info = api._get_asset_info_for_symbol('xyz:GOLD')
        assert info is not None and info['szDecimals'] == 4

    def test_bare_name_fallback_still_works(self):
        api = make_api()
        api.get_asset_info = lambda: self.UNIVERSE
        # Numeric-id prefixed form falls back to the bare name
        info = api._get_asset_info_for_symbol('147:BTC')
        assert info is not None and info['szDecimals'] == 5

    def test_unknown_symbol_returns_none(self):
        api = make_api()
        api.get_asset_info = lambda: self.UNIVERSE
        assert api._get_asset_info_for_symbol('cash:SILVER') is None

    def test_no_asset_info_returns_none(self):
        api = make_api()
        api.get_asset_info = lambda: None
        assert api._get_asset_info_for_symbol('BTC') is None


class TestCandleRouting:
    def test_known_symbol_uses_sdk_wrapper(self):
        api = make_api()
        api.info.name_to_coin = {'BTC': 'BTC'}
        api.info.candles_snapshot.return_value = [{'t': 1}]
        out = api._candles_snapshot_smart('BTC', '1h', 0, 1000)
        assert out == [{'t': 1}]
        api.info.candles_snapshot.assert_called_once()
        api.info.post.assert_not_called()

    def test_unknown_symbol_routes_raw_without_keyerror(self):
        api = make_api()
        api.info.name_to_coin = {'BTC': 'BTC'}
        api.info.post.return_value = [{'t': 2}]
        out = api._candles_snapshot_smart('TRUMP_SPOT', '1h', 0, 1000)
        assert out == [{'t': 2}]
        api.info.candles_snapshot.assert_not_called()
        endpoint, payload = api.info.post.call_args[0]
        assert payload['req']['coin'] == 'TRUMP_SPOT'

    def test_missing_name_map_routes_raw(self):
        api = make_api()
        del api.info.name_to_coin  # MagicMock attr removal
        api.info.configure_mock(**{'post.return_value': []})
        api.info.name_to_coin = None  # getattr default path
        api2 = make_api()
        api2.info = SimpleNamespace(post=lambda *a: [{'t': 3}])  # no name_to_coin at all
        assert api2._candles_snapshot_smart('BTC', '1h', 0, 1)[0]['t'] == 3


class TestTransient500Null:
    def make_api_with_breaker(self):
        api = HyperliquidAPI.__new__(HyperliquidAPI)
        api.logger = logging.getLogger('test_500null')
        api.circuit_breaker = MagicMock()
        api.health_monitor = MagicMock()
        api.rate_limiter = SimpleNamespace(acquire=lambda *a, **k: True)
        api._is_retryable_error = lambda e: False
        return api

    def test_500_null_skips_breaker_failure(self):
        api = self.make_api_with_breaker()
        def boom():
            raise Exception("(500, 'null')")
        with pytest.raises(Exception):
            api._rate_limited_call(boom)
        api.circuit_breaker.record_failure.assert_not_called()

    def test_other_errors_still_record_failure(self):
        api = self.make_api_with_breaker()
        def boom():
            raise Exception("(503, 'unavailable')")
        with pytest.raises(Exception):
            api._rate_limited_call(boom)
        api.circuit_breaker.record_failure.assert_called_once()
