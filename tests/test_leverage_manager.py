import pytest
from unittest.mock import MagicMock
from src.utils.leverage_manager import LeverageManager

class TestLeverageManager:

    @pytest.fixture
    def leverage_manager(self, mock_config):
        """Creates a LeverageManager instance."""
        # Config needs leverage_management block which is standard in conftest
        return LeverageManager(mock_config)

    def test_calculate_dynamic_leverage(self, leverage_manager):
        """Test dynamic leverage calculation."""
        # Base leverage = 2.0 (default in code or config)
        # Using simple values
        lev = leverage_manager.calculate_dynamic_leverage(
            symbol="BTC", 
            strategy_name="test", 
            signal_strength=0.0, 
            market_volatility=0.0, 
            current_price=50000
        )
        # base(2.0) * signal(1.0) * vol(1.0) * strat(1.0) = 2.0
        assert lev >= 1.0

    def test_stop_loss_calculation(self, leverage_manager):
        """Test stop loss with leverage."""
        entry = 100.0
        leverage = 2.0
        # fallback_stop_loss_pct = 0.05
        # adjusted = 0.05 / 2.0 = 0.025 (2.5%)
        
        sl_long = leverage_manager.calculate_stop_loss_with_leverage(entry, 'long', leverage)
        assert sl_long == 97.5
        
        sl_short = leverage_manager.calculate_stop_loss_with_leverage(entry, 'short', leverage)
        assert sl_short == pytest.approx(102.5)

    def test_take_profit_calculation(self, leverage_manager):
        """Test take profit with leverage."""
        entry = 100.0
        leverage = 2.0
        # base tp = sl * 2 = 0.10
        # adjusted = 0.10 / 2.0 = 0.05 (5%)
        
        tp_long = leverage_manager.calculate_take_profit_with_leverage(entry, 'long', leverage)
        assert tp_long == 105.0

    def test_calculate_leveraged_position_size(self, leverage_manager):
        """Test position size with leverage."""
        # Requires portfolio manager or fallback
        # Let's test fallback path first (no portfolio manager)
        leverage_manager.portfolio_manager = None
        
        size, margin, lev = leverage_manager.calculate_leveraged_position_size(
            symbol="BTC",
            current_price=50000,
            available_capital=10000,
            strategy_name="test",
            signal_strength=0.5,
            market_volatility=0.0
        )
        
        assert size > 0
        assert margin > 0
        assert lev >= 1.0
        
        # Check margin limit (should not exceed available * buffer)
        buffer_pct = leverage_manager.margin_buffer_percentage
        max_margin = 10000 * (1 - buffer_pct/100)
        assert margin <= max_margin
