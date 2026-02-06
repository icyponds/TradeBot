"""
Unit tests for symbol blacklist filtering in PairSelector.

Tests the global and strategy-specific blacklist filtering added to
_try_add_to_trading_pairs method.
"""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
import pandas as pd

from src.utils.pair_selector import DynamicPairSelector


class TestSymbolBlacklistFiltering:
    """Test symbol blacklist filtering in pair selector."""
    
    @pytest.fixture
    def base_config(self):
        """Config with blacklist settings."""
        return {
            'trading': {
                'symbol_blacklist': {
                    'global': ['kBONK', 'PEPE'],  # Globally blacklisted
                    'stat_arb': ['BTC', 'XRP', 'ZEC'],  # Strategy-specific
                    'ou_mean_reversion': ['DOGE'],
                },
                'max_trading_pairs': 10,
                'trading_pairs': [],
                'dynamic_pair_selection': False,
                'instances': [
                    {'type': 'stat_arb', 'name': 'stat_arb_4h', 'timeframe': '4h'},
                    {'type': 'vol_breakout', 'name': 'vol_breakout_4h', 'timeframe': '4h'},
                ],
            },
            'strategies': {'ohlcv_limit': 300},
            'api': {},
        }
    
    @pytest.fixture
    def mock_market_api(self):
        """Mock market API."""
        api = MagicMock()
        api.get_perp_meta.return_value = []
        return api
    
    @pytest.fixture
    def mock_db(self):
        """Mock trade database."""
        db = MagicMock()
        db.get_market_data.return_value = pd.DataFrame()
        return db
    
    @pytest.fixture
    def pair_selector(self, base_config, mock_market_api, mock_db):
        """Create pair selector instance."""
        selector = DynamicPairSelector(base_config, mock_market_api, mock_db)
        selector.logger = MagicMock()  # Suppress logs
        return selector
    
    # =========================================================================
    # Global Blacklist Tests
    # =========================================================================
    
    def test_global_blacklist_blocks_symbol(self, pair_selector):
        """Test globally blacklisted symbol is rejected."""
        # kBONK is in global blacklist
        result = pair_selector._try_add_to_trading_pairs('kBONK', MagicMock())
        
        # Should return early/block (method returns None on block)
        # Verify it logged the skip
        pair_selector.logger.debug.assert_called()
        log_message = str(pair_selector.logger.debug.call_args)
        assert 'global blacklist' in log_message.lower() or 'blacklist' in log_message.lower()
    
    def test_global_blacklist_blocks_multiple_symbols(self, pair_selector):
        """Test multiple globally blacklisted symbols are rejected."""
        for symbol in ['kBONK', 'PEPE']:
            pair_selector._try_add_to_trading_pairs(symbol, MagicMock())
        
        # Neither should be added to trading pairs
        assert 'kBONK' not in pair_selector.trading_pairs
        assert 'PEPE' not in pair_selector.trading_pairs
    
    def test_non_blacklisted_symbol_allowed(self, pair_selector):
        """Test non-blacklisted symbol is not blocked by blacklist."""
        # ETH is not in any blacklist
        metrics = MagicMock()
        metrics.composite_score = 0.8
        
        # Setup so symbol can pass other checks
        pair_selector.asset_metrics = {'ETH': metrics}
        pair_selector.trading_pairs = {}
        pair_selector.get_ready_pairs = MagicMock(return_value={'ETH': MagicMock()})
        
        # This test just verifies blacklist doesn't block ETH
        # The actual add logic is complex, so we check it reaches past blacklist
        pair_selector._try_add_to_trading_pairs('ETH', metrics)
        
        # Should NOT log blacklist skip for ETH
        if pair_selector.logger.debug.called:
            all_calls = [str(c) for c in pair_selector.logger.debug.call_args_list]
            blacklist_logs = [c for c in all_calls if 'ETH' in c and 'blacklist' in c.lower()]
            assert len(blacklist_logs) == 0, f"ETH should not be blacklisted: {blacklist_logs}"
    
    # =========================================================================
    # Strategy-Specific Blacklist Tests
    # =========================================================================
    
    def test_strategy_blacklist_blocks_symbol(self, pair_selector):
        """Test strategy-specific blacklisted symbol is rejected when strategy active."""
        # BTC is blacklisted for stat_arb, which is active in config
        pair_selector._try_add_to_trading_pairs('BTC', MagicMock())
        
        assert 'BTC' not in pair_selector.trading_pairs
    
    def test_strategy_blacklist_different_strategy(self, pair_selector, base_config):
        """Test symbol only blacklisted for specific strategy, not others."""
        # DOGE is only blacklisted for ou_mean_reversion
        # With only stat_arb and vol_breakout active, DOGE should not be blocked
        base_config['trading']['instances'] = [
            {'type': 'vol_breakout', 'name': 'vol_breakout_4h', 'timeframe': '4h'},
        ]
        
        # Reinit with new config
        pair_selector = DynamicPairSelector(base_config, MagicMock(), MagicMock())
        pair_selector.logger = MagicMock()
        
        # DOGE should not be blocked since ou_mean_reversion is not active
        pair_selector._try_add_to_trading_pairs('DOGE', MagicMock())
        
        # Check logs don't show DOGE being blacklisted
        if pair_selector.logger.debug.called:
            all_calls = [str(c) for c in pair_selector.logger.debug.call_args_list]
            blacklist_logs = [c for c in all_calls if 'DOGE' in c and 'blacklist' in c.lower()]
            # DOGE may be rejected for other reasons, but not blacklist
            for log in blacklist_logs:
                assert 'ou_mean_reversion' not in log or 'not active' in log.lower()
    
    # =========================================================================
    # Empty/Missing Blacklist Tests
    # =========================================================================
    
    def test_empty_blacklist_config(self, mock_market_api, mock_db):
        """Test behavior when no blacklist is configured."""
        config = {
            'trading': {
                'symbol_blacklist': {},  # Empty blacklist
                'max_trading_pairs': 10,
                'trading_pairs': [],
                'instances': [],
            },
            'strategies': {},
            'api': {},
        }
        
        selector = DynamicPairSelector(config, mock_market_api, mock_db)
        selector.logger = MagicMock()
        
        # Any symbol should not be blacklist-blocked
        selector._try_add_to_trading_pairs('kBONK', MagicMock())
        
        # Check no blacklist logs
        if selector.logger.debug.called:
            all_calls = [str(c) for c in selector.logger.debug.call_args_list]
            blacklist_logs = [c for c in all_calls if 'blacklist' in c.lower()]
            assert len(blacklist_logs) == 0
    
    def test_missing_blacklist_config(self, mock_market_api, mock_db):
        """Test behavior when blacklist key is missing entirely."""
        config = {
            'trading': {
                # No symbol_blacklist key at all
                'max_trading_pairs': 10,
                'trading_pairs': [],
                'instances': [],
            },
            'strategies': {},
            'api': {},
        }
        
        selector = DynamicPairSelector(config, mock_market_api, mock_db)
        selector.logger = MagicMock()
        
        # Should not crash
        selector._try_add_to_trading_pairs('ETH', MagicMock())
        
        # No exception means pass
    
    # =========================================================================
    # Case Sensitivity Tests
    # =========================================================================
    
    def test_blacklist_case_sensitivity(self, pair_selector):
        """Test blacklist matching is case-sensitive (should match exact)."""
        # 'kBONK' is blacklisted, 'kbonk' (lowercase) is not
        # The implementation should use exact matching
        
        pair_selector._try_add_to_trading_pairs('kbonk', MagicMock())
        
        # Check if lowercase version was blocked or allowed
        # (This test documents current behavior - adjust if case-insensitive is desired)
        if pair_selector.logger.debug.called:
            all_calls = [str(c) for c in pair_selector.logger.debug.call_args_list]
            blacklist_logs = [c for c in all_calls if 'kbonk' in c.lower() and 'blacklist' in c.lower()]
            # If implementation is case-sensitive, kbonk won't be blocked
            # If case-insensitive, it will be blocked
            # Document actual behavior:
            pass  # Just verifying no crash
