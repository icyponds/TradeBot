"""
Tests for broken multi-leg position detection and unwinding.

When a multi-leg position has some legs missing on the exchange (but others remain),
the position is "broken" and delta neutrality is compromised. The sync should:
1. Detect the broken position
2. Unwind (close) the remaining legs
3. Record the trade and remove from DB
"""

import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime


class TestBrokenMultiLegSync(unittest.TestCase):
    """Test suite for broken multi-leg position detection."""
    
    def setUp(self):
        from src.strategies.strategy_manager import StrategyManager
        from src.models.trade import MultiLegPosition, PositionLeg
        
        # Patching __init__ to avoid side effects
        with patch.object(StrategyManager, '__init__', lambda self, *args, **kwargs: None):
            self.manager = StrategyManager.__new__(StrategyManager)
            self.manager.logger = MagicMock()
            self.manager.market_api = MagicMock()
            self.manager.execution_engine = MagicMock()
            self.manager.performance_tracker = MagicMock()
            
            # Create a multi-leg position with two legs
            leg1 = PositionLeg(
                symbol='XRP',
                market_type='perp',
                side='short',
                size=62.0,
                entry_price=1.915,
                order_id='123'
            )
            leg2 = PositionLeg(
                symbol='ETH',
                market_type='perp',
                side='long',
                size=0.0007,
                entry_price=2907.1,
                order_id='456'
            )
            
            self.multi_leg_pos = MultiLegPosition(
                position_id='stat_arb_1h_XRP_123456',
                strategy='stat_arb_1h',
                entry_time=datetime.now(),
                legs=[leg1, leg2],
                capital_at_risk=100.0,
                metadata={}
            )
            
            self.manager.execution_engine.multi_leg_positions = {
                'stat_arb_1h_XRP_123456': self.multi_leg_pos
            }
            
            # Restore the method under test
            self.manager.sync_positions_with_exchange = StrategyManager.sync_positions_with_exchange.__get__(
                self.manager, StrategyManager
            )
            self.manager._find_closing_fill = MagicMock(return_value=(0.0, datetime.now(), "Unknown", 0.0))

    def test_broken_position_detected_when_one_leg_missing(self):
        """Test that a broken position is detected when one leg is missing from exchange."""
        # Exchange has XRP but NOT ETH (ETH leg was closed externally)
        exchange_positions = [
            {'symbol': 'XRP', 'size': 62.0, 'side': 'short', 'szi': -62.0, 'entry_price': 1.915}
        ]
        self.manager.market_api.get_positions.return_value = exchange_positions
        self.manager.execution_engine.positions = {}
        
        # Mock DB
        mock_db = MagicMock()
        mock_db.get_all_live_position_symbols.return_value = []
        self.manager.performance_tracker.db = mock_db
        
        # Mock execute_order to succeed
        self.manager.market_api.execute_order.return_value = {
            'filled_size': 62.0,
            'avg_fill_price': 1.90,
            'total_fee': 0.5
        }
        
        # Act
        self.manager.sync_positions_with_exchange()
        
        # Assert - should log critical about broken position
        critical_calls = [call for call in self.manager.logger.critical.call_args_list]
        self.assertTrue(len(critical_calls) > 0, "Should log critical for broken position")
        
        # Should have tried to close the remaining XRP leg
        self.manager.market_api.execute_order.assert_called()
        
        # Should record the trade
        self.manager.performance_tracker.record_trade_from_position.assert_called_once()
        call_kwargs = self.manager.performance_tracker.record_trade_from_position.call_args[1]
        self.assertEqual(call_kwargs['exit_reason'], 'Broken Position - Leg Missing')
        
        # Should delete from DB
        mock_db.delete_position.assert_called_with('stat_arb_1h_XRP_123456')

    def test_full_ghost_not_treated_as_broken(self):
        """Test that positions with ALL legs missing are treated as ghosts, not broken."""
        # Exchange has NEITHER XRP nor ETH (both closed externally)
        exchange_positions = []
        self.manager.market_api.get_positions.return_value = exchange_positions
        self.manager.execution_engine.positions = {}
        
        mock_db = MagicMock()
        mock_db.get_all_live_position_symbols.return_value = []
        self.manager.performance_tracker.db = mock_db
        
        # Act
        self.manager.sync_positions_with_exchange()
        
        # Should NOT log critical (broken), should log warning (ghost)
        critical_calls = [call for call in self.manager.logger.critical.call_args_list]
        self.assertEqual(len(critical_calls), 0, "Full ghost should not trigger critical log")
        
        # Should log warning about ghost
        self.manager.logger.warning.assert_called()

    def test_healthy_multileg_not_flagged(self):
        """Test that healthy multi-leg positions with all legs present are not flagged."""
        # Exchange has BOTH XRP and ETH
        exchange_positions = [
            {'symbol': 'XRP', 'size': 62.0, 'side': 'short', 'szi': -62.0, 'entry_price': 1.915},
            {'symbol': 'ETH', 'size': 0.0007, 'side': 'long', 'szi': 0.0007, 'entry_price': 2907.1}
        ]
        self.manager.market_api.get_positions.return_value = exchange_positions
        self.manager.execution_engine.positions = {}
        
        mock_db = MagicMock()
        mock_db.get_all_live_position_symbols.return_value = []
        self.manager.performance_tracker.db = mock_db
        
        # Act
        self.manager.sync_positions_with_exchange()
        
        # Should NOT log critical or delete the position
        critical_calls = [call for call in self.manager.logger.critical.call_args_list]
        self.assertEqual(len(critical_calls), 0, "Healthy position should not trigger critical")
        mock_db.delete_position.assert_not_called()


if __name__ == '__main__':
    unittest.main()

