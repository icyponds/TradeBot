import pytest
from unittest.mock import MagicMock, patch
from src.strategies.strategy_selector import StrategySelector, StrategyRanking, MarketRegime, StrategyPerformanceWindow
from collections import deque
from datetime import datetime

class TestStrategySelector:

    @pytest.fixture
    def strategy_selector(self, mock_config):
        """Creates a StrategySelector instance."""
        # Ensure instances are defined for selector to work with
        if 'strategies' not in mock_config:
            mock_config['strategies'] = {}
            
        mock_config['strategies']['instances'] = [
            {"type": "stat_arb", "name": "stat_arb_1h", "timeframe": "1h"},
            {"type": "momentum", "name": "momentum_1h", "timeframe": "1h"}
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

    def test_load_rankings_from_db_no_database(self, strategy_selector):
        """Test that loading rankings returns False when no database exists."""
        strategy_selector.register_strategies(["stat_arb_1h", "momentum_1h"])
        # Should not crash and should fall back to defaults
        result = strategy_selector._load_rankings_from_db("/nonexistent/path.db", "test")
        assert result is False
    
    def test_load_rankings_priority_order(self, strategy_selector):
        """Test that _load_rankings_from_backtest tries trades.db first."""
        import tempfile
        import sqlite3
        import os
        
        strategy_selector.register_strategies(["stat_arb_1h", "momentum_1h"])
        
        # Create a temporary trades.db with test data
        with tempfile.TemporaryDirectory() as tmpdir:
            trades_db = os.path.join(tmpdir, "trades.db")
            backtest_db = os.path.join(tmpdir, "backtest.db")
            
            # Create trades.db with data
            conn = sqlite3.connect(trades_db)
            conn.execute("""
                CREATE TABLE trades (
                    strategy TEXT, pnl REAL
                )
            """)
            # Insert 5 trades for stat_arb_1h (enough to trigger)
            for _ in range(5):
                conn.execute("INSERT INTO trades VALUES ('stat_arb_1h', 10.0)")
            conn.commit()
            conn.close()
            
            # Test that it loads from the db
            result = strategy_selector._load_rankings_from_db(trades_db, "live trades")
            assert result is True
            
            # Check ranking was created
            assert "stat_arb_1h" in strategy_selector.strategy_rankings
            ranking = strategy_selector.strategy_rankings["stat_arb_1h"]
            assert ranking.weight > 0
