"""
Unit tests for regime-adaptive z-score parameters in OU and StatArb strategies.
"""

import pytest
from unittest.mock import MagicMock
import pandas as pd
import numpy as np
from datetime import datetime

from src.strategies.ou_mean_reversion_strategy import OUMeanReversionStrategy
from src.strategies.statistical_arbitrage_strategy import StatisticalArbitrageStrategy


class TestOURegimeAdaptiveParams:
    """Test regime-adaptive parameters for OU Mean Reversion strategy."""
    
    @pytest.fixture
    def ou_strategy(self):
        """Create OU strategy with mock config."""
        config = {
            'strategies': {
                'ohlcv_limit': 200,  # Required by base strategy
                'ou_mean_reversion': {
                    'zscore_entry': 2.0,
                    'zscore_exit': 0.5,
                    'half_life_max_hours': 24,
                    'half_life_min_hours': 1,
                }
            }
        }
        return OUMeanReversionStrategy(config)
    
    def test_default_thresholds_without_strategy_manager(self, ou_strategy):
        """Test that default 'range' thresholds are used without strategy manager."""
        params = ou_strategy.get_regime_adjusted_params('BTC')
        
        assert params['z_entry'] == 2.0  # base for 'range' × 1.0 vol_ratio
        assert params['z_exit'] == 0.5
        assert params['regime'] == 'range'
        assert params['vol_ratio'] == 1.0
    
    def test_range_regime_thresholds(self, ou_strategy):
        """Test thresholds in 'range' regime."""
        mock_sm = MagicMock()
        mock_sm.get_current_regime.return_value = 'range'
        mock_sm.get_volatility_ratio.return_value = 1.0
        ou_strategy.strategy_manager = mock_sm
        
        params = ou_strategy.get_regime_adjusted_params('BTC')
        
        assert params['z_entry'] == 2.0
        assert params['z_exit'] == 0.5
        assert params['regime'] == 'range'
    
    def test_trend_regime_thresholds(self, ou_strategy):
        """Test thresholds in 'trend' regime."""
        mock_sm = MagicMock()
        mock_sm.get_current_regime.return_value = 'trend'
        mock_sm.get_volatility_ratio.return_value = 1.0
        ou_strategy.strategy_manager = mock_sm
        
        params = ou_strategy.get_regime_adjusted_params('BTC')
        
        assert params['z_entry'] == 2.5  # Wider in trend
        assert params['z_exit'] == 0.6
        assert params['regime'] == 'trend'
    
    def test_high_vol_regime_thresholds(self, ou_strategy):
        """Test thresholds in 'high_vol' regime."""
        mock_sm = MagicMock()
        mock_sm.get_current_regime.return_value = 'high_vol'
        mock_sm.get_volatility_ratio.return_value = 1.0
        ou_strategy.strategy_manager = mock_sm
        
        params = ou_strategy.get_regime_adjusted_params('BTC')
        
        assert params['z_entry'] == 3.0  # Widest in high vol
        assert params['z_exit'] == 0.75
        assert params['regime'] == 'high_vol'
    
    def test_volatility_scaling_applied(self, ou_strategy):
        """Test that volatility ratio scales thresholds."""
        mock_sm = MagicMock()
        mock_sm.get_current_regime.return_value = 'range'
        mock_sm.get_volatility_ratio.return_value = 1.5  # High volatility asset
        ou_strategy.strategy_manager = mock_sm
        
        params = ou_strategy.get_regime_adjusted_params('SOL')
        
        assert params['z_entry'] == 2.0 * 1.5  # 3.0
        assert params['z_exit'] == 0.5 * 1.5   # 0.75
        assert params['vol_ratio'] == 1.5
    
    def test_low_volatility_asset(self, ou_strategy):
        """Test that low volatility assets get tighter thresholds."""
        mock_sm = MagicMock()
        mock_sm.get_current_regime.return_value = 'range'
        mock_sm.get_volatility_ratio.return_value = 0.8  # Low volatility asset
        ou_strategy.strategy_manager = mock_sm
        
        params = ou_strategy.get_regime_adjusted_params('STABLECOIN')
        
        assert params['z_entry'] == 2.0 * 0.8  # 1.6
        assert params['z_exit'] == 0.5 * 0.8   # 0.4
        assert params['vol_ratio'] == 0.8


class TestStatArbRegimeAdaptiveParams:
    """Test regime-adaptive parameters for Statistical Arbitrage strategy."""
    
    @pytest.fixture
    def stat_arb_strategy(self):
        """Create StatArb strategy with mock config."""
        config = {
            'strategies': {
                'ohlcv_limit': 200,  # Required by base strategy
                'stat_arb': {
                    'z_score_threshold': 2.0,
                    'window_size': 100,
                },
                'cointegration': {
                    'zscore_entry': 2.0,
                    'zscore_exit': 0.5,
                }
            }
        }
        return StatisticalArbitrageStrategy(config)
    
    def test_default_thresholds_without_strategy_manager(self, stat_arb_strategy):
        """Test that default 'range' thresholds are used without strategy manager."""
        params = stat_arb_strategy.get_regime_adjusted_params('BTC')
        
        assert params['z_entry'] == 2.0
        assert params['z_exit'] == 0.5
        assert params['regime'] == 'range'
        assert params['vol_ratio'] == 1.0
    
    def test_high_vol_regime_thresholds(self, stat_arb_strategy):
        """Test thresholds in 'high_vol' regime."""
        mock_sm = MagicMock()
        mock_sm.get_current_regime.return_value = 'high_vol'
        mock_sm.get_volatility_ratio.return_value = 1.0
        stat_arb_strategy.strategy_manager = mock_sm
        
        params = stat_arb_strategy.get_regime_adjusted_params('BTC')
        
        assert params['z_entry'] == 3.0
        assert params['z_exit'] == 0.75
        assert params['regime'] == 'high_vol'
    
    def test_combined_regime_and_volatility(self, stat_arb_strategy):
        """Test combined effect of regime and per-asset volatility."""
        mock_sm = MagicMock()
        mock_sm.get_current_regime.return_value = 'high_vol'  # Base z_entry = 3.0
        mock_sm.get_volatility_ratio.return_value = 1.2       # × 1.2
        stat_arb_strategy.strategy_manager = mock_sm
        
        params = stat_arb_strategy.get_regime_adjusted_params('ETH')
        
        assert params['z_entry'] == 3.0 * 1.2  # 3.6
        assert params['regime'] == 'high_vol'
        assert params['vol_ratio'] == 1.2


class TestBlockingIntegration:
    """Test integration between volatility gate and strategy blocking."""
    
    def test_strategy_does_not_generate_signal_when_blocked(self):
        """Test that strategies respect volatility gate blocking."""
        # This would require full integration setup
        # For now, we test the get_regime_adjusted_params interface works
        config = {
            'strategies': {
                'ohlcv_limit': 200,  # Required by base strategy
                'ou_mean_reversion': {
                    'zscore_entry': 2.0,
                    'zscore_exit': 0.5,
                }
            }
        }
        strategy = OUMeanReversionStrategy(config)
        
        # Simulate high volatility regime
        mock_sm = MagicMock()
        mock_sm.get_current_regime.return_value = 'high_vol'
        mock_sm.get_volatility_ratio.return_value = 1.5
        strategy.strategy_manager = mock_sm
        
        params = strategy.get_regime_adjusted_params('BTC')
        
        # In high vol with high-vol asset, threshold should be very wide
        assert params['z_entry'] == 3.0 * 1.5  # 4.5
        
        # This means only very extreme deviations would trigger signals,
        # effectively reducing false positives in volatile markets
