
import unittest
from unittest.mock import MagicMock
from datetime import datetime, timedelta
from src.api.hyperliquid_api import HyperliquidAPI

class TestDynamicCooldowns(unittest.TestCase):
    def setUp(self):
        self.config = {
            'api': {
                'base_url': 'https://api.hyperliquid.xyz',
                'private_key': '0x123',
                'wallet_address': '0xABC',
                'key_created_at': '',
                'key_expiration_date': ''
            }
        }

    def test_critical_urgency_1h_cooldown(self):
        """Test CRITICAL urgency (<7 days) has 1 hour cooldown."""
        # Set expiry to 4 days from now (Critical)
        expiry_date = (datetime.now() + timedelta(days=4)).strftime("%Y-%m-%d")
        self.config['api']['key_expiration_date'] = expiry_date
        
        api = HyperliquidAPI(self.config)
        api.logger = MagicMock()
        api._last_expiration_check = 0
        
        # 1. First call -> SHOULD Log Critical
        api.check_api_key_expiration()
        self.assertEqual(api.logger.critical.call_count, 1)
        
        # 2. Call after 30 mins -> Should NOT Log (cooldown)
        api.check_api_key_expiration() # time not advanced enough
        self.assertEqual(api.logger.critical.call_count, 1)

        # 3. Call after 1h + 1s -> SHOULD Log again
        # Simulate time passing by modifying last check timestamp
        api._last_expiration_check -= 3601 
        api.check_api_key_expiration()
        self.assertEqual(api.logger.critical.call_count, 2)

    def test_warning_urgency_6h_cooldown(self):
        """Test WARNING urgency (<30 days) has 6 hour cooldown."""
        # Set expiry to 20 days from now (Warning)
        expiry_date = (datetime.now() + timedelta(days=20)).strftime("%Y-%m-%d")
        self.config['api']['key_expiration_date'] = expiry_date
        
        api = HyperliquidAPI(self.config)
        api.logger = MagicMock()
        api._last_expiration_check = 0
        
        # 1. First call -> SHOULD Log Warning
        api.check_api_key_expiration()
        self.assertEqual(api.logger.warning.call_count, 1)
        
        # 2. Call after 2 hours -> Should NOT Log
        api._last_expiration_check -= 7200 # 2 hours
        api.check_api_key_expiration() 
        self.assertEqual(api.logger.warning.call_count, 1)

        # 3. Call after 6h + 1s -> SHOULD Log again
        api._last_expiration_check -= 21601 # 6h + 1s (relative to now)
        # Note: logic relies on now - last > cooldown. 
        # Actually in test steps 2 modified self._last_check, 
        # so for step 3 we just need to ensure now - (modified_val_step2 - delta) > 21600.
        # Simpler way: just force reset last check to "6h ago"
        import time
        api._last_expiration_check = time.time() - 21610
        
        api.check_api_key_expiration()
        self.assertEqual(api.logger.warning.call_count, 2)

    def test_normal_urgency_24h_cooldown(self):
        """Test NORMAL urgency (>30 days) has 24 hour cooldown."""
        # Set expiry to 60 days from now (Normal)
        expiry_date = (datetime.now() + timedelta(days=60)).strftime("%Y-%m-%d")
        self.config['api']['key_expiration_date'] = expiry_date
        
        api = HyperliquidAPI(self.config)
        api.logger = MagicMock()
        api._last_expiration_check = 0
        
        # 1. First call -> SHOULD Log Info
        api.check_api_key_expiration()
        self.assertEqual(api.logger.info.call_count, 1) # First log is Info
        
        # 2. Call after 12 hours -> Should NOT Log
        import time
        api._last_expiration_check = time.time() - (12 * 3600)
        api.check_api_key_expiration()
        self.assertEqual(api.logger.info.call_count, 1)

        # 3. Call after 24h + 1s -> SHOULD Log again
        api._last_expiration_check = time.time() - 86410
        api.check_api_key_expiration()
        self.assertEqual(api.logger.info.call_count, 2)

if __name__ == '__main__':
    unittest.main()
