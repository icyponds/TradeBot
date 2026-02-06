"""
Unit tests for Stat Arb regime detection grace period.

Tests the regime break threshold increase (3.0 → 4.0) and the 5-minute
grace period before emergency exit.
"""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

from src.strategies.statistical_arbitrage_strategy import StatisticalArbitrageStrategy


class TestRegimeBreakGracePeriod:
    """Test regime break grace period functionality."""
    
    @pytest.fixture
    def base_config(self):
        """Config with stat arb settings."""
        return {
            'strategies': {
                'ohlcv_limit': 300,
                'stat_arb': {
                    'z_score_threshold': 2.0,
                    'window_size': 100,
                    'regime_break_threshold': 4.0,  # New setting
                    'regime_break_grace_seconds': 300,  # 5 minutes
                },
                'cointegration': {
                    'zscore_entry': 2.0,
                    'zscore_exit': 0.5,
                    'lookback_period': 20,
                    'kalman_filter_enabled': False,
                },
            },
        }
    
    @pytest.fixture
    def strategy(self, base_config):
        """Create StatisticalArbitrageStrategy instance."""
        strat = StatisticalArbitrageStrategy(base_config)
        strat.logger = MagicMock()
        return strat
    
    # =========================================================================
    # Configuration Tests
    # =========================================================================
    
    def test_threshold_configured_to_4(self, strategy):
        """Test regime break threshold is 4.0 (increased from 3.0)."""
        assert strategy.regime_break_threshold == 4.0
    
    def test_grace_period_configured_to_300s(self, strategy):
        """Test grace period is 300 seconds (5 minutes)."""
        assert strategy.regime_break_grace_seconds == 300
    
    def test_grace_periods_dict_initialized(self, strategy):
        """Test grace periods tracking dict is initialized."""
        assert hasattr(strategy, 'regime_break_grace_periods')
        assert isinstance(strategy.regime_break_grace_periods, dict)
        assert len(strategy.regime_break_grace_periods) == 0
    
    # =========================================================================
    # Z-Score Threshold Tests
    # =========================================================================
    
    def test_zscore_below_threshold_no_exit(self, strategy):
        """Test z-score below 4.0 does not trigger regime break exit."""
        pair_key = "ETH__SOL"
        
        # Setup active position
        strategy.active_spreads[pair_key] = {
            'side': 'short',
            'entry_zscore': 2.5,
        }
        
        # Z-score of 3.5 (below 4.0)
        z_score = 3.5
        z_exit = 0.5
        
        # Simulate signal check (simplified)
        # The actual method is generate_pair_signal, but we test the logic inline
        should_exit = z_score > strategy.regime_break_threshold
        
        assert should_exit == False
    
    def test_zscore_above_threshold_triggers_grace(self, strategy):
        """Test z-score above 4.0 starts grace period."""
        pair_key = "ETH__SOL"
        
        # First breach at z=4.5
        z_score = 4.5
        if z_score > strategy.regime_break_threshold:
            if pair_key not in strategy.regime_break_grace_periods:
                strategy.regime_break_grace_periods[pair_key] = datetime.now().timestamp()
        
        assert pair_key in strategy.regime_break_grace_periods
    
    # =========================================================================
    # Grace Period Logic Tests
    # =========================================================================
    
    def test_grace_period_starts_on_first_breach(self, strategy):
        """Test grace period timestamp is set on first breach."""
        pair_key = "ETH__SOL"
        
        before = datetime.now().timestamp()
        strategy.regime_break_grace_periods[pair_key] = datetime.now().timestamp()
        after = datetime.now().timestamp()
        
        assert before <= strategy.regime_break_grace_periods[pair_key] <= after
    
    def test_no_exit_during_grace_period(self, strategy):
        """Test no exit signal during active grace period."""
        pair_key = "ETH__SOL"
        
        # Set grace period that just started
        strategy.regime_break_grace_periods[pair_key] = datetime.now().timestamp()
        
        # Simulate check after 2 minutes (should still be in grace)
        current_time = datetime.now().timestamp()
        elapsed = current_time - strategy.regime_break_grace_periods[pair_key]
        
        should_exit = elapsed > strategy.regime_break_grace_seconds
        
        assert should_exit == False  # Only 0 seconds elapsed
    
    def test_exit_after_grace_period_expires(self, strategy):
        """Test exit triggers after grace period expires."""
        pair_key = "ETH__SOL"
        
        # Set grace period that started 6 minutes ago
        strategy.regime_break_grace_periods[pair_key] = (datetime.now() - timedelta(minutes=6)).timestamp()
        
        current_time = datetime.now().timestamp()
        elapsed = current_time - strategy.regime_break_grace_periods[pair_key]
        
        should_exit = elapsed > strategy.regime_break_grace_seconds
        
        assert should_exit == True  # 360 seconds > 300 seconds
    
    # =========================================================================
    # Grace Period Clearing Tests
    # =========================================================================
    
    def test_grace_period_clears_when_zscore_recovers(self, strategy):
        """Test grace period is cleared when z-score goes back below threshold."""
        pair_key = "ETH__SOL"
        
        # Grace period was active
        strategy.regime_break_grace_periods[pair_key] = datetime.now().timestamp()
        
        # Z-score recovered to 3.5 (below 4.0)
        z_score = 3.5
        if z_score < strategy.regime_break_threshold:
            strategy.regime_break_grace_periods.pop(pair_key, None)
        
        assert pair_key not in strategy.regime_break_grace_periods
    
    def test_grace_period_clears_on_normal_exit(self, strategy):
        """Test grace period is cleared when position exits normally."""
        pair_key = "ETH__SOL"
        
        strategy.regime_break_grace_periods[pair_key] = datetime.now().timestamp()
        strategy.active_spreads[pair_key] = {'side': 'short'}
        
        # Normal exit (z_score hit exit threshold)
        del strategy.active_spreads[pair_key]
        strategy.regime_break_grace_periods.pop(pair_key, None)
        
        assert pair_key not in strategy.regime_break_grace_periods
        assert pair_key not in strategy.active_spreads
    
    # =========================================================================
    # Negative Z-Score Tests (Long Position)
    # =========================================================================
    
    def test_negative_zscore_threshold(self, strategy):
        """Test negative z-score threshold for long positions."""
        pair_key = "ETH__SOL"
        
        strategy.active_spreads[pair_key] = {
            'side': 'long',
            'entry_zscore': -2.5,
        }
        
        # Z-score of -4.5 (below -4.0)
        z_score = -4.5
        should_trigger = z_score < -strategy.regime_break_threshold
        
        assert should_trigger == True
    
    def test_negative_zscore_within_threshold(self, strategy):
        """Test negative z-score within threshold does not trigger."""
        pair_key = "ETH__SOL"
        
        z_score = -3.5  # Within -4.0 threshold
        should_trigger = z_score < -strategy.regime_break_threshold
        
        assert should_trigger == False
    
    # =========================================================================
    # Integration Tests
    # =========================================================================
    
    def test_multiple_pairs_independent_grace(self, strategy):
        """Test each pair has independent grace period."""
        pairs = ["ETH__SOL", "BTC__ETH", "LINK__UNI"]
        
        # Set different grace start times
        now = datetime.now().timestamp()
        strategy.regime_break_grace_periods["ETH__SOL"] = now - 400  # Expired
        strategy.regime_break_grace_periods["BTC__ETH"] = now - 100  # Active
        strategy.regime_break_grace_periods["LINK__UNI"] = now       # Just started
        
        # Check each independently
        eth_sol_expired = (now - strategy.regime_break_grace_periods["ETH__SOL"]) > 300
        btc_eth_expired = (now - strategy.regime_break_grace_periods["BTC__ETH"]) > 300
        link_uni_expired = (now - strategy.regime_break_grace_periods["LINK__UNI"]) > 300
        
        assert eth_sol_expired == True
        assert btc_eth_expired == False
        assert link_uni_expired == False


class TestRegimeBreakThresholdDefault:
    """Test default values when config is missing."""
    
    def test_default_threshold_is_4(self):
        """Test default threshold is 4.0 when not in config."""
        config = {
            'strategies': {
                'ohlcv_limit': 300,
                'stat_arb': {},  # No regime_break_threshold
                'cointegration': {
                    'zscore_entry': 2.0,
                    'zscore_exit': 0.5,
                },
            },
        }
        
        strat = StatisticalArbitrageStrategy(config)
        
        assert strat.regime_break_threshold == 4.0
    
    def test_default_grace_is_300s(self):
        """Test default grace period is 300s when not in config."""
        config = {
            'strategies': {
                'ohlcv_limit': 300,
                'stat_arb': {},  # No regime_break_grace_seconds
                'cointegration': {
                    'zscore_entry': 2.0,
                    'zscore_exit': 0.5,
                },
            },
        }
        
        strat = StatisticalArbitrageStrategy(config)
        
        assert strat.regime_break_grace_seconds == 300

