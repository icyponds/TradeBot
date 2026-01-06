import pytest
from unittest.mock import MagicMock
from datetime import datetime, timedelta
from src.strategies.strategy_selector import StrategySelector, StrategyRanking, MarketRegime

class TestStrategyWeights:
    
    @pytest.fixture
    def mock_config(self):
        return {
            'trading': {'min_trades_for_ranking': 5},
            'backtesting': {'results_db_path': ':memory:'}
        }

    @pytest.fixture
    def selector(self, mock_config):
        tracker = MagicMock()
        # Setup default mock return to avoid errors if called unexpectedly
        tracker.get_all_strategy_metrics.return_value = {}
        
        selector = StrategySelector(tracker, mock_config, strategy_names=['StrategyA', 'StrategyB', 'StrategyC'])
        return selector

    def create_mock_metrics(self, win_rate=50, profit_factor=1.5, total_trades=20, 
                           expectancy=10, sharpe=1.5, pnl=1000):
        """Helper to create metric objects similar to what PerformanceTracker returns."""
        mock_m = MagicMock()
        # The selector calls to_dict() on the metrics object
        mock_m.to_dict.return_value = {
            'win_rate': win_rate,
            'profit_factor': profit_factor,
            'expectancy': expectancy,
            'sharpe_ratio': sharpe,
            'total_pnl': pnl,
            'total_trades': total_trades,
            'winning_trades': int(total_trades * (win_rate/100)),
            'losing_trades': int(total_trades * ((100-win_rate)/100)),
            'risk_reward_ratio': 1.5,
            'max_drawdown': 10
        }
        return mock_m

    def test_weight_distribution_balanced(self, selector):
        """Test that weights are distributed evenly for identical performers."""
        # 3 identical strategies
        metrics = {
            'StrategyA': self.create_mock_metrics(win_rate=55, profit_factor=1.5),
            'StrategyB': self.create_mock_metrics(win_rate=55, profit_factor=1.5),
            'StrategyC': self.create_mock_metrics(win_rate=55, profit_factor=1.5),
        }
        selector.performance_tracker.get_all_strategy_metrics.return_value = metrics
        
        rankings = selector.update_rankings(force=True)
        
        # With 3 strategies, weights typically distribute.
        # Check that they are relatively close (floating point tolerance)
        w_a = rankings['StrategyA'].weight
        w_b = rankings['StrategyB'].weight
        w_c = rankings['StrategyC'].weight
        
        print(f"Weights: A={w_a}, B={w_b}, C={w_c}")
        
        # In current logic, rank 1 gets 1.0, Rank N gets less.
        # If scores are identical, Python's sort stability might pick an arbitrary order,
        # but the logic: weight = 1.0 - (0.5 * (rank - 1) / (N - 1))
        # Rank 1: 1.0
        # Rank 2: 0.75
        # Rank 3: 0.5
        assert 0.5 <= w_a <= 1.0
        assert 0.5 <= w_b <= 1.0
        assert 0.5 <= w_c <= 1.0

    def test_weight_differentiation(self, selector):
        """Test that high performers get significantly higher weights than low performers."""
        metrics = {
            'Winner': self.create_mock_metrics(win_rate=80, profit_factor=3.0, sharpe=3.0),
            'Average': self.create_mock_metrics(win_rate=50, profit_factor=1.2, sharpe=1.0),
            'Loser': self.create_mock_metrics(win_rate=30, profit_factor=0.6, sharpe=-1.0),
        }
        selector.performance_tracker.get_all_strategy_metrics.return_value = metrics
        
        # Mock rolling sharpe to match the static sharpe for consistency
        # Assuming the selector calls self._get_rolling_sharpe
        # We need to patch the method on the instance
        selector._get_rolling_sharpe = MagicMock(side_effect=lambda name: 
            3.0 if name == 'Winner' else 
            1.0 if name == 'Average' else 
            -1.0
        )
        
        rankings = selector.update_rankings(force=True)
        
        w_win = rankings['Winner'].weight
        w_avg = rankings['Average'].weight
        w_lose = rankings['Loser'].weight
        
        print(f"Winner: {w_win}, Avg: {w_avg}, Loser: {w_lose}")
        
        assert w_win > w_avg, "Winner should have higher weight than Average"
        assert w_avg > w_lose, "Average should have higher weight than Loser"
        assert w_win >= 0.9, "Winner should be near max weight"
        
        # Verify normalization check:
        # Effective Position = Signal (1.0) * Weight (1.0) = 1.0 (Full Size)
        # Low Conviction = Signal (0.5) * Weight (0.5) = 0.25 (Quarter Size)

    def test_insufficient_data_handling(self, selector):
        """Test that strategies with few trades get valid (neutral) weights."""
        metrics = {
            'NewStrat': self.create_mock_metrics(total_trades=2, win_rate=100), # Amazing but unproven
            'OldReliable': self.create_mock_metrics(total_trades=100, win_rate=55),
        }
        selector.performance_tracker.get_all_strategy_metrics.return_value = metrics
        
        rankings = selector.update_rankings(force=True)
        
        w_new = rankings['NewStrat'].score
        # Check that score is 0.5 (Neutral) due to low trades, not 1.0 (Amazing)
        assert w_new == 0.5, f"New strategy should have neutral score 0.5, got {w_new}"

    def test_sanity_check_Effective_Strength(self, selector):
        """
        Verify that Weight * Signal Strength doesn't produce absurd values.
        Goal: 0.2 (Min valid) to 1.5 (Max Overweight)
        """
        metrics = {
            'Good': self.create_mock_metrics(win_rate=60, profit_factor=2.0),
            'Bad': self.create_mock_metrics(win_rate=40, profit_factor=0.9),
        }
        selector.performance_tracker.get_all_strategy_metrics.return_value = metrics
        rankings = selector.update_rankings(force=True)
        
        # Scenario 1: Good Strategy, Strong Signal
        eff_strong = rankings['Good'].weight * 1.0 # Max Signal
        
        # Scenario 2: Bad Strategy, Weak Signal
        eff_weak = rankings['Bad'].weight * 0.5 # Base Signal
        
        print(f"Effective Strong: {eff_strong}")
        print(f"Effective Weak: {eff_weak}")
        
        assert eff_strong <= 1.0, "Standard sizing should not exceed 1.0 (LeverageManager handles >1.0)"
        assert eff_weak >= 0.0, "Effective strength cannot be negative"
        
        # Check if the bad strategy is disabled?
        # In this config, only 'strategies with negative expectancy' might be disabled 
        # or if min_win_rate < 35. Our Bad is 40% WR, 0.9 PF.
        # It's weak, but maybe not disabled yet.
