#!/usr/bin/env python3
"""
Check PnL script for the trading bot.
This script shows current PnL for all open positions.
"""

import sys
import os
import json
from datetime import datetime
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from src.config.settings import load_config
from src.api.hyperliquid_sdk_api import HyperliquidSDKAPI
from src.models.trade import Position


def load_positions_from_file():
    """Load positions from JSON file."""
    try:
        with open('positions.json', 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def main():
    """Check PnL main function."""
    print("📊 POSITION PnL CHECKER")
    print("=" * 50)
    
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
    
    # Load positions
    positions_data = load_positions_from_file()
    
    if not positions_data:
        print("No open positions found.")
        sys.exit(0)
    
    print(f"Found {len(positions_data)} open positions:")
    print()
    
    # Convert to Position objects and update prices
    positions = []
    total_unrealized_pnl = 0.0
    total_position_value = 0.0
    
    for symbol, pos_data in positions_data.items():
        # Get current price
        current_price = api.get_current_price(symbol)
        if not current_price:
            print(f"⚠️  Could not get current price for {symbol}")
            continue
        
        # Create position object
        entry_time = datetime.fromisoformat(pos_data['entry_time'])
        position = Position(
            symbol=symbol,
            side=pos_data['side'],
            size=pos_data['size'],
            entry_price=pos_data['entry_price'],
            entry_time=entry_time,
            strategy=pos_data['strategy'],
            stop_loss=pos_data.get('stop_loss'),
            take_profit=pos_data.get('take_profit'),
            current_price=current_price,
        )
        
        # Calculate PnL
        unrealized_pnl = position.unrealized_pnl
        unrealized_pnl_percentage = position.unrealized_pnl_percentage
        position_value = current_price * position.size
        time_open = (datetime.now() - position.entry_time).total_seconds() / 3600
        
        if unrealized_pnl is not None:
            total_unrealized_pnl += unrealized_pnl
            total_position_value += position_value
        
        positions.append({
            'symbol': symbol,
            'side': position.side,
            'size': position.size,
            'entry_price': position.entry_price,
            'current_price': current_price,
            'unrealized_pnl': unrealized_pnl,
            'unrealized_pnl_percentage': unrealized_pnl_percentage,
            'position_value': position_value,
            'strategy': position.strategy,
            'time_open': time_open,
        })
    
    # Sort by PnL (best performing first)
    positions.sort(key=lambda x: x['unrealized_pnl'] or 0, reverse=True)
    
    # Display positions
    print(f"{'Symbol':<8} {'Side':<6} {'Size':>8} {'Entry':>10} {'Current':>10} {'PnL':>10} {'Pct':>6} {'Time':>5}")
    print("-" * 70)
    
    for pos in positions:
        pnl = pos['unrealized_pnl'] or 0
        pnl_pct = pos['unrealized_pnl_percentage'] or 0
        time_open = pos['time_open']
        
        # Color coding
        if pnl > 0:
            status = "🟢"
        elif pnl < 0:
            status = "🔴"
        else:
            status = "⚪"
        
        print(
            f"{pos['symbol']:<8} {pos['side'].upper():<6} {pos['size']:>8.3f} "
            f"${pos['entry_price']:>9.4f} ${pos['current_price']:>9.4f} "
            f"${pnl:>9.2f} {pnl_pct:>5.2f}% {time_open:>4.1f}h {status}"
        )
    
    print("-" * 70)
    print(
        f"TOTAL: {len(positions)} positions | "
        f"PnL: ${total_unrealized_pnl:>8.2f} | "
        f"Value: ${total_position_value:>8.2f}"
    )
    
    if total_position_value > 0:
        avg_pnl_pct = (total_unrealized_pnl / total_position_value) * 100
        print(f"Average PnL: {avg_pnl_pct:>6.2f}%")
    
    # Show profit/loss status
    if total_unrealized_pnl > 0:
        print("🎯 Overall: PROFIT")
    elif total_unrealized_pnl < 0:
        print("📉 Overall: LOSS")
    else:
        print("⚪ Overall: BREAKEVEN")


if __name__ == "__main__":
    main() 