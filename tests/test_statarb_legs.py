
import pytest
import pandas as pd
import numpy as np
from unittest.mock import MagicMock, patch
from src.strategies.statistical_arbitrage_strategy import StatisticalArbitrageStrategy

class TestStatArbLegs:
    
    @pytest.fixture
    def strategy(self):
        config = {
            'strategies': {
                'ohlcv_limit': 100,
                'stat_arb': {
                    'z_score_threshold': 2.0,
                    'window_size': 20,
                    'update_interval_hours': 24
                },
                'cointegration': {}
            }
        }
        strat = StatisticalArbitrageStrategy(config)
        return strat

    def test_entry_legs_long_spread(self, strategy):
        """Test legs generation for entering a long spread position (Long A, Short B)."""
        symbol_a = 'BTC'
        symbol_b = 'ETH'
        hedge_ratio = 0.5
        z_score = -3.0 # Triggers Buy (Long Spread)
        
        # Mock internal calculations
        strategy._get_hedge_ratio = MagicMock(return_value=hedge_ratio)
        strategy._calculate_spread_zscore = MagicMock(return_value=z_score)
        
        # Patch external dependencies
        with patch('src.strategies.statistical_arbitrage_strategy.hurst_exponent', return_value=0.4):
            # Generate signals
            ohlcv_a = pd.DataFrame({'close': [50000.0] * 100})
            ohlcv_b = pd.DataFrame({'close': [2000.0] * 100})
            
            signal = strategy.generate_pair_signal(symbol_a, ohlcv_a, symbol_b, ohlcv_b)
        
        assert signal is not None
        assert signal['signal'] == 'buy'
        assert signal['signal_type'] == 'multi_leg'
        assert signal['atomic'] is True
        
        legs = signal['legs']
        assert len(legs) == 2
        
        # Check Leg A (Long)
        leg_a = legs[0]
        assert leg_a['symbol'] == symbol_a
        assert leg_a['side'] == 'long'
        assert leg_a['hedge_ratio'] == 1.0
        
        # Check Leg B (Short)
        leg_b = legs[1]
        assert leg_b['symbol'] == symbol_b
        assert leg_b['side'] == 'short'
        assert leg_b['hedge_ratio'] == hedge_ratio

    def test_entry_legs_short_spread(self, strategy):
        """Test legs generation for entering a short spread position (Short A, Long B)."""
        symbol_a = 'BTC'
        symbol_b = 'ETH'
        hedge_ratio = 0.8
        z_score = 3.0 # Triggers Sell (Short Spread)
        
        strategy._get_hedge_ratio = MagicMock(return_value=hedge_ratio)
        strategy._calculate_spread_zscore = MagicMock(return_value=z_score)
        
        with patch('src.strategies.statistical_arbitrage_strategy.hurst_exponent', return_value=0.4):
            ohlcv_a = pd.DataFrame({'close': [50000.0] * 100})
            ohlcv_b = pd.DataFrame({'close': [2000.0] * 100})
            
            signal = strategy.generate_pair_signal(symbol_a, ohlcv_a, symbol_b, ohlcv_b)
        
        assert signal['signal'] == 'sell'
        legs = signal['legs']
        
        # Check Leg A (Short)
        leg_a = legs[0]
        assert leg_a['symbol'] == symbol_a
        assert leg_a['side'] == 'short'
        assert leg_a['hedge_ratio'] == 1.0
        
        # Check Leg B (Long)
        leg_b = legs[1]
        assert leg_b['symbol'] == symbol_b
        assert leg_b['side'] == 'long'
        assert leg_b['hedge_ratio'] == hedge_ratio

    def test_exit_legs(self, strategy):
        """Test legs generation for exiting a position."""
        symbol_a = 'BTC'
        symbol_b = 'ETH'
        hedge_ratio = 0.5
        z_score = 0.0 # Exit trigger
        
        # Setup existing position
        pair_key = f"{symbol_a}/{symbol_b}" # Use slash matching strategy code
        strategy.active_spreads = {
            pair_key: {
                'side': 'long',
                'entry_zscore': -3.0,
                'hedge_ratio': hedge_ratio,
                'hurst': 0.4
            }
        }
        
        strategy._get_hedge_ratio = MagicMock(return_value=hedge_ratio)
        strategy._calculate_spread_zscore = MagicMock(return_value=z_score)
        
        # Hurst check not done on exit, so no patch needed? 
        # But let's patch just in case it's called somewhere.
        with patch('src.strategies.statistical_arbitrage_strategy.hurst_exponent', return_value=0.4):
            ohlcv_a = pd.DataFrame({'close': [50000.0] * 100})
            ohlcv_b = pd.DataFrame({'close': [2000.0] * 100})
            
            signal = strategy.generate_pair_signal(symbol_a, ohlcv_a, symbol_b, ohlcv_b)
        
        assert signal['signal'] == 'sell' # Close Long means Sell signal
        legs = signal['legs']
        
        # Both legs should be closing
        assert legs[0]['side'] == 'close'
        assert legs[0]['reduce_only'] is True
        
        assert legs[1]['side'] == 'close'
        assert legs[1]['reduce_only'] is True
        assert legs[1]['hedge_ratio'] == hedge_ratio
