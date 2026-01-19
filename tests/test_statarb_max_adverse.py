
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
        
        strategy._get_hedge_ratio = MagicMock(return_value=(hedge_ratio, hedge_ratio))
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
        strategy._get_hedge_ratio = MagicMock(return_value=(0.5, 0.5))
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
        
        # Setup existing LONG position (Entered at -1.0)
        strategy.active_spreads = {
            pair_key: {
                'side': 'long',
                'entry_zscore': -1.0,
                'hedge_ratio': 0.5,
                'hurst': 0.4,
                'max_adverse_z': -1.0
            }
        }
        
        strategy._get_hedge_ratio = MagicMock(return_value=(0.5, 0.5))
        
        # Z-Score = -2.4 (Diff 1.4) -> HOLD
        # Note: Hard stop is -3.0. Max Adverse is -1.0 - 1.5 = -2.5.
        strategy._calculate_spread_zscore = MagicMock(return_value=-2.4)
        ohlcv_a = pd.DataFrame({'close': [50000.0] * 100})
        ohlcv_b = pd.DataFrame({'close': [2000.0] * 100})
        
        res = strategy.generate_pair_signal(symbol_a, ohlcv_a, symbol_b, ohlcv_b)
        assert res is None # Hold
        assert pair_key in strategy.active_spreads
        
        # Z-Score = -2.6 (Diff 1.6 > 1.5) -> STOP
        strategy._calculate_spread_zscore = MagicMock(return_value=-2.6)
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
        
        strategy._get_hedge_ratio = MagicMock(return_value=(0.5, 0.5))
        strategy._calculate_spread_zscore = MagicMock(return_value=-4.1)
        
        ohlcv_a = pd.DataFrame({'close': [50000.0] * 100})
        ohlcv_b = pd.DataFrame({'close': [2000.0] * 100})
        
        res = strategy.generate_pair_signal(symbol_a, ohlcv_a, symbol_b, ohlcv_b)
        
        assert res is not None
        assert res['signal'] == 'sell' # Close Long
        assert "Regime Break" in res['reason']
        assert pair_key not in strategy.active_spreads


class TestStatArbZScoreRestore:
    """Test z-score restoration from persisted positions."""
    
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
    
    def test_restore_active_spreads_from_metadata(self, strategy):
        """Test that z-score state is restored from position metadata."""
        positions = [
            {
                'position_id': 'stat_arb_pos_1',
                'strategy': 'stat_arb_4h',
                'side': 'short',
                'legs': [
                    {'symbol': 'BTC', 'market_type': 'perp', 'side': 'short', 'size': 0.01, 'entry_price': 100000},
                    {'symbol': 'ETH', 'market_type': 'perp', 'side': 'long', 'size': 0.1, 'entry_price': 3500},
                ],
                'metadata': {
                    'pair_key': 'BTC/ETH',
                    'entry_zscore': 2.5,
                    'current_z': 1.8,
                    'max_adverse_z': 2.7,
                    'hedge_ratio': 1.0764,
                }
            }
        ]
        
        strategy.restore_active_spreads(positions)
        
        assert 'BTC/ETH' in strategy.active_spreads
        data = strategy.active_spreads['BTC/ETH']
        assert data['entry_zscore'] == 2.5
        assert data['current_z'] == 1.8
        assert data['max_adverse_z'] == 2.7
        assert data['hedge_ratio'] == 1.0764
    
    def test_restore_reconstructs_pair_key_from_legs(self, strategy):
        """Test pair_key reconstruction when not in metadata."""
        positions = [
            {
                'position_id': 'stat_arb_pos_2',
                'strategy': 'stat_arb_15m',
                'side': 'long',
                'legs': [
                    {'symbol': 'SOL', 'market_type': 'perp', 'side': 'long', 'size': 10, 'entry_price': 200},
                    {'symbol': 'MATIC', 'market_type': 'perp', 'side': 'short', 'size': 1000, 'entry_price': 0.5},
                ],
                'metadata': {
                    'entry_zscore': -2.3,
                    'current_z': -1.5,
                    'max_adverse_z': -2.8,
                    'hedge_ratio': 0.95,
                }
            }
        ]
        
        strategy.restore_active_spreads(positions)
        
        assert 'SOL/MATIC' in strategy.active_spreads
        assert strategy.active_spreads['SOL/MATIC']['entry_zscore'] == -2.3
    
    def test_restore_ignores_non_statarb_positions(self, strategy):
        """Test that non-stat_arb positions are ignored."""
        positions = [
            {
                'position_id': 'funding_arb_pos_1',
                'strategy': 'funding_arb',
                'side': 'long',
                'legs': [
                    {'symbol': 'BTC', 'market_type': 'perp', 'side': 'short', 'size': 0.01, 'entry_price': 100000},
                ],
                'metadata': {
                    'entry_zscore': 2.0,
                }
            }
        ]
        
        strategy.restore_active_spreads(positions)
        
        assert len(strategy.active_spreads) == 0
    
    def test_restore_skips_positions_without_entry_zscore(self, strategy):
        """Test that positions without entry_zscore are skipped."""
        positions = [
            {
                'position_id': 'stat_arb_pos_3',
                'strategy': 'stat_arb_4h',
                'side': 'short',
                'legs': [
                    {'symbol': 'BTC', 'market_type': 'perp', 'side': 'short', 'size': 0.01, 'entry_price': 100000},
                    {'symbol': 'ETH', 'market_type': 'perp', 'side': 'long', 'size': 0.1, 'entry_price': 3500},
                ],
                'metadata': {
                    'pair_key': 'BTC/ETH',
                    # Missing entry_zscore
                }
            }
        ]
        
        strategy.restore_active_spreads(positions)
        
        assert 'BTC/ETH' not in strategy.active_spreads
