import pytest
from unittest.mock import MagicMock, patch, ANY
from src.strategies.execution_engine import ExecutionEngine
from datetime import datetime

class TestExecutionEngine:
    
    @pytest.fixture
    def execution_engine(self, mock_config, mock_market_api):
        """Creates an ExecutionEngine instance with mocked dependencies."""
        mock_leverage_manager = MagicMock()
        mock_portfolio_manager = MagicMock()
        mock_performance_tracker = MagicMock()
        mock_pair_selector = MagicMock()
        
        return ExecutionEngine(
            mock_config, 
            mock_market_api, 
            mock_leverage_manager,
            mock_portfolio_manager,
            mock_performance_tracker,
            mock_pair_selector
        )

    def test_execute_trade_submission(self, execution_engine, mock_market_api):
        """Test that execute_trade submits order to API."""
        symbol = "BTC"
        signal = {
            'signal': 'buy',
            'side': 'buy',
            'size': 0.1,
            'entry_price': 50000,
            'stop_loss': 49000,
            'take_profit': 52000,
            'leverage': 1,
            'strategy': 'test_strat',
            'margin_required': 5000,
            'signal_strength': 1.0,
            'market_volatility': 0.02,
            'z_score': 1.5,
            'sigma': 0.01,
            'mu': 0.0
        }
        current_price = 50000
        strategy_name = 'test_strat'
        
        # Mock API response for order submission
        mock_market_api.execute_order.return_value = {
            'order_id': 123, 'status': 'filled', 
            'filled_size': 0.1, 'avg_fill_price': 50000
        }
        
        # Mock strategy
        mock_strategy = MagicMock()
        mock_strategy.calculate_stop_loss.return_value = 49000
        mock_strategy.calculate_take_profit.return_value = 52000
        mock_strategy.get_trailing_stop_config.return_value = {'enabled': False}
        strategies_map = {'test_strat': mock_strategy}
        
        # Mock Leverage Manager methods that return values used in logic
        execution_engine.leverage_manager.calculate_stop_loss_with_leverage.return_value = 48000
        execution_engine.leverage_manager.calculate_take_profit_with_leverage.return_value = 55000
        execution_engine.leverage_manager.calculate_take_profit_with_capital_at_risk.return_value = 55000
        
        # Mock Portfolio Manager equity to avoid MagicMock math errors
        execution_engine.portfolio_manager.total_equity = 10000.0
        
        # Test execution
        execution_engine.execute_trade(symbol, signal, current_price, strategy_name, {}, strategies_map)
        
        # Verify order was executed (execute_trade returns None/void)
        mock_market_api.execute_order.assert_called_once()
        # Verify position was recorded
        assert symbol in execution_engine.positions
        # Verify trade was recorded with order_id
        assert len(execution_engine.trades) > 0
        assert execution_engine.trades[-1].order_id == 123
        
    def test_close_position(self, execution_engine, mock_market_api):
        """Test position closure."""
        # Setup position
        symbol = "ETH"
        from src.models.trade import Position
        pos = Position(
            symbol=symbol, side='long', size=1.0, entry_price=3000, strategy='test',
            entry_time=datetime.now()
        )
        execution_engine.positions[symbol] = pos
        
        # Mock API close
        mock_market_api.execute_order.return_value = {
            'status': 'filled', 'filled_size': 1.0, 'avg_fill_price': 3100
        }
        
        result = execution_engine.close_position(symbol, reason="test")
        
        assert result is True
        
        assert symbol not in execution_engine.positions
        mock_market_api.execute_order.assert_called()
