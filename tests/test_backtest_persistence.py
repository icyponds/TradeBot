import pytest
from unittest.mock import MagicMock, patch
import os
import sqlite3
import pandas as pd
from datetime import datetime

from src.utils.performance_tracker import PerformanceTracker, CompletedTrade
from src.utils.trade_database import TradeDatabase
from src.backtesting.backtest_engine import BacktestEngine
import contextlib
import logging

class InMemoryTradeDatabase(TradeDatabase):
    """Subclass that keeps a persistent connection for :memory: databases."""
    def __init__(self, db_path, table_prefix=""):
        self.logger = logging.getLogger(__name__)
        self.db_path = ":memory:"
        self.table_prefix = table_prefix
        self._persistent_conn = sqlite3.connect(":memory:")
        self._persistent_conn.row_factory = sqlite3.Row
        self._init_database()

    @contextlib.contextmanager
    def _get_connection(self):
        conn = self._persistent_conn
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e

class TestBacktestPersistence:
    
    @pytest.fixture
    def temp_db_config(self):
        """Setup in-memory config."""
        return {
            'persistence': {
                'db_path': ":memory:"
            },
            'trading': {
                'risk_per_trade': 0.01,
                'max_open_trades': 5
            },
            'backtesting': {
                'initial_capital': 10000
            }
        }

    def test_performance_tracker_prefix_isolation(self, temp_db_config):
        """
        Verify that PerformanceTracker uses the table prefix for all operations.
        """
        # Patch TradeDatabase to use our in-memory version
        with patch('src.utils.performance_tracker.TradeDatabase', side_effect=InMemoryTradeDatabase) as MockDB:
            # 1. Initialize Tracker with Prefix
            # Note: We must hold a reference to the DB instance to access its persistent connection
            tracker = PerformanceTracker(temp_db_config, table_prefix="backtest_")
            
            # 2. Add a trade
            # record_trade expects a CompletedTrade object
            trade = CompletedTrade(
                symbol='BTC/USD',
                strategy='test_strat',
                side='buy',
                entry_price=50000.0,
                exit_price=51000.0,
                size=0.1,
                entry_time=datetime.now(),
                exit_time=datetime.now(),
                pnl=100.0,
                pnl_percentage=0.02,
                capital_at_risk=5000.0,
                exit_reason='tp'
            )
            tracker.record_trade(trade)
            
            # 3. Verify it exists in backtest_trades table
            # Must use the tracker's internal DB connection
            db_instance = tracker.db
            
            with db_instance._get_connection() as conn:
                cursor = conn.cursor()
                
                # Check backtest table
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='backtest_trades'")
                assert cursor.fetchone() is not None
                
                cursor.execute("SELECT count(*) FROM backtest_trades")
                count = cursor.fetchone()[0]
                assert count == 1
                
                # Check that NO 'trades' table was created or written to (if it didn't exist)
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trades'")
                result = cursor.fetchone()
                if result:
                    # If it exists, it should be empty
                    cursor.execute("SELECT count(*) FROM trades")
                    assert cursor.fetchone()[0] == 0

    def test_backtest_engine_setup(self, temp_db_config):
        """
        Verify that BacktestEngine correctly initializes the PerformanceTracker with the prefix.
        """
        # Patch at the source because BacktestEngine does a local import
        with patch('src.utils.performance_tracker.PerformanceTracker') as MockTracker:
            # Setup mock to return a tracked instance we can inspect
            mock_tracker_instance = MagicMock()
            MockTracker.return_value = mock_tracker_instance
            
            # Setup Mock StrategyManager to avoid complex init
            # Patch StrategyManager at the module where BacktestEngine imports it, OR at source.
            # StrategyManager is imported at top level of backtest_engine.py, so we patch there.
            with patch('src.backtesting.backtest_engine.StrategyManager') as MockStratMan:
                 with patch('src.backtesting.backtest_engine.MockMarketAPI'):
                    engine = BacktestEngine(temp_db_config)
            
            # Verify PerformanceTracker was called with table_prefix="backtest_"
            MockTracker.assert_called_with(temp_db_config, table_prefix="backtest_")
            
            # Verify the engine set the tracker on the strategy manager
            MockStratMan.assert_called()
            call_args = MockStratMan.call_args[1] # kwargs
            assert 'performance_tracker' in call_args
            assert call_args['performance_tracker'] == mock_tracker_instance

    def test_trade_database_table_creation(self, temp_db_config):
        """
        Verify TradeDatabase creates prefixed tables correctly.
        """
        # Patch TradeDatabase to use our in-memory version
        # Note: We patch at the source where it would be used
        with patch('src.utils.performance_tracker.TradeDatabase', side_effect=InMemoryTradeDatabase):
            # Instantiate directly (but we want to test the class itself)
            # Since we are testing TradeDatabase itself, we should use InMemoryTradeDatabase directly
            # but verify that it respects the prefix passed in __init__
            
            db = InMemoryTradeDatabase(db_path=":memory:", table_prefix="sandbox_")
            
            with db._get_connection() as conn:
                cursor = conn.cursor()
                
                # Check for prefixed tables
                expected_tables = ['sandbox_trades', 'sandbox_equity_snapshots', 'sandbox_daily_pnl']
                for table in expected_tables:
                    cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'")
                    assert cursor.fetchone() is not None, f"Table {table} not created"
