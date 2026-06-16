"""Tests for exchange-native protective stop (trigger) orders.

Added after the 2026-06-11 deadlock froze the in-process exit loop for ~43h so
no stop fired. A reduce-only stop-market that rests on the exchange fires
independently of the bot, so it protects even when the bot hangs/crashes.

Covers:
- API place_stop_order builds the correct SDK trigger order_type + side/limit
- Position.stop_order_id round-trips through to_dict/from_dict
- ExecutionEngine places a native stop on entry (when enabled + stop set),
  stores its id, and cancels it on close
- gating: disabled config / no stop_loss / missing API support -> no-op
"""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.models.trade import Position
from src.api.hyperliquid_api import HyperliquidAPI
from src.strategies.execution_engine import ExecutionEngine
from datetime import datetime


# --------------------------- API layer ---------------------------

def make_api():
    api = HyperliquidAPI.__new__(HyperliquidAPI)
    api.logger = logging.getLogger('test_native_stop')
    api.exchange = MagicMock()
    api.order_tracker = MagicMock()
    api.cache = MagicMock()
    # Deterministic rounding/asset helpers
    api._get_asset_info_for_symbol = lambda s: {'szDecimals': 3}
    api._round_to_tick = lambda price, **kw: round(price, 5)
    api._rate_limited_call = lambda fn, *a, **k: fn(*a, **k)
    api._parse_order_response = lambda resp, *a, **k: {
        'order_id': 555, 'status': 'open', 'filled_size': 0, 'avg_fill_price': 0}
    return api


class TestPlaceStopOrderAPI:
    def test_sell_stop_for_long_builds_trigger_sl(self):
        api = make_api()
        api.place_stop_order('BTC', 'sell', 1.0, 49000.0)
        args, kwargs = api.exchange.order.call_args
        # exchange.order(name, is_buy, sz, limit_px, order_type, reduce_only=)
        name, is_buy, sz, limit_px, order_type = args
        assert name == 'BTC'
        assert is_buy is False                 # sell stop closes a long
        assert sz == 1.0
        assert order_type == {"trigger": {
            "triggerPx": 49000.0, "isMarket": True, "tpsl": "sl"}}
        assert kwargs['reduce_only'] is True
        # Sell stop limit must sit BELOW the trigger (willing to sell down).
        assert limit_px < 49000.0

    def test_buy_stop_for_short_limit_above_trigger(self):
        api = make_api()
        api.place_stop_order('XPL', 'buy', 203.0, 0.0653)
        args, kwargs = api.exchange.order.call_args
        name, is_buy, sz, limit_px, order_type = args
        assert is_buy is True                  # buy stop closes a short
        assert order_type['trigger']['tpsl'] == 'sl'
        assert order_type['trigger']['triggerPx'] == 0.0653
        assert limit_px > 0.0653               # willing to pay up to get filled

    def test_returns_parsed_result_with_order_id(self):
        api = make_api()
        result = api.place_stop_order('BTC', 'sell', 1.0, 49000.0)
        assert result['order_id'] == 555
        api.order_tracker.track.assert_called_once()

    def test_zero_rounded_size_is_rejected(self):
        api = make_api()
        api._get_asset_info_for_symbol = lambda s: {'szDecimals': 0}
        result = api.place_stop_order('BTC', 'sell', 0.4, 49000.0)  # rounds to 0
        assert result is None
        api.exchange.order.assert_not_called()

    def test_no_exchange_returns_none(self):
        api = make_api()
        api.exchange = None
        assert api.place_stop_order('BTC', 'sell', 1.0, 49000.0) is None


# --------------------------- Position model ---------------------------

class TestPositionStopOrderId:
    def test_round_trips_through_dict(self):
        pos = Position(symbol='BTC', side='long', entry_price=50000, size=1.0,
                       entry_time=datetime(2026, 6, 16), strategy='csm_4h',
                       stop_loss=49000, stop_order_id=777)
        restored = Position.from_dict(pos.to_dict())
        assert restored.stop_order_id == 777

    def test_defaults_to_none(self):
        pos = Position(symbol='BTC', side='long', entry_price=50000, size=1.0,
                       entry_time=datetime(2026, 6, 16), strategy='csm_4h')
        assert pos.stop_order_id is None


# --------------------------- ExecutionEngine ---------------------------

def make_engine(enabled=True):
    eng = ExecutionEngine.__new__(ExecutionEngine)
    eng.logger = logging.getLogger('test_native_stop_engine')
    eng.config = {'risk_management': {'native_stop_orders': {'enabled': enabled}}}
    eng.market_api = SimpleNamespace(
        place_stop_order=MagicMock(return_value={'order_id': 12345}),
        cancel_order=MagicMock(return_value=True),
    )
    return eng


def long_pos(stop=49000.0, oid=None):
    return Position(symbol='BTC', side='long', entry_price=50000, size=1.0,
                    entry_time=datetime(2026, 6, 16), strategy='csm_4h',
                    stop_loss=stop, stop_order_id=oid)


class TestEnginePlaceNativeStop:
    def test_places_and_stores_id_for_long(self):
        eng = make_engine(enabled=True)
        pos = long_pos()
        eng._place_native_stop(pos)
        eng.market_api.place_stop_order.assert_called_once()
        kwargs = eng.market_api.place_stop_order.call_args.kwargs
        assert kwargs['side'] == 'sell'          # closes a long
        assert kwargs['trigger_price'] == 49000.0
        assert kwargs['reduce_only'] is True
        assert pos.stop_order_id == 12345

    def test_short_uses_buy_stop(self):
        eng = make_engine(enabled=True)
        pos = Position(symbol='XPL', side='short', entry_price=0.0622, size=203,
                       entry_time=datetime(2026, 6, 16), strategy='csm_4h',
                       stop_loss=0.0653)
        eng._place_native_stop(pos)
        assert eng.market_api.place_stop_order.call_args.kwargs['side'] == 'buy'

    def test_disabled_does_nothing(self):
        eng = make_engine(enabled=False)
        pos = long_pos()
        eng._place_native_stop(pos)
        eng.market_api.place_stop_order.assert_not_called()
        assert pos.stop_order_id is None

    def test_no_stop_loss_does_nothing(self):
        eng = make_engine(enabled=True)
        pos = long_pos(stop=None)
        eng._place_native_stop(pos)
        eng.market_api.place_stop_order.assert_not_called()

    def test_api_without_support_is_noop(self):
        eng = make_engine(enabled=True)
        eng.market_api = SimpleNamespace()       # no place_stop_order attr
        pos = long_pos()
        eng._place_native_stop(pos)              # must not raise
        assert pos.stop_order_id is None

    def test_failed_placement_leaves_id_none(self):
        eng = make_engine(enabled=True)
        eng.market_api.place_stop_order = MagicMock(return_value=None)
        pos = long_pos()
        eng._place_native_stop(pos)
        assert pos.stop_order_id is None


class TestEngineCancelNativeStop:
    def test_cancels_and_clears_id(self):
        eng = make_engine(enabled=True)
        pos = long_pos(oid=999)
        eng._cancel_native_stop(pos)
        eng.market_api.cancel_order.assert_called_once_with('BTC', 999)
        assert pos.stop_order_id is None

    def test_no_id_is_noop(self):
        eng = make_engine(enabled=True)
        pos = long_pos(oid=None)
        eng._cancel_native_stop(pos)
        eng.market_api.cancel_order.assert_not_called()

    def test_cancel_error_still_clears_id(self):
        eng = make_engine(enabled=True)
        eng.market_api.cancel_order = MagicMock(side_effect=RuntimeError('boom'))
        pos = long_pos(oid=999)
        eng._cancel_native_stop(pos)             # must not raise
        assert pos.stop_order_id is None
