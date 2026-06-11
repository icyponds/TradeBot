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
        execution_engine.leverage_manager.calculate_leveraged_position_size.return_value = (0.1, 5000.0, 1.0)
        
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
        # Verify position was persisted to DB
        execution_engine.performance_tracker.db.save_position.assert_called()
        
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
        
        # close_position returns (bool, str) tuple
        assert result[0] is True
        
        assert symbol not in execution_engine.positions
        mock_market_api.execute_order.assert_called()

    def test_strategy_sl_prioritization(self, execution_engine, mock_market_api):
        """Test that strategy SL is prioritized over leverage fallback if safe."""
        symbol = "SOL"
        current_price = 100.0
        # Position Size 1.0, Leverage 1.0
        # Account Equity 10000. Max Loss 3% = 300.
        # Account SL = 100 - (300/1) = -200 (Safe)
        
        signal = {
            'signal': 'buy', 'side': 'buy', 'size': 1.0, 'leverage': 1.0, 
            'margin_required': 100.0, 'signal_strength': 1.0, 'market_volatility': 0.1
        }
        
        # Mocks
        mock_market_api.execute_order.return_value = {
            'order_id': 999, 'status': 'filled', 'filled_size': 1.0, 'avg_fill_price': 100.0
        }
        
        mock_strategy = MagicMock()
        # Strategy wants 4% SL (Price 96.0)
        mock_strategy.calculate_stop_loss.return_value = 96.0 
        mock_strategy.calculate_take_profit.return_value = 110.0
        mock_strategy.get_trailing_stop_config.return_value = {'enabled': False}
        strategies_map = {'test_strat': mock_strategy}
        
        # Leverage Manager Fallback wants 1.6% SL (Price 98.4)
        execution_engine.leverage_manager.calculate_stop_loss_with_leverage.return_value = 98.4
        execution_engine.leverage_manager.calculate_take_profit_with_leverage.return_value = 105.0
        execution_engine.leverage_manager.calculate_take_profit_with_capital_at_risk.return_value = 105.0
        execution_engine.leverage_manager.calculate_leveraged_position_size.return_value = (1.0, 100.0, 1.0)
        
        execution_engine.portfolio_manager.total_equity = 10000.0
        
        # Execute
        execution_engine.execute_trade(symbol, signal, current_price, 'test_strat', {}, strategies_map)
        
        # Verify Position
        pos = execution_engine.positions[symbol]
        
        # OLD Logic would pick 98.4 (Tightest/Max)
        # NEW Logic should pick 96.0 (Strategy) because it's safer than Account Limit (-200)
        assert pos.stop_loss == 96.0

    def test_multi_leg_entry_persists_to_db(self, execution_engine, mock_market_api):
        """Verify execute_multi_leg_entry persists position atomically to DB."""
        from unittest.mock import MagicMock
        # Real equity so the gross-leverage cap (2x) doesn't block the entry
        # (float(MagicMock()) coerces to 1.0)
        execution_engine.portfolio_manager.total_equity = 10000.0
        
        # Mock API order response
        mock_market_api.execute_order.return_value = {
            'filled_size': 1.0, 'avg_fill_price': 100.0, 'order_id': 'test123'
        }
        mock_market_api.get_current_price.return_value = 100.0
        mock_market_api.get_spot_token_for_perp.return_value = None
        mock_market_api.get_spot_price.return_value = None
        
        # Mock position sizing
        execution_engine.leverage_manager.calculate_leveraged_position_size.return_value = (1.0, 100.0, 1.0)
        execution_engine.leverage_manager.can_open_position.return_value = True
        execution_engine.portfolio_manager.calculate_available_capital_for_trading.return_value = 10000.0
        
        # Mock balance checks to ensure entry isn't blocked
        mock_market_api.get_spot_balance.return_value = 10000.0
        mock_market_api.get_perp_balance.return_value = {'withdrawable': 10000.0}
        mock_market_api.ensure_perp_funds.return_value = True
        mock_market_api.ensure_spot_funds.return_value = True
        
        signal = {
            'action': 'enter',
            'atomic': True,
            'legs': [
                {'symbol': 'BTC', 'market_type': 'perp', 'order_side': 'buy', 'side': 'long', 'hedge_ratio': 1.0},
                {'symbol': 'ETH', 'market_type': 'perp', 'order_side': 'sell', 'side': 'short', 'hedge_ratio': 1.0},
            ]
        }
        
        execution_engine.execute_multi_leg_entry('BTC-ETH', signal, 100.0, 'test_strat', {}, 0.8)
        
        # Verify position was persisted atomically via db.save_position (not bulk save)
        execution_engine.performance_tracker.db.save_position.assert_called()
        # Verify position exists in memory
        assert len(execution_engine.multi_leg_positions) > 0

    def test_multi_leg_scales_to_minimum_order(self, execution_engine, mock_market_api):
        """Verify multi-leg entry scales up when a leg is below $10 minimum."""
        from unittest.mock import MagicMock
        # Real equity so the gross-leverage cap (2x) doesn't block the entry
        execution_engine.portfolio_manager.total_equity = 10000.0
        
        # Setup: Prices so that hedge leg would be sub-$10
        # Leg 1: BTC @ $100, hedge_ratio 1.0 -> $100 notional (OK)
        # Leg 2: ETH @ $100, hedge_ratio 0.05 -> $5 notional (below $10)
        mock_market_api.execute_order.return_value = {
            'filled_size': 1.0, 'avg_fill_price': 100.0, 'order_id': 'test123'
        }
        mock_market_api.get_current_price.return_value = 100.0
        mock_market_api.get_spot_token_for_perp.return_value = None
        mock_market_api.get_spot_price.return_value = None
        mock_market_api.get_spot_balance.return_value = 10000.0
        mock_market_api.get_perp_balance.return_value = {'withdrawable': 10000.0}
        mock_market_api.ensure_perp_funds.return_value = True
        mock_market_api.ensure_spot_funds.return_value = True
        
        execution_engine.leverage_manager.calculate_leveraged_position_size.return_value = (1.0, 100.0, 1.0)
        execution_engine.leverage_manager.can_open_position.return_value = True
        execution_engine.portfolio_manager.calculate_available_capital_for_trading.return_value = 10000.0
        execution_engine.save_positions_to_db = MagicMock()
        
        signal = {
            'action': 'enter',
            'atomic': True,
            'legs': [
                {'symbol': 'BTC', 'market_type': 'perp', 'order_side': 'buy', 'side': 'long', 'hedge_ratio': 1.0},
                {'symbol': 'ETH', 'market_type': 'perp', 'order_side': 'sell', 'side': 'short', 'hedge_ratio': 0.05},  # Would be $5 < $10
            ]
        }
        
        # Execute with $100 notional (leg2 would be $5)
        execution_engine.execute_multi_leg_entry('BTC-ETH', signal, 100.0, 'test_strat', {}, 0.8)
        
        # Verify: should have scaled up and executed
        assert len(execution_engine.multi_leg_positions) > 0
        # Check that execute_order was called (trade went through after scaling)
        assert mock_market_api.execute_order.called

    def test_premature_displacement_blocked_by_risk(self, execution_engine, mock_market_api):
        """
        Verify that an existing trade is NOT displaced if the NEW trade fails risk checks.
        
        Bug Reproduction:
        1. Existing trade 'victim' uses BTC.
        2. New trade 'aggressor' uses BTC (conflict).
        3. 'aggressor' fails risk check (e.g. after scaling).
        4. Expected: 'victim' survives.
        5. Actual (Bug): 'victim' is displaced before 'aggressor' is checked.
        """
        from unittest.mock import MagicMock
        from src.models.trade import Position
        
        # 1. Setup VICTIM position (Single Leg BTC)
        victim_symbol = "BTC"
        victim_pos = Position(
            symbol=victim_symbol, side='long', size=1.0, entry_price=50000, strategy='victim_strat',
            entry_time=datetime.now()
        )
        execution_engine.positions[victim_symbol] = victim_pos
        
        # 2. Setup AGGRESSOR signal (Conflicting BTC leg)
        # Note: We use a multi-leg signal format
        signal = {
            'action': 'enter',
            'legs': [
                {'symbol': 'BTC', 'market_type': 'perp', 'order_side': 'sell', 'side': 'short', 'hedge_ratio': 1.0},
            ]
        }
        
        # 3. Mock Risk Check Failure
        # Logic: calculate_leveraged_position_size returns valid size...
        execution_engine.leverage_manager.calculate_leveraged_position_size.return_value = (1.0, 1000.0, 1.0)
        # ... BUT can_open_position returns FALSE (High Risk)
        execution_engine.leverage_manager.can_open_position.return_value = False
        
        # Mock other dependencies to ensure we reach the risk check
        execution_engine.portfolio_manager.calculate_available_capital_for_trading.return_value = 10000.0
        mock_market_api.get_current_price.return_value = 50000.0
        # CRITICAL: Mock execute_order so close_position succeeds during displacement
        mock_market_api.execute_order.return_value = {
            'filled_size': 1.0, 'avg_fill_price': 50000, 'order_id': 'close_123'
        }
        
        # Mock Strategy Manager for conflict resolution
        mock_strategy_manager = MagicMock()
        mock_strategy_manager._should_displace_position.return_value = True
        mock_strategy_manager._get_position_profitability_score.return_value = 0.0
        mock_strategy_manager._get_multi_leg_profitability_score.return_value = 0.0
        
        # 4. Execute
        execution_engine.execute_multi_leg_entry(
            'BTC-Short', signal, 50000.0, 'aggressor_strat', {}, 0.9, strategy_manager=mock_strategy_manager
        )
        
        # 5. Verify VICTIM SURVIVAL
        # If bug exists: Victim is gone (displaced)
        # If fixed: Victim remains
        assert victim_symbol in execution_engine.positions, "Victim trade was prematurely displaced!"
        
        # Verify Aggressor did NOT execute
        assert len(execution_engine.multi_leg_positions) == 0

    def test_close_position_urgency_detection(self, execution_engine, mock_market_api):
        """Test that urgency is correctly determined based on exit reason (substring matching)."""
        from src.models.trade import Position
        
        # Setup: Create a test position
        test_position = Position(
            symbol='TEST',
            side='long',
            size=1.0,
            entry_price=100.0,
            strategy='test_strat',
            entry_time=datetime.now(),
            capital_at_risk=100.0,
            leverage=1.0
        )
        execution_engine.positions['TEST'] = test_position
        
        # Mock successful order response
        mock_market_api.execute_order.return_value = {
            'order_id': 'close_123',
            'status': 'filled',
            'filled_size': 1.0,
            'avg_fill_price': 105.0,
            'fills': [{'oid': 'fill_123'}]
        }
        
        # Test cases: (reason, expected_urgency)
        test_cases = [
            ('stop_loss', 'high'),              # Exact match
            ('stop_loss_realtime', 'high'),     # Substring match (the bug fix)
            ('stop_loss_trailing', 'high'),     # Substring match
            ('liquidation_risk', 'high'),       # Exact match
            ('liquidation_risk_margin', 'high'),# Substring match
            ('emergency', 'high'),              # Exact match
            ('emergency_portfolio', 'high'),    # Substring match
            ('take_profit', 'normal'),          # No match
            ('take_profit_realtime', 'normal'), # No match
            ('manual', 'normal'),               # No match
            ('regime_break', 'normal'),         # No match
        ]
        
        for reason, expected_urgency in test_cases:
            # Reset position for each test
            execution_engine.positions['TEST'] = Position(
                symbol='TEST',
                side='long',
                size=1.0,
                entry_price=100.0,
                strategy='test_strat',
                entry_time=datetime.now(),
                capital_at_risk=100.0,
                leverage=1.0
            )
            mock_market_api.execute_order.reset_mock()
            
            # Execute close
            execution_engine.close_position('TEST', reason=reason)
            
            # Verify the API was called with the correct urgency
            call_kwargs = mock_market_api.execute_order.call_args[1]
            actual_urgency = call_kwargs.get('urgency', 'normal')
            
            assert actual_urgency == expected_urgency, \
                f"Reason '{reason}' expected urgency '{expected_urgency}', got '{actual_urgency}'"
