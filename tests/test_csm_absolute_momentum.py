"""
Tests for the CSM absolute-momentum (dual momentum) gate:
a top-decile RANK with a negative own-return must not produce a long when
the gate is on (Dec-2025 failure mode: longing assets that merely fell
slowest), and symmetrically for shorts.
"""

import numpy as np
import pandas as pd
import pytest

from src.strategies.cross_sectional_momentum_strategy import CrossSectionalMomentumStrategy


def make_strategy(**csm_overrides):
    base = {"lookback_period": 12, "top_n_percent": 0.15,
            "bottom_n_percent": 0.15, "adx_threshold": 0}
    base.update(csm_overrides)
    config = {"strategies": {"ohlcv_limit": 300,
                             "cross_sectional_momentum": base}}
    return CrossSectionalMomentumStrategy(config, timeframe='4h')


def make_two_phase_df(trend='up', recent_return=0.05, n=260, lookback=12):
    """
    Long directional phase (so the EMA200 filter sides with `trend`)
    followed by a `recent_return` move over the last `lookback` bars —
    this lets the lookback return point AGAINST the long-term trend
    (the exact configuration the gate exists for) while keeping the
    EMA filter satisfied. Noise keeps per-bar volatility realistic.
    """
    rng = np.random.default_rng(3)
    drift = 0.004 if trend == 'up' else -0.004
    phase1 = 100 * np.cumprod(1 + drift + rng.normal(0, 0.002, n - lookback))
    step = (1 + recent_return) ** (1 / lookback)
    phase2 = phase1[-1] * np.cumprod(np.full(lookback, step) + rng.normal(0, 0.002, lookback))
    prices = np.concatenate([phase1, phase2])

    end = pd.Timestamp('2026-01-15 00:00')  # hour % 4 == 0 -> rebalance slot
    idx = pd.date_range(end=end, periods=len(prices), freq='4h')
    return pd.DataFrame({
        'open': prices, 'high': prices * 1.01, 'low': prices * 0.99,
        'close': prices, 'volume': np.full(len(prices), 1000.0),
    }, index=idx)


def seed_universe(strategy, my_symbol, my_df, other_score, n_others=20):
    """Populate the shared ranking cache so our symbol's rank is pinned:
    `other_score` very negative -> we rank top; very positive -> bottom."""
    CrossSectionalMomentumStrategy._universe_stats = {}
    from datetime import datetime
    for i in range(n_others):
        CrossSectionalMomentumStrategy._universe_stats[f"ALT{i}"] = {
            'return': other_score, 'score': other_score,
            'timestamp': datetime.now(), 'volatility': 0.01,
        }
    return strategy.generate_signal(my_symbol, {'4h': my_df})


class TestAbsoluteMomentumGate:
    def test_gate_off_allows_long_with_negative_own_return(self):
        # Uptrend with a recent dip: rank pins top, EMA200 passes, own
        # return negative. Without the gate this long goes through.
        strat = make_strategy(require_absolute_momentum=0)
        sig = seed_universe(strat, 'BTC',
                            make_two_phase_df('up', recent_return=-0.03),
                            other_score=-1000.0)
        assert sig is not None and sig['signal'] == 'buy'

    def test_gate_blocks_long_with_negative_own_return(self):
        strat = make_strategy(require_absolute_momentum=1)
        sig = seed_universe(strat, 'BTC',
                            make_two_phase_df('up', recent_return=-0.03),
                            other_score=-1000.0)
        assert sig is None

    def test_gate_allows_long_with_positive_own_return(self):
        strat = make_strategy(require_absolute_momentum=1)
        sig = seed_universe(strat, 'BTC',
                            make_two_phase_df('up', recent_return=+0.05),
                            other_score=-1000.0)
        assert sig is not None and sig['signal'] == 'buy'

    def test_gate_blocks_short_with_positive_own_return(self):
        # Downtrend with a recent bounce: rank pins bottom, EMA200 passes
        # for shorts, own return positive -> gate must reject.
        strat = make_strategy(require_absolute_momentum=1)
        sig = seed_universe(strat, 'BTC',
                            make_two_phase_df('down', recent_return=+0.03),
                            other_score=+1000.0)
        assert sig is None

    def test_gate_allows_short_with_negative_own_return(self):
        strat = make_strategy(require_absolute_momentum=1)
        sig = seed_universe(strat, 'BTC',
                            make_two_phase_df('down', recent_return=-0.05),
                            other_score=+1000.0)
        assert sig is not None and sig['signal'] == 'sell'

    def test_min_abs_momentum_threshold(self):
        # Own return positive but below the configured floor -> rejected.
        strat = make_strategy(require_absolute_momentum=1, min_abs_momentum=0.5)
        sig = seed_universe(strat, 'BTC',
                            make_two_phase_df('up', recent_return=+0.05),
                            other_score=-1000.0)
        assert sig is None


class TestConfigurableStopLoss:
    def test_default_stop_loss(self):
        strat = make_strategy()
        assert strat.calculate_stop_loss(100.0, 'long') == pytest.approx(95.0)

    def test_overridden_stop_loss(self):
        strat = make_strategy(stop_loss_pct=0.03)
        assert strat.calculate_stop_loss(100.0, 'long') == pytest.approx(97.0)
        assert strat.calculate_stop_loss(100.0, 'short') == pytest.approx(103.0)
