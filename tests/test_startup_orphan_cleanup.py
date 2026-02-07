"""
Unit tests for _check_startup_orphans functionality.
Tests that positions from disabled strategies are correctly identified and closed on startup.
"""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime
from src.models.trade import Position, MultiLegPosition, PositionLeg


class TestStartupOrphanCleanup:
    """Tests for _check_startup_orphans in StrategyManager."""
    
    @pytest.fixture
    def base_config(self):
        """Minimal config for StrategyManager."""
        return {
            'strategies': {
                'ohlcv_limit': 300,
                'stat_arb_1h': {'enabled': True}  # Only stat_arb enabled
            },
            'trading': {
                'max_positions_percentage': 90,
                'base_currency': 'USDC',
                'order_timeout_minutes': 5,
                'enable_stale_order_cleanup': True,
                'position_sync_interval': 300,
                'enable_position_validation': False,
                'max_positions_per_strategy': 5,
                'max_pairs_to_trade': 50,
                'position_monitoring_interval': 60
            },
            'risk_management': {},
            'dynamic_pair_selection': {
                'enabled': False
            }
        }
    
    @pytest.fixture
    def mock_strategy_manager(self, base_config):
        """Create a minimal mock StrategyManager for testing orphan cleanup."""
        with patch('src.strategies.strategy_manager.StrategyManager.__init__', return_value=None):
            from src.strategies.strategy_manager import StrategyManager
            sm = StrategyManager.__new__(StrategyManager)
            
            # Setup minimal attributes
            sm.config = base_config
            sm.logger = MagicMock()
            sm.market_api = MagicMock()
            sm.execution_engine = MagicMock()
            sm.performance_tracker = MagicMock()
            
            # Only 'stat_arb_1h' is enabled
            sm.strategies = {'stat_arb_1h': MagicMock()}
            
            # Positions are stored in execution_engine (sm.positions is a property)
            sm.execution_engine.positions = {}
            sm.execution_engine.multi_leg_positions = {}
            
            return sm
    
    # ==========================================================================
    # SINGLE-LEG ORPHAN TESTS
    # ==========================================================================
    
    def test_single_leg_orphan_on_exchange_is_closed(self, mock_strategy_manager):
        """Test: Single-leg position from disabled strategy ON exchange gets closed."""
        sm = mock_strategy_manager
        
        # Create orphan position (strategy 'vol_breakout' is NOT in sm.strategies)
        orphan_pos = Position(
            symbol='BTC',
            side='long',
            size=0.1,
            entry_price=50000.0,
            strategy='vol_breakout',  # Disabled strategy
            entry_time=datetime.now()
        )
        sm.execution_engine.positions = {'BTC': orphan_pos}
        
        # Position exists on exchange
        sm.market_api.get_position.return_value = {'size': 0.1}
        
        # Mock close_position
        sm.close_position = MagicMock()
        
        # Run orphan check
        sm._check_startup_orphans()
        
        # Verify position was closed
        sm.close_position.assert_called_once_with('BTC', reason='startup_orphan_cleanup')
        sm.logger.warning.assert_any_call(
            "Found orphan position BTC (strategy: vol_breakout) on exchange (size=0.1). Closing..."
        )
    
    def test_single_leg_orphan_local_only_is_archived(self, mock_strategy_manager):
        """Test: Single-leg position from disabled strategy NOT on exchange gets archived."""
        sm = mock_strategy_manager
        
        # Create orphan position
        orphan_pos = Position(
            symbol='ETH',
            side='short',
            size=1.0,
            entry_price=3000.0,
            strategy='csm',  # Disabled strategy
            entry_time=datetime.now(),
            capital_at_risk=500.0,
            leverage=2.0
        )
        sm.execution_engine.positions = {'ETH': orphan_pos}
        
        # Position does NOT exist on exchange
        sm.market_api.get_position.return_value = {'size': 0.0}
        
        # Mock _find_closing_fill
        sm._find_closing_fill = MagicMock(return_value=(3100.0, datetime.now(), 'manual', 0.5))
        
        # Run orphan check
        sm._check_startup_orphans()
        
        # Verify position was archived (recorded + deleted)
        sm.performance_tracker.record_trade_from_position.assert_called_once()
        sm.execution_engine.delete_position_from_db.assert_called()
        assert 'ETH' not in sm.execution_engine.positions
    
    def test_enabled_strategy_position_not_touched(self, mock_strategy_manager):
        """Test: Position from enabled strategy is NOT touched."""
        sm = mock_strategy_manager
        
        # Create position from enabled strategy
        valid_pos = Position(
            symbol='SOL',
            side='long',
            size=10.0,
            entry_price=100.0,
            strategy='stat_arb_1h',  # Enabled strategy
            entry_time=datetime.now()
        )
        sm.execution_engine.positions = {'SOL': valid_pos}
        sm.close_position = MagicMock()
        
        # Run orphan check
        sm._check_startup_orphans()
        
        # Verify position was NOT closed
        sm.close_position.assert_not_called()
        assert 'SOL' in sm.execution_engine.positions
    
    # ==========================================================================
    # MULTI-LEG ORPHAN TESTS (with PositionLeg dataclass)
    # ==========================================================================
    
    def test_multi_leg_orphan_on_exchange_is_closed(self, mock_strategy_manager):
        """Test: Multi-leg position from disabled strategy with legs ON exchange gets closed."""
        sm = mock_strategy_manager
        
        # Create multi-leg position with PositionLeg DATACLASS objects (not dicts)
        leg1 = PositionLeg(symbol='BTC', side='long', size=0.01, entry_price=50000.0, market_type='perp')
        leg2 = PositionLeg(symbol='ETH', side='short', size=0.1, entry_price=3000.0, market_type='perp')
        
        orphan_ml = MultiLegPosition(
            position_id='ml_orphan_1',
            strategy='funding_arb',  # Disabled strategy (not in sm.strategies)
            legs=[leg1, leg2],
            entry_time=datetime.now()
        )
        sm.execution_engine.multi_leg_positions = {'ml_orphan_1': orphan_ml}
        
        # Legs exist on exchange
        def mock_get_position(symbol):
            if symbol == 'BTC':
                return {'size': 0.01}
            elif symbol == 'ETH':
                return {'size': -0.1}
            return {'size': 0.0}
        
        sm.market_api.get_position.side_effect = mock_get_position
        
        # Mock _handle_multi_leg_signal
        sm._handle_multi_leg_signal = MagicMock()
        
        # Run orphan check
        sm._check_startup_orphans()
        
        # Verify multi-leg exit was triggered
        sm._handle_multi_leg_signal.assert_called_once()
        call_args = sm._handle_multi_leg_signal.call_args
        assert call_args[0][1]['action'] == 'exit'
        assert call_args[0][1]['type'] == 'startup_orphan_cleanup'
    
    def test_multi_leg_orphan_local_only_is_deleted(self, mock_strategy_manager):
        """Test: Multi-leg position from disabled strategy NOT on exchange gets deleted."""
        sm = mock_strategy_manager
        
        # Create multi-leg position with PositionLeg DATACLASS objects
        leg1 = PositionLeg(symbol='LINK', side='long', size=5.0, entry_price=15.0, market_type='perp')
        leg2 = PositionLeg(symbol='AVAX', side='short', size=2.0, entry_price=40.0, market_type='perp')
        
        orphan_ml = MultiLegPosition(
            position_id='ml_orphan_2',
            strategy='csm',  # Disabled strategy
            legs=[leg1, leg2],
            entry_time=datetime.now()
        )
        sm.execution_engine.multi_leg_positions = {'ml_orphan_2': orphan_ml}
        
        # Legs do NOT exist on exchange
        sm.market_api.get_position.return_value = {'size': 0.0}
        
        # Run orphan check
        sm._check_startup_orphans()
        
        # Verify position was deleted
        assert 'ml_orphan_2' not in sm.execution_engine.multi_leg_positions
        sm.execution_engine.delete_position_from_db.assert_called_with('ml_orphan_2')
    
    # Note: test_multi_leg_with_dict_legs_still_works removed because:\n    # - MultiLegPosition.primary_symbol requires PositionLeg objects (not dicts)\n    # - Dict-style legs are not used in production - all legs are PositionLeg dataclass
    
    def test_enabled_strategy_multi_leg_not_touched(self, mock_strategy_manager):
        """Test: Multi-leg position from enabled strategy is NOT touched."""
        sm = mock_strategy_manager
        
        # Create multi-leg position from enabled strategy
        leg1 = PositionLeg(symbol='BTC', side='long', size=0.01, entry_price=50000.0, market_type='perp')
        leg2 = PositionLeg(symbol='ETH', side='short', size=0.1, entry_price=3000.0, market_type='perp')
        
        valid_ml = MultiLegPosition(
            position_id='ml_valid',
            strategy='stat_arb_1h',  # Enabled strategy
            legs=[leg1, leg2],
            entry_time=datetime.now()
        )
        sm.execution_engine.multi_leg_positions = {'ml_valid': valid_ml}
        sm._handle_multi_leg_signal = MagicMock()
        
        # Run orphan check
        sm._check_startup_orphans()
        
        # Verify multi-leg exit was NOT triggered
        sm._handle_multi_leg_signal.assert_not_called()
        assert 'ml_valid' in sm.execution_engine.multi_leg_positions


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
