import unittest
from unittest.mock import MagicMock, ANY, patch
from src.strategies.strategy_manager import StrategyManager

class MockPosition:
    def __init__(self, side, strength, **kwargs):
        self.side = side
        self.entry_signal_strength = float(strength)
        for k, v in kwargs.items():
            setattr(self, k, v)

class TestConflictResolution(unittest.TestCase):
    
    def setUp(self):
        self.mock_config = {
            'strategies': {'enabled': ['TestStrategy'], 'ohlcv_limit': 100},
            'risk_management': {
                'strategy_exploration': {'reserve_capital_pct': 0.1},
                'margin_buffer_percentage': 0.05,
                'liquidation_risk_threshold': 0.8
            },
            'trading': {
                'max_positions_total': 10,
                'max_positions_per_symbol': 1,
                'max_positions_per_strategy': 5,
                'position_monitoring_interval': 60,
                'position_sync_interval': 60,
                'use_portfolio_based_sizing': False,
                'max_position_size_percentage': 1.0,
                'max_positions_percentage': 50.0,
                'dynamic_pair_selection': False,
                'min_open_interest': 1000000,
                'scan_interval_minutes': 60,
                'excluded_assets': [],
                'included_assets': [],
                'base_currency': 'USD',
                'order_timeout_minutes': 60,
                'enable_stale_order_cleanup': True,
                'position_sync_interval': 60,
                'enable_position_validation': True
            },
            'percentage_risk': 1.0,
            'pair_selection': {'mode': 'simple'}
        }
        self.mock_market_api = MagicMock()
        self.mock_market_api.get_max_leverage.return_value = 50.0
        self.mock_market_api.get_market_data.return_value = {'current_price': 50000.0}

        self.patcher = patch('src.strategies.strategy_manager.ExecutionEngine')
        self.MockExecutionEngine = self.patcher.start()
        
        # We need to make sure the instance returned is usable
        self.mock_engine_instance = self.MockExecutionEngine.return_value
        self.mock_engine_instance.positions = {}
        self.mock_engine_instance.get_multi_leg_position_by_leg_symbol.return_value = None
        
        self.manager = StrategyManager(self.mock_config, self.mock_market_api)
        
        self.manager.leverage_manager = MagicMock()
        self.manager.leverage_manager.calculate_leveraged_position_size.return_value = (1.0, 100.0, 10.0)
        self.manager.leverage_manager.can_open_position.return_value = True

        # CRITICAL FIX: Mock the strategy's calculate_signal_strength to return the float from the signal
        # Otherwise it returns a MagicMock, causing TypeError in comparisons (Mock > Float)
        strategy_mock = MagicMock()
        def get_strength(ohlcv, symbol, signal_context):
            return float(signal_context.get('signal_strength', 0.0))
        strategy_mock.calculate_signal_strength.side_effect = get_strength
        
        self.manager.strategies = {'TestStrategy': strategy_mock}

    def tearDown(self):
        self.patcher.stop()

    def test_block_weak_conflict_single_leg(self):
        symbol = 'BTC/USD'
        pos = MockPosition('long', 0.5)
        self.mock_engine_instance.positions = {symbol: pos}
        
        # 0.6 < 0.5*1.3 (0.65) -> BLOCK
        signal = {'signal': 'sell', 'signal_strength': 0.6}
            
        should_exec = self.manager._should_execute_signal(symbol, signal, 50000, {}, 'TestStrategy')
        
        if should_exec == 'block':
            pass 
        else:
            self.assertFalse(should_exec, f"Should be blocked, got {should_exec}")

    def test_nuclear_displacement_triggered(self):
        """Test displacing a multi-leg arb with a strong single-leg signal."""
        symbol = 'BTC/USD'
        
        self.mock_engine_instance.positions = {}
            
        # Mock Multi-Leg Position via MockPosition
        # strength=0.4 (passed to __init__ -> entry_signal_strength)
        arb_pos = MockPosition('flat', 0.4) 
        arb_pos.position_id = 'arb_123'
        arb_pos.strategy = 'StatArb'
        arb_pos.primary_symbol = 'BTC-ETH-ARB'
        # metadata redundant but kept for completeness
        arb_pos.metadata = {'signal_strength': 0.4}
        
        # Explicitly set the return value on the instance method
        self.mock_engine_instance.get_multi_leg_position_by_leg_symbol.return_value = arb_pos
            
        # 0.9 > 0.4 * 2.0 (0.8) -> TRIGGER
        signal = {'signal': 'buy', 'signal_strength': 0.9}
        
        should_exec = self.manager._should_execute_signal(symbol, signal, 50000, {}, 'TestStrategy')
        
        self.assertTrue(should_exec)
        self.mock_engine_instance.execute_multi_leg_exit.assert_called_once()
        self.assertEqual(self.mock_engine_instance.execute_multi_leg_exit.call_args[1]['symbol'], 'BTC-ETH-ARB')

    def test_nuclear_displacement_blocked(self):
        symbol = 'BTC/USD'
        self.mock_engine_instance.positions = {}
        
        arb_pos = MockPosition('flat', 0.0)
        arb_pos.position_id = 'arb_123'
        arb_pos.strategy = 'StatArb'
        arb_pos.primary_symbol = 'BTC-ETH-ARB'
        arb_pos.metadata = {'signal_strength': 0.5}
        
        self.mock_engine_instance.get_multi_leg_position_by_leg_symbol.return_value = arb_pos
        
        # 0.9 < 0.5*2.0 (1.0) -> BLOCK
        signal = {'signal': 'buy', 'signal_strength': 0.9}
        
        should_exec = self.manager._should_execute_signal(symbol, signal, 50000, {}, 'TestStrategy')
        
        self.assertFalse(should_exec)
        self.mock_engine_instance.execute_multi_leg_exit.assert_not_called()
