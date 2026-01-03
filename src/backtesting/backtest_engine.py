
import logging
import pandas as pd
from datetime import datetime
from typing import Dict, Any, List, Optional
import time

from src.strategies.strategy_manager import StrategyManager
from src.backtesting.mock_market_api import MockMarketAPI

class BacktestEngine:
    """
    Event-driven backtesting engine.
    Orchestrates the simulation of strategy execution over historical data.
    """
    
    def __init__(self, config: Dict[str, Any], historical_data: Dict[str, pd.DataFrame]):
        """
        Initialize the backtest engine.
        
        Args:
            config: Bot configuration
            historical_data: Dictionary of OHLCV DataFrames {symbol: dataframe}
        """
        self.config = config
        self.historical_data = historical_data
        self.logger = logging.getLogger(__name__)
        
        # Initialize Mock API
        self.mock_api = MockMarketAPI(config, historical_data)
        
        # Initialize Strategy Manager with injected Mock API
        self.strategy_manager = StrategyManager(config, market_api=self.mock_api)
        
        # Results
        self.results = {
            'trades': [],
            'performance': {},
            'status': 'idx'
        }
        
    def run(self, start_date: datetime, end_date: datetime, interval_minutes: int = 60):
        """
        Run the backtest simulation.
        
        Args:
            start_date: Start datetime
            end_date: End datetime
            interval_minutes: Step size in minutes
        """
        self.logger.info(f"Starting backtest from {start_date} to {end_date}")
        
        # Set running flag manually since we aren't calling start()
        self.strategy_manager.is_running = True
        
        # Generate simulation timestamps
        timestamps = pd.date_range(start=start_date, end=end_date, freq=f'{interval_minutes}T')
        
        total_steps = len(timestamps)
        
        for i, current_time in enumerate(timestamps):
            # Update Mock API time
            self.mock_api.set_time(current_time)
            
            # Run one cycle of Strategy Manager
            try:
                self.strategy_manager.run_trading_cycle()
            except Exception as e:
                self.logger.error(f"Error at step {current_time}: {e}")
                
            # Optional: Progress logging
            if i % 100 == 0:
                print(f"Progress: {i}/{total_steps} ({i/total_steps*100:.1f}%)")
                
        # Clean shutdown
        self.strategy_manager.is_running = False
        self.logger.info("Backtest completed")
        
        return self.generate_report()
        
    def generate_report(self) -> Dict[str, Any]:
        """Generate performance report from the backtest."""
        trades = self.mock_api.orders
        positions = self.mock_api.positions
        
        # Calculate simplistic PnL
        # This is very basic; a real engine would track equity curve per step
        spot_balance = self.mock_api.get_spot_balance('USDC')
        perp_balance = self.mock_api.get_perp_balance()['withdrawable']
        total_equity = spot_balance + perp_balance
        
        report = {
            'total_equity': total_equity,
            'positions_open': len(positions),
            'total_orders': len(trades),
            'spot_balance': spot_balance,
            'perp_balance': perp_balance
        }
        
        return report
