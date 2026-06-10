"""
Refresh market_data and funding_rates in data/trades.db from the public
Hyperliquid info endpoint (no credentials required).

- Candles: 15m/1h/4h/1d for crypto-native perps already in the DB plus ALL
  live assets on the `xyz` HIP-3 dex (deepest liquidity/history of the
  builder dexes). Fetches incrementally from each series' last stored bar
  (new symbols start at --genesis). Only fully closed candles are stored.
- Funding: hourly funding history for the same perp symbols (skips spot).
- Rate-limit aware: candleSnapshot/fundingHistory are weight-20 requests
  against a 1200 weight/min IP budget, so we pace at ~1 request/second.
- Resumable: re-running continues from the last stored timestamp.

Usage:
  python scripts/refresh_market_data.py                  # candles + funding
  python scripts/refresh_market_data.py --no-funding
  python scripts/refresh_market_data.py --genesis 2025-11-01
"""

import argparse
import os
import sys
import time
import logging
from datetime import datetime, timezone

import pandas as pd
import requests

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
from src.utils.trade_database import TradeDatabase

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("refresh")

INFO_URL = "https://api.hyperliquid.xyz/info"
TIMEFRAMES = {'15m': 900, '1h': 3600, '4h': 14400, '1d': 86400}
MAX_CANDLES_PER_REQ = 4900  # API cap is 5000
REQUEST_INTERVAL = 1.05     # seconds between weight-20 requests (~1140 weight/min)
HIP3_DEX = "xyz"

_last_request_time = [0.0]


def _post(payload, retries=4):
    """Rate-limited POST with backoff per repo convention [2,10,30,60]."""
    backoff = [2, 10, 30, 60]
    for attempt in range(retries + 1):
        wait = REQUEST_INTERVAL - (time.time() - _last_request_time[0])
        if wait > 0:
            time.sleep(wait)
        _last_request_time[0] = time.time()
        try:
            resp = requests.post(INFO_URL, json=payload, timeout=(5, 30))
            if resp.status_code == 429:
                raise RuntimeError("429 rate limited")
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            if attempt < retries:
                step = backoff[min(attempt, len(backoff) - 1)]
                logger.warning(f"Request failed ({e}); retrying in {step}s")
                time.sleep(step)
            else:
                raise
    return None


def get_xyz_assets():
    """All non-delisted assets on the xyz HIP-3 dex, as dex-prefixed symbols."""
    data = _post({"type": "metaAndAssetCtxs", "dex": HIP3_DEX})
    universe = data[0]['universe']
    out = []
    for asset in universe:
        if asset.get('isDelisted'):
            continue
        name = asset['name']
        if not name.startswith(f"{HIP3_DEX}:"):
            name = f"{HIP3_DEX}:{name}"
        out.append(name)
    return out


def get_native_perp_names():
    """Names of live crypto-native perps (to filter out delisted DB symbols)."""
    data = _post({"type": "meta"})
    return {a['name'] for a in data['universe'] if not a.get('isDelisted')}


def candles_to_df(candles, interval_s, now_ms):
    """Convert API candles to the DB DataFrame format, closed bars only."""
    rows = []
    for c in candles:
        if c['t'] + interval_s * 1000 > now_ms:
            continue  # forming bar
        rows.append({
            'time': c['t'] // 1000,
            'open': float(c['o']), 'high': float(c['h']),
            'low': float(c['l']), 'close': float(c['c']),
            'volume': float(c['v']),
        })
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df['timestamp'] = pd.to_datetime(df['time'], unit='s')
    df.set_index('timestamp', inplace=True)
    return df.drop(columns=['time'])


def refresh_candles(db, symbol, timeframe, genesis_ms):
    interval_s = TIMEFRAMES[timeframe]
    now_ms = int(time.time() * 1000)

    last = db.get_market_data(symbol, timeframe)
    if last is not None and not last.empty:
        start_ms = int(last.index.max().timestamp() * 1000) + interval_s * 1000
    else:
        start_ms = genesis_ms

    inserted = 0
    while start_ms + interval_s * 1000 <= now_ms:
        end_ms = min(start_ms + MAX_CANDLES_PER_REQ * interval_s * 1000, now_ms)
        candles = _post({"type": "candleSnapshot",
                         "req": {"coin": symbol, "interval": timeframe,
                                 "startTime": start_ms, "endTime": end_ms}})
        if not candles:
            break
        df = candles_to_df(candles, interval_s, now_ms)
        if df is None or df.empty:
            break
        db.insert_market_data(df, symbol, timeframe)
        inserted += len(df)
        new_start = int(df.index.max().timestamp() * 1000) + interval_s * 1000
        if new_start <= start_ms:
            break  # no forward progress; bail out
        start_ms = new_start
    return inserted


def refresh_funding(db, symbol, genesis_ms):
    now_ms = int(time.time() * 1000)
    existing = db.get_funding_rates(symbol)
    if existing is not None and not existing.empty:
        start_ms = int(existing.index.max().timestamp() * 1000) + 3600 * 1000
    else:
        start_ms = genesis_ms

    inserted = 0
    while start_ms < now_ms:
        records = _post({"type": "fundingHistory", "coin": symbol, "startTime": start_ms})
        if not records:
            break
        rows = [{'timestamp': pd.to_datetime(r['time'], unit='ms'),
                 'funding_rate': float(r['fundingRate'])} for r in records]
        df = pd.DataFrame(rows).set_index('timestamp')
        db.insert_funding_rates(df, symbol)
        inserted += len(df)
        new_start = max(r['time'] for r in records) + 1
        if new_start <= start_ms:
            break
        start_ms = new_start
        if len(records) < 400:  # short page = reached the present
            break
    return inserted


def main():
    parser = argparse.ArgumentParser(description="Refresh market data from Hyperliquid")
    parser.add_argument('--genesis', default='2025-11-01',
                        help='Start date for symbols with no stored data (YYYY-MM-DD)')
    parser.add_argument('--no-funding', action='store_true')
    parser.add_argument('--symbols', help='Comma-separated override of symbols to refresh')
    args = parser.parse_args()

    genesis_ms = int(datetime.strptime(args.genesis, '%Y-%m-%d')
                     .replace(tzinfo=timezone.utc).timestamp() * 1000)

    db = TradeDatabase()

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(',') if s.strip()]
    else:
        live_natives = get_native_perp_names()
        db_symbols = set(db.get_market_data_symbols('4h'))
        # Crypto natives we already track and that still exist on the exchange
        crypto = sorted(s for s in db_symbols
                        if ':' not in s and not s.endswith('_SPOT') and s in live_natives)
        # All live xyz HIP-3 assets (preferred builder dex: liquidity + history).
        # Other dexes (cash:, flx:, ...) are intentionally not refreshed; the
        # universe selector dedupes duplicates toward xyz anyway.
        hip3 = sorted(get_xyz_assets())
        symbols = crypto + hip3

    logger.info(f"Refreshing {len(symbols)} symbols "
                f"({sum(1 for s in symbols if ':' not in s)} crypto, "
                f"{sum(1 for s in symbols if ':' in s)} HIP-3) "
                f"x {list(TIMEFRAMES)} from last stored bar (genesis {args.genesis})")

    total = 0
    for i, symbol in enumerate(symbols, 1):
        for tf in TIMEFRAMES:
            try:
                n = refresh_candles(db, symbol, tf, genesis_ms)
                total += n
                if n:
                    logger.info(f"[{i}/{len(symbols)}] {symbol} {tf}: +{n} candles")
            except Exception as e:
                logger.error(f"[{i}/{len(symbols)}] {symbol} {tf}: {e}")

    logger.info(f"Candle refresh complete: +{total} candles")

    if not args.no_funding:
        ftotal = 0
        perps = [s for s in symbols if not s.endswith('_SPOT')]
        for i, symbol in enumerate(perps, 1):
            try:
                n = refresh_funding(db, symbol, genesis_ms)
                ftotal += n
                if n:
                    logger.info(f"[{i}/{len(perps)}] funding {symbol}: +{n} records")
            except Exception as e:
                logger.error(f"[{i}/{len(perps)}] funding {symbol}: {e}")
        logger.info(f"Funding refresh complete: +{ftotal} records")

    logger.info("REFRESH COMPLETE")


if __name__ == "__main__":
    main()
