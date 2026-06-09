"""
Tests for once-per-closed-bar signal gating in StrategyManager:
- _has_new_closed_bar: entry signals are evaluated once per closed candle
- _symbol_has_open_position: symbols with open positions bypass the gate
  (exit logic must never be throttled)
"""

import pandas as pd
import pytest
from unittest.mock import MagicMock, patch


class DummyStrategy:
    TIMEFRAME_MINUTES = {'1m': 1, '5m': 5, '15m': 15, '30m': 30,
                         '1h': 60, '4h': 240, '1d': 1440}
    timeframe = '4h'


def make_manager():
    from src.strategies.strategy_manager import StrategyManager

    with patch.object(StrategyManager, '__init__', lambda self, *a, **k: None):
        manager = StrategyManager.__new__(StrategyManager)

    manager.logger = MagicMock()
    manager._last_signal_bar = {}
    manager.execution_engine = MagicMock()
    manager.execution_engine.positions = {}
    manager.execution_engine.multi_leg_positions = {}
    return manager


def make_ohlcv(periods=50, end_offset_hours=8, freq='4h'):
    """Closed 4h bars ending `end_offset_hours` in the past."""
    end = (pd.Timestamp.utcnow().tz_localize(None).floor('4h')
           - pd.Timedelta(hours=end_offset_hours))
    idx = pd.date_range(end=end, periods=periods, freq=freq)
    return pd.DataFrame({'close': range(periods)}, index=idx, dtype=float)


class TestHasNewClosedBar:

    def test_first_evaluation_passes(self):
        manager = make_manager()
        ohlcv = {'4h': make_ohlcv()}

        assert manager._has_new_closed_bar('BTC', 'vol_breakout_4h', DummyStrategy(), ohlcv) is True

    def test_same_bar_is_gated(self):
        manager = make_manager()
        ohlcv = {'4h': make_ohlcv()}
        strategy = DummyStrategy()

        assert manager._has_new_closed_bar('BTC', 'vol_breakout_4h', strategy, ohlcv) is True
        assert manager._has_new_closed_bar('BTC', 'vol_breakout_4h', strategy, ohlcv) is False
        assert manager._has_new_closed_bar('BTC', 'vol_breakout_4h', strategy, ohlcv) is False

    def test_new_bar_resets_gate(self):
        manager = make_manager()
        strategy = DummyStrategy()

        df = make_ohlcv(end_offset_hours=12)
        assert manager._has_new_closed_bar('BTC', 'vol_breakout_4h', strategy, {'4h': df}) is True

        # A new closed candle appears
        df2 = make_ohlcv(end_offset_hours=8)
        assert manager._has_new_closed_bar('BTC', 'vol_breakout_4h', strategy, {'4h': df2}) is True

    def test_forming_bar_does_not_retrigger(self):
        """
        Live cache includes the forming bar as the last row; ticks update it
        continuously. The gate must key on the last CLOSED bar, so a fresh
        tick on the forming bar must not re-trigger evaluation.
        """
        manager = make_manager()
        strategy = DummyStrategy()

        closed = make_ohlcv(end_offset_hours=4)
        assert manager._has_new_closed_bar('BTC', 'vol_breakout_4h', strategy, {'4h': closed}) is True

        # Same closed bars + the currently forming bar appended
        forming_idx = closed.index[-1] + pd.Timedelta(hours=4)
        with_forming = pd.concat([
            closed,
            pd.DataFrame({'close': [999.0]}, index=[forming_idx])
        ])
        assert manager._has_new_closed_bar('BTC', 'vol_breakout_4h', strategy, {'4h': with_forming}) is False

    def test_gate_is_per_symbol_and_strategy(self):
        manager = make_manager()
        strategy = DummyStrategy()
        ohlcv = {'4h': make_ohlcv()}

        assert manager._has_new_closed_bar('BTC', 'vol_breakout_4h', strategy, ohlcv) is True
        assert manager._has_new_closed_bar('ETH', 'vol_breakout_4h', strategy, ohlcv) is True
        assert manager._has_new_closed_bar('BTC', 'other_strategy', strategy, ohlcv) is True
        assert manager._has_new_closed_bar('BTC', 'vol_breakout_4h', strategy, ohlcv) is False

    def test_fails_open_without_data(self):
        """Missing/empty data must never block evaluation."""
        manager = make_manager()
        strategy = DummyStrategy()

        assert manager._has_new_closed_bar('BTC', 's', strategy, {}) is True
        assert manager._has_new_closed_bar('BTC', 's', strategy, {'4h': pd.DataFrame()}) is True


class TestSymbolHasOpenPosition:

    def test_single_leg_position_detected(self):
        manager = make_manager()
        manager.execution_engine.positions = {'BTC': MagicMock()}

        assert manager._symbol_has_open_position('BTC') is True
        assert manager._symbol_has_open_position('ETH') is False

    def test_multi_leg_position_detected(self):
        manager = make_manager()
        ml_pos = MagicMock()
        ml_pos.primary_symbol = 'ETH'
        manager.execution_engine.multi_leg_positions = {'ml_1': ml_pos}

        assert manager._symbol_has_open_position('ETH') is True
        assert manager._symbol_has_open_position('SOL') is False
