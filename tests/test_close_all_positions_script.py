"""The standalone flatten command must also cancel resting orders.

scripts/close_all_positions.py bypasses close_position (raw reduce-only market
orders), so without an explicit cancel the native protective stops would orphan
on the exchange and could fire against a later re-entry. This verifies the
script cancels resting orders after flattening.
"""

import os
import sys
import importlib.util
from unittest.mock import MagicMock, patch

import pytest

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      'scripts', 'close_all_positions.py')


def load_script():
    spec = importlib.util.spec_from_file_location('close_all_positions_script', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_api():
    api = MagicMock()
    api.test_connection.return_value = True
    api.get_positions.return_value = [
        {'symbol': 'XPL', 'size': -203.0, 'entry_price': 0.0622,
         'unrealized_pnl': -5.0},
    ]
    api.place_order.return_value = {'status': 'filled', 'filled_size': 203.0,
                                    'avg_fill_price': 0.088}
    api.cancel_all_orders.return_value = 1
    return api


def run_main(api, argv):
    mod = load_script()
    with patch.object(mod, 'load_config', return_value={'api': {}}), \
         patch.object(mod, 'HyperliquidAPI', return_value=api), \
         patch.object(sys, 'argv', argv), \
         patch('builtins.input', return_value='CLOSE ALL'):
        mod.main()


def test_flatten_then_cancels_resting_orders():
    api = make_api()
    run_main(api, ['close_all_positions.py'])
    # Closed the position AND cancelled resting orders (the native stop).
    api.place_order.assert_called_once()
    api.cancel_all_orders.assert_called_once()


def test_dry_run_does_not_close_or_cancel():
    api = make_api()
    run_main(api, ['close_all_positions.py', '--dry-run'])
    api.place_order.assert_not_called()
    api.cancel_all_orders.assert_not_called()
