#!/usr/bin/env python3
"""
Kill switch script for the trading bot.
This script can be run independently to close all open positions.
"""

import sys
import os
import time
import signal
import json
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from src.config.settings import load_config
from src.api.hyperliquid_sdk_api import HyperliquidSDKAPI
from src.utils.leverage_manager import LeverageManager


def load_positions_from_file():
    """Load positions from a JSON file (if positions are saved there)."""
    try:
        with open('positions.json', 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_positions_to_file(positions):
    """Save positions to a JSON file."""
    with open('positions.json', 'w') as f:
        json.dump(positions, f, indent=2, default=str)


def main():
    """Kill switch main function."""
    print("🚨 TRADING BOT KILL SWITCH 🚨")
    print("=" * 40)
    
    # Load configuration
    config = load_config()
    if not config:
        print("ERROR: Failed to load configuration")
        sys.exit(1)
    
    # Initialize API
    api = HyperliquidSDKAPI(config)
    
    # Test connection
    if not api.test_connection():
        print("ERROR: Failed to connect to API")
        sys.exit(1)
    
    # Initialize leverage manager
    leverage_manager = LeverageManager(config)
    
    # Try to load positions from file
    positions = load_positions_from_file()
    
    if not positions:
        print("⚠️  No saved positions found.")
        print("⚠️  This will attempt to close any open positions via API.")
    else:
        print(f"📊 Found {len(positions)} saved positions:")
        for symbol, pos in positions.items():
            print(f"   {symbol}: {pos.get('side', 'unknown')} @ {pos.get('entry_price', 'unknown')}")
    
    print("\n⚠️  WARNING: This will close ALL open positions!")
    print("⚠️  This action cannot be undone!")
    
    # Ask for confirmation
    confirm = input("\nType 'KILL' to confirm closing all positions: ")
    
    if confirm != "KILL":
        print("Kill switch cancelled.")
        sys.exit(0)
    
    print("\n🔄 Closing all positions...")
    
    closed_count = 0
    total_pnl = 0.0
    
    # Close positions from saved data
    for symbol, position_data in positions.items():
        try:
            current_price = api.get_current_price(symbol)
            if not current_price:
                print(f"⚠️  Could not get current price for {symbol}, skipping...")
                continue
            
            # Determine close side
            close_side = 'sell' if position_data.get('side') == 'long' else 'buy'
            size = position_data.get('size', 0)
            
            if size <= 0:
                print(f"⚠️  Invalid size for {symbol}, skipping...")
                continue
            
            # Place close order
            order_result = api.place_order(symbol, close_side, size, current_price)
            
            if order_result and order_result.get('status') == 'success':
                # Calculate PnL
                entry_price = position_data.get('entry_price', 0)
                if entry_price > 0:
                    if position_data.get('side') == 'long':
                        pnl = (current_price - entry_price) * size
                    else:
                        pnl = (entry_price - current_price) * size
                    total_pnl += pnl
                
                print(f"✅ Closed {symbol}: {close_side} {size} @ {current_price}")
                closed_count += 1
            else:
                print(f"❌ Failed to close {symbol}")
                
        except Exception as e:
            print(f"❌ Error closing {symbol}: {e}")
    
    # Clear saved positions
    if positions:
        save_positions_to_file({})
    
    print(f"\n✅ Kill switch completed!")
    print(f"📊 Summary: Closed {closed_count} positions")
    print(f"💰 Total PnL: ${total_pnl:.2f}")
    
    if closed_count > 0:
        print("🎯 All positions closed successfully!")
    else:
        print("⚠️  No positions were closed. Check if positions exist.")


if __name__ == "__main__":
    main() 