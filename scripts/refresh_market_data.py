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

- Retention-aware: the API serves only the most recent ~5000 candles per
  series (4h ~ 2.3 years, 1h ~ 7 months, 15m ~ 7 weeks). A request that
  starts before that horizon returns nothing, so the fetcher jumps to the
  horizon and logs the unrecoverable gap instead of silently stopping.
- Backfill: --backfill fills history BEFORE each series' earliest stored bar
  back to --genesis (within retention); funding history has no retention cap.
- Survivorship: --include-delisted adds delisted perps (still served by the
  API) so long-history backtests are not limited to today's survivors.

Usage:
  python scripts/refresh_market_data.py                  # candles + funding
  python scripts/refresh_market_data.py --no-funding
  python scripts/refresh_market_data.py --genesis 2025-11-01
  python scripts/refresh_market_data.py --backfill --genesis 2024-06-25 \\
      --timeframes 4h,1d --crypto-only --include-delisted
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
HISTORY_DEPTH_BARS = 5000   # API retains only the most recent 5000 candles per series
RETENTION_MARGIN_BARS = 100  # land safely inside retention (it slides while we fetch)
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


def get_xyz_assets(include_delisted=False):
    """Assets on the xyz HIP-3 dex, as dex-prefixed symbols (live only by default)."""
    data = _post({"type": "metaAndAssetCtxs", "dex": HIP3_DEX})
    universe = data[0]['universe']
    out = []
    for asset in universe:
        if asset.get('isDelisted') and not include_delisted:
            continue
        name = asset['name']
        if not name.startswith(f"{HIP3_DEX}:"):
            name = f"{HIP3_DEX}:{name}"
        out.append(name)
    return out


def get_native_perp_names(include_delisted=False):
    """Names of crypto-native perps: live only (default), or live + delisted."""
    data = _post({"type": "meta"})
    return {a['name'] for a in data['universe']
            if include_delisted or not a.get('isDelisted')}


def build_spot_coin_map(db):
    """
    Map DB spot symbols (e.g. BTC_SPOT) to their API pair ids (e.g. @142).

    Spot candles are addressed by pair id, resolved via spotMeta:
    DB symbol -> token name (SPOT_INTERNAL_TO_API) -> token index ->
    USDC-quoted pair in the universe.
    """
    from src.api.hyperliquid_api import HyperliquidAPI
    token_map = HyperliquidAPI.SPOT_INTERNAL_TO_API

    meta = _post({"type": "spotMeta"})
    token_index = {t['name']: t['index'] for t in meta.get('tokens', [])}
    usdc_idx = token_index.get('USDC', 0)
    pair_by_base = {}
    for pair in meta.get('universe', []):
        toks = pair.get('tokens', [])
        if len(toks) == 2 and toks[1] == usdc_idx:
            pair_by_base[toks[0]] = pair['name']

    out = {}
    for db_sym in db.get_market_data_symbols('1h'):
        if not db_sym.endswith('_SPOT'):
            continue
        token_name = token_map.get(db_sym, db_sym.replace('_SPOT', ''))
        idx = token_index.get(token_name)
        if idx is None or idx not in pair_by_base:
            logger.warning(f"Cannot resolve spot pair for {db_sym} (token {token_name})")
            continue
        out[db_sym] = pair_by_base[idx]
    return out


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


def _fmt_ms(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')


def retention_floor_ms(interval_s, now_ms):
    """Earliest bar start the API still serves for this interval."""
    return now_ms - (HISTORY_DEPTH_BARS - RETENTION_MARGIN_BARS) * interval_s * 1000


def _fetch_range(db, symbol, timeframe, coin, start_ms, end_ms, now_ms):
    """
    Fetch closed candles whose bar START lies in [start_ms, end_ms) and store
    them. Requests that begin before the API retention horizon return an
    empty list (not an error), so the start is clamped to the horizon and
    the unrecoverable gap is logged rather than aborting the whole series.
    """
    interval_s = TIMEFRAMES[timeframe]
    interval_ms = interval_s * 1000
    floor = retention_floor_ms(interval_s, now_ms)
    if start_ms < floor:
        if end_ms <= floor:
            logger.warning(f"{symbol} {timeframe}: range {_fmt_ms(start_ms)} -> {_fmt_ms(end_ms)} "
                           f"is entirely beyond API retention (starts {_fmt_ms(floor)}); skipped")
            return 0
        logger.warning(f"{symbol} {timeframe}: bars {_fmt_ms(start_ms)} -> {_fmt_ms(floor)} are beyond "
                       f"API retention; that gap stays unfilled")
        start_ms = floor

    end_ms = min(end_ms, now_ms)
    inserted = 0
    while start_ms < end_ms and start_ms + interval_ms <= now_ms:
        req_end = min(start_ms + MAX_CANDLES_PER_REQ * interval_ms, end_ms - 1)
        candles = _post({"type": "candleSnapshot",
                         "req": {"coin": coin, "interval": timeframe,
                                 "startTime": start_ms, "endTime": req_end}})
        if not candles:
            break
        candles = [c for c in candles if c['t'] < end_ms]
        df = candles_to_df(candles, interval_s, now_ms)
        if df is None or df.empty:
            break
        db.insert_market_data(df, symbol, timeframe)
        inserted += len(df)
        new_start = int(df.index.max().timestamp() * 1000) + interval_ms
        if new_start <= start_ms:
            break  # no forward progress; bail out
        start_ms = new_start
    return inserted


def refresh_candles(db, symbol, timeframe, genesis_ms, coin=None):
    """Fetch forward from the last stored bar (or genesis) to now.
    `coin` is the API identifier when it differs from the DB symbol
    (spot pairs: BTC_SPOT is stored under that name but fetched as @N)."""
    coin = coin or symbol
    interval_s = TIMEFRAMES[timeframe]
    now_ms = int(time.time() * 1000)

    last = db.get_market_data(symbol, timeframe)
    if last is not None and not last.empty:
        start_ms = int(last.index.max().timestamp() * 1000) + interval_s * 1000
    else:
        start_ms = genesis_ms
    return _fetch_range(db, symbol, timeframe, coin, start_ms, now_ms, now_ms)


def backfill_candles(db, symbol, timeframe, genesis_ms, coin=None):
    """Fill history BEFORE the earliest stored bar back to genesis (within
    API retention). Series with no stored data are fetched from genesis."""
    existing = db.get_market_data(symbol, timeframe)
    if existing is None or existing.empty:
        return refresh_candles(db, symbol, timeframe, genesis_ms, coin=coin)
    earliest_ms = int(existing.index.min().timestamp() * 1000)
    if earliest_ms <= genesis_ms:
        return 0
    now_ms = int(time.time() * 1000)
    return _fetch_range(db, symbol, timeframe, coin or symbol, genesis_ms, earliest_ms, now_ms)


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
                 'funding': float(r['fundingRate'])} for r in records]
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


def backfill_funding(db, symbol, genesis_ms):
    """Fill funding history BEFORE the earliest stored record back to genesis
    (fundingHistory has no retention cap). Empty series fetch from genesis."""
    existing = db.get_funding_rates(symbol)
    if existing is None or existing.empty:
        return refresh_funding(db, symbol, genesis_ms)
    earliest_ms = int(existing.index.min().timestamp() * 1000)
    start_ms = genesis_ms
    inserted = 0
    while start_ms < earliest_ms:
        records = _post({"type": "fundingHistory", "coin": symbol,
                         "startTime": start_ms, "endTime": earliest_ms - 1})
        if not records:
            break
        records = [r for r in records if r['time'] < earliest_ms]
        if not records:
            break
        rows = [{'timestamp': pd.to_datetime(r['time'], unit='ms'),
                 'funding': float(r['fundingRate'])} for r in records]
        df = pd.DataFrame(rows).set_index('timestamp')
        db.insert_funding_rates(df, symbol)
        inserted += len(df)
        new_start = max(r['time'] for r in records) + 1
        if new_start <= start_ms:
            break
        start_ms = new_start
        if len(records) < 400:  # short page = reached the stored history
            break
    return inserted


def main():
    parser = argparse.ArgumentParser(description="Refresh market data from Hyperliquid")
    parser.add_argument('--genesis', default='2025-11-01',
                        help='Start date for symbols with no stored data (YYYY-MM-DD)')
    parser.add_argument('--no-funding', action='store_true')
    parser.add_argument('--funding-only', action='store_true')
    parser.add_argument('--spot-only', action='store_true',
                        help='Only refresh spot pairs already tracked in the DB')
    parser.add_argument('--symbols', help='Comma-separated override of symbols to refresh')
    parser.add_argument('--backfill', action='store_true',
                        help='Fill history BEFORE the earliest stored bar back to --genesis '
                             '(instead of fetching forward from the last stored bar)')
    parser.add_argument('--timeframes', default=','.join(TIMEFRAMES),
                        help='Comma-separated timeframes to fetch (default: all)')
    parser.add_argument('--include-delisted', action='store_true',
                        help='Include delisted perps (survivorship-free long-history universe)')
    parser.add_argument('--crypto-only', action='store_true',
                        help='Skip HIP-3 builder-dex assets')
    args = parser.parse_args()

    timeframes = [tf.strip() for tf in args.timeframes.split(',') if tf.strip()]
    unknown = [tf for tf in timeframes if tf not in TIMEFRAMES]
    if unknown:
        parser.error(f"unknown timeframes {unknown}; choose from {list(TIMEFRAMES)}")
    candle_fn = backfill_candles if args.backfill else refresh_candles
    funding_fn = backfill_funding if args.backfill else refresh_funding

    genesis_ms = int(datetime.strptime(args.genesis, '%Y-%m-%d')
                     .replace(tzinfo=timezone.utc).timestamp() * 1000)

    db = TradeDatabase()

    if args.spot_only:
        spot_map = build_spot_coin_map(db)
        logger.info(f"Refreshing {len(spot_map)} spot pairs: {sorted(spot_map)}")
        total = 0
        for db_sym, coin in sorted(spot_map.items()):
            for tf in TIMEFRAMES:
                try:
                    n = refresh_candles(db, db_sym, tf, genesis_ms, coin=coin)
                    total += n
                    if n:
                        logger.info(f"{db_sym} ({coin}) {tf}: +{n} candles")
                except Exception as e:
                    logger.error(f"{db_sym} {tf}: {e}")
        logger.info(f"Spot refresh complete: +{total} candles")
        logger.info("REFRESH COMPLETE")
        return

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(',') if s.strip()]
    else:
        exchange_natives = get_native_perp_names(include_delisted=args.include_delisted)
        if args.include_delisted:
            # Every perp the exchange has ever listed: a long-history backtest
            # universe restricted to today's survivors is biased.
            crypto = sorted(exchange_natives)
        else:
            db_symbols = set(db.get_market_data_symbols('4h'))
            # Crypto natives we already track and that still exist on the exchange
            crypto = sorted(s for s in db_symbols
                            if ':' not in s and not s.endswith('_SPOT') and s in exchange_natives)
        # All xyz HIP-3 assets (preferred builder dex: liquidity + history).
        # Other dexes (cash:, flx:, ...) are intentionally not refreshed; the
        # universe selector dedupes duplicates toward xyz anyway.
        hip3 = [] if args.crypto_only else sorted(get_xyz_assets(include_delisted=args.include_delisted))
        symbols = crypto + hip3

    logger.info(f"Refreshing {len(symbols)} symbols "
                f"({sum(1 for s in symbols if ':' not in s)} crypto, "
                f"{sum(1 for s in symbols if ':' in s)} HIP-3) "
                f"x {timeframes} "
                f"{'backfilling before earliest stored bar' if args.backfill else 'from last stored bar'} "
                f"(genesis {args.genesis})")

    total = 0
    for i, symbol in enumerate(symbols, 1):
        if args.funding_only:
            break
        for tf in timeframes:
            try:
                n = candle_fn(db, symbol, tf, genesis_ms)
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
                n = funding_fn(db, symbol, genesis_ms)
                ftotal += n
                if n:
                    logger.info(f"[{i}/{len(perps)}] funding {symbol}: +{n} records")
            except Exception as e:
                logger.error(f"[{i}/{len(perps)}] funding {symbol}: {e}")
        logger.info(f"Funding refresh complete: +{ftotal} records")

    logger.info("REFRESH COMPLETE")


if __name__ == "__main__":
    main()
