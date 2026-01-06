import pytest
from unittest.mock import MagicMock, ANY, patch, PropertyMock
from src.strategies.strategy_manager import StrategyManager

class TestConflictResolution:
    
    @pytest.fixture
    def strategy_manager(self):
        # Setup minimal mocks
        mock_config = {
            'strategies': {
                'enabled': ['TestStrategy'],
                'ohlcv_limit': 100
            },
            'risk_management': {
                'strategy_exploration': {'reserve_capital_pct': 0.1},
                'margin_buffer_percentage': 0.05,
                'liquidation_risk_threshold': 0.8  # 80% max margin utilization
            },
            'trading': {
                'max_positions_total': 10,
                'max_positions_per_symbol': 1,
                'max_positions_per_strategy': 5,
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
            'percentage_risk': 1.0,  # Legacy fallback
            'pair_selection': {'mode': 'simple'}
        }
        mock_market_api = MagicMock()
        manager = StrategyManager(mock_config, mock_market_api)
        
        # Mock execution engine and strategies
        manager.execution_engine = MagicMock()
        manager.strategies = {'TestStrategy': MagicMock()}
        manager._get_multi_leg_position_for_symbol = MagicMock(return_value=None)
        
        return manager

    def test_block_weak_conflict(self, strategy_manager):
        """Test blocking a weak opposing signal."""
        symbol = 'BTC/USD'
        
        # Existing Long Position (Strength 0.5)
        pos = MagicMock(side='long')
        pos.entry_signal_strength = 0.5
        
        # Mock positions property
        with patch.object(StrategyManager, 'positions', new_callable=PropertyMock) as mock_positions:
            mock_positions.return_value = {symbol: pos}
            
            # New Sell Signal (Strength 0.52 - barely higher)
            signal = {'signal': 'sell'}
            
            # Setup strategy to return 0.52
            strategy_manager.strategies['TestStrategy'].calculate_signal_strength.return_value = 0.52
            
            # Should execute? No, difference too small (needs > 0.55)
            should_exec = strategy_manager._should_execute_signal(symbol, signal, 50000, {}, 'TestStrategy')
            
            assert should_exec is False
            strategy_manager.execution_engine.close_position.assert_not_called()

    def test_flip_strong_conflict(self, strategy_manager):
        """Test flipping on strong opposing signal."""
        symbol = 'BTC/USD'
        
        # Existing Long (Strength 0.4)
        pos = MagicMock(side='long')
        pos.entry_signal_strength = 0.4
        
        with patch.object(StrategyManager, 'positions', new_callable=PropertyMock) as mock_positions:
            mock_positions.return_value = {symbol: pos}
            
            # New Sell Signal (Strength 0.8) -> 0.8 > 0.4*1.1 (0.44) -> FLIP
            signal = {'signal': 'sell'}
            strategy_manager.strategies['TestStrategy'].calculate_signal_strength.return_value = 0.8
            
            should_exec = strategy_manager._should_execute_signal(symbol, signal, 50000, {}, 'TestStrategy')
            
            assert should_exec is True
            strategy_manager.execution_engine.close_position.assert_called_with(symbol, 'conflict_flip', timestamp=ANY)

    def test_upgrade_same_direction(self, strategy_manager):
        """Test upgrading on strong same-side signal."""
        symbol = 'BTC/USD'
        
        # Existing Long (Strength 0.3)
        pos = MagicMock(side='long')
        pos.entry_signal_strength = 0.3
        
        with patch.object(StrategyManager, 'positions', new_callable=PropertyMock) as mock_positions:
            mock_positions.return_value = {symbol: pos}
            
            # New Buy Signal (Strength 0.8) -> 0.8 > 0.3 + 0.2 (0.5) -> UPGRADE
            signal = {'signal': 'buy'}
            strategy_manager.strategies['TestStrategy'].calculate_signal_strength.return_value = 0.8
            
            should_exec = strategy_manager._should_execute_signal(symbol, signal, 50000, {}, 'TestStrategy')
            
            assert should_exec is True
            strategy_manager.execution_engine.close_position.assert_called_with(symbol, 'conflict_upgrade', timestamp=ANY)

    def test_nuclear_displacement(self, strategy_manager):
        """Test displacing a multi-leg arb with a strong single-leg signal."""
        symbol = 'BTC/USD'
        
        # Use property mock to return empty dict for single-leg positions
        with patch.object(StrategyManager, 'positions', new_callable=PropertyMock) as mock_positions:
            mock_positions.return_value = {} 
            
            # Mock Multi-Leg Position
            arb_pos = MagicMock()
            arb_pos.position_id = 'arb_123'
            arb_pos.strategy = 'StatArb'
            arb_pos.entry_signal_strength = 0.5
            strategy_manager._get_multi_leg_position_for_symbol = MagicMock(return_value=arb_pos)
            
            # Reuse handle_multi_leg mock - ENSURE IT IS A MOCK
            strategy_manager._handle_multi_leg_signal = MagicMock()
            
            # New Single Leg Signal (Strength 0.9) -> 0.9 > 0.5 * 1.5 (0.75) -> NUCLEAR
            signal = {'signal': 'buy'}
            strategy_manager.strategies['TestStrategy'].calculate_signal_strength.return_value = 0.9
            
            should_exec = strategy_manager._should_execute_signal(symbol, signal, 50000, {}, 'TestStrategy')
            
            assert should_exec is True
            # Verify multi-leg close was triggered
            strategy_manager._handle_multi_leg_signal.assert_called()
            call_args = strategy_manager._handle_multi_leg_signal.call_args
            assert call_args[0][1]['action'] == 'exit' # Signal dict
            assert call_args[0][1]['type'] == 'nuclear_displacement'

