"""
Unit tests for MarketDataRepairer - specifically covering:
1. Timezone handling (naive UTC datetimes should be treated as UTC, not local time)
2. Closed candle filtering (incomplete candles should not be persisted/compared)
"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch
import pandas as pd


class TestSymbolResolution:
    """Tests for symbol resolution logic."""

    def test_hip3_symbol_preserved(self):
        """Verify that HIP-3 symbols with colons (e.g. km:US500) are NOT stripped."""
        from src.utils.market_data_repair import MarketDataRepairer
        
        mock_api = MagicMock()
        mock_api.get_spot_api_name.return_value = None  # Default: not a spot asset
        mock_db = MagicMock()
        repairer = MarketDataRepairer(mock_api, mock_db)
        
        # Should be preserved
        assert repairer.resolve_api_symbol("km:US500") == "km:US500"

        
    def test_spot_internal_resolution(self):
        """Verify internal spot naming conventions still resolve."""
        from src.utils.market_data_repair import MarketDataRepairer
        
        mock_api = MagicMock()
        # Mock spot resolution
        mock_api.get_spot_api_name.side_effect = lambda x: "@109" if x == "UBTC" else None
        mock_api.SPOT_INTERNAL_TO_API = {"BTC_SPOT": "UBTC"}
        
        mock_db = MagicMock()
        repairer = MarketDataRepairer(mock_api, mock_db)
        
        # BTC_SPOT -> UBTC -> @109
        assert repairer.resolve_api_symbol("BTC_SPOT") == "@109"


class TestTimezoneHandling:
    """Tests for correct timezone handling in MarketDataRepairer."""
    
    def test_naive_datetime_treated_as_utc_in_ingest_range(self):
        """
        Verify that naive datetimes are treated as UTC when converting to milliseconds.
        This prevents the 5-hour offset bug when running in EST timezone.
        """
        from src.utils.market_data_repair import MarketDataRepairer
        
        # Create mock API and DB
        mock_api = MagicMock()
        mock_db = MagicMock()
        mock_db.db_path = 'test.db'
        
        # Mock the API call to capture the start_ms and end_ms
        captured_args = {}
        def capture_call(func, *args):
            captured_args['args'] = args
            return []  # Return empty candles
        mock_api._rate_limited_call = capture_call
        mock_api.info = MagicMock()
        
        repairer = MarketDataRepairer(mock_api, mock_db)
        
        # Use a naive datetime representing 02:15 UTC on Jan 13, 2026
        naive_start = datetime(2026, 1, 13, 2, 15, 0)
        naive_end = datetime(2026, 1, 13, 7, 20, 0)
        
        # Expected milliseconds (treating as UTC)
        expected_start_ms = int(naive_start.replace(tzinfo=timezone.utc).timestamp() * 1000)
        expected_end_ms = int(naive_end.replace(tzinfo=timezone.utc).timestamp() * 1000)
        
        # Call _ingest_range
        repairer._ingest_range('BTC', '5m', naive_start, naive_end)
        
        # Verify the API was called with correct UTC timestamps
        assert 'args' in captured_args
        actual_start_ms = captured_args['args'][2]
        actual_end_ms = captured_args['args'][3]
        
        assert actual_start_ms == expected_start_ms, \
            f"Start timestamp mismatch: {actual_start_ms} != {expected_start_ms}"
        assert actual_end_ms == expected_end_ms, \
            f"End timestamp mismatch: {actual_end_ms} != {expected_end_ms}"
    
    def test_naive_datetime_treated_as_utc_in_verify_and_repair(self):
        """
        Verify that naive datetimes are treated as UTC in verify_and_repair.
        """
        from src.utils.market_data_repair import MarketDataRepairer
        
        # Create mock API and DB
        mock_api = MagicMock()
        mock_db = MagicMock()
        mock_db.db_path = 'test.db'
        mock_db.get_market_data = MagicMock(return_value=pd.DataFrame())
        
        # Mock the API call to capture the start_ms and end_ms
        captured_args = {}
        def capture_call(func, *args):
            captured_args['args'] = args
            return []  # Return empty candles
        mock_api._rate_limited_call = capture_call
        mock_api.info = MagicMock()
        
        repairer = MarketDataRepairer(mock_api, mock_db)
        
        # Use a naive datetime
        naive_start = datetime(2026, 1, 13, 2, 15, 0)
        naive_end = datetime(2026, 1, 13, 7, 20, 0)
        
        # Expected milliseconds (treating as UTC)
        expected_start_ms = int(naive_start.replace(tzinfo=timezone.utc).timestamp() * 1000)
        expected_end_ms = int(naive_end.replace(tzinfo=timezone.utc).timestamp() * 1000)
        
        # Call verify_and_repair
        repairer.verify_and_repair('BTC', '5m', naive_start, naive_end, repair=False)
        
        # Verify the API was called with correct UTC timestamps
        assert 'args' in captured_args
        actual_start_ms = captured_args['args'][2]
        actual_end_ms = captured_args['args'][3]
        
        assert actual_start_ms == expected_start_ms
        assert actual_end_ms == expected_end_ms


class TestClosedCandleFiltering:
    """Tests for filtering out incomplete candles."""
    
    def test_incomplete_candles_not_persisted(self):
        """
        Verify that candles whose close time is in the future are not persisted.
        """
        from src.utils.market_data_repair import MarketDataRepairer
        
        # Create mock API and DB
        mock_api = MagicMock()
        mock_db = MagicMock()
        mock_db.db_path = 'test.db'
        mock_db.insert_market_data = MagicMock()
        
        # Create candles response - mix of closed and incomplete
        now = datetime.now(timezone.utc)
        now_ms = int(now.timestamp() * 1000)
        interval_ms = 5 * 60 * 1000  # 5 minutes
        
        # Candle that closed 10 minutes ago (should be persisted)
        closed_candle = {
            't': now_ms - (15 * 60 * 1000),  # Started 15 min ago
            'o': '100', 'h': '101', 'l': '99', 'c': '100.5', 'v': '1000'
        }
        
        # Candle that is still open (should NOT be persisted)
        incomplete_candle = {
            't': now_ms - (3 * 60 * 1000),  # Started 3 min ago, would close in 2 min
            'o': '100', 'h': '101', 'l': '99', 'c': '100.5', 'v': '1000'
        }
        
        mock_api._rate_limited_call = MagicMock(return_value=[closed_candle, incomplete_candle])
        mock_api.info = MagicMock()
        
        repairer = MarketDataRepairer(mock_api, mock_db)
        
        # Call _ingest_range
        start_dt = now - timedelta(hours=1)
        end_dt = now
        repairer._ingest_range('BTC', '5m', start_dt, end_dt)
        
        # Verify insert_market_data was called
        assert mock_db.insert_market_data.called
        
        # Get the DataFrame that was passed to insert_market_data
        call_args = mock_db.insert_market_data.call_args
        inserted_df = call_args[0][0]
        
        # Should only have 1 row (the closed candle)
        assert len(inserted_df) == 1, f"Expected 1 closed candle, got {len(inserted_df)}"
    
    def test_only_closed_candles_compared(self):
        """
        Verify that incomplete candles are not included in mismatch comparison.
        """
        from src.utils.market_data_repair import MarketDataRepairer
        
        # Create mock API and DB
        mock_api = MagicMock()
        mock_db = MagicMock()
        mock_db.db_path = 'test.db'
        mock_db.get_market_data = MagicMock(return_value=pd.DataFrame())
        
        now = datetime.now(timezone.utc)
        now_ms = int(now.timestamp() * 1000)
        interval_ms = 5 * 60 * 1000
        
        # Incomplete candle (should be filtered out)
        incomplete_candle = {
            't': now_ms - (2 * 60 * 1000),  # Started 2 min ago
            'o': '100', 'h': '101', 'l': '99', 'c': '100.5', 'v': '1000'
        }
        
        mock_api._rate_limited_call = MagicMock(return_value=[incomplete_candle])
        
        repairer = MarketDataRepairer(mock_api, mock_db)
        
        start_dt = now - timedelta(hours=1)
        end_dt = now
        
        # Call verify_and_repair
        mismatches = repairer.verify_and_repair('BTC', '5m', start_dt, end_dt, repair=False)
        
        # Should have 0 mismatches since the only candle was filtered out
        assert mismatches == 0


class TestChunkingThreshold:
    """Tests for the chunking threshold optimization."""
    
    def test_chunking_threshold_is_500_intervals(self):
        """
        Verify that the chunking threshold is set to 500 intervals
        to match the API's max candles per call.
        """
        from src.utils.market_data_repair import MarketDataRepairer
        
        mock_api = MagicMock()
        mock_db = MagicMock()
        mock_db.db_path = 'test.db'
        
        repairer = MarketDataRepairer(mock_api, mock_db)
        
        # Create mismatches that span a large range but should be in one cluster
        now = datetime.now(timezone.utc)
        interval = 300  # 5 minutes in seconds
        
        # 400 candles apart (should be in same cluster with 500 threshold)
        mismatches = [
            now - timedelta(hours=10),
            now - timedelta(hours=10) + timedelta(seconds=interval * 400)
        ]
        
        # Mock _ingest_range to capture how many clusters were made
        ingest_calls = []
        def mock_ingest(symbol, timeframe, start, end):
            ingest_calls.append((start, end))
        
        repairer._ingest_range = mock_ingest
        
        # Call _repair_clusters
        repairer._repair_clusters('BTC', '5m', mismatches, interval)
        
        # Should be 1 cluster (not 2) since 400 < 500
        assert len(ingest_calls) == 1, f"Expected 1 cluster, got {len(ingest_calls)}"
