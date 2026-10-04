"""Weekly cross-sectional momentum (research round 9) + its engine hooks.

Covers: weekly rebalance gating on CLOSED bars, quintile entries, rank-band
exits only at the rebalance, vol-scaled catastrophe stop, no trailing/TP,
the engine's take-profit opt-out (the capital-based TP truncated csm
winners even though csm "disabled" TP), stop_pct pass-through for sizing,
and per-strategy position-cap overrides.
"""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from src.strategies.weekly_momentum_strategy import WeeklyMomentumStrategy

MONDAY_00 = pd.Timestamp('2026-01-12 00:00')  # a Monday
TUESDAY_00 = pd.Timestamp('2026-01-13 00:00')


def make_strategy(sim_time=None, **overrides):
    cfg = {'lookback_days': 21, 'top_n_percent': 0.20, 'bottom_n_percent': 0.20,
           'hold_percent': 0.40, 'min_universe': 10}
    cfg.update(overrides)
    api = SimpleNamespace(current_time=sim_time) if sim_time is not None else None
    return WeeklyMomentumStrategy({'strategies': {'ohlcv_limit': 300, 'weekly_momentum': cfg}},
                                  timeframe='4h', market_api=api)


def bars(last_start, drift=0.0, n=200, seed=1, vol=0.01):
    rng = np.random.default_rng(seed)
    prices = 100 * np.cumprod(1 + drift + rng.normal(0, vol, n))
    idx = pd.date_range(end=last_start, periods=n, freq='4h')
    return pd.DataFrame({'open': prices, 'high': prices, 'low': prices, 'close': prices,
                         'volume': 1000.0}, index=idx)


def seed_peers(bar_ts, scores):
    WeeklyMomentumStrategy._universe_stats = {
        f"P{i}": {'score': s, 'return': s, 'bar_vol': 0.01, 'bar_ts': pd.Timestamp(bar_ts)}
        for i, s in enumerate(scores)
    }


@pytest.fixture(autouse=True)
def _clean():
    WeeklyMomentumStrategy._universe_stats = {}
    yield
    WeeklyMomentumStrategy._universe_stats = {}


def _closes_at(bar_start):
    """Sim time just after `bar_start`'s 4h bar closed."""
    return bar_start + pd.Timedelta(hours=4, minutes=15)


# --- construction ------------------------------------------------------------

def test_bar_math_and_flags():
    s = make_strategy()
    assert s.lookback_bars == 126 and s.week_bars == 42
    assert s.USES_ENGINE_TAKE_PROFIT is False
    assert s.get_trailing_stop_config()['enabled'] is False
    assert s.calculate_take_profit(100.0, 'long') == 0.0
    assert s.needs_exit_data() is True


def test_settings_define_strategy_but_do_not_enable_it():
    from src.config.settings import load_config
    cfg = load_config()
    assert cfg['strategies']['weekly_momentum']['lookback_days'] == 21
    assert not any(i['type'] == 'weekly_momentum' for i in cfg['strategies']['instances'])
    assert cfg['trading']['max_positions_per_strategy_overrides']['wmom_4h'] == 14


def test_registered_with_strategy_manager():
    from src.strategies.strategy_manager import STRATEGY_CLASSES
    assert STRATEGY_CLASSES['weekly_momentum'] == ('weekly_momentum_strategy', 'WeeklyMomentumStrategy')


# --- rebalance gating ----------------------------------------------------------

def test_no_signal_off_rebalance_bar():
    s = make_strategy(sim_time=_closes_at(TUESDAY_00))
    seed_peers(TUESDAY_00, [-1.0] * 20)
    assert s.generate_signal('WIN', {'4h': bars(TUESDAY_00, drift=0.01)}) is None
    # ...but stats are still refreshed every bar
    assert WeeklyMomentumStrategy._universe_stats['WIN']['bar_ts'] == TUESDAY_00


def test_forming_bar_is_dropped():
    """Live OHLCV includes the forming bar: at Mon 02:00 the Mon 00:00 bar
    has not closed, so the latest CLOSED bar is Sun 20:00 — no rebalance."""
    s = make_strategy(sim_time=MONDAY_00 + pd.Timedelta(hours=2))
    seed_peers(MONDAY_00, [-1.0] * 20)
    assert s.generate_signal('WIN', {'4h': bars(MONDAY_00, drift=0.01)}) is None
    assert WeeklyMomentumStrategy._universe_stats['WIN']['bar_ts'] == MONDAY_00 - pd.Timedelta(hours=4)


# --- entries ---------------------------------------------------------------------

def test_top_quintile_long_with_vol_scaled_stop():
    s = make_strategy(sim_time=_closes_at(MONDAY_00))
    seed_peers(MONDAY_00, [-1.0] * 20)
    sig = s.generate_signal('WIN', {'4h': bars(MONDAY_00, drift=0.01)})
    assert sig['signal'] == 'buy'
    assert sig['confidence'] == s.signal_strength_value
    stats = WeeklyMomentumStrategy._universe_stats['WIN']
    expected = min(0.35, max(0.08, 2.0 * stats['bar_vol'] * np.sqrt(42)))
    assert sig['stop_pct'] == pytest.approx(expected)


def test_bottom_quintile_short_and_middle_none():
    s = make_strategy(sim_time=_closes_at(MONDAY_00))
    seed_peers(MONDAY_00, [5.0] * 20)
    assert s.generate_signal('LOSER', {'4h': bars(MONDAY_00, drift=-0.01)})['signal'] == 'sell'

    seed_peers(MONDAY_00, list(np.linspace(-1, 1, 20)))
    assert s.generate_signal('MID', {'4h': bars(MONDAY_00, drift=0.0, vol=0.0001)}) is None


def test_direction_filter():
    s = make_strategy(sim_time=_closes_at(MONDAY_00), direction='long')
    seed_peers(MONDAY_00, [5.0] * 20)
    assert s.generate_signal('LOSER', {'4h': bars(MONDAY_00, drift=-0.01)}) is None


def test_rank_uses_latest_peer_scores_even_if_one_bar_old():
    """Regression (smoke test 2026-10-04): the manager evaluates symbols one
    by one per closed bar; requiring all peers on the same bar lost ~half
    the universe's signals at the rebalance."""
    s = make_strategy(sim_time=_closes_at(MONDAY_00))
    seed_peers(MONDAY_00 - pd.Timedelta(hours=4), [-1.0] * 20)  # peers not yet refreshed
    sig = s.generate_signal('WIN', {'4h': bars(MONDAY_00, drift=0.01)})
    assert sig is not None and sig['signal'] == 'buy'


def test_stale_peers_older_than_a_week_excluded():
    s = make_strategy(sim_time=_closes_at(MONDAY_00))
    seed_peers(MONDAY_00 - pd.Timedelta(days=8), [-1.0] * 20)
    assert s.generate_signal('WIN', {'4h': bars(MONDAY_00, drift=0.01)}) is None  # < min_universe


def test_insufficient_history_no_signal():
    s = make_strategy(sim_time=_closes_at(MONDAY_00))
    seed_peers(MONDAY_00, [-1.0] * 20)
    assert s.generate_signal('NEW', {'4h': bars(MONDAY_00, drift=0.01, n=100)}) is None


# --- stops -----------------------------------------------------------------------

def test_stop_clamps_and_fallback():
    s = make_strategy()
    assert s._stop_pct(None) == s.fallback_stop_pct
    assert s._stop_pct(0.0001) == 0.08   # floor
    assert s._stop_pct(0.2) == 0.35      # cap
    assert s.calculate_stop_loss(100.0, 'long', {'stop_pct': 0.1}) == pytest.approx(90.0)
    assert s.calculate_stop_loss(100.0, 'short', {'stop_pct': 0.1}) == pytest.approx(110.0)
    assert s.calculate_stop_loss(100.0, 'long', {}) == pytest.approx(85.0)


# --- exits -----------------------------------------------------------------------

def _pos(symbol, side):
    return SimpleNamespace(symbol=symbol, side=side)


def test_rank_exit_only_at_rebalance():
    s = make_strategy(sim_time=_closes_at(TUESDAY_00))
    seed_peers(TUESDAY_00, list(range(20)))
    WeeklyMomentumStrategy._universe_stats['P0']['score'] = -99  # bottom rank
    assert s.should_exit(_pos('P0', 'long'), 1.0, {}) == (False, None)  # Tuesday: hold


def test_long_exits_when_outside_hold_band_at_rebalance():
    s = make_strategy(sim_time=_closes_at(MONDAY_00))
    seed_peers(MONDAY_00, list(range(20)))  # P0 lowest ... P19 highest
    hit, reason = s.should_exit(_pos('P5', 'long'), 1.0, {})   # rank 0.30 < 0.60
    assert hit and reason.startswith('weekly_rank_exit')
    assert s.should_exit(_pos('P15', 'long'), 1.0, {}) == (False, None)  # rank 0.80


def test_short_band():
    s = make_strategy(sim_time=_closes_at(MONDAY_00))
    seed_peers(MONDAY_00, list(range(20)))
    assert s.should_exit(_pos('P5', 'short'), 1.0, {}) == (False, None)  # 0.30 <= 0.40
    assert s.should_exit(_pos('P12', 'short'), 1.0, {})[0] is True       # 0.65 > 0.40


def test_exit_refreshes_held_symbol_from_ohlcv():
    s = make_strategy(sim_time=_closes_at(MONDAY_00))
    seed_peers(MONDAY_00, [0.0] * 19)
    WeeklyMomentumStrategy._universe_stats['HELD'] = {
        'score': 9.9, 'return': 9.9, 'bar_vol': 0.01, 'bar_ts': MONDAY_00 - pd.Timedelta(days=7)}
    hit, _ = s.should_exit(_pos('HELD', 'long'), 1.0, {'ohlcv': bars(MONDAY_00, drift=-0.01)})
    assert hit
    assert WeeklyMomentumStrategy._universe_stats['HELD']['bar_ts'] == MONDAY_00


# --- engine hooks ------------------------------------------------------------------

@pytest.fixture
def engine(mock_config, mock_market_api):
    from src.strategies.execution_engine import ExecutionEngine
    ee = ExecutionEngine(mock_config, mock_market_api, MagicMock(), MagicMock(), MagicMock(), MagicMock())
    ee.leverage_manager.calculate_stop_loss_with_leverage.return_value = 98.0
    ee.leverage_manager.calculate_take_profit_with_leverage.return_value = 104.0
    ee.leverage_manager.calculate_take_profit_with_capital_at_risk.return_value = 104.0
    ee.leverage_manager.calculate_leveraged_position_size.return_value = (10.0, 500.0, 2.0)
    ee.portfolio_manager.total_equity = 50000.0
    mock_market_api.execute_order.return_value = {
        'order_id': 7, 'status': 'filled', 'filled_size': 10.0, 'avg_fill_price': 100.0}
    return ee


def _signal(**extra):
    sig = {'signal': 'buy', 'side': 'buy', 'size': 10.0, 'leverage': 2.0, 'margin_required': 500.0,
           'signal_strength': 0.8, 'market_volatility': 0.5}
    sig.update(extra)
    return sig


def test_engine_skips_take_profit_for_opted_out_strategy(engine):
    strat = make_strategy()
    engine.execute_trade('SOL', _signal(stop_pct=0.12), 100.0, 'wmom_4h', {}, {'wmom_4h': strat})
    pos = engine.positions['SOL']
    assert pos.take_profit is None
    assert pos.stop_loss == pytest.approx(88.0)


def test_engine_passes_stop_pct_to_sizing(engine):
    strat = make_strategy()
    engine.execute_trade('SOL', _signal(stop_pct=0.12), 100.0, 'wmom_4h', {}, {'wmom_4h': strat})
    kwargs = engine.leverage_manager.calculate_leveraged_position_size.call_args.kwargs
    assert kwargs['stop_loss_pct'] == pytest.approx(0.12)


def test_engine_keeps_take_profit_for_other_strategies(engine):
    other = MagicMock()
    other.USES_ENGINE_TAKE_PROFIT = True
    other.calculate_stop_loss.return_value = 95.0
    other.calculate_take_profit.return_value = 0.0
    other.get_trailing_stop_config.return_value = {'enabled': False}
    engine.execute_trade('SOL', _signal(), 100.0, 'x', {}, {'x': other})
    assert engine.positions['SOL'].take_profit == pytest.approx(105.0)  # 1:1 R:R expansion of 104


def test_per_strategy_position_cap_override():
    from src.strategies.strategy_manager import StrategyManager
    mgr = StrategyManager.__new__(StrategyManager)
    mgr.max_positions_per_strategy = 5
    mgr.max_positions_per_strategy_overrides = {'wmom_4h': 14}
    assert mgr._strategy_position_limit('wmom_4h') == 14
    assert mgr._strategy_position_limit('csm_4h') == 5
    assert mgr._strategy_position_limit(None) == 5
