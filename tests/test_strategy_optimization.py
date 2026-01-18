
import pytest
import pandas as pd
import numpy as np
from unittest.mock import MagicMock, patch
from src.strategies.statistical_arbitrage_strategy import StatisticalArbitrageStrategy
from src.strategies.ou_mean_reversion_strategy import OUMeanReversionStrategy
from src.strategies.liquidation_hunter_strategy import LiquidationHunterStrategy
from src.strategies.strategy_manager import StrategyManager

@pytest.fixture
def mock_config():
    return {
        'strategies': {
            'enabled': ['stat_arb', 'ou_mean_reversion', 'liquidation_hunter'],
            'stat_arb': {'window_size': 100, 'max_adverse_z_delta': 1.0},
            'ou_mean_reversion': {'min_data_points': 50},
            'liquidation_hunter': {'std_dev_threshold': 3.5},
            'ohlcv_limit': 100
        },
        'trading': {
            'max_positions_percentage': 1.0, 
            'base_currency': 'USD',
            'order_timeout_minutes': 5,
            'enable_stale_order_cleanup': True,
            'position_sync_interval': 60,
            'enable_position_validation': True,
            'use_portfolio_based_sizing': False,
            'max_position_size_percentage': 0.1,
            'dynamic_pair_selection': True,
            'min_open_interest': 100000,
            'scan_interval_minutes': 15,
            'excluded_assets': ['USDC', 'USDT'],
            'included_assets': []
        },
        'risk_management': {
            'margin_buffer_percentage': 0.05,
            'liquidation_risk_threshold': 0.8
        },
        'hip3': {'enabled': False},
        'spot': {'enabled': False},
        'pair_selection': {'mode': 'sophisticated'}
    }

def create_mock_ohlcv(length=100, trend=False):
    """Create OHLCV data. If trend=True, creating a strong uptrend."""
    base_price = 100.0
    prices = []
    for i in range(length):
        if trend:
            # Strong trend (ADX will be high)
            base_price += 1.0 + np.random.normal(0, 0.1)
        else:
            # Mean reverting
            base_price += np.random.normal(0, 1.0)
        prices.append(base_price)
    
    df = pd.DataFrame({
        'open': prices,
        'high': [p + 0.5 for p in prices],
        'low': [p - 0.5 for p in prices],
        'close': prices,
        'volume': [1000] * length
    })
    return df

class TestStrategyOptimizations:

    def test_stat_arb_tight_stop(self, mock_config):
        """Test that Stat Arb triggers stop loss at 3.0 sigma (Hard Stop)."""
        strategy = StatisticalArbitrageStrategy(mock_config)
        strategy.active_spreads = {
            'A/B': {
                'side': 'short',
                'entry_zscore': 2.0,
                'hedge_ratio': 1.0,
                'current_z': 2.0
            }
        }
        
        strategy._calculate_spread_zscore = MagicMock(return_value=3.1)
        strategy._get_hedge_ratio = MagicMock(return_value=(1.0, 1.0))
        
        df_a = create_mock_ohlcv()
        df_b = create_mock_ohlcv()
        
        res = strategy.generate_pair_signal('A', df_a, 'B', df_b)
        
        # Expect a 'buy' signal (closing short) with reason "Regime Break ... > 3.0"
        assert res is not None
        assert res['signal'] == 'buy' # Close short
        assert "Regime Break" in res['reason']
        assert "3.0" in res['reason'] # Verify updated threshold

    def test_ou_adx_filter(self, mock_config):
        """Test that OU Mean Reversion rejects signals when ADX > 30."""
        strategy = OUMeanReversionStrategy(mock_config)
        
        # Create trending data (ADX should be high)
        trending_ohlcv = create_mock_ohlcv(length=100, trend=True)
        
        # Mock valid OU params so it doesn't fail on estimation
        mock_params = MagicMock()
        mock_params.theta = 0.5
        mock_params.mu = 150.0 
        mock_params.sigma = 1.0
        mock_params.half_life = 10.0
        strategy._get_ou_parameters = MagicMock(return_value=mock_params)
        strategy._is_tradeable = MagicMock(return_value=True)
        
        res = strategy._generate_signal_internal(trending_ohlcv, 'BTC')
        
        # Should be None because ADX > 30 (strong trend)
        assert res is None

    def test_liq_hunter_signal_boost(self, mock_config):
        """Test signal strength boost for extreme Z-scores."""
        strategy = LiquidationHunterStrategy(mock_config)
        
        # Z = 3.6 (Normal Entry) -> Strength ~0.5
        s1 = strategy.calculate_signal_strength({}, signal_context={'z_score': 3.6})
        assert 0.5 <= s1 <= 0.6
        
        # Z = 4.6 (Extreme) -> Strength Boosted
        s2 = strategy.calculate_signal_strength({}, signal_context={'z_score': 4.6})
        assert s2 == 1.0

    def test_efficiency_filter(self, mock_config):
        """Test Strategy Manager's Dynamic Efficiency Filter (Hurst)."""
        mock_strat = MagicMock()
        signal_dict = {
            'action': 'open',
            'zscore': 2.5 # Valid standard signal
        }
        # Configure both methods to ensure signal is returned regardless of call path
        mock_strat.generate_signal.return_value = signal_dict
        mock_strat.generate_signal_for_symbol.return_value = signal_dict
    
        # Explicit mocks to prevent config failures
        mock_api = MagicMock()
        mock_tracker = MagicMock()
    
        manager = StrategyManager(mock_config, market_api=mock_api, performance_tracker=mock_tracker)
        manager.strategy_selector = MagicMock()
        manager.strategy_selector.is_strategy_enabled.return_value = True
    
        trending_df = create_mock_ohlcv(length=100, trend=True)
        ohlcv_dict = {'15m': trending_df}
    
        # Patch hurst_exponent to force strict efficiency filtering (Hurst > 0.6)
        with patch('src.strategies.strategy_manager.hurst_exponent', return_value=0.8):
            
            # Case 1: Hurst=0.8 + Z=2.5 (< 3.0) -> Should return None (Rejected)
            res = manager._generate_signal_for_strategy(
                'BTC', 'ou_mean_reversion', mock_strat, ohlcv_dict, 100.0
            )
            assert res is None
            
            # Case 2: Broaden threshold check: Hurst=0.8 + Z=3.1 (> 3.0) -> Should Pass
            signal_dict_2 = {
                'action': 'open',
                'zscore': 3.1
            }
            mock_strat.generate_signal.return_value = signal_dict_2
            mock_strat.generate_signal_for_symbol.return_value = signal_dict_2
            
            res2 = manager._generate_signal_for_strategy(
                'BTC', 'ou_mean_reversion', mock_strat, ohlcv_dict, 100.0
            )
            assert res2 is not None
