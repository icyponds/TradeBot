
import pytest
import pandas as pd
import numpy as np
from unittest.mock import MagicMock
from src.utils.statistics import calculate_annualized_volatility
from src.utils.leverage_manager import LeverageManager
from src.config.settings import load_config

class TestSizingLogic:
    
    @pytest.fixture
    def config(self):
        """Mock configuration."""
        return {
            'trading': {
                'max_position_size_percentage': 10.0,
                'max_account_loss_per_trade': 3.0,
                'margin_buffer_percentage': 5.0,
                'liquidation_risk_threshold': 0.80
            },
            'leverage_management': {
                'target_annual_volatility': 0.40,
                'risk_per_trade_pct': 1.0,
                'fallback_stop_loss_pct': 0.05,
                'min_leverage': 0.5,
                'volatility_min': 0.10,
                'base_risk_per_trade_pct': 0.5 # Old key, should be ignored/overridden
            },
            'risk_management': {
                'margin_buffer_percentage': 5.0,
                'liquidation_risk_threshold': 0.80
            }
        }
        
    @pytest.fixture
    def leverage_manager(self, config):
        return LeverageManager(config)

    def test_annualized_volatility_calculation(self):
        """Verify volatility calculation matches annualization logic."""
        # 1% daily returns constant
        returns = [1.01] * 30
        prices = np.cumprod(returns)
        # Constant returns = 0 std dev
        vol = calculate_annualized_volatility(pd.Series(prices))
        assert vol == 0.0
        
        # Alternating +1% / -1% returns
        # Std dev of [0.01, -0.01, ...] is approx 0.01
        # Annualized = 0.01 * sqrt(365) ~= 0.191
        prices = [100]
        for i in range(50):
            change = 1.01 if i % 2 == 0 else 0.99
            prices.append(prices[-1] * change)
            
        vol = calculate_annualized_volatility(pd.Series(prices))
        # Expected around 0.19
        assert 0.18 < vol < 0.20

    def test_dynamic_leverage_vol_targeting(self, leverage_manager):
        """Test leverage scales based on volatility."""
        target_vol = 0.40
        
        # Case 1: Low Vol Asset (10% Annual) -> High Leverage (4x)
        lev_low = leverage_manager.calculate_dynamic_leverage(
            symbol="BTC", strategy_name="test", signal_strength=0.5,
            market_volatility=0.10, current_price=100, asset_max_leverage=10.0
        )
        # 0.40 / 0.10 = 4.0x
        assert lev_low == 4.0
        
        # Case 2: High Vol Asset (80% Annual) -> Low Leverage (0.5x)
        lev_high = leverage_manager.calculate_dynamic_leverage(
            symbol="MEME", strategy_name="test", signal_strength=0.5,
            market_volatility=0.80, current_price=100, asset_max_leverage=10.0
        )
        # 0.40 / 0.80 = 0.5x
        assert lev_high == 0.5
        
        # Case 3: Target Vol Asset (40%) -> 1.0x
        lev_match = leverage_manager.calculate_dynamic_leverage(
            symbol="ETH", strategy_name="test", signal_strength=0.5,
            market_volatility=0.40, current_price=100, asset_max_leverage=10.0
        )
        assert lev_match == 1.0

    def test_risk_based_sizing(self, leverage_manager):
        """Test sizing logic: Size = (Equity * Risk) / SL."""
        equity = 10000.0
        leverage_manager.portfolio_manager = MagicMock()
        leverage_manager.portfolio_manager.total_equity = equity
        leverage_manager.portfolio_manager.calculate_max_position_size = lambda x: 1000.0 # 10%
        leverage_manager.portfolio_manager.calculate_available_capital_for_trading = MagicMock(return_value=equity)
        leverage_manager.portfolio_manager.can_open_position = MagicMock(return_value=True)
        
        risk_pct = 0.01 # 1%
        risk_budget = equity * risk_pct # $100
        
        # Case 1: Strategy provides 5% SL
        sl_pct = 0.05
        expected_notional = risk_budget / sl_pct # $100 / 0.05 = $2000
        # But max position size is 10% ($1000)
        # So expected is $1000
        
        size, margin, lev = leverage_manager.calculate_leveraged_position_size(
            "BTC", current_price=100.0, available_capital=equity,
            strategy_name="test", signal_strength=0.5, market_volatility=0.40, # Lev 1.0x
            asset_max_leverage=10.0, stop_loss_pct=sl_pct
        )
        
        assert size * 100.0 == 1000.0 # Capped at max size
        
        # Case 2: Strategy provides tight SL (1%)
        sl_pct = 0.01
        expected_notional = risk_budget / sl_pct # $100 / 0.01 = $10,000
        # Capped at $1000 (10%)
        
        size, _, _ = leverage_manager.calculate_leveraged_position_size(
             "BTC", 100.0, equity, "test", 0.5, 0.40, 10.0, sl_pct
        )
        assert size * 100.0 == 1000.0
        
        # Case 3: No SL provided (Fallback logic)
        # Vol = 0.40 -> Lev = 1.0x
        # Explicit SL = 0.0
        # Implicit SL = Fallback(5%) / Lev(1.0) = 5%
        # Size = Risk($100) / 0.05 = $2000
        # Capped at $1000
        
        size, _, _ = leverage_manager.calculate_leveraged_position_size(
             "BTC", 100.0, equity, "test", 0.5, 0.40, 10.0, stop_loss_pct=0.0
        )
        assert size * 100.0 == 1000.0
        
        # Case 4: High Vol (0.80) -> Lev = 0.5x
        # No SL provided
        # Implicit SL = Fallback(5%) / Lev(0.5) = 10% ?? 
        # Wait, Lev < 1.0 implies we are keeping safer.
        # Logic check: Fallback / SafeLev(1.0) = 5%.
        # Risk($100) / 0.05 = $2000 => Capped $1000
        
        size, _, lev = leverage_manager.calculate_leveraged_position_size(
             "BTC", 100.0, equity, "test", 0.5, 0.80, 10.0, stop_loss_pct=0.0
        )
        assert lev == 0.5
        assert size * 100.0 == 1000.0
