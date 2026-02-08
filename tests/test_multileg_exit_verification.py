"""
Unit tests for multi-leg pre-exit verification and ghost position cleanup.

Tests the pre-exit leg verification in execute_multi_leg_exit and the
_cleanup_ghost_position helper method.
"""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime

from src.strategies.execution_engine import ExecutionEngine, PositionLeg, MultiLegPosition


class TestMultiLegPreExitVerification:
    """Test pre-exit leg verification in execute_multi_leg_exit."""
    
    @pytest.fixture
    def mock_config(self):
        """Default config for tests."""
        return {
            'trading': {
                'multi_leg': {
                    'auto_scale_to_funds': False,
                }
            },
            'risk_management': {},
        }
    
    @pytest.fixture
    def mock_market_api(self):
        """Mock market API."""
        api = MagicMock()
        api.get_positions.return_value = []
        api.execute_order.return_value = {
            'order_id': '123',
            'filled_size': 1.0,
            'avg_fill_price': 100.0,
            'status': 'filled',
        }
        return api
    
    @pytest.fixture
    def execution_engine(self, mock_config, mock_market_api):
        """Create execution engine with mocks."""
        mock_leverage_manager = MagicMock()
        mock_portfolio_manager = MagicMock()
        mock_performance_tracker = MagicMock()
        mock_performance_tracker.db = MagicMock()
        mock_pair_selector = MagicMock()
        
        ee = ExecutionEngine(
            mock_config, 
            mock_market_api, 
            mock_leverage_manager,
            mock_portfolio_manager,
            mock_performance_tracker,
            mock_pair_selector
        )
        return ee
    
    @pytest.fixture
    def sample_multi_leg_position(self):
        """Create sample multi-leg position with two legs."""
        legs = [
            PositionLeg(
                symbol="ETH",
                market_type="perp",
                side="long",
                size=1.0,
                entry_price=3000.0,
                order_id="order1"
            ),
            PositionLeg(
                symbol="ETH_SPOT",
                market_type="spot",
                side="short",
                size=1.0,
                entry_price=3005.0,
                order_id="order2"
            ),
        ]
        
        return MultiLegPosition(
            position_id="stat_arb_4h_ETH_1234567890",
            strategy="stat_arb_4h",
            entry_time=datetime.now(),
            legs=legs,
            capital_at_risk=500.0,
            metadata={},
        )
    
    # =========================================================================
    # Pre-Exit Verification Tests
    # =========================================================================
    
    def test_exit_with_all_legs_present(self, execution_engine, mock_market_api, sample_multi_leg_position):
        """Test normal exit when all legs exist on exchange."""
        # Setup: Both legs exist on exchange
        mock_market_api.get_positions.return_value = [
            {'coin': 'ETH', 'szi': '1.0'},
            {'coin': 'ETH_SPOT', 'szi': '-1.0'},
        ]
        
        execution_engine.multi_leg_positions[sample_multi_leg_position.position_id] = sample_multi_leg_position
        
        signal = {'action': 'exit', 'reason': 'Take Profit'}
        execution_engine.execute_multi_leg_exit(
            symbol="ETH",
            signal=signal,
            strategy_name="stat_arb_4h",
            strategies_map={},
        )
        
        # Should execute close orders for both legs
        assert mock_market_api.execute_order.call_count == 2
    
    def test_exit_with_one_leg_missing(self, execution_engine, mock_market_api, sample_multi_leg_position):
        """Test exit when one leg is missing from exchange."""
        # Setup: Only perp leg exists, spot leg is gone
        mock_market_api.get_positions.return_value = [
            {'coin': 'ETH', 'szi': '1.0'},
            # ETH_SPOT is missing!
        ]
        
        execution_engine.multi_leg_positions[sample_multi_leg_position.position_id] = sample_multi_leg_position
        
        signal = {'action': 'exit', 'reason': 'Take Profit'}
        execution_engine.execute_multi_leg_exit(
            symbol="ETH",
            signal=signal,
            strategy_name="stat_arb_4h",
            strategies_map={},
        )
        
        # Should only execute close order for remaining leg (ETH perp)
        assert mock_market_api.execute_order.call_count == 1
        call_args = mock_market_api.execute_order.call_args
        assert call_args.kwargs['symbol'] == 'ETH'
    
    def test_exit_with_all_legs_missing(self, execution_engine, mock_market_api, sample_multi_leg_position):
        """Test exit when all legs are missing from exchange (ghost position)."""
        # Setup: No legs exist on exchange
        mock_market_api.get_positions.return_value = []
        
        execution_engine.multi_leg_positions[sample_multi_leg_position.position_id] = sample_multi_leg_position
        
        signal = {'action': 'exit', 'reason': 'Take Profit'}
        execution_engine.execute_multi_leg_exit(
            symbol="ETH",
            signal=signal,
            strategy_name="stat_arb_4h",
            strategies_map={},
        )
        
        # Should NOT execute any close orders
        assert mock_market_api.execute_order.call_count == 0
    
    def test_exit_no_position_found(self, execution_engine, mock_market_api):
        """Test exit when no position exists in memory."""
        signal = {'action': 'exit', 'reason': 'Take Profit'}
        
        # Should return early without error
        execution_engine.execute_multi_leg_exit(
            symbol="ETH",
            signal=signal,
            strategy_name="stat_arb_4h",
            strategies_map={},
        )
        
        assert mock_market_api.execute_order.call_count == 0
    
    # =========================================================================
    # Ghost Position Cleanup Tests
    # =========================================================================
    
    def test_cleanup_ghost_position_records_trade(self, execution_engine, sample_multi_leg_position):
        """Test ghost cleanup records trade with estimated PnL."""
        # Store original legs for PnL calculation
        sample_multi_leg_position._original_legs = sample_multi_leg_position.legs.copy()
        
        # Mock get_leg_price to return current prices
        execution_engine.get_leg_price = MagicMock(side_effect=[3050.0, 3040.0])  # ETH up $50, ETH_SPOT down $35
        
        execution_engine._cleanup_ghost_position(sample_multi_leg_position, reason="External Close")
        
        # Verify trade was recorded
        execution_engine.performance_tracker.record_trade.assert_called_once()
        call_kwargs = execution_engine.performance_tracker.record_trade.call_args.kwargs
        assert call_kwargs['strategy'] == 'stat_arb_4h'
        assert 'Ghost' in call_kwargs['exit_reason']
    
    def test_cleanup_ghost_position_removes_from_memory(self, execution_engine, sample_multi_leg_position):
        """Test ghost cleanup removes position from memory."""
        execution_engine.multi_leg_positions[sample_multi_leg_position.position_id] = sample_multi_leg_position
        
        # Mock get_leg_price to return numeric values (prevents MagicMock format error)
        execution_engine.get_leg_price = MagicMock(return_value=3050.0)
        
        execution_engine._cleanup_ghost_position(sample_multi_leg_position, reason="Test")
        
        assert sample_multi_leg_position.position_id not in execution_engine.multi_leg_positions
    
    def test_cleanup_ghost_position_deletes_from_db(self, execution_engine, sample_multi_leg_position):
        """Test ghost cleanup deletes position from DB."""
        # Mock get_leg_price to return numeric values
        execution_engine.get_leg_price = MagicMock(return_value=3050.0)
        
        execution_engine._cleanup_ghost_position(sample_multi_leg_position, reason="Test")
        
        execution_engine.performance_tracker.db.delete_position.assert_called_with(
            sample_multi_leg_position.position_id
        )
    
    def test_cleanup_ghost_position_releases_margin(self, execution_engine, sample_multi_leg_position):
        """Test ghost cleanup releases margin in leverage manager."""
        # Mock get_leg_price to return numeric values
        execution_engine.get_leg_price = MagicMock(return_value=3050.0)
        
        execution_engine._cleanup_ghost_position(sample_multi_leg_position, reason="Test")
        
        execution_engine.leverage_manager.close_position.assert_called_with('ETH', 0.0)


class TestPositionLegSymbolMatching:
    """Test symbol matching logic for leg verification."""
    
    @pytest.fixture
    def mock_config(self):
        return {'trading': {}, 'risk_management': {}}
    
    @pytest.fixture
    def mock_market_api(self):
        api = MagicMock()
        return api
    
    @pytest.fixture
    def execution_engine(self, mock_config, mock_market_api):
        mock_leverage_manager = MagicMock()
        mock_portfolio_manager = MagicMock()
        mock_performance_tracker = MagicMock()
        mock_performance_tracker.db = MagicMock()
        mock_pair_selector = MagicMock()
        
        return ExecutionEngine(
            mock_config, 
            mock_market_api, 
            mock_leverage_manager,
            mock_portfolio_manager,
            mock_performance_tracker,
            mock_pair_selector
        )
    
    def test_symbol_matching_with_coin_field(self, execution_engine, mock_market_api):
        """Test symbol matching when API returns 'coin' field."""
        legs = [
            PositionLeg(symbol="ETH", market_type="perp", side="long", size=1.0, entry_price=3000.0),
        ]
        position = MultiLegPosition(
            position_id="test_pos",
            strategy="stat_arb",
            entry_time=datetime.now(),
            legs=legs,
            capital_at_risk=500.0,
        )
        
        mock_market_api.get_positions.return_value = [
            {'coin': 'ETH', 'szi': '1.0'},  # Uses 'coin' field
        ]
        mock_market_api.execute_order.return_value = {
            'order_id': '123', 'filled_size': 1.0, 'avg_fill_price': 3050.0
        }
        
        execution_engine.multi_leg_positions[position.position_id] = position
        
        execution_engine.execute_multi_leg_exit("ETH", {}, "stat_arb", {})
        
        # Should find the leg and attempt to close it
        assert mock_market_api.execute_order.call_count == 1
    
    def test_symbol_matching_with_symbol_field(self, execution_engine, mock_market_api):
        """Test symbol matching when API returns 'symbol' field."""
        legs = [
            PositionLeg(symbol="ETH", market_type="perp", side="long", size=1.0, entry_price=3000.0),
        ]
        position = MultiLegPosition(
            position_id="test_pos",
            strategy="stat_arb",
            entry_time=datetime.now(),
            legs=legs,
            capital_at_risk=500.0,
        )
        
        mock_market_api.get_positions.return_value = [
            {'symbol': 'ETH', 'szi': '1.0'},  # Uses 'symbol' field instead
        ]
        mock_market_api.execute_order.return_value = {
            'order_id': '123', 'filled_size': 1.0, 'avg_fill_price': 3050.0
        }
        
        execution_engine.multi_leg_positions[position.position_id] = position
        
        execution_engine.execute_multi_leg_exit("ETH", {}, "stat_arb", {})
        
        # Should find the leg and attempt to close it
        assert mock_market_api.execute_order.call_count == 1
