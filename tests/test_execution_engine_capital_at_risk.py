
import pytest
from unittest.mock import MagicMock, patch
from src.strategies.execution_engine import ExecutionEngine
from src.models.trade import Position

class TestExecutionEngineRiskCalc:
    @pytest.fixture
    def mock_components(self):
        config = {'trading': {'max_account_loss_per_trade': 1.0}}
        market_api = MagicMock()
        leverage_manager = MagicMock()
        portfolio_manager = MagicMock()
        performance_tracker = MagicMock()
        pair_selector = MagicMock()
        
        # Setup DB mock via performance_tracker
        db = MagicMock()
        performance_tracker.db = db
        
        execution_engine = ExecutionEngine(
            config=config,
            market_api=market_api,
            leverage_manager=leverage_manager,
            portfolio_manager=portfolio_manager,
            performance_tracker=performance_tracker,
            pair_selector=pair_selector
        )
        return execution_engine, market_api, leverage_manager, performance_tracker

    def test_capital_at_risk_uses_filled_size(self, mock_components):
        """Verify that DB records capital_at_risk based on actual filled size, not intended size."""
        engine, api, lev_man, perf_tracker = mock_components
        
        # 1. Setup Sizing (Intention: Buy 100 units)
        lev_man.calculate_leveraged_position_size.return_value = (100.0, 100.0, 10) 
        
        # 2. Setup API Execution (Reality: Partial fill, only 10 units filled)
        api.execute_order.return_value = {
            'filled_size': 10.0,
            'avg_fill_price': 10.0,
            'order_id': 'oid-partial',
            'status': 'partial'
        }
        
        # Mock other dependencies
        engine.portfolio_manager.calculate_available_capital_for_trading.return_value = 5000
        engine.portfolio_manager.total_equity = 5000
        lev_man.calculate_stop_loss_with_leverage.return_value = 9.0  # Dummy SL
        lev_man.calculate_take_profit_with_leverage.return_value = 11.0
        lev_man.calculate_take_profit_with_capital_at_risk.return_value = 11.0
        
        # Mock strategy
        strategy = MagicMock()
        strategy.name = "TestStrat"
        strategy.calculate_stop_loss.return_value = None
        strategy.calculate_take_profit.return_value = 11.0
        strategy.get_trailing_stop_config.return_value = {}
        
        # ACT
        strategies_map = {"TestStrat": strategy}
        engine.execute_trade(
            symbol="BTC",
            signal={'signal': 'buy', 'signal_strength': 1.0, 'market_volatility': 0.05},
            current_price=10.0,
            strategy_name="TestStrat",
            ohlcv=None,
            strategies_map=strategies_map
        )
        
        # ASSERT
        assert perf_tracker.db.save_position.called, "DB should be called for valid fill"
        
        # Capture the Position object
        saved_pos = perf_tracker.db.save_position.call_args[0][0]
        
        # Verify Size matches Fill
        assert saved_pos['size'] == 10.0
        
        # CRITICAL: Verify Capital at Risk matches Fill, not Intention
        # Note: capital_at_risk is stored in metadata dict
        saved_risk = saved_pos['metadata']['capital_at_risk']
        assert saved_risk == 10.0, f"Capital at Risk {saved_risk} != Expected $10.0"

    def test_no_db_write_on_api_failure(self, mock_components):
        """Verify that DB is NOT written to if API executes failed (timeout/error)."""
        engine, api, lev_man, perf_tracker = mock_components
        
        # Sizing success
        lev_man.calculate_leveraged_position_size.return_value = (100.0, 100.0, 10)
        
        # API Failure (returns None)
        api.execute_order.return_value = None
        
        # Mock strategy
        strategy = MagicMock()
        strategy.calculate_stop_loss.return_value = None
        strategy.get_trailing_stop_config.return_value = {}

        # ACT
        strategies_map = {"TestStrat": strategy}
        engine.execute_trade(
            symbol="BTC",
            signal={'signal': 'buy', 'signal_strength': 1.0, 'market_volatility': 0.05},
            current_price=10.0,
            strategy_name="TestStrat",
            ohlcv=None,
            strategies_map=strategies_map
        )
        
        # ASSERT
        assert not perf_tracker.db.save_position.called, "DB should NOT be called on API failure"
