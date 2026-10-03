"""csm research-round-8 features: rank-decay exit, blended lookbacks, funding filter.

All default OFF (the live csm_4h config must be unchanged until validated):
- exit_rank_percent: pre-2026-10 csm had NO rank exit (base should_exit
  no-op) despite its docstring — positions ended only via stop/trail/flip
  and stale names kept the 5 position slots.
- lookback_periods: blended-horizon score.
- funding_filter_apr: skip longs into crowded positive funding (and shorts
  into crowded negative funding).
"""
from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from src.strategies.cross_sectional_momentum_strategy import CrossSectionalMomentumStrategy


def make_strategy(market_api=None, **csm_overrides):
    base = {"lookback_period": 12, "top_n_percent": 0.15,
            "bottom_n_percent": 0.15, "adx_threshold": 0}
    base.update(csm_overrides)
    config = {"strategies": {"ohlcv_limit": 300, "cross_sectional_momentum": base}}
    return CrossSectionalMomentumStrategy(config, timeframe='4h', market_api=market_api)


def trending_df(drift=0.004, n=260, seed=3):
    rng = np.random.default_rng(seed)
    prices = 100 * np.cumprod(1 + drift + rng.normal(0, 0.002, n))
    end = pd.Timestamp('2026-01-15 00:00')
    idx = pd.date_range(end=end, periods=n, freq='4h')
    return pd.DataFrame({'open': prices, 'high': prices * 1.01, 'low': prices * 0.99,
                         'close': prices, 'volume': np.full(n, 1000.0)}, index=idx)


def seed_universe(scores):
    CrossSectionalMomentumStrategy._universe_stats = {
        f"ALT{i}": {'return': s, 'score': s, 'timestamp': datetime.now(), 'volatility': 0.01}
        for i, s in enumerate(scores)
    }


@pytest.fixture(autouse=True)
def _clean_cache():
    CrossSectionalMomentumStrategy._universe_stats = {}
    yield
    CrossSectionalMomentumStrategy._universe_stats = {}


def _pos(symbol, side):
    return SimpleNamespace(symbol=symbol, side=side)


# --- defaults ---------------------------------------------------------------

def test_defaults_are_off_and_single_lookback_score_unchanged():
    s = make_strategy()
    assert s.exit_rank_percent == 0.0
    assert s.lookback_periods == []
    assert s.funding_filter_apr == 0.0
    assert not s.needs_exit_data()

    df = trending_df()
    stats = s._compute_stats(df)
    close = df['close']
    momentum = close.iloc[-1] / close.iloc[-12] - 1
    vol = close.iloc[-12:].pct_change().std()
    assert stats['return'] == pytest.approx(momentum)
    assert stats['score'] == pytest.approx(momentum / vol)


def test_rank_exit_disabled_never_exits():
    s = make_strategy()
    seed_universe([0.0] * 10 + [-5.0])
    CrossSectionalMomentumStrategy._universe_stats['ALT10']['score'] = -5.0
    assert s.should_exit(_pos('ALT10', 'long'), 1.0, {}) == (False, None)


# --- rank-decay exit ----------------------------------------------------------

def test_long_exits_when_rank_leaves_hold_band():
    s = make_strategy(exit_rank_percent=0.30)
    seed_universe(list(range(20)))  # ALT0 lowest ... ALT19 highest
    exit_low, reason = s.should_exit(_pos('ALT5', 'long'), 1.0, {})
    assert exit_low and reason.startswith('rank_decay')
    # rank of ALT15 = 16/20 = 0.80 >= 0.70 -> still in band
    assert s.should_exit(_pos('ALT15', 'long'), 1.0, {}) == (False, None)


def test_short_exits_when_rank_leaves_bottom_band():
    s = make_strategy(exit_rank_percent=0.30)
    seed_universe(list(range(20)))
    assert s.should_exit(_pos('ALT2', 'short'), 1.0, {}) == (False, None)  # rank 0.15
    hit, reason = s.should_exit(_pos('ALT12', 'short'), 1.0, {})         # rank 0.65
    assert hit and 'rank_decay' in reason


def test_reversal_mode_mirrors_hold_bands():
    s = make_strategy(exit_rank_percent=0.30, invert=1)
    seed_universe(list(range(20)))
    # reversal longs hold the BOTTOM tail
    assert s.should_exit(_pos('ALT2', 'long'), 1.0, {}) == (False, None)
    assert s.should_exit(_pos('ALT15', 'long'), 1.0, {})[0] is True


def test_rank_exit_recomputes_held_symbol_from_ohlcv():
    """A stale top score in the cache must not keep a decayed position open."""
    s = make_strategy(exit_rank_percent=0.30)
    seed_universe([0.0] * 19)
    CrossSectionalMomentumStrategy._universe_stats['HELD'] = {
        'return': 0.5, 'score': 99.0, 'timestamp': datetime.now(), 'volatility': 0.01}
    falling = trending_df(drift=-0.01)
    hit, reason = s.should_exit(_pos('HELD', 'long'), 1.0, {'ohlcv': falling})
    assert hit, reason
    assert CrossSectionalMomentumStrategy._universe_stats['HELD']['score'] < 0


def test_rank_exit_needs_minimum_universe():
    s = make_strategy(exit_rank_percent=0.30)
    seed_universe([1.0, 2.0, 3.0])
    assert s.should_exit(_pos('ALT0', 'long'), 1.0, {}) == (False, None)


def test_rank_exit_reports_needing_exit_data():
    assert make_strategy(exit_rank_percent=0.3).needs_exit_data()


# --- blended lookbacks --------------------------------------------------------

def test_blended_score_is_mean_of_horizon_tstats():
    s = make_strategy(lookback_periods=[12, 42, 126])
    df = trending_df()
    stats = s._compute_stats(df)
    close = df['close']
    vol = close.iloc[-126:].pct_change().std()
    zs = [(close.iloc[-1] / close.iloc[-k] - 1) / (vol * np.sqrt(k)) for k in (12, 42, 126)]
    rets = [close.iloc[-1] / close.iloc[-k] - 1 for k in (12, 42, 126)]
    assert stats['score'] == pytest.approx(np.mean(zs))
    assert stats['return'] == pytest.approx(np.mean(rets))


def test_blended_requires_longest_history():
    s = make_strategy(lookback_periods=[12, 200])
    assert s._compute_stats(trending_df(n=150)) is None


def test_blend_changes_ranking_when_horizons_disagree():
    """Short-term pop inside a long decline: single 12-bar lookback ranks it
    top, the blend (dominated by the long decline) does not."""
    rng = np.random.default_rng(1)
    decline = 100 * np.cumprod(1 - 0.004 + rng.normal(0, 0.002, 240))
    pop = decline[-1] * np.cumprod(np.full(20, 1.006))
    prices = np.concatenate([decline, pop])
    idx = pd.date_range(end=pd.Timestamp('2026-01-15'), periods=len(prices), freq='4h')
    df = pd.DataFrame({'open': prices, 'high': prices, 'low': prices, 'close': prices,
                       'volume': 1000.0}, index=idx)
    assert make_strategy()._compute_stats(df)['score'] > 0
    assert make_strategy(lookback_periods=[12, 42, 126])._compute_stats(df)['score'] < 0


# --- funding filter -----------------------------------------------------------

class FundingAPI:
    def __init__(self, hourly_rate):
        self.hourly_rate = hourly_rate
        self.current_time = pd.Timestamp('2026-01-15 00:00')
        self.calls = 0

    def get_funding_history(self, symbol, start_ms, end_ms):
        self.calls += 1
        return [{'coin': symbol, 'fundingRate': str(self.hourly_rate), 'time': start_ms + i * 3600_000}
                for i in range(24)]


def _top_ranked_long(strategy):
    seed_universe([-5.0] * 20)
    return strategy.generate_signal('PUMP', {'4h': trending_df(drift=0.006)})


def test_funding_filter_blocks_crowded_long():
    api = FundingAPI(hourly_rate=0.0001)  # 87.6% APR
    s = make_strategy(market_api=api, funding_filter_apr=0.30, require_absolute_momentum=1)
    assert _top_ranked_long(s) is None
    assert api.calls == 1


def test_funding_filter_allows_baseline_funding_long():
    api = FundingAPI(hourly_rate=0.0000125)  # ~11% APR baseline
    s = make_strategy(market_api=api, funding_filter_apr=0.30)
    sig = _top_ranked_long(s)
    assert sig is not None and sig['signal'] == 'buy'


def test_funding_filter_blocks_crowded_short():
    df = trending_df(drift=-0.006)
    seed_universe([5.0] * 20)
    unfiltered = make_strategy(market_api=FundingAPI(-0.0001)).generate_signal('DUMP', {'4h': df})
    assert unfiltered is not None and unfiltered['signal'] == 'sell'  # counterfactual

    s = make_strategy(market_api=FundingAPI(-0.0001), funding_filter_apr=0.30)
    seed_universe([5.0] * 20)
    assert s.generate_signal('DUMP', {'4h': df}) is None


def test_funding_filter_fails_open_without_api():
    s = make_strategy(funding_filter_apr=0.30)
    sig = _top_ranked_long(s)
    assert sig is not None and sig['signal'] == 'buy'


def test_funding_filter_off_by_default_does_not_query():
    api = FundingAPI(hourly_rate=0.001)
    s = make_strategy(market_api=api)
    assert _top_ranked_long(s)['signal'] == 'buy'
    assert api.calls == 0


def test_funding_lookup_cached_per_hour():
    api = FundingAPI(hourly_rate=0.0000125)
    s = make_strategy(market_api=api, funding_filter_apr=0.30)
    s._trailing_funding_apr('X')
    s._trailing_funding_apr('X')
    assert api.calls == 1
    assert s._trailing_funding_apr('X') == pytest.approx(0.0000125 * 8760)


def test_lookback_periods_accept_cli_string():
    assert make_strategy(lookback_periods='12,42,126').lookback_periods == [12, 42, 126]
    assert make_strategy(lookback_periods=42).lookback_periods == [42]
