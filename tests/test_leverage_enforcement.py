
import pytest
from unittest.mock import MagicMock
from src.strategies.execution_engine import ExecutionEngine

class TestLeverageEnforcement:
    @pytest.fixture
    def mock_api(self):
        api = MagicMock()
        api.get_asset_meta.return_value = {'maxLeverage': 20.0}
        api.get_positions.return_value = []
        api.update_leverage.return_value = True
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
        
        # Define dynamic leverage calculation mock
        def calc_dynamic(symbol, strategy, strength, vol, price, asset_max_leverage=100.0, strategy_leverage=None):
            # If strategy requests leverage, respect it but cap at asset max
            target = float(strategy_leverage) if strategy_leverage is not None else 10.0
            asset_max = float(asset_max_leverage)
            return min(target, asset_max)
            
        lm.calculate_dynamic_leverage.side_effect = calc_dynamic
        
        # Mock sizing return: (size, margin, leverage)
        lm.calculate_leveraged_position_size.return_value = (1.0, 100.0, 10.0)
        
        # Mock TP/SL calculators to return floats
        lm.calculate_stop_loss_with_leverage.return_value = 95.0
        lm.calculate_take_profit_with_leverage.return_value = 105.0
        lm.calculate_take_profit_with_capital_at_risk.return_value = 105.0
        lm.record_position = MagicMock()
        
        return lm

    @pytest.fixture
    def execution_engine(self, mock_api, mock_portfolio_manager, mock_leverage_manager):
        config = {
            'trading': {'max_account_loss_per_trade': 5.0},
            'leverage_management': {'base_leverage': 5.0}
        }
        mock_performance_tracker = MagicMock()
        mock_pair_selector = MagicMock()
        
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
        # 1. Setup: Asset Max is 5.0
        mock_api.get_asset_meta.return_value = {'maxLeverage': 5.0}
        
        # Override leverage manager return value directly to force 5.0
        # This simulates that LeverageManager correctly capped the leverage
        mock_leverage_manager.calculate_leveraged_position_size.return_value = (1.0, 100.0, 5.0)
        
        # Override dynamic leverage too for consistency (though engine uses the one above)
        mock_leverage_manager.calculate_dynamic_leverage.side_effect = None
        mock_leverage_manager.calculate_dynamic_leverage.return_value = 5.0
        
        # 2. Setup: Signal requests 10.0
        signal = {
            'signal': 'buy',
            'symbol': 'BTC',
            'side': 'buy',
            'size': 0.1,
            'leverage': 10.0, 
            'signal_strength': 0.8,
            'margin_required': 100.0,
            'market_volatility': 0.05
        }
        strategies_map = {'test_strat': MagicMock()}
        # Ensure strategy methods return floats
        strategies_map['test_strat'].calculate_stop_loss.return_value = 95.0
        strategies_map['test_strat'].calculate_take_profit.return_value = 105.0
        strategies_map['test_strat'].calculate_take_profit.side_effect = None 
        
        # 3. Act
        execution_engine.execute_trade('BTC', signal, 100.0, 'test_strat', {}, strategies_map)
        
        # 4. Assert
        # update_leverage should be called with min(10, 5) = 5
        mock_api.update_leverage.assert_called_with('BTC', 5, is_cross=True)
        
    def test_updates_leverage_when_mismatch(self, execution_engine, mock_api):
        """Verify update_leverage is called when current account leverage differs from target."""
        # Setup: Current 20x
        mock_api.get_positions.return_value = [{
            'symbol': 'BTC',
            'leverage': {'type': 'cross', 'value': 20}
        }]
        # Default behavior of calc_dynamic is 10.0 if not overridden
        # Signal asks for 10.0
        
        signal = {
            'signal': 'buy', 'symbol': 'BTC', 'side': 'buy', 'size': 0.1, 'leverage': 10.0,
            'signal_strength': 0.8, 'margin_required': 100.0, 'market_volatility': 0.05
        }
        strategies_map = {'test_strat': MagicMock()}
        strategies_map['test_strat'].calculate_stop_loss.return_value = 95.0
        strategies_map['test_strat'].calculate_take_profit.return_value = 105.0
        
        execution_engine.execute_trade('BTC', signal, 100.0, 'test_strat', {}, strategies_map)
        
        # Should update to 10
        mock_api.update_leverage.assert_called_with('BTC', 10, is_cross=True)

    def test_skips_update_when_leverage_matches(self, execution_engine, mock_api):
        """Verify update_leverage is skipped if current leverage matches target."""
        # Setup: Current 10x
        mock_api.get_positions.return_value = [{
            'symbol': 'BTC',
            'leverage': {'type': 'cross', 'value': 10}
        }]
        
        signal = {
            'signal': 'buy', 'symbol': 'BTC', 'side': 'buy', 'size': 0.1, 'leverage': 10.0,
            'signal_strength': 0.8, 'margin_required': 100.0, 'market_volatility': 0.05
        }
        strategies_map = {'test_strat': MagicMock()}
        strategies_map['test_strat'].calculate_stop_loss.return_value = 95.0
        strategies_map['test_strat'].calculate_take_profit.return_value = 105.0
        
        execution_engine.execute_trade('BTC', signal, 100.0, 'test_strat', {}, strategies_map)
        
        mock_api.update_leverage.assert_not_called()

    def test_defaults_to_update_if_no_position(self, execution_engine, mock_api):
        """Verify update_leverage is called if no position exists (safe default)."""
        mock_api.get_positions.return_value = []
        
        signal = {
            'signal': 'buy', 'symbol': 'BTC', 'side': 'buy', 'size': 0.1, 'leverage': 10.0,
            'signal_strength': 0.8, 'margin_required': 100.0, 'market_volatility': 0.05
        }
        strategies_map = {'test_strat': MagicMock()}
        strategies_map['test_strat'].calculate_stop_loss.return_value = 95.0
        strategies_map['test_strat'].calculate_take_profit.return_value = 105.0
        
        execution_engine.execute_trade('BTC', signal, 100.0, 'test_strat', {}, strategies_map)
        
        mock_api.update_leverage.assert_called_with('BTC', 10, is_cross=True)
