
import pytest
from unittest.mock import MagicMock, patch
import threading
import time
from src.utils.pair_selector import DynamicPairSelector, AssetMetrics

class TestPairSelectorTimeframes:
    """Test suite for DynamicPairSelector background timeframe fetching."""
    
    @pytest.fixture
    def mock_deps(self):
        """Setup mock dependencies."""
        api = MagicMock()
        api.repairer = MagicMock()  # Has repairer
        
        # Strategy Manager with get_required_timeframes
        sm = MagicMock()
        sm.get_required_timeframes.return_value = ['1h']
        
        config = {
            'trading': {
                'dynamic_pair_selection': True,
                'min_open_interest': 1000,
                'scan_interval_minutes': 60,
                'max_pairs_to_trade': 3,
                'excluded_assets': [],
                'included_assets': [],
            },
            'hip3': {'enabled': False},
            'spot': {'enabled': False},
            'pair_selection': {'mode': 'sophisticated'}
        }
        
        return api, sm, config

    def test_background_fetcher_uses_dynamic_timeframes(self, mock_deps):
        """
        Verify that _background_data_fetcher calls get_ohlcv ONLY for timeframes
        returned by strategy_manager.get_required_timeframes().
        """
        api, sm, config = mock_deps
        
        # Instantiate Selector
        selector = DynamicPairSelector(config, api, strategy_manager=sm)
        
        # Prepare state to trigger "needs_warmup" logic
        # 1. Add asset to queue
        asset = {'name': 'TEST_ASSET', 'market_type': 'perp', 'volume24h': 100000}
        selector.backfill_queue.append(asset)
        
        # 2. Mock _try_add_to_trading_pairs to simulate adding to pool
        def mock_add_to_pool(a):
            selector.selected_pairs.append(a['name'])
            
        with patch.object(selector, '_try_add_to_trading_pairs', side_effect=mock_add_to_pool), \
             patch.object(selector, '_get_price_history'):
            
            # 3. START background fetcher for short duration
            selector._backfill_running = True
            
            # Run the fetcher in a separate thread so we can stop it
            t = threading.Thread(target=selector._background_data_fetcher)
            t.start()
            
            # Allow one iteration (wait for queue to empty)
            start = time.time()
            while selector.backfill_queue and time.time() - start < 2:
                time.sleep(0.1)
                
            # Stop fetcher
            selector._backfill_running = False
            t.join(timeout=2)
        
        # 4. ASSERTIONS
        
        # Check Strategy Manager was consulted
        sm.get_required_timeframes.assert_called()
        
        # Check get_ohlcv called for '1h' (from mock)
        # Should NOT be called for '5m', '15m', '4h' unless they're in the list
        
        # Collect all timeframes passed to get_ohlcv
        calls = api.get_ohlcv.call_args_list
        fetch_tfs = [c.args[1] for c in calls if c.args[0] == 'TEST_ASSET']
        
        assert '1h' in fetch_tfs, f"Should have fetched '1h'. Got: {fetch_tfs}"
        assert '5m' not in fetch_tfs, "Should NOT have fetched '5m' (not in requirements)"
        
        # Check repairer called with correct timeframes
        api.repairer.process_asset.assert_called_with('TEST_ASSET', timeframes=['1h'])

    def test_background_fetcher_fallback_defaults(self, mock_deps):
        """Verify fallback to ['15m', '1h'] if StrategyManager missing capabilities."""
        api, sm, config = mock_deps
        
        # Remove get_required_timeframes capability
        del sm.get_required_timeframes
        
        selector = DynamicPairSelector(config, api, strategy_manager=sm)
        
        # Queue item
        asset = {'name': 'DEFAULT_TEST', 'market_type': 'perp'}
        selector.backfill_queue.append(asset)
        
        # Mock pool addition
        with patch.object(selector, '_try_add_to_trading_pairs', side_effect=lambda a: selector.selected_pairs.append(a['name'])), \
             patch.object(selector, '_get_price_history'):
             
            selector._backfill_running = True
            t = threading.Thread(target=selector._background_data_fetcher)
            t.start()
            
            time.sleep(0.2) # Let it process
            selector._backfill_running = False
            t.join(timeout=2)
            
        # Assert fallback calls
        calls = api.get_ohlcv.call_args_list
        fetch_tfs = [c.args[1] for c in calls if c.args[0] == 'DEFAULT_TEST']
        
        assert '15m' in fetch_tfs and '1h' in fetch_tfs
        assert '5m' not in fetch_tfs, f"Should NOT have fetched '5m'. Got: {set(fetch_tfs)}"
        assert '4h' not in fetch_tfs, f"Should NOT have fetched '4h'. Got: {set(fetch_tfs)}"
        assert set(fetch_tfs) == {'15m', '1h'}, f"Should only fetch defaults. Got: {set(fetch_tfs)}"
