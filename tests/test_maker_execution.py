"""
Tests for the probabilistic maker (post-only) execution model in
MockMarketAPI:
- disabled by default -> taker behavior (slippage + taker fee)
- enabled: entries fill at the limit price (no slippage) with maker fee,
  or are rejected with ~(1 - fill_prob) frequency
- exits (reduce_only) always fill and always pay taker costs
- deterministic under a fixed seed
"""

import pandas as pd
import pytest

from src.backtesting.mock_market_api import MockMarketAPI


def make_api(maker=None, fee_bps=5.0, slippage_bps=5.0):
    idx = pd.date_range('2026-01-01', periods=300, freq='1h')
    df = pd.DataFrame({
        'open': 100.0, 'high': 101.0, 'low': 99.0, 'close': 100.0,
        'volume': 1000.0,
    }, index=idx)
    config = {
        'backtesting': {
            'initial_capital': 50000.0,
            'fee_bps': fee_bps,
            'slippage_bps': slippage_bps,
            **({'maker_execution': maker} if maker else {}),
        },
        'trading': {'symbols': ['BTC']},
        'strategies': {'ohlcv_limit': 300},
    }
    api = MockMarketAPI(config, {'BTC': {'1h': df}})
    api.current_time = idx[-1] + pd.Timedelta(hours=1)
    return api


class TestTakerDefault:
    def test_entry_pays_slippage_and_taker_fee(self):
        api = make_api()
        r = api.execute_order('BTC', 'buy', 1.0)
        assert r['status'] in ('ok', 'filled')
        assert r['avg_fill_price'] == pytest.approx(100.0 * 1.0005)
        assert r['fee'] == pytest.approx(1.0 * 100.05 * 0.0005)


class TestMakerModel:
    MAKER = {'enabled': True, 'fill_prob': 1.0, 'maker_fee_bps': 1.5, 'seed': 7}

    def test_certain_fill_at_limit_with_maker_fee(self):
        api = make_api(maker=self.MAKER)
        r = api.execute_order('BTC', 'buy', 1.0)
        assert r.get('filled_size', 0) > 0
        assert r['avg_fill_price'] == pytest.approx(100.0)  # no slippage
        assert r['fee'] == pytest.approx(1.0 * 100.0 * 0.00015)

    def test_zero_fill_prob_rejects_entries(self):
        api = make_api(maker={**self.MAKER, 'fill_prob': 0.0})
        r = api.execute_order('BTC', 'buy', 1.0)
        assert r['status'] == 'rejected'
        assert 'maker_unfilled' in r['reason']
        assert api.maker_stats == {'attempted': 1, 'filled': 0, 'rejected': 1}

    def test_exits_always_fill_at_taker(self):
        api = make_api(maker={**self.MAKER, 'fill_prob': 0.0})
        # Open a position bypassing the maker model via reduce_only=False?
        # No - use fill_prob=1 api for entry, then swap prob for the exit.
        api.maker_fill_prob = 1.0
        r = api.execute_order('BTC', 'buy', 1.0)
        assert r.get('filled_size', 0) > 0
        api.maker_fill_prob = 0.0
        r2 = api.execute_order('BTC', 'sell', 1.0, reduce_only=True)
        assert r2.get('filled_size', 0) > 0           # never rejected
        assert r2['avg_fill_price'] == pytest.approx(100.0 * 0.9995)  # slippage
        assert r2['fee'] == pytest.approx(1.0 * 99.95 * 0.0005)       # taker fee

    def test_fill_rate_approximates_fill_prob(self):
        api = make_api(maker={**self.MAKER, 'fill_prob': 0.7})
        fills = sum(
            1 for _ in range(400)
            if api.execute_order('BTC', 'buy', 0.001).get('filled_size', 0) > 0
        )
        assert 0.6 < fills / 400 < 0.8

    def test_deterministic_under_seed(self):
        def run():
            api = make_api(maker={**self.MAKER, 'fill_prob': 0.5, 'seed': 123})
            return [api.execute_order('BTC', 'buy', 0.001).get('status')
                    for _ in range(20)]
        assert run() == run()
