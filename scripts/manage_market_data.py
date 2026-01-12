import argparse
import sys
import os
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.api.hyperliquid_api import HyperliquidAPI
from src.utils.trade_database import TradeDatabase
from src.utils.market_data_repair import MarketDataRepairer
from src.config.settings import load_config

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("MarketDataManager")

# ==============================================================================
# Helper functions wrapping MarketDataRepairer
# ==============================================================================

def get_date_from_str(date_str: str) -> datetime:
    """Parse YYYY-MM-DD or return now."""
    if not date_str:
        return datetime.now(timezone.utc)
    dt = datetime.strptime(date_str, '%Y-%m-%d')
    return dt.replace(tzinfo=timezone.utc)

def get_target_assets(api: HyperliquidAPI, assets_arg: str, db: TradeDatabase = None) -> list:
    """Resolve target assets list from argument."""
    if assets_arg == "ALL":
        if db:
            syms = db.get_all_symbols()
            logger.info(f"Resolved ALL assets from DB: {len(syms)} found")
            return syms
        else:
            logger.error("Cannot use ALL assets without DB connection")
            return []
            
    if not assets_arg or assets_arg == "TOP_50":
        # Logic to get top 50 by volume
        try:
            meta_and_ctxs = api.info.meta_and_asset_ctxs()
            universe = meta_and_ctxs[0]['universe']
            asset_ctxs = meta_and_ctxs[1]
            assets_with_vol = []
            for i, asset in enumerate(universe):
                ctx = asset_ctxs[i]
                vol = float(ctx.get('dayNtlVlm', 0))
                assets_with_vol.append((asset['name'], vol))
            assets_with_vol.sort(key=lambda x: x[1], reverse=True)
            return [x[0] for x in assets_with_vol[:50]]
        except Exception as e:
            logger.error(f"Error fetching top assets: {e}")
            return ['BTC', 'ETH', 'SOL', 'AVAX']
    else:
        return [s.strip() for s in assets_arg.split(',')]

def main():
    parser = argparse.ArgumentParser(description="Manage Market Data (Ingest, Repair, Fill Gaps)")
    
    parser.add_argument('mode', choices=['ingest', 'fill', 'repair', 'verify'], 
                        help="Operation mode")
    
    parser.add_argument('--symbol', type=str, help="Target symbol (e.g. BTC)")
    parser.add_argument('--assets', type=str, default="TOP_50", 
                        help="Comma-separated list of assets, 'TOP_50', or 'ALL' (requires DB)")
    
    parser.add_argument('--timeframe', type=str, 
                        help="Timeframe (1m, 5m, 15m, 1h, 4h, 1d). If omitted, runs for all standard TFs (except 1m).")
        
    parser.add_argument('--start', type=str, help="Start date YYYY-MM-DD")
    parser.add_argument('--end', type=str, help="End date YYYY-MM-DD")
    
    parser.add_argument('--db-path', type=str, default="data/trades.db",
                        help="Path to SQLite database")

    args = parser.parse_args()

    # Initialize Components
    db = TradeDatabase(args.db_path)
    
    # Init API with Config
    config_dict = load_config()
    api = HyperliquidAPI(config_dict)
    
    # Authenticate (Optional logic removed for read-only Ops)
    if not api.start():
        logger.error("Failed to start API")
        return

    # Use the Repairer Wrapper
    repairer = MarketDataRepairer(api, db)

    # Resolve Assets
    if args.symbol:
        assets = [args.symbol]
    else:
        assets = get_target_assets(api, args.assets, db)
        
    # Resolve Dates
    # Note: DB requires naive UTC
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    
    if args.start:
        start_dt = get_date_from_str(args.start).replace(tzinfo=None)
    else:
        start_dt = now_utc - timedelta(days=2) # Default 2 days
        
    if args.end:
        end_dt = get_date_from_str(args.end).replace(tzinfo=None)
    else:
        end_dt = now_utc

    # Timeframes to process
    if args.timeframe:
        timeframes = [args.timeframe]
    else:
        timeframes = ['5m', '15m', '1h', '4h', '1d']
    
    logger.info(f"Starting {args.mode.upper()} for {len(assets)} assets. Timeframes: {timeframes}")

    for symbol in assets:
        for tf in timeframes:
            try:
                if args.mode == 'repair':
                    count = repairer.verify_and_repair(symbol, tf, start_dt, end_dt, repair=True)
                    if count > 0:
                        logger.info(f"[{symbol} {tf}] Repaired {count} candles.") 
                    else:
                        logger.info(f"[{symbol} {tf}] Verified OK.")
                elif args.mode == 'verify':
                    count = repairer.verify_and_repair(symbol, tf, start_dt, end_dt, repair=False)
                    if count > 0:
                         logger.warning(f"[{symbol} {tf}] Found {count} mismatches (Dry Run).")
                    else:
                        logger.info(f"[{symbol} {tf}] Verified OK.")
                elif args.mode == 'fill':
                    logger.warning("Fill mode not fully implemented in CLI wrapper yet. Use repair.")
                elif args.mode == 'ingest':
                     repairer._ingest_range(symbol, tf, start_dt, end_dt)
                     
            except Exception as e:
                logger.error(f"Error processing {symbol} {tf}: {e}")

    logger.info("Operation Complete.")
    
    # Cleanup
    api.stop()

if __name__ == "__main__":
    main()
