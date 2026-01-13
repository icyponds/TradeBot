"""
Unit tests for multi-leg conflict resolution.

Tests the conflict detection and resolution logic when opening multi-leg positions
that have legs conflicting with existing positions.
"""
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime

from src.strategies.execution_engine import ExecutionEngine
from src.models.trade import MultiLegPosition, PositionLeg, Position


class MockPosition:
    """Mock single-leg position for testing."""
    def __init__(self, symbol, side='long', strategy='test_strategy', **kwargs):
        self.symbol = symbol
        self.side = side
        self.strategy = strategy
        self.entry_time = kwargs.get('entry_time', datetime.now())
        self.entry_price = kwargs.get('entry_price', 100.0)
        self.current_price = kwargs.get('current_price', 100.0)
        self.capital_at_risk = kwargs.get('capital_at_risk', 100.0)
        self.unrealized_pnl = kwargs.get('unrealized_pnl', 0.0)
        self.unrealized_pnl_percentage = kwargs.get('unrealized_pnl_percentage', 0.0)
        self.entry_signal_strength = kwargs.get('entry_signal_strength', 0.5)
        self.highest_price = kwargs.get('highest_price', None)
        self.lowest_price = kwargs.get('lowest_price', None)
        self.take_profit = kwargs.get('take_profit', None)
        self.stop_loss = kwargs.get('stop_loss', None)


class MockMultiLegPosition:
    """Mock multi-leg position for testing."""
    def __init__(self, position_id, primary_symbol, legs, **kwargs):
        self.position_id = position_id
        self.primary_symbol = primary_symbol
        self.strategy = kwargs.get('strategy', 'stat_arb_4h')
        self.entry_time = kwargs.get('entry_time', datetime.now())
        self.capital_at_risk = kwargs.get('capital_at_risk', 100.0)
        self.total_notional = kwargs.get('total_notional', 500.0)
        self.unrealized_pnl = kwargs.get('unrealized_pnl', 0.0)
        self.legs = legs


class MockLeg:
    """Mock position leg for testing."""
    def __init__(self, symbol, side='long', **kwargs):
        self.symbol = symbol
        self.side = side
        self.market_type = kwargs.get('market_type', 'perp')
        self.size = kwargs.get('size', 1.0)
        self.entry_price = kwargs.get('entry_price', 100.0)


def create_mock_execution_engine():
    """Create a mock ExecutionEngine with all required dependencies."""
    mock_config = {'trading': {}, 'risk_management': {}}
    mock_market_api = MagicMock()
    mock_leverage_manager = MagicMock()
    mock_portfolio_manager = MagicMock()
    mock_performance_tracker = MagicMock()
    mock_pair_selector = MagicMock()
    
    engine = ExecutionEngine(
        mock_config,
        mock_market_api,
        mock_leverage_manager,
        mock_portfolio_manager,
        mock_performance_tracker,
        mock_pair_selector
    )
    return engine


class TestMultiLegConflictDetection(unittest.TestCase):
    """Tests for _detect_leg_conflicts method."""
    
    def setUp(self):
        self.engine = create_mock_execution_engine()
    
    def test_detect_no_conflicts(self):
        """No conflicts when no positions exist."""
        self.engine.positions = {}
        self.engine.multi_leg_positions = {}
        
        legs = [{'symbol': 'BTC'}, {'symbol': 'ETH'}]
        conflicts = self.engine._detect_leg_conflicts(legs)
        
        self.assertEqual(len(conflicts), 0)
    
    def test_detect_single_leg_conflict(self):
        """Detect conflict with existing single-leg position."""
        btc_position = MockPosition('BTC')
        self.engine.positions = {'BTC': btc_position}
        self.engine.multi_leg_positions = {}
        
        legs = [{'symbol': 'BTC'}, {'symbol': 'ETH'}]
        conflicts = self.engine._detect_leg_conflicts(legs)
        
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]['type'], 'single_leg')
        self.assertEqual(conflicts[0]['symbol'], 'BTC')
        self.assertEqual(conflicts[0]['position'], btc_position)
    
    def test_detect_multi_leg_conflict(self):
        """Detect conflict with existing multi-leg position leg."""
        self.engine.positions = {}
        
        eth_arb = MockMultiLegPosition(
            position_id='eth_arb_123',
            primary_symbol='ETH',
            legs=[
                MockLeg('ETH', 'long'),
                MockLeg('BTC', 'short')
            ]
        )
        self.engine.multi_leg_positions = {'eth_arb_123': eth_arb}
        
        # New multi-leg wants to use BTC
        legs = [{'symbol': 'BTC'}, {'symbol': 'SOL'}]
        conflicts = self.engine._detect_leg_conflicts(legs)
        
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]['type'], 'multi_leg')
        self.assertEqual(conflicts[0]['symbol'], 'BTC')
        self.assertEqual(conflicts[0]['position'], eth_arb)
    
    def test_detect_multiple_conflicts(self):
        """Detect multiple conflicts from different sources."""
        sol_position = MockPosition('SOL')
        self.engine.positions = {'SOL': sol_position}
        
        eth_arb = MockMultiLegPosition(
            position_id='eth_arb_123',
            primary_symbol='ETH',
            legs=[MockLeg('ETH', 'long'), MockLeg('BTC', 'short')]
        )
        self.engine.multi_leg_positions = {'eth_arb_123': eth_arb}
        
        # New multi-leg wants BTC and SOL (conflicts with both)
        legs = [{'symbol': 'BTC'}, {'symbol': 'SOL'}]
        conflicts = self.engine._detect_leg_conflicts(legs)
        
        self.assertEqual(len(conflicts), 2)
        types = {c['type'] for c in conflicts}
        self.assertEqual(types, {'single_leg', 'multi_leg'})


class TestMultiLegConflictResolution(unittest.TestCase):
    """Tests for _resolve_leg_conflicts method."""
    
    def setUp(self):
        self.engine = create_mock_execution_engine()
        self.mock_strategy_manager = MagicMock()
        self.mock_strategy_manager._check_position_limit.return_value = False
    
    def test_resolve_empty_conflicts(self):
        """No conflicts should proceed."""
        result = self.engine._resolve_leg_conflicts([], 0.7, self.mock_strategy_manager)
        self.assertTrue(result)
    
    def test_resolve_block_when_existing_stronger(self):
        """Block new position when existing position has higher score."""
        btc_position = MockPosition('BTC')
        conflicts = [{'type': 'single_leg', 'symbol': 'BTC', 'position': btc_position}]
        
        # Existing score = 0.8, new strength = 0.5, threshold = 0.10
        # 0.5 > 0.8 + 0.10? No -> BLOCK
        self.mock_strategy_manager._get_position_profitability_score.return_value = 0.8
        self.mock_strategy_manager._should_displace_position.return_value = False
        
        result = self.engine._resolve_leg_conflicts(conflicts, 0.5, self.mock_strategy_manager)
        
        self.assertFalse(result)
        self.engine.close_position = MagicMock()
        self.engine.close_position.assert_not_called()
    
    def test_resolve_displace_when_new_stronger(self):
        """Displace existing position when new signal is stronger."""
        btc_position = MockPosition('BTC')
        conflicts = [{'type': 'single_leg', 'symbol': 'BTC', 'position': btc_position}]
        
        # Existing score = 0.4, new strength = 0.8, threshold = 0.10
        # 0.8 > 0.4 + 0.10? Yes -> DISPLACE
        self.mock_strategy_manager._get_position_profitability_score.return_value = 0.4
        self.mock_strategy_manager._should_displace_position.return_value = True
        
        # Mock close_position to succeed
        self.engine.close_position = MagicMock(return_value=True)
        
        result = self.engine._resolve_leg_conflicts(conflicts, 0.8, self.mock_strategy_manager)
        
        self.assertTrue(result)
        self.engine.close_position.assert_called_once_with('BTC', reason='leg_conflict_displacement')
    
    def test_resolve_multi_leg_displacement(self):
        """Displace multi-leg position when new signal is stronger."""
        eth_arb = MockMultiLegPosition(
            position_id='eth_arb_123',
            primary_symbol='ETH',
            legs=[MockLeg('ETH'), MockLeg('BTC')]
        )
        conflicts = [{'type': 'multi_leg', 'symbol': 'BTC', 'position': eth_arb}]
        
        self.mock_strategy_manager._get_multi_leg_profitability_score.return_value = 0.3
        self.mock_strategy_manager._should_displace_position.return_value = True
        
        self.engine.execute_multi_leg_exit = MagicMock()
        
        result = self.engine._resolve_leg_conflicts(conflicts, 0.9, self.mock_strategy_manager)
        
        self.assertTrue(result)
        self.engine.execute_multi_leg_exit.assert_called_once()
        call_args = self.engine.execute_multi_leg_exit.call_args
        self.assertEqual(call_args.kwargs['symbol'], 'ETH')  # Primary symbol
        self.assertEqual(call_args.kwargs['signal']['reason'], 'leg_conflict_displacement')
    
    def test_resolve_all_or_nothing(self):
        """If any conflict blocks, the whole entry is blocked."""
        btc_position = MockPosition('BTC')
        eth_arb = MockMultiLegPosition(
            position_id='eth_arb_123',
            primary_symbol='ETH',
            legs=[MockLeg('ETH'), MockLeg('SOL')]
        )
        
        conflicts = [
            {'type': 'single_leg', 'symbol': 'BTC', 'position': btc_position},
            {'type': 'multi_leg', 'symbol': 'SOL', 'position': eth_arb}
        ]
        
        # BTC would be displaced, but SOL's arb is stronger
        def displacement_side_effect(score, strength):
            if score == 0.3:  # BTC score
                return True  # Would displace BTC
            else:  # SOL arb score = 0.8
                return False  # Blocked by arb
        
        def score_side_effect(symbol, strength):
            return 0.3  # BTC score
        
        self.mock_strategy_manager._get_position_profitability_score.side_effect = score_side_effect
        self.mock_strategy_manager._get_multi_leg_profitability_score.return_value = 0.8
        self.mock_strategy_manager._should_displace_position.side_effect = displacement_side_effect
        
        result = self.engine._resolve_leg_conflicts(conflicts, 0.6, self.mock_strategy_manager)
        
        # Second conflict should block the whole thing
        self.assertFalse(result)


if __name__ == '__main__':
    unittest.main()
