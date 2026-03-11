import pytest
from unittest.mock import MagicMock, patch
import pandas as pd
from datetime import datetime, timedelta, timezone
import time

class TestAPIBackfill:
    
    @pytest.fixture
    def mock_db(self):
        db = MagicMock()
        db.get_market_data.return_value = pd.DataFrame() # Default empty
        return db
        
    @pytest.fixture
    def api_client(self, shared_api_client, mock_db):
        """Use shared module-scoped client, reset mocks."""
        shared_api_client.exchange.reset_mock()
        shared_api_client.info.reset_mock()
        shared_api_client.market_db = mock_db
        return shared_api_client

    def test_get_ohlcv_backfills_history(self, api_client):
        """
        Test that get_ohlcv fetches missing history when DB has recent data 
        but insufficient length for the requested limit.
        """
        symbol = "BTC"
        interval = "1h"
        limit = 100
        
        # 1. Setup DB state: 48h of recent data
        now = datetime.now(timezone.utc)
        start_db_time = now - timedelta(hours=48)
        
        # Create dummy dataframe with 48 hours of data
        dates = pd.date_range(start=start_db_time, periods=48, freq='1h', tz='UTC')
        # DB returns naive UTC
        dates_naive = [d.replace(tzinfo=None) for d in dates]
        
        initial_df = pd.DataFrame({
            'open': [100.0] * 48,
            'high': [105.0] * 48,
            'low': [95.0] * 48,
            'close': [102.0] * 48,
            'volume': [1000.0] * 48
        }, index=pd.DatetimeIndex(dates_naive, name='timestamp'))
        
        api_client.market_db.get_market_data.return_value = initial_df
        
        # 2. Mock API Response for the BACKFILL (hours 49-100)
        # We expect a fetch from T-100h to T-48h
        backfill_candles = []
        backfill_start_ts = int((start_db_time - timedelta(hours=52)).timestamp() * 1000)
        
        for i in range(52):
            backfill_candles.append({
                't': backfill_start_ts + (i * 3600000),
                'o': 90.0, 'h': 95.0, 'l': 85.0, 'c': 92.0, 'v': 500.0, 'n': 10
            })
            
        def candles_side_effect(symbol, timeframe, start_time, end_time):
            # If start_time is "recent" (Forward Fill), return empty
            recent_threshold = int((now - timedelta(hours=10)).timestamp() * 1000)
            if start_time > recent_threshold:
                return []
            # Else return backfill data
            return backfill_candles
            
        api_client.info.candles_snapshot.side_effect = candles_side_effect
        api_client._get_asset_info_for_symbol = MagicMock(return_value={'name': 'BTC'})
        
        # 3. Call get_ohlcv
        with patch('time.sleep'): # skip sleeps
            # We must clear cache or it might return cached result if shared_api_client is dirty
            api_client.ohlcv_cache.cache.clear()
            
            result_df = api_client.get_ohlcv(symbol, interval, limit=limit)
            
        # 4. Verifications
        
        # Should have called API to backfill
        assert api_client.info.candles_snapshot.called, "Should have called API for backfill"
        
        # Check call arguments (scan all calls for the backfill one)
        # We might have 2 calls: Forward Gap Fill (due to test date gap) and Backward Backfill
        # We want to verify the BACKFILL happened.
        
        backfill_found = False
        # With MIN_FETCH_LIMIT=500 and 1h interval, we now ask for 500 hours back from NOW.
        # But we also add the DB gap logic.
        # Let's just find ANY call that requests a start time significantly before start_db_time
        start_db_ts = int(start_db_time.timestamp() * 1000)
        
        for call in api_client.info.candles_snapshot.call_args_list:
            args, _ = call
            start_ts = args[2]
            # Check if this call starts before our DB data starts (it's a backfill)
            if start_ts < start_db_ts:
                backfill_found = True
                break
                
        assert backfill_found, f"Backfill call not found! Calls: {api_client.info.candles_snapshot.call_args_list}"
        
        # Should have persisted the backfilled data
        assert api_client.market_db.insert_market_data.called
        
        # Result should combine DB data (48) + Backfill (52) -> 100 (or close)
        assert len(result_df) >= 90, f"Expected ~100 rows, got {len(result_df)}"
        
    def test_get_ohlcv_no_backfill_needed(self, api_client):
        """Test that no backfill occurs if DB has sufficient history (>= MIN_FETCH_LIMIT)."""
        symbol = "BTC"
        interval = "1h"
        limit = 24
        
        # Setup DB with 550h data (more than limit AND more than MIN_FETCH_LIMIT=500)
        now = datetime.now(timezone.utc)
        dates = pd.date_range(end=now, periods=550, freq='1h', tz='UTC')
        dates_naive = [d.replace(tzinfo=None) for d in dates]
        
        initial_df = pd.DataFrame({
            'open': [100.0] * 550,
            'high': [105.0] * 550,
            'low': [95.0] * 550,
            'close': [102.0] * 550,
            'volume': [1000.0] * 550
        }, index=pd.DatetimeIndex(dates_naive, name='timestamp'))
        
        api_client.market_db.get_market_data.return_value = initial_df
        api_client.info.candles_snapshot.reset_mock()
        api_client._get_asset_info_for_symbol = MagicMock(return_value={'name': 'BTC'})

        with patch('time.sleep'):
            api_client.ohlcv_cache.cache.clear()
            result_df = api_client.get_ohlcv(symbol, interval, limit=limit)
            
        # We can inspect the call args if it IS called to ensure it wasn't a historical fetch.
        if api_client.info.candles_snapshot.called:
             args, _ = api_client.info.candles_snapshot.call_args
             fetch_start = args[2]
             db_min_ts = int(dates[0].timestamp() * 1000)
             # If fetch_start is >= db_min_ts, it's a forward fill. 
             # If fetch_start < db_min_ts, it's a backfill (BAD for this test because DB had 550 candles).
             assert fetch_start >= db_min_ts, "Triggered historical backfill when not needed!"

    def test_backfill_loop_prevention(self, api_client):
        """Test that infinite backfill loops are prevented by state checking."""
        
        # 1. Setup Wrapper
        symbol = "BTC"
        interval = "1h"
        # Mock API to return empty list (Simulating Genesis)
        api_client.info.candles_snapshot.return_value = []
        api_client.info.candles_snapshot.side_effect = None # Clear leaky side_effect from previous tests
        # Clear backfill state to ensure clean test
        if hasattr(api_client, '_backfill_state'):
            api_client._backfill_state.clear()
        
        # Seed cache with some recent data
        now = time.time()
        recent_bars = [{
            'time': int(now - i*3600),
            'open': 100, 'high': 101, 'low': 99, 'close': 100, 'volume': 100
        } for i in range(10)]
        api_client.ohlcv_cache.seed(symbol, interval, recent_bars)
        
        # FIX: Also seed market_db so _fetch logic sees "partial data" and triggers backfill check
        df_db = pd.DataFrame(recent_bars)
        df_db['timestamp'] = pd.to_datetime(df_db['time'], unit='s')
        df_db.set_index('timestamp', inplace=True)
        # Ensure index is sorted ascending (recent_bars creation was descending)
        df_db.sort_index(inplace=True)
        api_client.market_db.get_market_data.return_value = df_db
        
        # 2. First Call: Should attempt to fetch history
        # Requiring 20 bars (we have 10), so it will look back
        df = api_client.get_ohlcv(symbol, interval, limit=20)
        
        # Verify call was made
        assert api_client.info.candles_snapshot.call_count == 1
        
        # 3. Second Call: Should SHORT-CIRCUIT
        # The first call returned empty, so _backfill_state should be updated
        df = api_client.get_ohlcv(symbol, interval, limit=20)
        
        # Verify call count is STILL 1 (No new call)
        assert api_client.info.candles_snapshot.call_count == 1
        
        # Verify state was set
        assert (symbol, interval) in api_client._backfill_state
             
