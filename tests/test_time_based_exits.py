
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock
import pandas as pd
from src.strategies.statistical_arbitrage_strategy import StatisticalArbitrageStrategy
from src.strategies.ou_mean_reversion_strategy import OUMeanReversionStrategy
from src.models.trade import Position

class TestTimeBasedExits:
    
    def test_statarb_max_holding_time_exceeded(self, mock_config):
        """
        Verify StatArb triggers exit for old positions even if active_spreads is empty.
        This simulates a bot restart where memory of the spread is lost but DB position remains.
        """
        # modify config to ensure defaults are set
        mock_config['strategies']['stat_arb'] = {
            'max_holding_hours': 120,
            'z_score_threshold': 2.0
        }
        
        strategy = StatisticalArbitrageStrategy(mock_config)
        
        # Ensure internal memory is empty (simulating restart)
        strategy.active_spreads = {} 
        
        # Create a "Zombie" position from 6 days ago
        entry_time = datetime.now() - timedelta(hours=144) # 6 days
        
        position = MagicMock(spec=Position)
        position.symbol = "BTC"
        position.entry_time = entry_time
        position.entry_price = 50000
        position.side = "long"
        
        # Act
        should_exit, reason = strategy.should_exit(position, 50000, {})
        
        # Assert
        assert should_exit is True
        assert "max_holding_time_exceeded" in reason
        assert "144" in str(reason) or "6 days" in str(reason)

    def test_ou_mean_reversion_max_holding_time_exceeded(self, mock_config):
        """
        Verify OU Mean Reversion triggers exit for old positions.
        """
        # modify config
        mock_config['strategies']['ou_mean_reversion'] = {
            'max_holding_hours': 48, # shorter test limit
            'zscore_entry': 2.0,
            'zscore_exit': 0.5
        }
        
        strategy = OUMeanReversionStrategy(mock_config)
        
        # Create a "Zombie" position from 3 days ago (72h > 48h)
        entry_time = datetime.now() - timedelta(hours=72)
        
        position = MagicMock(spec=Position)
        position.symbol = "ETH"
        position.entry_time = entry_time
        
        # Act
        should_exit, reason = strategy.should_exit(position, 2000, {})
        
        # Assert
        assert should_exit is True
        assert "max_holding_time_exceeded" in reason

    def test_time_check_handles_string_timestamps(self, mock_config):
        """
        Verify logic handles string timestamps (which might come from DB/JSON).
        """
        strategy = StatisticalArbitrageStrategy(mock_config)
        
        # String entry time from 10 days ago
        entry_time_dt = datetime.now() - timedelta(days=10)
        entry_time_str = entry_time_dt.isoformat()
        
        position = MagicMock(spec=Position)
        position.symbol = "SOL"
        position.entry_time = entry_time_str
        
        should_exit, reason = strategy.should_exit(position, 100, {})
        
        assert should_exit is True
        assert "max_holding_time_exceeded" in reason
