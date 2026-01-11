
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta
from src.strategies.strategy_manager import StrategyManager

class TestGhostPnLMatching:
    
    @pytest.fixture
    def mock_deps(self):
        with patch('src.strategies.strategy_manager.PortfolioManager') as MockPM, \
             patch('src.strategies.strategy_manager.LeverageManager') as MockLM, \
             patch('src.strategies.strategy_manager.ExecutionEngine') as MockEE, \
             patch('src.strategies.strategy_manager.PerformanceTracker') as MockPT, \
             patch('src.strategies.strategy_manager.DynamicPairSelector') as MockDPS, \
             patch('src.strategies.strategy_manager.CorrelationManager') as MockCM, \
             patch('src.strategies.strategy_manager.StrategySelector') as MockSS:
            
            yield {
                'pm': MockPM,
                'lm': MockLM,
                'ee': MockEE,
                'pt': MockPT,
                'dps': MockDPS,
                'cm': MockCM,
                'ss': MockSS
            }

    def test_find_closing_fill_timestamp_skew(self, mock_deps):
        """
        Test that _find_closing_fill fails if local clock is ahead of server clock
        (entry_time > fill_time), causing 0.0 PnL fallback.
        """
        # Mock API
        mock_api = MagicMock()
        mock_api.get_user_fills.return_value = [
            {
                'coin': 'HBAR',
                'side': 'Sell',
                'px': '0.11828',
                'sz': '1000.0',
                'time': 1700000000000, # Server time: 1700000000 (s)
                'dir': 'Close Long'
            }
        ]
        
        # Strategy Manager (execution_engine will be mocked by patch)
        config = {
            'trading': {
                'strategies': {},
                'max_positions_percentage': 100,
                'base_currency': 'USDC',
                'order_timeout_minutes': 5,
                'enable_stale_order_cleanup': False,
                'position_sync_interval': 60,
                'enable_position_validation': False
            },
            'strategies': {'ohlcv_limit': 100, 'enabled': []}
        }
        
        sm = StrategyManager(config=config, market_api=mock_api)
        
        # Mock execution engine behavior if needed (accessed via sm.execution_engine)
        # Verify it uses the mock from patch
        assert isinstance(sm.execution_engine, MagicMock) or isinstance(sm.execution_engine, type(mock_deps['ee'].return_value))

        # Scenario: Local entry time was 5 seconds *after* the fill time
        server_fill_time_ms = 1700000000000
        server_fill_time_sec = server_fill_time_ms / 1000
        
        # Local time is 5s ahead
        entry_time_skewed = datetime.fromtimestamp(server_fill_time_sec + 5)
        
        # Test
        price, time, reason, _ = sm._find_closing_fill('HBAR', 'long', entry_time_skewed)
        
        # Expect SUCCESS (price > 0) with logic fix (skew tolerance)
        assert price == 0.11828, f"Should find fill despite timestamp skew. Got {price}"

    def test_find_closing_fill_side_casing(self, mock_deps):
        """
        Test that _find_closing_fill fails if API uses 'S' instead of 'Sell'.
        """
        mock_api = MagicMock()
        mock_api.get_user_fills.return_value = [
            {
                'coin': 'HBAR',
                'side': 'S', # Short form
                'px': '0.11828',
                'sz': '1000.0',
                'time': 1700000050000,
                'dir': 'Close Long'
            }
        ]
        
        config = {
            'trading': {
                'strategies': {},
                'max_positions_percentage': 100,
                'base_currency': 'USDC',
                'order_timeout_minutes': 5,
                'enable_stale_order_cleanup': False,
                'position_sync_interval': 60,
                'enable_position_validation': False
            },
            'strategies': {'ohlcv_limit': 100, 'enabled': []}
        }
        
        sm = StrategyManager(config=config, market_api=mock_api)
        
        entry_time = datetime.fromtimestamp(1700000000) # entry before fill
        
        # Test
        price, time, reason, _ = sm._find_closing_fill('HBAR', 'long', entry_time)
        
        # Expect SUCCESS with fix applied
        assert price > 0.1, "Should find fill despite side casing difference"
        assert price == 0.11828, "Should match correct price"

    def test_find_closing_fill_limit_depth(self, mock_deps):
        """Test finding a fill deep in history (beyond 100)."""
        mock_api = MagicMock()
        # Generate 200 fills
        fills = []
        base_time = 1700000000000
        for i in range(200):
            fills.append({
                'coin': 'BTC', 'side': 'B', 'px': '50000', 'sz': '1', 'time': base_time + i*1000
            })
        
        # Target fill at index 150 (older than top 100)
        fills.append({
                'coin': 'HBAR', 'side': 'S', 'px': '0.11828', 'sz': '1000.0', 'time': base_time - 1000, 
                'dir': 'Close Long'
        })
        
        # Sort descending (api behavior)
        fills.sort(key=lambda x: x['time'], reverse=True)
        
        mock_api.get_user_fills.return_value = fills
        
        config = {
            'trading': {
                'strategies': {},
                'max_positions_percentage': 100,
                'base_currency': 'USDC',
                'order_timeout_minutes': 5,
                'enable_stale_order_cleanup': False,
                'position_sync_interval': 60,
                'enable_position_validation': False
            },
            'strategies': {'ohlcv_limit': 100, 'enabled': []}
        }
        sm = StrategyManager(config=config, market_api=mock_api)
        
        entry_time = datetime.fromtimestamp((base_time - 5000)/1000)
        price, _, _, _ = sm._find_closing_fill('HBAR', 'long', entry_time)
        
        assert price == 0.11828, "Should find deep fill with increased limit"
