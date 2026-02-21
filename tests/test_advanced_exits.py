import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from src.strategies.volatility_breakout_strategy import VolatilityBreakoutStrategy
from src.strategies.liquidation_hunter_strategy import LiquidationHunterStrategy
from src.strategies.ou_mean_reversion_strategy import OUMeanReversionStrategy

@pytest.fixture
def mock_config():
    return {
        'strategies': {
            'ohlcv_limit': 100,
            'volatility_breakout': {'bband_length': 20},
            'liquidation_hunter': {'window': 20},
            'ou_mean_reversion': {'min_data_points': 50}
        }
    }

def test_volatility_breakout_dynamic_tp(mock_config):
    strategy = VolatilityBreakoutStrategy(mock_config, '4h')
    
    # Create fake OHLCV data for ATR calculation (length > atr_length which is 14)
    dates = pd.date_range(start='2024-01-01', periods=20, freq='4h')
    
    # Create an artificially widening range to give a clear ATR
    high = np.linspace(105, 120, 20)
    low = np.linspace(95, 100, 20)
    close = np.linspace(100, 110, 20)
    
    df = pd.DataFrame({'high': high, 'low': low, 'close': close}, index=dates)
    
    ohlcv = {'4h': df}
    
    entry_price = 100.0
    tp_long = strategy.calculate_take_profit(entry_price, 'long', ohlcv)
    tp_short = strategy.calculate_take_profit(entry_price, 'short', ohlcv)
    
    # Using ATR based TP should be calculable and > entry for long, < entry for short
    assert tp_long > entry_price
    assert tp_short < entry_price
    # Check it's not simply exactly 10% (from default fallback)
    assert abs(tp_long - (entry_price * 1.10)) > 0.01

def test_volatility_breakout_time_decay_stop(mock_config):
    strategy = VolatilityBreakoutStrategy(mock_config, '4h')
    
    # Create position Mock
    position = MagicMock()
    position.side = 'long'
    position.entry_price = 100.0
    
    now = datetime.now(timezone.utc)
    
    # Case 1: Held for 17 hours, price stagnant (+0.5%) -> Should exit
    position.entry_time = now - timedelta(hours=17)
    current_price = 100.5
    should_exit, reason = strategy.should_exit(position, current_price, {})
    assert should_exit is True
    assert "time_decay_stop" in reason
    
    # Case 2: Held for 17 hours, price is trending strongly (+3%) -> Should NOT exit
    position.entry_time = now - timedelta(hours=17)
    current_price = 103.0
    should_exit, reason = strategy.should_exit(position, current_price, {})
    assert should_exit is False
    assert reason is None
    
    # Case 3: Held for 5 hours, price stagnant (+0.5%) -> Should NOT exit
    position.entry_time = now - timedelta(hours=5)
    current_price = 100.5
    should_exit, reason = strategy.should_exit(position, current_price, {})
    assert should_exit is False

def test_liquidation_hunter_time_decay_stop(mock_config):
    strategy = LiquidationHunterStrategy(mock_config, '15m')
    
    # Create position Mock
    position = MagicMock()
    now = datetime.now(timezone.utc)
    
    # Case 1: Held for 2 hours -> Should exit
    position.entry_time = now - timedelta(hours=2)
    current_price = 100.0
    should_exit, reason = strategy.should_exit(position, current_price, {})
    assert should_exit is True
    assert "time_decay_stop" in reason
    
    # Case 2: Held for 30 mins -> Should NOT exit
    position.entry_time = now - timedelta(minutes=30)
    should_exit, reason = strategy.should_exit(position, current_price, {})
    assert should_exit is False

def test_ou_mean_reversion_asymmetric_exit(mock_config):
    strategy = OUMeanReversionStrategy(mock_config, '4h')
    
    # Mock parameter estimation
    mock_params = MagicMock()
    mock_params.mu = 100.0  # log_mu will be log(100)
    mock_params.sigma = 0.05
    strategy._estimate_ou_parameters = MagicMock(return_value=mock_params)
    strategy.zscore_exit = 0.5
    
    # We need prev_price and current_price to calculate z_score_change
    # Let's say prev_price = 115 (z_score ~2.7), current_price = 101 (z_score ~0.2) -> dz ~2.5
    dates = pd.date_range(start='2024-01-01', periods=50, freq='4h')
    close = np.full(50, 100.0)
    close[-2] = 115.0 # Very high previous price

    df = pd.DataFrame({'close': close}, index=dates)
    current_data = {'ohlcv': df}

    position = MagicMock()
    position.side = 'short'  # Shorting the spike -> expecting reversion to mean
    
    # Valid entry time to avoid timedelta MagicMock crash
    position.entry_time = pd.Timestamp.now(tz=timezone.utc) - pd.Timedelta(hours=2)

    current_price = 101.0 # Has reverted massively (dz > 1.0) and z < 0.5 (normal exit is <0.5)

    should_exit, reason = strategy.should_exit(position, current_price, current_data)

    # Because dz > 1.0 (violent reversion), asymmetric exit ignores the threshold and holds!
    assert should_exit is False
    assert reason is None

    # Now let's test a slow reversion where dz < 1.0
    close[-2] = 103.0 # Slow reversion, prev z_score ~0.59. dz = 0.59 - 0.19 = 0.40
    df2 = pd.DataFrame({'close': close}, index=dates)
    current_data2 = {'ohlcv': df2}

    should_exit, reason = strategy.should_exit(position, current_price, current_data2)
    # Because dz < 1.0, it respects the zscore_exit threshold of < 0.5 and exits.
    assert should_exit is True
    assert "mean_reversion_complete" in reason
