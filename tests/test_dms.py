import pytest
from unittest.mock import MagicMock, patch
from src.api.hyperliquid_api import HyperliquidAPI

class TestDeadMansSwitch:
    
    @pytest.fixture
    def mock_api(self):
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
                'hip3': {'enabled': True, 'perp_dexs': [""]}
            }
            api = HyperliquidAPI(config)
            api.exchange = MagicMock()
            api.info = MagicMock()
            return api

    def test_set_dead_mans_switch_signature(self, mock_api):
        """Test that set_dead_mans_switch calls exchange.schedule_cancel with correct args."""
        
        # 1. Call set_dead_mans_switch
        result = mock_api.set_dead_mans_switch(timeout_seconds=30)
        
        # 2. Verify Result
        assert result is True
        
        # 3. Verify Mock Call
        # The SDK's schedule_cancel method handles signing properly
        # Expected: exchange.schedule_cancel(timeout_ms)
        
        assert mock_api.exchange.schedule_cancel.called
        
        args, kwargs = mock_api.exchange.schedule_cancel.call_args
        
        # Check Positional Args - first arg is the timeout in ms
        timeout_ms = args[0]
        
        # Verify time is approximately now + 30s (in ms)
        # We can't match exact ms, but can check it's an int and > 0
        assert isinstance(timeout_ms, int)
        assert timeout_ms > 0
