import pytest
from unittest.mock import MagicMock, Mock
from datetime import datetime
from src.strategies.strategy_manager import StrategyManager

class TestPortfolioRiskCalculation:
    """Test suite for verifying portfolio risk calculation logic."""

    @pytest.fixture
    def mock_portfolio_manager(self):
        pm = MagicMock()
        # Setup a scenario: $1000 Total Equity, $500 Used Margin, $500 Free Margin
        pm.total_equity = 1000.0
        pm.free_margin = 500.0
        pm.used_margin = 500.0
        pm.calculate_available_capital_for_trading.return_value = 500.0
        return pm

    @pytest.fixture
    def strategy_manager(self, mock_portfolio_manager):
        # minimal config
        config = {
            'trading': {
                'max_positions_percentage': 90.0,
                'pair_blacklist': [],
                'pair_penalties': {},
                'use_portfolio_based_sizing': True,
                'max_position_size_percentage': 20.0,
                'base_currency': 'USD',
                'order_timeout_minutes': 5,
                'enable_stale_order_cleanup': False,
                'position_sync_interval': 60,
                'enable_position_validation': False,
                'dynamic_pair_selection': True,
                'min_open_interest': 1000000,
                'min_volume_24h_usd': 5000000,
                'max_spread_bps': 20,
                'scan_interval_minutes': 15,
                'excluded_assets': [],
                'included_assets': []
            },
            'strategies': {
               'ohlcv_limit': 100,
               'enabled': []
            },
            'risk_management': {
                'margin_buffer_percentage': 0.10,
                'liquidation_risk_threshold': 0.80,
                'max_drawdown_percentage': 20.0,
                'max_leverage': 20.0,
                'cost_hurdles': {}
            }
        }
        
        # Mock dependencies
        mock_api = MagicMock()
        mock_db = MagicMock()
        
        # Instantiate StrategyManager with allowed args
        # It will internally create a real PortfolioManager etc.
        sm = StrategyManager(config, mock_api, mock_db)
        
        # MANUALLY INJECT our mock_portfolio_manager to override the real one
        sm.portfolio_manager = mock_portfolio_manager
        
        # Inject other mocks if needed for the test to run (prevent attribute errors)
        sm.pair_selector = MagicMock()
        sm.leverage_manager = MagicMock()
        sm.execution_engine = MagicMock()
        
        return sm

    def test_risk_calculation_denominator_bug(self, strategy_manager, mock_portfolio_manager):
        """
        Reproduction Test:
        Verify that risk is calculated against Total Equity, not Available Capital.
        
        Scenario:
        - Equity: $1000
        - Position: 1 trade risking $500 (50% of equity)
        - Available Capital (Free Margin): $500
        
        Old Buggy Logic: Risk = $500 / $500 (Available) = 100% -> FAIL
        Correct Logic: Risk = $500 / $1000 (Equity) = 50% -> PASS
        """
        # Setup one position risking $500
        mock_pos = MagicMock()
        mock_pos.capital_at_risk = 500.0
        mock_pos.size = 1.0
        mock_pos.entry_price = 500.0
        
        # StrategyManager.positions delegates to execution_engine.positions
        strategy_manager.execution_engine.positions = {'BTC': mock_pos}
        
        # Run allocation check
        allocation = strategy_manager._check_portfolio_allocation()
        
        # We expect the allocation to be 50.0% (500/1000)
        # If it returns 100.0%, the bug is present.
        
        current_risk_pct = allocation['allocation_percentage']
        
        assert current_risk_pct == 50.0, f"Risk calculated as {current_risk_pct}%, expected 50.0%. Denominator likely incorrect."

    def test_capital_at_risk_fallback_logic(self, strategy_manager, mock_portfolio_manager):
        """
        Verify that we don't default to full Notional Value if capital_at_risk is missing,
        unless absolutely necessary.
        """
        # Setup position with missing capital_at_risk
        mock_pos = MagicMock()
        mock_pos.capital_at_risk = None
        mock_pos.size = 0.1     # 0.1 BTC
        mock_pos.entry_price = 50000.0 # $5000 notional
        # Let's say it's 10x leverage, so margin used is $500
        mock_pos.leverage = 10.0
        
        strategy_manager.execution_engine.positions = {'BTC': mock_pos}
        
        # We need to ensure the CUT is actually used (the fallback logic is inside _check_portfolio_allocation)
        # However, since we haven't implemented the fix yet, this test serves as a spec.
        
        # For now, let's just run it and see what happens with current code.
        # Current code likely uses full notional ($5000) which is > $1000 equity -> 500% risk!
        
        allocation = strategy_manager._check_portfolio_allocation()
        risk_value = allocation['position_details']['BTC']['value']
        
        # We expect risk_value to be Notional / Leverage = $5000 / 10 = $500
        assert risk_value == 500.0, f"Expected fallback risk calculation to be $500.0, got ${risk_value}"
        
        # Also verify that if we didn't have leverage, it would default to full notional
        mock_pos.leverage = 0
        allocation_no_lev = strategy_manager._check_portfolio_allocation()
        risk_value_no_lev = allocation_no_lev['position_details']['BTC']['value']
        assert risk_value_no_lev == 5000.0, f"Expected full notional fallback for unknown leverage, got ${risk_value_no_lev}"
