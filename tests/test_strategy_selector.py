import pytest
from unittest.mock import MagicMock, patch
from src.strategies.strategy_selector import StrategySelector, StrategyRanking, MarketRegime, StrategyPerformanceWindow
from collections import deque
from datetime import datetime, timedelta
import os
from src.utils.trade_database import TradeDatabase

class TestStrategySelector:

    @pytest.fixture
    def strategy_selector(self, mock_config):
        """Creates a StrategySelector instance."""
        # Ensure instances are defined for selector to work with
        if 'strategies' not in mock_config:
            mock_config['strategies'] = {}
            
        mock_config['strategies']['instances'] = [
            {"type": "stat_arb", "name": "stat_arb_1h", "timeframe": "1h"},
            {"type": "momentum", "name": "momentum_1h", "timeframe": "1h"},
            {"type": "high_vol", "name": "HighVolumeStrategy", "timeframe": "1h"},
            {"type": "low_vol", "name": "LowVolumeStrategy", "timeframe": "1h"}
        ]
        
        # Mock performance tracker
        mock_tracker = MagicMock()
        
        selector = StrategySelector(mock_tracker, mock_config)
        # Store mock tracker on instance for tests to access
        selector.performance_tracker = mock_tracker 
        return selector

    def test_get_signal_strength_modifier_no_history(self, strategy_selector):
        """Test modifier returns 1.0 when no history exists."""
        modifier = strategy_selector.get_signal_strength_modifier("test_strategy")
        assert modifier == 1.0

    def test_get_signal_strength_modifier_insufficient_data(self, strategy_selector):
        """Test modifier returns 1.0 when < 3 trades exist."""
        strategy_selector.performance_windows["test_strategy"] = StrategyPerformanceWindow()
        # Add 2 trades (1 win, 1 loss)
        strategy_selector.performance_windows["test_strategy"].add_return(0.01, datetime.now(), MarketRegime.UNKNOWN)
        strategy_selector.performance_windows["test_strategy"].add_return(-0.01, datetime.now(), MarketRegime.UNKNOWN)
        
        modifier = strategy_selector.get_signal_strength_modifier("test_strategy")
        assert modifier == 1.0

    def test_get_signal_strength_modifier_low_win_rate(self, strategy_selector):
        """Test modifier penalizes low win rate (< 30%)."""
        strategy_selector.performance_windows["test_strategy"] = StrategyPerformanceWindow()
        
        # Add 10 trades: 2 wins, 8 losses (20% win rate)
        # Expected: 0.5 + (0.2 * 1.0) = 0.7
        for _ in range(2):
            strategy_selector.performance_windows["test_strategy"].add_return(0.01, datetime.now(), MarketRegime.UNKNOWN)
        for _ in range(8):
            strategy_selector.performance_windows["test_strategy"].add_return(-0.01, datetime.now(), MarketRegime.UNKNOWN)
            
        modifier = strategy_selector.get_signal_strength_modifier("test_strategy")
        assert modifier == 0.7

    def test_get_signal_strength_modifier_high_win_rate(self, strategy_selector):
        """Test modifier boosts high win rate (> 70%)."""
        strategy_selector.performance_windows["test_strategy"] = StrategyPerformanceWindow()
        
        # Add 10 trades: 8 wins, 2 losses (80% win rate)
        # Expected: 0.5 + (0.8 * 1.0) = 1.3
        for _ in range(8):
            strategy_selector.performance_windows["test_strategy"].add_return(0.01, datetime.now(), MarketRegime.UNKNOWN)
        for _ in range(2):
            strategy_selector.performance_windows["test_strategy"].add_return(-0.01, datetime.now(), MarketRegime.UNKNOWN)
            
        modifier = strategy_selector.get_signal_strength_modifier("test_strategy")
        assert modifier == 1.3

    def test_initialization(self, strategy_selector):
        """Test initialization matches config."""
        # Use register_strategies to simulate loading from config in real usage
        strategy_selector.register_strategies(["stat_arb_1h", "momentum_1h"])
        assert len(strategy_selector.strategy_rankings) == 2
        assert "stat_arb_1h" in strategy_selector.strategy_rankings
        
    def test_update_rankings(self, strategy_selector):
        """Test ranking update logic."""
        # Mock performance tracker data
        strategy_selector.performance_tracker.get_all_strategy_metrics = MagicMock(return_value={
            "stat_arb_1h": MagicMock(to_dict=lambda: {'sharpe_ratio': 2.0, 'win_rate': 60.0, 'total_trades': 10}),
            "momentum_1h": MagicMock(to_dict=lambda: {'sharpe_ratio': 1.0, 'win_rate': 40.0, 'total_trades': 10})
        })
        
        # Ensure strategies are registered
        strategy_selector.register_strategies(["stat_arb_1h", "momentum_1h"])
        
        strategy_selector.update_rankings(force=True)
        
        # Verify weights were calculated
        ranking = strategy_selector.strategy_rankings.get("stat_arb_1h")
        assert ranking is not None
        assert ranking.score > 0

    
    def test_fetch_db_data_no_database(self, strategy_selector):
        """Test that fetching from non-existent DB returns empty structures."""
        history = strategy_selector._fetch_db_data("/nonexistent/path.db", "test")
        assert history == {}
    
        assert history == {}

    def test_initialize_strategies_hybrid_fallback(self, strategy_selector):
        """Test that strategy initialization falls back to backtest DB correctly."""
        import tempfile
        import sqlite3
        import os
        
        # Create a temporary trades.db with test data
        with tempfile.TemporaryDirectory() as tmpdir:
            trades_db = os.path.join(tmpdir, "trades.db")
            # Legacy backtest path (ignored by new logic, but kept for config compatibility)
            backtest_db = os.path.join(tmpdir, "backtest.db")
            
            # Set backtest path and register strategies
            strategy_selector.backtest_results_path = backtest_db
            strategy_selector.register_strategies(["blended_strat"])
            
            # Create trades.db with BOTH tables (Unified DB Architecture)
            conn = sqlite3.connect(trades_db)
            
            # 1. Live Trades Table
            conn.execute("CREATE TABLE trades (strategy TEXT, pnl REAL, pnl_percentage REAL, exit_time TEXT)")
            base_time = datetime.now()
            # 5 Recent Live Trades
            for i in range(5):
                t = base_time - timedelta(minutes=i)
                conn.execute(f"INSERT INTO trades VALUES ('blended_strat', 10.0, 0.01, '{t.isoformat()}')")
            
            # 2. Backtest Trades Table (New Source)
            conn.execute("CREATE TABLE backtest_trades (strategy TEXT, pnl REAL, pnl_percentage REAL, exit_time TEXT)")
            # 50 Older Backtest Trades
            for i in range(50):
                t = base_time - timedelta(days=1, minutes=i)
                conn.execute(f"INSERT INTO backtest_trades VALUES ('blended_strat', 20.0, 0.02, '{t.isoformat()}')")
                
            conn.commit()
            conn.close()
            
            # Run initialization
            strategy_selector._initialize_strategies_from_history(live_db_path=trades_db)
            
            # Verify Blended Loading
            assert "blended_strat" in strategy_selector.strategy_rankings
            rank = strategy_selector.strategy_rankings["blended_strat"]
            
            # Should have 20 trades total (Target Size)
            # 5 Live + 15 Backtest to fill gap to 20
            # Note: total_trades in metrics counts the blended set
            assert rank.metrics['total_trades'] == 20
            # assert rank.metrics.get('from_backtest') is True # Verify flag is set
            
            # Calculate expected PnL: (5 * 10.0) + (15 * 20.0) = 50 + 300 = 350.0
            assert rank.metrics['total_pnl'] == 350.0
            
            # Verify Window Population
            window = strategy_selector.performance_windows["blended_strat"]
            assert len(window.returns) == 20
            
    def test_load_rankings_per_strategy_limit(self, strategy_selector):
        """Test that loading limits trades PER strategy, not globally."""
        # Create a real temporary database
        db_path = "test_history_loading.db"
        if os.path.exists(db_path):
            os.remove(db_path)
            
        try:
            db = TradeDatabase(db_path)
            strategy_selector.register_strategies(["HighVolumeStrategy", "LowVolumeStrategy"])
            
            # Insert 100 trades for Strategy A (more than limit)
            # Timestamps: recent
            base_time = datetime.now()
            for i in range(100):
                db.insert_trade({
                    'symbol': 'BTC',
                    'strategy': 'HighVolumeStrategy',
                    'side': 'long',
                    'entry_price': 50000,
                    'exit_price': 51000,
                    'size': 0.01,
                    'entry_time': base_time - timedelta(minutes=100-i),
                    'exit_time': base_time - timedelta(minutes=100-i),
                    'pnl': 10,
                    'pnl_percentage': 0.02,
                    'capital_at_risk': 1000,
                    'exit_reason': 'take_profit'
                })
                
            # Insert 5 trades for Strategy B (infrequent)
            # Timestamps: OLDER than Strategy A's trades
            # If global limit 50 was used, these would be starvation victims
            old_base_time = base_time - timedelta(days=1)
            for i in range(5):
                db.insert_trade({
                    'symbol': 'ETH',
                    'strategy': 'LowVolumeStrategy',
                    'side': 'long',
                    'entry_price': 3000,
                    'exit_price': 3100,
                    'size': 0.1,
                    'entry_time': old_base_time - timedelta(minutes=10-i),
                    'exit_time': old_base_time - timedelta(minutes=10-i),
                    'pnl': 10,
                    'pnl_percentage': 0.02,
                    'capital_at_risk': 1000,
                    'exit_reason': 'take_profit'
                })
            
            # Fetch data with helper
            history = strategy_selector._fetch_db_data(db_path, "test_db")
            
            # Check Strategy A History: Should have exactly 50 recent trades (window limit 50)
            assert 'HighVolumeStrategy' in history
            trades_a = history.get('HighVolumeStrategy', [])
            assert len(trades_a) == 50
            
            # Check Strategy B History: Should have ALL 5 trades
            assert 'LowVolumeStrategy' in history
            trades_b = history.get('LowVolumeStrategy', [])
            assert len(trades_b) == 5

            
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)
            # Clean up TradeDatabase side effects (it creates wal/shm files)
            if os.path.exists(db_path + "-wal"):
                os.remove(db_path + "-wal")
            if os.path.exists(db_path + "-shm"):
                os.remove(db_path + "-shm")
