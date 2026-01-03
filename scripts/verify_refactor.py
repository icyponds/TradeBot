
import sys
import os
import pandas as pd
import logging
from datetime import datetime

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.strategies.legacy.moving_average_strategy import MovingAverageStrategy
from src.strategies.legacy.rsi_strategy import RSIStrategy
from src.strategies.legacy.bollinger_band_strategy import BollingerBandSqueezeStrategy
from src.strategies.legacy.supertrend_strategy import SupertrendStrategy
from src.strategies.legacy.vwap_strategy import VWAPStrategy
from src.strategies.statistical_arbitrage_strategy import StatisticalArbitrageStrategy

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("VerifyRefactor")

def create_mock_ohlcv(length=100):
    prices = [100.0]
    for i in range(1, length):
        prices.append(prices[-1] * (1 + (0.01 if i % 2 == 0 else -0.01)))
    
    df = pd.DataFrame({
        'timestamp': [datetime.now() for _ in range(length)],
        'open': prices,
        'high': [p * 1.01 for p in prices],
        'low': [p * 0.99 for p in prices],
        'close': prices,
        'volume': [1000.0] * length
    })
    return df

def test_moving_average():
    config = {'strategies': {'ohlcv_limit': 100, 'moving_average': {'short_period': 5, 'long_period': 10}}}
    strategy = MovingAverageStrategy(config)
    ohlcv = create_mock_ohlcv(20)
    
    strength = strategy.calculate_signal_strength(ohlcv)
    logger.info(f"MovingAverage Strength: {strength}")
    assert 0.0 <= strength <= 1.0

def test_rsi():
    config = {'strategies': {'ohlcv_limit': 100, 'rsi': {'period': 14, 'overbought': 70, 'oversold': 30}}}
    strategy = RSIStrategy(config)
    ohlcv = create_mock_ohlcv(20)
    
    strength = strategy.calculate_signal_strength(ohlcv)
    logger.info(f"RSI Strength: {strength}")
    assert 0.0 <= strength <= 1.0

def test_bollinger():
    config = {'strategies': {'ohlcv_limit': 100, 'bollinger_band': {'period': 20, 'std_dev': 2.0}}}
    strategy = BollingerBandSqueezeStrategy(config)
    ohlcv = create_mock_ohlcv(30)
    
    strength = strategy.calculate_signal_strength(ohlcv)
    logger.info(f"Bollinger Strength: {strength}")
    assert 0.0 <= strength <= 1.0

def test_supertrend():
    config = {'strategies': {'ohlcv_limit': 100, 'supertrend': {'atr_period': 10, 'multiplier': 3.0}}}
    strategy = SupertrendStrategy(config)
    ohlcv = create_mock_ohlcv(20)
    
    strength = strategy.calculate_signal_strength(ohlcv)
    logger.info(f"Supertrend Strength: {strength}")
    assert strength == 0.8 # As per implementation

def test_vwap():
    config = {'strategies': {'ohlcv_limit': 100, 'vwap': {'std_dev_mult': 2.0}}}
    strategy = VWAPStrategy(config)
    ohlcv = create_mock_ohlcv(200) # VWAP needs more data
    
    strength = strategy.calculate_signal_strength(ohlcv)
    logger.info(f"VWAP Strength: {strength}")
    assert 0.0 <= strength <= 1.0

def test_stat_arb():
    config = {'strategies': {'ohlcv_limit': 100, 'statistical_arbitrage': {}}}
    # StatArb init might be complex but we implemented it minimally
    # It might require 'statistical_arbitrage' key in config
    strategy = StatisticalArbitrageStrategy(config)
    ohlcv = create_mock_ohlcv(20)
    
    strength = strategy.calculate_signal_strength(ohlcv)
    logger.info(f"StatArb Strength: {strength}")
    assert strength == 0.8

def run_all():
    try:
        test_moving_average()
        test_rsi()
        test_bollinger()
        test_supertrend()
        test_vwap()
        test_stat_arb()
        logger.info("All signal strength tests PASSED!")
    except Exception as e:
        logger.error(f"Test FAILED: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    run_all()
