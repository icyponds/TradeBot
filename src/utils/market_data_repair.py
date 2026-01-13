from __future__ import annotations
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple, Dict, Any, Union, TYPE_CHECKING
import pandas as pd
import time

if TYPE_CHECKING:
    from src.api.hyperliquid_api import HyperliquidAPI
from src.utils.trade_database import TradeDatabase

logger = logging.getLogger(__name__)

class MarketDataRepairer:
    """
    Handles verification and repair of historical market data.
    Provides mechanism to ensure local DB matches API source-of-truth.
    """
    
    def __init__(self, api: 'HyperliquidAPI', db: TradeDatabase):
        self.api = api
        self.db = db
        self.timeframes = ['5m', '15m', '1h', '4h', '1d']
    
    def get_interval_seconds(self, timeframe: str) -> int:
        mapping = {
            '1m': 60,
            '5m': 300,
            '15m': 900,
            '1h': 3600,
            '4h': 14400,
            '1d': 86400
        }
        return mapping.get(timeframe, 3600)

    def resolve_api_symbol(self, symbol: str) -> str:
        """
        Resolve internal symbol to API symbol.
        
        Examples:
            'xyz:SOL' -> 'SOL' (HIP-3 deployer prefix stripped)
            'BTC' -> 'BTC' (perp symbol unchanged)
            'UENA' -> '@142' (spot symbol resolved to @index)
            'BTC_SPOT' -> '@109' (internal spot name resolved)
        """
        # Handle deployer prefixes (e.g., 'xyz:SOL' -> 'SOL')
        # [MODIFIED] Removed prefix stripping. HIP-3 assets like 'km:US500' MUST keep their prefix.
        # if ':' in symbol:
        #    parts = symbol.split(':')
        #    return parts[1]

        
        # Handle spot assets - need to convert to @index format
        # Check if this is a spot asset that needs resolution
        if hasattr(self.api, 'get_spot_api_name'):
            # Try direct lookup first (e.g., 'UENA' -> '@142')
            spot_api = self.api.get_spot_api_name(symbol)
            if spot_api:
                return spot_api
            
            # Try internal naming convention (e.g., 'BTC_SPOT' -> 'UBTC' -> '@109')
            if symbol.endswith('_SPOT') and hasattr(self.api, 'SPOT_INTERNAL_TO_API'):
                api_token_name = self.api.SPOT_INTERNAL_TO_API.get(symbol)
                if api_token_name:
                    spot_api = self.api.get_spot_api_name(api_token_name)
                    if spot_api:
                        return spot_api
        
        return symbol

    def _ingest_range(self, symbol: str, timeframe: str, start_dt: datetime, end_dt: datetime):
        """Fetch and replace data for a specific range. Only persists CLOSED candles."""
        # IMPORTANT: Treat naive datetimes as UTC (they come from DB which stores UTC)
        # Using .timestamp() on naive datetime assumes LOCAL timezone, causing offset errors
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)
        target_symbol = self.resolve_api_symbol(symbol)
        interval_ms = self.get_interval_seconds(timeframe) * 1000
        
        try:
            candles = self.api._rate_limited_call(
                self.api.info.candles_snapshot, 
                target_symbol, timeframe, start_ms, end_ms
            )
        except Exception as e:
            logger.warning(f"[{symbol} {timeframe}] API Fetch Error: {e}")
            return

        if not candles:
            return

        # Filter to only persist CLOSED candles (candle_start + interval <= now)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        closed_candles = [c for c in candles if c['t'] + interval_ms <= now_ms]
        
        if not closed_candles:
            return

        # Convert to DataFrame
        bars = []
        for c in closed_candles:
            bars.append({
                'time': c['t'] // 1000,
                'open': float(c['o']),
                'high': float(c['h']),
                'low': float(c['l']),
                'close': float(c['c']),
                'volume': float(c['v']),
            })
            
        df = pd.DataFrame(bars)
        df['timestamp'] = pd.to_datetime(df['time'], unit='s')
        df.set_index('timestamp', inplace=True)
        
        # Persist (INSERT OR REPLACE)
        self.db.insert_market_data(df, symbol, timeframe)
        logger.info(f"[{symbol} {timeframe}] Repaired range {start_dt} -> {end_dt} ({len(df)} candles)")

    def verify_and_repair(self, symbol: str, timeframe: str, start_dt: datetime, end_dt: datetime, repair: bool = True) -> int:
        """
        Verify data against API and repair if needed.
        Only compares CLOSED candles to avoid false positives from incomplete data.
        Returns number of mismatches found.
        """
        interval = self.get_interval_seconds(timeframe)
        interval_ms = interval * 1000
        
        # IMPORTANT: Treat naive datetimes as UTC (they come from DB which stores UTC)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        
        # 1. Fetch Local
        df_db = self.db.get_market_data(symbol, timeframe, start_date=start_dt, end_date=end_dt)
        
        # 2. Fetch Remote (Source of Truth)
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)
        target_symbol = self.resolve_api_symbol(symbol)
        
        try:
            candles = self.api._rate_limited_call(
                self.api.info.candles_snapshot,
                target_symbol, timeframe, start_ms, end_ms
            )
        except Exception as e:
            logger.warning(f"[{symbol} {timeframe}] Failed to verify (API Error): {e}")
            return 0
            
        if not candles:
            return 0
        
        # Filter to only compare CLOSED candles
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        closed_candles = [c for c in candles if c['t'] + interval_ms <= now_ms]
        
        if not closed_candles:
            return 0
            
        # Map API candles (closed only)
        api_map = {}
        for c in closed_candles:
            ts = pd.to_datetime(c['t'], unit='ms')
            api_map[ts] = {
                'close': float(c['c']),
                'volume': float(c['v'])
            }
            
        # 3. Compare
        mismatches = []
        
        # Check DB rows against API
        for ts, row in df_db.iterrows():
            if ts not in api_map:
                continue
                
            api_row = api_map[ts]
            
            # Close price tolerance (fixed, very tight for prices)
            close_mismatch = abs(row['close'] - api_row['close']) > 1e-6
            
            # Volume tolerance (relative: 1% of volume, min 0.01)
            volume_tol = max(0.01, api_row['volume'] * 0.01)
            volume_mismatch = abs(row['volume'] - api_row['volume']) > volume_tol
            
            if close_mismatch or volume_mismatch:
                mismatches.append(ts)
        
        # Check API rows against DB (Missing candles)
        db_index_set = set(df_db.index)
        for ts in api_map:
            if ts not in db_index_set:
                mismatches.append(ts)
                
        if not mismatches:
            return 0
            
        if repair:
            self._repair_clusters(symbol, timeframe, mismatches, interval)
            
        return len(mismatches)

    def _repair_clusters(self, symbol: str, timeframe: str, mismatches: List[datetime], interval: int):
        """Repair mismatches using efficient clustering."""
        mismatches.sort()
        logger.info(f"[{symbol} {timeframe}] Repairing {len(mismatches)} mismatches...")
        
        clusters = []
        current_cluster = [mismatches[0]]
        threshold_seconds = interval * 500  # Match API max candles per call
        
        for m in mismatches[1:]:
            t1 = m
            t0 = current_cluster[-1]
            if (t1 - t0).total_seconds() > threshold_seconds:
                clusters.append(current_cluster)
                current_cluster = [m]
            else:
                current_cluster.append(m)
        clusters.append(current_cluster)
        
        for cluster in clusters:
            min_bad = min(cluster).to_pydatetime() if hasattr(min(cluster), 'to_pydatetime') else min(cluster)
            max_bad = max(cluster).to_pydatetime() if hasattr(max(cluster), 'to_pydatetime') else max(cluster)
            
            # Add buffer
            ingest_start = min_bad - timedelta(seconds=interval * 2)
            ingest_end = max_bad + timedelta(seconds=interval * 2)
            
            self._ingest_range(symbol, timeframe, ingest_start, ingest_end)

    def repair_all(self, days_back: int = 2):
        """Run repair cycle for all assets in DB."""
        if not self.db:
            return
            
        symbols = self.db.get_all_symbols()
        end_dt = datetime.now(timezone.utc).replace(tzinfo=None) 
        start_dt = end_dt - timedelta(days=days_back)
        
        logger.info(f"[MarketDataRepairer] Starting integrity check for {len(symbols)} assets (Last {days_back} days)")
        
        for symbol in symbols:
            # Compromise: check small timeframes mostly.
            for tf in ['5m', '15m']: 
                try:
                    count = self.verify_and_repair(symbol, tf, start_dt, end_dt, repair=True)
                    if count > 0:
                        logger.info(f"[MarketDataRepairer] Repaired {count} candles for {symbol} {tf}")
                except Exception as e:
                    logger.error(f"[MarketDataRepairer] Error checking {symbol} {tf}: {e}")
                
            # Sleep slightly to allow other threads priority
            time.sleep(0.1)

    def process_asset(self, symbol: str, days_back: int = 2):
        """
        Process a single asset for integrity checks (Integration with Background Fetcher).
        Checks 5m and 15m timeframes for the specified lookback period.
        """
        if not self.db:
            return
            
        try:
            end_dt = datetime.now(timezone.utc).replace(tzinfo=None)
            # Add 0.1 day (~2.4 hours) buffer to prevent edge-case mismatches at window boundary
            start_dt = end_dt - timedelta(days=days_back + 0.1)
            
            # SNAP to the start of the hour to ensure we capture the full candle at the boundary
            # logic: API includes candle overlapping start, DB query excludes if start > candle_time
            start_dt = start_dt.replace(minute=0, second=0, microsecond=0)
            
            logger.info(f"[MarketDataRepairer] Checking integrity from {start_dt} to {end_dt} (~{days_back:.1f} days)")
            
            # Check key timeframes
            for tf in ['5m', '15m']:
                try:
                    mismatches = self.verify_and_repair(symbol, tf, start_dt, end_dt, repair=True)
                    if mismatches > 0:
                        logger.info(f"[Integrity] {symbol} {tf}: Repaired {mismatches} mismatches")
                except Exception as e:
                    logger.debug(f"[Integrity] {symbol} {tf} check failed: {e}")
                    
        except Exception as e:
            logger.error(f"[Integrity] Processing error for {symbol}: {e}")
