"""
Tests for the same-underlying concentration guard:
- dex-prefixed duplicates collapse (xyz:GOLD vs cash:GOLD)
- tokenized aliases collapse (PAXG vs xyz:GOLD)
- _SPOT suffixes collapse (BTC_SPOT vs BTC)
- different underlyings are not blocked
- multi-leg legs participate (position-logic parity)
- disable flag honored
"""

import logging
from types import SimpleNamespace

from src.strategies.strategy_manager import StrategyManager


def make_manager(open_symbols=(), ml_leg_symbols=(), enabled=True):
    mgr = StrategyManager.__new__(StrategyManager)
    mgr.config = {"risk_management": {"underlying_concentration": {"enabled": enabled}}}
    # positions / multi_leg_positions are properties delegating to the engine
    legs = [SimpleNamespace(symbol=s) for s in ml_leg_symbols]
    mgr.execution_engine = SimpleNamespace(
        positions={s: SimpleNamespace(symbol=s) for s in open_symbols},
        multi_leg_positions=({"ml_1": SimpleNamespace(legs=legs)} if legs else {}),
    )
    mgr.logger = logging.getLogger("test_uc")
    return mgr


class TestUnderlyingKey:
    def test_dex_prefix_stripped(self):
        mgr = make_manager()
        assert mgr._underlying_key('xyz:GOLD') == 'GOLD'
        assert mgr._underlying_key('cash:GOLD') == 'GOLD'

    def test_aliases_applied(self):
        mgr = make_manager()
        assert mgr._underlying_key('PAXG') == 'GOLD'
        assert mgr._underlying_key('kPEPE') == 'PEPE'
        assert mgr._underlying_key('UBTC') == 'BTC'

    def test_spot_suffix_stripped(self):
        mgr = make_manager()
        assert mgr._underlying_key('BTC_SPOT') == 'BTC'
        assert mgr._underlying_key('HYPE_SPOT') == 'HYPE'


class TestConcentrationGuard:
    def test_blocks_tokenized_duplicate(self):
        # The live 2026-06-10 case: PAXG open, xyz:GOLD signal
        mgr = make_manager(open_symbols=['PAXG'])
        assert mgr._underlying_concentration_blocked('xyz:GOLD')

    def test_blocks_cross_dex_duplicate(self):
        mgr = make_manager(open_symbols=['xyz:GOLD'])
        assert mgr._underlying_concentration_blocked('cash:GOLD')

    def test_allows_different_underlying(self):
        mgr = make_manager(open_symbols=['PAXG', 'CRV'])
        assert not mgr._underlying_concentration_blocked('xyz:AMZN')

    def test_same_symbol_not_self_blocked(self):
        # Re-evaluating the same symbol is the conflict resolver's job
        mgr = make_manager(open_symbols=['xyz:GOLD'])
        assert not mgr._underlying_concentration_blocked('xyz:GOLD')

    def test_multi_leg_legs_counted(self):
        mgr = make_manager(ml_leg_symbols=['PURR', 'PURR_SPOT'])
        assert mgr._underlying_concentration_blocked('PURR')
        assert not mgr._underlying_concentration_blocked('BTC')

    def test_disable_flag(self):
        mgr = make_manager(open_symbols=['PAXG'], enabled=False)
        assert not mgr._underlying_concentration_blocked('xyz:GOLD')
