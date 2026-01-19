
import unittest
from unittest.mock import MagicMock, patch
from src.strategies.strategy_manager import StrategyManager

class TestStrategyManagerTimeframes(unittest.TestCase):
    """Test suite for StrategyManager.get_required_timeframes optimization."""
    
    def setUp(self):
        # Patch __init__ to avoid complex initialization
        with patch.object(StrategyManager, '__init__', lambda self, *args, **kwargs: None):
            self.manager = StrategyManager.__new__(StrategyManager)
            self.manager.strategies = {}

    def test_no_strategies_returns_empty(self):
        """Test that no strategies results in no timeframes (no forced 5m)."""
        self.manager.strategies = {}
        
        required = self.manager.get_required_timeframes()
        self.assertEqual(required, [], "Should return empty list for no strategies")

    def test_single_strategy_timeframe(self):
        """Test that single strategy timeframe is returned."""
        mock_strat = MagicMock()
        mock_strat.timeframe = '1h'
        self.manager.strategies = {'strat1': mock_strat}
        
        required = self.manager.get_required_timeframes()
        self.assertEqual(required, ['1h'])

    def test_exclude_none_timeframes(self):
        """Test that strategies without timeframes are ignored."""
        mock_strat_valid = MagicMock()
        mock_strat_valid.timeframe = '1h'
        
        mock_strat_invalid = MagicMock()
        mock_strat_invalid.timeframe = None
        
        self.manager.strategies = {
            'valid': mock_strat_valid,
            'invalid': mock_strat_invalid
        }
        
        required = self.manager.get_required_timeframes()
        self.assertEqual(required, ['1h'])

    def test_mixed_strategies_timeframes(self):
        """Test aggregation of multiple strategies."""
        s1 = MagicMock(); s1.timeframe = '1h'
        s2 = MagicMock(); s2.timeframe = '15m'
        s3 = MagicMock(); s3.timeframe = '4h'
        
        self.manager.strategies = {'s1': s1, 's2': s2, 's3': s3}
        
        required = self.manager.get_required_timeframes()
        # Should be sorted per tf_order logic
        self.assertEqual(required, ['15m', '1h', '4h'])

    def test_respects_explicit_5m_strategy(self):
        """Test that 5m IS included if a strategy explicitly requests it."""
        s1 = MagicMock(); s1.timeframe = '5m'
        self.manager.strategies = {'s1': s1}
        
        required = self.manager.get_required_timeframes()
        self.assertEqual(required, ['5m'])

if __name__ == '__main__':
    unittest.main()
