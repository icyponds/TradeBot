"""Tests for the explicit close-all-positions helpers.

Covers the bug fixed 2026-06-13: scripts/close_all_positions.py checked
`result.get('status') == 'ok'`, but place_order() returns 'filled'/'open'/
'pending' (and 'ok' only nested under raw_response). The old check reported
every successful close as a failure and exited non-zero. order_succeeded()
encodes the real contract; plan_close_order() builds the reduce-only order.
"""

import pytest

from src.utils.position_close import plan_close_order, order_succeeded


class TestPlanCloseOrder:
    def test_long_closes_with_sell(self):
        order = plan_close_order({'symbol': 'CRV', 'size': 55.5})
        assert order == {
            'symbol': 'CRV', 'side': 'sell', 'size': 55.5,
            'reduce_only': True, 'order_type': 'market',
        }

    def test_short_closes_with_buy(self):
        order = plan_close_order({'symbol': 'XPL', 'size': -203.0})
        assert order['side'] == 'buy'
        assert order['size'] == 203.0       # absolute value
        assert order['reduce_only'] is True
        assert order['order_type'] == 'market'

    def test_hip3_symbol_preserved(self):
        # Builder-dex symbols must pass through unchanged for correct routing.
        order = plan_close_order({'symbol': 'xyz:GOLD', 'size': -0.0031})
        assert order['symbol'] == 'xyz:GOLD'
        assert order['side'] == 'buy'

    def test_zero_size_returns_none(self):
        assert plan_close_order({'symbol': 'BTC', 'size': 0}) is None

    def test_missing_size_returns_none(self):
        assert plan_close_order({'symbol': 'BTC'}) is None

    def test_non_numeric_size_returns_none(self):
        assert plan_close_order({'symbol': 'BTC', 'size': None}) is None
        assert plan_close_order({'symbol': 'BTC', 'size': 'oops'}) is None


class TestOrderSucceeded:
    def test_filled_is_success(self):
        # Regression: the real place_order() success shape — top-level
        # status='filled', with 'ok' only nested under raw_response.
        result = {
            'order_id': 467568727083, 'symbol': 'CRV', 'status': 'filled',
            'filled_size': 55.5, 'avg_fill_price': 0.23963,
            'raw_response': {'status': 'ok'},
        }
        assert order_succeeded(result) is True

    def test_resting_open_is_success(self):
        assert order_succeeded({'status': 'open', 'order_id': 1}) is True

    def test_pending_is_success(self):
        assert order_succeeded({'status': 'pending'}) is True

    def test_none_is_failure(self):
        # place_order returns None on any error/rejection.
        assert order_succeeded(None) is False

    def test_legacy_ok_check_would_have_failed(self):
        # Documents the bug: place_order never returns top-level 'ok', so the
        # old `== 'ok'` check rejected genuine fills.
        result = {'status': 'filled', 'raw_response': {'status': 'ok'}}
        assert result.get('status') != 'ok'        # old check -> false failure
        assert order_succeeded(result) is True      # new check -> correct

    def test_error_status_is_failure(self):
        assert order_succeeded({'status': 'error'}) is False

    def test_non_dict_is_failure(self):
        assert order_succeeded("ok") is False
        assert order_succeeded(['filled']) is False
