
import pytest
from unittest.mock import MagicMock, patch
from src.api.hyperliquid_api import HyperliquidAPI

class TestHyperliquidSubscription:
    """Test suite for HyperliquidAPI.subscribe_symbol timeframes."""

    @pytest.fixture
    def api(self):
        """Mocked HyperliquidAPI instance."""
        config = {
            "api": {"base_url": "mock", "wallet_address": "mock", "private_key": "mock"},
            "trading": {"base_currency": "USDC"}
        }
        with patch('hyperliquid.exchange.Exchange'), \
             patch('hyperliquid.info.Info'), \
             patch('eth_account.Account'):
            api = HyperliquidAPI(config)
            
            # Mock executor to run immediately (synchronous)
            api._persistence_executor = MagicMock()
            api._persistence_executor.submit = lambda func, *args, **kwargs: func(*args, **kwargs)
            
            # Mock ohlcv_cache
            api.ohlcv_cache = MagicMock()
            
            return api

    def test_default_subscription_timeframes(self, api):
        """Test default logical fallback when no timeframes provided."""
        symbol = "BTC"
        
        # We need _initialize_live_data to actually CALL _finalize_subscription
        # But avoid network stuff.
        # Strategy: Don't mock _initialize_live_data, but mock internal steps if needed.
        # Or mock _initialize_live_data side_effect to call _finalize_subscription manually.
        
        def mock_init(sym, api_sym, required_timeframes=None, max_retries=2):
            api._finalize_subscription(sym, required_timeframes)
            return True

        with patch.object(api, '_get_asset_info_for_symbol', return_value={'name': 'BTC'}), \
             patch.object(api, '_initialize_live_data', side_effect=mock_init):
            
            api.subscribe_symbol(symbol)
            
            # Should NOT initialize any timeframes by default (strictly opt-in)
            calls = api.ohlcv_cache.ensure_timeframe.call_args_list
            tfs = [c.args[1] for c in calls]
            
            assert len(tfs) == 0, f"Should verify no persistence by default. Got: {tfs}"
            
            assert '15m' not in tfs
            assert '1h' not in tfs
            assert '1d' not in tfs
            assert '5m' not in tfs, f"Should avoid 5m default. Got: {tfs}"
            assert '4h' not in tfs, f"Should avoid 4h default. Got: {tfs}"

    def test_explicit_subscription_timeframes(self, api):
        """Test passing explicit timeframes passes through correctly."""
        symbol = "ETH"
        required = ['1h', '4h']
        
        def mock_init(sym, api_sym, required_timeframes=None, max_retries=2):
            api._finalize_subscription(sym, required_timeframes)
            return True

        with patch.object(api, '_get_asset_info_for_symbol', return_value={'name': 'ETH'}), \
             patch.object(api, '_initialize_live_data', side_effect=mock_init):
            
            api.subscribe_symbol(symbol, required_timeframes=required)
            
            calls = api.ohlcv_cache.ensure_timeframe.call_args_list
            tfs = [c.args[1] for c in calls]
            
            assert '1h' in tfs
            assert '4h' in tfs
            assert len(tfs) == 2
            assert '15m' not in tfs # Should not add defaults if explicit given
            assert '5m' not in tfs

    def test_mixed_subscription(self, api):
        """Test with single timeframe."""
        symbol = "SOL"
        required = ['5m'] # Actually asking for 5m
        
        def mock_init(sym, api_sym, required_timeframes=None, max_retries=2):
            api._finalize_subscription(sym, required_timeframes)
            return True

        with patch.object(api, '_get_asset_info_for_symbol', return_value={'name': 'SOL'}), \
             patch.object(api, '_initialize_live_data', side_effect=mock_init):
            
            api.subscribe_symbol(symbol, required_timeframes=required)
            
            calls = api.ohlcv_cache.ensure_timeframe.call_args_list
            tfs = [c.args[1] for c in calls]
            
            assert '5m' in tfs
            assert len(tfs) == 1
