
import unittest
from unittest.mock import MagicMock, patch, ANY
from datetime import datetime
from src.models.trade import Position

class TestPositionSyncSmartAdoption(unittest.TestCase):
    """Test suite for 'Smart Adoption' position sync protocol."""
    
    def setUp(self):
        # Create mock StrategyManager
        # We can't import StrategyManager safely due to potential circular deps in test eiv
        # So we mock the class or use patch
        from src.strategies.strategy_manager import StrategyManager
        
        # Patching __init__ to avoid side effects
        with patch.object(StrategyManager, '__init__', lambda self, *args, **kwargs: None):
            self.manager = StrategyManager.__new__(StrategyManager)
            self.manager.logger = MagicMock()
            self.manager.market_api = MagicMock()
            self.manager.execution_engine = MagicMock()
            self.manager.execution_engine.multi_leg_positions = {}
            self.manager.performance_tracker = MagicMock()
            
            # Restore the method under test
            self.manager.sync_positions_with_exchange = StrategyManager.sync_positions_with_exchange.__get__(self.manager, StrategyManager)
            # Restore helper if needed (assuming _find_closing_fill is used)
            self.manager._find_closing_fill = MagicMock(return_value=(0.0, datetime.now(), "Unknown", 0.0))

    def test_unrecorded_position_immediate_close(self):
        """Protocol 1: Unrecorded Position -> Immediate Close (Reduce-Only)."""
        # Setup: Exchange has BTC, DB has nothing
        exchange_pos = [{'symbol': 'BTC', 'size': 1.0, 'side': 'LONG', 'entry_price': 50000.0}]
        self.manager.market_api.get_positions.return_value = exchange_pos
        self.manager.execution_engine.positions = {} # Empty local
        
        # Act
        self.manager.sync_positions_with_exchange()
        
        # Assert
        # Should call execute_order to CLOSE the unrecorded position
        # Side should be SHORT (opposite of LONG), Size 1.0, reduce_only=True
        self.manager.execution_engine.market_api.execute_order.assert_called_once_with(
            symbol='BTC',
            side='sell',
            size=1.0,
            reduce_only=True,
            urgency='high'
        )
        self.manager.logger.warning.assert_called()

    def test_mismatch_external_reduce_smart_adoption(self):
        """Protocol 2A: Exchange < DB -> Adopt Exchange Size, Record Phantom Trade."""
        # Setup: Exchange has 0.5, DB has 1.0
        exchange_pos = [{'symbol': 'BTC', 'size': 0.5, 'side': 'LONG', 'entry_price': 50000.0}]
        self.manager.market_api.get_positions.return_value = exchange_pos
        
        local_pos = Position(
            symbol='BTC', side='long', size=1.0, entry_price=50000.0,
            entry_time=datetime.now(), strategy='test', capital_at_risk=1000.0
        )
        self.manager.execution_engine.positions = {'BTC': local_pos}
        self.manager.execution_engine.save_positions_to_db = MagicMock()
        
        # Mock DB to return BTC so sync finds it
        mock_db = MagicMock()
        mock_db.get_all_live_position_symbols.return_value = ['BTC']
        self.manager.performance_tracker.db = mock_db
        
        # Act
        self.manager.sync_positions_with_exchange()
        
        # Assert
        # 1. DB Size updated
        self.assertEqual(local_pos.size, 0.5)
        
        # 2. Phantom Trade Recorded for difference (0.5)
        # Verify performance_tracker.record_trade_from_position called
        self.manager.performance_tracker.record_trade_from_position.assert_called_once()
        call_kwargs = self.manager.performance_tracker.record_trade_from_position.call_args[1]
        
        self.assertEqual(call_kwargs['symbol'], 'BTC')
        self.assertEqual(call_kwargs['size'], 0.5) # Diff
        self.assertEqual(call_kwargs['exit_reason'], 'External Partial Close')
        self.assertEqual(call_kwargs['side'], 'long')
        
        # 3. Position persisted atomically via _persist_position
        self.manager.execution_engine._persist_position.assert_called()

    def test_mismatch_external_add_smart_adoption(self):
        """Protocol 2B: Exchange > DB -> Adopt Exchange Size & Price, Recalc Risk."""
        # Setup: Exchange has 1.5 @ $52000. DB has 1.0 @ $50000.
        # User added 0.5 externally at a higher price.
        exchange_pos = [{'symbol': 'BTC', 'size': 1.5, 'side': 'LONG', 'entry_price': 52000.0, 'leverage': 10}]
        self.manager.market_api.get_positions.return_value = exchange_pos
        
        local_pos = Position(
            symbol='BTC', side='long', size=1.0, entry_price=50000.0,
            entry_time=datetime.now(), strategy='test',
            capital_at_risk=5000.0, # $50k / 10
            leverage=10
        )
        self.manager.execution_engine.positions = {'BTC': local_pos}
        self.manager.execution_engine.save_positions_to_db = MagicMock()
        
        # Mock DB to return BTC so sync finds it
        mock_db = MagicMock()
        mock_db.get_all_live_position_symbols.return_value = ['BTC']
        self.manager.performance_tracker.db = mock_db
        
        # Act
        self.manager.sync_positions_with_exchange()
        
        # Assert
        # 1. DB Size Updated
        self.assertEqual(local_pos.size, 1.5)
        
        # 2. DB Entry Price Updated (Adopt Exchange Truth)
        self.assertEqual(local_pos.entry_price, 52000.0)
        
        # 3. Risk Recalculated
        # New Notional = 1.5 * 52000 = 78000. Risk = 7800 / 10 = 7800.
        # Original was 5000.
        expected_risk = (1.5 * 52000.0) / 10
        self.assertEqual(local_pos.capital_at_risk, expected_risk)
        
        # 4. Position persisted atomically via _persist_position
        self.manager.execution_engine._persist_position.assert_called()

if __name__ == '__main__':
    unittest.main()
