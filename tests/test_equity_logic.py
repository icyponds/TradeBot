import pytest
from unittest.mock import MagicMock, patch
from src.api.hyperliquid_api import HyperliquidAPI

class TestEquityAggregation:
    
    @pytest.fixture
    def mock_api(self):
        # Create API with mocks and full config
        with patch('eth_account.Account'), \
             patch('hyperliquid.exchange.Exchange'), \
             patch('hyperliquid.info.Info'):
             
            config = {
                'wallet': {'private_key': '0x123', 'address': '0x123'},
                'api': {
                    'base_url': 'https://api.hyperliquid.xyz',
                    'private_key': '0x123',
                    'wallet_address': '0x123'
                },
                'hip3': {'enabled': True, 'perp_dexs': ["", "Hypurr"]}
            }
            api = HyperliquidAPI(config)
            api.exchange = MagicMock()
            api.info = MagicMock()
            # These tests exercise the LEGACY (split spot/perp) aggregation
            # path; pin the mode so they don't depend on MagicMock coercion
            # of the unified-account abstraction query.
            api._abstraction_mode = 'disabled'
            return api

    def test_shared_collateral_aggregation(self, mock_api):
        """
        Verify equity aggregation when collateral (USDC) is shared.
        Dex "" (native): Equity 1000 (900 Cash + 100 PnL)
        Dex "Hypurr" (HIP-3): Equity 950 (900 Cash + 50 Spot Value)
        Expected Total: 1000 + 50 = 1050 (Cash counted once)
        """
        mock_api.perp_dexs = ["", "Hypurr"]  # String dex names
        
        def mock_user_state(address, dex=""):
            if dex == "":  # Native dex
                return {
                    'marginSummary': {
                        'accountValue': '1000.0',
                        'totalMarginUsed': '100.0',
                        'totalUnrealizedPnl': '100.0',
                        'withdrawable': '900.0'
                    }
                }
            elif dex == "Hypurr":  # HIP-3 dex
                return {
                    'marginSummary': {
                        'accountValue': '950.0', # 900 Cash + 50 Spot
                        'totalMarginUsed': '50.0', # Spot value usually shows as margin used or separate
                        'totalUnrealizedPnl': '0.0',
                        'withdrawable': '900.0' # Same cash balance
                    }
                }
            return {}
            
        mock_api.info.user_state.side_effect = mock_user_state
        
        balance = mock_api.get_account_balance()
        
        # Expected:
        # Dex "" Equity: 1000
        # Dex "Hypurr" Contribution: 950 - 900 (shared cash) = 50
        # Total: 1050
        assert balance['total_equity'] == 1050.0
        assert balance['unrealized_pnl'] == 100.0 # Only native dex has PnL here
        assert balance['used_margin'] == 150.0 # 100 + 50

    def test_segregated_collateral_aggregation(self, mock_api):
        """
        Verify equity aggregation when collateral is NOT shared.
        Dex "" (native): Equity 1000 (900 Cash + 100 PnL)
        Dex "Hypurr" (HIP-3): Equity 500 (500 Cash + 0 PnL) - Completely separate
        Expected Total: 1000 + 500 = 1500
        """
        mock_api.perp_dexs = ["", "Hypurr"]  # String dex names
        
        def mock_user_state(address, dex=""):
            if dex == "":  # Native dex
                return {
                    'marginSummary': {
                        'accountValue': '1000.0',
                        'totalMarginUsed': '100.0',
                        'totalUnrealizedPnl': '100.0',
                        'withdrawable': '900.0'
                    }
                }
            elif dex == "Hypurr":  # HIP-3 dex
                return {
                    'marginSummary': {
                        'accountValue': '500.0',
                        'totalMarginUsed': '0.0',
                        'totalUnrealizedPnl': '0.0',
                        'withdrawable': '500.0' # Different cash balance
                    }
                }
            return {}
            
        mock_api.info.user_state.side_effect = mock_user_state
        
        balance = mock_api.get_account_balance()
        
        # Expected:
        # Dex "" Equity: 1000
        # Dex "Hypurr" Contribution: 500 (full amount since withdrawable differs)
        # Total: 1500
        assert balance['total_equity'] == 1500.0
