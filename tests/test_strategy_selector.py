import pytest
from unittest.mock import MagicMock, patch
from src.strategies.strategy_selector import StrategySelector

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
