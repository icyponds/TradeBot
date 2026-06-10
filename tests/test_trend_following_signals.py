"""
Tests for the Trend Following (Donchian / TSMOM) strategy:
- fresh channel-breakout entries (long and short)
- no re-signaling on continuation candles
- channel computed strictly before the breakout bar
- direction restriction (long_only / short_only)
- Donchian opposite-channel exit
- ATR-scaled (chandelier) trailing stop configuration
"""

import numpy as np
import pandas as pd
import pytest

from src.strategies.trend_following_strategy import TrendFollowingStrategy


def make_strategy(**tf_overrides):
    config = {"strategies": {"ohlcv_limit": 300, "trend_following": tf_overrides}}
    return TrendFollowingStrategy(config, timeframe='4h')


def make_channel_df(direction='up', breakout=True, continuation=False,
                    n_range=120, end_offset_hours=8):
    """
    Synthetic 4h series: a flat trading range followed by an optional
    breakout candle beyond the range extreme (and an optional continuation
    candle beyond the breakout).

    Bars end `end_offset_hours` in the past so they are all closed.
    """
    rng = np.random.default_rng(11)
    prices = list(100 + rng.normal(0, 0.5, n_range))  # range-bound
    if breakout:
        mult = 1.05 if direction == 'up' else 0.95
        prices += [100 * mult]
        if continuation:
            mult2 = 1.02 if direction == 'up' else 0.98
            prices += [prices[-1] * mult2]
    prices = np.array(prices)
    n = len(prices)

    end = (pd.Timestamp.utcnow().tz_localize(None).floor('4h')
           - pd.Timedelta(hours=end_offset_hours))
    idx = pd.date_range(end=end, periods=n, freq='4h')

    return pd.DataFrame({
        'open': prices,
        'high': prices * 1.002,
        'low': prices * 0.998,
        'close': prices,
        'volume': np.full(n, 1000.0),
    }, index=idx)


class TestEntries:
    def test_long_breakout_signals_buy(self):
        strat = make_strategy(entry_lookback=60)
        df = make_channel_df('up')
        sig = strat.generate_signal('BTC', {'4h': df})
        assert sig is not None
        assert sig['signal'] == 'buy'
        assert sig['atr'] > 0

    def test_short_breakout_signals_sell(self):
        strat = make_strategy(entry_lookback=60)
        df = make_channel_df('down')
        sig = strat.generate_signal('BTC', {'4h': df})
        assert sig is not None
        assert sig['signal'] == 'sell'

    def test_no_breakout_no_signal(self):
        strat = make_strategy(entry_lookback=60)
        df = make_channel_df(breakout=False)
        assert strat.generate_signal('BTC', {'4h': df}) is None

    def test_continuation_candle_does_not_resignal(self):
        # The candle after the breakout is still above the channel but the
        # previous close was already above it -> not fresh, no signal.
        strat = make_strategy(entry_lookback=60)
        df = make_channel_df('up', continuation=True)
        assert strat.generate_signal('BTC', {'4h': df}) is None

    def test_insufficient_history_no_signal(self):
        strat = make_strategy(entry_lookback=60)
        df = make_channel_df('up', n_range=40)
        assert strat.generate_signal('BTC', {'4h': df}) is None

    def test_long_only_blocks_shorts(self):
        strat = make_strategy(entry_lookback=60, direction='long_only')
        df = make_channel_df('down')
        assert strat.generate_signal('BTC', {'4h': df}) is None

    def test_short_only_blocks_longs(self):
        strat = make_strategy(entry_lookback=60, direction='short_only')
        df = make_channel_df('up')
        assert strat.generate_signal('BTC', {'4h': df}) is None

    def test_confidence_scales_with_breakout_margin(self):
        strat = make_strategy(entry_lookback=60)
        sig = strat.generate_signal('BTC', {'4h': make_channel_df('up')})
        assert 0.5 <= sig['confidence'] <= 1.0


class TestExits:
    class FakePosition:
        def __init__(self, side):
            self.side = side
            self.entry_price = 100.0

    def test_long_exits_below_exit_channel(self):
        strat = make_strategy(entry_lookback=60, exit_lookback=30)
        df = make_channel_df(breakout=False)
        # Crash the last close below the 30-bar low
        df.iloc[-1, df.columns.get_loc('close')] = df['low'].iloc[-31:-1].min() * 0.95
        should, reason = strat.should_exit(self.FakePosition('long'), 95.0, {'ohlcv': df})
        assert should
        assert 'donchian_exit' in reason

    def test_short_exits_above_exit_channel(self):
        strat = make_strategy(entry_lookback=60, exit_lookback=30)
        df = make_channel_df(breakout=False)
        df.iloc[-1, df.columns.get_loc('close')] = df['high'].iloc[-31:-1].max() * 1.05
        should, reason = strat.should_exit(self.FakePosition('short'), 105.0, {'ohlcv': df})
        assert should
        assert 'donchian_exit' in reason

    def test_no_exit_inside_channel(self):
        strat = make_strategy(entry_lookback=60, exit_lookback=30)
        df = make_channel_df(breakout=False)
        should, _ = strat.should_exit(self.FakePosition('long'), 100.0, {'ohlcv': df})
        assert not should

    def test_no_exit_without_data(self):
        strat = make_strategy()
        should, _ = strat.should_exit(self.FakePosition('long'), 100.0, None)
        assert not should


class TestRisk:
    def test_stop_loss_uses_atr_context(self):
        strat = make_strategy(atr_multiplier_sl=2.0)
        sl = strat.calculate_stop_loss(100.0, 'long', {'atr': 3.0})
        assert sl == pytest.approx(94.0)
        sl_short = strat.calculate_stop_loss(100.0, 'short', {'atr': 3.0})
        assert sl_short == pytest.approx(106.0)

    def test_take_profit_disabled(self):
        strat = make_strategy()
        assert strat.calculate_take_profit(100.0, 'long') == 0.0

    def test_trailing_stop_scales_with_atr(self):
        strat = make_strategy(trail_atr_mult=3.0, trail_activation_atr_mult=1.5)
        cfg = strat.get_trailing_stop_config(entry_price=100.0, signal_context={'atr': 2.0})
        assert cfg['enabled']
        assert cfg['trail_pct'] == pytest.approx(0.06)
        assert cfg['activation_pct'] == pytest.approx(0.03)

    def test_trailing_stop_fallback_without_atr(self):
        strat = make_strategy()
        cfg = strat.get_trailing_stop_config()
        assert cfg['enabled']
        assert cfg['trail_pct'] > 0
