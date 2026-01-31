
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime
from src.strategies.strategy_manager import StrategyManager
from src.models.trade import MultiLegPosition, PositionLeg

class TestMultiLegDrift(unittest.TestCase):
    def setUp(self):
        self.config = {
            'strategies': {
                'enabled': [], 
                'instances': [],
                'ohlcv_limit': 100
            },
            'trading': {
                'position_sync_interval': 60,
                'enable_position_validation': True,
                'base_currency': 'USDC',
                'max_positions_percentage': 50,
                'order_timeout_minutes': 5,
                'enable_stale_order_cleanup': True,
                'strategy_cooldowns': {}
            },
            'risk_management': {}
        }
        self.market_api = MagicMock()
        self.performance_tracker = MagicMock()
        self.logger = MagicMock()
        
        # Patch dependencies
        with patch('src.strategies.strategy_manager.ExecutionEngine'), \
             patch('src.strategies.strategy_manager.PortfolioManager'), \
             patch('src.strategies.strategy_manager.LeverageManager'), \
             patch('src.strategies.strategy_manager.CorrelationManager'), \
             patch('src.strategies.strategy_manager.StrategySelector'), \
             patch('src.strategies.strategy_manager.DynamicPairSelector'):
             
             self.strategy_manager = StrategyManager(self.config, self.market_api, self.performance_tracker)
             
        self.strategy_manager.logger = self.logger
        self.strategy_manager.execution_engine = MagicMock()
        self.strategy_manager.execution_engine.multi_leg_positions = {}
        
        # Setup DB mock
        self.strategy_manager.performance_tracker.db = MagicMock()
        self.strategy_manager.performance_tracker.db.get_all_live_position_symbols.return_value = [] # No single leg positions

    def test_multileg_drift_correction(self):
        """Test that a multi-leg position with flipped side on exchange is corrected locally."""
        # 1. Setup Local Position (LONG)
        pos_id = "pos_stat_arb_LINK_AVAX"
        leg1 = PositionLeg(symbol="LINK", side="long", size=10.0, entry_price=10.0, market_type="perp")
        leg2 = PositionLeg(symbol="AVAX", side="short", size=5.0, entry_price=20.0, market_type="perp")
        
        ml_pos = MultiLegPosition(
            position_id=pos_id,
            strategy="stat_arb",
            legs=[leg1, leg2],
            entry_time=datetime.now()
        )
        
        self.strategy_manager.execution_engine.multi_leg_positions = {pos_id: ml_pos}
        
        # 2. Setup Exchange Map (LINK flipped to SHORT)
        # Exchange says LINK is Short (size=-10) and AVAX is Long (size=5) -> Both flipped? 
        # Or just one? Let's flip both to simulate reversal.
        exchange_map = {
            "LINK": {"size": -10.0, "szi": -10.0, "entry_price": 10.0}, # Short
            "AVAX": {"size": 5.0, "szi": 5.0, "entry_price": 20.0}      # Long
        }
        
        # Mock get_positions to return list form of map
        self.strategy_manager.market_api.get_positions.return_value = [
            {"symbol": "LINK", "size": -10.0, "szi": -10.0, "entry_price": 10.0},
            {"symbol": "AVAX", "size": 5.0, "szi": 5.0, "entry_price": 20.0}
        ]
        
        # 3. Run Sync
        # We need to ensure _get_exchange_positions logic creates the map correctly
        # The method calls self.market_api.get_positions() internaly
        
        self.strategy_manager.sync_positions_with_exchange()
        
        # 4. Assert Update
        updated_pos = self.strategy_manager.multi_leg_positions[pos_id]
        
        # Check Leg 1 (LINK)
        link_leg = next(l for l in updated_pos.legs if l.symbol == "LINK")
        self.assertEqual(link_leg.side, "short", "LINK leg should be updated to short")
        
        # Check Leg 2 (AVAX)
        avax_leg = next(l for l in updated_pos.legs if l.symbol == "AVAX")
        self.assertEqual(avax_leg.side, "long", "AVAX leg should be updated to long")
        
        # Verify Persistence called
        self.strategy_manager.execution_engine._persist_multi_leg_position.assert_called_with(updated_pos)
        
        # Verify Log Warning
        self.strategy_manager.logger.warning.assert_any_call(
            unittest.mock.ANY
        )
        # Check strictly if "Correcting Multi-Leg Drift" message was logged
        cleanup_calls = [c[0][0] for c in self.strategy_manager.logger.warning.call_args_list if "Correcting Multi-Leg Drift" in str(c[0][0])]
        self.assertTrue(cleanup_calls, "Should verify drift correction log")

    def test_multileg_size_drift(self):
        """Test that a size mismatch (e.g. partial manual close) is corrected locally."""
        pos_id = "pos_stat_arb_ETH_BTC"
        # Local: 10 ETH
        leg1 = PositionLeg(symbol="ETH", side="long", size=10.0, entry_price=2000.0, market_type="perp")
        
        ml_pos = MultiLegPosition(
            position_id=pos_id,
            strategy="stat_arb",
            legs=[leg1],
            entry_time=datetime.now()
        )
        self.strategy_manager.execution_engine.multi_leg_positions = {pos_id: ml_pos}
        
        # Exchange: 8 ETH (2 ETH closed manually)
        self.strategy_manager.market_api.get_positions.return_value = [
            {"symbol": "ETH", "size": 8.0, "szi": 8.0, "entry_price": 2000.0}
        ]
        
        self.strategy_manager.sync_positions_with_exchange()
        
        updated_pos = self.strategy_manager.execution_engine.multi_leg_positions[pos_id]
        eth_leg = updated_pos.legs[0]
        
        self.assertEqual(eth_leg.size, 8.0, "ETH leg size should update to 8.0")
        self.strategy_manager.execution_engine._persist_multi_leg_position.assert_called()

if __name__ == '__main__':
    unittest.main()
