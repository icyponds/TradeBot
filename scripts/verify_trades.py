#!/usr/bin/env python3
"""
Script to verify trades between local database and Hyperliquid Exchange.
"""

import sys
import os
import json
import sqlite3
from datetime import datetime, timedelta

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config.settings import load_config
from src.api.hyperliquid_api import HyperliquidAPI

def get_db_trades(db_path, hours=24):
    """Fetch recent trades from local DB."""
    if not os.path.exists(db_path):
        print(f"Error: DB not found at {db_path}")
        return []
        
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    since_time = datetime.now() - timedelta(hours=hours)
    
    query = """
    SELECT * FROM trades 
    WHERE exit_time > ? 
    ORDER BY exit_time DESC
    """
    
    cursor.execute(query, (since_time.timestamp(),))
    trades = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return trades

def main():
    config = load_config()
    api = HyperliquidAPI(config)
    
    addresses_to_check = [config['api']['wallet_address']]
    public_addr = config['api'].get('public_account_address')
    if public_addr and public_addr != config['api']['wallet_address']:
        print(f"Adding public account address to check: {public_addr}")
        addresses_to_check.append(public_addr)
        
    print(f"API Base URL: {api.base_url}")
    print(f"Private Key configured: {'YES' if config['api'].get('private_key') else 'NO'}")
    
    # 1. Fetch Exchange Fills
    print("Fetching fills from Hyperliquid API...")
    fails = []
    
    for addr in addresses_to_check:
        print(f"Fetching fills for {addr}...")
        try:
            # user_fills returns list of fills
            addr_fills = api.info.user_fills(addr)
            print(f"  Found {len(addr_fills)} fills for {addr}")
            fails.extend(addr_fills)
        except Exception as e:
            print(f"  Error fetching fills for {addr}: {e}")
            
    # Remove duplicates if any (though user_fills should be distinct per address or same if agent relationship is confusing)
    # Actually, user_fills for Main Account covers all, user_fills for Agent might be empty?
    # We will just verify against the combined list.

    print(f"Found {len(fails)} fills on exchange.")
    
    # 2. Fetch Local DB Trades
    db_path = config.get('persistence', {}).get('db_path') or 'data/trades.db'
    if not os.path.isabs(db_path):
        db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), db_path)
        
    print(f"Fetching trades from local DB: {db_path}")
    local_trades = get_db_trades(db_path)
    print(f"Found {len(local_trades)} trades in local DB (last 24h).")
    
    # 3. Compare
    print("\n--- COMPARISON ---")
    
    # Convert exchange fills to a lookup dict by time/symbol to fuzzy match
    exchange_fills_map = []
    for fill in fails:
        exchange_fills_map.append({
            'symbol': fill['coin'],
            'side': fill['side'], # 'B' or 'A' (buy/ask? need to check format) or 'open'/'close'
            'size': float(fill['sz']),
            'price': float(fill['px']),
            'time': datetime.fromtimestamp(fill['time']/1000),
            'oid': fill['oid']
        })
        
    # Check for recent local trades in exchange data
    found_count = 0
    missing_count = 0
    
    print("\nChecking recent LOCAL trades against EXCHANGE:")
    for trade in local_trades[:20]: # Check last 20
        symbol = trade['symbol']
        size = trade['size']
        price = trade['exit_price'] # Usually checking exit trades based on context
        try:
            if isinstance(trade['exit_time'], (int, float)):
                ts = datetime.fromtimestamp(trade['exit_time'])
            else:
                ts = datetime.fromisoformat(trade['exit_time'])
        except ValueError:
             # Fallback for other formats if needed, or skip
             print(f"Skipping trade with invalid time format: {trade['exit_time']}")
             continue
        
        # Simple match: look for a fill with same symbol and roughly same time (+/- 1m)
        found = False
        for fill in exchange_fills_map:
            time_diff = abs((fill['time'] - ts).total_seconds())
            if fill['symbol'] == symbol and time_diff < 120: # 2 mins tolerance
                print(f"✅ MATCH: {symbol} {trade['side']} size={size} @ {price} (DB) vs {fill['size']} @ {fill['price']} (Exch)")
                found = True
                found_count += 1
                break
        
        if not found:
            print(f"❌ MISSING: {symbol} {trade['side']} size={size} @ {price} ({ts}) caused by {trade['exit_reason']}")
            missing_count += 1
            
    print(f"\nSummary: {found_count} verified, {missing_count} missing.")

if __name__ == "__main__":
    main()
