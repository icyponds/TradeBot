"""
Unit tests for multi-leg signal handler with instance strategy names.
Tests the fix for KeyError when strategy_name is an instance name like 'stat_arb_15m'.
"""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime


class TestMultiLegSignalHandler:
    """Tests for _handle_multi_leg_signal handling of instance strategy names."""
    
    def test_exit_signal_with_instance_strategy_name(self):
        """
        Ensure exit signals work even when strategy_name is an instance name 
        (e.g., 'stat_arb_15m') not directly in self.strategies.
        """
        from src.strategies.strategy_manager import StrategyManager
        
        # Create a minimal mock of StrategyManager
        sm = MagicMock(spec=StrategyManager)
        sm.strategies = {'stat_arb': MagicMock()}  # Only base name exists
        sm.execution_engine = MagicMock()
        sm.logger = MagicMock()
        
        # Bind the real method to the mock
        sm._handle_multi_leg_signal = StrategyManager._handle_multi_leg_signal.__get__(sm, StrategyManager)
        
        # Exit signal with instance name (should NOT raise KeyError)
        exit_signal = {'action': 'exit', 'reason': 'manual_dashboard'}
        
        # This should not raise KeyError
        sm._handle_multi_leg_signal(
            symbol='BTC',
            signal=exit_signal,
            current_price=50000.0,
            strategy_name='stat_arb_15m',  # Instance name, not in strategies
            ohlcv={},
            timestamp=datetime.now()
        )
        
        # Verify execution engine was called
        sm.execution_engine.handle_multi_leg_signal.assert_called_once()
    
    def test_close_signal_with_instance_strategy_name(self):
        """Ensure 'close' action also works with instance strategy names."""
        from src.strategies.strategy_manager import StrategyManager
        
        sm = MagicMock(spec=StrategyManager)
        sm.strategies = {'funding_rate_arbitrage': MagicMock()}
        sm.execution_engine = MagicMock()
        sm.logger = MagicMock()
        
        sm._handle_multi_leg_signal = StrategyManager._handle_multi_leg_signal.__get__(sm, StrategyManager)
        
        close_signal = {'action': 'close', 'reason': 'z_score_target'}
        
        # Should not raise KeyError
        sm._handle_multi_leg_signal(
            symbol='ETH',
            signal=close_signal,
            current_price=3000.0,
            strategy_name='funding_rate_arbitrage_1h',  # Instance name
            ohlcv={},
            timestamp=datetime.now()
        )
        
        sm.execution_engine.handle_multi_leg_signal.assert_called_once()
    
    def test_entry_signal_with_base_strategy_name(self):
        """Ensure entry signals still work with base strategy names."""
        from src.strategies.strategy_manager import StrategyManager
        
        mock_strategy = MagicMock()
        mock_strategy.calculate_signal_strength = MagicMock(return_value=0.8)
        
        sm = MagicMock(spec=StrategyManager)
        sm.strategies = {'stat_arb': mock_strategy}
        sm.execution_engine = MagicMock()
        sm.logger = MagicMock()
        
        sm._handle_multi_leg_signal = StrategyManager._handle_multi_leg_signal.__get__(sm, StrategyManager)
        
        entry_signal = {'action': 'enter', 'legs': []}
        
        sm._handle_multi_leg_signal(
            symbol='BTC',
            signal=entry_signal,
            current_price=50000.0,
            strategy_name='stat_arb',  # Base name exists
            ohlcv={},
            timestamp=datetime.now()
        )
        
        # Should use actual strategy's calculate_signal_strength
        sm.execution_engine.handle_multi_leg_signal.assert_called_once()
        call_args = sm.execution_engine.handle_multi_leg_signal.call_args
        # The 6th positional arg (index 5) is the signal_strength_fn
        assert call_args[0][5] == mock_strategy.calculate_signal_strength
    
    def test_entry_signal_with_instance_name_falls_back_to_base(self):
        """Ensure entry with instance name falls back to base strategy."""
        from src.strategies.strategy_manager import StrategyManager
        
        mock_strategy = MagicMock()
        mock_strategy.calculate_signal_strength = MagicMock(return_value=0.7)
        
        sm = MagicMock(spec=StrategyManager)
        sm.strategies = {'stat_arb': mock_strategy}  # Only base exists
        sm.execution_engine = MagicMock()
        sm.logger = MagicMock()
        
        sm._handle_multi_leg_signal = StrategyManager._handle_multi_leg_signal.__get__(sm, StrategyManager)
        
        entry_signal = {'action': 'open', 'legs': []}
        
        sm._handle_multi_leg_signal(
            symbol='ETH',
            signal=entry_signal,
            current_price=3000.0,
            strategy_name='stat_arb_15m',  # Instance name, should fall back to 'stat_arb'
            ohlcv={},
            timestamp=datetime.now()
        )
        
        # Should have called with base strategy's function
        sm.execution_engine.handle_multi_leg_signal.assert_called_once()
        call_args = sm.execution_engine.handle_multi_leg_signal.call_args
        assert call_args[0][5] == mock_strategy.calculate_signal_strength
    
    def test_entry_signal_with_unknown_strategy_logs_error(self):
        """Ensure entry with completely unknown strategy logs error and returns."""
        from src.strategies.strategy_manager import StrategyManager
        
        sm = MagicMock(spec=StrategyManager)
        sm.strategies = {'some_other_strategy': MagicMock()}  # Different strategy
        sm.execution_engine = MagicMock()
        sm.logger = MagicMock()
        
        sm._handle_multi_leg_signal = StrategyManager._handle_multi_leg_signal.__get__(sm, StrategyManager)
        
        entry_signal = {'action': 'enter', 'legs': []}
        
        sm._handle_multi_leg_signal(
            symbol='BTC',
            signal=entry_signal,
            current_price=50000.0,
            strategy_name='completely_unknown_strat',  # Not in strategies
            ohlcv={},
            timestamp=datetime.now()
        )
        
        # Should NOT have called execution engine (early return)
        sm.execution_engine.handle_multi_leg_signal.assert_not_called()
        # Should have logged error
        sm.logger.error.assert_called_once()
