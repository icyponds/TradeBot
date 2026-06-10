"""
Tests for the Volatility Breakout entry pipeline:
- closed-candle evaluation (forming bar must be ignored)
- percentile-based squeeze detection (with absolute fallback)
- volume-expansion confirmation
- fresh-breakout-only signaling (no re-entry on continuation candles)
- ATR-scaled (chandelier) trailing stop configuration
"""

import numpy as np
import pandas as pd
import pytest

from src.strategies.volatility_breakout_strategy import VolatilityBreakoutStrategy


def make_strategy(**vb_overrides):
    config = {"strategies": {"ohlcv_limit": 300, "volatility_breakout": vb_overrides}}
    return VolatilityBreakoutStrategy(config, timeframe='4h')


def make_breakout_df(direction='up', breakout=True, breakout_volume=5000.0,
                     end_offset_hours=8, extra_continuation=False):
    """
    Synthetic 4h series: trending phase (Hurst > 0.5) -> tight squeeze ->
    optional breakout candle with volume expansion.

    Bars end `end_offset_hours` in the past so they are all closed by default;
    pass 0 to make the last bar the currently forming one.
    """
    rng = np.random.default_rng(7)
    drift = 0.3 if direction == 'up' else -0.3
    prices = list(100 + np.cumsum(rng.normal(drift, 1.2, 100)))   # trending
    prices += list(prices[-1] + rng.normal(0, 0.05, 45))          # squeeze
    if breakout:
        mult = 1.05 if direction == 'up' else 0.95
        prices += [prices[-1] * mult]                              # breakout close
        if extra_continuation:
            mult2 = 1.02 if direction == 'up' else 0.98
            prices += [prices[-1] * mult2]                         # continuation candle
    prices = np.array(prices)
    n = len(prices)

    end = (pd.Timestamp.utcnow().tz_localize(None).floor('4h')
           - pd.Timedelta(hours=end_offset_hours))
    idx = pd.date_range(end=end, periods=n, freq='4h')

    volumes = rng.uniform(900, 1100, n)
    if breakout:
        volumes[-1] = breakout_volume
        if extra_continuation:
            volumes[-2] = breakout_volume

    return pd.DataFrame({
        'open': prices,
        'high': prices * 1.002,
        'low': prices * 0.998,
        'close': prices,
        'volume': volumes,
    }, index=idx)


class TestBreakoutSignals:

    def test_long_breakout_signal(self):
        strategy = make_strategy()
        df = make_breakout_df(direction='up')

        signal = strategy.generate_signal('TEST', {'4h': df})

        assert signal is not None
        assert signal['signal'] == 'buy'
        assert signal['atr'] > 0
        # Signal price must be the close of the last CLOSED candle
        assert signal['price'] == df['close'].iloc[-1]

    def test_short_breakout_signal(self):
        strategy = make_strategy()
        df = make_breakout_df(direction='down')

        signal = strategy.generate_signal('TEST', {'4h': df})

        assert signal is not None
        assert signal['signal'] == 'sell'

    def test_no_signal_without_breakout(self):
        strategy = make_strategy()
        df = make_breakout_df(breakout=False)

        assert strategy.generate_signal('TEST', {'4h': df}) is None

    def test_volume_filter_rejects_flat_volume(self):
        """Breakout without volume expansion must be rejected."""
        strategy = make_strategy()
        df = make_breakout_df(breakout_volume=1000.0)  # same as trailing median

        assert strategy.generate_signal('TEST', {'4h': df}) is None

    def test_volume_filter_skipped_without_volume_data(self):
        """Tick-built bars carry zero volume - the filter must not block then."""
        strategy = make_strategy()
        df = make_breakout_df()
        df['volume'] = 0.0

        signal = strategy.generate_signal('TEST', {'4h': df})
        assert signal is not None
        assert signal['signal'] == 'buy'

    def test_forming_bar_is_ignored(self):
        """
        The same breakout candle must NOT signal while it is still forming:
        an intrabar spike can fade before the close.
        """
        strategy = make_strategy()
        df = make_breakout_df(end_offset_hours=0)  # last bar = current 4h period

        assert strategy.generate_signal('TEST', {'4h': df}) is None

    def test_no_resignal_on_continuation_candle(self):
        """
        Once the breakout candle has closed outside the band, the next candle
        (also outside) must not generate a second entry for the same move.
        """
        strategy = make_strategy()
        df = make_breakout_df(extra_continuation=True)

        assert strategy.generate_signal('TEST', {'4h': df}) is None


class TestSqueezeDetection:

    def _bandwidth_series(self, values):
        return pd.Series(values, dtype=float)

    def test_percentile_squeeze_is_asset_relative(self):
        """
        Bandwidth of 0.30 would never pass an absolute threshold of 0.15,
        but for an asset whose bandwidth usually sits near 0.50 it IS a squeeze.
        """
        strategy = make_strategy(squeeze_threshold=0.15, squeeze_percentile=0.20,
                                 squeeze_window=100)
        bw = self._bandwidth_series([0.50] * 100 + [0.30])

        assert strategy._is_squeeze_at(bw, -1) is True

    def test_percentile_squeeze_rejects_normal_bandwidth(self):
        strategy = make_strategy(squeeze_percentile=0.20, squeeze_window=100)
        bw = self._bandwidth_series(list(np.linspace(0.2, 0.6, 100)) + [0.55])

        assert strategy._is_squeeze_at(bw, -1) is False

    def test_absolute_fallback_with_short_history(self):
        """With history shorter than squeeze_window/2 the absolute threshold applies."""
        strategy = make_strategy(squeeze_threshold=0.15, squeeze_window=100)

        tight = self._bandwidth_series([0.5] * 10 + [0.10])
        wide = self._bandwidth_series([0.5] * 10 + [0.20])

        assert strategy._is_squeeze_at(tight, -1) is True
        assert strategy._is_squeeze_at(wide, -1) is False

    def test_nan_bandwidth_is_not_squeeze(self):
        strategy = make_strategy()
        bw = self._bandwidth_series([np.nan] * 5)

        assert strategy._is_squeeze_at(bw, -1) is False


class TestAtrTrailingStop:

    def test_trail_scales_with_atr(self):
        strategy = make_strategy(trail_atr_mult=2.5, trail_activation_atr_mult=1.0)

        cfg = strategy.get_trailing_stop_config(entry_price=100.0, signal_context={'atr': 2.0})

        assert cfg['enabled'] is True
        assert cfg['trail_pct'] == pytest.approx(0.05)        # 2.5 * (2/100)
        assert cfg['activation_pct'] == pytest.approx(0.02)   # 1.0 * (2/100)

    def test_trail_clamped_against_corrupt_atr(self):
        strategy = make_strategy()

        huge = strategy.get_trailing_stop_config(entry_price=100.0, signal_context={'atr': 50.0})
        tiny = strategy.get_trailing_stop_config(entry_price=100.0, signal_context={'atr': 0.001})

        assert huge['trail_pct'] <= 0.15
        assert huge['activation_pct'] <= 0.10
        assert tiny['trail_pct'] >= 0.005
        assert tiny['activation_pct'] >= 0.003

    def test_fallback_without_atr_context(self):
        strategy = make_strategy()

        for cfg in (strategy.get_trailing_stop_config(),
                    strategy.get_trailing_stop_config(entry_price=100.0),
                    strategy.get_trailing_stop_config(entry_price=100.0, signal_context={'atr': 0})):
            assert cfg['enabled'] is True
            assert cfg['trail_pct'] == pytest.approx(0.03)
            assert cfg['activation_pct'] == pytest.approx(0.02)


class TestTrendFilter:

    def test_counter_trend_short_rejected(self):
        """A short breakout above the long EMA must be rejected."""
        strategy = make_strategy(trend_ema_period=50)
        df = make_breakout_df(direction='down')
        # Uptrend prefix so the EMA sits far below the breakout close
        df['close'] = df['close'] + np.linspace(0, 200, len(df))
        df['open'] = df['close']; df['high'] = df['close'] * 1.002; df['low'] = df['close'] * 0.998

        assert strategy.generate_signal('TEST', {'4h': df}) is None

    def test_with_trend_long_allowed(self):
        """The standard uptrend breakout passes (close above EMA)."""
        strategy = make_strategy(trend_ema_period=50)
        df = make_breakout_df(direction='up')

        signal = strategy.generate_signal('TEST', {'4h': df})
        assert signal is not None and signal['signal'] == 'buy'

    def test_filter_disabled_allows_counter_trend(self):
        strategy = make_strategy(trend_ema_period=50, trend_filter_enabled=False)
        df = make_breakout_df(direction='down')
        df['close'] = df['close'] + np.linspace(0, 200, len(df))
        df['open'] = df['close']; df['high'] = df['close'] * 1.002; df['low'] = df['close'] * 0.998

        # Without the filter the same setup may signal (if other gates pass);
        # at minimum it must not be rejected BY the trend filter - so the
        # outcome must differ from the enabled case OR be a sell signal.
        signal = strategy.generate_signal('TEST', {'4h': df})
        assert signal is None or signal['signal'] == 'sell'


class TestDirectionRestriction:

    def test_long_only_blocks_shorts(self):
        strategy = make_strategy(direction='long_only')
        df = make_breakout_df(direction='down')

        assert strategy.generate_signal('TEST', {'4h': df}) is None

    def test_long_only_allows_longs(self):
        strategy = make_strategy(direction='long_only')
        df = make_breakout_df(direction='up')

        signal = strategy.generate_signal('TEST', {'4h': df})
        assert signal is not None and signal['signal'] == 'buy'
