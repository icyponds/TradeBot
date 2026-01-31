
import unittest
from unittest.mock import MagicMock, patch, ANY
from src.strategies.execution_engine import ExecutionEngine
from src.models.trade import PositionLeg, MultiLegPosition
from datetime import datetime
import time

class TestMultiLegParallelExit(unittest.TestCase):
    def setUp(self):
        self.config = {
            'trading': {
                'max_positions_percentage': 90,
                'base_currency': 'USDC'
            }
        }
        self.market_api = MagicMock()
        self.leverage_manager = MagicMock()
        self.portfolio_manager = MagicMock()
        self.performance_tracker = MagicMock()
        self.pair_selector = MagicMock()
        
        # Initialize ExecutionEngine
        with patch('src.strategies.execution_engine.ExecutionEngine.load_positions_from_db'):
            self.execution_engine = ExecutionEngine(
                self.config,
                self.market_api,
                self.leverage_manager,
                self.portfolio_manager,
                self.performance_tracker,
                self.pair_selector
            )

    def test_parallel_multi_leg_exit(self):
        """Verify that multiple legs are executed and position is cleaned up."""
        # 1. Setup Position with 2 legs
        leg1 = PositionLeg(symbol="LINK", side="long", size=10.0, entry_price=10.0, market_type="perp")
        leg2 = PositionLeg(symbol="AVAX", side="short", size=5.0, entry_price=20.0, market_type="perp")
        
        pos_id = "test_ml_exit"
        ml_pos = MultiLegPosition(
            position_id=pos_id,
            strategy="stat_arb",
            legs=[leg1, leg2],
            entry_time=datetime.now()
        )
        self.execution_engine.multi_leg_positions[pos_id] = ml_pos
        
        # 2. Mock execute_order to simulate different execution times
        def slow_execute(symbol, **kwargs):
            if symbol == "LINK":
                time.sleep(0.1) # Simulate network lag
            return {
                'avg_fill_price': 11.0 if symbol == "LINK" else 19.0,
                'filled_size': 10.0 if symbol == "LINK" else 5.0,
                'status': 'filled',
                'fee': 0.1
            }
            
        self.market_api.execute_order.side_effect = slow_execute
        self.market_api.get_execution_fee.return_value = 0.1

        # 3. Trigger Exit
        signal = {'action': 'exit', 'reason': 'test', 'urgency': 'high'}
        self.execution_engine.execute_multi_leg_exit("LINK", signal, "stat_arb", {})

        # 4. Verifications
        # Verify both orders called
        self.assertEqual(self.market_api.execute_order.call_count, 2)
        
        # Verify Trade Recorded
        self.performance_tracker.record_trade_from_position.assert_called_once()
        
        # Verify Position Deleted from memory
        self.assertNotIn(pos_id, self.execution_engine.multi_leg_positions)
        
        # Verify Position Deleted from DB
        self.performance_tracker.db.delete_position.assert_called_with(pos_id)
        
        # Check PnL calculation logic (Leg 1: (11-10)*10=10, Leg 2: (20-19)*5=5 -> Total 15)
        self.assertEqual(self.execution_engine.total_pnl, 15.0)

if __name__ == '__main__':
    unittest.main()
