"""Point-in-time universe, coarse bar resolution and CLI helpers in scripts/run_backtest.py.

Research round 8 (2026-10-03): select_universe ranked symbols by notional
volume INSIDE the test window and required in-window coverage — a look-ahead
that stocks each month's universe with that month's biggest movers (volume
spikes with large moves, which is what momentum profits from) and drops
names delisted mid-window.
"""
import importlib.util
import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pandas as pd
import pytest

_spec = importlib.util.spec_from_file_location(
    "run_backtest_universe",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "run_backtest.py"),
)
rb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rb)

START = datetime(2026, 3, 1)
END = datetime(2026, 4, 1)


def _series(start, end, notional_fn):
    idx = pd.date_range(start, end, freq='4h', inclusive='left')
    close = pd.Series(1.0, index=idx)
    volume = pd.Series([notional_fn(ts) for ts in idx], index=idx)
    return pd.DataFrame({'close': close, 'volume': volume})


class FakeDB:
    def __init__(self, data):
        self.data = data

    def get_market_data(self, sym, tf):
        return self.data.get(sym, pd.DataFrame())


def _db():
    pre = START - timedelta(days=30)
    return FakeDB({
        # Steady liquid name, trades before and during the window
        'STEADY': _series(pre, END, lambda ts: 1000.0),
        # Quiet before the window, explodes during it (the biased pick)
        'PUMPER': _series(pre, END, lambda ts: 100.0 if ts < START else 50_000.0),
        # Liquid before the window, delisted 5 days into it
        'DELISTED': _series(pre, START + timedelta(days=5), lambda ts: 800.0),
        # Listed only mid-window: unknowable at window start
        'NEWLIST': _series(START + timedelta(days=10), END, lambda ts: 90_000.0),
    })


def test_ranking_window_modes():
    assert rb.universe_ranking_window(START, END, 'pit', 30) == (START - timedelta(days=30), START)
    assert rb.universe_ranking_window(START, END, 'window') == (START, END)
    with pytest.raises(ValueError):
        rb.universe_ranking_window(START, END, 'future')


def test_pit_universe_uses_only_pre_window_liquidity():
    selected = rb.select_universe(_db(), ['STEADY', 'PUMPER', 'DELISTED', 'NEWLIST'],
                                  START, END, max_n=2, mode='pit', lookback_days=30)
    # PUMPER's in-window volume and NEWLIST's mid-window listing are invisible
    assert selected == ['STEADY', 'DELISTED']


def test_window_mode_reproduces_legacy_lookahead():
    selected = rb.select_universe(_db(), ['STEADY', 'PUMPER', 'DELISTED', 'NEWLIST'],
                                  START, END, max_n=2, mode='window')
    # legacy: picks the in-window pumper, drops the name delisted mid-window
    assert selected[0] == 'PUMPER'
    assert 'DELISTED' not in selected


def test_pit_excludes_bar_at_window_start():
    """The bar starting exactly at window start closes inside the window."""
    pre = START - timedelta(days=30)
    db = FakeDB({
        'A': _series(pre, START + timedelta(hours=4), lambda ts: 1e9 if ts == START else 10.0),
        'B': _series(pre, START, lambda ts: 20.0),
    })
    assert rb.select_universe(db, ['A', 'B'], START, END, max_n=1, mode='pit') == ['B']


def test_native_bar_resolution_keeps_legacy_requirements():
    config = {'backtesting': {}}
    assert rb.apply_bar_resolution(config, ['4h'], 'native') == {'4h', '1h'}
    assert 'exclude_timeframes' not in config['backtesting']


def test_coarse_bar_resolution_excludes_fine_timeframes():
    config = {'backtesting': {}}
    req = rb.apply_bar_resolution(config, ['4h', '1h'], '4h', active_timeframes=['4h'])
    assert req == {'4h'}
    assert config['backtesting']['exclude_timeframes'] == ['5m', '15m', '1h']


def test_coarse_bar_resolution_rejects_sub_4h_strategies():
    with pytest.raises(ValueError, match='1h'):
        rb.apply_bar_resolution({'backtesting': {}}, ['4h'], '4h', active_timeframes=['4h', '1h'])


def test_generic_overrides_any_section():
    config = {'strategy_selection': {'win_rate_strength_modifier': True}}
    rb.apply_generic_overrides(config, [
        'strategy_selection.win_rate_strength_modifier=false',
        'backtesting.fee_bps=1.5',
        'no-section-here',
    ])
    assert config['strategy_selection']['win_rate_strength_modifier'] is False
    assert config['backtesting']['fee_bps'] == 1.5


def test_result_record_shape():
    rec = rb.result_record(
        {'total_equity': 51234.567, 'backtest_trades': 42, 'backtest_total_pnl': 1234.567,
         'backtest_win_rate': 45.0, 'backtest_profit_factor': 1.2345, 'backtest_max_drawdown_pct': 7.1,
         'funding_paid': 12.3},
        START, END, extra={'tag': 'x'})
    assert rec == {'start': '2026-03-01', 'end': '2026-04-01', 'final_equity': 51234.57, 'trades': 42,
                   'pnl': 1234.57, 'win_rate': 45.0, 'profit_factor': 1.234, 'max_dd_pct': 7.1,
                   'funding_paid': 12.3, 'tag': 'x'}


# --- BacktestEngine coarse-mode loading -------------------------------------

def _bare_engine(config):
    from src.backtesting.backtest_engine import BacktestEngine
    engine = BacktestEngine.__new__(BacktestEngine)
    engine.config = config
    engine.logger = MagicMock()
    engine.db = MagicMock()
    engine.db.get_market_data_symbols.side_effect = lambda tf: {'1h': ['BTC'], '4h': ['BTC', 'MATIC']}[tf]
    engine.db.get_market_data.side_effect = lambda sym, tf: pd.DataFrame(
        {'open': [1.0], 'high': [1.0], 'low': [1.0], 'close': [1.0], 'volume': [1.0]},
        index=pd.to_datetime(['2024-08-01']))
    return engine


def test_engine_native_loads_all_timeframes_keyed_off_1h():
    engine = _bare_engine({'backtesting': {}})
    data = engine._load_data_from_db()
    assert list(data) == ['BTC']
    assert set(data['BTC']) == {'5m', '15m', '1h', '4h', '1d'}


def test_engine_coarse_loads_4h_symbols_without_fine_timeframes():
    engine = _bare_engine({'backtesting': {'exclude_timeframes': ['5m', '15m', '1h']}})
    data = engine._load_data_from_db()
    # delisted MATIC only has 4h history and must still be discoverable
    assert sorted(data) == ['BTC', 'MATIC']
    assert set(data['MATIC']) == {'4h', '1d'}
