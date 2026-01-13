
import pytest
import pandas as pd
from unittest.mock import MagicMock, patch
from src.strategies.statistical_arbitrage_strategy import StatisticalArbitrageStrategy

class TestStatArbMaxAdverse:
    
    @pytest.fixture
    def strategy(self):
        config = {
            'strategies': {
                'ohlcv_limit': 100,
                'stat_arb': {
                    'z_score_threshold': 2.0,
                    'max_adverse_z_delta': 1.5,
                    'window_size': 20
                },
                'cointegration': {}
            }
        }
        strat = StatisticalArbitrageStrategy(config)
        return strat

    def test_entry_initializes_metadata(self, strategy):
        """Test that entry initializes max_adverse_z and current_z metadata."""
        symbol_a = 'BTC'
        symbol_b = 'ETH'
        hedge_ratio = 0.5
        z_score = -2.5 # Entry (Long Spread)
        
        strategy._get_hedge_ratio = MagicMock(return_value=hedge_ratio)
        strategy._calculate_spread_zscore = MagicMock(return_value=z_score)
        
        with patch('src.strategies.statistical_arbitrage_strategy.hurst_exponent', return_value=0.4):
            ohlcv_a = pd.DataFrame({'close': [50000.0] * 100})
            ohlcv_b = pd.DataFrame({'close': [2000.0] * 100})
            
            strategy.generate_pair_signal(symbol_a, ohlcv_a, symbol_b, ohlcv_b)
            
        pair_key = f"{symbol_a}/{symbol_b}"
        assert pair_key in strategy.active_spreads
        data = strategy.active_spreads[pair_key]
        
        assert data['entry_zscore'] == z_score
        assert data['current_z'] == z_score
        assert data['max_adverse_z'] == z_score

    def test_holding_updates_metadata(self, strategy):
        """Test that holding updates max_adverse_z correctly."""
        symbol_a = 'BTC'
        symbol_b = 'ETH'
        pair_key = f"{symbol_a}/{symbol_b}"
        
        # Setup existing LONG position (Entered at -2.0)
        strategy.active_spreads = {
            pair_key: {
                'side': 'long',
                'entry_zscore': -2.0,
                'hedge_ratio': 0.5,
                'hurst': 0.4,
                'max_adverse_z': -2.0,
                'current_z': -2.0
            }
        }
        
        # Scenario 1: Z moves favorably (to -1.0) -> No change to max_adverse
        strategy._get_hedge_ratio = MagicMock(return_value=0.5)
        strategy._calculate_spread_zscore = MagicMock(return_value=-1.0)
        
        ohlcv_a = pd.DataFrame({'close': [50000.0] * 100})
        ohlcv_b = pd.DataFrame({'close': [2000.0] * 100})
        
        strategy.generate_pair_signal(symbol_a, ohlcv_a, symbol_b, ohlcv_b)
        
        assert strategy.active_spreads[pair_key]['current_z'] == -1.0
        assert strategy.active_spreads[pair_key]['max_adverse_z'] == -2.0 # Unchanged
        
        # Scenario 2: Z moves adversely (to -3.0) -> Update max_adverse
        strategy._calculate_spread_zscore = MagicMock(return_value=-3.0)
        strategy.generate_pair_signal(symbol_a, ohlcv_a, symbol_b, ohlcv_b)
        
        assert strategy.active_spreads[pair_key]['current_z'] == -3.0
        assert strategy.active_spreads[pair_key]['max_adverse_z'] == -3.0 # Updated to lower value (more negative)

    def test_max_adverse_stop_trigger(self, strategy):
        """Test max adverse stop triggers at entry +/- 1.5."""
        symbol_a = 'BTC'
        symbol_b = 'ETH'
        pair_key = f"{symbol_a}/{symbol_b}"
        
        # Setup existing LONG position (Entered at -2.0)
        strategy.active_spreads = {
            pair_key: {
                'side': 'long',
                'entry_zscore': -2.0,
                'hedge_ratio': 0.5,
                'hurst': 0.4,
                'max_adverse_z': -2.0
            }
        }
        
        strategy._get_hedge_ratio = MagicMock(return_value=0.5)
        
        # Z-Score = -3.4 (Diff 1.4) -> HOLD
        strategy._calculate_spread_zscore = MagicMock(return_value=-3.4)
        ohlcv_a = pd.DataFrame({'close': [50000.0] * 100})
        ohlcv_b = pd.DataFrame({'close': [2000.0] * 100})
        
        res = strategy.generate_pair_signal(symbol_a, ohlcv_a, symbol_b, ohlcv_b)
        assert res is None # Hold
        assert pair_key in strategy.active_spreads
        
        # Z-Score = -3.6 (Diff 1.6 > 1.5) -> STOP
        strategy._calculate_spread_zscore = MagicMock(return_value=-3.6)
        res = strategy.generate_pair_signal(symbol_a, ohlcv_a, symbol_b, ohlcv_b)
        
        assert res is not None
        assert res['signal'] == 'sell' # Close Long
        assert "Max Adverse" in res['reason']
        assert pair_key not in strategy.active_spreads # Position removed

    def test_hard_stop_trigger(self, strategy):
        """Test hard stop triggers at |z| > 4.0."""
        symbol_a = 'BTC'
        symbol_b = 'ETH'
        pair_key = f"{symbol_a}/{symbol_b}"
        
        # Setup existing LONG position (Entered at -1.0)
        # Max adverse logic won't trigger (-1.0 - 1.5 = -2.5)
        # But we jump straight to -4.1
        strategy.active_spreads = {
            pair_key: {
                'side': 'long',
                'entry_zscore': -1.0, 
                'hedge_ratio': 0.5,
                'max_adverse_z': -1.0
            }
        }
        
        strategy._get_hedge_ratio = MagicMock(return_value=0.5)
        strategy._calculate_spread_zscore = MagicMock(return_value=-4.1)
        
        ohlcv_a = pd.DataFrame({'close': [50000.0] * 100})
        ohlcv_b = pd.DataFrame({'close': [2000.0] * 100})
        
        res = strategy.generate_pair_signal(symbol_a, ohlcv_a, symbol_b, ohlcv_b)
        
        assert res is not None
        assert res['signal'] == 'sell' # Close Long
        assert "Regime Break" in res['reason']
        assert pair_key not in strategy.active_spreads
