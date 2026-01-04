import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime
from src.utils.performance_tracker import PerformanceTracker, CompletedTrade

class TestPerformanceTracker:
    
    @pytest.fixture
    def tracker(self, mock_config, tmp_path):
        """Creates PerformanceTracker with temp DB."""
        # Override config to use temp path
        mock_config['persistence'] = {'db_path': str(tmp_path / "perf_trades.db")}
        return PerformanceTracker(mock_config, data_dir=str(tmp_path))

    def test_record_trade(self, tracker):
        """Test recording a trade object."""
        trade = CompletedTrade(
            symbol='ETH', strategy='test', side='long',
            entry_price=2000, exit_price=2100, size=1.0,
            entry_time=datetime.now(), exit_time=datetime.now(),
            pnl=100.0, pnl_percentage=5.0, capital_at_risk=2000.0,
            exit_reason='tp'
        )
        
        tracker.record_trade(trade)
        
        assert len(tracker.completed_trades) == 1
        assert tracker.completed_trades[0].pnl == 100.0
        
    def test_metrics_calculation(self, tracker):
        """Test metrics calculation (wraps DB aggregation)."""
        t1 = CompletedTrade(
            symbol='A', strategy='s1', side='long',
            entry_price=100, exit_price=110, size=1,
            entry_time=datetime.now(), exit_time=datetime.now(),
            pnl=10, pnl_percentage=10, capital_at_risk=100, exit_reason='tp'
        )
        tracker.record_trade(t1)
        
        metrics = tracker.calculate_metrics()
        assert metrics.total_trades == 1
        assert metrics.win_rate == 100.0
        assert metrics.total_pnl == 10.0
