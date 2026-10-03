"""Per-strategy subaccount processes: profiles, vault_address signing, guards.

Research round 6 (2026-07-02) showed two individually validated strategies
lose money when co-run on ONE account (symbol occupancy + capital rotation);
capital sleeves removed eviction but not occupancy. Separate Hyperliquid
subaccounts — one bot process each — remove both channels.
"""
from unittest.mock import MagicMock, patch

import pytest

from src.config.settings import apply_profile, load_config


def _cfg(**persistence):
    return {
        'strategies': {
            'instances': [{'type': 'cross_sectional_momentum', 'name': 'csm_4h', 'timeframe': '4h'}],
            'profiles': {'sentiment': [{'type': 'sentiment_ml', 'name': 'sentiment_ml_1h', 'timeframe': '1h'}]},
        },
        'persistence': dict(persistence),
        'logging': {'file': 'trading_bot.log'},
        'api': {},
    }


def test_profile_replaces_instances_and_isolates_storage():
    cfg = apply_profile(_cfg(db_path=None), 'sentiment')
    assert [i['name'] for i in cfg['strategies']['instances']] == ['sentiment_ml_1h']
    assert cfg['runtime_profile'] == 'sentiment'
    assert cfg['persistence']['db_path'] == 'data/trades_sentiment.db'
    assert cfg['logging']['file'] == 'trading_bot_sentiment.log'


def test_profile_keeps_explicit_db_path():
    cfg = apply_profile(_cfg(db_path='/srv/custom.db'), 'sentiment')
    assert cfg['persistence']['db_path'] == '/srv/custom.db'


def test_profile_instances_are_copies():
    cfg = _cfg()
    apply_profile(cfg, 'sentiment')
    cfg['strategies']['instances'][0]['name'] = 'mutated'
    assert cfg['strategies']['profiles']['sentiment'][0]['name'] == 'sentiment_ml_1h'


def test_unknown_profile_raises():
    with pytest.raises(ValueError, match='nope'):
        apply_profile(_cfg(), 'nope')


def test_load_config_default_has_no_profile():
    cfg = load_config()
    assert 'runtime_profile' not in cfg
    assert cfg['strategies']['profiles'] == {}
    assert 'subaccount_address' in cfg['api']


# --- main.py guard -----------------------------------------------------------

def test_profile_without_subaccount_is_refused():
    from src.main import check_profile_account
    cfg = apply_profile(_cfg(), 'sentiment')
    assert 'HYPERLIQUID_SUBACCOUNT_ADDRESS' in check_profile_account(cfg)
    cfg['api']['subaccount_address'] = '0xsub'
    assert check_profile_account(cfg) is None


def test_default_process_needs_no_subaccount():
    from src.main import check_profile_account
    assert check_profile_account(_cfg()) is None


def test_main_parses_profile_flag():
    from src.main import parse_args
    assert parse_args(['--profile', 'sentiment']).profile == 'sentiment'
    assert parse_args([]).profile is None


# --- HyperliquidAPI subaccount wiring ----------------------------------------

def _api(subaccount=''):
    config = {
        'api': {'base_url': 'https://api.hyperliquid.xyz', 'wallet_address': '0xagent',
                'private_key': '0xabc', 'public_account_address': '0xmain',
                'subaccount_address': subaccount},
        'trading': {}, 'risk_management': {}, 'strategies': {'ohlcv_limit': 100},
    }
    with patch('src.api.hyperliquid_api.HyperliquidAPI._init_sdk_clients'):
        from src.api.hyperliquid_api import HyperliquidAPI
        return HyperliquidAPI(config)


def test_subaccount_becomes_the_account_for_reads():
    api = _api('0xsub')
    assert api.subaccount_address == '0xsub'
    assert api.public_account_address == '0xsub'


def test_no_subaccount_keeps_main_account():
    api = _api()
    assert api.subaccount_address == ''
    assert api.public_account_address == '0xmain'


@pytest.mark.parametrize("sub,expected_vault", [('0xsub', '0xsub'), ('', None)])
def test_exchange_signs_with_vault_address_only_for_subaccount(sub, expected_vault):
    api = _api(sub)
    api.perp_dexs = ['']
    with patch('hyperliquid.info.Info'), \
         patch('hyperliquid.exchange.Exchange') as exchange_cls, \
         patch('eth_account.Account'):
        api._init_sdk_clients()
    kwargs = exchange_cls.call_args.kwargs
    assert kwargs['vault_address'] == expected_vault
    assert kwargs['account_address'] == (sub or '0xmain')


def test_execution_fee_reads_fills_of_trading_account():
    """Pre-fix: fills were queried for the API agent wallet -> fees always 0.0."""
    api = _api()
    api.info = MagicMock()
    api.info.user_fills.side_effect = lambda addr: (
        [{'oid': 7, 'fee': '0.42'}] if addr == '0xmain' else [])
    api._rate_limited_call = lambda fn, **kw: fn()
    assert api.get_execution_fee(7) == pytest.approx(0.42)
    api.info.user_fills.assert_called_with('0xmain')


# --- hot reload keeps the profile ----------------------------------------------

def test_reconcile_reloads_with_runtime_profile(mock_config, mock_market_api):
    from src.strategies.strategy_manager import StrategyManager
    mock_config['strategies']['instances'] = []
    mock_config['trading'].update({
        'position_monitoring_interval': 10,
        'enable_stale_order_cleanup': True,
        'position_sync_interval': 300,
        'enable_position_validation': True,
        'order_timeout_minutes': 5,
    })
    with patch('src.strategies.strategy_manager.StrategySelector'), \
         patch('src.strategies.strategy_manager.ExecutionEngine'), \
         patch('src.strategies.strategy_manager.DynamicPairSelector'), \
         patch('src.strategies.strategy_manager.PerformanceTracker'):
        manager = StrategyManager(mock_config, mock_market_api)
    manager.config['runtime_profile'] = 'sentiment'
    with patch('src.config.settings.load_config',
               return_value={'strategies': {'instances': []}}) as loader:
        manager._importlib = MagicMock()
        manager.reconcile_strategies()
    loader.assert_called_with(profile='sentiment')
