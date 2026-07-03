"""
Cross-sectional funding carry strategy (research round 7, 2026-07-03).

Funding as a directional signal: short the most crowded longs (top
decile trailing funding), long the most crowded shorts. One leg per
name — avoids the multi-leg fee math that killed funding_rate_arbitrage.
"""
import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from src.strategies.funding_carry_strategy import FundingCarryStrategy
from src.backtesting.mock_market_api import MockMarketAPI


NOW = datetime(2026, 6, 15, 12, 0, 0)


def _ohlcv(price=100.0, n=50):
    dates = pd.date_range(end=NOW, periods=n, freq='1h')
    closes = np.full(n, price)
    return {'1h': pd.DataFrame({
        'open': closes, 'high': closes, 'low': closes,
        'close': closes, 'volume': 1000.0,
    }, index=dates)}


def _funding_records(hourly_rate, hours=24):
    return [{'coin': 'X', 'fundingRate': str(hourly_rate),
             'time': int((NOW - timedelta(hours=h)).timestamp() * 1000)}
            for h in range(hours, 0, -1)]


def _make_strategy(rates_by_symbol, config_overrides=None):
    """Strategy wired to a fake market_api serving per-symbol funding."""
    config = {'strategies': {'ohlcv_limit': 100,
                             'funding_carry': dict(config_overrides or {})}}
    api = MagicMock()
    api.current_time = NOW
    api.get_funding_history.side_effect = (
        lambda symbol, s, e: _funding_records(rates_by_symbol.get(symbol, 0.0)))
    strategy = FundingCarryStrategy(config, market_api=api, timeframe='1h')
    return strategy


@pytest.fixture(autouse=True)
def clear_universe_cache():
    FundingCarryStrategy._funding_stats = {}
    yield
    FundingCarryStrategy._funding_stats = {}


def _prime_universe(strategy, rates_by_symbol):
    """Feed every symbol once so the cross-sectional cache is populated."""
    results = {}
    for sym in rates_by_symbol:
        results[sym] = strategy.generate_signal(sym, _ohlcv())
    return results


def _universe(extreme_pos=0.0002, extreme_neg=-0.0002, n_neutral=10):
    """extreme_pos hourly = ~175% APR; neutral names ~ +/- tiny rates."""
    rates = {f"MID{i}": 0.0000005 * (i - n_neutral // 2) for i in range(n_neutral)}
    rates['CROWDED_LONGS'] = extreme_pos
    rates['CROWDED_SHORTS'] = extreme_neg
    return rates


class TestSignals:
    def test_fades_crowded_longs_short_only_default(self):
        rates = _universe()
        strategy = _make_strategy(rates)
        _prime_universe(strategy, rates)
        # First pass may miss extremes seen before the universe filled;
        # second pass ranks against the full cache.
        sig_short = strategy.generate_signal('CROWDED_LONGS', _ohlcv())
        sig_long = strategy.generate_signal('CROWDED_SHORTS', _ohlcv())

        assert sig_short is not None and sig_short['signal'] == 'sell'
        assert sig_short['funding_apr'] > 0.30
        # Default direction='short': the long side is OFF (HL baseline
        # funding mean-reverts negative spikes within hours — fee bleed)
        assert sig_long is None

    def test_direction_both_enables_longs(self):
        rates = _universe()
        strategy = _make_strategy(rates, {'direction': 'both'})
        _prime_universe(strategy, rates)

        sig_long = strategy.generate_signal('CROWDED_SHORTS', _ohlcv())
        assert sig_long is not None and sig_long['signal'] == 'buy'
        assert sig_long['funding_apr'] < -0.30

    def test_neutral_names_get_no_signal(self):
        rates = _universe()
        strategy = _make_strategy(rates)
        _prime_universe(strategy, rates)

        assert strategy.generate_signal('MID3', _ohlcv()) is None

    def test_apr_threshold_blocks_weak_extremes(self):
        # Extreme by rank but only ~13% APR — barely above baseline, below
        # the 30% gate (direction 'both' so the long side is also checked)
        rates = _universe(extreme_pos=0.000015, extreme_neg=-0.000015)
        strategy = _make_strategy(rates, {'direction': 'both'})
        _prime_universe(strategy, rates)

        assert strategy.generate_signal('CROWDED_LONGS', _ohlcv()) is None
        assert strategy.generate_signal('CROWDED_SHORTS', _ohlcv()) is None

    def test_min_universe_gate(self):
        rates = {'A': 0.0002, 'B': -0.0002, 'C': 0.0}
        strategy = _make_strategy(rates)  # default min_universe=10 > 3
        _prime_universe(strategy, rates)

        assert strategy.generate_signal('A', _ohlcv()) is None

    def test_no_market_api_returns_none(self):
        strategy = FundingCarryStrategy(
            {'strategies': {'ohlcv_limit': 100, 'funding_carry': {}}},
            market_api=None, timeframe='1h')
        assert strategy.generate_signal('BTC', _ohlcv()) is None

    def test_sparse_funding_history_is_untrusted(self):
        config = {'strategies': {'ohlcv_limit': 100,
                                 'funding_carry': {'min_universe': 2}}}
        api = MagicMock()
        api.current_time = NOW
        # Only 3 of 24 expected hourly records
        api.get_funding_history.return_value = _funding_records(0.0002, hours=3)
        strategy = FundingCarryStrategy(config, market_api=api, timeframe='1h')

        assert strategy._trailing_funding('BTC', 24) is None


class TestExit:
    def _position(self, symbol='BTC', side='short', age_hours=12):
        pos = MagicMock()
        pos.symbol = symbol
        pos.side = side
        pos.timestamp = NOW - timedelta(hours=age_hours)
        return pos

    def test_short_exits_when_funding_flips_negative(self):
        strategy = _make_strategy({'BTC': -0.0001})  # ~-88% APR, past buffer
        should, reason = strategy.should_exit(self._position(side='short'), 100.0)
        assert should is True
        assert 'flipped' in reason

    def test_short_holds_while_funding_positive(self):
        strategy = _make_strategy({'BTC': 0.0002})
        should, _ = strategy.should_exit(self._position(side='short'), 100.0)
        assert should is False

    def test_long_exits_when_funding_flips_positive(self):
        strategy = _make_strategy({'BTC': 0.0001})
        should, reason = strategy.should_exit(self._position(side='long'), 100.0)
        assert should is True

    def test_flip_within_buffer_holds(self):
        """Hysteresis: an 8h mean drifting barely negative (~-2.6% APR,
        inside the 5% buffer) must NOT churn the position out."""
        strategy = _make_strategy({'BTC': -0.000003})
        should, _ = strategy.should_exit(self._position(side='short'), 100.0)
        assert should is False

    def test_min_holding_blocks_early_flip_exit(self):
        """Anti-churn: no funding-flip exit while the position is young."""
        strategy = _make_strategy({'BTC': -0.0001})  # clearly flipped
        should, _ = strategy.should_exit(
            self._position(side='short', age_hours=2), 100.0)
        assert should is False

    def test_no_funding_data_holds(self):
        config = {'strategies': {'ohlcv_limit': 100, 'funding_carry': {}}}
        api = MagicMock()
        api.current_time = NOW
        api.get_funding_history.return_value = []
        strategy = FundingCarryStrategy(config, market_api=api, timeframe='1h')
        should, _ = strategy.should_exit(self._position(side='short'), 100.0)
        assert should is False


class TestSignalStrength:
    def test_strength_scales_with_apr(self):
        strategy = _make_strategy({})
        weak = strategy.calculate_signal_strength({}, signal_context={'funding_apr': 0.30})
        mid = strategy.calculate_signal_strength({}, signal_context={'funding_apr': 0.90})
        strong = strategy.calculate_signal_strength({}, signal_context={'funding_apr': 2.00})
        assert weak == 0.5
        assert 0.5 < mid < 1.0
        assert strong == 1.0


class TestMockFundingHistoryParity:
    def _mock_api(self, sim_time):
        mock = MockMarketAPI({'backtesting': {'initial_capital': 50000}, 'api': {}}, {})
        idx = pd.date_range(end=NOW + timedelta(hours=24), periods=72, freq='1h')
        mock.set_funding_data({'BTC': pd.DataFrame({'funding_rate': 0.0001}, index=idx)})
        mock.current_time = sim_time
        return mock

    def test_returns_real_api_format(self):
        mock = self._mock_api(sim_time=NOW)
        start_ms = int((NOW - timedelta(hours=10)).timestamp() * 1000)
        end_ms = int(NOW.timestamp() * 1000)

        records = mock.get_funding_history('BTC', start_ms, end_ms)

        assert len(records) > 0
        assert set(records[0].keys()) == {'coin', 'fundingRate', 'time'}
        assert isinstance(records[0]['fundingRate'], str)

    def test_no_lookahead_past_sim_clock(self):
        """Data exists 24h past sim time; the window must clamp to now."""
        mock = self._mock_api(sim_time=NOW)
        start_ms = int((NOW - timedelta(hours=5)).timestamp() * 1000)
        end_ms = int((NOW + timedelta(hours=24)).timestamp() * 1000)

        records = mock.get_funding_history('BTC', start_ms, end_ms)

        assert records, "window before sim time should return data"
        latest_ms = max(r['time'] for r in records)
        assert latest_ms <= int(NOW.timestamp() * 1000) + 1

    def test_unknown_symbol_empty(self):
        mock = self._mock_api(sim_time=NOW)
        assert mock.get_funding_history('NOPE', 0, 10**15) == []
