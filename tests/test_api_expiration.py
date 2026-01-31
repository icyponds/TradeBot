
import unittest
import time
from unittest.mock import MagicMock
from datetime import datetime, timedelta
from src.api.hyperliquid_api import HyperliquidAPI

class TestAPIExpiration(unittest.TestCase):
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
    
    def test_explicit_expiration_valid(self):
        """Test with explicit expiration date (6 months in future)."""
        future_date = (datetime.now() + timedelta(days=180)).strftime("%Y-%m-%d")
        self.config['api']['key_expiration_date'] = future_date
        
        api = HyperliquidAPI(self.config)
        api.logger = MagicMock()
        api._last_expiration_check = 0 # Reset cooldown
        api.check_api_key_expiration()
        
        # Verify it was called efficiently
        self.assertTrue(api.logger.info.called)
        args, _ = api.logger.info.call_args
        self.assertIn("✅ API Key valid", args[0])

    def test_explicit_expiration_warning(self):
        """Test with explicit expiration date (20 days away)."""
        soon_date = (datetime.now() + timedelta(days=20)).strftime("%Y-%m-%d")
        self.config['api']['key_expiration_date'] = soon_date
        
        api = HyperliquidAPI(self.config)
        api.logger = MagicMock()
        api._last_expiration_check = 0 # Reset cooldown
        api.check_api_key_expiration()
        
        self.assertTrue(api.logger.warning.called)
        args, _ = api.logger.warning.call_args
        self.assertIn("⚠️ API Key expires in", args[0])

    def test_fallback_creation_date(self):
        """Test fallback to creation date if explicit date missing."""
        self.config['api']['key_expiration_date'] = ''
        created_date = (datetime.now() - timedelta(days=160)).strftime("%Y-%m-%d")
        self.config['api']['key_created_at'] = created_date
        
        api = HyperliquidAPI(self.config)
        api.logger = MagicMock()
        api._last_expiration_check = 0 # Reset cooldown
        api.check_api_key_expiration()
        
        self.assertTrue(api.logger.warning.called)
        args, _ = api.logger.warning.call_args
        self.assertIn("⚠️ API Key expires in", args[0])
        
        # 5. Trigger again immediately - should NOT log (cooldown)
        api.check_api_key_expiration()

    def test_startup_warning_and_cooldown(self):
        """Test periodic cooldown logic."""
        soon_date = (datetime.now() + timedelta(days=20)).strftime("%Y-%m-%d")
        self.config['api']['key_expiration_date'] = soon_date
        
        # 1. Init API (runs check internally, sets cooldown)
        api = HyperliquidAPI(self.config)
        
        # 2. Mock logger AFTER init
        api.logger = MagicMock()
        
        # 3. Reset cooldown to force a check now
        api._last_expiration_check = 0
        
        # 4. Trigger check - should log
        api.check_api_key_expiration()
        self.assertEqual(api.logger.warning.call_count, 1)
        args, _ = api.logger.warning.call_args
        self.assertIn("⚠️ API Key expires in", args[0])
        
        # 5. Trigger again immediately - should NOT log (cooldown)
        api.check_api_key_expiration()
        self.assertEqual(api.logger.warning.call_count, 1) # Still 1
        
        # 6. Time travel > 24 hours + 1s
        api._last_expiration_check -= 86410 
        api.check_api_key_expiration()
        self.assertEqual(api.logger.warning.call_count, 2) # Now 2

if __name__ == '__main__':
    unittest.main()
