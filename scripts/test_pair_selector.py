
import sys
import os
import logging
from datetime import datetime
import pandas as pd
import numpy as np

# Add src to path
sys.path.append(os.getcwd())

from src.utils.pair_selector import DynamicPairSelector
from src.utils.correlation_manager import CorrelationManager

# Mock classes
class MockMarketAPI:
    def get_asset_info(self):
        return {
            'universe': [
                {'name': 'BTC', 'volume24h': 1000000000, 'openInterest': 500000000, 'markPrice': 50000.0},
                {'name': 'ETH', 'volume24h': 500000000, 'openInterest': 200000000, 'markPrice': 3000.0},
                {'name': 'SOL', 'volume24h': 200000000, 'openInterest': 100000000, 'markPrice': 150.0},
                {'name': 'AVAX', 'volume24h': 50000000, 'openInterest': 20000000, 'markPrice': 30.0},
                {'name': 'LTC', 'volume24h': 40000000, 'openInterest': 10000000, 'markPrice': 80.0},
            ]
        }
    
    def get_ohlcv(self, symbol, timeframe='1h', limit=100):
        # Generate random walk data
        dates = pd.date_range(end=datetime.now(), periods=limit, freq='1h')
        base_price = 100.0
        if symbol == 'BTC': base_price = 50000.0
        elif symbol == 'ETH': base_price = 3000.0
        
        prices = [base_price]
        for _ in range(limit-1):
            prices.append(prices[-1] * (1 + np.random.normal(0, 0.01)))
            
        return pd.DataFrame({
            'timestamp': dates,
            'open': prices,
            'high': [p*1.01 for p in prices],
            'low': [p*0.99 for p in prices],
            'close': prices,
            'volume': [1000.0] * limit
        })
        
    def get_price_history(self, symbol):
        df = self.get_ohlcv(symbol, timeframe='1d', limit=30)
        return df['close']

def test_pair_selector():
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger("TestPairSelector")
    logger.info("Starting Pair Selector Test...")
    
    config = {
        'trading': {
            'dynamic_pair_selection': True,
            'min_open_interest': 0,
            'scan_interval_minutes': 60,
            'excluded_assets': [],
            'included_assets': [],
            'max_pairs_to_trade': 3
        },
        'pair_selection': {
            'mode': 'sophisticated',
            'weights': {
                'liquidity': 0.2,
                'volatility': 0.2,
                'strategy_fit': 0.4, # Heavy weight on fit (cointegration)
                'diversification': 0.1,
                'historical_performance': 0.1
            }
        },
        'strategies': {
            'stat_arb': {
                'min_correlation': 0.5
            }
        }
    }
    
    api = MockMarketAPI()
    
    # Initialize CorrelationManager
    logger.info("Initializing CorrelationManager...")
    corr_manager = CorrelationManager(api, config)
    
    # Initialize PairSelector
    logger.info("Initializing DynamicPairSelector...")
    selector = DynamicPairSelector(config, api, correlation_manager=corr_manager)
    
    # Run scan
    logger.info("Running scan_and_select_pairs...")
    selected_pairs = selector.scan_and_select_pairs()
    
    logger.info(f"Selected Pairs: {selected_pairs}")
    
    # Check if cointegration logic ran
    pairs = corr_manager.get_cointegrated_pairs_dict()
    logger.info(f"Cointegrated Pairs Found: {len(pairs)}")
    
    # Verify metadata
    for p in selected_pairs:
        meta = selector.get_pair_metadata(p)
        logger.info(f"Metadata for {p}: Score={meta['composite_score']:.3f} Fit={meta['scores']['strategy_fit']:.3f}")
        
    print("Test Complete!")

if __name__ == "__main__":
    test_pair_selector()
