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
                'hip3': {'enabled': True, 'perp_dexs': [0]}
            }
            api = HyperliquidAPI(config)
            api.exchange = MagicMock()
            api.info = MagicMock()
            return api

    def test_set_dead_mans_switch_signature(self, mock_api):
        """Test that set_dead_mans_switch calls exchange.post with correct args (no nonce)."""
        
        # 1. Call set_dead_mans_switch
        result = mock_api.set_dead_mans_switch(timeout_seconds=30)
        
        # 2. Verify Result
        assert result is True
        
        # 3. Verify Mock Call
        # Expected: exchange.post("/exchange", payload)
        # Payload should be {'type': 'mkt', 'action': {'type': 'setDms', 'timeout': 30000}}
        # CRITICAL: Confirm 'nonce' kwarg is NOT passed
        
        assert mock_api.exchange.post.called
        
        args, kwargs = mock_api.exchange.post.call_args
        
        # Check Positional Args
        assert args[0] == "/exchange"
        
        payload = args[1]
        payload = args[1]
        assert payload['type'] == 'scheduleCancel'
        # Verify time is approximately now + 30s (in ms)
        # We can't match exact ms, but can check it's an int and > 0
        assert isinstance(payload['time'], int)
        assert payload['time'] > 0
        
        # Check Keyword Args
        # Ensure 'nonce' is NOT in kwargs
        assert 'nonce' not in kwargs, "Exchange.post was called with invalid 'nonce' argument!"
