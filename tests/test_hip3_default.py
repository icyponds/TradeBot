import pytest
import sys
from unittest.mock import MagicMock, patch

# Mock the external modules BEFORE importing the API
# This ensures that when the API does 'from hyperliquid.info import Info', it gets our mock
sys.modules['hyperliquid'] = MagicMock()
sys.modules['hyperliquid.info'] = MagicMock()
sys.modules['hyperliquid.exchange'] = MagicMock()
sys.modules['eth_account'] = MagicMock()

from src.api.hyperliquid_api import HyperliquidAPI

class TestHip3Default:
    
    @pytest.fixture
    def mock_config(self):
        return {
            'api': {
                'base_url': 'https://api.hyperliquid.xyz',
                'private_key': '00'*32,
                'wallet_address': '0x'+'00'*20,
            }
        }
        
    @patch('hyperliquid.info.Info')
    @patch('hyperliquid.exchange.Exchange')
    @patch('eth_account.Account')
    def test_auto_discovery_and_query(self, mock_account, mock_exchange, mock_info_cls, mock_config):
        """Test that HIP-3 is enabled by default and queries multiple DEXs."""
        
        # Setup mocks
        mock_info_instance = MagicMock()
        mock_info_cls.return_value = mock_info_instance
        
        # Mock class-level _discover_perp_dexs to simulate finding multiple universes
        # We patch the METHOD on the class src.api.hyperliquid_api.HyperliquidAPI
        with patch('src.api.hyperliquid_api.HyperliquidAPI._discover_perp_dexs', return_value=[0, 1]):
            api = HyperliquidAPI(mock_config)
            
            # 1. Verify HIP-3 enabled flag
            assert api.hip3_enabled is True
            assert api.perp_dexs == [0, 1]
            
            # 2. Test get_positions queries both indices (0 and 1)
            # Querying user_state(address, dex=dex_index)
            mock_info_instance.user_state.side_effect = [
                # Response for DEX 0 (Native)
                {'assetPositions': [{'position': {'coin': 'BTC', 'szi': '1.0', 'entryPx': '50000'}}]},
                # Response for DEX 1 (HIP-3)
                {'assetPositions': [{'position': {'coin': 'TSLA', 'szi': '10.0', 'entryPx': '200'}}]}
            ]
            
            positions = api.get_positions()
            
            # Verify calls
            assert mock_info_instance.user_state.call_count == 2
            mock_info_instance.user_state.assert_any_call(api.public_account_address, dex=0)
            mock_info_instance.user_state.assert_any_call(api.public_account_address, dex=1)
            
            # Verify results aggregated
            assert len(positions) == 2
            symbols = {p['symbol'] for p in positions}
            assert 'BTC' in symbols
            assert '1:TSLA' in symbols # Prefix is applied for index 1
            
    @patch('hyperliquid.info.Info')
    @patch('hyperliquid.exchange.Exchange')
    @patch('eth_account.Account')
    def test_discovery_fallback(self, mock_account, mock_exchange, mock_info_cls, mock_config):
        """Test fallback to [0] if discovery fails."""
         # Mock discovery failure
        with patch('src.api.hyperliquid_api.HyperliquidAPI._discover_perp_dexs', side_effect=Exception("API Error")):
             api = HyperliquidAPI(mock_config)
             
             # Should default to [0] and not crash
             assert api.perp_dexs == [0]
             assert api.hip3_enabled is True # Still enabled in intent
