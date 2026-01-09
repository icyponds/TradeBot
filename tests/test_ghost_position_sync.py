"""
Unit tests for Ghost Position Reconciliation (sync_positions_with_exchange).
"""

import unittest
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime
import os

from src.models.trade import Position


class TestGhostPositionSync(unittest.TestCase):
    """Test sync_positions_with_exchange functionality."""
    
    def setUp(self):
        """Set up test fixtures."""
        # We'll mock StrategyManager and its dependencies
        self.mock_market_api = MagicMock()
        self.mock_execution_engine = MagicMock()
        self.mock_performance_tracker = MagicMock()
        
    def _create_mock_strategy_manager(self):
        """Create a minimal mock StrategyManager with sync method."""
        # Import here to avoid circular imports during test collection
        from src.strategies.strategy_manager import StrategyManager
        
        # Create a partial mock - we'll patch the dependencies
        with patch.object(StrategyManager, '__init__', lambda self, *args, **kwargs: None):
            manager = StrategyManager.__new__(StrategyManager)
            
            # Set up mocks
            manager.market_api = self.mock_market_api
            manager.execution_engine = self.mock_execution_engine
            manager.performance_tracker = self.mock_performance_tracker
            manager.logger = MagicMock()
            manager.consecutive_errors = 0
            
            # Import the real method
            manager.sync_positions_with_exchange = StrategyManager.sync_positions_with_exchange.__get__(manager, StrategyManager)
            
            return manager

    def test_no_ghost_positions(self):
        """Test that no action is taken when exchange and DB positions match."""
        manager = self._create_mock_strategy_manager()
        
        # Setup: Both have BTC position
        self.mock_market_api.get_positions.return_value = [
            {'symbol': 'BTC', 'size': 0.5}
        ]
        self.mock_execution_engine.positions = {
            'BTC': Position(
                symbol='BTC', side='long', size=0.5, entry_price=42000.0,
                entry_time=datetime.now(), strategy='csm_4h'
            )
        }
        
        # Execute
        manager.sync_positions_with_exchange()
        
        # Verify: No deletion, no trade recording
        self.mock_performance_tracker.record_trade_from_position.assert_not_called()
        self.mock_execution_engine.save_positions_to_db.assert_not_called()

    def test_ghost_position_detected_and_closed(self):
        """Test that ghost position is detected and closed locally."""
        manager = self._create_mock_strategy_manager()
        
        # Setup: Exchange has no positions, but DB has BTC
        self.mock_market_api.get_positions.return_value = []
        self.mock_market_api.get_user_fills.return_value = []  # No fills for simplicity
        
        ghost_position = Position(
            symbol='BTC', side='long', size=0.5, entry_price=42000.0,
            entry_time=datetime.now(), strategy='csm_4h',
            capital_at_risk=1000.0, leverage=5.0
        )
        # Use a real dict, not a MagicMock
        positions_dict = {'BTC': ghost_position}
        self.mock_execution_engine.positions = positions_dict
        
        # Mock the DB delete
        mock_db = MagicMock()
        self.mock_performance_tracker.db = mock_db
        
        # Execute
        manager.sync_positions_with_exchange()
        
        # Verify: Trade recorded for PnL
        self.mock_performance_tracker.record_trade_from_position.assert_called_once()
        call_kwargs = self.mock_performance_tracker.record_trade_from_position.call_args[1]
        self.assertEqual(call_kwargs['symbol'], 'BTC')
        self.assertEqual(call_kwargs['exit_reason'], 'External Close')
        
        # Verify: Position removed from local state
        self.assertNotIn('BTC', positions_dict)
        
        # Verify: DB delete called
        mock_db.delete_position.assert_called_once_with('pos_csm_4h_BTC')

    def test_ghost_position_with_fills_gets_correct_exit_price(self):
        """Test that exit price is extracted from fills when available."""
        manager = self._create_mock_strategy_manager()
        
        entry_time = datetime(2024, 1, 1, 12, 0, 0)
        
        # Setup: Exchange has no positions
        self.mock_market_api.get_positions.return_value = []
        
        # Setup: Fills show the position was closed at 45000
        self.mock_market_api.get_user_fills.return_value = [
            {
                'coin': 'BTC',
                'side': 'Sell',  # Opposite of long position
                'px': '45000.0',
                'time': int(entry_time.timestamp() * 1000) + 3600000,  # 1 hour after entry
                'dir': 'close'
            }
        ]
        
        ghost_position = Position(
            symbol='BTC', side='long', size=0.5, entry_price=42000.0,
            entry_time=entry_time, strategy='csm_4h',
            capital_at_risk=1000.0, leverage=5.0
        )
        positions_dict = {'BTC': ghost_position}
        self.mock_execution_engine.positions = positions_dict
        
        mock_db = MagicMock()
        self.mock_performance_tracker.db = mock_db
        
        # Execute
        manager.sync_positions_with_exchange()
        
        # Verify: Exit price from fills is used
        call_kwargs = self.mock_performance_tracker.record_trade_from_position.call_args[1]
        self.assertEqual(call_kwargs['exit_price'], 45000.0)

    def test_liquidation_detected_from_fills(self):
        """Test that liquidation reason is detected from fills."""
        manager = self._create_mock_strategy_manager()
        
        entry_time = datetime(2024, 1, 1, 12, 0, 0)
        
        self.mock_market_api.get_positions.return_value = []
        self.mock_market_api.get_user_fills.return_value = [
            {
                'coin': 'BTC',
                'side': 'Sell',
                'px': '38000.0',
                'time': int(entry_time.timestamp() * 1000) + 3600000,
                'dir': 'liquidation'  # Liquidation indicator
            }
        ]
        
        ghost_position = Position(
            symbol='BTC', side='long', size=0.5, entry_price=42000.0,
            entry_time=entry_time, strategy='csm_4h',
            capital_at_risk=1000.0, leverage=5.0
        )
        positions_dict = {'BTC': ghost_position}
        self.mock_execution_engine.positions = positions_dict
        
        mock_db = MagicMock()
        self.mock_performance_tracker.db = mock_db
        
        # Execute
        manager.sync_positions_with_exchange()
        
        # Verify: Liquidation reason detected
        call_kwargs = self.mock_performance_tracker.record_trade_from_position.call_args[1]
        self.assertEqual(call_kwargs['exit_reason'], 'Liquidation')
        self.assertEqual(call_kwargs['exit_price'], 38000.0)

    def test_ghost_multi_leg_position_detected(self):
        """Test that ghost multi-leg position is detected and closed."""
        manager = self._create_mock_strategy_manager()
        
        # Setup: Multi-Leg Position exists locally
        from src.strategies.execution_engine import MultiLegPosition
        
        # Create a mock MultiLegPosition
        # structure of MultiLegPosition is simpler, usually has .legs list
        mock_leg = MagicMock()
        mock_leg.symbol = 'BTC'
        mock_pos = MagicMock()
        mock_pos.position_id = 'ml_123'
        mock_pos.legs = [mock_leg]
        
        # execution_engine.multi_leg_positions is where they live
        ml_dict = {'ml_123': mock_pos}
        # Use configure_mock to ensure it sticks
        self.mock_execution_engine.configure_mock(multi_leg_positions=ml_dict)
        
        # Also try explicitly setting the MagicMock return value if it's being treated as a method (unlikely but possible)
        # self.mock_execution_engine.multi_leg_positions = ml_dict
        
        # Setup: Exchange has NO positions (BTC is missing)
        self.mock_market_api.get_positions.return_value = []
        
        # Mock DB
        mock_db = MagicMock()
        self.mock_performance_tracker.db = mock_db
        
        # Execution Engine needs positions dict too (single leg)
        self.mock_execution_engine.positions = {}
        
        # Execute
        manager.sync_positions_with_exchange()
        
        # Verify: Position removed from local dict
        self.assertNotIn('ml_123', ml_dict)
        
        # Verify: DB delete called
        mock_db.delete_position.assert_called_with('ml_123')



class TestSyncPositionsPeriodic(unittest.TestCase):
    """Test _sync_positions_periodic throttling."""
    
    def test_sync_throttled_within_interval(self):
        """Test that sync is not called if within 5 minute interval."""
        from src.strategies.strategy_manager import StrategyManager
        import time
        
        with patch.object(StrategyManager, '__init__', lambda self, *args, **kwargs: None):
            manager = StrategyManager.__new__(StrategyManager)
            manager.logger = MagicMock()
            manager.sync_positions_with_exchange = MagicMock()
            manager._sync_positions_periodic = StrategyManager._sync_positions_periodic.__get__(manager, StrategyManager)
            
            # Set last sync to recent (within 300s)
            manager.last_position_sync = time.time() - 60  # 1 minute ago
            
            # Execute
            manager._sync_positions_periodic()
            
            # Verify: sync was NOT called
            manager.sync_positions_with_exchange.assert_not_called()

    def test_sync_called_after_interval(self):
        """Test that sync is called if past 5 minute interval."""
        from src.strategies.strategy_manager import StrategyManager
        import time
        
        with patch.object(StrategyManager, '__init__', lambda self, *args, **kwargs: None):
            manager = StrategyManager.__new__(StrategyManager)
            manager.logger = MagicMock()
            manager.sync_positions_with_exchange = MagicMock()
            manager._sync_positions_periodic = StrategyManager._sync_positions_periodic.__get__(manager, StrategyManager)
            
            # Set last sync to old (past 300s)
            manager.last_position_sync = time.time() - 400  # 6+ minutes ago
            
            # Execute
            manager._sync_positions_periodic()
            
            # Verify: sync WAS called
            manager.sync_positions_with_exchange.assert_called_once()


if __name__ == '__main__':
    unittest.main()
