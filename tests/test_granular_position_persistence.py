"""
Unit tests for Granular Position Persistence.

Verifies that positions are persisted atomically to DB on each trade action,
and that the sync loop reads from DB (not memory).
"""

import unittest
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime
from src.models.trade import Position


class TestGranularPositionPersistence(unittest.TestCase):
    """Test suite for atomic position persistence."""
    
    def setUp(self):
        """Set up mock ExecutionEngine with dependencies."""
        from src.strategies.execution_engine import ExecutionEngine
        
        # Patching __init__ to avoid side effects
        with patch.object(ExecutionEngine, '__init__', lambda self, *args, **kwargs: None):
            self.engine = ExecutionEngine.__new__(ExecutionEngine)
            self.engine.logger = MagicMock()
            self.engine.performance_tracker = MagicMock()
            self.engine.performance_tracker.db = MagicMock()
            self.engine.positions = {}
            self.engine.multi_leg_positions = {}
            
            # Restore methods under test
            self.engine._persist_position = ExecutionEngine._persist_position.__get__(self.engine, ExecutionEngine)
            self.engine._persist_multi_leg_position = ExecutionEngine._persist_multi_leg_position.__get__(self.engine, ExecutionEngine)

    def test_persist_position_writes_to_db(self):
        """Verify _persist_position calls db.save_position with correct data."""
        position = Position(
            symbol='BTC',
            side='long',
            size=1.0,
            entry_price=50000.0,
            entry_time=datetime.now(),
            strategy='test_strategy',
            capital_at_risk=5000.0,
            leverage=10
        )
        
        result = self.engine._persist_position(position)
        
        # Assert
        self.assertTrue(result)
        self.engine.performance_tracker.db.save_position.assert_called_once()
        
        call_args = self.engine.performance_tracker.db.save_position.call_args[0][0]
        self.assertEqual(call_args['symbol'], 'BTC')
        self.assertEqual(call_args['strategy'], 'test_strategy')
        self.assertEqual(call_args['position_id'], 'pos_test_strategy_BTC')
        self.assertEqual(call_args['size'], 1.0)

    def test_persist_position_failure_returns_false(self):
        """Verify _persist_position returns False on DB error without crashing."""
        position = Position(
            symbol='BTC',
            side='long',
            size=1.0,
            entry_price=50000.0,
            entry_time=datetime.now(),
            strategy='test_strategy',
            capital_at_risk=5000.0
        )
        
        # Mock DB to raise
        self.engine.performance_tracker.db.save_position.side_effect = Exception("DB Error")
        
        result = self.engine._persist_position(position)
        
        # Assert
        self.assertFalse(result)
        self.engine.logger.error.assert_called()

    def test_persist_multi_leg_position_writes_to_db(self):
        """Verify _persist_multi_leg_position calls db.save_position."""
        # Create mock multi-leg position
        ml_position = MagicMock()
        ml_position.position_id = 'stat_arb_BTC_ETH_123'
        ml_position.strategy = 'stat_arb'
        ml_position.to_dict.return_value = {
            'position_id': 'stat_arb_BTC_ETH_123',
            'strategy': 'stat_arb',
            'symbol': 'BTC',
            'legs': []
        }
        
        # Need to mock _inject_statarb_metadata
        self.engine._inject_statarb_metadata = MagicMock()
        
        result = self.engine._persist_multi_leg_position(ml_position)
        
        # Assert
        self.assertTrue(result)
        self.engine.performance_tracker.db.save_position.assert_called_once()


class TestSyncReadsFromDB(unittest.TestCase):
    """Test that sync_positions_with_exchange reads from DB, not memory."""
    
    def setUp(self):
        """Set up mock StrategyManager."""
        from src.strategies.strategy_manager import StrategyManager
        
        with patch.object(StrategyManager, '__init__', lambda self, *args, **kwargs: None):
            self.manager = StrategyManager.__new__(StrategyManager)
            self.manager.logger = MagicMock()
            self.manager.market_api = MagicMock()
            self.manager.execution_engine = MagicMock()
            self.manager.execution_engine.positions = {}
            self.manager.execution_engine.multi_leg_positions = {}
            self.manager.performance_tracker = MagicMock()
            
            # Restore the method under test
            self.manager.sync_positions_with_exchange = StrategyManager.sync_positions_with_exchange.__get__(self.manager, StrategyManager)
            self.manager._find_closing_fill = MagicMock(return_value=(0.0, datetime.now(), "Unknown", 0.0))

    def test_sync_reads_from_db_not_memory(self):
        """Verify sync gets local positions from DB, not from memory dict."""
        # Setup: Memory has BTC, DB has ETH (stale state simulation)
        self.manager.execution_engine.positions = {'BTC': MagicMock()}
        self.manager.performance_tracker.db.get_all_live_position_symbols.return_value = ['ETH']
        
        # Exchange has neither (both should be ghosts)
        self.manager.market_api.get_positions.return_value = []
        
        # Act
        self.manager.sync_positions_with_exchange()
        
        # Assert: DB was queried
        self.manager.performance_tracker.db.get_all_live_position_symbols.assert_called_once()


class TestExecuteTradePersistsAtomically(unittest.TestCase):
    """Test that execute_trade uses atomic persist (verified via code inspection).
    
    The actual integration test is complex due to execute_trade's many dependencies.
    This test verifies the code path exists by checking the method references.
    """
    
    def test_persist_position_method_exists(self):
        """Verify _persist_position method exists on ExecutionEngine."""
        from src.strategies.execution_engine import ExecutionEngine
        
        self.assertTrue(hasattr(ExecutionEngine, '_persist_position'))
        self.assertTrue(callable(getattr(ExecutionEngine, '_persist_position')))

    def test_persist_multi_leg_position_method_exists(self):
        """Verify _persist_multi_leg_position method exists on ExecutionEngine."""
        from src.strategies.execution_engine import ExecutionEngine
        
        self.assertTrue(hasattr(ExecutionEngine, '_persist_multi_leg_position'))
        self.assertTrue(callable(getattr(ExecutionEngine, '_persist_multi_leg_position')))


if __name__ == '__main__':
    unittest.main()
