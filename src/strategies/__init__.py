# Strategies package

from .base_strategy import BaseStrategy
from .strategy_manager import StrategyManager
from .strategy_selector import StrategySelector
from .legacy.moving_average_strategy import MovingAverageStrategy
from .legacy.rsi_strategy import RSIStrategy
from .legacy.bollinger_band_strategy import BollingerBandSqueezeStrategy
from .legacy.supertrend_strategy import SupertrendStrategy
from .legacy.vwap_strategy import VWAPStrategy
from .statistical_arbitrage_strategy import StatisticalArbitrageStrategy
from .funding_rate_arbitrage_strategy import FundingRateArbitrageStrategy
from .ou_mean_reversion_strategy import OUMeanReversionStrategy
from .momentum_factor_strategy import MomentumFactorStrategy

__all__ = [
    'BaseStrategy',
    'StrategyManager',
    'StrategySelector',
    'MovingAverageStrategy',
    'RSIStrategy',
    'BollingerBandSqueezeStrategy',
    'SupertrendStrategy',
    'VWAPStrategy',
    'StatisticalArbitrageStrategy',
    'FundingRateArbitrageStrategy',
    'OUMeanReversionStrategy',
    'MomentumFactorStrategy',
]