#!/usr/bin/env python3
"""
Emergency Position Cleanup Script

Use this script to close all open positions if the bot crashed or was killed with 'kill -9'.
This is a standalone script that doesn't require the bot to be running.

Usage:
    python scripts/close_all_positions.py [--dry-run]

Options:
    --dry-run    Show positions that would be closed without actually closing them
"""

import sys
import os
import argparse

# Add the project root to Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config.settings import load_config
from src.api.hyperliquid_api import HyperliquidAPI


def main():
    parser = argparse.ArgumentParser(description="Close all open positions on Hyperliquid")
    parser.add_argument('--dry-run', action='store_true', 
                        help="Show positions without closing them")
    args = parser.parse_args()
    
    print("=" * 60)
    print("EMERGENCY POSITION CLEANUP")
    print("=" * 60)
    
    # Load configuration
    config = load_config()
    if not config:
        print("ERROR: Failed to load configuration")
        sys.exit(1)
    
    # Initialize API
    api = HyperliquidAPI(config)
    
    if not api.test_connection():
        print("ERROR: Failed to connect to Hyperliquid API")
        sys.exit(1)
    
    print("Connected to Hyperliquid API")
    
    # Get all open positions
    positions = api.get_positions()
    
    if not positions:
        print("\n✓ No open positions found. Nothing to close.")
        return
    
    print(f"\nFound {len(positions)} open position(s):")
    print("-" * 60)
    
    for pos in positions:
        symbol = pos.get('symbol', '?')
        size = float(pos.get('size', 0))
        side = 'LONG' if size > 0 else 'SHORT'
        entry_price = float(pos.get('entry_price', 0))
        unrealized_pnl = float(pos.get('unrealized_pnl', 0))
        
        print(f"  {symbol}: {side} {abs(size)} @ ${entry_price:.2f} | PnL: ${unrealized_pnl:+.2f}")
    
    print("-" * 60)
    
    if args.dry_run:
        print("\n[DRY RUN] Would close the above positions. Run without --dry-run to execute.")
        return
    
    # Confirm before closing
    print("\n⚠️  WARNING: This will close ALL positions with market orders!")
    confirm = input("Type 'CLOSE ALL' to confirm: ")
    
    if confirm != "CLOSE ALL":
        print("Aborted.")
        return
    
    print("\nClosing positions...")
    
    success_count = 0
    fail_count = 0
    
    for pos in positions:
        symbol = pos.get('symbol', '?')
        size = float(pos.get('size', 0))
        
        if size == 0:
            continue
        
        # Close by placing opposite market order
        close_side = 'sell' if size > 0 else 'buy'
        close_size = abs(size)
        
        try:
            result = api.place_order(
                symbol=symbol,
                side=close_side,
                size=close_size,
                order_type='market',
                reduce_only=True
            )
            
            if result and result.get('status') == 'ok':
                print(f"  ✓ Closed {symbol}")
                success_count += 1
            else:
                print(f"  ✗ Failed to close {symbol}: {result}")
                fail_count += 1
                
        except Exception as e:
            print(f"  ✗ Error closing {symbol}: {e}")
            fail_count += 1
    
    print("\n" + "=" * 60)
    print(f"CLEANUP COMPLETE: {success_count} closed, {fail_count} failed")
    print("=" * 60)
    
    if fail_count > 0:
        print("\n⚠️  Some positions failed to close. Check the exchange manually!")
        sys.exit(1)


if __name__ == "__main__":
    main()

