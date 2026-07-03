"""
Live post-only (ALO) maker entries — trading.maker_entries.

Round-4 research: taker->maker cost structure is the decisive variable
(csm_4h maker ensemble all-seeds-positive, ~2x the taker total). This is
the live analogue of backtesting.maker_execution: rest at the touch,
timeout = missed entry (never chased with taker), exits stay taker.
"""
import pytest
from unittest.mock import MagicMock, patch

from src.api.hyperliquid_api import HyperliquidAPI
from src.backtesting.mock_market_api import MockMarketAPI
from src.strategies.execution_engine import ExecutionEngine


@pytest.fixture
def api():
    config = {
        'api': {'base_url': 'https://api.hyperliquid.xyz',
                'private_key': '0xabc', 'wallet_address': '0x123'},
        'hip3': {'enabled': False, 'perp_dexs': []},
    }
    with patch('hyperliquid.info.Info'):
        with patch.object(HyperliquidAPI, '_discover_perp_dexs', return_value=['']):
            api = HyperliquidAPI(config)
    api.exchange = MagicMock()
    api.info = MagicMock()
    api._rate_limited_call = lambda fn, *a, **kw: fn(*a, **{k: v for k, v in kw.items() if k != 'weight'})
    api._resolve_market_info = MagicMock(return_value={
        'symbol': 'BTC', 'display_symbol': 'BTC',
        'price': 50000.0, 'sz_decimals': 3,
    })
    api._round_to_tick = lambda p, **kw: p
    return api


def _resting_response(oid=777):
    return {'response': {'data': {'statuses': [{'resting': {'oid': oid}}]}}}


def _filled_response(oid=777, sz=1.0, px=50000.0):
    return {'response': {'data': {'statuses': [
        {'filled': {'oid': oid, 'totalSz': str(sz), 'avgPx': str(px)}}]}}}


class TestRealApiMakerOrder:
    def test_places_alo_order_at_bid_for_buy(self, api):
        api.info.l2_snapshot.return_value = {
            'levels': [[{'px': '49990'}], [{'px': '50010'}]]}
        api.exchange.order.return_value = _filled_response(px=49990.0)

        result = api.execute_maker_order('BTC', 'buy', 1.0)

        assert result['status'] == 'filled'
        args, kwargs = api.exchange.order.call_args
        assert args[3] == 49990.0          # joined the best bid
        assert args[4] == {'limit': {'tif': 'Alo'}}
        assert kwargs.get('reduce_only') is False

    def test_sell_joins_the_ask(self, api):
        api.info.l2_snapshot.return_value = {
            'levels': [[{'px': '49990'}], [{'px': '50010'}]]}
        api.exchange.order.return_value = _filled_response(px=50010.0)

        api.execute_maker_order('BTC', 'sell', 1.0)

        args, _ = api.exchange.order.call_args
        assert args[3] == 50010.0          # joined the best ask

    def test_fill_while_resting(self, api):
        api.info.l2_snapshot.return_value = {'levels': [[{'px': '49990'}], []]}
        api.exchange.order.return_value = _resting_response(oid=42)
        api.get_order_status = MagicMock(return_value={
            'status': 'filled', 'filled_size': 1.0, 'avg_fill_price': 49990.0})

        with patch('time.sleep'):
            result = api.execute_maker_order('BTC', 'buy', 1.0,
                                             timeout_seconds=30,
                                             poll_interval_seconds=1)

        assert result['status'] == 'filled'
        assert result['filled_size'] == 1.0
        assert result['avg_fill_price'] == 49990.0

    def test_timeout_cancels_and_reports_missed(self, api):
        api.info.l2_snapshot.return_value = {'levels': [[{'px': '49990'}], []]}
        api.exchange.order.return_value = _resting_response(oid=42)
        api.get_order_status = MagicMock(return_value={
            'status': 'open', 'filled_size': 0.0, 'avg_fill_price': 0.0})
        api.cancel_order = MagicMock(return_value=True)

        fake_now = [1000.0]

        def fake_time():
            return fake_now[0]

        def fake_sleep(s):
            fake_now[0] += s

        with patch('src.api.hyperliquid_api.time.time', side_effect=fake_time), \
             patch('src.api.hyperliquid_api.time.sleep', side_effect=fake_sleep):
            result = api.execute_maker_order('BTC', 'buy', 1.0,
                                             timeout_seconds=10,
                                             poll_interval_seconds=5)

        assert result['status'] == 'missed'
        assert result['reason'] == 'timeout'
        api.cancel_order.assert_called_once_with('BTC', 42)

    def test_alo_rejection_is_a_miss_not_an_error(self, api):
        """An ALO that would cross is rejected by the exchange: legitimate
        miss, must NOT fall back to taker."""
        api.info.l2_snapshot.return_value = {'levels': [[{'px': '49990'}], []]}
        api.exchange.order.return_value = {'response': {'data': {'statuses': [
            {'error': 'Post only order would have immediately matched'}]}}}

        result = api.execute_maker_order('BTC', 'buy', 1.0)

        assert result['status'] == 'missed'
        assert result['reason'] == 'alo_rejected'

    def test_min_order_value_enforced(self, api):
        result = api.execute_maker_order('BTC', 'buy', 0.0001)
        assert result is None


class TestMockMakerOrder:
    def _mock_api(self, fill_prob):
        cfg = {
            'backtesting': {
                'initial_capital': 50000,
                'maker_execution': {'enabled': False, 'fill_prob': fill_prob,
                                    'maker_fee_bps': 1.5, 'seed': 7},
            },
            'api': {},
        }
        mock = MockMarketAPI(cfg, {})
        mock.get_current_price = MagicMock(return_value=100.0)
        return mock

    def test_fill_uses_maker_fee_and_no_slippage(self):
        mock = self._mock_api(fill_prob=1.0)
        start_balance = mock.perp_balance['withdrawable']

        result = mock.execute_maker_order('SOL', 'buy', 10.0)

        assert result['status'] == 'filled'
        assert result['avg_fill_price'] == 100.0  # at the touch, no slippage
        expected_fee = 10.0 * 100.0 * 1.5 / 10000.0
        assert result['fee'] == pytest.approx(expected_fee)
        assert mock.perp_balance['withdrawable'] == pytest.approx(start_balance - expected_fee)

    def test_miss_leaves_no_position(self):
        mock = self._mock_api(fill_prob=0.0)

        result = mock.execute_maker_order('SOL', 'buy', 10.0)

        assert result['status'] == 'missed'
        assert result['filled_size'] == 0.0
        assert mock.positions == {}

    def test_seeded_determinism(self):
        results_a = [self._mock_api(0.5).execute_maker_order('SOL', 'buy', 10.0)['status']
                     for _ in range(1)]
        results_b = [self._mock_api(0.5).execute_maker_order('SOL', 'buy', 10.0)['status']
                     for _ in range(1)]
        assert results_a == results_b


class TestEngineRouting:
    def _engine(self, maker_cfg):
        config = {
            'trading': {'maker_entries': maker_cfg},
            'risk_management': {},
            'strategies': {},
        }
        engine = ExecutionEngine.__new__(ExecutionEngine)
        engine.config = config
        engine.market_api = MagicMock()
        engine.logger = MagicMock()
        return engine

    def _maker_cfg_check(self, engine, strategy_name, market_type='perp'):
        """Replicates the routing predicate from execute_trade."""
        maker_cfg = (engine.config.get('trading', {}) or {}).get('maker_entries', {}) or {}
        return (
            bool(maker_cfg.get('enabled', False))
            and market_type != 'spot'
            and hasattr(engine.market_api, 'execute_maker_order')
            and (not maker_cfg.get('strategies')
                 or strategy_name in maker_cfg.get('strategies', []))
        )

    def test_disabled_by_default_routes_taker(self):
        engine = self._engine({'enabled': False})
        assert self._maker_cfg_check(engine, 'csm_4h') is False

    def test_enabled_routes_maker_for_all_strategies(self):
        engine = self._engine({'enabled': True, 'strategies': []})
        assert self._maker_cfg_check(engine, 'csm_4h') is True
        assert self._maker_cfg_check(engine, 'anything') is True

    def test_strategy_filter(self):
        engine = self._engine({'enabled': True, 'strategies': ['csm_4h']})
        assert self._maker_cfg_check(engine, 'csm_4h') is True
        assert self._maker_cfg_check(engine, 'sentiment_ml_1h') is False

    def test_spot_never_maker(self):
        engine = self._engine({'enabled': True, 'strategies': []})
        assert self._maker_cfg_check(engine, 'csm_4h', market_type='spot') is False
