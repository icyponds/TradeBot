
import unittest
import time
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta
from src.api.hyperliquid_api import HyperliquidAPI

class TestDynamicCooldownsFast(unittest.TestCase):
    def setUp(self):
        self.config = {
            'api': {
                'base_url': 'mock',
                'private_key': '', # Empty to avoid validation in some paths, but patched out anyway
                'wallet_address': '0xABC',
                'key_created_at': '',
                'key_expiration_date': ''
            }
        }

    @patch('src.api.hyperliquid_api.HyperliquidAPI._init_sdk_clients')
    @patch('src.api.hyperliquid_api.HyperliquidAPI._enable_websocket')
    def test_dynamic_cooldowns(self, mock_ws, mock_init):
        """Test all cooldown logic without network calls."""
        # Setup
        mock_init.return_value = None # Bypass SDK init
        api = HyperliquidAPI(self.config)
        api.logger = MagicMock()
        
        # --- TEST 1: CRITICAL (1 hour) ---
        expiry_date = (datetime.now() + timedelta(days=4)).strftime("%Y-%m-%d")
        api.config['api']['key_expiration_date'] = expiry_date
        
        # Reset
        api._last_expiration_check = 0
        
        # Call 1 -> Critical Log
        api.check_api_key_expiration()
        self.assertEqual(api.logger.critical.call_count, 1)
        
        # Call 2 (after 30 mins) -> No Log
        api._last_expiration_check = time.time() - 1800
        api.check_api_key_expiration()
        self.assertEqual(api.logger.critical.call_count, 1)
        
        # Call 3 (after 1h + 1s) -> New Log
        api._last_expiration_check = time.time() - 3601
        api.check_api_key_expiration()
        self.assertEqual(api.logger.critical.call_count, 2)
        
        # --- TEST 2: WARNING (6 hours) ---
        # Reset logger and config
        api.logger.reset_mock()
        expiry_date = (datetime.now() + timedelta(days=20)).strftime("%Y-%m-%d")
        api.config['api']['key_expiration_date'] = expiry_date
        
        # Call 1 -> Warning Log
        api._last_expiration_check = 0
        api.check_api_key_expiration()
        self.assertEqual(api.logger.warning.call_count, 1)
        
        # Call 2 (after 4 hours) -> No Log
        api._last_expiration_check = time.time() - 14400
        api.check_api_key_expiration()
        self.assertEqual(api.logger.warning.call_count, 1)
        
        # Call 3 (after 6h + 1s) -> New Log
        api._last_expiration_check = time.time() - 21601
        api.check_api_key_expiration()
        self.assertEqual(api.logger.warning.call_count, 2)

        # --- TEST 3: NORMAL (24 hours) ---
        api.logger.reset_mock()
        expiry_date = (datetime.now() + timedelta(days=60)).strftime("%Y-%m-%d")
        api.config['api']['key_expiration_date'] = expiry_date
        
        # Call 1 -> Info Log
        api._last_expiration_check = 0
        api.check_api_key_expiration()
        self.assertEqual(api.logger.info.call_count, 1)
        
        # Call 2 (after 23 hours) -> No Log
        api._last_expiration_check = time.time() - (23 * 3600)
        api.check_api_key_expiration()
        self.assertEqual(api.logger.info.call_count, 1)
        
        # Call 3 (after 24h + 1s) -> New Log
        api._last_expiration_check = time.time() - 86401
        api.check_api_key_expiration()
        self.assertEqual(api.logger.info.call_count, 2)

if __name__ == '__main__':
    unittest.main()
