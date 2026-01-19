"""
Unit tests for stop loss and take profit direction.
Ensures SL/TP are on correct side of entry for longs and shorts.
"""

import pytest
from unittest.mock import MagicMock


class TestStopLossDirection:
    """Tests to ensure SL is calculated with correct direction for long/short."""
    
    @pytest.fixture
    def leverage_manager(self):
        """Create a minimal leverage manager for testing."""
        from src.utils.leverage_manager import LeverageManager
        
        config = {
            'risk_management': {
                'margin_buffer_percentage': 10.0,
                'liquidation_risk_threshold': 0.15,
                'max_drawdown_percentage': 20.0,
                'position_limit': 10,
            },
            'leverage_management': {
                'risk_per_trade_pct': 1.0,
                'fallback_stop_loss_pct': 0.05,  # 5%
            },
            'trading': {
                'use_portfolio_based_sizing': True,
                'max_position_size_percentage': 10.0,
            },
        }
        
        portfolio_manager = MagicMock()
        portfolio_manager.total_equity = 10000
        
        return LeverageManager(config, portfolio_manager)
    
    def test_long_stop_loss_below_entry(self, leverage_manager):
        """For LONG positions, stop loss should be BELOW entry price."""
        entry_price = 100.0
        leverage = 2.0
        
        stop_loss = leverage_manager.calculate_stop_loss_with_leverage(
            entry_price, 'long', leverage
        )
        
        assert stop_loss < entry_price, \
            f"Long SL {stop_loss} should be < entry {entry_price}"
    
    def test_short_stop_loss_above_entry(self, leverage_manager):
        """For SHORT positions, stop loss should be ABOVE entry price."""
        entry_price = 100.0
        leverage = 2.0
        
        stop_loss = leverage_manager.calculate_stop_loss_with_leverage(
            entry_price, 'short', leverage
        )
        
        assert stop_loss > entry_price, \
            f"Short SL {stop_loss} should be > entry {entry_price}"
    
    def test_long_take_profit_above_entry(self, leverage_manager):
        """For LONG positions, take profit should be ABOVE entry price."""
        entry_price = 100.0
        leverage = 2.0
        
        take_profit = leverage_manager.calculate_take_profit_with_leverage(
            entry_price, 'long', leverage
        )
        
        assert take_profit > entry_price, \
            f"Long TP {take_profit} should be > entry {entry_price}"
    
    def test_short_take_profit_below_entry(self, leverage_manager):
        """For SHORT positions, take profit should be BELOW entry price."""
        entry_price = 100.0
        leverage = 2.0
        
        take_profit = leverage_manager.calculate_take_profit_with_leverage(
            entry_price, 'short', leverage
        )
        
        assert take_profit < entry_price, \
            f"Short TP {take_profit} should be < entry {entry_price}"
    
    def test_invalid_side_defaults_to_long(self, leverage_manager):
        """Invalid side should default to long behavior (SL below entry)."""
        entry_price = 100.0
        leverage = 2.0
        
        # Pass invalid side 'sell' instead of 'short'
        stop_loss = leverage_manager.calculate_stop_loss_with_leverage(
            entry_price, 'sell', leverage  # BUG: should be 'short'
        )
        
        # Should default to long (SL below entry)
        assert stop_loss < entry_price, \
            "Invalid side should default to long behavior"
    
    def test_sl_percentage_scales_with_leverage(self, leverage_manager):
        """Higher leverage should result in tighter stop loss."""
        entry_price = 100.0
        
        sl_1x = leverage_manager.calculate_stop_loss_with_leverage(entry_price, 'long', 1.0)
        sl_5x = leverage_manager.calculate_stop_loss_with_leverage(entry_price, 'long', 5.0)
        
        # 5x leverage should have tighter SL (closer to entry)
        distance_1x = entry_price - sl_1x
        distance_5x = entry_price - sl_5x
        
        assert distance_5x < distance_1x, \
            f"5x leverage SL should be tighter than 1x"


class TestOUStrategyTPDirection:
    """Tests for OU mean reversion strategy TP calculation."""
    
    @pytest.fixture
    def ou_strategy(self):
        """Create OU strategy for testing."""
        from src.strategies.ou_mean_reversion_strategy import OUMeanReversionStrategy
        
        config = {
            'strategies': {
                'ohlcv_limit': 100,
                'ou_mean_reversion': {},
            },
        }
        
        return OUMeanReversionStrategy(config)
    
    def test_long_signal_tp_above_entry(self, ou_strategy):
        """For LONG position, TP should be ABOVE entry price."""
        entry_price = 100.0
        
        take_profit = ou_strategy.calculate_take_profit(
            entry_price, 'long', signal_strength=1.0
        )
        
        assert take_profit > entry_price, \
            f"Long TP {take_profit} should be > entry {entry_price}"
    
    def test_short_signal_tp_below_entry(self, ou_strategy):
        """For SHORT position, TP should be BELOW entry price."""
        entry_price = 100.0
        
        take_profit = ou_strategy.calculate_take_profit(
            entry_price, 'short', signal_strength=1.0
        )
        
        assert take_profit < entry_price, \
            f"Short TP {take_profit} should be < entry {entry_price}"
    
    def test_tp_at_least_5_percent(self, ou_strategy):
        """TP should be at least 5% for 1:1 R:R with fallback SL."""
        entry_price = 100.0
        
        take_profit = ou_strategy.calculate_take_profit(
            entry_price, 'long', signal_strength=1.0
        )
        
        tp_pct = (take_profit - entry_price) / entry_price
        
        assert tp_pct >= 0.05, \
            f"TP percentage {tp_pct*100:.1f}% should be >= 5% for 1:1 R:R"
