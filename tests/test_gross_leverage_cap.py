"""
Tests for the portfolio gross-leverage ceiling:
- gross notional sums single-leg positions AND every multi-leg leg
- delta-neutral (funding-arb style) pairs count BOTH legs at full notional
- entries blocked when projected gross exceeds max_gross_leverage x equity
- under the cap allows; disabled flag honored; zero equity fails open
"""

import logging
from types import SimpleNamespace

import pytest

from src.strategies.execution_engine import ExecutionEngine


def make_engine(equity=1000.0, positions=None, ml_legs=None,
                enabled=True, max_gross=2.0):
    eng = ExecutionEngine.__new__(ExecutionEngine)
    eng.logger = logging.getLogger('test_gross')
    eng.config = {'risk_management': {'gross_leverage_cap': {
        'enabled': enabled, 'max_gross_leverage': max_gross}}}
    eng.portfolio_manager = SimpleNamespace(total_equity=equity)
    eng.positions = positions or {}
    if ml_legs:
        eng.multi_leg_positions = {
            'ml_1': SimpleNamespace(legs=[
                SimpleNamespace(size=s, entry_price=p) for s, p in ml_legs])}
    else:
        eng.multi_leg_positions = {}
    return eng


def pos(size, price, current=None):
    return SimpleNamespace(size=size, entry_price=price, current_price=current)


class TestGrossNotional:
    def test_sums_single_leg_at_current_price(self):
        eng = make_engine(positions={
            'BTC': pos(0.01, 50000, current=60000),   # |0.01*60000| = 600
            'XPL': pos(-203, 0.0622),                 # |203*0.0622| ~ 12.63
        })
        assert eng._gross_notional() == pytest.approx(600 + 12.6266)

    def test_delta_neutral_pair_counts_both_legs(self):
        # Funding-arb shape: long spot + short perp, same notional
        eng = make_engine(ml_legs=[(100, 5.0), (-100, 5.0)])
        assert eng._gross_notional() == pytest.approx(1000.0)  # 2 x 500

    def test_mixed_book(self):
        eng = make_engine(positions={'CRV': pos(55.5, 0.2282)},
                          ml_legs=[(10, 3.0)])
        assert eng._gross_notional() == pytest.approx(55.5 * 0.2282 + 30.0)


class TestGrossCap:
    def test_blocks_projected_breach(self):
        # gross 1500 + new 600 = 2100 > 2.0 x 1000
        eng = make_engine(positions={'A': pos(1, 1500)})
        assert eng._gross_cap_blocks(600.0, 'B')

    def test_allows_under_cap(self):
        eng = make_engine(positions={'A': pos(1, 1500)})
        assert not eng._gross_cap_blocks(400.0, 'B')

    def test_multi_leg_planned_notional_counts(self):
        # Existing 1500; planned funding-arb pair 2 legs x 300 = 600 -> blocked
        eng = make_engine(positions={'A': pos(1, 1500)})
        assert eng._gross_cap_blocks(300.0 * 2, 'FARB')

    def test_disabled_never_blocks(self):
        eng = make_engine(positions={'A': pos(1, 10000)}, enabled=False)
        assert not eng._gross_cap_blocks(10000.0, 'B')

    def test_zero_equity_fails_open(self):
        # Equity unknown/zero: margin checks elsewhere already prevent
        # entries; the cap must not divide by zero or block spuriously
        eng = make_engine(equity=0.0)
        assert not eng._gross_cap_blocks(100.0, 'B')

    def test_custom_multiplier(self):
        eng = make_engine(positions={'A': pos(1, 900)}, max_gross=1.0)
        assert eng._gross_cap_blocks(200.0, 'B')      # 1100 > 1.0x1000
        assert not eng._gross_cap_blocks(50.0, 'B')   # 950 < 1000

    def test_non_numeric_inputs_fail_open(self):
        # A backstop must not block on bad data (e.g. mocked/missing values)
        from unittest.mock import MagicMock
        eng = make_engine()
        eng.portfolio_manager = SimpleNamespace(total_equity=MagicMock())
        assert not eng._gross_cap_blocks(MagicMock(), 'B')
