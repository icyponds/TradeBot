
import unittest
import sqlite3
import os
import json
from datetime import datetime
from src.utils.trade_database import TradeDatabase

class TestTradeDatabasePersistence(unittest.TestCase):
    def setUp(self):
        # Use a temporary database
        self.db_path = "tests/test_trades.db"
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        self.db = TradeDatabase(self.db_path)

    def tearDown(self):
        # Clean up database
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_save_and_load_single_leg_position(self):
        # 1. Define Position Data
        position = {
            'position_id': 'BTC_SPOT',
            'strategy': 'manual',
            'symbol': 'BTC',
            'side': 'long',
            'size': 1.5,
            'leverage': 1.0,
            'entry_price': 50000.0,
            'entry_time': datetime.now().isoformat(),
            'stop_loss': 49000.0,
            'take_profit': 55000.0,
            'metadata': {'trailing_stop_active': True}
        }
        
        # 2. Save Position
        # (This method doesn't exist yet, we expect this test to fail initially or we mock it)
        # For true TDD, we write the test assuming the method signature we designed
        try:
            self.db.save_position(position)
        except AttributeError:
             self.skipTest("save_position method not implemented yet")

        # 3. Load Position
        active_positions = self.db.get_all_active_positions()
        
        # 4. Verify
        self.assertEqual(len(active_positions), 1)
        saved_pos = active_positions[0]
        self.assertEqual(saved_pos['position_id'], 'BTC_SPOT')
        self.assertEqual(saved_pos['symbol'], 'BTC')
        self.assertEqual(saved_pos['side'], 'long')
        self.assertEqual(saved_pos['size'], 1.5)
        # Check Metadata deserialization
        self.assertEqual(saved_pos['metadata']['trailing_stop_active'], True)

    def test_save_and_load_multi_leg_position(self):
        # 1. Define Multi-Leg Position
        position_id = "arb_strategy_12345"
        legs = [
            {'symbol': 'ETH/USD', 'market_type': 'spot', 'side': 'buy', 'size': 10, 'entry_price': 3000},
            {'symbol': 'ETH-PERP', 'market_type': 'perp', 'side': 'sell', 'size': 10, 'entry_price': 3010}
        ]
        
        position = {
            'position_id': position_id,
            'strategy': 'stat_arb_15m',
            'symbol': 'ETH',
            'side': 'neutral',
            'size': 20, # Total notional or similar
            'leverage': 2.0,
            'entry_price': 3005.0, # Avg
            'entry_time': datetime.now().isoformat(),
            'stop_loss': None,
            'take_profit': None,
            'metadata': {'hedge_ratio': 1.0},
            'legs': legs # Expected to be handled by save_position logic
        }

        # 2. Save
        try:
            self.db.save_position(position)
        except AttributeError:
             self.skipTest("save_position method not implemented yet")

        # 3. Load
        active_positions = self.db.get_all_active_positions()
        
        # 4. Verify
        self.assertEqual(len(active_positions), 1)
        saved_pos = active_positions[0]
        self.assertEqual(saved_pos['position_id'], position_id)
        self.assertEqual(len(saved_pos['legs']), 2)
        
        # Verify Legs
        spot_leg = next(l for l in saved_pos['legs'] if l['market_type'] == 'spot')
        self.assertEqual(spot_leg['symbol'], 'ETH/USD')
        self.assertEqual(spot_leg['size'], 10)

    def test_delete_position(self):
        # 1. Create Position
        position = {
            'position_id': 'SOL_SPOT',
            'strategy': 'sentiment',
            'symbol': 'SOL',
            'side': 'long',
            'size': 100,
            'leverage': 1.0,
            'entry_price': 20.0,
            'entry_time': datetime.now().isoformat(),
            'stop_loss': 18.0,
            'take_profit': 25.0,
            'metadata': {}
        }
        
        try:
            self.db.save_position(position)
            self.db.delete_position('SOL_SPOT')
            active_positions = self.db.get_all_active_positions()
            self.assertEqual(len(active_positions), 0)
        except AttributeError:
             self.skipTest("Methods not implemented yet")

    def test_save_and_load_position_with_order_id(self):
        """Test that order_id is persisted and retrieved correctly."""
        # 1. Single-leg position with order_id
        position = {
            'position_id': 'pos_csm_4h_BTC',
            'strategy': 'csm_4h',
            'symbol': 'BTC',
            'side': 'long',
            'size': 0.5,
            'leverage': 5.0,
            'entry_price': 42000.0,
            'entry_time': datetime.now().isoformat(),
            'stop_loss': 40000.0,
            'take_profit': 50000.0,
            'order_id': '0x1234abcd',  # Exchange OID
            'metadata': {}
        }
        
        # 2. Save
        self.db.save_position(position)
        
        # 3. Load
        active_positions = self.db.get_all_active_positions()
        
        # 4. Verify order_id is preserved
        self.assertEqual(len(active_positions), 1)
        saved_pos = active_positions[0]
        self.assertEqual(saved_pos['order_id'], '0x1234abcd')
        self.assertEqual(saved_pos['position_id'], 'pos_csm_4h_BTC')

    def test_save_and_load_multi_leg_with_order_ids(self):
        """Test that order_id is persisted for each leg in multi-leg positions."""
        position = {
            'position_id': 'funding_arb_ETH_123',
            'strategy': 'funding_rate_arbitrage',
            'symbol': 'ETH',
            'side': 'neutral',
            'size': 1.0,
            'leverage': 1.0,
            'entry_price': 2200.0,
            'entry_time': datetime.now().isoformat(),
            'stop_loss': None,
            'take_profit': None,
            'order_id': None,  # Multi-leg head has no single order_id
            'metadata': {},
            'legs': [
                {'symbol': 'ETH', 'market_type': 'perp', 'side': 'short', 'size': 1.0, 'entry_price': 2200.0, 'order_id': '0xAAAA1111'},
                {'symbol': 'ETH', 'market_type': 'spot', 'side': 'long', 'size': 1.0, 'entry_price': 2198.0, 'order_id': '0xBBBB2222'},
            ]
        }
        
        # Save
        self.db.save_position(position)
        
        # Load
        active_positions = self.db.get_all_active_positions()
        
        # Verify
        self.assertEqual(len(active_positions), 1)
        saved_pos = active_positions[0]
        self.assertEqual(len(saved_pos['legs']), 2)
        
        # Verify each leg has order_id
        perp_leg = next(l for l in saved_pos['legs'] if l['market_type'] == 'perp')
        spot_leg = next(l for l in saved_pos['legs'] if l['market_type'] == 'spot')
        self.assertEqual(perp_leg['order_id'], '0xAAAA1111')
        self.assertEqual(spot_leg['order_id'], '0xBBBB2222')

if __name__ == '__main__':
    unittest.main()
