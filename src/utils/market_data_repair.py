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
        """Resolve internal symbol to API symbol (e.g., 'xyz:SOL' -> 'SOL')."""
        if ':' in symbol:
            parts = symbol.split(':')
            return parts[1]
        return symbol

    def _ingest_range(self, symbol: str, timeframe: str, start_dt: datetime, end_dt: datetime):
        """Fetch and replace data for a specific range."""
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)
        target_symbol = self.resolve_api_symbol(symbol)
        
        try:
            # Use the API's rate-limited call if possible, or direct info client access 
            # (which now has retry wrapper if accessing via API wrapper, but here we access info directly?)
            # Wait, self.api.info is the SDK client. It doesn't use _rate_limited_call unless we wrap it.
            # However, if we are part of the bot logic, maybe we should use api.get_candles wrapper?
            # But api.get_ohlcv implements the split logic (db/cache).
            # We want RAW API access here.
            # To benefit from retry logic, we should probably wrap this call or rely on SDK retries?
            # The user asked for robust retry. 
            # self.api._rate_limited_call(self.api.info.candles_snapshot, ...)
            
            candles = self.api._rate_limited_call(
                self.api.info.candles_snapshot, 
                target_symbol, timeframe, start_ms, end_ms
            )
        except Exception as e:
            logger.warning(f"[{symbol} {timeframe}] API Fetch Error: {e}")
            return

        if not candles:
            return

        # Convert to DataFrame
        bars = []
        for c in candles:
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
        Returns number of mismatches found.
        """
        interval = self.get_interval_seconds(timeframe)
        
        # 1. Fetch Local
        df_db = self.db.get_market_data(symbol, timeframe, start_date=start_dt, end_date=end_dt)
        
        # 2. Fetch Remote (Source of Truth)
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)
        target_symbol = self.resolve_api_symbol(symbol)
        
        try:
            # Wrap with robust retry
            candles = self.api._rate_limited_call(
                self.api.info.candles_snapshot,
                target_symbol, timeframe, start_ms, end_ms
            )
        except Exception as e:
            logger.warning(f"[{symbol} {timeframe}] Failed to verify (API Error): {e}")
            return 0
            
        if not candles:
            return 0
            
        # Map API candles
        api_map = {}
        for c in candles:
            ts = pd.to_datetime(c['t'], unit='ms')
            api_map[ts] = {
                'close': float(c['c']),
                'volume': float(c['v'])
            }
            
        # 3. Compare
        mismatches = []
        
        # Check DB rows against API
        # note: df_db index is timestamp
        for ts, row in df_db.iterrows():
            if ts not in api_map:
                continue
                
            api_row = api_map[ts]
            # Floating point tolerance
            if abs(row['close'] - api_row['close']) > 1e-8 or abs(row['volume'] - api_row['volume']) > 1e-4:
                mismatches.append(ts)
        
        # Check API rows against DB (Missing candles)
        # Note: df_db might represent a subset if gaps exist.
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
        threshold_seconds = interval * 50 # Gap threshold to split request
        
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
            start_dt = end_dt - timedelta(days=days_back)
            
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
