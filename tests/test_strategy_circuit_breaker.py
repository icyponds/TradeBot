"""
Unit tests for StrategyCircuitBreaker module.

Tests the auto-disable functionality for strategies exceeding daily loss limits.
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from src.utils.strategy_circuit_breaker import StrategyCircuitBreaker


class TestStrategyCircuitBreaker:
    """Test suite for StrategyCircuitBreaker."""
    
    @pytest.fixture
    def default_config(self):
        """Default config with $50 daily loss limit."""
        return {
            'risk_management': {
                'strategy_daily_loss_limit': 50.0,
                'strategy_reset_hour': 0,  # Reset at UTC midnight
            }
        }
    
    @pytest.fixture
    def circuit_breaker(self, default_config):
        """Create circuit breaker instance."""
        return StrategyCircuitBreaker(default_config)
    
    # =========================================================================
    # Basic Functionality Tests
    # =========================================================================
    
    def test_initialization(self, circuit_breaker):
        """Test circuit breaker initializes correctly."""
        assert circuit_breaker.daily_loss_limit == 50.0
        assert circuit_breaker.reset_hour == 0
        assert len(circuit_breaker.disabled_strategies) == 0
        assert len(circuit_breaker.strategy_pnl_today) == 0
    
    def test_record_winning_trade(self, circuit_breaker):
        """Test recording winning trades doesn't trigger disable."""
        is_disabled = circuit_breaker.record_trade("stat_arb_4h", pnl=10.0)
        
        assert is_disabled == False
        assert circuit_breaker.strategy_pnl_today["stat_arb_4h"] == 10.0
        assert "stat_arb_4h" not in circuit_breaker.disabled_strategies
    
    def test_record_small_losing_trade(self, circuit_breaker):
        """Test small losses don't trigger disable."""
        is_disabled = circuit_breaker.record_trade("stat_arb_4h", pnl=-10.0)
        
        assert is_disabled == False
        assert circuit_breaker.strategy_pnl_today["stat_arb_4h"] == -10.0
        assert "stat_arb_4h" not in circuit_breaker.disabled_strategies
    
    # =========================================================================
    # Threshold Tests
    # =========================================================================
    
    def test_disable_at_threshold(self, circuit_breaker):
        """Test strategy is disabled exactly at loss threshold."""
        # Accumulate losses to hit threshold
        circuit_breaker.record_trade("stat_arb_4h", pnl=-25.0)
        circuit_breaker.record_trade("stat_arb_4h", pnl=-20.0)
        is_disabled = circuit_breaker.record_trade("stat_arb_4h", pnl=-6.0)  # Total: -$51
        
        assert is_disabled == True
        assert "stat_arb_4h" in circuit_breaker.disabled_strategies
        assert circuit_breaker.strategy_pnl_today["stat_arb_4h"] == -51.0
    
    def test_disable_not_triggered_at_exactly_limit(self, circuit_breaker):
        """Test strategy NOT disabled at exactly -$50 (needs to exceed)."""
        is_disabled = circuit_breaker.record_trade("stat_arb_4h", pnl=-50.0)
        
        assert is_disabled == False  # -50 is AT limit, not EXCEEDING
        assert "stat_arb_4h" not in circuit_breaker.disabled_strategies
    
    def test_disable_single_large_loss(self, circuit_breaker):
        """Test single large loss triggers disable."""
        is_disabled = circuit_breaker.record_trade("vol_breakout_4h", pnl=-60.0)
        
        assert is_disabled == True
        assert "vol_breakout_4h" in circuit_breaker.disabled_strategies
    
    def test_wins_offset_losses(self, circuit_breaker):
        """Test winning trades offset losing trades."""
        circuit_breaker.record_trade("stat_arb_4h", pnl=-40.0)
        circuit_breaker.record_trade("stat_arb_4h", pnl=20.0)  # Net: -$20
        is_disabled = circuit_breaker.record_trade("stat_arb_4h", pnl=-20.0)  # Net: -$40
        
        assert is_disabled == False
        assert circuit_breaker.strategy_pnl_today["stat_arb_4h"] == -40.0
    
    # =========================================================================
    # Multiple Strategy Tests
    # =========================================================================
    
    def test_independent_strategy_tracking(self, circuit_breaker):
        """Test each strategy is tracked independently."""
        circuit_breaker.record_trade("stat_arb_4h", pnl=-55.0)  # Disabled
        is_disabled_other = circuit_breaker.record_trade("vol_breakout_4h", pnl=-10.0)
        
        assert "stat_arb_4h" in circuit_breaker.disabled_strategies
        assert "vol_breakout_4h" not in circuit_breaker.disabled_strategies
        assert is_disabled_other == False
    
    def test_multiple_strategies_disabled(self, circuit_breaker):
        """Test multiple strategies can be disabled simultaneously."""
        circuit_breaker.record_trade("stat_arb_4h", pnl=-55.0)
        circuit_breaker.record_trade("vol_breakout_4h", pnl=-60.0)
        circuit_breaker.record_trade("ou_mean_reversion_4h", pnl=-51.0)
        
        assert len(circuit_breaker.disabled_strategies) == 3
    
    # =========================================================================
    # is_disabled() Tests
    # =========================================================================
    
    def test_is_disabled_returns_true_for_disabled(self, circuit_breaker):
        """Test is_disabled returns True for disabled strategy."""
        circuit_breaker.record_trade("stat_arb_4h", pnl=-55.0)
        
        assert circuit_breaker.is_disabled("stat_arb_4h") == True
    
    def test_is_disabled_returns_false_for_active(self, circuit_breaker):
        """Test is_disabled returns False for active strategy."""
        circuit_breaker.record_trade("stat_arb_4h", pnl=-10.0)
        
        assert circuit_breaker.is_disabled("stat_arb_4h") == False
    
    def test_is_disabled_returns_false_for_unknown(self, circuit_breaker):
        """Test is_disabled returns False for unknown strategy."""
        assert circuit_breaker.is_disabled("unknown_strategy") == False
    
    # =========================================================================
    # Daily Reset Tests
    # =========================================================================
    
    def test_daily_reset_clears_disabled(self, circuit_breaker):
        """Test daily reset re-enables disabled strategies."""
        # Disable a strategy
        circuit_breaker.record_trade("stat_arb_4h", pnl=-55.0)
        assert "stat_arb_4h" in circuit_breaker.disabled_strategies
        
        # Simulate next day trade
        tomorrow = datetime.now() + timedelta(days=1)
        circuit_breaker.record_trade("stat_arb_4h", pnl=5.0, timestamp=tomorrow)
        
        assert "stat_arb_4h" not in circuit_breaker.disabled_strategies
        assert circuit_breaker.strategy_pnl_today["stat_arb_4h"] == 5.0
    
    def test_daily_reset_clears_pnl(self, circuit_breaker):
        """Test daily reset clears accumulated PnL."""
        circuit_breaker.record_trade("stat_arb_4h", pnl=-30.0)
        
        # Simulate next day
        tomorrow = datetime.now() + timedelta(days=1)
        circuit_breaker.record_trade("stat_arb_4h", pnl=-30.0, timestamp=tomorrow)
        
        # Should be at -$30 for new day, not -$60 cumulative
        assert circuit_breaker.strategy_pnl_today["stat_arb_4h"] == -30.0
        assert "stat_arb_4h" not in circuit_breaker.disabled_strategies
    
    # =========================================================================
    # Force Enable/Disable Tests
    # =========================================================================
    
    def test_force_disable(self, circuit_breaker):
        """Test manual force disable."""
        circuit_breaker.force_disable("stat_arb_4h", reason="Manual override")
        
        assert "stat_arb_4h" in circuit_breaker.disabled_strategies
    
    def test_force_enable(self, circuit_breaker):
        """Test manual force enable."""
        circuit_breaker.record_trade("stat_arb_4h", pnl=-55.0)  # Auto-disabled
        circuit_breaker.force_enable("stat_arb_4h")
        
        assert "stat_arb_4h" not in circuit_breaker.disabled_strategies
    
    def test_force_enable_on_non_disabled(self, circuit_breaker):
        """Test force enable on non-disabled strategy is no-op."""
        circuit_breaker.force_enable("stat_arb_4h")  # Never disabled
        
        assert "stat_arb_4h" not in circuit_breaker.disabled_strategies
    
    # =========================================================================
    # Status Summary Tests
    # =========================================================================
    
    def test_status_summary_structure(self, circuit_breaker):
        """Test status summary has correct structure."""
        circuit_breaker.record_trade("stat_arb_4h", pnl=-30.0)
        circuit_breaker.record_trade("vol_breakout_4h", pnl=10.0)
        
        summary = circuit_breaker.get_status_summary()
        
        assert 'daily_loss_limit' in summary
        assert 'reset_hour_utc' in summary
        assert 'strategies' in summary
        assert 'stat_arb_4h' in summary['strategies']
        assert 'vol_breakout_4h' in summary['strategies']
    
    def test_status_summary_values(self, circuit_breaker):
        """Test status summary has correct values."""
        circuit_breaker.record_trade("stat_arb_4h", pnl=-30.0)
        circuit_breaker.record_trade("stat_arb_4h", pnl=-10.0)
        
        summary = circuit_breaker.get_status_summary()
        strategy_status = summary['strategies']['stat_arb_4h']
        
        assert strategy_status['pnl_today'] == -40.0
        assert strategy_status['trades_today'] == 2
        assert strategy_status['disabled'] == False
        assert strategy_status['loss_utilization_pct'] == 80.0  # 40/50 * 100
    
    # =========================================================================
    # Edge Cases
    # =========================================================================
    
    def test_record_trade_returns_true_if_already_disabled(self, circuit_breaker):
        """Test recording trade for already disabled strategy returns True."""
        circuit_breaker.record_trade("stat_arb_4h", pnl=-55.0)  # Disabled
        is_disabled = circuit_breaker.record_trade("stat_arb_4h", pnl=-5.0)
        
        assert is_disabled == True
        # PnL should NOT be updated for disabled strategy
        assert circuit_breaker.strategy_pnl_today["stat_arb_4h"] == -55.0
    
    def test_zero_pnl_trade(self, circuit_breaker):
        """Test zero PnL trade is handled."""
        is_disabled = circuit_breaker.record_trade("stat_arb_4h", pnl=0.0)
        
        assert is_disabled == False
        assert circuit_breaker.strategy_pnl_today["stat_arb_4h"] == 0.0
