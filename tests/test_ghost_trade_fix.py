import pytest
from unittest.mock import MagicMock, patch, ANY
from src.strategies.statistical_arbitrage_strategy import StatisticalArbitrageStrategy
from src.strategies.execution_engine import ExecutionEngine
from src.models.trade import MultiLegPosition, PositionLeg
import pandas as pd
from datetime import datetime

class TestGhostTradeFixes:
    """Tests for the ghost trade fixes (Stat Arb re-entry and Cleanup crash)."""

    @pytest.fixture
    def mock_strategy(self):
        config = {
            'strategies': {
                'ohlcv_limit': 100,
                'stat_arb': {
                    'z_score_threshold': 2.0,
                    'regime_break_threshold': 4.0, # Test this specific threshold
                    'window_size': 20,
                    'update_interval_hours': 24
                },
                'cointegration': {}
            }
        }
        return StatisticalArbitrageStrategy(config)

    @pytest.fixture
    def mock_execution_engine(self):
        mock_config = MagicMock()
        mock_market_api = MagicMock()
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

    def test_stat_arb_prevent_reentry_on_regime_break(self, mock_strategy):
        """
        Verify that Stat Arb strategy does NOT generate an entry signal 
        if the Z-score is already above the regime_break_threshold.
        This prevents the immediate exit-on-entry loop.
        """
        symbol_a = 'BTC'
        symbol_b = 'ETH'
        hedge_ratio = 0.5
        
        # Case 1: Z-score is 3.0 (Entry trigger > 2.0, but < 4.0). Should ENTER.
        z_score_valid = 3.0
        mock_strategy._get_hedge_ratio = MagicMock(return_value=(hedge_ratio, hedge_ratio))
        mock_strategy._calculate_spread_zscore = MagicMock(return_value=z_score_valid)
        
        with patch('src.strategies.statistical_arbitrage_strategy.hurst_exponent', return_value=0.4):
            ohlcv_a = pd.DataFrame({'close': [50000.0] * 100})
            ohlcv_b = pd.DataFrame({'close': [2000.0] * 100})
            
            signal = mock_strategy.generate_pair_signal(symbol_a, ohlcv_a, symbol_b, ohlcv_b)
            
        assert signal is not None
        assert signal['signal'] == 'sell' # Short spread
        
        # Case 2: Z-score is 5.0 (Above regime break 4.0). Should NOT ENTER.
        z_score_break = 5.0
        mock_strategy._calculate_spread_zscore = MagicMock(return_value=z_score_break)
        
        with patch('src.strategies.statistical_arbitrage_strategy.hurst_exponent', return_value=0.4):
            signal = mock_strategy.generate_pair_signal(symbol_a, ohlcv_a, symbol_b, ohlcv_b)
            
        assert signal is None # Should be filtered out

    def test_cleanup_ghost_position_calls_close_position(self, mock_execution_engine):
        """
        Verify that _cleanup_ghost_position calls the correct close_position method
        and deletes the position from the database.
        """
        # Setup ghost position
        pos_id = "ghost_123"
        leg1 = PositionLeg(symbol="BTC", market_type="perp", side="long", size=1.0, entry_price=50000, order_id="1")
        leg2 = PositionLeg(symbol="ETH", market_type="perp", side="short", size=10.0, entry_price=3000, order_id="2")
        
        position = MultiLegPosition(
            position_id=pos_id,
            legs=[leg1, leg2],
            strategy="stat_arb",
            entry_time=datetime.now()
        )
        mock_execution_engine.multi_leg_positions[pos_id] = position
        
        # Mock close_position to return success
        mock_execution_engine.close_position = MagicMock(return_value=(True, ""))

        # FIX: Mock get_leg_price to return float (prevents log crash)
        mock_execution_engine.get_leg_price = MagicMock(return_value=100.0)
        
        # Call cleanup
        mock_execution_engine._cleanup_ghost_position(position)
        
        # Verify close_position was NOT called (we don't send orders for ghosts)
        assert mock_execution_engine.close_position.call_count == 0
        
        # Verify DB deletion was called
        mock_execution_engine.performance_tracker.db.delete_position.assert_called_with(pos_id)
        
        # Verify leverage manager close called (margin release)
        mock_execution_engine.leverage_manager.close_position.assert_called()
        
        # Verify removed from memory
        assert pos_id not in mock_execution_engine.multi_leg_positions

