
import sqlite3
import pandas as pd
import logging
import os
import sys

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.api.hyperliquid_api import HyperliquidAPI

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

DB_PATH = os.path.join("data", "trades.db")

def migrate_spot_assets():
    if not os.path.exists(DB_PATH):
        logger.error(f"Database not found at {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Get all distinct symbols
    cursor.execute("SELECT DISTINCT symbol FROM market_data")
    symbols = [row[0] for row in cursor.fetchall()]
    logger.info(f"Found {len(symbols)} distinct symbols in database.")

    # Mappings to migrate (API Name -> Internal Spot Name)
    mapping = HyperliquidAPI.SPOT_INTERNAL_TO_API
    # Invert mapping to get: UBTC -> BTC_SPOT
    migration_map = {v: k for k, v in mapping.items()}

    logger.info(f"Migration map size: {len(migration_map)}")

    migrated_count = 0
    
    for api_name, internal_name in migration_map.items():
        if api_name in symbols:
            logger.info(f"Migrating {api_name} -> {internal_name}...")
            
            # 1. Copy rows using INSERT ... ON CONFLICT DO NOTHING
            # This ensures we don't overwrite if BTC_SPOT already exists for that valid timestamp
            query_copy = f"""
            INSERT INTO market_data (symbol, timeframe, timestamp, open, high, low, close, volume)
            SELECT ?, timeframe, timestamp, open, high, low, close, volume
            FROM market_data
            WHERE symbol = ?
            ON CONFLICT(symbol, timeframe, timestamp) DO NOTHING;
            """
            
            try:
                cursor.execute(query_copy, (internal_name, api_name))
                inserted_rows = cursor.rowcount
                logger.info(f"  Copied {inserted_rows} rows to {internal_name}.")
                
                # 2. Delete old rows
                query_delete = "DELETE FROM market_data WHERE symbol = ?"
                cursor.execute(query_delete, (api_name,))
                deleted_rows = cursor.rowcount
                logger.info(f"  Deleted {deleted_rows} rows from {api_name}.")
                
                migrated_count += 1
                conn.commit()
                
            except Exception as e:
                logger.error(f"  Error migrating {api_name}: {e}")
                conn.rollback()

    logger.info(f"Migration complete. Migrated {migrated_count} assets.")
    
    # Final verification
    cursor.execute("SELECT DISTINCT symbol FROM market_data ORDER BY symbol")
    final_symbols = [row[0] for row in cursor.fetchall()]
    logger.info(f"Remaining symbols: {final_symbols}")
    
    conn.close()

if __name__ == "__main__":
    migrate_spot_assets()
