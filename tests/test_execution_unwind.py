
import pytest
from unittest.mock import MagicMock, call
import time
from src.strategies.execution_engine import ExecutionEngine
from src.models.trade import PositionLeg

class TestExecutionUnwind:
    
    @pytest.fixture
    def mock_components(self):
        mock_config = {'execution': {'max_slippage_bps': 200}}
        mock_market_api = MagicMock()
        mock_leverage_manager = MagicMock()
        mock_portfolio_manager = MagicMock()
        mock_performance_tracker = MagicMock()
        mock_pair_selector = MagicMock()
        
        # Mock exchange object for market_close fallback
        mock_market_api.exchange = MagicMock()
        
        return {
            'config': mock_config,
            'api': mock_market_api,
            'lev': mock_leverage_manager,
            'port': mock_portfolio_manager,
            'perf': mock_performance_tracker,
            'pair': mock_pair_selector
        }

    @pytest.fixture
    def engine(self, mock_components):
        return ExecutionEngine(
            mock_components['config'],
            mock_components['api'],
            mock_components['lev'],
            mock_components['port'],
            mock_components['perf'],
            mock_components['pair']
        )

    def test_unwind_success_first_try(self, engine, mock_components):
        """Test successful unwind on first attempt (High Urgency)."""
        legs = [PositionLeg(symbol='BTC', side='long', size=1.0, market_type='perp', entry_price=100.0)]
        
        # Mock successful execution
        mock_components['api'].execute_order.return_value = {'filled_size': 1.0}
        
        engine.unwind_executed_legs(legs)
        
        # Verify call args
        mock_components['api'].execute_order.assert_called_once()
        args, kwargs = mock_components['api'].execute_order.call_args
        assert kwargs['urgency'] == 'high'
        assert kwargs['max_slippage_bps'] is None  # Default for level 1

    def test_unwind_escalation_to_second_level(self, engine, mock_components):
        """Test escalation to 500bps after first failure."""
        legs = [PositionLeg(symbol='ETH', side='short', size=10.0, market_type='perp', entry_price=2000.0)]
        
        # First call fails (raise Exception), second succeeds
        mock_components['api'].execute_order.side_effect = [
            Exception("Simulated Failure 1"),
            {'filled_size': 10.0}
        ]
        
        # Mock sleep to speed up test
        with pytest.MonkeyPatch.context() as m:
            m.setattr(time, 'sleep', lambda x: None)
            engine.unwind_executed_legs(legs)
        
        assert mock_components['api'].execute_order.call_count == 2
        
        # Check first call (High urgency)
        call1 = mock_components['api'].execute_order.call_args_list[0]
        assert call1.kwargs['urgency'] == 'high'
        assert call1.kwargs['max_slippage_bps'] is None
        
        # Check second call (500bps)
        call2 = mock_components['api'].execute_order.call_args_list[1]
        assert call2.kwargs['urgency'] == 'high'
        assert call2.kwargs['max_slippage_bps'] == 500

    def test_unwind_fallback_to_market_close(self, engine, mock_components):
        """Test fallback to direct market_close when all execute_order attempts fail."""
        legs = [PositionLeg(symbol='SOL', side='long', size=100.0, market_type='perp', entry_price=50.0)]
        
        # All 4 normal levels fail
        mock_components['api'].execute_order.side_effect = Exception("Exec Fail")
        
        # market_close succeeds
        mock_components['api'].exchange.market_close.return_value = {'status': 'ok'}
        
        with pytest.MonkeyPatch.context() as m:
            m.setattr(time, 'sleep', lambda x: None)
            engine.unwind_executed_legs(legs)
            
        # Verify execute_order called 4 times (all levels)
        assert mock_components['api'].execute_order.call_count == 4
        
        # Verify fallback called
        mock_components['api'].exchange.market_close.assert_called_once_with(
            'SOL', sz=100.0, px=None, slippage=0.5
        )

    def test_all_unwind_attempts_fail(self, engine, mock_components):
        """Test critical log when even fallback fails."""
        legs = [PositionLeg(symbol='XRP', side='long', size=500.0, market_type='perp', entry_price=1.0)]
        
        mock_components['api'].execute_order.side_effect = Exception("Exec Fail")
        mock_components['api'].exchange.market_close.side_effect = Exception("Fallback Fail")
        
        # Capture logs
        engine.logger = MagicMock()
        
        with pytest.MonkeyPatch.context() as m:
            m.setattr(time, 'sleep', lambda x: None)
            engine.unwind_executed_legs(legs)
            
        # Verify critical log
        engine.logger.critical.assert_any_call("❌ STRANDED POSITION: XRP long 500.0")
