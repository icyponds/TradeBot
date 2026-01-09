import pytest
from unittest.mock import MagicMock
from src.strategies.execution_engine import ExecutionEngine

class TestLeverageEnforcement:
    @pytest.fixture
    def mock_api(self):
        api = MagicMock()
        # Mock asset meta
        api.get_asset_meta.return_value = {'maxLeverage': 20.0}
        # Mock positions (default empty)
        api.get_positions.return_value = []
        # Mock update leverage
        api.update_leverage.return_value = True
        # Mock order execution
        api.execute_order.return_value = {
            'status': 'filled',
            'filled_size': 1.0,
            'avg_fill_price': 100.0,
            'order_id': '123'
        }
        return api

    @pytest.fixture
    def mock_portfolio_manager(self):
        pm = MagicMock()
        pm.calculate_available_capital_for_trading.return_value = 1000.0
        pm.total_equity = 1000.0
        return pm

    @pytest.fixture
    def mock_leverage_manager(self):
        lm = MagicMock()
        # Default behavior: return requested leverage or cap
        def calc_dynamic(symbol, strategy, strength, vol, price, asset_max_leverage=100.0, strategy_leverage=None):
            # Simple logic for testing: requested (strategy_leverage or 10.0), cap at asset_max
            target = strategy_leverage if strategy_leverage is not None else 10.0
            return min(target, asset_max_leverage)
        
        lm.calculate_dynamic_leverage.side_effect = calc_dynamic
        
        # Mock sizing
        lm.calculate_leveraged_position_size.return_value = (1.0, 100.0, 10.0) # size, margin, leverage
        
        # Mock TP/SL
        lm.calculate_stop_loss_with_leverage.return_value = 95.0
        lm.calculate_take_profit_with_leverage.return_value = 105.0
        lm.calculate_take_profit_with_capital_at_risk.return_value = 105.0
        
        return lm

    @pytest.fixture
    def execution_engine(self, mock_api, mock_portfolio_manager, mock_leverage_manager):
        config = {
            'trading': {'max_account_loss_per_trade': 5.0},
            'leverage_management': {'base_leverage': 5.0}
        }
        mock_performance_tracker = MagicMock()
        mock_pair_selector = MagicMock()
        
        # Correct order: config, api, leverage, portfolio, tracker, pair_selector
        engine = ExecutionEngine(
            config, 
            mock_api, 
            mock_leverage_manager, 
            mock_portfolio_manager, 
            mock_performance_tracker, 
            mock_pair_selector
        )
        return engine

    def test_enforces_asset_max_leverage(self, execution_engine, mock_api, mock_leverage_manager):
        """Verify that leverage is capped by the asset's max leverage."""
        # Setup: Asset has low max leverage (e.g. 5x)
        mock_api.get_asset_meta.return_value = {'maxLeverage': 5.0}
        
        # Setup: Strategy signal
        signal = {
            'signal': 'buy',
            'symbol': 'BTC',
            'side': 'buy',
            'size': 0.1,
            'leverage': 10.0, # Signal requested 10x
            'signal_strength': 0.8,
            'margin_required': 100.0,
            'market_volatility': 0.05
        }
        strategies_map = {'test_strat': MagicMock()}
        
        # Act
        execution_engine.execute_trade('BTC', signal, 100.0, 'test_strat', {}, strategies_map)
        
        # Assert
        # Check that update_leverage was called with 5 (the asset max) not 10
        mock_api.update_leverage.assert_called_with('BTC', 5, is_cross=True)
        
    def test_updates_leverage_when_mismatch(self, execution_engine, mock_api):
        """Verify update_leverage is called when current account leverage differs from target."""
        # Setup: Target 10x (via calc_dynamic default in mock), Current 20x
        mock_api.get_positions.return_value = [{
            'symbol': 'BTC',
            'leverage': {'type': 'cross', 'value': 20}
        }]
        
        signal = {
            'signal': 'buy',
            'symbol': 'BTC', 'side': 'buy', 'size': 0.1, 'leverage': 10.0, 
            'signal_strength': 0.8, 'margin_required': 100.0, 'market_volatility': 0.05
        }
        strategies_map = {'test_strat': MagicMock()}
        
        execution_engine.execute_trade('BTC', signal, 100.0, 'test_strat', {}, strategies_map)
        
        # Assert
        mock_api.update_leverage.assert_called_with('BTC', 10, is_cross=True)

    def test_skips_update_when_leverage_matches(self, execution_engine, mock_api):
        """Verify update_leverage is skipped if current leverage matches target."""
        # Setup: Target 10x, Current 10x
        mock_api.get_positions.return_value = [{
            'symbol': 'BTC',
            'leverage': {'type': 'cross', 'value': 10}
        }]
        
        signal = {
            'signal': 'buy',
            'symbol': 'BTC', 'side': 'buy', 'size': 0.1, 'leverage': 10.0, 
            'signal_strength': 0.8, 'margin_required': 100.0, 'market_volatility': 0.05
        }
        strategies_map = {'test_strat': MagicMock()}
        
        execution_engine.execute_trade('BTC', signal, 100.0, 'test_strat', {}, strategies_map)
        
        # Assert
        mock_api.update_leverage.assert_not_called()

    def test_defaults_to_update_if_no_position(self, execution_engine, mock_api):
        """Verify update_leverage is called if no position exists (safe default)."""
        # Setup: No open positions
        mock_api.get_positions.return_value = []
        
        signal = {
            'signal': 'buy',
            'symbol': 'BTC', 'side': 'buy', 'size': 0.1, 'leverage': 10.0, 
            'signal_strength': 0.8, 'margin_required': 100.0, 'market_volatility': 0.05
        }
        strategies_map = {'test_strat': MagicMock()}
        
        execution_engine.execute_trade('BTC', signal, 100.0, 'test_strat', {}, strategies_map)
        
        # Assert
        mock_api.update_leverage.assert_called_with('BTC', 10, is_cross=True)
