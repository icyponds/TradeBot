"""--risk-param override parsing in scripts/run_backtest.py."""
import importlib.util
import os

_spec = importlib.util.spec_from_file_location(
    "run_backtest",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "run_backtest.py"),
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
apply_risk_overrides = _mod.apply_risk_overrides


def test_nested_bool_override():
    config = {'risk_management': {'capital_sleeves': {'enabled': False, 'weights': {}}}}
    apply_risk_overrides(config, ['capital_sleeves.enabled=true'])
    assert config['risk_management']['capital_sleeves']['enabled'] is True


def test_numeric_and_string_typing():
    config = {'risk_management': {}}
    apply_risk_overrides(config, [
        'circuit_breaker.loss_threshold_pct=5.5',
        'circuit_breaker.lookback_days=7',
        'whipsaw_lockout.ref_symbol=ETH',
    ])
    cb = config['risk_management']['circuit_breaker']
    assert cb['loss_threshold_pct'] == 5.5
    assert cb['lookback_days'] == 7 and isinstance(cb['lookback_days'], int)
    assert config['risk_management']['whipsaw_lockout']['ref_symbol'] == 'ETH'


def test_creates_missing_path_and_ignores_invalid():
    config = {}
    apply_risk_overrides(config, ['capital_sleeves.enabled=false', 'not-a-valid-override'])
    assert config['risk_management']['capital_sleeves']['enabled'] is False


def test_none_overrides_is_noop():
    config = {'risk_management': {'x': 1}}
    apply_risk_overrides(config, None)
    assert config == {'risk_management': {'x': 1}}


def test_trading_section_override():
    apply_section_overrides = _mod.apply_section_overrides
    config = {'trading': {'maker_entries': {'enabled': False}}}
    apply_section_overrides(config, 'trading', ['maker_entries.enabled=true',
                                                'maker_entries.timeout_seconds=120'])
    assert config['trading']['maker_entries']['enabled'] is True
    assert config['trading']['maker_entries']['timeout_seconds'] == 120
