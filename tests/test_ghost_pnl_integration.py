
import pytest
import shutil
import os
import time
from datetime import datetime
from unittest.mock import MagicMock, patch
from src.strategies.strategy_manager import StrategyManager
from src.utils.trade_database import TradeDatabase
from src.backtesting.mock_market_api import MockMarketAPI

class TestGhostPnLIntegration:
    
    @classmethod
    def setup_class(cls):
        # Setup temporary DB path
        cls.db_path = "tests/data/test_ghost_pnl_integ.db"
        os.makedirs("tests/data", exist_ok=True)
        if os.path.exists(cls.db_path):
            os.remove(cls.db_path)
    
    @classmethod
    def teardown_class(cls):
        # Cleanup
        if os.path.exists(cls.db_path):
            os.remove(cls.db_path)

    @patch('src.strategies.strategy_manager.DynamicPairSelector')
    def test_full_lifecycle_ghost_close(self, MockPairSelector):
        """
        Simulate:
        1. Open Position
        2. Shut down bot
        3. External Close (Ghost)
        4. Restart Bot
        5. Verify Reconciliation
        """
        # Configure Mock Pair Selector to be harmless
        MockPairSelector.return_value.get_selected_pairs.return_value = ['HBAR']
        
        # Prepare Config
        config = {
            'trading': {
                'strategies': {'enabled': ['test_strat']},
                'max_positions_percentage': 100,
                'base_currency': 'USDC',
                'order_timeout_minutes': 5,
                'enable_stale_order_cleanup': False,
                'position_sync_interval': 60,
                'enable_position_validation': True,
                'max_positions_per_strategy': 5,
                'use_portfolio_based_sizing': True,
                'max_position_size_percentage': 100,
                'dynamic_pair_selection': False
            },
            'risk_management': {
                'margin_buffer_percentage': 0.05,
                'liquidation_risk_threshold': 0.9
            },
            'strategies': {'ohlcv_limit': 100, 'enabled': ['test_strat']},
            'database': {'path': self.db_path},
            'persistence': {'db_path': self.db_path}
        }

        # Prepare Mock Historical Data (Required for price checks)
        # Create a tiny DataFrame for HBAR
        import pandas as pd
        dates = pd.date_range(end=datetime.now(), periods=10, freq='1min')
        df = pd.DataFrame({
            'open': [0.10]*10, 'high': [0.11]*10, 'low': [0.09]*10, 'close': [0.10]*10, 'volume': [1000]*10
        }, index=dates)
        historical_data = {'HBAR': {'1m': df}}

        # --- Shared Infrastructure ---
        # Mock API persists across "restarts" (simulating the exchange)
        market_api = MockMarketAPI(config=config, historical_data=historical_data)
        market_api.perp_balance['withdrawable'] = 10000.0
        
        # Ensure we have a current price for HBAR
        market_api.current_time = dates[-1] # Set time to end of data

        
        # --- STEP 1: Bot Run 1 (Open Position) ---
        print("\n\n--- STEP 1: Starting Bot (Run 1) ---")
        sm1 = StrategyManager(config=config, market_api=market_api)
        
        # Create a Long position on HBAR
        symbol = 'HBAR'
        # Create a Long position on HBAR manually to avoid complex execute_trade signal requirements
        # We just need the state to be in DB and Memory
        from src.models.trade import Position
        symbol = 'HBAR'
        pos = Position(
            symbol=symbol,
            side='long',
            entry_price=0.10,
            size=1000.0,
            strategy='test_strat',
            entry_time=datetime.now()
        )
        sm1.execution_engine.positions[symbol] = pos
        sm1.execution_engine.save_positions_to_db()
        
        # Also ensure MockAPI has this position so checking balances doesn't freak out
        market_api.positions[symbol] = {
            'side': 'long',
            'size': 1000.0,
            'entry_price': 0.10,
            'leverage': 1.0,
            'margin_used': 100.0
        }
        
        # Verify position is open in DB
        active_positions = sm1.execution_engine.performance_tracker.db.get_all_active_positions()
        assert len(active_positions) == 1
        assert active_positions[0]['symbol'] == symbol
        assert float(active_positions[0]['entry_price']) == 0.10
        print("Position Opened Successfully.")
        
        # --- STEP 2: Shutdown ---
        print("--- STEP 2: Shutting down Bot ---")
        del sm1
        
        # --- STEP 3: External Close (The Ghost) ---
        print("--- STEP 3: Simulating External Close ---")
        # Direct manipulation of Mock API to simulate a fill that the bot didn't execute
        # Close Price: 0.12 (Profit)
        close_price = 0.12
        close_time_ms = int(time.time() * 1000)
        
        # 3a. Update Mock API State (Remove position)
        # MockMarketAPI methods normally handle this, but we are simulating *manual* action
        # so we bypass the engine and go straight to the exchange state
        
        # Add Closing Fill to History
        closing_fill = {
            'coin': symbol,
            'side': 'Sell', # Capitalized to test normalization too
            'px': str(close_price),
            'sz': '1000.0',
            'time': close_time_ms,
            'dir': 'Close Long',
            'fee': '0.06' # Fee = 120 * 0.0005 = 0.06
        }
        market_api.fills.insert(0, closing_fill) # Insert at top (newest)
        
        # Update Balance (Add PnL)
        # Entry: 1000 * 0.10 = 100 cost
        # Exit: 1000 * 0.12 = 120 value
        # PnL: +20
        market_api.perp_balance['withdrawable'] += 20.0
        # Remove from internal mock positions
        if symbol in market_api.positions:
            del market_api.positions[symbol]
            
        print(f"External Close Executed at {close_price}")
        
        # --- STEP 4: Bot Run 2 (Restart) ---
        print("--- STEP 4: Restarting Bot (Run 2) ---")
        # Create NEW instance with SAME DB and SAME Mock API
        sm2 = StrategyManager(config=config, market_api=market_api)
        
        # At start, bot loads positions from DB.
        # It thinks it still has the position open.
        assert len(sm2.execution_engine.positions) == 1
        print("Bot reloaded position from DB (Ghost state confirmed).")
        
        # --- STEP 5: Reconciliation ---
        print("--- STEP 5: Syncing with Exchange ---")
        
        # Trigger the sync logic
        # this calls sync_positions_with_exchange -> _find_closing_fill
        sm2.sync_positions_with_exchange()
        
        # --- STEP 6: Verification ---
        print("--- STEP 6: Verifying PnL ---")
        
        # 1. Position should be gone from ExecutionEngine
        assert len(sm2.execution_engine.positions) == 0, "Ghost position should be removed from memory"
        
        # 2. Position should be gone from DB active positions
        db_positions = sm2.execution_engine.performance_tracker.db.get_all_active_positions()
        assert len(db_positions) == 0, "Ghost position should be removed from DB live_positions"
        
        # 3. Trade should be recorded in trades table with CORRECT PnL
        trades = sm2.execution_engine.performance_tracker.db.get_all_trades()
        assert len(trades) == 1, "Should have 1 closed trade"
        
        trade = trades[0]
        print(f"Recorded Trade: {trade}")
        
        assert trade['exit_price'] == close_price, f"Exit price should be {close_price}, got {trade['exit_price']}"
        
        # Calculate Expected PnL
        # Entry: 1000 * 0.10 = 100
        # Exit: 1000 * 0.12 = 120
        # Gross PnL: +20
        # Fees: MockAPI charges 0.05% on value.
        # Fee = 120 * 0.0005 = 0.06
        # Net PnL = 20 - 0.06 = 19.94
        
        # Check that we capture fees
        fee = trade.get('fees', 0.0)
        print(f"Recorded Fees: {fee}")
        assert fee > 0, "Fees should be recorded"
        
        expected_pnl = 20.0 - fee
        assert abs(trade['pnl'] - expected_pnl) < 1e-6, f"PnL should be Net of fees. Got {trade['pnl']}, Expected {expected_pnl}"
        
        print("SUCCESS: Ghost position correctly reconciled with Net PnL!")

if __name__ == "__main__":
    pytest.main([__file__])
