#!/usr/bin/env python3
"""
Explicit Close-All-Positions Command

Flattens the entire book with reduce-only market orders, across the native
and HIP-3 (xyz/...) perp dexes. Standalone — does not require the bot to be
running, so it also doubles as crash/`kill -9` cleanup.

This is OPT-IN by design: the bot KEEPS positions on shutdown by default
(settings: system.close_on_shutdown = False) so routine restarts for code
changes don't churn the book. Run this when you genuinely want to wind down.

Usage:
    python scripts/close_all_positions.py             # flatten everything
    python scripts/close_all_positions.py --dry-run   # preview, no orders

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
from src.utils.position_close import plan_close_order, order_succeeded


def main():
    parser = argparse.ArgumentParser(description="Close all open positions on Hyperliquid")
    parser.add_argument('--dry-run', action='store_true', 
                        help="Show positions without closing them")
    args = parser.parse_args()
    
    print("=" * 60)
    print("CLOSE ALL POSITIONS" + ("  [DRY RUN]" if args.dry_run else ""))
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
        order = plan_close_order(pos)
        if order is None:
            continue  # zero/dust size, nothing to close

        try:
            result = api.place_order(
                symbol=order['symbol'],
                side=order['side'],
                size=order['size'],
                order_type=order['order_type'],
                reduce_only=order['reduce_only'],
            )

            if order_succeeded(result):
                status = result.get('status')
                print(f"  ✓ Closed {symbol} ({status})")
                success_count += 1
            else:
                print(f"  ✗ Failed to close {symbol}: {result}")
                fail_count += 1

        except Exception as e:
            print(f"  ✗ Error closing {symbol}: {e}")
            fail_count += 1

    # Cancel any resting orders (notably native protective stops). This script
    # bypasses close_position, so without this the reduce-only stops would
    # orphan on the exchange and could fire against a later re-entry.
    print("\nCancelling resting orders (native stops, etc.)...")
    try:
        cancelled = api.cancel_all_orders()
        print(f"  ✓ Cancelled {cancelled} resting order(s)")
    except Exception as e:
        print(f"  ⚠️  Could not cancel resting orders: {e}")

    print("\n" + "=" * 60)
    print(f"CLEANUP COMPLETE: {success_count} closed, {fail_count} failed")
    print("=" * 60)
    
    if fail_count > 0:
        print("\n⚠️  Some positions failed to close. Check the exchange manually!")
        sys.exit(1)


if __name__ == "__main__":
    main()


