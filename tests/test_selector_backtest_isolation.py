"""StrategySelector: backtest isolation from the live DB + win-rate modifier toggle.

Research round 8 (2026-10-03): every backtest seeded the selector's
performance windows from LIVE trades in data/trades.db (csm_4h: January
trades from an older config plus June 2026 live fills), so the win-rate
signal-strength modifier at the start of e.g. a Nov-2025 window reflected
trades from the future, and results depended on the live DB's contents.
"""
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from src.strategies.strategy_selector import (
    MarketRegime,
    StrategyPerformanceWindow,
    StrategySelector,
)


def _make_db(path, n_live=10, live_pct=-0.01):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE trades (strategy TEXT, pnl REAL, pnl_percentage REAL, exit_time TEXT)")
    conn.execute("CREATE TABLE backtest_trades (strategy TEXT, pnl REAL, pnl_percentage REAL, exit_time TEXT)")
    now = datetime.now()
    for i in range(n_live):
        conn.execute("INSERT INTO trades VALUES ('csm_4h', ?, ?, ?)",
                     (-10.0, live_pct, (now - timedelta(minutes=i)).isoformat()))
    conn.commit()
    conn.close()


def _selector(config):
    sel = StrategySelector(MagicMock(), config)
    sel._registered_strategies = ['csm_4h']
    return sel


@pytest.mark.parametrize("config", [
    {'mode': 'backtest', 'backtesting': {'enabled': True}},
    {'backtesting': {'enabled': True}},
])
def test_backtest_does_not_seed_from_live_db(tmp_path, config):
    db = str(tmp_path / "trades.db")
    _make_db(db)
    sel = _selector(config)

    sel._initialize_strategies_from_history(live_db_path=db)

    assert sel.strategy_rankings['csm_4h'].metrics['total_trades'] == 0
    assert len(sel.performance_windows['csm_4h'].returns) == 0
    # 10 straight live losses would otherwise pin the modifier at 0.5
    assert sel.get_signal_strength_modifier('csm_4h') == 1.0


def test_backtest_seeding_can_be_opted_in(tmp_path):
    db = str(tmp_path / "trades.db")
    _make_db(db)
    sel = _selector({'mode': 'backtest', 'backtesting': {'enabled': True, 'seed_selector_from_db': True}})

    sel._initialize_strategies_from_history(live_db_path=db)

    assert sel.strategy_rankings['csm_4h'].metrics['total_trades'] == 10


def test_live_mode_still_seeds_from_db(tmp_path):
    db = str(tmp_path / "trades.db")
    _make_db(db)
    sel = _selector({'backtesting': {'enabled': False}})

    sel._initialize_strategies_from_history(live_db_path=db)

    assert sel.strategy_rankings['csm_4h'].metrics['total_trades'] == 10
    assert sel.get_signal_strength_modifier('csm_4h') == 0.5


def _losing_window():
    w = StrategyPerformanceWindow()
    for _ in range(8):
        w.add_return(-0.01, datetime.now(), MarketRegime.UNKNOWN)
    for _ in range(2):
        w.add_return(0.01, datetime.now(), MarketRegime.UNKNOWN)
    return w


def test_win_rate_modifier_default_on():
    sel = _selector({})
    sel.performance_windows['csm_4h'] = _losing_window()
    assert sel.get_signal_strength_modifier('csm_4h') == pytest.approx(0.7)


def test_win_rate_modifier_disabled_returns_neutral():
    sel = _selector({'strategy_selection': {'win_rate_strength_modifier': False}})
    sel.performance_windows['csm_4h'] = _losing_window()
    assert sel.get_signal_strength_modifier('csm_4h') == 1.0
