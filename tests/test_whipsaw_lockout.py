"""
Tests for the market-level whipsaw lockout in StrategyManager:
- triggers after consecutive opposite >threshold daily BTC moves
- expires after lockout_days
- does not trigger on same-direction large moves or sub-threshold flips
- disabled by default; per-cycle result caching
"""

from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pandas as pd

from src.strategies.strategy_manager import StrategyManager


def make_btc_4h(daily_rets, end_day='2026-01-15'):
    """4h closes implementing the given sequence of daily returns."""
    closes = []
    px = 100.0
    for r in daily_rets:
        # 6 bars/day, ramp linearly to the day's closing return
        for i in range(1, 7):
            closes.append(px * (1 + r * i / 6))
        px *= (1 + r)
    idx = pd.date_range(end=pd.Timestamp(end_day) + pd.Timedelta(hours=20),
                        periods=len(closes), freq='4h')
    return pd.DataFrame({'close': closes, 'open': closes, 'high': closes,
                         'low': closes, 'volume': 1000.0}, index=idx)


def make_manager(df, now, enabled=True, threshold=3.0, lockout_days=10.0):
    mgr = StrategyManager.__new__(StrategyManager)
    mgr.config = {"risk_management": {"whipsaw_lockout": {
        "enabled": enabled, "threshold_pct": threshold,
        "lockout_days": lockout_days, "ref_symbol": "BTC",
    }}}
    mgr.market_api = SimpleNamespace(get_ohlcv=lambda *a, **k: df)
    mgr._cycle_timestamp = now
    import logging
    mgr.logger = logging.getLogger("test_wl")
    return mgr


FLAT = [0.001, -0.001] * 7


class TestWhipsawLockout:
    def test_triggers_on_opposite_big_moves(self):
        # ... flat, -4.5%, +5.8%, flat -> flip on the last big day
        df = make_btc_4h(FLAT + [-0.045, 0.058, 0.001])
        now = df.index[-1].to_pydatetime()
        assert make_manager(df, now)._whipsaw_lockout_active()

    def test_expires_after_lockout_days(self):
        df = make_btc_4h(FLAT + [-0.045, 0.058] + [0.001] * 12)
        now = df.index[-1].to_pydatetime()  # flip ~12 days ago
        assert not make_manager(df, now, lockout_days=10)._whipsaw_lockout_active()

    def test_same_direction_moves_do_not_trigger(self):
        df = make_btc_4h(FLAT + [-0.045, -0.058, 0.001])
        now = df.index[-1].to_pydatetime()
        assert not make_manager(df, now)._whipsaw_lockout_active()

    def test_subthreshold_flip_does_not_trigger(self):
        df = make_btc_4h(FLAT + [-0.02, 0.025, 0.001])
        now = df.index[-1].to_pydatetime()
        assert not make_manager(df, now)._whipsaw_lockout_active()

    def test_disabled_never_triggers(self):
        df = make_btc_4h(FLAT + [-0.045, 0.058, 0.001])
        now = df.index[-1].to_pydatetime()
        assert not make_manager(df, now, enabled=False)._whipsaw_lockout_active()

    def test_result_cached_per_cycle(self):
        df = make_btc_4h(FLAT + [-0.045, 0.058, 0.001])
        now = df.index[-1].to_pydatetime()
        calls = []
        mgr = make_manager(df, now)
        orig_api = mgr.market_api
        mgr.market_api = SimpleNamespace(
            get_ohlcv=lambda *a, **k: (calls.append(1), df)[1])
        assert mgr._whipsaw_lockout_active()
        assert mgr._whipsaw_lockout_active()
        assert len(calls) == 1

    def test_no_data_fails_open(self):
        mgr = make_manager(None, datetime(2026, 1, 15))
        assert not mgr._whipsaw_lockout_active()
