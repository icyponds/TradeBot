import unittest
from unittest.mock import MagicMock, patch, ANY
import sys
import datetime
from src.strategies.strategy_manager import StrategyManager

import logging

import logging

class TestStrategyManagerReconciliation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        logging.basicConfig(level=logging.INFO)
    @classmethod
    def setUpClass(cls):
        logging.basicConfig(level=logging.INFO)
    def setUp(self):
        self.config = {
            'strategies': {
                'enabled': [],
                'instances': [{'type': 'cross_sectional_momentum', 'name': 'csm_1h', 'timeframe': '1h'}],
                'ohlcv_limit': 100
            },
            'trading': {
                'ohlcv_limit': 100,
                'max_positions_percentage': 50,
                'base_currency': 'USDC',
                'order_timeout_minutes': 5,
                'enable_stale_order_cleanup': True,
                'position_sync_interval': 60,
                'enable_position_validation': True,
                'strategy_cooldowns': {}
            },
            'risk_management': {}
        }
        self.market_api = MagicMock()
        self.performance_tracker = MagicMock()
        
        # Patch dependencies that are imported inside methods
        self.mock_execution_engine = MagicMock()
        self.mock_execution_engine.positions = {}
        self.mock_execution_engine.multi_leg_positions = {}
        
        with patch('src.strategies.strategy_manager.ExecutionEngine', return_value=self.mock_execution_engine), \
             patch('src.strategies.strategy_manager.PortfolioManager'), \
             patch('src.strategies.strategy_manager.LeverageManager'), \
             patch('src.strategies.strategy_manager.CorrelationManager'), \
             patch('src.strategies.strategy_manager.StrategySelector'), \
             patch('src.strategies.strategy_manager.DynamicPairSelector'), \
             patch('src.strategies.strategy_manager.PerformanceTracker', return_value=self.performance_tracker):
             
             self.manager = StrategyManager(self.config, self.market_api, self.performance_tracker)
             
        # Mock strategies
        self.manager.strategies = {'csm_1h': MagicMock()}
        
        # Setup positions on execution engine
        position_mock = MagicMock()
        position_mock.strategy = 'csm_1h'
        self.manager.execution_engine.positions = {'BTC-USD': position_mock}
        self.manager.execution_engine.multi_leg_positions = {}

    @patch('src.config.settings.load_config')
    def test_reconcile_strategies_remove_add(self, mock_load_config):
        # Setup new config: Remove csm_1h, Add stat_arb_15m
        new_config = self.config.copy()
        new_config['strategies'] = {
            'instances': [{'type': 'stat_arb', 'name': 'stat_arb_15m', 'timeframe': '15m'}],
            'ohlcv_limit': 100
        }
        mock_load_config.return_value = new_config
        
        # Inject module into sys.modules check is covered by patch loading it
        # sys.modules['src.config.settings'] = MagicMock()
        
        # Mock strategy factory for added strategy
        mock_strategy_class = MagicMock()
        
        # Mock self._importlib on the manager instance
        self.manager._importlib = MagicMock()
        mock_module = MagicMock()
        mock_module.StatisticalArbitrageStrategy = mock_strategy_class
        self.manager._importlib.import_module.return_value = mock_module
            
        # Execute
        print(f"DEBUG: Before reconcile: {list(self.manager.strategies.keys())}")
        self.manager.reconcile_strategies()
        print(f"DEBUG: After reconcile: {list(self.manager.strategies.keys())}")
        
        # Verify Removal
        self.assertNotIn('csm_1h', self.manager.strategies)
        # Verify close_position was called for the removed strategy's position
        self.manager.execution_engine.close_position.assert_called_with('BTC-USD', 'strategy_removed', timestamp=ANY)
        
        # Verify Addition
        self.assertIn('stat_arb_15m', self.manager.strategies)
        
        # Verify import was called correctly
        # STRATEGY_CLASSES map 'stat_arb' to ('statistical_arbitrage_strategy', 'StatisticalArbitrageStrategy')
        self.manager._importlib.import_module.assert_called_with("src.strategies.statistical_arbitrage_strategy")
        mock_strategy_class.assert_called()

    def test_check_startup_orphans(self):
        # Setup orphan: Strategy list empty, Position exists
        self.manager.strategies = {} 
        position_mock = MagicMock()
        position_mock.strategy = 'old_strat'
        self.manager.execution_engine.positions = {'ETH-USD': position_mock}
        
        # Mock exchange position as empty (Local Only)
        self.manager.market_api.get_position.return_value = {'size': 0}
        
        self.manager._check_startup_orphans()
        
        # "LOCAL ONLY" orphan -> delete from DB, no close order
        self.manager.execution_engine.close_position.assert_not_called()
        # pos_id for single leg might be constructed, need to verify implementation logic
        # Implementation: pos_id = f"pos_{pos_obj.strategy}_{symbol}"
        pos_id = "pos_old_strat_ETH-USD" 
        self.manager.execution_engine.delete_position_from_db.assert_called_with(pos_id)

    def test_check_startup_orphans_multileg(self):
        # Setup orphan multi-leg
        self.manager.strategies = {}
        ml_pos_mock = MagicMock()
        ml_pos_mock.strategy = 'old_ml_strat'
        ml_pos_mock.primary_symbol = 'SOL-USD'
        ml_pos_mock.position_id = 'ml_123'
        
        self.manager.execution_engine.multi_leg_positions = {'ml_123': ml_pos_mock}
        
        # We need to mock _handle_multi_leg_signal since it's called
        self.manager._handle_multi_leg_signal = MagicMock()
        
        self.manager._check_startup_orphans()
        
        # "LOCAL ONLY" orphan should be deleted silently from DB, no signal sent
        self.manager._handle_multi_leg_signal.assert_not_called()
        self.manager.execution_engine.delete_position_from_db.assert_called_with('ml_123')
        
        # Verify removed from local state
        self.assertNotIn('ml_123', self.manager.execution_engine.multi_leg_positions)
