import pytest
from unittest.mock import MagicMock
import pandas as pd
import numpy as np
from datetime import datetime
from src.strategies.cross_sectional_momentum_strategy import CrossSectionalMomentumStrategy

class TestCrossSectionalMomentumStrategy:
    
    @pytest.fixture
    def strategy(self, mock_config):
        """Create CSM strategy instance."""
        # Ensure fresh state
        CrossSectionalMomentumStrategy._universe_stats = {}
        
        # Add specific config
        mock_config['strategies']['cross_sectional_momentum'] = {
            'lookback_period': 24,
            'top_n_percent': 0.2, # 20% for easier testing with few assets
            'bottom_n_percent': 0.2,
            'rebalance_interval': 4
        }
        
        return CrossSectionalMomentumStrategy(mock_config, timeframe='1h')
    
    def create_ohlcv(self, price_start, price_end, length=30):
        """Helper to create OHLCV dataframe."""
        prices = np.linspace(price_start, price_end, length)
        df = pd.DataFrame({
            'open': prices,
            'high': prices,
            'low': prices,
            'close': prices,
            'volume': [1000] * length
        })
        time_idx = pd.date_range("2024-01-01", periods=length, freq='1h')
        df.index = time_idx # Although strategy might not use index directly if implicit
        return df

    def test_ranking_logic(self, strategy):
        """Test that assets are ranked correctly."""
        # Need 5 assets for ranking to trigger (hardcoded < 5 check)
        
        # Asset A: +10% (Winner)
        df_a = self.create_ohlcv(100, 110, 30)
        # Asset B: +5%
        df_b = self.create_ohlcv(100, 105, 30)
        # Asset C: 0%
        df_c = self.create_ohlcv(100, 100, 30)
        # Asset D: -5%
        df_d = self.create_ohlcv(100, 95, 30)
        # Asset E: -10% (Loser)
        df_e = self.create_ohlcv(100, 90, 30)
        
        assets = {
            'A': df_a, 'B': df_b, 'C': df_c, 'D': df_d, 'E': df_e
        }
        
        # 1. Populate cache (call internal method or generate_signal to prime it)
        for sym, df in assets.items():
            strategy.generate_signal(sym, {'1h': df})
            
        # 2. Check signals
        # A should be Top 20% of 5 assets (Rank 1/5 = 0.2? No 0-indexed sorted)
        # Returns: [-0.1, -0.05, 0, 0.05, 0.10]
        # A is 0.10. sorted index 4. Rank = 4/5 = 0.8. 
        # (This logic depends on implementation details I read: idx/len or explicit rank)
        # The code implementation: idx / len(sorted_returns)
        # A: idx 4 / 5 = 0.8. >= (1 - 0.2 = 0.8). BUY signal.
        
        sig_a = strategy.generate_signal('A', {'1h': df_a})
        assert sig_a['signal'] == 'buy'
        
        # E: idx 0 / 5 = 0.0. <= 0.2. SELL signal.
        sig_e = strategy.generate_signal('E', {'1h': df_e})
        assert sig_e['signal'] == 'sell'
        
        # C: idx 2 / 5 = 0.4. Hold.
        sig_c = strategy.generate_signal('C', {'1h': df_c})
        assert sig_c is None

    def test_trend_filter(self, strategy):
        """Test EMA trend filter."""
        # Need to start with 5 assets to enable ranking
        # Create 4 dummy fillers
        for s in ['1','2','3','4']:
            CrossSectionalMomentumStrategy._universe_stats[s] = {
                'return': 0.0, 'timestamp': datetime.now(), 'volatility': 0.01
            }
            
        # Target Asset: Winner (+20%) but below EMA (downtrend context)
        # To simulate "Price < EMA", we need a long history of high prices dropping recently
        # But still having positive momentum over lookback? 
        # Actually logic is: Momentum over 24h.
        # Trend EMA is 200 period.
        
        # Scenario: Price dropped from 200 to 100 (EMA ~150), then rallied to 110.
        # Price 110 < EMA 150.
        # Momentum (100->110) is positive +10%.
        
        prices = [200.0] * 200 + [100.0] * 20 + [110.0] # 221 points
        df = pd.DataFrame({'close': prices, 'open': prices, 'high': prices, 'low': prices, 'volume': [1]*221})
        
        # Generate signal
        # With +10% return (100->110 if lookback is 24?), wait lookback is 24.
        # last 24 candles: 
        # -24 was 100 (from the [100]*20 block? wait 20+1 = 21. )
        # let's make it simpler.
        
        # We manually patch the trend check if data creation is hard
        # But data creation isn't too hard.
        # Just creating a DF where close[-1] < ema[-1]
        
        # Use simple decreasing series for EMA, but sharp spike for momentum
        p = np.linspace(200, 100, 300) # Downtrend
        p[-1] = 110 # Spike up
        p[-25] = 100 # Reference for momentum (approx)
        
        df = pd.DataFrame({'close': p, 'open': p, 'high': p, 'low': p, 'volume': [1]*300})
        
        sig = strategy.generate_signal('Target', {'1h': df})
        
        # Should be a winner (stats return unknown vs fillers, but likely high since fillers are 0)
        # But filter should block it
        assert sig is None # Filtered

    def test_insufficient_universe(self, strategy):
        """Test that nothing happens with too few assets."""
        df = self.create_ohlcv(100, 110, 30)
        sig = strategy.generate_signal('A', {'1h': df})
        assert sig is None
