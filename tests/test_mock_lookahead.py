"""
Tests for MockMarketAPI temporal correctness:
- get_ohlcv must only return candles fully CLOSED at sim time (DB rows are
  keyed by bar START but contain the final OHLC - including a bar that has
  merely started leaks up to one interval of future data)
- get_current_price must come from the finest timeframe's last closed bar
- subscribe_symbol must warm the OHLCV cache (readiness gate parity)
- hourly funding accrual on open perp positions
"""

import pandas as pd
import pytest

from src.backtesting.mock_market_api import MockMarketAPI


@pytest.fixture
def api():
    # 4h bars at 00:00/04:00/08:00/12:00 with distinct closes
    idx4 = pd.date_range('2026-01-01 00:00', periods=4, freq='4h')
    df4 = pd.DataFrame({
        'open': [1.0, 2.0, 3.0, 4.0], 'high': [1, 2, 3, 4], 'low': [1, 2, 3, 4],
        'close': [10.0, 20.0, 30.0, 40.0], 'volume': [1] * 4,
    }, index=idx4)
    # 15m bars covering the same span, close == bar index position
    idx15 = pd.date_range('2026-01-01 00:00', periods=64, freq='15min')
    df15 = pd.DataFrame({
        'open': 1.0, 'high': 1.0, 'low': 1.0,
        'close': [float(i) for i in range(64)], 'volume': 1.0,
    }, index=idx15)

    api = MockMarketAPI({'backtesting': {'initial_capital': 1000.0}},
                        {'BTC': {'4h': df4, '15m': df15}})
    api.reset_state()
    return api


class TestClosedBarSlicing:

    def test_forming_4h_bar_not_visible(self, api):
        """At 13:00 the 12:00 4h candle (closes 16:00) must NOT be returned."""
        api.set_time(pd.Timestamp('2026-01-01 13:00'))
        df = api.get_ohlcv('BTC', '4h', limit=10)

        assert df.index[-1] == pd.Timestamp('2026-01-01 08:00')
        assert df['close'].iloc[-1] == 30.0

    def test_bar_visible_exactly_at_close(self, api):
        api.set_time(pd.Timestamp('2026-01-01 16:00'))
        df = api.get_ohlcv('BTC', '4h', limit=10)

        assert df.index[-1] == pd.Timestamp('2026-01-01 12:00')
        assert df['close'].iloc[-1] == 40.0

    def test_no_closed_bars_returns_none(self, api):
        api.set_time(pd.Timestamp('2026-01-01 02:00'))  # first 4h bar closes 04:00
        assert api.get_ohlcv('BTC', '4h', limit=10) is None


class TestCurrentPrice:

    def test_price_from_finest_closed_bar(self, api):
        """At 13:00 price = close of the 12:45 15m bar (closes exactly 13:00)."""
        api.set_time(pd.Timestamp('2026-01-01 13:00'))
        price = api.get_current_price('BTC')

        # 12:45 bar is index position 51 (00:00 + 51*15m), close == 51.0
        assert price == 51.0

    def test_price_never_from_future(self, api):
        """Mid-bar, price must come from the last CLOSED bar, not the forming one."""
        api.set_time(pd.Timestamp('2026-01-01 13:05'))
        price = api.get_current_price('BTC')

        assert price == 51.0  # 12:45 bar; the forming 13:00 bar (close 52) is invisible

    def test_falls_back_to_coarser_timeframe_when_fine_stale(self):
        """15m data that ended long ago must not pin the price; 4h takes over."""
        idx15 = pd.date_range('2026-01-01 00:00', periods=4, freq='15min')
        df15 = pd.DataFrame({'open': 1.0, 'high': 1.0, 'low': 1.0,
                             'close': [1.0, 2.0, 3.0, 4.0], 'volume': 1.0}, index=idx15)
        idx4 = pd.date_range('2026-01-01 00:00', periods=12, freq='4h')
        df4 = pd.DataFrame({'open': 1.0, 'high': 1.0, 'low': 1.0,
                            'close': [float(100 + i) for i in range(12)], 'volume': 1.0}, index=idx4)
        api = MockMarketAPI({'backtesting': {'initial_capital': 1000.0}},
                            {'BTC': {'15m': df15, '4h': df4}})
        api.reset_state()
        api.set_time(pd.Timestamp('2026-01-02 12:00'))

        price = api.get_current_price('BTC')

        # Last closed 4h bar at sim time: the 08:00 bar on Jan 2 (index 8, close 108)
        assert price == 108.0


class TestSubscribeWarmsCache:

    def test_cache_populated_on_subscribe(self, api):
        """
        Regression: _is_data_ready_for_symbol requires cached bars for the
        required timeframes. subscribe_symbol used to create EMPTY deques,
        so readiness never passed and backtests silently analyzed nothing.
        """
        api.set_time(pd.Timestamp('2026-01-01 13:00'))
        api.subscribe_symbol('BTC', required_timeframes=['4h'])

        cached = api.ohlcv_cache.cache['BTC']['4h']
        assert len(cached) >= 2


class TestFundingAccrual:

    def _with_funding(self, api, rate=0.0001):
        fdf = pd.DataFrame({'funding_rate': [rate] * 40},
                           index=pd.date_range('2026-01-01 00:00', periods=40, freq='1h'))
        api.set_funding_data({'BTC': fdf})

    def test_long_pays_positive_funding(self, api):
        api.set_time(pd.Timestamp('2026-01-01 13:00'))
        api.positions['BTC'] = {'symbol': 'BTC', 'size': 1.0, 'entry_price': 50.0,
                                'side': 'long', 'mark_price': 50.0}
        self._with_funding(api, rate=0.0001)

        balance_before = api.perp_balance['withdrawable']
        api.set_time(pd.Timestamp('2026-01-01 15:00'))  # crosses 2 hour boundaries

        assert api.total_funding_paid > 0
        assert api.perp_balance['withdrawable'] == balance_before - api.total_funding_paid

    def test_short_receives_positive_funding(self, api):
        api.set_time(pd.Timestamp('2026-01-01 13:00'))
        api.positions['BTC'] = {'symbol': 'BTC', 'size': -1.0, 'entry_price': 50.0,
                                'side': 'short', 'mark_price': 50.0}
        self._with_funding(api, rate=0.0001)

        balance_before = api.perp_balance['withdrawable']
        api.set_time(pd.Timestamp('2026-01-01 15:00'))

        assert api.total_funding_paid < 0
        assert api.perp_balance['withdrawable'] > balance_before

    def test_no_double_payment_within_same_hour(self, api):
        api.set_time(pd.Timestamp('2026-01-01 13:00'))
        api.positions['BTC'] = {'symbol': 'BTC', 'size': 1.0, 'entry_price': 50.0,
                                'side': 'long', 'mark_price': 50.0}
        self._with_funding(api)

        api.set_time(pd.Timestamp('2026-01-01 14:00'))
        paid_once = api.total_funding_paid
        api.set_time(pd.Timestamp('2026-01-01 14:15'))
        api.set_time(pd.Timestamp('2026-01-01 14:30'))

        assert api.total_funding_paid == paid_once

    def test_no_funding_without_positions(self, api):
        self._with_funding(api)
        api.set_time(pd.Timestamp('2026-01-01 13:00'))
        api.set_time(pd.Timestamp('2026-01-01 15:00'))

        assert api.total_funding_paid == 0.0


class TestOhlcvSanitization:

    def _df(self, closes, volumes):
        idx = pd.date_range('2026-01-01', periods=len(closes), freq='15min')
        return pd.DataFrame({'open': closes, 'high': closes, 'low': closes,
                             'close': closes, 'volume': volumes}, index=idx)

    def test_zero_volume_scale_corruption_dropped(self):
        """Regression: flat $0.00072 zero-volume BERA placeholders produced a
        fictional 1200x backtest gain."""
        from src.backtesting.backtest_engine import BacktestEngine

        closes = [0.88, 0.89, 0.90] + [0.00072] * 10 + [0.91, 0.92] * 5
        volumes = [100, 100, 100] + [0.0] * 10 + [100, 100] * 5
        df, dropped = BacktestEngine._sanitize_ohlcv(self._df(closes, volumes))

        assert dropped == 10
        assert df['close'].min() > 0.5

    def test_real_crash_with_volume_kept(self):
        """A genuine -50% crash candle (with volume) must survive."""
        from src.backtesting.backtest_engine import BacktestEngine

        closes = [100.0] * 20 + [50.0] + [48.0] * 5
        volumes = [100.0] * 26
        df, dropped = BacktestEngine._sanitize_ohlcv(self._df(closes, volumes))

        assert dropped == 0
        assert len(df) == 26

    def test_isolated_absurd_spike_dropped(self):
        from src.backtesting.backtest_engine import BacktestEngine

        closes = [10.0] * 10 + [500.0] + [10.0] * 10  # 50x spike, volume present
        volumes = [100.0] * 21
        df, dropped = BacktestEngine._sanitize_ohlcv(self._df(closes, volumes))

        assert 500.0 not in df['close'].values


class TestSpotPriceResolution:

    def _api(self):
        idx = pd.date_range('2026-01-01', periods=30, freq='15min')
        df = pd.DataFrame({'open': 1.0, 'high': 1.0, 'low': 1.0,
                           'close': 2.5, 'volume': 1.0}, index=idx)
        api = MockMarketAPI({'backtesting': {'initial_capital': 1000.0}},
                            {'PURR': {'15m': df}, 'PURR_SPOT': {'15m': df * 0.99}})
        api.reset_state()
        api.set_time(pd.Timestamp('2026-01-01 05:00'))
        return api

    def test_base_token_resolves(self):
        api = self._api()
        assert api.get_spot_price('PURR') is not None

    def test_suffixed_token_resolves(self):
        """Regression: get_spot_token_for_perp returns 'X_SPOT'; get_spot_price
        used to append _SPOT again ('X_SPOT_SPOT'), killing multi-leg entries."""
        api = self._api()
        token = api.get_spot_token_for_perp('PURR')
        assert api.get_spot_price(token) is not None
